"""变异验证：这些断言真的在看着实现，还是无论如何都会绿？

做法与 `test_postgres_repo.py` 末尾那组一致：**改跑着的代码，再跑一遍判据**。
改源码文本再跑全量当然更彻底，但它证明的是「某次手工操作有效」；
改运行期对象证明的是「这条断言本身有效」——后者才能放进 CI。

四个方向，对应 issue 要求的「至少两条」并多加两个：

    M1 计量回调改 no-op      → 判据 1、2 红（账本上一个字都没有）
    M2 单价未知返回 0        → 判据 3 红（「不知道」被渲染成「免费」）
    M3 失败调用不记账        → 判据 4 红（重试风暴隐形，失败率恒为 0）
    M4 写路径做聚合          → 判据 5 红（对话关键路径上跑全表 SUM）

每个变异都在 `finally` 里还原，且都断言「还原之后判据重新变绿」——
不然一个只红不绿的测试环境会让人怀疑整套东西。
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from linger.cost import meter as meter_mod
from linger.cost.meter import RecordingProvider
from linger.cost.report import build_report
from linger.llm.mock import MockProvider
from linger.models import UsageRecord, now_utc
from linger.storage.sqlite import SQLiteRepository


class _Recorder:
    def __init__(self) -> None:
        self.rows: list[UsageRecord] = []

    def __call__(self, record: UsageRecord) -> None:
        self.rows.append(record)


class _Boom:
    name = "boom"
    model = "boom-1"

    def generate(self, system, messages, ctx=None):
        raise RuntimeError("上游 500")
        yield ""  # pragma: no cover


class _Weird(MockProvider):
    name = "acme"
    model = "ghost-1"


# --------------------------------------------------------------------------- #
# M1：计量回调改成 no-op
# --------------------------------------------------------------------------- #
def test_mutation_noop_recorder_breaks_criterion_1_and_2():
    """把记账改成什么都不做 → 判据 1（有记录）与判据 2（聚合有数）都必须红。"""
    recorder = _Recorder()
    llm = RecordingProvider(MockProvider(), recorder)
    saved = llm._record

    try:
        llm._record = lambda *a, **k: None  # 变异：计量变 no-op
        list(llm.generate("sys", [{"role": "user", "content": "在吗"}]))

        # 判据 1 在这一刻应该红：
        assert recorder.rows == [], "no-op 之后还记了账？那这个变异没生效"
        # 判据 2 在这一刻应该红：账本是空的，看板只能报 0 次调用
        report = build_report(recorder.rows, days=7)
        assert report["totals"]["calls"] == 0
        assert report["by_provider"] == []
    finally:
        llm._record = saved

    # 还原之后判据重新变绿——证明刚才的红是这个变异造成的
    list(llm.generate("sys", [{"role": "user", "content": "在吗"}]))
    assert len(recorder.rows) == 1
    assert build_report(recorder.rows, days=7)["totals"]["calls"] == 1


# --------------------------------------------------------------------------- #
# M2：单价未知返回 0
# --------------------------------------------------------------------------- #
def test_mutation_unknown_price_as_zero_breaks_criterion_3():
    """把「未知 → None」改成「未知 → 0」 → 判据 3 红。

    变异打在 `report.estimate_cost`（report 是 `from ... import` 拿到的引用，
    改 `pricing` 模块里那个名字对它无效——这本身就是一处值得记住的细节：
    import 之后的引用是**绑定**，不是指针）。
    """
    import linger.cost.report as report_mod

    saved = report_mod.estimate_cost

    def mutant(prices, provider, model, tokens_in, tokens_out):
        out = saved(prices, provider, model, tokens_in, tokens_out)
        if out["total"] is None:
            # 变异：未知就当作免费（现实中那行"懒得处理 None，填 0 吧"的写法）
            return {"input": 0.0, "output": 0.0, "total": 0.0, "currency": "USD"}
        return out

    record = UsageRecord(
        created_at=now_utc(), provider="acme", model="ghost-1", tokens_in=10, tokens_out=10
    )
    empty_prices: dict = {}

    # 变异前：判据 3 绿
    assert build_report([record], days=7, prices=empty_prices)["totals"]["cost"] is None

    try:
        report_mod.estimate_cost = mutant
        mutated = build_report([record], days=7, prices=empty_prices)
        assert mutated["totals"]["cost"] == 0.0, "变异没生效"
        # 判据 3 的断言在这一刻会红：它要求 None，而变异给出了 0（=「免费」）
        assert mutated["totals"]["cost"] is not None
    finally:
        report_mod.estimate_cost = saved

    # 还原后重新绿
    assert build_report([record], days=7, prices=empty_prices)["totals"]["cost"] is None


# --------------------------------------------------------------------------- #
# M3：失败的调用不记账
# --------------------------------------------------------------------------- #
def test_mutation_skipping_failures_breaks_criterion_4():
    """只在成功时记账 → 失败率恒等于 0，重试风暴在看板上隐形。"""
    recorder = _Recorder()
    llm = RecordingProvider(_Boom(), recorder)
    saved = llm._record

    def only_on_success(messages, text, error, elapsed):
        if error is None:  # 变异：失败就不记
            saved(messages, text, error, elapsed)

    try:
        llm._record = only_on_success
        with pytest.raises(RuntimeError):
            list(llm.generate("sys", [{"role": "user", "content": "在吗"}]))
        assert recorder.rows == [], "失败被记下来了？那这个变异没生效"
    finally:
        llm._record = saved

    with pytest.raises(RuntimeError):
        list(llm.generate("sys", [{"role": "user", "content": "在吗"}]))
    assert len(recorder.rows) == 1
    assert recorder.rows[0].ok is False
    assert build_report(recorder.rows, days=7)["totals"]["failure_rate"] == 1.0


# --------------------------------------------------------------------------- #
# M4：写路径做聚合
# --------------------------------------------------------------------------- #
def test_mutation_aggregate_on_write_path_breaks_criterion_5(tmp_path):
    """在写路径上塞一条 SUM → 判据 5（写只允许一条 INSERT）必须红。"""
    repo = SQLiteRepository(tmp_path / "m4.db")
    seen: list[str] = []

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

    record = UsageRecord(created_at=now_utc(), provider="mock", model="mock",
                         tokens_in=1, tokens_out=1)
    saved = repo.record_usage

    def mutant(r):
        saved(r)
        # 变异：每次写都顺手算一遍总额（现实里最自然的那条捷径）
        repo.conn.execute("SELECT SUM(tokens_in) FROM llm_usage").fetchall()

    try:
        repo.conn = TracedConn(repo.conn)
        repo.record_usage = mutant
        repo.record_usage(record)
        assert len(seen) == 2, f"变异没生效，只看到 {seen}"
        # 判据 5 的断言在这一刻会红：它要求 len(seen) == 1
        assert len(seen) != 1
    finally:
        repo.record_usage = saved
        repo.close()


def test_usage_record_shape_is_stable():
    """兜底：`UsageRecord` 的字段名是接口的一部分（前端与文档都按它写）。"""
    assert set(UsageRecord.model_fields) >= {
        "created_at", "provider", "model", "purpose", "ok",
        "elapsed_ms", "tokens_in", "tokens_out",
    }
    # `meter` 里的用途常量与写进库的值必须是同一批
    assert meter_mod.PURPOSE_CHAT == "chat"


def test_usage_since_boundary_is_inclusive(tmp_path):
    """窗口边界含起点：卡在整点上的那条记录不能因为时区/比较丢在半路。"""
    repo = SQLiteRepository(tmp_path / "edge.db")
    try:
        edge = datetime.now(timezone.utc).replace(microsecond=0) - timedelta(days=1)
        repo.record_usage(UsageRecord(created_at=edge, provider="mock", model="mock"))
        assert len(repo.usage_since(edge)) == 1
        assert repo.usage_since(edge + timedelta(seconds=1)) == []
    finally:
        repo.close()
