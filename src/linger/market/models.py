"""人格市场的两条记录：上架条目、安装副本。

**为什么是两张表，而不是给 `persona` 加一个 `listed` 字段**（判据 8）：
上架是人格之外的一层关系——同一个人格可以下架再上架（历史要留），
一个条目对应无数份安装副本。把状态塞进 `persona` 会让「人格是什么」
和「这个人格在市场里处于什么状态」纠缠在一起，删条目就等于改人格。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

# 状态机：pending_review → approved → unlisted
#          pending_review → rejected（终点）
STATUS_PENDING = "pending_review"
STATUS_APPROVED = "approved"
STATUS_REJECTED = "rejected"
STATUS_UNLISTED = "unlisted"

#: 允许出现在公共列表里的唯一状态（判据 4）。
PUBLIC_STATUSES = (STATUS_APPROVED,)


@dataclass
class MarketListing:
    """一条上架记录。

    `persona_snapshot` 是**上架那一刻的人格卡字节**，不是人格 id 的引用。
    审核过的字节必须等于分发的字节，否则审核就是摆设——所以这里存快照，
    不回查活人格。（代价是快照占空间，量级是「人格数」不是「对话数」。）
    """

    id: str
    persona_id: str
    author_id: str
    title: str
    summary: str
    snapshot: dict[str, Any]
    status: str = STATUS_PENDING
    listed_at: datetime | None = None
    reviewed_at: datetime | None = None
    reviewer_id: str = ""
    review_note: str = ""
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass
class InstalledCopy:
    """一条「某人装了某条目」的记录。

    副本本身是 `persona` 表里的一行（新 id、归属安装者），
    这张表只是**关系**：它让「同一账号装同一条目」可以幂等，
    也给副本留下来源（判据 6 的 `provenance.origin` 落库侧的证据）。
    """

    listing_id: str
    user_id: str
    persona_id: str
    installed_at: datetime
