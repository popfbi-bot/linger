"""用量计量（M4-D）：每次 LLM 调用落一条记录。

## 为什么不写在 router 里

`llm/router.py` 的超时 / 重试 / 熔断 / 「首 chunk 后不切换 provider」是 M4-A
已验收的韧性语义，动一下就是回归。所以计量做成**包装层**：

    ResilientProvider --(旁路回调 last_report)--> RecordingProvider --> 真正的 provider

两条路都能记，且有主次：

1. **主路：`RecordingProvider` 包在 router 外层。** 它是「一次业务调用」的账：
   一次对话算一次，token 数来自 provider 自己的 usage（真模型有真数字），
   重试了几次、降级到谁，从 `router.last_report()` 里取。这是看板上的一行。
2. **兜底：`ResilientProvider(on_report=...)` 旁路回调。** 只给**没被包住**的
   provider 用（比如有人直接用 `ResilientProvider(MockProvider())`），
   记的是每一次尝试。回调默认是 None，行为与 M4-A 逐字节相同。

## 失败也要记账

只统计成功的调用，会让「重试风暴」在看板上完全隐形——而重试风暴正是烧钱的主因。
所以 `ok=False` 的记录进的是**同一张表**（不是另一张「错误表」），
失败率才能用一条 SQL 算出来。

## 写是 O(1)，聚合在查端点时做

`record()` 只做一件事：单条 INSERT。不在对话关键路径上跑 SUM / GROUP BY——
那会让每一轮对话的延迟随历史记录线性增长（判据 5）。
"""

from __future__ import annotations

import time
from collections.abc import Callable, Iterator
from datetime import datetime, timezone
from typing import Any

from ..compose.compiler import estimate_tokens
from ..llm.base import LLMProvider
from ..models import UsageRecord

# 一次业务调用的「用途」。写进库是为了让看板能区分
# 「陪聊烧的钱」和「后台蒸馏烧的钱」——两者出问题时要查的地方完全不同。
PURPOSE_CHAT = "chat"
PURPOSE_GENERATE = "generate"
PURPOSE_DISTILL = "distill"
PURPOSE_DIARY = "diary"

Recorder = Callable[[UsageRecord], Any]


def _now() -> datetime:
    return datetime.now(timezone.utc)


def usage_of(provider: Any, messages: list[dict[str, str]], text: str) -> dict[str, int]:
    """取本次调用的 token 用量。

    优先用 provider 自己报的（真模型的 usage 是账单口径，唯一可信的那一份）；
    provider 不报（mock、以及所有流式实现）时退化为**估算**，并打上 `estimated=True`。
    估算不是账单，看板上必须能看出「这行是估的」。
    """
    reported = getattr(provider, "last_usage", None)
    if isinstance(reported, dict) and "in" in reported and "out" in reported:
        return {
            "in": int(reported["in"]),
            "out": int(reported["out"]),
            "cached": int(reported.get("cached", 0) or 0),
            "estimated": False,
        }
    prompt_text = "\n".join(m.get("content", "") for m in messages)
    return {"in": estimate_tokens(prompt_text), "out": estimate_tokens(text), "cached": 0, "estimated": True}


