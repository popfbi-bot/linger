"""混合检索：打分 / 闸门 / MMR 去冗余（docs/ARCHITECTURE.md §3.3.2 ④）。

设计要点（P2：记得对 > 记得多）：
    · 相关性闸门 —— 相似度不够的记忆直接丢弃，无论重要度多高。
    · MMR 去冗余 —— 五条同义记忆塞进上下文只会放大偏见。
    · 打分可解释 —— 每一项分数都要能在 trace 里看到。
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime, timezone

from ..models import MemoryNode
from .embedding import cosine

WEIGHTS = {
    "sim": 0.42,
    "recency": 0.20,
    "importance": 0.18,
    "arousal": 0.08,
    "relation": 0.07,
    "recall": 0.05,
}


@dataclass
class Scored:
    node: MemoryNode
    score: float
    parts: dict[str, float] = field(default_factory=dict)

    def why(self) -> str:
        """给 trace 用的一句话归因。"""
        top = sorted(self.parts.items(), key=lambda kv: -kv[1])[:2]
        label = {"sim": "语义", "recency": "新鲜", "importance": "重要", "arousal": "高唤醒",
                 "relation": "关联", "recall": "被反复唤起"}
        return "+".join(label.get(k, k) for k, _ in top)


@dataclass
class RetrievalResult:
    query: str
    candidates: int
    passed_gate: int
    selected: list[Scored]
    dropped_below_gate: int
    dropped_by_mmr: int

    @property
    def nodes(self) -> list[MemoryNode]:
        return [s.node for s in self.selected]


def _recency(created: datetime, now: datetime, tau_days: float) -> float:
    if created.tzinfo is None:
        created = created.replace(tzinfo=timezone.utc)
    days = max(0.0, (now - created).total_seconds() / 86400.0)
    return math.exp(-days / tau_days)


def score_one(
    query_vec: list[float],
    node: MemoryNode,
    now: datetime,
    tau_days: float = 30.0,
    entity_hit: float = 0.0,
) -> Scored:
    sim = cosine(query_vec, node.embedding or [])
    arousal = abs(float(node.emotion.get("arousal", 0.0)))
    parts = {
        "sim": sim,
        "recency": _recency(node.created_at, now, tau_days),
        "importance": node.importance,
        "arousal": arousal,
        "relation": entity_hit,
        "recall": min(node.recall_count / 5.0, 1.0),
    }
    total = sum(WEIGHTS[k] * v for k, v in parts.items())
    return Scored(node=node, score=round(total, 4), parts={k: round(v, 3) for k, v in parts.items()})


def mmr(cands: list[Scored], k: int, lam: float = 0.7) -> tuple[list[Scored], int]:
    """Maximal Marginal Relevance —— 在"相关"和"不重复"之间取平衡。"""
    pool = list(cands)
    chosen: list[Scored] = []
    while pool and len(chosen) < k:
        best, best_val = None, -1e9
        for c in pool:
            redundancy = max(
                (cosine(c.node.embedding or [], s.node.embedding or []) for s in chosen), default=0.0
            )
            val = lam * c.score - (1 - lam) * redundancy
            if val > best_val:
                best, best_val = c, val
        assert best is not None
        chosen.append(best)
        pool.remove(best)
    return chosen, len(pool)


def retrieve(
    query: str,
    query_vec: list[float],
    nodes: list[MemoryNode],
    k: int = 8,
    gate: float = 0.35,
    tau_days: float = 30.0,
    entities: set[str] | None = None,
    now: datetime | None = None,
) -> RetrievalResult:
    now = now or datetime.now(timezone.utc)
    entities = entities or set()

    scored: list[Scored] = []
    for n in nodes:
        if n.status != "active":
            continue
        hit = 1.0 if entities and (set(n.entities) & entities) else 0.0
        scored.append(score_one(query_vec, n, now, tau_days, hit))

    # 相关性闸门：不相关的记忆宁可不用
    passed = [s for s in scored if s.parts["sim"] >= gate]
    selected, dropped_mmr = mmr(passed, min(k, len(passed)))

    return RetrievalResult(
        query=query,
        candidates=len(scored),
        passed_gate=len(passed),
        selected=selected,
        dropped_below_gate=len(scored) - len(passed),
        dropped_by_mmr=dropped_mmr,
    )


def decayed_retention(node: MemoryNode, now: datetime | None = None) -> float:
    """艾宾浩斯修正版：retention = exp(-Δt / S)，S 由重要度与复习次数决定。"""
    now = now or datetime.now(timezone.utc)
    s = 30.0 * (1 + 0.4 * node.importance) * (1 + 0.3 * node.recall_count)
    created = node.created_at if node.created_at.tzinfo else node.created_at.replace(tzinfo=timezone.utc)
    days = max(0.0, (now - created).total_seconds() / 86400.0)
    return math.exp(-days / s)


def should_archive(node: MemoryNode, now: datetime | None = None) -> bool:
    """归档而非删除。用户能看见"她忘了什么"，但记忆不该被销毁。"""
    if node.pinned or node.layer >= 4:
        return False
    return decayed_retention(node, now) < 0.25 and node.importance < 0.5
