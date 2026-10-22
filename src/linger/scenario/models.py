"""结构化场景模型。

一条硬约定：**节拍推进不用模型**。
`Beat.trigger` 是关键词列表，命中即推进——确定性、可复现、可在测试里断言。
小模型判定听上去更聪明，但它把「场景走到哪了」变成一个不可复现的随机量，
排查成本远高于收益（§3.5 原方案里的「关键词 + 小模型」先只做关键词这一半）。
"""

from __future__ import annotations

from pydantic import BaseModel, Field


class Beat(BaseModel):
    """一个剧情节拍。松约束：它是「还没发生的事」，不是必须按顺序打勾的任务。"""

    id: str
    goal: str
    trigger: list[str] = Field(default_factory=list)
    done: bool = False


class Scenario(BaseModel):
    id: str
    title: str
    world: str = ""
    setting: str = ""
    opening_narration: str = ""
    user_role: str = ""
    beats: list[Beat] = Field(default_factory=list)
    constraints: list[str] = Field(default_factory=list)
    exit_conditions: list[str] = Field(default_factory=list)

    # ---- 节拍状态 ---------------------------------------------------- #
    def pending_beats(self) -> list[Beat]:
        return [b for b in self.beats if not b.done]

    def done_beats(self) -> list[str]:
        return [b.id for b in self.beats if b.done]

    def advance(self, user_text: str) -> list[str]:
        """按关键词推进节拍，返回本轮新达成的 beat id。

        命中即 done，一次可以推进多个（用户一句话里同时提到两件事）。
        已完成的节拍不会重复返回，也不会重复标记。
        """
        text = (user_text or "").strip()
        if not text:
            return []
        newly: list[str] = []
        for beat in self.beats:
            if beat.done:
                continue
            if any(k and k in text for k in beat.trigger):
                beat.done = True
                newly.append(beat.id)
        return newly

    # ---- 进度快照 ---------------------------------------------------- #
    def progress(self) -> dict:
        """可持久化的最简结构（SQLite 里存的就是这个）。"""
        return {"scenario_id": self.id, "done_beats": self.done_beats()}

    def restore(self, data: dict | None) -> Scenario:
        """把持久化的进度贴回模型；未知 beat id 忽略（YAML 改过也不会炸）。"""
        done = set((data or {}).get("done_beats") or [])
        for beat in self.beats:
            beat.done = beat.id in done
        return self

    def state(self) -> dict:
        """给 done 事件 / GET /v1/sessions/{id} 的场景状态摘要。"""
        return {
            "scenario_id": self.id,
            "title": self.title,
            "done_beats": self.done_beats(),
            "pending_beats": [b.id for b in self.pending_beats()],
        }
