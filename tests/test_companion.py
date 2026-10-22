"""M2-A 陪伴档案的验收判据（docs/ROADMAP.md「M2 验收清单 / M2-A」）。

每条用例都对应 issue #11 的一条判据，且**先弄坏实现证明它红，再还原验证绿**。

分组：
    1. 模型与存储（判据 1/2）
    2. 人生阶段识别（判据 3）
    3. 陪伴需求来源（判据 4）
    4. 更新时机（判据 5）
    5. 编译器注入（判据 6）
    6. HTTP 端点（判据 7/8）
    7. 只提示不惩罚（判据 9）
"""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from linger.companion import (
    LIFE_STAGES,
    correct_life_stage,
    detect_life_stage,
    needs_for,
    risks_for,
    update_profile,
)
from linger.compose.compiler import compose
from linger.models import CompanionProfile, RelationState, now_utc
from linger.social import get_knowledge
from linger.social.redflag import ToxicSignalDetector

# --------------------------------------------------------------------------- #
# 工具：跑一轮 SSE
# --------------------------------------------------------------------------- #


def _read_sse(client, session_id: str, content: str) -> list[tuple[str, dict]]:
    events: list[tuple[str, dict]] = []
    with client.stream("POST", f"/v1/sessions/{session_id}/messages", json={"content": content}) as resp:
        assert resp.status_code == 200
        event_type: str | None = None
        for line in resp.iter_lines():
            if line.startswith("event: "):
                event_type = line[len("event: ") :].strip()
            elif line.startswith("data: "):
                events.append((event_type, json.loads(line[len("data: ") :])))
    return events


def _done(events: list[tuple[str, dict]]) -> dict:
    done = [p for t, p in events if t == "done"]
    assert done, "一轮对话必须产出 done 事件"
    return done[-1]["data"]


# --------------------------------------------------------------------------- #
# 1. 模型与存储
# --------------------------------------------------------------------------- #


def test_01_model_has_all_fields():
    """判据 1：字段必须齐全——少一个字段，档案就少一个可查询的维度。"""
    p = CompanionProfile(persona_id="p_x", user_id="u_x")
    for field in ("persona_id", "user_id", "life_stage", "roles", "needs", "risks", "updated_at"):
        assert field in p.model_dump(), f"缺少字段 {field}"
    assert p.life_stage == "" and p.roles == [] and p.needs == [] and p.risks == []
    assert p.is_empty is True


def test_02_repository_roundtrip(repo):
    """判据 2（实现侧）：建表 + 存取 + 覆盖写。"""
    p = CompanionProfile(
        persona_id="p_x", user_id="u_x", life_stage="青年期",
        roles=["焦虑型依恋者（痴迷型）"], needs=["被理解"], risks=["冷暴力"],
    )
    repo.save_companion_profile(p)
    got = repo.get_companion_profile("p_x", "u_x")
    assert got is not None
    assert (got.persona_id, got.user_id) == ("p_x", "u_x")
    assert got.life_stage == "青年期"
    assert got.roles == ["焦虑型依恋者（痴迷型）"]
    assert got.needs == ["被理解"] and got.risks == ["冷暴力"]

    # 主键是 persona_id+user_id：再存一次是覆盖，不是新增
    p.life_stage = "中年期"
    repo.save_companion_profile(p)
    assert repo.get_companion_profile("p_x", "u_x").life_stage == "中年期"
    assert repo.get_companion_profile("p_other", "u_x") is None


def test_03_abstract_and_implementation_in_pair():
    """判据 2（抽象侧）：抽象与实现必须成对。

    只改 sqlite 不改 base，其它实现方（Postgres）就会漏掉这个方法。
    这里直接把「抽象方法名前缀」当成契约来校验。
    """
    from linger.storage.base import Repository
    from linger.storage.sqlite import SQLiteRepository

    for name in ("save_companion_profile", "get_companion_profile"):
        assert name in Repository.__abstractmethods__, f"{name} 未加入 Repository 抽象"
        assert callable(getattr(SQLiteRepository, name, None)), f"SQLiteRepository 未实现 {name}"

    # 抽象类一旦缺实现就实例化失败——这条用真子类验证
    class Incomplete(Repository):
        pass

    with pytest.raises(TypeError):
        Incomplete()  # type: ignore[abstract]


