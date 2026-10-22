"""issue #13：`SocialKnowledge.companion_profiles()` 必须真的返回人生阶段陪伴需求。

老实现写死了 `section == "上篇：按人生阶段梳理陪伴需求"`——那是「上篇」这个
`##` 大标题，从来不是任何一条条目所属的 `###` 章节名，所以恒返回 `[]`。
下面的用例把它钉死：

    1. 必须非空（空列表就是缺陷本身）；
    2. 每一条都必须落在某个已知人生阶段章节里（不能拿陪伴「角色类型」凑数）；
    3. 语料改了条数跟着变（证明取自语料，不是硬编码文案）；
    4. `companion_pitfalls(life_stage)` 的既有行为不受影响。
"""

from __future__ import annotations

import re

import pytest

from linger.social import get_knowledge
from linger.social.knowledge import SocialItem, is_life_stage_section

# 语料里上篇真实存在的人生阶段章节（比引擎的 8 个阶段更细）
_CORPUS_STAGES = (
    "胎儿期", "婴儿期", "幼儿期", "童年期", "学龄期",
    "青春期", "青年期", "中年期", "更年期", "准老年期", "老年期",
)

# 下篇「按陪伴角色类型梳理」的章节——绝不能被当成人生阶段
_ROLE_SECTIONS = ("亲情陪伴", "友情陪伴", "师长与引路人", "社会与事业陪伴", "情感与亲密陪伴")


@pytest.fixture(scope="module")
def kb():
    return get_knowledge()


def _profile_items(kb) -> list[SocialItem]:
    """上篇（人生阶段）的正面条目——用大标题 + 章节名双重判定，避开下篇「亲情陪伴」的撞词。"""
    return [
        i for i in kb.by_stage("陪伴")
        if i.polarity == "companion"
        and "人生阶段" in i.heading
        and "角色类型" not in i.heading
        and is_life_stage_section(i.section)
    ]


def test_companion_profiles_not_empty(kb):
    """判据 1：不能是空列表。老实现返回 []，这条就是它的红灯。"""
    profiles = kb.companion_profiles()
    assert len(profiles) > 50, f"上篇语料远不止 {len(profiles)} 条，返回这么少说明过滤条件不对"


def test_every_profile_belongs_to_known_life_stage(kb):
    """判据 2：每条都必须落在某个已知人生阶段章节（按条目所属 section 反查）。"""
    sections = {i.section for i in _profile_items(kb)}
    assert sections, "上篇的人生阶段章节一个都没识别出来"
    for section in sections:
        assert re.match(r"^[一二三四五六七八九十]+、", section), section
        assert any(stage in section for stage in _CORPUS_STAGES), f"{section} 不是人生阶段章节"

    allowed = {i.text for i in _profile_items(kb)}
    profiles = kb.companion_profiles()
    assert set(profiles) <= allowed, "有返回条目不属于任何人生阶段章节"


def test_profiles_cover_all_corpus_stages(kb):
    """每个真实人生阶段章节都要有产出——漏阶段等于残缺。"""
    items = _profile_items(kb)
    sections = {i.section for i in items}
    for stage in _CORPUS_STAGES:
        assert any(stage in s for s in sections), f"{stage} 没有被识别成人生阶段章节"
    profiles = kb.companion_profiles()
    for text in {i.text for i in items}:
        assert text in profiles


def test_profiles_exclude_role_sections(kb):
    """下篇是「陪伴角色类型」，不是人生阶段，一条都不能混进来。"""
    profiles = kb.companion_profiles()
    role_items = {
        i.text for i in kb.by_stage("陪伴")
        if i.polarity == "companion"
        and any(name in i.section for name in _ROLE_SECTIONS)
    }
    assert role_items, "下篇条目应该存在，否则这条判据失去意义"
    assert not (set(profiles) & role_items)


def test_profiles_follow_corpus(kb):
    """判据 3：条数等于语料里上篇条目数——语料改了它跟着变，不是硬编码。"""
    expected = [i.text for i in _profile_items(kb)]
    assert kb.companion_profiles() == expected


def test_pitfalls_behaviour_unchanged(kb):
    """判据 4：`companion_pitfalls(life_stage)` 保持原样（它本来是对的）。"""
    assert kb.companion_pitfalls("老年期"), "老年期负面条目应存在"
    assert len(kb.companion_pitfalls()) == len(kb.companion_pitfalls(None))
    all_pitfalls = kb.companion_pitfalls(limit=999)
    assert len(all_pitfalls) > len(kb.companion_pitfalls("老年期"))


def test_needs_for_generic_now_hits_profiles(kb):
    """阶段未知时的通用需求，现在直接来自 companion_profiles()，不再靠回退。"""
    from linger.companion.profile import needs_for

    assert needs_for("", kb) == kb.companion_profiles()[:5]
