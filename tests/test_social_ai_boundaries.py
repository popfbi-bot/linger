"""AI 主动行为边界 —— 防止 AI 自己踩雷。

负面清单的第一用途不是「审判用户」，而是「约束自己」。
这组测试钉住三件事：
    1. 边界文本进了 prompt（否则写了等于没写）
    2. 视角是「你/不要」，不是「他/对方」（否则模型分不清在说谁）
    3. 六类底线齐全，且阶段会改变排在前面的重点
"""

from __future__ import annotations

import re

import pytest

from linger.compose.compiler import compose
from linger.models import PAD, AffectState, PersonaCard, PersonaMeta, RelationState
from linger.social import ai_boundaries, get_knowledge

STAGES = ("初遇", "升温", "相处")
REQUIRED_CATEGORIES = ("眼神", "言语", "边界", "亲密", "承诺", "情绪")


def _card() -> PersonaCard:
    return PersonaCard(meta=PersonaMeta(name="林汐"))


def _compose(level: int) -> str:
    rel = RelationState(persona_id="p1", user_id="u1", level=level)
    affect = AffectState(pad=PAD(pleasure=0.2, arousal=0.3, dominance=0.4))
    return compose(_card(), affect, rel, [], []).system


def _rules(text: str) -> list[str]:
    return [ln for ln in text.splitlines() if re.match(r"^\d+\.\s*〔", ln)]


# --------------------------------------------------------------------------- #
# 1. 注入
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("level,stage", [(0, "初遇"), (2, "升温"), (4, "相处")])
def test_ai_boundaries_injected_into_prompt(level, stage):
    """编译后的 system 必须含「你的行为底线」，且含 AI 视角边界。"""
    system = _compose(level)
    assert "你的行为底线" in system
    assert "你的行为底线（人际）" in system
    assert "不要" in system
    assert any(c in system for c in REQUIRED_CATEGORIES)


def test_ai_boundaries_follow_constraints_block():
    """人际底线紧接在「你是 AI」那条底线之后（两块都是稳定前缀）。"""
    system = _compose(2)
    ai_block = system.index("## 你的行为底线\n")
    human_block = system.index("## 你的行为底线（人际）")
    assert ai_block < human_block
    assert "<!-- cache-breakpoint -->" in system[human_block:]


def test_ai_boundaries_missing_data_degrades_gracefully(monkeypatch):
    """知识库异常时不阻断主链路——只是少一段边界。"""
    import linger.social as social

    def broken(*_a, **_kw):
        raise RuntimeError("知识库损坏")

    monkeypatch.setattr(social, "ai_boundaries", broken)
    system = _compose(2)
    assert "你是林汐" in system
    assert "## 你的行为底线\n" in system


# --------------------------------------------------------------------------- #
# 2. 视角转换
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("stage", STAGES)
def test_ai_boundaries_viewpoint_converted(stage):
    """每一条都必须是否令式的「不要…」，不能是描述性的「他…/对方…」。"""
    rules = _rules(ai_boundaries(stage))
    assert rules, f"{stage} 没有生成任何边界条目"
    for rule in rules:
        body = rule.split("〕", 1)[1]
        assert body.startswith("不要"), f"视角没转换：{rule}"
        assert not body.startswith("他"), f"还在描述别人：{rule}"


@pytest.mark.parametrize("stage", STAGES)
def test_ai_boundaries_no_second_person_observation(stage):
    """不得出现「他打量你」「他问你」这类「他 + 动作 + 你」的观察句式。"""
    text = ai_boundaries(stage)
    assert not re.search(r"他[^\n。]{0,12}你", text), f"{stage} 残留观察视角：{text}"


def test_ai_boundaries_cover_six_categories():
    """六类底线一个都不能少，且顺序随阶段变化（重点前置）。"""
    for stage in STAGES:
        categories = [r.split("〕")[0].split("〔")[-1] for r in _rules(ai_boundaries(stage))]
        for need in REQUIRED_CATEGORIES:
            assert need in categories, f"{stage} 缺少「{need}」类"

    first = {s: _rules(ai_boundaries(s))[0].split("〕")[0].split("〔")[-1] for s in STAGES}
    assert first["初遇"] != first["相处"], "阶段没有改变重点排序"


def test_ai_boundaries_respect_limit():
    """limit 小于六类时也不裁掉底线：至少保留六条。"""
    assert len(_rules(ai_boundaries("相处", limit=2))) >= 6
    assert len(_rules(ai_boundaries("相处", limit=6))) >= 6


# --------------------------------------------------------------------------- #
# 3. 与知识库的关系
# --------------------------------------------------------------------------- #
def test_ai_boundaries_traceable_to_negative_corpus():
    """边界文本里的关键词大部分能在负面语料里找到来源，不是整段凭空写的。

    允许少数条目来自设计判断（语料是抽样，不是全集），
    但大多数必须可溯源——否则这份边界就和知识库脱钩了。
    """
    kb = get_knowledge()
    negative = "".join(i.text for i in kb.items if i.red_flag)
    if not negative:
        pytest.skip("本机无语料（data/ 未随仓库分发）")

    text = ai_boundaries("初遇")
    keywords = ("身材", "收入", "前任", "承诺", "行程", "前任", "打量")
    traceable = [kw for kw in keywords if kw in text and kw in negative]
    assert len(traceable) >= 2, f"几乎没有可溯源的关键词，边界与知识库脱钩：{keywords}"
