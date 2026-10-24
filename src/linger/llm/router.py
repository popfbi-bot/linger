"""韧性层（M4-A）：超时 / 重试 / 降级 / 熔断。

`ResilientProvider` 包装一主一备（可多个备）provider，对外仍然只是
`LLMProvider.generate()`。所以 orchestrator / generator / diary / distill
一行都不用改——这是本层存在的意义：**韧性是包装，不是协议变更**。

一条规则比什么都重要：

    **首 chunk 到达之后，不许切 provider。**

`generate()` 返回的是流。已经吐出去的字收不回来，中途换一个模型接着说，
用户会听到「前半句是 A 写的，后半句是 B 写的」——语气当场断裂，
比直接报错更伤人。所以：

    · 首 chunk 前失败（连接失败 / 超时 / 首字节超时 / 空输出）→ 可重试、可降级
    · 首 chunk 后失败 → 抛 `LLMStreamBrokenError`，不重试、不降级

## 超时怎么做成「真超时」

httpx 的 timeout 只覆盖真实网络。假 provider（含测试里的）不会超时，
所以这里不能依赖 provider 自己的 timeout 参数，必须由本层强制。

做法：每个 provider 的每一次尝试都跑在**独立的守护线程**里，
结果通过 `queue.Queue` 回传；主协程只做带截止时间的 `queue.get`。

    首 chunk 超时 → 该次尝试判失败（线程留着不管：守护线程不挡进程退出）
    整体超时     → 无论卡在哪一步，主协程都能自己醒来抛错

代价是每个尝试一个线程。对话场景里同时进行的 LLM 调用是个位数，
这比「为了超时引入 anyio 任务组或第三方向量」便宜得多，也不改变协议。
（备选的嵌套 `queue.get` 轮询做不到精确定时；
"主线程等结果 + 计时器线程注入" 又会在假 provider 的生成器被 GC 时炸出
"generator ignored GeneratorExit"，脏得没法看。）
"""

from __future__ import annotations

import queue
import threading
import time
from collections.abc import Callable, Iterator
from typing import Any

from .base import (
    LLMProvider,
    LLMStreamBrokenError,
    LLMTimeoutError,
    LLMUnavailableError,
)
from .circuit import CircuitBreaker

_OK = "ok"
_ERR = "err"
_END = "end"


class _Channel:
    """一次（provider, 尝试）的通信通道：生成线程写，调用方读。

    `cancel` 是给生成线程的退出信号：超时放弃之后不许它继续把字塞进队列——
    否则下一次尝试会和上一次的残留混在一起。
    """

    def __init__(self) -> None:
        self.q: queue.Queue = queue.Queue()
        self.cancel = threading.Event()

    # -- 生成线程侧 ----------------------------------------------------- #
    def put(self, kind: str, payload: Any = None) -> None:
        self.q.put((kind, payload))

    def finish(self) -> None:
        self.q.put((_END, None))

    def cancelled(self) -> bool:
        return self.cancel.is_set()

    # -- 调用方侧 ------------------------------------------------------- #
    def get(self, timeout: float) -> tuple[str, Any] | None:
        """等一条消息；超时返回 None。"""
        try:
            return self.q.get(timeout=max(0.0, timeout))
        except queue.Empty:
            return None


def _make_channel(
    provider: LLMProvider,
    system: str,
    messages: list[dict[str, str]],
    ctx: dict[str, Any] | None,
) -> _Channel:
    """起一个守护线程，替我们跑 provider.generate 并把产出转发到队列。"""
    ch = _Channel()

    def pump() -> None:
        try:
            for chunk in provider.generate(system, messages, ctx):
                if ch.cancelled():
                    return
                # 空白 chunk 不算内容：有些网关会稳定吐 "" 或 "\n"，
                # 把它们当作「已开始吐字」会让空输出检测失效。
                if chunk and chunk.strip():
                    ch.put(_OK, chunk)
        except BaseException as exc:  # noqa: BLE001 - 原样转交给调用方
            ch.put(_ERR, exc)
        finally:
            ch.finish()

    threading.Thread(
        target=pump, name=f"llm-{getattr(provider, 'name', 'provider')}", daemon=True
    ).start()
    return ch


