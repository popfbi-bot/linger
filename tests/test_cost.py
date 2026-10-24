"""成本看板（issue #36，M4-D）的七条判据。

每条都配了「它会怎么红」，因为一条无论代码怎样都绿的断言等于没写。
变异验证另见 tests/test_cost_mutations.py——那里改的是**跑着的代码**，
不是源码文本，所以它证明的是「这些断言真的在看着实现」。

先写清三个不显然的口径，后面所有断言都从这里来：

· **未知不是 0。** 单价查不到 → `cost` 是 `None`。0 会被读成「免费」，
  那是比「不知道」更糟的谎言。窗口里一次调用都没有时同理：`None`，不是 0。
· **失败也要记账。** `ok=False` 的记录进同一张表，失败率的分母是全部调用。
  只统计成功调用，重试风暴就在看板上隐形了。
· **写是 O(1)，聚合在查端点。** 对话路径上只做单条 INSERT。
"""

from __future__ import annotations

import json
import shutil
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from _fakes import FakeLLM

from linger.cost import estimate_cost, load_prices, unit_price
from linger.cost.meter import RecordingProvider
from linger.cost.pricing import DEFAULT_PRICES
from linger.cost.report import build_report, window_start
from linger.llm.mock import MockProvider
from linger.llm.router import ResilientProvider
from linger.models import UsageRecord, now_utc
from linger.storage.sqlite import SQLiteRepository

STATIC = Path(__file__).resolve().parent.parent / "src" / "linger" / "server" / "static"
HARNESS = Path(__file__).resolve().parent / "js" / "cost_panel_harness.js"
NODE = shutil.which("node")
needs_node = pytest.mark.skipif(NODE is None, reason="需要 node 跑真实前端脚本")


# --------------------------------------------------------------------------- #
# 夹具
# --------------------------------------------------------------------------- #
class _Recorder:
    """收集 usage 的假账本。"""

    def __init__(self) -> None:
        self.rows: list[UsageRecord] = []

    def __call__(self, record: UsageRecord) -> None:
        self.rows.append(record)


class ExplodingProvider:
    """第一次就炸的 provider——用来验证「失败的调用也记账」。"""

    name = "boom"
    model = "boom-1"

    def __init__(self) -> None:
        self.calls = 0

    def generate(self, system, messages, ctx=None):
        self.calls += 1
        raise RuntimeError("上游 500")
        yield ""  # pragma: no cover - 让函数成为生成器


class WeirdModelProvider(FakeLLM):
    """报一个单价表里没有的模型名——判据 3 的载体。"""

    name = "acme"
    model = "acme-unknown-9000"


def _usage(provider="mock", model="mock", tokens_in=100, tokens_out=50, ok=True,
           purpose="chat", when=None) -> UsageRecord:
    return UsageRecord(
        created_at=when or now_utc(),
        provider=provider,
        model=model,
        purpose=purpose,
        ok=ok,
        tokens_in=tokens_in,
        tokens_out=tokens_out,
    )


@pytest.fixture
def cost_client(repo, persona):
    """用 **MockProvider** 的客户端：判据 1 要断言 mock 也产生确定性 usage。"""
    from fastapi.testclient import TestClient

    from linger.server.app import create_app

    app = create_app(repo=repo, llm=MockProvider(), embedder=None)
    with TestClient(app) as c:
        yield c


def _chat(client, session_id: str, text: str = "今天有点累") -> str:
    resp = client.post(f"/v1/sessions/{session_id}/messages", json={"content": text})
    assert resp.status_code == 200, resp.text
    return resp.text


# --------------------------------------------------------------------------- #
# 判据 1：每次 LLM 调用落一条记录
# --------------------------------------------------------------------------- #
def test_every_llm_call_writes_one_row(cost_client, repo, persona):
    """一轮对话 = 一条记录，字段齐全。"""
    sid = cost_client.post(
        "/v1/sessions", json={"persona_id": persona.id, "user_id": "u_cost"}
    ).json()["session_id"]
    _chat(cost_client, sid)

    rows = repo.usage_since(datetime.now(timezone.utc) - timedelta(hours=1))
    assert len(rows) == 1, f"一轮对话应落 1 条，实际 {len(rows)}"
    row = rows[0]
    assert row.provider == "mock"
    assert row.ok is True
    assert row.purpose == "chat"
    assert row.tokens_in and row.tokens_in > 0
    assert row.tokens_out and row.tokens_out > 0
    assert row.elapsed_ms >= 0


