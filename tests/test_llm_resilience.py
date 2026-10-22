"""M4-A 验收：LLM 调用层的韧性——超时 / 重试 / 降级 / 熔断。

每一条用例都对应任务里的验收判据（编号一致）。判据 5/6/7 额外配了
「先弄坏实现证明它红」的证据，见 `tests/test_llm_resilience_redgreen.py`。

假 provider 都是可控的：要它慢它就慢，要它抛就抛，要它数被调用几次就数。
不用 sleep 等冷却（熔断器的 now 可注入），也不碰网络。
"""

from __future__ import annotations

import threading
import time
from collections.abc import Iterator
from typing import Any

import pytest

from linger.llm.base import LLMStreamBrokenError, LLMTimeoutError, LLMUnavailableError
from linger.llm.circuit import CircuitBreaker
from linger.llm.router import ResilientProvider

# --------------------------------------------------------------------------- #
# 可控假 provider
# --------------------------------------------------------------------------- #


class Recorder:
    """所有假 provider 共用一张记账表：谁被调了几次。"""

    def __init__(self) -> None:
        self.calls: dict[str, int] = {}

    def bump(self, name: str) -> int:
        self.calls[name] = self.calls.get(name, 0) + 1
        return self.calls[name]

    def count(self, name: str) -> int:
        return self.calls.get(name, 0)


class ScriptedProvider:
    """按剧本吐 chunk；可以在指定位置抛异常 / 阻塞 / 什么都不吐。

    `chunks`：要吐的内容
    `raise_at`：吐出第几个 chunk 之后抛（0 = 吐字前就抛；None = 不抛）
    `block`：True 时整个 generate 阻塞，直到 `release` 被 set
    `delay`：每个 chunk 之间等多久（用来制造「整体超时」）
    """

    def __init__(
        self,
        name: str,
        chunks: list[str] | None = None,
        *,
        recorder: Recorder | None = None,
        raise_at: int | None = None,
        exc: BaseException | None = None,
        block: bool = False,
        delay: float = 0.0,
    ) -> None:
        self.name = name
        self.chunks = chunks if chunks is not None else ["你好"]
        self.recorder = recorder or Recorder()
        self.raise_at = raise_at
        self.exc = exc or RuntimeError("模型挂了")
        self.block = block
        self.delay = delay
        self.release = threading.Event()
        self.started = threading.Event()

    @property
    def calls(self) -> int:
        return self.recorder.count(self.name)

    def generate(self, system, messages, ctx=None) -> Iterator[str]:
        self.recorder.bump(self.name)
        self.started.set()
        if self.block:
            # 永不产出：首 chunk 超时判据就靠它
            self.release.wait(10)
            return
        for i, chunk in enumerate(self.chunks):
            if self.raise_at is not None and i == self.raise_at:
                raise self.exc
            if self.delay:
                time.sleep(self.delay)
            yield chunk
        if self.raise_at is not None and self.raise_at >= len(self.chunks):
            raise self.exc


def _drain(provider, **kwargs) -> str:
    return "".join(provider.generate("sys", [{"role": "user", "content": "hi"}], **kwargs))


# --------------------------------------------------------------------------- #
# 判据 1：首 chunk 超时
# --------------------------------------------------------------------------- #


def test_c1_first_chunk_timeout_falls_back_fast():
    """永不产出的 provider 在 ~first_chunk_timeout 后被放弃，并降级到备。"""
    rec = Recorder()
    stuck = ScriptedProvider("stuck", recorder=rec, block=True)
    backup = ScriptedProvider("backup", ["我在。"], recorder=rec)
    router = ResilientProvider(
        stuck,
        [backup],
        first_chunk_timeout=0.2,
        total_timeout=10.0,
        retries_per_provider=0,
    )

    t0 = time.monotonic()
    out = _drain(router)
    elapsed = time.monotonic() - t0

    assert out == "我在。"
    assert elapsed < 1.5, f"首 chunk 超时没有生效，耗时 {elapsed:.2f}s"
    assert rec.count("backup") == 1
    report = router.last_report()
    assert report["degraded"] is True
    assert report["ok"] is True
    stuck.release.set()


