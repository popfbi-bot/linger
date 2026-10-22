"""M4-A 判据 9 与 HTTP 层错误码：`/v1/llm/health` + 503 契约。

三种端点两种语义：
    SSE（/v1/sessions/…/messages）：首 chunk 前全挂 → 503；已经吐字后断 → error 事件
    普通 JSON（人格生成 / 蒸馏 / 日记）→ 503

判据 9 的另一半（`LINGER_LLM=mock` 时 circuits 为空）用注入 mock provider 验，
不去改全局 settings——这个端点读的是**装配时**的 provider，而不是配置字面量。
"""

from __future__ import annotations

import json
import tempfile
import time
from collections.abc import Iterator
from pathlib import Path

import pytest
from _fakes import FakeEmbedder
from fastapi.testclient import TestClient

from linger.llm.base import LLMTimeoutError, LLMUnavailableError
from linger.llm.circuit import CircuitBreaker
from linger.llm.mock import MockProvider
from linger.llm.router import ResilientProvider
from linger.server.app import create_app
from linger.storage.sqlite import SQLiteRepository

# --------------------------------------------------------------------------- #
# 假件与夹具（本文件独立，不动 tests/ 下既有文件）
# --------------------------------------------------------------------------- #


class DeadProvider:
    """恒失败：连不上。"""

    name = "dead"

    def __init__(self) -> None:
        self.calls = 0

    def generate(self, system, messages, ctx=None) -> Iterator[str]:
        self.calls += 1
        raise ConnectionError("连不上")
        yield ""  # pragma: no cover - 让函数成为生成器


class SlowProvider:
    """永不吐字：首 chunk 超时。"""

    name = "slow"

    def generate(self, system, messages, ctx=None) -> Iterator[str]:
        time.sleep(5)
        yield ""


class BrokenMidStream:
    """先吐 2 个 chunk 再断。"""

    name = "broken"

    def generate(self, system, messages, ctx=None) -> Iterator[str]:
        yield "前半句"
        yield "还在说"
        raise ConnectionError("断了")


@pytest.fixture
def repo() -> Iterator[SQLiteRepository]:
    with tempfile.TemporaryDirectory() as tmp:
        r = SQLiteRepository(Path(tmp) / "test.db")
        yield r
        r.close()


@pytest.fixture
def persona(repo: SQLiteRepository):
    from linger.persona.templates import load_template

    card = load_template("linxi")
    repo.save_persona(card)
    return card


class _App:
    """预装配的 TestClient（已进入 lifespan，app.state 可用）。

    `TestClient` 不当上下文管理器用时不会跑 lifespan，`app.state.llm` 就取不到。
    """

    def __init__(self, repo, llm) -> None:
        self.app = create_app(repo=repo, llm=llm, embedder=FakeEmbedder())
        self._client = TestClient(self.app)

    def __enter__(self) -> TestClient:
        self._client.__enter__()
        return self._client

    def __exit__(self, *exc) -> None:
        self._client.__exit__(*exc)


def _session(client: TestClient, persona) -> str:
    resp = client.post("/v1/sessions", json={"persona_id": persona.id, "user_id": "u_health"})
    assert resp.status_code == 201
    return resp.json()["session_id"]


def _events(resp) -> list[tuple[str, dict]]:
    out: list[tuple[str, dict]] = []
    event_type: str | None = None
    for line in resp.iter_lines():
        if line.startswith("event: "):
            event_type = line[len("event: ") :].strip()
        elif line.startswith("data: "):
            out.append((event_type, json.loads(line[len("data: ") :])))
    return out


# --------------------------------------------------------------------------- #
# 判据 9：健康端点
# --------------------------------------------------------------------------- #


def test_health_shape_for_resilient(repo, persona):
    """韧性层在用时，结构完整（字段一个不少）。"""
    rec: list[str] = []
    primary = MockProvider()
    backup = MockProvider()
    llm = ResilientProvider(primary, [backup], first_chunk_timeout=5.0, total_timeout=30.0)
    llm.providers[0].name = "openai"  # 只为断言名字，不改行为
    with _App(repo, llm) as client:

        body = client.get("/v1/llm/health").json()
        assert body["provider"] == "resilient"
        assert body["primary"] == "openai"
        assert body["fallbacks"] == ["mock"]
        assert [c["name"] for c in body["circuits"]] == ["openai", "mock"]
        assert [c["state"] for c in body["circuits"]] == ["closed", "closed"]
        assert [c["consecutive_failures"] for c in body["circuits"]] == [0, 0]
        assert body["last_call"] is None or set(body["last_call"]) == {
            "ok",
            "degraded",
            "provider",
            "elapsed_ms",
            "first_chunk_ms",
        }
        _ = rec