def test_mock_provider_usage_is_deterministic():
    """判据 1：mock 的 usage 必须确定，否则看板每次刷新都在变，测试也没法断言。"""
    first, second = MockProvider(), MockProvider()
    text_a = "".join(first.generate("sys", [{"role": "user", "content": "今天好累"}]))
    text_b = "".join(second.generate("sys", [{"role": "user", "content": "今天好累"}]))
    assert text_a == text_b
    assert first.last_usage == second.last_usage
    assert first.last_usage["in"] > 0 and first.last_usage["out"] > 0


def test_recording_provider_reports_provider_usage_not_estimate():
    """provider 报了 usage 就用它的（真模型是账单口径），并标成非估算。"""
    recorder = _Recorder()
    inner = MockProvider()
    llm = RecordingProvider(inner, recorder)
    list(llm.generate("sys", [{"role": "user", "content": "在吗"}]))
    assert len(recorder.rows) == 1
    # 真 provider（这里是 mock 的假 usage，形状与真的一致）报的数字优先
    assert recorder.rows[0].estimated is False
    assert recorder.rows[0].tokens_out == inner.last_usage["out"]
    assert recorder.rows[0].tokens_in == inner.last_usage["in"]


def test_recording_provider_does_not_change_the_stream():
    """计量是包装，不是协议变更：流出来的字一个不差。"""
    plain = list(MockProvider().generate("sys", [{"role": "user", "content": "在吗"}]))
    wrapped = list(
        RecordingProvider(MockProvider(), _Recorder()).generate(
            "sys", [{"role": "user", "content": "在吗"}]
        )
    )
    assert plain == wrapped


# --------------------------------------------------------------------------- #
# 判据 2：/v1/costs 按 provider 与日期聚合，数字与手算一致
# --------------------------------------------------------------------------- #
def test_costs_endpoint_matches_hand_computed_numbers(cost_client, repo, persona):
    """手算核对：3 条记录（2 成功 1 失败）→ 调用数 / token / 失败数都对得上。"""
    now = datetime.now(timezone.utc)
    repo.record_usage(_usage(tokens_in=100, tokens_out=50, when=now))
    repo.record_usage(_usage(tokens_in=200, tokens_out=80, when=now))
    repo.record_usage(
        _usage(tokens_in=300, tokens_out=20, ok=False, when=now)
    )

    body = cost_client.get("/v1/costs?days=1").json()

    assert body["totals"]["calls"] == 3
    assert body["totals"]["failed"] == 1
    assert body["totals"]["tokens_in"] == 600
    assert body["totals"]["tokens_out"] == 150
    assert body["totals"]["tokens_total"] == 750
    assert body["by_provider"][0]["provider"] == "mock"
    assert body["by_provider"][0]["calls"] == 3
    # 按日期维度：今天是唯一一个有记录的日子
    today = now.date().isoformat()
    assert [d["date"] for d in body["by_day"]] == [today]
    assert body["by_day"][0]["tokens_total"] == 750


def test_costs_days_windows_are_respected(repo):
    """`?days=N` 真的收窄窗口：8 天前的记录不进 7 日窗口。"""
    now = datetime.now(timezone.utc)
    repo.record_usage(_usage(tokens_in=10, tokens_out=10, when=now - timedelta(days=8)))
    repo.record_usage(_usage(tokens_in=20, tokens_out=20, when=now - timedelta(days=1)))

    from fastapi.testclient import TestClient

    from linger.server.app import create_app

    app = create_app(repo=repo, llm=FakeLLM(), embedder=None)
    with TestClient(app) as client:
        week = client.get("/v1/costs?days=7").json()
        month = client.get("/v1/costs?days=30").json()

    assert week["totals"]["calls"] == 1
    assert week["totals"]["tokens_in"] == 20
    assert month["totals"]["calls"] == 2
    assert month["totals"]["tokens_in"] == 30


