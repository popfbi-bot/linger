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


# --------------------------------------------------------------------------- #
# 判定函数本身
#
# 上面那些用例验的是「端到端结果」，这里是「判定层」。缺这一段会出假绿：
# 本窗口验收 PR #14 时实测过——把 `_is_profiles_heading` 改成恒真、
# 去掉 `if "陪伴" in name` 那层，上面 7 条端到端用例**全绿**，
# 因为 `polarity == "companion"` 已经挡住了负面语料，那两层对当前语料是冗余的。
#
# 冗余不等于该删（语料新增文件时能兜住），但**没被测试覆盖的判据等于不存在**。
# 所以这里直接测判定函数，并逐条验证过「改坏会红」。
# --------------------------------------------------------------------------- #

from linger.social.knowledge import _is_profiles_heading, is_life_stage_section  # noqa: E402


@pytest.mark.parametrize("heading,expected", [
    ("上篇：按人生阶段梳理陪伴需求", True),
    ("上篇：按人生阶段梳理的陪伴需求", True),
    ("下篇：按陪伴角色类型梳理", False),
    ("下篇：按陪伴角色类型梳理的需求", False),
    ("", True),          # 语料没有 ## 分层时退回只看章节名
    ("随便一个标题", False),
])
def test_is_profiles_heading(heading, expected):
    """大标题必须能区分「人生阶段篇」与「角色类型篇」。

    反例：把 `return "人生阶段" in heading and "角色类型" not in heading`
    改成 `return True` → 后 3 条红（上篇/下篇分不开）。
    """
    assert _is_profiles_heading(heading) is expected


@pytest.mark.parametrize("section,expected", [
    ("一、胎儿期（出生前）", True),
    ("二、婴儿期（0-1岁）", True),
    ("八、成年/中年期（30—55岁）", True),
    ("一、学龄期", True),
    ("一、更年期/准老年期", True),
    # 下篇的章节。注意——这些 False 是「阶段名比对」挡的
    # （下篇标题里根本没有任何阶段名），**不是**「陪伴」二字挡的。
    ("一、亲情陪伴", False),
    ("一、友情陪伴", False),
    ("一、情感与亲密陪伴", False),
    # 不是人生阶段的章节
    ("一、目光交汇的瞬间", False),
    ("如何走出孤独与有毒关系", False),
    ("", False),
])
def test_is_life_stage_section(section, expected):
    """章节名必须能认出「序号 + 阶段名」。

    反例 1：去掉阶段名比对 → 5 条 True 用例红
    反例 2：去掉序号前缀要求 →「如何走出孤独与有毒关系」变 True
    """
    assert is_life_stage_section(section) is expected


def test_stage_name_matching_is_what_rejects_role_sections():
    """说清一件事：挡住下篇的是「阶段名比对」，不是「陪伴」二字。

    语料里下篇标题形如「一、亲情陪伴：血浓于水的牵绊」——不含任何阶段名，
    所以 `any(stage in name ...)` 直接 False。`if "陪伴" in name` 那层
    对当前语料不生效（死代码），它防的是「一、青春期陪伴」这种
    **阶段名与陪伴撞车**的未来章节。

    这条测试把那个事实钉住，免得后人误以为下篇是靠「陪伴」挡掉的，
    也顺便给那层防御一个判据（去掉它 → 本条红）。
    """
    # 不含阶段名的下篇标题：被阶段名比对挡掉
    assert is_life_stage_section("一、亲情陪伴：血浓于水的牵绊") is False
    # 构造「阶段名 + 陪伴」的撞车标题：这才是「陪伴」那层唯一能生效的场景
    assert is_life_stage_section("一、青春期陪伴：叛逆期的对话") is False
    # 对照：同样的阶段名，不带「陪伴」就认得出
    # （证明上一条不是被阶段名比对挡的，而是被「陪伴」那层挡的）
    assert is_life_stage_section("一、青春期（12—18岁）") is True


def test_negative_corpus_never_enters_profiles(kb):
    """「陪伴缺失与有毒关系」的上篇同样按「一、胎儿期…」排版，
    光靠 is_life_stage_section 认不出来——所以 companion_profiles() 必须另有
    `polarity == "companion"` 这道闸。这条钉住「两道闸都要在」。"""
    negative_texts = {
        i.text for i in kb.items
        if i.polarity == "negative" and i.dimension == "companion_negative"
    }
    assert negative_texts, "负面语料应该存在，否则这条判据失去意义"
    assert not (set(kb.companion_profiles()) & negative_texts), (
        "负面语料混进了陪伴需求——`polarity` 那道闸失效了"
    )
