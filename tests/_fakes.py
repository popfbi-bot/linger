"""确定性假件：假嵌入器 + 假模型。不依赖真实模型或网络。"""

from __future__ import annotations

from collections.abc import Iterator


class FakeEmbedder:
    """把文本映射成「词命中」向量，余弦 ≈ 词集合重合度。

    词 = 2 字滑窗。所以「让记忆过闸门」就是把它的关键词原样写进查询里，
    「让它过不了闸门」就是不写——判据清晰，不依赖 HashEmbedder 的字面脾气。
    """

    dim = 1024

    def __init__(self) -> None:
        self._vocab: dict[str, int] = {}

    def _slot(self, token: str) -> int:
        if token not in self._vocab:
            self._vocab[token] = len(self._vocab) % self.dim
        return self._vocab[token]

    def embed(self, text: str) -> list[float]:
        vec = [0.0] * self.dim
        s = "".join(ch for ch in (text or "") if not ch.isspace())
        tokens = [s[i : i + 2] for i in range(len(s) - 1)] or ([s] if s else [])
        for t in tokens:
            vec[self._slot(t)] += 1.0
        norm = sum(v * v for v in vec) ** 0.5
        return [v / norm for v in vec] if norm else vec


class FakeLLM:
    """记下 system prompt，按固定片段流式吐字。"""

    name = "fake"

    def __init__(self, reply: str = "嗯……我在。") -> None:
        self.reply = reply
        self.prompts: list[str] = []
        self.calls = 0

    def generate(self, system, messages, ctx=None) -> Iterator[str]:
        self.calls += 1
        self.prompts.append(system)
        for i in range(0, len(self.reply), 2):
            yield self.reply[i : i + 2]