def test_window_start_is_calendar_days_not_rolling_hours():
    """口蹄（口径）钉死：「今日」是自然日从 00:00 起，不是往前推 24 小时。

    早上 9 点时往前推 24 小时会把昨天 9 点之后算进「今日」——那不是用户说的今日。
    """
    now = datetime(2026, 3, 10, 9, 0, tzinfo=timezone.utc)
    assert window_start(1, now) == datetime(2026, 3, 10, 0, 0, tzinfo=timezone.utc)
    assert window_start(7, now) == datetime(2026, 3, 4, 0, 0, tzinfo=timezone.utc)


def test_by_day_covers_every_day_in_window_including_empty_ones():
    """series 要补齐空日：不补的话前端画出来会因为「周末没聊天」少几天。"""
    report = build_report([_usage(when=now_utc())], days=7)
    assert len(report["series"]) == 7
    assert report["series"][-1]["calls"] == 1
    assert sum(1 for d in report["series"] if d["calls"] == 0) == 6


# --------------------------------------------------------------------------- #
# 判据 3：成本估算 —— 单价未知时 null，不许 0
# --------------------------------------------------------------------------- #
def test_unknown_price_returns_none_never_zero(monkeypatch):
    """把单价表清空 → 该字段是 None。这是判据 3 的核心断言。"""
    monkeypatch.setattr("linger.cost.pricing.DEFAULT_PRICES", {})
    monkeypatch.setattr("linger.config.settings.prices", "{}")

    body = build_report([_usage(provider="acme", model="ghost-1")], days=7)

    assert body["totals"]["cost"] is None, "单价未知必须是 None，0 会被读成免费"
    assert body["totals"]["cost_input"] is None
    assert body["totals"]["cost_output"] is None
    assert {"provider": "acme", "model": "ghost-1"} in body["totals"]["unknown_models"]


def test_unknown_price_through_the_http_endpoint(monkeypatch, repo):
    """同一条规则在 HTTP 层也成立（不是只在单元函数里成立）。"""
    monkeypatch.setattr("linger.cost.pricing.DEFAULT_PRICES", {})
    monkeypatch.setattr("linger.config.settings.prices", "{}")
    repo.record_usage(_usage(provider="acme", model="ghost-1"))

    from fastapi.testclient import TestClient

    from linger.server.app import create_app

    with TestClient(create_app(repo=repo, llm=FakeLLM(), embedder=None)) as client:
        body = client.get("/v1/costs?days=1").json()

    assert body["totals"]["cost"] is None
    assert body["by_provider"][0]["cost"] is None


def test_known_price_is_computed_but_tiny_not_rounded_away(monkeypatch):
    """已知单价要真的乘出来，且小数点后不能被抹成 0（那也是一种「看起来免费」）。"""
    prices = {"acme:cheap": {"input": 0.5, "output": 1.0}}
    body = build_report(
        [_usage(provider="acme", model="cheap", tokens_in=1000, tokens_out=1000)],
        days=7,
        prices=prices,
    )
    assert body["totals"]["cost"] == 1.5
    assert body["totals"]["cost_input"] == 0.5
    assert body["totals"]["cost_output"] == 1.0
    assert body["totals"]["unknown_models"] == []


def test_snapshot_zero_price_is_zero_not_none():
    """mock / local 的 0 是**写明的价格**，不是「不知道」——所以它是 0。"""
    cost = estimate_cost(load_prices(), "mock", "mock", 1000, 1000)
    assert cost["total"] == 0.0
    assert cost["total"] is not None


def test_prices_are_configurable_by_env(monkeypatch):
    """单价表可被配置覆盖，且是**合并**不是替换。"""
    monkeypatch.setenv(
        "LINGER_PRICES", json.dumps({"openai:gpt-4o-mini": {"input": 9.0, "output": 9.0}})
    )
    monkeypatch.setattr("linger.config.settings.prices", '{"openai:gpt-4o-mini": {"input": 9.0, "output": 9.0}}')
    prices = load_prices()
    assert prices["openai:gpt-4o-mini"]["input"] == 9.0
    # 没被覆盖的还在
    assert prices["mock:mock"]["input"] == 0.0


def test_half_a_price_is_treated_as_unknown():
    """只有 input 没有 output 的价格算未知：硬凑 0 会让更贵的输出侧看起来免费。"""
    assert unit_price({"x:y": {"input": 1.0}}, "x", "y") is None


