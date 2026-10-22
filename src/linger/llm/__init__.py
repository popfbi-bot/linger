"""模型层：协议 + 一个真实实现 + 一个 mock + 一层韧性包装（M4-A）。

调用方只认 `LLMProvider` 协议。`get_provider()` 返回的可能是裸 provider，
也可能是 `ResilientProvider`——两者协议一致，所以 orchestrator /
persona.generator / diary / distill **一行都不用改**。
"""

from ..config import settings
from .base import (
    LLMProvider,
    LLMRuntimeError,
    LLMStreamBrokenError,
    LLMTimeoutError,
    LLMUnavailableError,
    chunks,
)
from .circuit import CircuitBreaker
from .mock import MockProvider
from .router import ResilientProvider

__all__ = [
    "CircuitBreaker",
    "LLMProvider",
    "LLMRuntimeError",
    "LLMStreamBrokenError",
    "LLMTimeoutError",
    "LLMUnavailableError",
    "MockProvider",
    "ResilientProvider",
    "chunks",
    "get_provider",
]


def _build(name: str) -> LLMProvider:
    """按名字造一个 provider。空名返回 None 的语义由调用方处理。"""
    if name == "mock":
        return MockProvider()
    if name in ("openai", "openai_compat"):
        from .openai_compat import OpenAICompatProvider

        return OpenAICompatProvider()
    raise ValueError(f"未知模型提供者: {name}")


def get_provider() -> LLMProvider:
    """按配置装配 provider（含韧性层）。

    `LINGER_LLM=mock` 时**不套**韧性层：mock 不会失败，套上只会给演示
    和测试加一层线程开销和一个假的降级故事。

    `LINGER_LLM_FALLBACK` 是备 provider；空字符串表示不降级。
    """
    primary = _build(settings.llm_provider)
    if settings.llm_provider == "mock":
        return primary

    fallbacks: list[LLMProvider] = []
    fallback_name = (settings.llm_fallback or "").strip()
    if fallback_name:
        try:
            fallbacks.append(_build(fallback_name))
        except (ValueError, RuntimeError) as exc:
            # 备 provider 构造失败（比如配了 openai 却没给 key）不该让主链路起不来：
            # 降级是「多一层保险」，不是启动前置条件。
            print(f"[linger] 备模型 {fallback_name} 不可用，本次不降级：{exc}")

    return ResilientProvider(
        primary,
        fallbacks,
        first_chunk_timeout=settings.llm_timeout,
        total_timeout=settings.llm_total_timeout,
        retries_per_provider=settings.llm_retries,
        breaker_factory=lambda: CircuitBreaker(
            fail_threshold=settings.llm_breaker_threshold,
            cooldown_seconds=settings.llm_breaker_cooldown,
        ),
    )