def test_health_last_call_after_successful_turn(repo, persona):
    llm = ResilientProvider(MockProvider(), [], first_chunk_timeout=5.0, total_timeout=30.0)
    with _App(repo, llm) as client:
        sid = _session(client, persona)
        with client.stream("POST", f"/v1/sessions/{sid}/messages", json={"content": "今天面试砸了。"}) as r:
            assert r.status_code == 200
            _events(r)

        body = client.get("/v1/llm/health").json()
        last = body["last_call"]
        assert last["ok"] is True
        assert last["degraded"] is False
        assert last["provider"] == "mock"
        assert isinstance(last["elapsed_ms"], int)
        assert isinstance(last["first_chunk_ms"], int)
def test_health_last_call_degraded(repo, persona):
    llm = ResilientProvider(
        DeadProvider(), [MockProvider()], first_chunk_timeout=5.0, total_timeout=30.0,
        retries_per_provider=0,
    )
    with _App(repo, llm) as client:
        sid = _session(client, persona)
        with client.stream("POST", f"/v1/sessions/{sid}/messages", json={"content": "在吗"}) as r:
            assert r.status_code == 200
            _events(r)

        body = client.get("/v1/llm/health").json()
        assert body["last_call"]["degraded"] is True
        assert body["last_call"]["provider"] == "mock"
        # 主 provider 失败了一次，熔断器记账
        assert body["circuits"][0]["consecutive_failures"] == 1
def test_health_mock_provider_returns_empty_circuits(repo, persona):
    """`LINGER_LLM=mock`（即没有韧性层）→ circuits 为空，且不是 404。"""
    with _App(repo, MockProvider()) as client:
        resp = client.get("/v1/llm/health")
        assert resp.status_code == 200
        body = resp.json()
        assert body["provider"] == "mock"
        assert body["circuits"] == []
        assert body["last_call"] is None
# --------------------------------------------------------------------------- #
# 普通 JSON 端点：模型不可用 → 503
# --------------------------------------------------------------------------- #


def _all_dead() -> ResilientProvider:
    return ResilientProvider(
        DeadProvider(), [DeadProvider()], first_chunk_timeout=2.0, total_timeout=5.0,
        retries_per_provider=0,
    )


def test_persona_generate_returns_503(repo, persona):
    with _App(repo, _all_dead()) as client:
        resp = client.post("/v1/personas/generate", json={"description": "一个嘴硬心软的退役剑客"})
        assert resp.status_code == 503
        assert resp.json()["detail"]
def test_persona_distill_returns_503(repo, persona):
    with _App(repo, _all_dead()) as client:
        transcript = "\n".join(["阿澈：今天夜班累死了", "我：那你早点睡", "阿澈：嗯，晚安"]) 
        resp = client.post(
            "/v1/personas/distill",
            json={"transcript": transcript, "authorized": True},
        )
        assert resp.status_code == 503
        assert resp.json()["detail"]
def test_diary_generate_returns_503(repo, persona):
    """日记要先有当天的对话才走到模型；用可用模型造一轮，再换成全挂模型。"""
    with _App(repo, MockProvider()) as client:
        sid = _session(client, persona)
        with client.stream("POST", f"/v1/sessions/{sid}/messages", json={"content": "今天面试砸了。"}) as r:
            _events(r)

        # 换一个全挂的实现，直接替换 app.state（同一进程内验证错误码映射）
        client.app.state.orch.llm = _all_dead()
        resp = client.post(
            f"/v1/personas/{persona.id}/diary/generate",
            json={"user_id": "u_health"},
        )
        assert resp.status_code == 503
        assert resp.json()["detail"]
def test_timeout_also_maps_to_503(repo, persona):
    """LLMTimeoutError 与不可用同码：用户视角都是「现在用不了」。"""
    llm = ResilientProvider(SlowProvider(), [], first_chunk_timeout=0.1, total_timeout=0.2,
                            retries_per_provider=0)
    with _App(repo, llm) as client:
        resp = client.post("/v1/personas/generate", json={"description": "一个很安静的人"})
        assert resp.status_code == 503
# --------------------------------------------------------------------------- #
# SSE 端点：首 chunk 前 → 503
# --------------------------------------------------------------------------- #