def test_c1b_first_chunk_timeout_without_fallback_is_unavailable():
    """没有备 provider 时，首 chunk 超时 → LLMUnavailableError（不是挂死）。"""
    stuck = ScriptedProvider("stuck", block=True)
    router = ResilientProvider(
        stuck, [], first_chunk_timeout=0.15, total_timeout=10.0, retries_per_provider=0
    )
    t0 = time.monotonic()
    with pytest.raises(LLMUnavailableError):
        _drain(router)
    assert time.monotonic() - t0 < 1.5
    stuck.release.set()


def test_c1c_mock_provider_never_times_out():
    """真 provider 的正常路径不该被韧性层打断（超时是保护，不是限制）。"""
    from linger.llm.mock import MockProvider

    router = ResilientProvider(
        MockProvider(), None, first_chunk_timeout=20.0, total_timeout=30.0
    )
    assert _drain(router).strip()
    assert router.last_report()["ok"] is True
    assert router.last_report()["degraded"] is False


# --------------------------------------------------------------------------- #
# 判据 2：重试
# --------------------------------------------------------------------------- #


def test_c2_retry_until_success():
    """前 N 次抛异常、第 N+1 次成功 → retries_per_provider=N 时最终成功。"""

    class FlakyProvider:
        name = "flaky"

        def __init__(self, fail_times: int) -> None:
            self.fail_times = fail_times
            self.calls = 0

        def generate(self, system, messages, ctx=None) -> Iterator[str]:
            self.calls += 1
            if self.calls <= self.fail_times:
                raise ConnectionError("网络抖了")
            yield "恢复"

    primary = FlakyProvider(fail_times=2)
    router = ResilientProvider(primary, [], retries_per_provider=2)
    assert _drain(router) == "恢复"
    assert primary.calls == 3
    assert router.last_report()["ok"] is True
    assert router.last_report()["degraded"] is False


def test_c2b_retries_exhausted_then_fallback():
    """重试用尽立刻换备 provider，不再多试。"""
    rec = Recorder()
    always = ScriptedProvider("always", recorder=rec, raise_at=0)
    backup = ScriptedProvider("backup", ["备的"], recorder=rec)
    router = ResilientProvider(always, [backup], retries_per_provider=2)
    assert _drain(router) == "备的"
    assert rec.count("always") == 3  # 1 次 + 2 次重试
    assert rec.count("backup") == 1


# --------------------------------------------------------------------------- #
# 判据 3：降级
# --------------------------------------------------------------------------- #


def test_c3_fallback_and_report():
    rec = Recorder()
    primary = ScriptedProvider("primary", recorder=rec, raise_at=0)
    backup = ScriptedProvider("backup", ["嗯", "我在。"], recorder=rec)
    router = ResilientProvider(primary, [backup], retries_per_provider=0)

    assert _drain(router) == "嗯我在。"
    report = router.last_report()
    assert report["degraded"] is True
    assert report["ok"] is True
    assert report["provider"] == "backup"
    assert report["attempts"][0]["provider"] == "primary"
    assert report["first_chunk_ms"] is not None


def test_c3b_primary_used_when_healthy():
    rec = Recorder()
    primary = ScriptedProvider("primary", ["主的"], recorder=rec)
    backup = ScriptedProvider("backup", ["备的"], recorder=rec)
    router = ResilientProvider(primary, [backup])
    assert _drain(router) == "主的"
    assert router.last_report()["degraded"] is False
    assert rec.count("backup") == 0


# --------------------------------------------------------------------------- #
# 判据 4：全部失败
# --------------------------------------------------------------------------- #