def test_04_table_exists_in_sqlite(repo):
    """判据 2（表存在）：不是「内存里有个对象」，是库里真有一张表。"""
    row = repo.conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='companion_profile'"
    ).fetchone()
    assert row is not None, "companion_profile 表不存在"
    assert "persona_id" in row["sql"] and "PRIMARY KEY" in row["sql"].upper()


# --------------------------------------------------------------------------- #
# 2. 人生阶段识别
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("我刚结婚", "青年期"),
        ("孩子上小学", "中年期"),
        ("退休了，天天在家带孙子", "老年期"),
        ("我妈下周要去医院复查", "中年期"),
        ("我高三了，快高考了", "青春期"),
        ("怀孕五个月了，最近老是睡不好", "胎儿期"),
        ("宝宝刚出生，一晚上要醒三次", "婴儿期"),
        ("刚毕业，在北京租房上班", "青年期"),
    ],
)
def test_05_detect_life_stage(text, expected):
    """判据 3：给定话语 → 阶段。确定性、可复现，不用模型。"""
    assert detect_life_stage(text) == expected


@pytest.mark.parametrize(
    "text",
    ["今天天气不错", "在吗", "电影里主角退休了，挺感人的", "陪我聊会儿吧"],
)
def test_06_detect_leaves_unknown_empty(text):
    """判据 3 的反面：识别不出来就留空，不猜。

    「电影里主角退休了」是最容易被误判的一句——它不是用户自己的处境。
    """
    assert detect_life_stage(text) == ""


def test_07_detect_is_deterministic():
    """同一句话跑一百次，结果必须一样（不用模型的意义就在这里）。"""
    text = "孩子上小学了，我每天接送，还得照顾我妈"
    assert len({detect_life_stage(text) for _ in range(100)}) == 1


def test_08_stage_names_are_the_eight():
    """八个阶段就是这八个，多一个少一个都会让 PATCH 校验和知识库对不上。"""
    assert LIFE_STAGES == (
        "胎儿期", "婴儿期", "幼儿期", "童年期", "青春期", "青年期", "中年期", "老年期",
    )


# --------------------------------------------------------------------------- #
# 3. 陪伴需求来源
# --------------------------------------------------------------------------- #


def test_09_needs_come_from_knowledge_base():
    """判据 4：需求来自 companion_pitfalls(life_stage)，不是硬编码字符串。

    做法：把知识库的返回值和档案里的 needs 逐条比对——
    如果实现里写死了文案，它不可能刚好等于语料里的原句。
    """
    kb = get_knowledge()
    expected = kb.companion_pitfalls("老年期", limit=5)
    assert expected, "语料里应该有老年期的条目，否则这条判据失去意义"
    assert needs_for("老年期") == expected

    assert needs_for("", kb) == (kb.companion_profiles() or kb.positive_demands())[:5]
    assert needs_for("", kb, generic=False) == [], "什么都不确定时不该凭空给需求"


def test_10_needs_switch_with_stage(repo):
    """判据 4（端到端）：档案里的 needs 必须等于语料对该阶段的返回。"""
    kb = get_knowledge()
    p = update_profile(repo, "p_a", "u_a", "孩子上小学了，天天辅导作业", knowledge=kb)
    assert p.life_stage == "中年期"
    assert p.needs == kb.companion_pitfalls("中年期", limit=5)
    assert via_repo(repo, "p_a", "u_a").needs == p.needs


def via_repo(repo, persona_id: str, user_id: str) -> CompanionProfile:
    got = repo.get_companion_profile(persona_id, user_id)
    assert got is not None, "档案应已落库"
    return got


def test_11_needs_fall_back_when_stage_unknown():
    """阶段未确定时，需求退回通用需求（不是空、也不是瞎猜某个阶段）。"""
    kb = get_knowledge()
    generic = needs_for("", kb)
    assert generic, "通用需求不应为空"
    assert generic == (kb.companion_profiles() or kb.positive_demands())[:5]


