from ..config import settings
from .base import LLMProvider, chunks
from .mock import MockProvider

__all__ = ["LLMProvider", "MockProvider", "chunks", "get_provider"]


def get_provider() -> LLMProvider:
    if settings.llm_provider == "mock":
        return MockProvider()
    if settings.llm_provider in ("openai", "openai_compat"):
        from .openai_compat import OpenAICompatProvider

        return OpenAICompatProvider()
    raise ValueError(f"未知模型提供者: {settings.llm_provider}")
