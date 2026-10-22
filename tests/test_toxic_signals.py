"""「陪伴缺失 / 有毒关系」信号识别的行为判据。

核心契约：
    1. 语料被解析成 companion_negative 维度（上篇按人生阶段，下篇按有毒关系类型）
    2. ToxicSignalDetector 从用户倾诉中识别信号，只提示、不惩罚
    3. orchestrator 把 toxic_signals 放进 done 事件，但绝不影响 unresolved_conflicts
    4. 评论他人（无自指/无助/无孤独信号）不触发
"""

from __future__ import annotations

import pytest

from linger.social import get_knowledge
from linger.social.redflag import ToxicSignalDetector, detect_toxic_signals


@pytest.fixture(scope="module")
def kb():
    return get_knowledge()


# --------------------------------------------------------------------------- #
# 1. 语料解析：陪伴缺失与有毒关系维度
# --------------------------------------------------------------------------- #
def test_companion_negative_dimension_exists(kb):
    """第六份语料必须被解析成独立维度，不混入 stage 边界。"""
    items = [i for i in kb.items if i.dimension == "companion_negative"]
    assert len(items) >= 170, f"应解析出至少 170 条，实际 {len(items)}"


def test_life_stage_indexing(kb):
    """上篇按人生阶段索引：每个阶段都能查到陪伴缺失。"""
    for stage in ("胎儿期", "婴儿期", "幼儿期", "童年期", "青春期", "青年期", "中年期", "老年期"):
        pitfalls = kb.companion_pitfalls(stage)
        assert pitfalls, f"{stage} 缺少陪伴缺失条目"


def test_toxic_relation_types(kb):
    """下篇按有毒关系类型分类。"""
    types = kb.toxic_relation_types()
    assert "亲情里的伤害" in types
    assert "亲密关系里的伤害" in types
    assert "友情里的伤害" in types
    assert "职场与社会关系里的伤害" in types


def test_toxic_relation_items_queryable(kb):
    """按类型名能查到具体条目。"""
    items = kb.toxic_relation_items("亲情里的伤害")
    assert len(items) >= 10
    assert any("情感勒索" in i or "否定打压" in i for i in items)


def test_companion_recovery(kb):
    """「如何走出孤独与有毒关系」的行动建议可查。"""
    recovery = kb.companion_recovery()
    assert len(recovery) >= 5
    assert any("独处" in r or "远离" in r for r in recovery)


# --------------------------------------------------------------------------- #
# 2. ToxicSignalDetector：识别信号，只提示不惩罚
# --------------------------------------------------------------------------- #
def test_detect_emotional_blackmail():
    hits = detect_toxic_signals("我妈总说「我都是为了你」，不听她的就是翅膀硬了")
    assert any(h.category == "情感勒索" for h in hits)


def test_detect_belittling():
    hits = detect_toxic_signals("他天天骂我没用，说离开他没人要我，我好自卑")
    assert any(h.category == "否定打压" for h in hits)


def test_detect_cold_violence():
    hits = detect_toxic_signals("每次找他他都不回消息，一冷战就消失好几天")
    assert any(h.category == "冷暴力" for h in hits)


def test_detect_neglect_without_subject():
    """中文省略主语的孤独倾诉也应识别（一个人/没人）。"""
    hits = detect_toxic_signals("一个人在医院排队，翻遍通讯录没人可以说话")
    assert any(h.category == "忽视" for h in hits)


def test_detect_helping_fallback():
    """纯求助信号（无关键词命中）降级为「求助陪伴」，AI 必须接住。"""
    hits = detect_toxic_signals("好难过，快撑不住了")
    assert hits, "求助信号不应被忽略"
    assert hits[0].category == "求助陪伴"


def test_no_false_positive_on_commentary():
    """评论他人/抽象话题不应触发（无自指、无求助、无孤独信号）。"""
    hits = detect_toxic_signals("最近新闻说很多人在感情里被PUA，你怎么看")
    assert hits == []


def test_no_false_positive_on_abstract():
    hits = detect_toxic_signals("我觉得那个明星的粉丝关系很toxic")
    assert hits == []


# --------------------------------------------------------------------------- #
# 3. orchestrator 集成：done 事件带 toxic_signals，但不影响关系惩罚
# --------------------------------------------------------------------------- #
def _build_orchestrator(repo, embedder):
    from _fakes import FakeLLM

    from linger.orchestrator import Orchestrator
    from linger.persona.templates import load_template

    card = load_template("linxi")
    repo.save_persona(card)
    orch = Orchestrator(
        repo=repo,
        llm=FakeLLM(),
        embedder=embedder,
    )
    return orch, card.id


def test_orchestrator_done_includes_toxic_signals(repo, fake_embedder):
    """done 事件必须携带 toxic_signals，且不参与 unresolved_conflicts。"""
    from linger.orchestrator import Orchestrator

    orch, persona_id = _build_orchestrator(repo, fake_embedder)
    session = orch.create_session(persona_id, "u1")

    events = list(orch.send_message(session.id, "我妈总说「我都是为了你」，不听她的就是翅膀硬了"))
    done = [e for e in events if e.type == "done"][0]
    state = done.data.get("state", {})
    assert "toxic_signals" in state
    assert any(s["category"] == "情感勒索" for s in state["toxic_signals"])
    # 有毒信号绝不影响关系惩罚
    assert state["relation"]["unresolved_conflicts"] == 0


def test_orchestrator_toxic_signals_do_not_block_level_up(repo, fake_embedder):
    """有毒信号不累加 unresolved_conflicts，关系仍可正常升级。"""
    from linger.orchestrator import Orchestrator

    orch, persona_id = _build_orchestrator(repo, fake_embedder)
    session = orch.create_session(persona_id, "u1")

    # 连续两轮倾诉「被否定打压」，关系不应被卡住（toxic 不惩罚）
    for msg in (
        "他天天骂我没用，说离开他没人要我，我好自卑",
        "老板也说公司留我是给我面子，天天PUA我",
    ):
        events = list(orch.send_message(session.id, msg))
        done = [e for e in events if e.type == "done"][0]
        state = done.data.get("state", {})
        assert state["relation"]["unresolved_conflicts"] == 0, "有毒信号不应计入冲突惩罚"
        assert state["toxic_signals"], "应识别到有毒信号"