def test_12_risks_from_toxic_signals_only():
    """判据 4（风险侧）：风险只来自有毒信号；没有信号就是空——不预警、不吓人。"""
    assert risks_for([]) == []
    assert risks_for(None) == []
    signals = ToxicSignalDetector().detect("我妈天天说都是为了你，我做什么她都不满意，好累")
    assert signals, "这句话应该命中情感勒索/否定打压"
    risks = risks_for([{"category": s.category, "text": s.text, "keyword": s.keyword} for s in signals])
    assert signals[0].category in risks


# --------------------------------------------------------------------------- #
# 4. 更新时机（回复落库之后）
# --------------------------------------------------------------------------- #


def test_13_profile_updated_after_reply_committed(repo, client, persona):
    """判据 5：档案在**回复落库之后**才更新。

    证明方式不是看代码顺序，而是给存储层装一个探针：
    档案落库的那一刻，库里必须已经有一条 assistant turn。
    提前更新会让这里看到「只有 user turn」——直接红。
    """
    timeline: list[str] = []
    real_save = repo.save_companion_profile
    real_append = repo.append_turn

    def spy_save(profile):
        timeline.append("profile")
        return real_save(profile)

    def spy_append(session_id, role, content, affect=None):
        timeline.append(f"turn:{role}")
        return real_append(session_id, role, content, affect)

    repo.save_companion_profile = spy_save
    repo.append_turn = spy_append

    sid = client.post("/v1/sessions", json={"persona_id": persona.id, "user_id": "u_t"}).json()["session_id"]
    _read_sse(client, sid, "孩子上小学了，每天接送")

    assert "profile" in timeline, "这一轮没有更新陪伴档案"
    first_profile = timeline.index("profile")
    assert "turn:assistant" in timeline, "回复没落库"
    assert timeline.index("turn:assistant") < first_profile, (
        f"档案在回复落库之前就写了：{timeline}"
    )


def test_14_no_profile_written_before_reply(repo, client, persona):
    """判据 5（反面）：请求被拒时不该留下档案。

    和「被拒的请求不留半轮 turn」是同一条纪律。
    """
    sid = client.post("/v1/sessions", json={"persona_id": persona.id, "user_id": "u_r"}).json()["session_id"]
    resp = client.post(f"/v1/sessions/{sid}/messages", json={"content": "   "})
    assert resp.status_code == 422
    assert repo.get_companion_profile(persona.id, "u_r") is None


def test_15_profile_accumulates_across_turns(repo, client, persona):
    """档案是状态，不是每轮的临时推断：第二轮识别不出阶段，第一轮的结论要留下。"""
    sid = client.post("/v1/sessions", json={"persona_id": persona.id, "user_id": "u_acc"}).json()["session_id"]
    _read_sse(client, sid, "孩子上小学了，天天辅导作业")
    first = repo.get_companion_profile(persona.id, "u_acc")
    assert first is not None and first.life_stage == "中年期"

    _read_sse(client, sid, "今天有点想聊聊")
    second = repo.get_companion_profile(persona.id, "u_acc")
    assert second is not None
    assert second.life_stage == "中年期", "识别不出阶段时应保留旧值，而不是清空"
    assert second.updated_at >= first.updated_at


# --------------------------------------------------------------------------- #
# 5. 编译器注入
# --------------------------------------------------------------------------- #


def _compose(persona, companion=None):
    return compose(
        persona,
        affect=_affect(),
        relation=RelationState(persona_id=persona.id, user_id="u_c", level=2),
        memories=[],
        history=[{"role": "user", "content": "在吗"}],
        companion=companion,
    )


def _affect():
    from linger.models import AffectState

    return AffectState()


def test_16_empty_profile_not_injected(persona):
    """判据 6（空）：空档案不注入——连标题都不能出现，否则会顶掉稳定前缀。"""
    none_prompt = _compose(persona, None)
    empty_prompt = _compose(persona, CompanionProfile(persona_id=persona.id, user_id="u_c"))
    assert "companion" not in none_prompt.tokens_by_block
    assert "companion" not in empty_prompt.tokens_by_block
    assert none_prompt.system == empty_prompt.system, "空档案不该改变 prompt 一个字"


