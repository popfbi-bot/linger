"""关系角色图谱测试（src/linger/social/roles.py）。

验证：
    - 210 个角色全部加载，14 个维度齐全
    - 分类索引 / 搜索 / 互补角色 / 动态剧本 / 文本识别
    - 与 SocialKnowledge（行为边界）互不污染
"""

from __future__ import annotations

from linger.social import get_knowledge
from linger.social.roles import RelationRoleKnowledge, get_roles


def test_loads_all_210_roles():
    kb = RelationRoleKnowledge()
    assert kb.total == 210


def test_dimensions_complete():
    kb = RelationRoleKnowledge()
    dims = kb.dimensions
    assert len(dims) == 14
    for expected in (
        "依恋类型", "家庭功能", "权力与主导", "情感功能", "戏剧三角",
        "追逃模式", "浪漫叙事", "现代标签", "隐性身份", "内在子人格",
        "冲突模式", "人生阶段", "社会社交", "比喻身份",
    ):
        assert expected in dims


def test_each_dimension_has_roles():
    kb = RelationRoleKnowledge()
    for d in kb.dimensions:
        assert len(kb.by_dimension(d)) > 0


def test_role_has_name_and_description():
    kb = RelationRoleKnowledge()
    for r in kb.roles:
        assert r.name
        assert r.description
        assert r.index >= 1


def test_get_by_name():
    kb = RelationRoleKnowledge()
    role = kb.get("照顾者（拯救者/妈位/爸位）")
    assert role is not None
    assert role.dimension == "情感功能"


def test_search_by_keyword():
    kb = RelationRoleKnowledge()
    hits = kb.search("控制")
    assert len(hits) > 0
    assert any("控制" in h.name or "控制" in h.description for h in hits)


def test_complementary_roles_anxious():
    kb = RelationRoleKnowledge()
    comp = kb.complementary_roles("焦虑型依恋者（痴迷型）")
    assert comp
    names = [c["name"] for c in comp]
    assert "安全基地" in names
    assert all(c["description"] for c in comp)


def test_complementary_roles_victim():
    kb = RelationRoleKnowledge()
    comp = kb.complementary_roles("受害者")
    names = [c["name"] for c in comp]
    # 受害者的互补位里必须有「接住情绪」的位
    assert any(n in names for n in ("倾听者", "情绪稳定器", "避风港"))


def test_complementary_roles_caregiver():
    kb = RelationRoleKnowledge()
    comp = kb.complementary_roles("照顾者（拯救者/妈位/爸位）")
    names = [c["name"] for c in comp]
    assert "被照顾者" in names  # 用户一直照顾别人，AI 应该让用户也被照顾


def test_dynamics_chase_escape():
    kb = RelationRoleKnowledge()
    scripts = kb.dynamics(["追的人（追梦者）", "逃的人（筑墙者）"])
    assert any("追逃" in s for s in scripts)


def test_dynamics_triangle():
    kb = RelationRoleKnowledge()
    scripts = kb.dynamics(["拯救者", "受害者"])
    assert any("卡普曼" in s for s in scripts)


def test_dynamics_inner_child():
    kb = RelationRoleKnowledge()
    scripts = kb.dynamics(["内在小孩"])
    assert any("内在小孩" in s for s in scripts)


def test_detect_anxious():
    kb = RelationRoleKnowledge()
    roles = kb.detect("他总是不回我消息，我一个人在医院，好难过")
    assert roles
    assert roles[0].name == "焦虑型依恋者（痴迷型）"


def test_detect_caregiver():
    kb = RelationRoleKnowledge()
    roles = kb.detect("我来照顾你，别怕，有我呢")
    assert any(r.name == "照顾者（拯救者/妈位/爸位）" for r in roles)


def test_detect_empty_text():
    kb = RelationRoleKnowledge()
    assert kb.detect("") == []
    assert kb.detect("   ") == []


def test_detect_no_false_positive_on_neutral():
    kb = RelationRoleKnowledge()
    assert kb.detect("今天天气不错，我们一起去吃饭吧") == []


def test_singleton_consistent():
    assert get_roles() is get_roles()


def test_no_pollution_with_social_knowledge():
    """角色库与行为边界库是两套独立索引，条目互不串。"""
    role_kb = RelationRoleKnowledge()
    social_kb = get_knowledge()

    # 角色库的条目不应该出现在 social 的 stage 维度里
    social_names = {i.text for i in social_kb.items}
    role_names = {r.name for r in role_kb.roles}
    # 「照顾者」这种词两边都可能出现，但角色条目不应该整个出现在 social 条目里
    overlap = role_names & social_names
    assert len(overlap) < 5  # 允许极少数词面巧合，但不允许大范围重叠


# --------------------------------------------------------------------------- #
# Orchestrator 集成：角色识别进入 done 事件与 prompt
# --------------------------------------------------------------------------- #
def test_orchestrator_done_includes_roles(repo, persona, real_embedder):
    from _fakes import FakeLLM
    from linger.orchestrator import Orchestrator

    orch = Orchestrator(repo, FakeLLM(), real_embedder)
    session = orch.create_session(persona.id, "u1")
    events = list(orch.send_message(session.id, "他总是不回我消息，我好难过，是不是不爱我了"))
    done = events[-1]
    assert done.type == "done"
    roles = done.data["state"].get("roles", [])
    assert roles, "识别到焦虑型依恋信号时 done 事件必须带 roles"
    assert roles[0]["name"] == "焦虑型依恋者（痴迷型）"


def test_orchestrator_prompt_contains_role_block(repo, persona, real_embedder):
    from _fakes import FakeLLM
    from linger.orchestrator import Orchestrator

    llm = FakeLLM()
    orch = Orchestrator(repo, llm, real_embedder)
    session = orch.create_session(persona.id, "u1")
    list(orch.send_message(session.id, "他总是不回我消息，我好难过，是不是不爱我了"))
    system = llm.prompts[-1]
    assert "关系角色洞察" in system
    assert "安全基地" in system  # 焦虑型的互补角色被注入


def test_orchestrator_no_roles_on_neutral(repo, persona, real_embedder):
    from _fakes import FakeLLM
    from linger.orchestrator import Orchestrator

    llm = FakeLLM()
    orch = Orchestrator(repo, llm, real_embedder)
    session = orch.create_session(persona.id, "u1")
    list(orch.send_message(session.id, "今天天气不错，我们一起去吃饭吧"))
    system = llm.prompts[-1]
    assert "关系角色洞察" not in system