class RecordingProvider:
    """包在 router 外层的计量包装。对外仍然只是 `generate()`。

    **它不改变任何行为**：不重试、不降级、不拦异常、不吞异常。
    唯一的多余动作是「拿到结果之后写一条记录」，而写记录本身失败也不许
    影响这次调用——账记错了可以补，回复丢了不能补。
    """

    name = "recording"

    def __init__(
        self,
        inner: LLMProvider,
        recorder: Recorder | None = None,
        *,
        purpose: str = PURPOSE_CHAT,
        clock: Callable[[], float] = time.monotonic,
        wall: Callable[[], datetime] = _now,
    ) -> None:
        self.inner = inner
        self.recorder = recorder
        self.purpose = purpose
        self._clock = clock
        self._wall = wall

    # router 的观测数据要能透传（`/v1/llm/health` 靠它）
    def __getattr__(self, item: str) -> Any:
        return getattr(self.inner, item)

    def last_report(self) -> dict[str, Any]:
        report = getattr(self.inner, "last_report", None)
        return report() if callable(report) else {}

    def circuits(self) -> list[dict[str, Any]]:
        circuits = getattr(self.inner, "circuits", None)
        return circuits() if callable(circuits) else []

    def generate(
        self,
        system: str,
        messages: list[dict[str, str]],
        ctx: dict[str, Any] | None = None,
    ) -> Iterator[str]:
        started = self._clock()
        parts: list[str] = []
        error: BaseException | None = None
        try:
            for delta in self.inner.generate(system, messages, ctx):
                parts.append(delta)
                yield delta
        except BaseException as exc:  # noqa: BLE001 - 记完账原样抛出，绝不改语义
            error = exc
            raise
        finally:
            # 用户中途断开（GeneratorExit）也算一次调用：上游已经把 token 烧了。
            # 把它当成本轮失败来记，比当作「什么都没发生」更接近账单。
            self._record(messages, "".join(parts), error, self._clock() - started)

    # ------------------------------------------------------------------ #
    def _record(
        self,
        messages: list[dict[str, str]],
        text: str,
        error: BaseException | None,
        elapsed: float,
    ) -> None:
        if self.recorder is None:
            return
        report = self.last_report() or {}
        provider = report.get("provider") or getattr(self.inner, "name", "unknown")
        # router 的 report.provider 是**实际干活的**那个（降级后就是备）。真模型名
        # 在它下面那层 provider 上；拿不到就用 provider 名兜底。
        model = getattr(self.inner, "model", None) or _model_of(report) or provider
        try:
            usage = usage_of(self._inner_provider(), messages, text)
        except Exception:  # noqa: BLE001 - 取用量失败不该让调用失败
            usage = {"in": None, "out": None, "cached": 0, "estimated": True}
        record = UsageRecord(
            created_at=self._wall(),
            provider=str(provider),
            model=str(model),
            purpose=self.purpose,
            ok=error is None,
            degraded=bool(report.get("degraded", False)),
            error=None if error is None else _describe(error),
            elapsed_ms=int(elapsed * 1000),
            chunks=int(report.get("chunks", 0) or 0),
            attempts=len(report.get("attempts", []) or []),
            skipped=list(report.get("skipped", []) or []),
            tokens_in=usage["in"],
            tokens_out=usage["out"],
            tokens_cached=usage["cached"],
            estimated=usage["estimated"],
        )
        try:
            self.recorder(record)
        except Exception:  # noqa: BLE001 - 记账失败不许影响这次对话
            pass

    def _inner_provider(self) -> Any:
        """真正产字的那个 provider（穿过多层包装找 `model` / `last_usage`）。"""
        node: Any = self.inner
        for _ in range(4):
            if hasattr(node, "model") or hasattr(node, "last_usage"):
                return node
            nxt = getattr(node, "primary", None) or getattr(node, "inner", None)
            if nxt is None:
                return node
            node = nxt
        return node

    def __repr__(self) -> str:  # pragma: no cover - 调试用
        return f"RecordingProvider({getattr(self.inner, 'name', '?')}, purpose={self.purpose})"


def _model_of(report: dict[str, Any]) -> str | None:
    return None if not isinstance(report, dict) else report.get("model")


def _describe(exc: BaseException) -> str:
    text = str(exc) or type(exc).__name__
    return f"{type(exc).__name__}: {text}"[:200]


def record_attempt(
    recorder: Recorder | None,
    provider: Any,
    report: dict[str, Any],
    *,
    purpose: str = PURPOSE_CHAT,
) -> None:
    """把 `ResilientProvider` 的一次**尝试**记成一条记录（兜底路径用）。

    只在没有 `RecordingProvider` 包着的时候需要。token 数取不到真值
    （router 的 report 里没有 prompt/messages），所以这里一律记估算或 None——
    宁可标成「估的」，也不要编一个看起来很精确的假数字。
    """
    if recorder is None:
        return
    ok = bool(report.get("ok"))
    recorder(
        UsageRecord(
            provider=str(report.get("provider") or getattr(provider, "name", "unknown")),
            model=str(getattr(provider, "model", None) or getattr(provider, "name", "unknown")),
            purpose=purpose,
            ok=ok,
            degraded=bool(report.get("degraded", False)),
            error=report.get("error"),
            elapsed_ms=int(report.get("elapsed_ms", 0) or 0),
            chunks=int(report.get("chunks", 0) or 0),
            attempts=len(report.get("attempts", []) or []),
            skipped=list(report.get("skipped", []) or []),
            tokens_in=None,
            tokens_out=None,
            tokens_cached=0,
            estimated=True,
        )
    )