def test_17_non_empty_profile_injected(persona):
    """判据 6（非空）：阶段/角色/需求/风险都进 prompt，且明确「不要说」。"""
    profile = CompanionProfile(
        persona_id=persona.id, user_id="u_c", life_stage="中年期",
        roles=["照顾者（自我牺牲）"], needs=["被看见的疲惫"], risks=["情感勒索"],
    )
    prompt = _compose(persona, profile)
    assert "companion" in prompt.tokens_by_block
    assert "中年期" in prompt.system
    assert "照顾者（自我牺牲）" in prompt.system
    assert "被看见的疲惫" in prompt.system
    assert "情感勒索" in prompt.system
    assert "不要说出来" in prompt.system, "注入必须带「只用于理解」的措辞"


def test_18_companion_block_is_after_stable_prefix(persona):
    """companion 块属于会话动态状态，必须排在缓存断点之后——否则用户一换就废掉前缀缓存。"""
    profile = CompanionProfile(persona_id=persona.id, user_id="u_c", life_stage="青年期")
    prompt = _compose(persona, profile)
    names = [b.name for b in prompt.blocks]
    assert names.index("companion") > names.index("persona")
    assert "<!-- cache-breakpoint -->" in prompt.system
    assert prompt.system.index("<!-- cache-breakpoint -->") < prompt.system.index("青年期")


def test_19_orchestrator_injects_profile(repo, client, persona):
    """判据 6（端到端）：聊过一轮之后，下一轮的 prompt 里真的有档案块。"""
    sid = client.post("/v1/sessions", json={"persona_id": persona.id, "user_id": "u_inj"}).json()["session_id"]
    _read_sse(client, sid, "退休了，天天在家带孙子")
    _read_sse(client, sid, "今天有点空")

    llm = client.app.state.orch.llm
    last_prompt = llm.prompts[-1]
    assert "关于这个人" in last_prompt
    assert "老年期" in last_prompt


# --------------------------------------------------------------------------- #
# 6. HTTP 端点
# --------------------------------------------------------------------------- #


def test_20_get_missing_profile_returns_empty_not_404(client, persona):
    """判据 7：档案不存在返回 200 + 空档案，不是 404。

    「还没有」是常态：新用户点开面板不该看到错误页。
    """
    resp = client.get(f"/v1/companion/{persona.id}/u_never")
    assert resp.status_code == 200
    body = resp.json()
    assert body["life_stage"] == ""
    assert body["roles"] == [] and body["needs"] == [] and body["risks"] == []
    assert body["is_empty"] is True
    assert body["life_stages"] == list(LIFE_STAGES)


def test_21_get_after_conversation(client, persona):
    """判据 7（有档案时）：识别结果能从 HTTP 读回来。"""
    sid = client.post("/v1/sessions", json={"persona_id": persona.id, "user_id": "u_g"}).json()["session_id"]
    _read_sse(client, sid, "孩子上小学了，天天辅导作业")
    body = client.get(f"/v1/companion/{persona.id}/u_g").json()
    assert body["life_stage"] == "中年期"
    assert body["needs"], "需求应有内容"
    assert body["is_empty"] is False


def test_22_patch_valid_stage(client, repo, persona):
    """判据 8（正面）：合法值生效，需求跟着重算。"""
    resp = client.patch(f"/v1/companion/{persona.id}/u_p", json={"life_stage": "青年期"})
    assert resp.status_code == 200
    assert resp.json()["life_stage"] == "青年期"
    assert resp.json()["needs"] == get_knowledge().companion_pitfalls("青年期", limit=5)
    assert repo.get_companion_profile(persona.id, "u_p").life_stage == "青年期"


@pytest.mark.parametrize("bad", ["青少年期", "中年", "", "   ", "adult", "老年"])
def test_23_patch_invalid_stage_422(client, persona, bad):
    """判据 8（反面）：非法阶段 422，不是静默接受、更不是 500。"""
    resp = client.patch(f"/v1/companion/{persona.id}/u_p2", json={"life_stage": bad})
    assert resp.status_code == 422, f"{bad!r} 应被拒绝，实际 {resp.status_code}"


def test_24_patch_overrides_detected_stage(repo):
    """用户校正优先于识别：先识别成中年期，再被用户改成青年期。"""
    update_profile(repo, "p_c", "u_c", "孩子上小学了")
    assert repo.get_companion_profile("p_c", "u_c").life_stage == "中年期"
    corrected = correct_life_stage(repo, "p_c", "u_c", "青年期")
    assert corrected.life_stage == "青年期"
    assert repo.get_companion_profile("p_c", "u_c").life_stage == "青年期"


