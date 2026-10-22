"""关系引擎：等级、亲密度、越界边界（docs/ARCHITECTURE.md §3.4 + §13.3 修正 1）。

核心约束：关系只能靠「时间」或「共鸣」累积，不能靠「高频」刷。
v0.2 修订：时间与深度是「或」的关系，不是「与」——脉冲式使用的用户也能推进。
"""

from __future__ import annotations

import math
from datetime import datetime, timezone

from ..models import RelationState

LEVEL_NAMES = ["陌生", "认识", "熟人", "朋友", "亲近", "依赖"]

# 亲密度门槛（0~100 归一化后）
THRESHOLD = [0, 8, 20, 38, 58, 78]

# 天数门槛（或条件之一）
MIN_DAYS = [0, 1, 7, 21, 45, 90]

# 深度对话门槛（或条件之二）—— 一次深夜长谈抵十天寒暄
MIN_DEEP_TURNS = [0, 1, 3, 8, 18, 35]

# 情绪共振门槛（或条件之三）
MIN_RESONANCE = [0, 1, 3, 8, 16, 28]


def intimacy_score(
    active_days: int,
    deep_turns: int,
    resonance_events: int,
    self_disclosure: int,
    unresolved_conflicts: int,
) -> float:
    raw = (
        12 * math.log(1 + active_days)
        + 8 * min(deep_turns / 50, 1)
        + 6 * min(resonance_events / 10, 1)
        + 5 * min(self_disclosure / 20, 1)
        - 4 * unresolved_conflicts
    )
    # 理论上限 31 → 归一到 0~100
    return max(0.0, min(100.0, raw / 31 * 100))


def refresh(state: RelationState) -> RelationState:
    state.intimacy = round(
        intimacy_score(
            state.active_days,
            state.deep_turns,
            state.resonance_events,
            state.self_disclosure,
            state.unresolved_conflicts,
        ),
        2,
    )
    return state


# 未消解冲突的硬闸门：攒够这么多，关系就卡住不再上行（§3.4 的「降级」）。
BLOCKING_CONFLICTS = 3


def can_level_up(state: RelationState, now: datetime | None = None) -> bool:
    now = now or datetime.now(timezone.utc)
    if state.level >= len(LEVEL_NAMES) - 1:
        return False

    # 红灯攒着没消解，就别谈更近一步
    if state.unresolved_conflicts >= BLOCKING_CONFLICTS:
        return False

    target = state.level + 1
    if state.intimacy < THRESHOLD[target]:
        return False

    days = (now - state.first_met_at).days
    return (
        days >= MIN_DAYS[target]
        or state.deep_turns >= MIN_DEEP_TURNS[target]
        or state.resonance_events >= MIN_RESONANCE[target]
    )


def tick(
    state: RelationState,
    is_deep: bool,
    disclosed: bool,
    resonated: bool,
    conflict: bool = False,
) -> RelationState:
    """一轮对话后的微调。

    conflict=True 表示这一轮用户踩了人际红灯（由 social.redflag 判定）。
    红灯不扣别的，只累加 unresolved_conflicts —— 未消解的冲突按 §3.4 压低亲密度，
    且它不会自己消失（没有人来道歉，这件事就一直挂着）。
    默认 False 时行为与引入该参数前完全一致。
    """
    state.turns += 1
    if is_deep:
        state.deep_turns += 1
    if disclosed:
        state.self_disclosure += 1
    if resonated:
        state.resonance_events += 1
    if conflict:
        state.unresolved_conflicts += 1

    today = datetime.now(timezone.utc).date()
    if state.level_up_at is None or state.level_up_at.date() != today:
        # 简化的活跃天数统计：同一天只算一次。生产用 turn 表 group by date。
        state.active_days += 1 if state.turns == 1 else 0

    refresh(state)
    if can_level_up(state):
        state.level += 1
        state.level_up_at = datetime.now(timezone.utc)
    return state


def boundaries(level: int) -> str:
    """越界防护：注入编译器的硬约束（§3.4.3）。"""
    table = {
        0: "你们刚认识。不要表达情感，不要用昵称，不要提及任何过往记忆，不要装作熟稔。",
        1: "你们刚认识。可以礼貌回应，不主动展开私人话题。",
        2: "算熟人了。可以聊日常，可以表达轻度关心，不涉及私密话题。",
        3: "是朋友了。可以表达情绪共鸣，可以提及共同经历，但仍然叫「你」。",
        4: "关系亲近。可以用昵称，可以主动关心生活，可以提起从前的承诺。",
        5: "关系很深。维持亲密，但要在合适的时候提醒对方：现实里的人也在等他。",
    }
    return table.get(level, table[5])


def addressing(level: int, card_aliases: list[str]) -> str:
    if level >= 4 and card_aliases:
        return card_aliases[0]
    return "你"