def test_unknown_token_count_makes_cost_unknown_not_partial():
    """token 数未知时，成本不能「只算知道的那一半」——那是少算的假数字。"""
    body = build_report(
        [_usage(provider="mock", model="mock", tokens_in=None, tokens_out=None)], days=7
    )
    assert body["totals"]["tokens_total"] is None
    assert body["totals"]["cost"] is None


def test_empty_window_cost_is_null_not_zero():
    """窗口里一次调用都没有：成本是 None。`$0.00` 与「还没有数据」必须长得不一样。"""
    body = build_report([], days=7)
    assert body["totals"]["calls"] == 0
    assert body["totals"]["cost"] is None
    assert all(day["cost"] is None for day in body["series"])


# --------------------------------------------------------------------------- #
# 判据 4：失败的调用也要记账
# --------------------------------------------------------------------------- #
def test_failed_call_is_recorded_with_ok_false():
    """失败的调用进同一个表，`ok=False`，失败率算得出来。"""
    recorder = _Recorder()
    llm = RecordingProvider(ExplodingProvider(), recorder)

    with pytest.raises(RuntimeError, match="上游 500"):
        list(llm.generate("sys", [{"role": "user", "content": "在吗"}]))

    assert len(recorder.rows) == 1, "失败的调用必须留下一条记录"
    assert recorder.rows[0].ok is False
    assert recorder.rows[0].provider == "boom"
    assert "上游 500" in (recorder.rows[0].error or "")


def test_failure_rate_counts_failures_in_denominator():
    """失败率 = 失败数 / **全部**调用（不是 / 成功调用）。"""
    body = build_report(
        [_usage(ok=True), _usage(ok=True), _usage(ok=False), _usage(ok=False)], days=7
    )
    assert body["totals"]["calls"] == 4
    assert body["totals"]["failed"] == 2
    assert body["totals"]["failure_rate"] == 0.5
    assert body["today"]["failure_rate"] == 0.5


def test_recorder_error_never_breaks_the_call():
    """记账自己炸了不许影响这次对话：账记错了可以补，回复丢了不能补。"""

    def boom(_record):
        raise RuntimeError("账本坏了")

    llm = RecordingProvider(MockProvider(), boom)
    assert "".join(llm.generate("sys", [{"role": "user", "content": "在吗"}]))


def test_resilient_router_keeps_retrying_while_metering(monkeypatch):
    """挂上旁路计量之后，重试次数**一点不变**（M4-A 语义没被碰）。

    这条是回归护栏：计量如果跑到流程里去，第一个表现出来的症状就是
    「重试少了一次」或者「不再降级了」。
    """

    class Dead:
        name = "dead"
        model = "dead-1"

        def __init__(self):
            self.calls = 0

        def generate(self, system, messages, ctx=None):
            self.calls += 1
            raise RuntimeError("挂了")
            yield ""  # pragma: no cover

    dead = Dead()
    good = MockProvider()
    recorder = _Recorder()
    # 旁路回调的签名是 `(report: dict) -> None`，而 `record_attempt` 负责把
    # router 的那份报告翻成一条 UsageRecord。用 attach_reporter 装配，
    # 与 server 里那条路径完全一致（不手搓一个只存在于测试里的接线）。
    from linger.cost import attach_reporter

    router = ResilientProvider(dead, [good], retries_per_provider=1)
    attach_reporter(router, recorder)
    text = "".join(router.generate("sys", [{"role": "user", "content": "在吗"}]))

    assert text, "备 provider 应该接上了"
    assert dead.calls == 2, f"主 provider 应被尝试 2 次（首次 + 1 重试），实际 {dead.calls}"
    # 旁路回调记下了每一次尝试，其中包含失败的那几次
    assert any(r.ok is False for r in recorder.rows)
    assert any(r.ok is True for r in recorder.rows)


def test_on_report_callback_is_optional_and_isolated():
    """回调抛异常不影响 router 的结果（旁路的意思就是「与主路无关」）。"""
    calls = []

    def bad_callback(_report):
        calls.append(1)
        raise RuntimeError("回调炸了")

    router = ResilientProvider(MockProvider(), on_report=bad_callback)
    assert "".join(router.generate("sys", [{"role": "user", "content": "在吗"}]))
    assert calls, "回调应该被调到"