def test_sse_503_before_stream(repo, persona):
    """所有 provider 在首 chunk 前就挂 → 503 + detail（不是 200 后再报错）。"""
    with _App(repo, _all_dead()) as client:
        sid = _session(client, persona)
        resp = client.post(f"/v1/sessions/{sid}/messages", json={"content": "在吗"})
        assert resp.status_code == 503
        assert resp.json()["detail"]
def test_sse_503_when_first_chunk_times_out(repo, persona):
    llm = ResilientProvider(SlowProvider(), [], first_chunk_timeout=0.1, total_timeout=0.3,
                            retries_per_provider=0)
    with _App(repo, llm) as client:
        sid = _session(client, persona)
        started = time.monotonic()
        resp = client.post(f"/v1/sessions/{sid}/messages", json={"content": "在吗"})
        assert resp.status_code == 503
        assert time.monotonic() - started < 3.0
# --------------------------------------------------------------------------- #
# SSE 端点：首 chunk 后断 → error 事件（状态码改不了，但话要说清）
# --------------------------------------------------------------------------- #


def test_sse_error_event_after_stream_started(repo, persona):
    llm = ResilientProvider(BrokenMidStream(), [MockProvider()], first_chunk_timeout=2.0,
                            total_timeout=10.0, retries_per_provider=1)
    with _App(repo, llm) as client:
        sid = _session(client, persona)

        with client.stream("POST", f"/v1/sessions/{sid}/messages", json={"content": "在吗"}) as r:
            assert r.status_code == 200  # 流已建立，状态码改不了
            events = _events(r)

        types = [t for t, _ in events]
        assert "error" in types, f"首 chunk 后断流必须发 error 事件，实际: {types}"
        assert types.index("error") == len(types) - 1, "error 之后不该再有事件"
        error = dict(events)["error"]
        assert error["data"]["kind"] == "LLMStreamBrokenError"
        assert error["data"]["stage"] == "llm"
        assert error["data"]["message"]
        # 已经吐出来的字确实发出去了
        deltas = [d["data"]["text"] for t, d in events if t == "delta"]
        assert deltas == ["前半句", "还在说"]
def test_sse_does_not_switch_after_first_chunk(repo, persona):
    """首 chunk 后断流时，备 provider 一次都不该被调用（判据 6 的 HTTP 视角）。"""
    backup = MockProvider()
    calls = {"n": 0}
    original = backup.generate

    def counting(*a, **k):
        calls["n"] += 1
        yield from original(*a, **k)

    backup.generate = counting
    llm = ResilientProvider(BrokenMidStream(), [backup], first_chunk_timeout=2.0,
                            total_timeout=10.0, retries_per_provider=2)
    with _App(repo, llm) as client:
        sid = _session(client, persona)
        with client.stream("POST", f"/v1/sessions/{sid}/messages", json={"content": "在吗"}) as r:
            _events(r)
        assert calls["n"] == 0
# --------------------------------------------------------------------------- #
# 熔断在 HTTP 层的表现：不再白等
# --------------------------------------------------------------------------- #


def test_breaker_skips_dead_primary(repo, persona):
    """主 provider 熔断后，请求直接走备 provider，不再碰主。"""
    dead = DeadProvider()
    llm = ResilientProvider(
        dead,
        [MockProvider()],
        first_chunk_timeout=2.0,
        total_timeout=10.0,
        retries_per_provider=0,
        breaker_factory=lambda: CircuitBreaker(
            fail_threshold=1, cooldown_seconds=600.0, now=lambda: 0.0
        ),
    )
    with _App(repo, llm) as client:
        sid = _session(client, persona)

        for _ in range(2):
            with client.stream("POST", f"/v1/sessions/{sid}/messages", json={"content": "在吗"}) as r:
                assert r.status_code == 200
                _events(r)

        assert dead.calls == 1, "熔断后仍然打了主 provider"
        body = client.get("/v1/llm/health").json()
        assert body["circuits"][0]["state"] == "open"
        assert body["circuits"][1]["state"] == "closed"
def test_exceptions_are_llm_runtime_errors():
    """三个异常都必须能被 HTTP 层用同一个基类捕获。"""
    from linger.llm.base import LLMRuntimeError

    for exc in (LLMUnavailableError, LLMTimeoutError):
        assert issubclass(exc, LLMRuntimeError)
    assert issubclass(LLMRuntimeError, RuntimeError)
