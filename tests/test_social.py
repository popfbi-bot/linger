"""人际知识库的行为判据。

每个用例先弄坏实现、证明它红，再还原验证绿。
覆盖：
    1. 语料确实被解析成结构化条目（不是整块文本）
    2. 每个阶段都有可查询的行为边界
    3. 负面条目在边界输出中优先于正面（雷区优先，防出戏）
    4. 阶段映射正确（level → stage）
    5. 编译器把行为边界注入 system prompt
"""

from __future__ import annotations

import pytest

from linger.compose.compiler import compose
from linger.models import AffectState, PAD, PersonaCard, PersonaMeta, RelationState
from linger.social import get_knowledge, stage_for_level


@pytest.fixture(scope="module")
def kb():
    return get_knowledge()


def _card() -> PersonaCard:
    return PersonaCard(meta=PersonaMeta(name="林汐"))


# --------------------------------------------------------------------------- #
# 1. 语料确实被解析成结构化条目
# --------------------------------------------------------------------------- #
def test_corpus_parsed_into_items(kb):
    """必须能逐条查询，而不是一整块文本。"""
    items = kb.by_stage("初遇")
    assert len(items) > 400
    assert any("对视" in i.text for i in items[:100]), "初遇正面语料应包含「人群里的对视」"
    assert all(i.stage == "初遇" for i in items)


def test_negative_and_positive_both_present(kb):
    """每个关系阶段都必须有「不要做」与「可以这样做」。"""
    for stage in ("初遇", "升温"):
        items = kb.by_stage(stage)
        assert any(i.polarity == "positive" for i in items), f"{stage} 缺少正面"
        assert any(i.polarity == "negative" for i in items), f"{stage} 缺少负面"


def test_negative_priority_in_boundaries(kb):
    """雷区必须优先于甜点输出——防止出戏比增加心动更重要。"""
    b = kb.boundaries("升温", limit=6)
    assert "不要做" in b
    assert "可以这样做" in b
    # 负面块出现在正面块之前
    assert b.index("「不要做」") < b.index("「可以这样做」")


# --------------------------------------------------------------------------- #
# 2. 阶段映射
# --------------------------------------------------------------------------- #
def test_stage_for_level():
    assert stage_for_level(0) == "初遇"
    assert stage_for_level(1) == "初遇"
    assert stage_for_level(2) == "升温"
    assert stage_for_level(3) == "升温"
    assert stage_for_level(4) == "相处"
    assert stage_for_level(5) == "相处"


def test_red_flags_exist(kb):
    """红灯清单必须存在，且来自负面语料。"""
    flags = kb.red_flags("初遇")
    assert len(flags) > 0
    assert all(len(f) > 5 for f in flags)


# --------------------------------------------------------------------------- #
# 3. 编译器接入
# --------------------------------------------------------------------------- #
def test_social_block_injected_into_prompt():
    """行为边界必须出现在编译后的 system prompt 里。"""
    rel = RelationState(persona_id="p1", user_id="u1", level=2)
    affect = AffectState(pad=PAD(pleasure=0.2, arousal=0.3, dominance=0.4))
    prompt = compose(_card(), affect, rel, [], [])
    assert "人际行为边界" in prompt.system
    assert "不要做" in prompt.system


def test_social_block_missing_data_degrades_gracefully(monkeypatch):
    """语料缺失时编译器优雅降级，不抛异常、不阻断主链路。"""
    rel = RelationState(persona_id="p1", user_id="u1", level=2)
    affect = AffectState(pad=PAD(pleasure=0.2, arousal=0.3, dominance=0.4))

    import linger.social as social

    original = social.get_knowledge

    def broken():
        raise RuntimeError("知识库损坏")

    monkeypatch.setattr(social, "get_knowledge", broken)
    prompt = compose(_card(), affect, rel, [], [])
    assert "人际行为边界" not in prompt.system
    assert "你是林汐" in prompt.system  # 主链路不受影响
    monkeypatch.setattr(social, "get_knowledge", original)