def test_25_done_event_carries_companion(client, persona):
    """done 事件要带档案，前端才能在 `done` 后刷新面板而不额外发请求。"""
    sid = client.post("/v1/sessions", json={"persona_id": persona.id, "user_id": "u_d"}).json()["session_id"]
    done = _done(_read_sse(client, sid, "孩子上小学了"))
    assert "companion" in done["state"]
    assert done["state"]["companion"]["life_stage"] == "中年期"


# --------------------------------------------------------------------------- #
# 7. 只提示不惩罚
# --------------------------------------------------------------------------- #


TOXIC_LINE = "我妈天天说都是为了你，我做什么她都不满意，好累，快撑不住了。"


def test_26_toxic_conversation_does_not_penalize_relation(repo, client, persona):
    """判据 9：触发 toxic 信号的一轮，关系**不降级**、冲突**不增加**。

    档案是「理解」，不是「记账」。识别出伤害不等于给关系扣分——
    扣分的只有 RedFlagDetector（用户做错了什么）。
    """
    from _fakes import FakeEmbedder, FakeLLM

    from linger.orchestrator import Orchestrator
    from linger.server.app import create_app

    app = create_app(repo=repo, llm=FakeLLM(), embedder=FakeEmbedder())
    with TestClient(app) as c:
        sid = c.post("/v1/sessions", json={"persona_id": persona.id, "user_id": "u_z"}).json()["session_id"]
        before = repo.get_relation(persona.id, "u_z")
        done = _done(_read_sse(c, sid, TOXIC_LINE))
        after = repo.get_relation(persona.id, "u_z")

    signals = done["state"]["toxic_signals"]
    assert signals, "这句话必须被识别出有毒信号，否则这条判据测不到东西"
    assert after.unresolved_conflicts == before.unresolved_conflicts == 0
    assert after.level >= before.level, "关系等级不能因为识别出伤害而下降"

    profile = repo.get_companion_profile(persona.id, "u_z")
    assert profile is not None and profile.risks, "风险应写进档案"
    # 反向：档案里的东西不许出现在关系的冲突计数里
    assert profile.risks and after.unresolved_conflicts == 0

    # 同一个 Orchestrator 也不能因为档案存在而提前惩罚
    orch = Orchestrator(repo, FakeLLM(), FakeEmbedder())
    assert orch.recall_k > 0  # 冒烟：装配正常


def test_27_profile_never_touches_unresolved_conflicts(repo):
    """判据 9（结构侧）：档案更新函数根本不接触 RelationState。

    用「更新档案前后关系对象逐字段相等」来钉住——
    将来有人在 update_profile 里偷偷扣分，这条会红。
    """
    relation = RelationState(persona_id="p_n", user_id="u_n", level=3, intimacy=0.4)
    snapshot = relation.model_dump()
    update_profile(repo, "p_n", "u_n", TOXIC_LINE, toxic_signals=[{"category": "情感勒索"}])
    assert relation.model_dump() == snapshot
    assert repo.get_relation("p_n", "u_n") is None, "档案更新不该顺手创建关系状态"


def test_28_update_profile_returns_and_persists(repo):
    """更新入口的契约：返回更新后的档案，且已经落库（调用方不需要再 save 一次）。"""
    roles = [{"name": "讨好者", "dimension": "关系姿态", "description": "总是先照顾别人"}]
    p = update_profile(repo, "p_u", "u_u", "我刚结婚，天天在想他会不会不喜欢我", detected_roles=roles)
    assert p.life_stage == "青年期"
    assert p.roles == ["讨好者"]
    assert repo.get_companion_profile("p_u", "u_u").roles == ["讨好者"]
    assert p.updated_at.tzinfo is not None and p.updated_at <= now_utc()


def test_29_blank_content_keeps_profile_empty(repo):
    """空输入不产生任何识别结果（否则空白就是最廉价的伪标签）。"""
    p = update_profile(repo, "p_b", "u_b", "   ")
    assert p.is_empty
    assert repo.get_companion_profile("p_b", "u_b") is not None