def test_c4_all_providers_fail():
    rec = Recorder()
    router = ResilientProvider(
        ScriptedProvider("a", recorder=rec, raise_at=0),
        [ScriptedProvider("b", recorder=rec, raise_at=0)],
        retries_per_provider=1,
    )
    with pytest.raises(LLMUnavailableError):
        _drain(router)
    report = router.last_report()
    assert report["ok"] is False
    assert report["provider"] is None


# --------------------------------------------------------------------------- #
# 判据 5：熔断
# --------------------------------------------------------------------------- #


def test_c5_breaker_opens_then_skips_requests():
    """连续失败达阈值 → open；此后调用不再发起请求；冷却后半开、试探成功回 closed。"""
    clock = {"t": 1000.0}
    breaker = CircuitBreaker(fail_threshold=2, cooldown_seconds=30.0, now=lambda: clock["t"])
    rec = Recorder()
    primary = ScriptedProvider("primary", recorder=rec, raise_at=0)
    router = ResilientProvider(primary, [], retries_per_provider=0, breaker=breaker)

    for _ in range(2):
        with pytest.raises(LLMUnavailableError):
            _drain(router)
    assert breaker.state == "open"
    assert rec.count("primary") == 2

    # open：直接跳过，一次请求都不发
    with pytest.raises(LLMUnavailableError):
        _drain(router)
    assert rec.count("primary") == 2, "open 状态下仍然发起了请求"

    # 冷却到点 → half_open，放行一次试探
    clock["t"] += 31.0
    assert breaker.state == "half_open"
    primary.raise_at = None
    primary.chunks = ["回来了"]
    assert _drain(router) == "回来了"
    assert rec.count("primary") == 3
    assert breaker.state == "closed"
    assert breaker.consecutive_failures == 0


def test_c5b_probe_failure_reopens_and_resets_cooldown():
    clock = {"t": 0.0}
    breaker = CircuitBreaker(fail_threshold=1, cooldown_seconds=10.0, now=lambda: clock["t"])
    rec = Recorder()
    primary = ScriptedProvider("primary", recorder=rec, raise_at=0)
    router = ResilientProvider(primary, [], retries_per_provider=0, breaker=breaker)

    with pytest.raises(LLMUnavailableError):
        _drain(router)
    assert breaker.state == "open"

    clock["t"] += 11.0
    assert breaker.state == "half_open"
    with pytest.raises(LLMUnavailableError):
        _drain(router)  # 试探失败
    assert breaker.state == "open"

    # 冷却从**试探失败那一刻**重新算：刚证明它还是坏的，不该立刻再试
    clock["t"] += 5.0
    assert breaker.state == "open"
    clock["t"] += 6.0
    assert breaker.state == "half_open"


def test_c5c_breaker_is_per_provider():
    """主熔断了不该连累备：备还能正常服务，而且是降级而非失败。"""
    rec = Recorder()
    primary = ScriptedProvider("primary", recorder=rec, raise_at=0)
    backup = ScriptedProvider("backup", ["备的"], recorder=rec)
    router = ResilientProvider(
        primary,
        [backup],
        retries_per_provider=0,
        breaker_factory=lambda: CircuitBreaker(fail_threshold=1, cooldown_seconds=600.0,
                                              now=lambda: 0.0),
    )
    primary_breaker = router.breaker_for(primary)

    assert _drain(router) == "备的"
    assert primary_breaker.state == "open"
    assert router.breaker_for(backup).state == "closed"
    assert _drain(router) == "备的"
    assert rec.count("primary") == 1, "熔断后仍然打了主 provider"
    assert rec.count("backup") == 2


def test_c5d_half_open_allows_only_one_probe():
    clock = {"t": 0.0}
    breaker = CircuitBreaker(fail_threshold=1, cooldown_seconds=1.0, now=lambda: clock["t"])
    breaker.record_failure()
    clock["t"] += 2.0
    assert breaker.allow() is True   # 第一个进来的拿到试探权
    assert breaker.allow() is False  # 试探还没回来，别的请求继续被挡


# --------------------------------------------------------------------------- #
# 判据 6：首 chunk 后不切换
# --------------------------------------------------------------------------- #


