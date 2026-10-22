"""模型抽象。

业务层只依赖 `LLMProvider.generate()`，不关心背后是 mock、OpenAI 还是本地模型。
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any, Protocol


class LLMProvider(Protocol):
    name: str

    def generate(
        self,
        system: str,
        messages: list[dict[str, str]],
        ctx: dict[str, Any] | None = None,
    ) -> Iterator[str]:
        """流式产出文本增量。ctx 携带结构化状态，mock 用得上，真实模型可忽略。"""
        ...


def chunks(text: str, size: int = 3) -> Iterator[str]:
    for i in range(0, len(text), size):
        yield text[i : i + size]