class ResilientProvider:
    """按优先级包装多个 provider，带超时 / 重试 / 降级 / 熔断。"""

    name = "resilient"

    def __init__(
        self,
        primary: LLMProvider,
        fallbacks: list[LLMProvider] | None = None,
        *,
        first_chunk_timeout: float = 20.0,
        total_timeout: float = 120.0,
        retries_per_provider: int = 1,
        breaker: CircuitBreaker | None = None,
        breaker_factory: Callable[[], CircuitBreaker] | None = None,
        on_report: Callable[[dict[str, Any]], None] | None = None,
        now=time.monotonic,
    ) -> None:
        if first_chunk_timeout <= 0:
            raise ValueError("first_chunk_timeout 必须为正")
        if total_timeout <= 0:
            raise ValueError("total_timeout 必须为正")
        self.primary = primary
        self.fallbacks = list(fallbacks or [])
        self.first_chunk_timeout = first_chunk_timeout
        self.total_timeout = total_timeout
        self.retries_per_provider = max(0, retries_per_provider)
        # 每个 provider 一个熔断器：主挂了不该连累备，备挂了也不该让主更早被判死。
        self._breakers: list[CircuitBreaker] = [
            breaker if breaker is not None else (breaker_factory or CircuitBreaker)()
            for _ in self.providers
        ]
        self._now = now
        self._last_report: dict[str, Any] | None = None
        # 旁路计量回调（M4-D）。**不参与任何流程控制**：它拿到的是已经算完的
        # 那份观测报告，调用它之后代码照旧往下走；它抛异常也只被吞掉。
        # 为什么不做成 router 内部直接记账：M4-A 的韧性语义已验收，
        # 在里面加分支就等于让「有没有账本」变成「重试几次」的隐变量。
        self.on_report = on_report

    # ------------------------------------------------------------------ #
    @property
    def providers(self) -> list[LLMProvider]:
        return [self.primary, *self.fallbacks]

    def breaker_for(self, provider: LLMProvider) -> CircuitBreaker:
        return self._breakers[self.providers.index(provider)]

    def last_report(self) -> dict[str, Any]:
        """最近一次调用的观测数据。

        没调用过时给一个「空报告」而不是 None：调用方（日志、前端）
        不必为「还没调用过」多写一个分支。
        """
        return dict(self._last_report or _empty_report())

    def circuits(self) -> list[dict[str, Any]]:
        """各 provider 的熔断状态，给观测端点用。"""
        return [
            b.snapshot(getattr(p, "name", "unknown"))
            for p, b in zip(self.providers, self._breakers, strict=True)
        ]

    # ------------------------------------------------------------------ #
    def generate(
        self,
        system: str,
        messages: list[dict[str, str]],
        ctx: dict[str, Any] | None = None,
    ) -> Iterator[str]:
        started = self._now()
        deadline = started + self.total_timeout
        attempts: list[dict[str, Any]] = []
        skipped: list[str] = []

        for index, provider in enumerate(self.providers):
            name = getattr(provider, "name", "unknown")
            breaker = self._breakers[index]

            # 熔断：不发起请求，直接跳过。这是「主模型持续 500，
            # 每个请求都白等一次超时」的唯一解法。
            if not breaker.allow():
                skipped.append(name)
                continue

            delivered: list[str] = []
            first_at: float | None = None
            outcome: dict[str, Any] = {}

            for _attempt in range(self.retries_per_provider + 1):
                ch = _make_channel(provider, system, messages, ctx)
                try:
                    first_deadline = self._now() + self.first_chunk_timeout
                    while True:
                        msg = ch.get(self._wait_for(first_deadline, deadline, first_at))
                        if msg is None:
                            # 超时归因：「首 chunk 超时」还能降级，「整体超时」
                            # 是这一轮彻底失败——先看整体截止，再看首 chunk 截止。
                            now = self._now()
                            if now >= deadline:
                                raise LLMTimeoutError(
                                    f"模型调用超过整体时限（{self.total_timeout:.0f}s）"
                                )
                            raise _FirstChunkTimeout() if first_at is None else _IdleTimeout()

                        kind, payload = msg
                        if kind == _OK:
                            if first_at is None:
                                first_at = self._now()
                                breaker.record_success()
                            delivered.append(payload)
                            yield payload
                        elif kind == _END:
                            if not delivered:
                                raise _EmptyOutput()
                            outcome = {"done": True}
                            break
                        else:  # _ERR
                            exc = payload
                            if first_at is None:
                                raise exc
                            # 首 chunk 之后断了：**不许**重试，**不许**降级。
                            outcome = {"error": exc}
                            break
                except Exception as exc:  # noqa: BLE001 - 首 chunk 前的任何失败都可重试
                    ch.cancel.set()
                    attempts.append({"provider": name, "error": _describe(exc)})
                    # 旁路计量（M4-D）：**每一次失败尝试**都要被看见，而不是只有
                    # 最后那次 `_report`。重试风暴的表现正是「中间那些尝试全失败」——
                    # 只统计最终结果，看板上只看得到一个成功的调用，风暴完全隐形。
                    # 这个回调不参与分支判断：`continue` / `raise` 该走还是走。
                    self._notify(
                        {
                            "ok": False,
                            "degraded": index > 0,
                            "provider": name,
                            "error": _describe(exc),
                            "elapsed_ms": int((self._now() - started) * 1000),
                            "attempt": len(attempts),
                        }
                    )
                    if isinstance(exc, LLMTimeoutError):
                        # 整体超时是全局失败，不是「这个 provider 不行」——
                        # 再换一个也只会把剩下的时间烧光。
                        self._report(
                            started=started,
                            ok=False,
                            degraded=index > 0,
                            provider=name,
                            first_at=first_at,
                            attempts=attempts,
                            skipped=skipped,
                            delivered=delivered,
                            error=_describe(exc),
                        )
                        raise LLMTimeoutError(
                            f"模型调用超过整体时限（{self.total_timeout:.0f}s）"
                        ) from exc
                    # 继续下一次尝试（重试），重试用尽则换下一个 provider（降级）
                    continue
                else:
                    # 正常收尾 / 首 chunk 后断流：都不进 `except`，也不进 `finally`
                    #（收尾放在 else 里，见 `except` 上方注释）
                    if outcome.get("done"):
                        self._report(
                            started=started,
                            ok=True,
                            degraded=index > 0,
                            provider=name,
                            first_at=first_at,
                            attempts=attempts,
                            skipped=skipped,
                            delivered=delivered,
                        )
                        return
                    exc = outcome["error"]
                    report_error = f"模型（{name}）在已经开始输出后中断：{exc}"
                    self._report(
                        started=started,
                        ok=False,
                        degraded=index > 0,
                        provider=name,
                        first_at=first_at,
                        attempts=attempts,
                        skipped=skipped,
                        delivered=delivered,
                        error=report_error,
                    )
                    raise LLMStreamBrokenError(report_error) from exc

            breaker.record_failure()

        error = LLMUnavailableError(
            "所有模型都不可用"
            + (f"（已跳过：{'、'.join(skipped)}）" if skipped else "")
            + (f"；失败记录：{attempts}" if attempts else "")
        )
        self._report(
            started=started,
            ok=False,
            degraded=False,
            provider=None,
            first_at=None,
            attempts=attempts,
            skipped=skipped,
            delivered=[],
            error=str(error),
        )
        raise error

    # ------------------------------------------------------------------ #
    def _wait_for(
        self, first_deadline: float, deadline: float, first_at: float | None
    ) -> float:
        """算这一次该等多久（秒）。

        两个截止取最早：首 chunk 截止（首 chunk 前）与整体截止。
        时间已经用光时返回 0.0，让调用方**不阻塞地**再取一次队列——
        消息可能刚好在边界上到了。真正的超时归因在调用方做
        （先看整体截止，再看首 chunk 截止）。

        首 chunk **之后**的截止时间是整体截止：一个已经开始吐字的流，
        只要没超过整体时限就继续等。
        """
        now = self._now()
        if first_at is not None:
            return max(0.0, deadline - now)
        return max(0.0, min(first_deadline, deadline) - now)

    def _report(
        self,
        *,
        started: float,
        ok: bool,
        degraded: bool,
        provider: str | None,
        first_at: float | None,
        attempts: list[dict[str, Any]],
        skipped: list[str],
        delivered: list[str],
        error: str | None = None,
    ) -> None:
        now = self._now()
        self._last_report = {
            "ok": ok,
            "degraded": degraded,
            "provider": provider,
            "elapsed_ms": int((now - started) * 1000),
            "first_chunk_ms": int((first_at - started) * 1000) if first_at else None,
            "chunks": len(delivered),
            "chars": sum(len(c) for c in delivered),
            "attempts": list(attempts),
            "skipped": list(skipped),
            "error": error,
        }
        self._notify(self._last_report)

    def _notify(self, report: dict[str, Any]) -> None:
        """把观测报告交给旁路回调。

        吞掉一切异常：账记错了可以补，回复丢了不能补。回调里抛出去的错
        一旦冒到这里，会变成「一次对话因为记账挂了而失败」——那是荒谬的因果。
        """
        callback = getattr(self, "on_report", None)
        if callback is None:
            return
        try:
            callback(dict(report))
        except Exception:  # noqa: BLE001 - 见 docstring
            pass

    def __repr__(self) -> str:  # pragma: no cover - 调试用
        names = "、".join(getattr(p, "name", "?") for p in self.providers)
        return f"ResilientProvider({names})"


# --------------------------------------------------------------------------- #
# 内部异常：只在本层流转，不暴露给调用方
# --------------------------------------------------------------------------- #


class _FirstChunkTimeout(Exception):
    pass


class _IdleTimeout(Exception):
    pass


class _EmptyOutput(Exception):
    pass


def _describe(exc: BaseException) -> str:
    if isinstance(exc, _FirstChunkTimeout):
        return "首 chunk 超时"
    if isinstance(exc, _IdleTimeout):
        return "输出停滞超时"
    if isinstance(exc, _EmptyOutput):
        return "空输出（0 个有效 chunk）"
    return f"{type(exc).__name__}: {exc}"[:200]


def _empty_report() -> dict[str, Any]:
    return {
        "ok": False,
        "degraded": False,
        "provider": None,
        "elapsed_ms": 0,
        "first_chunk_ms": None,
        "chunks": 0,
        "chars": 0,
        "attempts": [],
        "skipped": [],
        "error": None,
    }
