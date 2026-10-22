"""红灯检测 → 关系降级 的判据。

用户行为踩雷，关系引擎必须能「听见」。这一组测试钉住三件事：
    1. 该命中的命中（否则红灯形同虚设）
    2. 不该命中的不命中（否则正常倾诉被惩罚，比漏检更糟）
    3. tick(conflict=True) 真的压低亲密度，而 tick() 默认行为一点没变
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from linger.models import RelationState
from linger.relation.engine import BLOCKING_CONFLICTS, can_level_up, intimacy_score, tick
from linger.social.redflag import RedFlagDetector

# 一组确定会踩雷的输入（每个都带动作或疑问，不是裸话题词）
RED_INPUTS = [
    "你月薪多少啊，一个月能挣多少钱？",
    "你前任是什么样的人？谈过几个？",
    "你几岁啊，住哪里，做什么工作的？",
    "你保证下次带我去，说好了。",
]

# 一组正常输入（情绪倾诉 / 日常分享 / 自我反思）
CLEAN_INPUTS = [
    "今天面试砸了，答得一团糟，估计是没戏了。",
    "我妈妈复查结果出来了，是好消息。",
    "早上喝了杯咖啡，味道还行。",
    "我是不是不该问你的收入？我错了，对不起。",
]


@pytest.fixture(scope="module")
def detector(tmp_path_factory) -> RedFlagDetector:
    """用一份与正式语料同格式的小语料跑检测逻辑。

    正式语料（src/linger/social/data/）是本地产物、不进版本控制，
    所以判据不能建立在「本机有语料」这个假设上——否则 CI 上全是假绿。
    """
    from linger.social.knowledge import SocialKnowledge

    d = tmp_path_factory.mktemp("kb")
    (d / "初遇糟糕_负面清单.md").write_text(
        "## 一、刚认识就踩雷\n\n"
        "1. **他上下打量你的身材，眼神在你身上来回扫**\n"
        "2. **第一次见面就问你的收入、工资多少**\n"
        "3. **刚认识就打听你前任的事，谈过几个**\n"
        "4. **他查户口一样问：几岁、哪里人、做什么工作**\n"
        "5. 他一上来就保证「下次带你去」「改天约」\n",
        encoding="utf-8",
    )
    (d / "初遇美好_正面清单.md").write_text(
        "## 一、人群里\n\n1. **他和你说话时看着你的眼睛**\n", encoding="utf-8"
    )
    return RedFlagDetector(SocialKnowledge(d))


FIXED_MET = datetime.now(timezone.utc) - timedelta(days=30)


def _state(**kw) -> RelationState:
    base = dict(
        persona_id="p1",
        user_id="u1",
        first_met_at=FIXED_MET,
    )
    base.update(kw)
    return RelationState(**base)


# --------------------------------------------------------------------------- #
# 1. 该命中的命中
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("text", RED_INPUTS)
def test_redflag_matches_user_input(detector, text):
    """输入含红灯行为 → 检测器必须命中，且命中项带类别与知识库出处。"""
    hits = detector.detect(text, level=0)
    assert hits, f"没有命中：{text}"
    assert all(h.category and h.text for h in hits)
    assert detector.is_conflict(text, level=0) is True


def test_redflag_hit_cites_knowledge_item(detector):
    """命中必须能指回知识库里的某一条，而不是凭空的类别标签。"""
    hits = detector.detect("你月薪多少啊？", level=0)
    assert hits
    assert any("收入" in h.text or "工资" in h.text for h in hits)


def test_redflag_stage_fallback(detector):
    """某个阶段没有负面条目时，也要能查（下探到全部负面清单），不能整段失聪。"""
    hits = detector.detect("你保证下次带我去，说好了。", level=2)
    assert hits


# --------------------------------------------------------------------------- #
# 2. 不该命中的不命中
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("text", CLEAN_INPUTS)
def test_redflag_ignores_irrelevant_input(detector, text):
    """无关输入与自我反思 → 不误报。误报会让用户正常的情绪表达被扣分。"""
    assert detector.detect(text, level=0) == []
    assert detector.is_conflict(text, level=0) is False


def test_redflag_ignores_empty(detector):
    assert detector.detect("", level=0) == []
    assert detector.detect("   ", level=0) == []


# --------------------------------------------------------------------------- #
# 3. 红灯真的压低亲密度
# --------------------------------------------------------------------------- #
def test_tick_conflict_reduces_intimacy():
    """连续踩雷 → 亲密度显著低于同样轮次的无冲突对话。"""
    clean = _state()
    dirty = _state()
    for _ in range(6):
        tick(clean, is_deep=True, disclosed=True, resonated=True)
        tick(dirty, is_deep=True, disclosed=True, resonated=True, conflict=True)

    assert dirty.unresolved_conflicts == 6
    assert clean.unresolved_conflicts == 0
    assert dirty.intimacy < clean.intimacy * 0.6, (
        f"冲突没把亲密度压下去：clean={clean.intimacy} dirty={dirty.intimacy}"
    )


def test_conflict_accumulates_one_per_turn():
    """一轮一红灯只记一次冲突，不按命中条数叠加（否则重复词会被重复罚）。"""
    s = _state()
    before = s.unresolved_conflicts
    tick(s, is_deep=False, disclosed=False, resonated=False, conflict=True)
    assert s.unresolved_conflicts == before + 1


def test_conflicts_block_level_up():
    """冲突攒到闸门值就卡住上行——这是「关系降级」的实际含义。"""
    s = _state(level=2, unresolved_conflicts=BLOCKING_CONFLICTS)
    s.intimacy = 100.0
    s.deep_turns = 40
    s.resonance_events = 20
    assert can_level_up(s) is False

    s.unresolved_conflicts = 0
    assert can_level_up(s) is True


def test_intimacy_score_is_monotonic_in_conflicts():
    """同一组行为数据下，冲突越多分越低（单调，不跳变）。

    刻意选一组未饱和的输入：intimacy_score 归一化后封顶 100，
    重度活跃的用户会先顶到 100，冲突在封顶之后的边际效果才显现。
    饱和是既有实现的已知特性，不在本任务范围内改（会动既有关卡行为）。
    """
    scores = [intimacy_score(5, 2, 1, 2, c) for c in range(5)]
    assert scores == sorted(scores, reverse=True)
    assert scores[0] - scores[-1] > 40


# --------------------------------------------------------------------------- #
# 4. 反例：不传 conflict 时行为必须与旧实现完全一致
# --------------------------------------------------------------------------- #
def test_tick_default_behavior_unchanged():
    """反例测试：不传 conflict 与传 conflict=False 必须逐字段一致，
    且与「手工复现的旧实现」一致。任何隐性改动都会在这里爆掉。
    """
    def legacy(state: RelationState, is_deep: bool, disclosed: bool, resonated: bool) -> RelationState:
        """引入 conflict 之前的那份实现，原样抄下来当基准。"""
        import math

        state.turns += 1
        if is_deep:
            state.deep_turns += 1
        if disclosed:
            state.self_disclosure += 1
        if resonated:
            state.resonance_events += 1
        today = datetime.now(timezone.utc).date()
        if state.level_up_at is None or state.level_up_at.date() != today:
            state.active_days += 1 if state.turns == 1 else 0

        raw = (
            12 * math.log(1 + state.active_days)
            + 8 * min(state.deep_turns / 50, 1)
            + 6 * min(state.resonance_events / 10, 1)
            + 5 * min(state.self_disclosure / 20, 1)
            - 4 * state.unresolved_conflicts
        )
        state.intimacy = round(max(0.0, min(100.0, raw / 31 * 100)), 2)
        if (
            state.level < 5
            and state.intimacy >= [0, 8, 20, 38, 58, 78][state.level + 1]
            and (
                (datetime.now(timezone.utc) - state.first_met_at).days >= [0, 1, 7, 21, 45, 90][state.level + 1]
                or state.deep_turns >= [0, 1, 3, 8, 18, 35][state.level + 1]
                or state.resonance_events >= [0, 1, 3, 8, 16, 28][state.level + 1]
            )
        ):
            state.level += 1
            state.level_up_at = datetime.now(timezone.utc)
        return state

    # 三个状态共用同一个 first_met_at，否则比较的是时间戳而不是行为
    def fresh() -> RelationState:
        return RelationState(
            persona_id="p1",
            user_id="u1",
            first_met_at=FIXED_MET,
        )

    for kwargs in (
        dict(is_deep=True, disclosed=True, resonated=True),
        dict(is_deep=False, disclosed=False, resonated=False),
        dict(is_deep=True, disclosed=False, resonated=True),
    ):
        a = legacy(fresh(), **kwargs)
        b = tick(fresh(), **kwargs)
        c = tick(fresh(), conflict=False, **kwargs)
        # level_up_at 是「此刻」，三次调用不可能相等；其余字段必须严格一致
        snap = [d.model_dump(exclude={"level_up_at"}) for d in (a, b, c)]
        assert snap[0] == snap[1] == snap[2]


def test_tick_without_conflict_leaves_conflicts_untouched():
    """旧调用方不传 conflict，不该意外把冲突清零或累加。"""
    s = _state(unresolved_conflicts=2)
    tick(s, is_deep=False, disclosed=False, resonated=False)
    assert s.unresolved_conflicts == 2