def test_c6_no_switch_after_first_chunk():
    """吐了 2 个 chunk 再断 → 异常抛出，备 provider 一次都没被调用。"""
    rec = Recorder()
    primary = ScriptedProvider(
        "primary", ["前半句", "还在说"], recorder=rec, raise_at=2, exc=ConnectionError("断了")
    )
    backup = ScriptedProvider("backup", ["后半句"], recorder=rec)
    router = ResilientProvider(primary, [backup], retries_per_provider=3)

    got: list[str] = []
    with pytest.raises(LLMStreamBrokenError):
        for chunk in router.generate("sys", [{"role": "user", "content": "hi"}]):
            got.append(chunk)

    assert got == ["前半句", "还在说"], "已经吐出去的字不该被吞掉"
    assert rec.count("backup") == 0, "首 chunk 之后不许切换到备 provider"
    assert rec.count("primary") == 1, "首 chunk 之后不许重试"
    report = router.last_report()
    assert report["ok"] is False
    assert report["provider"] == "primary"


def test_c6b_break_after_first_chunk_is_not_retried():
    """重试也不许：已经在吐字了，重来一次会让用户看到两遍开头。"""
    rec = Recorder()
    primary = ScriptedProvider("primary", ["开头"], recorder=rec, raise_at=1)
    router = ResilientProvider(primary, [ScriptedProvider("backup", recorder=rec)],
                               retries_per_provider=5)
    with pytest.raises(LLMStreamBrokenError):
        _drain(router)
    assert rec.count("primary") == 1
    assert rec.count("backup") == 0


# --------------------------------------------------------------------------- #
# 判据 7：空输出算失败
# --------------------------------------------------------------------------- #


def test_c7_empty_output_is_failure():
    rec = Recorder()
    primary = ScriptedProvider("primary", [], recorder=rec)  # 200 但 0 个 chunk
    backup = ScriptedProvider("backup", ["我来说"], recorder=rec)
    router = ResilientProvider(primary, [backup], retries_per_provider=0)

    assert _drain(router) == "我来说"
    assert router.last_report()["degraded"] is True
    assert router.last_report()["attempts"][0]["error"].startswith("空输出")


def test_c7b_whitespace_only_counts_as_empty():
    rec = Recorder()
    primary = ScriptedProvider("primary", ["", " ", "\n"], recorder=rec)
    backup = ScriptedProvider("backup", ["备的"], recorder=rec)
    router = ResilientProvider(primary, [backup], retries_per_provider=0)
    assert _drain(router) == "备的"
    assert rec.count("backup") == 1


def test_c7c_all_empty_output():
    """所有 provider 都空输出 → 仍要抛不可用，不能返回空字符串假装成功。"""
    rec = Recorder()
    router = ResilientProvider(
        ScriptedProvider("a", [], recorder=rec),
        [ScriptedProvider("b", [""], recorder=rec)],
        retries_per_provider=0,
    )
    with pytest.raises(LLMUnavailableError):
        _drain(router)


# --------------------------------------------------------------------------- #
# 判据 8：整体超时
# --------------------------------------------------------------------------- #


def test_c8_total_timeout():
    """一个 chunk 喂一点、总耗时超过 total_timeout → LLMTimeoutError。"""
    slow = ScriptedProvider("slow", ["一", "二", "三", "四", "五"], delay=0.15)
    router = ResilientProvider(
        slow,
        [ScriptedProvider("backup", ["备的"])],
        first_chunk_timeout=5.0,
        total_timeout=0.4,
        retries_per_provider=0,
    )

    t0 = time.monotonic()
    with pytest.raises(LLMTimeoutError):
        _drain(router)
    elapsed = time.monotonic() - t0
    assert elapsed < 2.0, f"整体超时没有生效，耗时 {elapsed:.2f}s"
    assert router.last_report()["ok"] is False


