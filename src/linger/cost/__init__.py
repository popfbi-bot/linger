"""成本看板的后端（M4-D）：计量 / 单价 / 聚合。

分三层，各自的边界很清楚：

    meter.py    写：一次 LLM 调用 → 一条 UsageRecord（O(1) 单条 INSERT）
    pricing.py  算：token × 单价 → 成本（**未知就 None，不许 0**）
    report.py   读：查端点时才聚合，对话路径上一次 SUM 都不跑

对外的入口只有两个：`wrap()`（给 app 装配用）与 `build_report()`（给
`GET /v1/costs` 用）。中间那些类不该被业务层 import——
否则「计量」会慢慢长出业务逻辑，最后变成第二个 router。
"""

from __future__ import annotations

from typing import Any

from ..llm.base import LLMProvider
from ..llm.router import ResilientProvider
from .meter import (
    PURPOSE_CHAT,
    PURPOSE_DIARY,
    PURPOSE_DISTILL,
    PURPOSE_GENERATE,
    Recorder,
    RecordingProvider,
)
from .pricing import DEFAULT_PRICES, estimate_cost, known_models, load_prices, unit_price
from .report import build_report

__all__ = [
    "DEFAULT_PRICES",
    "PURPOSE_CHAT",
    "PURPOSE_DIARY",
    "PURPOSE_DISTILL",
    "PURPOSE_GENERATE",
    "Recorder",
    "RecordingProvider",
    "build_report",
    "estimate_cost",
    "known_models",
    "load_prices",
    "resilient",
    "unit_price",
    "wrap",
]


def wrap(provider: Any, recorder: Recorder | None, *, purpose: str = PURPOSE_CHAT) -> Any:
    """给 provider 套上计量。`recorder` 为 None 时**原样返回**。

    这条「None 就不包」不是省事，是语义：没有账本的时候多包一层
    只是让调用栈更深、`/v1/llm/health` 的 isinstance 判断多一个变数。
    """
    if recorder is None:
        return provider
    if isinstance(provider, RecordingProvider):
        return provider
    return RecordingProvider(provider, recorder, purpose=purpose)


def resilient(
    primary: LLMProvider,
    fallbacks: list[LLMProvider] | None = None,
    *,
    on_report: Recorder | None = None,
    **kwargs: Any,
) -> ResilientProvider:
    """造一个 `ResilientProvider`，可选挂上旁路计量回调。

    **旁路回调不参与流程**：`router.generate()` 的每一次尝试、每一次重试、
    每一次降级之后，`_report()` 顺手把那份报告交给回调，然后照旧继续。
    回调里抛异常也不会被 router 捕获并影响结果（见 router 里的 `_notify`）。

    为什么不直接把计量写进 router：M4-A 的韧性语义已经验收过，
    在它内部加分支就等于把「有没有账本」变成「重试几次」的隐变量。
    """
    router = ResilientProvider(primary, fallbacks, **kwargs)
    if on_report is not None:
        attach_reporter(router, on_report)
    return router


def attach_reporter(router: ResilientProvider, recorder: Recorder) -> None:
    """给已有 router 挂一个旁路计量回调（每次尝试一条记录）。

    只在**没有** `RecordingProvider` 外层包装时才有意义：否则同一笔账会记两遍。
    接线在 `server.app` 里做决定（见 `_wire_metering`），这里只提供开关。
    """
    from .meter import record_attempt

    def on_report(report: dict[str, Any]) -> None:
        record_attempt(recorder, router, report)

    router.on_report = on_report