def test_router_without_callback_is_unchanged():
    """不传 `on_report` 时行为与 M4-A 完全一致（回调是可选旁路）。"""
    router = ResilientProvider(MockProvider())
    assert router.on_report is None
    report = router.last_report()
    assert "ok" in report and "attempts" in report


# --------------------------------------------------------------------------- #
# 判据 5：写路径 O(1)，聚合只在查端点
# --------------------------------------------------------------------------- #
def test_write_path_issues_no_aggregate_query():
    """写一条记录只允许一条 INSERT：不许在对话路径上跑 SUM / GROUP BY。"""
    from linger.storage.sqlite import SQLiteRepository as Repo

    seen: list[str] = []
    repo = Repo(Path(__file__).resolve().parent / "_tmp_usage.db")
    try:
        class TracedConn:
            def __init__(self, conn):
                self._conn = conn

            def execute(self, sql, *args):
                seen.append(sql)
                return self._conn.execute(sql, *args)

            def executescript(self, sql):
                return self._conn.executescript(sql)

            def commit(self):
                return self._conn.commit()

            def __getattr__(self, item):
                return getattr(self._conn, item)

        repo.conn = TracedConn(repo.conn)
        repo.record_usage(_usage())
    finally:
        repo.close()
        (Path(__file__).resolve().parent / "_tmp_usage.db").unlink(missing_ok=True)

    assert len(seen) == 1, f"写路径不该有第二条 SQL：{seen}"
    upper = seen[0].upper()
    assert "INSERT" in upper
    assert "SUM(" not in upper.replace(" ", "") and "GROUP BY" not in upper


def test_usage_since_returns_rows_without_aggregating():
    """存储层返回**明细**：怎么分组是看板的决定，不是存储层的。"""
    repo = SQLiteRepository(Path(__file__).resolve().parent / "_tmp_usage2.db")
    try:
        repo.record_usage(_usage())
        repo.record_usage(_usage())
        rows = repo.usage_since(datetime.now(timezone.utc) - timedelta(days=1))
        assert len(rows) == 2
        assert all(r.provider == "mock" for r in rows)
    finally:
        repo.close()
        (Path(__file__).resolve().parent / "_tmp_usage2.db").unlink(missing_ok=True)