def test_c8b_total_timeout_before_first_chunk():
    """整体时限比首 chunk 时限还短时，以整体时限为准。"""
    stuck = ScriptedProvider("stuck", block=True)
    router = ResilientProvider(
        stuck, [], first_chunk_timeout=10.0, total_timeout=0.2, retries_per_provider=0
    )
    t0 = time.monotonic()
    with pytest.raises(LLMTimeoutError):
        _drain(router)
    assert time.monotonic() - t0 < 1.5
    stuck.release.set()


# --------------------------------------------------------------------------- #
# 观测：last_report 的结构
# --------------------------------------------------------------------------- #


def test_report_shape():
    rec = Recorder()
    router = ResilientProvider(ScriptedProvider("p", ["a", "b"], recorder=rec))
    assert router.last_report()["ok"] is False  # 还没调用过
    _drain(router)
    report = router.last_report()
    for key in ("ok", "degraded", "provider", "elapsed_ms", "first_chunk_ms"):
        assert key in report
    assert report["chunks"] == 2
    assert report["chars"] == 2


def test_circuits_snapshot():
    rec = Recorder()
    router = ResilientProvider(
        ScriptedProvider("a", recorder=rec),
        [ScriptedProvider("b", recorder=rec)],
        breaker=CircuitBreaker(fail_threshold=1, now=lambda: 0.0),
    )
    snap = router.circuits()
    assert [c["name"] for c in snap] == ["a", "b"]
    assert [c["state"] for c in snap] == ["closed", "closed"]
    assert [c["consecutive_failures"] for c in snap] == [0, 0]


# --------------------------------------------------------------------------- #
# 装配（判据 9 的另一半：LINGER_LLM=mock 时不套韧性层）
# --------------------------------------------------------------------------- #


def test_get_provider_mock_is_bare(monkeypatch):
    from linger.config import settings
    from linger.llm import MockProvider, get_provider

    monkeypatch.setattr(settings, "llm_provider", "mock")
    assert isinstance(get_provider(), MockProvider)


def test_get_provider_wraps_with_resilience():
    from linger.config import settings
    from linger.llm import MockProvider, ResilientProvider

    class _Dummy(LLMProviderLike := object):  # noqa: F841 - 只为构造一个非 mock 主 provider
        name = "dummy"

        def generate(self, system, messages, ctx=None):
            yield "x"

    import linger.llm as llm_module

    monkeypatch_target = llm_module._build
    try:
        llm_module._build = lambda name: MockProvider() if name == "mock" else _Dummy()
        settings_backup = (
            settings.llm_provider,
            settings.llm_fallback,
            settings.llm_timeout,
            settings.llm_retries,
        )
        settings.llm_provider = "openai"
        settings.llm_fallback = "mock"
        settings.llm_timeout = 7.0
        settings.llm_retries = 2
        provider = llm_module.get_provider()
        assert isinstance(provider, ResilientProvider)
        assert provider.first_chunk_timeout == 7.0
        assert provider.retries_per_provider == 2
        assert [p.name for p in provider.providers] == ["dummy", "mock"]
    finally:
        llm_module._build = monkeypatch_target
        (
            settings.llm_provider,
            settings.llm_fallback,
            settings.llm_timeout,
            settings.llm_retries,
        ) = settings_backup


def test_get_provider_no_fallback_when_blank():
    import linger.llm as llm_module
    from linger.config import settings
    from linger.llm import MockProvider, ResilientProvider

    class _Dummy:
        name = "dummy"

        def generate(self, system, messages, ctx=None):
            yield "x"

    backup = llm_module._build
    saved = (settings.llm_provider, settings.llm_fallback)
    try:
        llm_module._build = lambda name: MockProvider() if name == "mock" else _Dummy()
        settings.llm_provider = "openai"
        settings.llm_fallback = ""
        provider = llm_module.get_provider()
        assert isinstance(provider, ResilientProvider)
        assert provider.fallbacks == []
    finally:
        llm_module._build = backup
        settings.llm_provider, settings.llm_fallback = saved


def _unused(_: Any) -> None:  # pragma: no cover
    pass
