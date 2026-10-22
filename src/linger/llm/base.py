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


# --------------------------------------------------------------------------- #
# 韧性层异常（M4-A）
# --------------------------------------------------------------------------- #
# 三个都继承 `LLMRuntimeError`，而不是裸 Exception：
# HTTP 层只需要捕获这一个基类就能把「模型层面挂了」翻译成 503，
# 不必为每个子类各写一遍。三个子类分开，是为了让调用方/日志能说清是哪种坏法。


class LLMRuntimeError(RuntimeError):
    """模型不可用的基类。HTTP 层据此返回 503。"""


class LLMUnavailableError(LLMRuntimeError):
    """所有 provider 都不可用（含被熔断跳过、空输出、试探失败）。"""


class LLMTimeoutError(LLMRuntimeError):
    """整体超时：从开始到结束超过了 total_timeout。"""


class LLMStreamBrokenError(LLMRuntimeError):
    """首 chunk 之后流断了。

    这一轮不能重试、不能降级——已经吐出去的字收不回来，
    换一个模型接着说，语气会当场断裂（用户听到「前半句 A 写的，后半句 B 写的」）。
    把异常抛给调用方，让它按「这一轮失败」处理，是更诚实的做法。
    """
