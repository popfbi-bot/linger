"""流式事件载体（docs/ARCHITECTURE.md §5.3）。

本次只定义类型，不实现 HTTP 服务。事件序列固定为：
    thinking → affect → delta* → done

为什么把 affect 单独发一个事件：前端据此让头像表情、气泡颜色、打字速度变化。
情绪只体现在文本里，那它就只是一句话的事（§5.3）。
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

EventType = Literal["thinking", "affect", "delta", "done"]


class ServerEvent(BaseModel):
    type: str
    data: dict = Field(default_factory=dict)

    @classmethod
    def thinking(cls, stage: str, count: int | None = None, **extra: object) -> ServerEvent:
        data: dict = {"stage": stage}
        if count is not None:
            data["count"] = count
        data.update(extra)
        return cls(type="thinking", data=data)

    @classmethod
    def affect(cls, label: str, pad: dict[str, float], relation_level: int | None = None) -> ServerEvent:
        data: dict = {"label": label, **pad}
        if relation_level is not None:
            data["relation_level"] = relation_level
        return cls(type="affect", data=data)

    @classmethod
    def delta(cls, text: str) -> ServerEvent:
        return cls(type="delta", data={"text": text})

    @classmethod
    def done(
        cls,
        turn_id: int,
        used_memories: list[dict],
        state: dict,
        usage: dict,
    ) -> ServerEvent:
        # done 必须带齐这四项，否则前端与 trace 页面拿不到可解释性数据（§5.3）
        return cls(
            type="done",
            data={"turn_id": turn_id, "used_memories": used_memories, "state": state, "usage": usage},
        )


__all__ = ["EventType", "ServerEvent"]