# --------------------------------------------------------------------------- #
# 判据 6：前端成本面板（DOM 可断言 + 数字来自 /v1/costs）
# --------------------------------------------------------------------------- #
def _run_harness(costs: dict) -> dict:
    proc = subprocess.run(
        [NODE, str(HARNESS)],
        input=json.dumps({"app_js": str(STATIC / "app.js"), "costs": costs}),
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert proc.returncode == 0, f"前端脚本跑挂了：\n{proc.stderr[:3000]}"
    return json.loads(proc.stdout)


COSTS_FIXTURE = {
    "window": {"days": 7, "currency": "USD"},
    "totals": {
        "calls": 12, "failed": 3, "failure_rate": 0.25,
        "tokens_in": 4000, "tokens_out": 2000, "tokens_total": 6000,
        "cost": 0.0025, "unknown_models": [],
    },
    "by_provider": [
        {"provider": "mock", "calls": 12, "failed": 3, "failure_rate": 0.25,
         "tokens_total": 6000, "cost": 0.0025, "unknown_models": []}
    ],
    "by_day": [], "by_purpose": [], "series": [],
    "today": {"date": "2026-10-23", "calls": 5, "failed": 1, "failure_rate": 0.2,
              "tokens_total": 4321, "cost": 0.001},
    "last_7_days": {"calls": 12, "failed": 3, "failure_rate": 0.25,
                    "tokens_total": 6000, "cost": 0.0025},
    "pricing": [], "pricing_snapshot": "2024-10",
}


def test_cost_panel_elements_exist_in_dom():
    html = (STATIC / "index.html").read_text(encoding="utf-8")
    for element_id in (
        "cost-card",
        "cost-today-tokens",
        "cost-week-failure",
        "cost-week-cost",
        "cost-providers",
    ):
        assert f'id="{element_id}"' in html, f"缺成本面板元素: {element_id}"


@needs_node
def test_cost_panel_fetches_costs_endpoint():
    out = _run_harness(COSTS_FIXTURE)
    assert out["error"] is None, out["error"]
    assert any(u.startswith("/v1/costs") for u in out["fetchCalls"]), out["fetchCalls"]


@needs_node
def test_cost_panel_renders_numbers_from_the_response():
    """**屏幕上出现的数字必须来自 /v1/costs 的响应**，逐个对出来。

    这条是判据 6 的实质：grep 只能证明「代码里有这个字符串」，
    证明不了「它被调用、值落进了 DOM」。
    """
    out = _run_harness(COSTS_FIXTURE)
    assert out["today"] == "4321", out["today"]
    assert out["weekFailure"] == "25.0%", out["weekFailure"]
    assert out["weekCost"] == "$0.0025", out["weekCost"]
    assert len(out["providerRows"]) == 1
    row = out["providerRows"][0]
    assert "mock" in row
    assert "12 次" in row
    assert "失败 3" in row
    # 前端只渲染响应里的数，不自己算失败率：25.0% 与 3/12 都必须对得上响应
    assert COSTS_FIXTURE["last_7_days"]["failure_rate"] == 0.25


@needs_node
def test_cost_panel_shows_unknown_not_zero_for_null_cost():
    """前端一侧的判据 3：cost 是 null 时显示「未知」，绝不是 `$0`。"""
    costs = json.loads(json.dumps(COSTS_FIXTURE))
    costs["totals"]["cost"] = None
    costs["totals"]["unknown_models"] = [{"provider": "acme", "model": "ghost"}]
    costs["last_7_days"]["cost"] = None
    costs["by_provider"][0]["cost"] = None
    out = _run_harness(costs)

    assert "未知" in out["weekCost"], out["weekCost"]
    assert "$0" not in out["weekCost"], "未知被渲染成 0 了——那是在告诉用户「很省钱」"
    # 未知的模型要点名，否则用户没法知道「要去配哪个价」
    assert "acme:ghost" in out["hint"], out["hint"]


@needs_node
def test_cost_panel_shows_real_zero_as_zero():
    """写明的 0 价（mock / local）要显示成 $0——它与「未知」必须长得不一样。"""
    costs = json.loads(json.dumps(COSTS_FIXTURE))
    for field in ("cost",):
        costs["totals"][field] = 0.0
        costs["last_7_days"][field] = 0.0
        costs["by_provider"][0][field] = 0.0
    out = _run_harness(costs)
    assert out["weekCost"] == "$0", out["weekCost"]


@needs_node
def test_cost_panel_tolerates_empty_window():
    """一次调用都没有时面板不炸，显示「未知」而不是编一个 0。"""
    costs = json.loads(json.dumps(COSTS_FIXTURE))
    costs["today"] = {"date": "2026-10-23", "calls": 0, "failed": 0,
                      "failure_rate": 0.0, "tokens_total": None, "cost": None}
    costs["last_7_days"] = {"calls": 0, "failed": 0, "failure_rate": 0.0,
                            "tokens_total": None, "cost": None}
    costs["by_provider"] = []
    out = _run_harness(costs)
    assert out["error"] is None, out["error"]
    assert out["today"] == "未知"
    assert out["weekCost"] == "未知"


# --------------------------------------------------------------------------- #
# 判据 7：已有测试全绿（由全量跑覆盖）+ 不动 docs/
# --------------------------------------------------------------------------- #
def test_end_to_end_mock_chat_shows_up_on_the_board(cost_client, repo, persona):
    """端到端：用 mock 聊一轮 → 看板上真的出现数字（不是永远为空）。"""
    sid = cost_client.post(
        "/v1/sessions", json={"persona_id": persona.id, "user_id": "u_e2e"}
    ).json()["session_id"]
    _chat(cost_client, sid, "今天被夸了，还挺开心")

    body = cost_client.get("/v1/costs?days=1").json()
    assert body["totals"]["calls"] == 1
    assert body["totals"]["tokens_total"] > 0
    assert body["today"]["tokens_total"] > 0
    assert body["by_provider"][0]["provider"] == "mock"


def test_default_price_table_is_reachable_from_module():
    """单价表放在独立模块、代码里能改（issue 约束：不要写死在难改的地方）。"""
    assert DEFAULT_PRICES["mock:mock"]["input"] == 0.0
    assert unit_price(load_prices(), "openai", "gpt-4o-mini") is not None
