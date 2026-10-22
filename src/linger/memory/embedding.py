"""嵌入器抽象。

骨架用 HashEmbedder：字符 n-gram 哈希，零依赖、确定性、可离线。
对中文来说，字 bigram 的重叠其实就是一种朴素词法相似度，足够演示检索链路。

生产替换：BGE-M3 / text-embedding-3-large（1024 维），实现同一个 Protocol 即可。
"""

from __future__ import annotations

import hashlib
import math
import re
from typing import Protocol

_CJK = re.compile(r"[\u4e00-\u9fff]")
_PUNCT = re.compile(r"[\s\u3001-\u303f\uff00-\uff0f\uff1a-\uff20,.!?;:'\"()\[\]<>/\\|~`@#$%^&*+=_-]+")


class Embedder(Protocol):
    dim: int

    def embed(self, text: str) -> list[float]: ...


class HashEmbedder:
    def __init__(self, dim: int = 256) -> None:
        self.dim = dim

    def _bucket(self, token: str) -> int:
        h = hashlib.blake2b(token.encode("utf-8"), digest_size=4).digest()
        return int.from_bytes(h, "big") % self.dim

    def embed(self, text: str) -> list[float]:
        vec = [0.0] * self.dim
        s = _PUNCT.sub(" ", (text or "").lower())
        for chunk in s.split():
            for i, ch in enumerate(chunk):
                vec[self._bucket("u:" + ch)] += 0.5
                if i + 1 < len(chunk):
                    vec[self._bucket("b:" + chunk[i : i + 2])] += 1.0
        norm = math.sqrt(sum(v * v for v in vec))
        if norm == 0:
            return vec
        return [v / norm for v in vec]


def cosine(a: list[float], b: list[float]) -> float:
    if not a or not b or len(a) != len(b):
        return 0.0
    return float(sum(x * y for x, y in zip(a, b)))


def get_embedder(kind: str = "hash", dim: int = 256) -> Embedder:
    if kind == "hash":
        return HashEmbedder(dim)
    raise ValueError(f"未知的 embedder: {kind}（骨架阶段仅支持 hash）")
