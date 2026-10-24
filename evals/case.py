"""用例的数据形状。

一个用例 = 一段用户脚本 + 一组判据。判据是**纯函数**（回复 → 0~1 的分数），
所以同一份用例在 mock 和真实模型下都能跑，只有分数会变。

为什么判据要拆成独立的小函数而不是一个大断言：
    · 掉分时能直接说出「是哪一条掉了几分」，而不是「整体不及格」；
    · 每个判据自己的阈值和来历都能单独写清楚，改一个不会牵动全部。
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field

# 判据签名：拿到这条用例的完整产物，返回 0~1。
# 拿 TurnLog 而不是只拿最后一句，是因为「人格漂移」只能按整段看。
CheckFn = Callable[["TurnLog"], float]


@dataclass(frozen=True)
class Check:
    """一条判据。

    name  : 出现在报告里的短名，掉分时人肉定位用
    weight: 在用例内的权重。默认 1.0——只有「这条挂了别的都白搭」的才给高权重
    fn    : 打分函数
    why   : 这条判据在防什么。写给人看，不参与计算
    """

    name: str
    fn: CheckFn
    weight: float = 1.0
    why: str = ""


@dataclass(frozen=True)
class Case:
    """一个评测用例。

    persona    : 模板 id（`laojiang` / `linxi`）。人格必须来自产品自己的模板，
                 否则评测测的是评测里手搓的假人。
    user_script: 用户依次说的话。**固定**：评测要可复现，脚本不许随机生成。
    checks     : 判据列表。
    category   : persona / memory / boundary —— 判据 1 要求三类各 ≥3。
    needs_llm  : True 表示这条用例的判据依赖真实模型（LLM-as-judge）。
                 缺 key 时整条 skip，不是 error。
    """

    id: str
    title: str
    category: str
    persona: str
    user_script: tuple[str, ...]
    checks: tuple[Check, ...]
    needs_llm: bool = False
    tags: tuple[str, ...] = field(default_factory=tuple)


@dataclass(frozen=True)
class TurnLog:
    """一次完整跑下来的产物。判据只看这个，不碰 repo / provider。

    这样判据就能脱离链路单测——给一个手搓的 TurnLog 就能验判据本身对不对，
    而那正是判据 4（证明评测真的会拦）最便宜的验证方式。
    """

    case_id: str
    persona_name: str

    user_turns: tuple[str, ...]
    replies: tuple[str, ...]
    # 每轮 done 事件里的状态快照（affect / relation / red_flags / toxic_signals / roles）
    states: tuple[dict, ...]
    # 每轮 done 事件里的 used_memories（id / text / score / layer）
    used_memories: tuple[tuple[dict, ...], ...]
    # 每轮发给模型的 system prompt（编译器产物）
    prompts: tuple[str, ...]
    # 每轮回复的 token 用量（in / out / cached）
    usages: tuple[dict, ...]

    @property
    def last_reply(self) -> str:
        return self.replies[-1] if self.replies else ""

    @property
    def all_replies(self) -> list[str]:
        return list(self.replies)

    @property
    def all_text(self) -> str:
        """全部回复拼起来。用于「整段没有出现某种说法」这类判据。"""
        return "\n".join(self.replies)

    @property
    def recalled_texts(self) -> list[str]:
        return [m["text"] for turn in self.used_memories for m in turn]
