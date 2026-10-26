"""M1.5-B 场景运行时验收判据（M1.5-B 验收判据即本文件的用例，共 12 条）。

场景是角色扮演的第二层：人格回答「她是谁」，场景回答「我们在哪、发生什么」。
这里的用例钉住四件事：

    · 场景是**结构化的**（YAML → Scenario），不是一段字符串；
    · 节拍靠**关键词**确定性推进，可复现，不调模型；
    · 已完成的节拍**不再进 prompt**（否则 AI 会反复推进同一节拍）；
    · 进度**持久化**，重建 Orchestrator / Repository 后不丢。
"""

from __future__ import annotations

import json

import pytest
from _fakes import FakeEmbedder, FakeLLM
from fastapi.testclient import TestClient

from linger.compose.compiler import compose
from linger.models import AffectState, RelationState
from linger.orchestrator import Orchestrator, PersonaFactory
from linger.scenario import Beat, list_scenarios, load_scenario
from linger.server.app import create_app
from linger.storage.sqlite import SQLiteRepository

SCENARIO_FIELDS = (
    "id",
    "title",
    "world",
    "setting",
    "opening_narration",
    "user_role",
    "beats",
    "constraints",
    "exit_conditions",
)


# --------------------------------------------------------------------------- #
# 夹具
# --------------------------------------------------------------------------- #


@pytest.fixture
def orch(repo, persona, fake_embedder) -> Orchestrator:
    return Orchestrator(repo, FakeLLM("嗯……"), fake_embedder)


@pytest.fixture
def client(repo, persona):
    app = create_app(repo=repo, llm=FakeLLM("嗯……"), embedder=FakeEmbedder())
    with TestClient(app) as c:
        yield c


def _prompt_for(repo, persona, scenario, embedder, user_text="你好") -> str:
    """跑一轮，返回 LLM 实际收到的 system prompt。"""
    return _prompts_for(repo, persona, scenario, embedder, [user_text])[-1]


def _prompts_for(repo, persona, scenario, embedder, user_texts: list[str]) -> list[str]:
    """连跑多轮，返回每轮 LLM 实际收到的 system prompt。"""
    llm = FakeLLM("嗯……")
    orch = Orchestrator(repo, llm, embedder)
    session = orch.create_session(persona.id, "u1", scenario)
    for text in user_texts:
        list(orch.send_message(session.id, text))
    return llm.prompts


# --------------------------------------------------------------------------- #
# B1 · Scenario 模型字段齐备，YAML 能加载
# --------------------------------------------------------------------------- #


def test_b1_scenario_model_has_all_fields():
    """B1：Scenario 字段齐备（缺任何一个，运行时就没法渲染场景状态）。"""
    scenario = load_scenario("rainy_bookstore")
    for name in SCENARIO_FIELDS:
        assert hasattr(scenario, name), f"Scenario 缺字段 {name}"
    assert scenario.id == "rainy_bookstore"
    assert scenario.title
    assert scenario.setting


def test_b1b_beat_is_structured():
    """B1 补充：Beat 有 id / goal / trigger / done，trigger 是非空关键词列表。"""
    beat = Beat(id="b1", goal="建立独处的氛围", trigger=["先坐", "坐吧"])
    assert beat.done is False
    assert beat.trigger
    for scenario in (load_scenario("rainy_bookstore"), load_scenario("late_night_store")):
        for b in scenario.beats:
            assert b.id and b.goal
            assert isinstance(b.trigger, list) and b.trigger
            assert b.done is False, "YAML 里不该写 done=True（进度属于运行时）"


# --------------------------------------------------------------------------- #
# B2 · 至少两个内置场景都能加载
# --------------------------------------------------------------------------- #


def test_b2_two_builtin_scenarios_load():
    """B2：两个内置场景都能 load_scenario 成功，且各有 ≥3 个 beat。"""
    ids = {s["id"] for s in list_scenarios()}
    assert {"rainy_bookstore", "late_night_store"} <= ids
    for sid in ("rainy_bookstore", "late_night_store"):
        scenario = load_scenario(sid)
        assert len(scenario.beats) >= 3, f"{sid} 至少要有 3 个 beat"
        assert scenario.opening_narration, "开场旁白不能为空（要写出画面感）"


def test_b2b_unknown_scenario_raises_file_not_found():
    """B2 补充：不存在的场景抛 FileNotFoundError（由调用方翻译成 404）。"""
    with pytest.raises(FileNotFoundError):
        load_scenario("no_such_scenario")


# --------------------------------------------------------------------------- #
# B3 · 关键词推进 / 不命中不推进
# --------------------------------------------------------------------------- #


def test_b3_advance_on_keyword_hit():
    """B3：用户输入命中 trigger → 该 beat 变 done；不命中 → 不变。"""
    scenario = load_scenario("rainy_bookstore")
    first = scenario.beats[0]
    keyword = first.trigger[0]

    assert scenario.advance("今天天气不错") == [], "没命中关键词不该推进"
    assert first.done is False

    assert scenario.advance(f"那个……{keyword}") == [first.id]
    assert first.done is True


def test_b4_advance_is_idempotent():
    """B4：同一个 beat 不会被重复标记（advance 两次只返回一次）。"""
    scenario = load_scenario("rainy_bookstore")
    keyword = scenario.beats[0].trigger[0]
    hit = scenario.advance(keyword)
    assert hit
    assert scenario.advance(keyword) == [], "已完成的节拍不能再被标记"
    assert len(scenario.done_beats()) == len(hit)


# --------------------------------------------------------------------------- #
# B5 / B6 · 已完成节拍不进 prompt，未完成节拍进 prompt
# --------------------------------------------------------------------------- #


def test_b5_done_beat_disappears_from_prompt(repo, persona, fake_embedder):
    """B5（最关键）：已完成的 beat 不再出现在编译后的 prompt 里。

    否则 AI 会反复推进同一个节拍——这是场景运行时最典型的翻车方式。
    """
    scenario = load_scenario("rainy_bookstore")
    first = scenario.beats[0]
    # 注意：trigger 的关键词可能出现在别的 beat 的 goal 文本里（如「怎么这么早」
    # 就写在 b2 的 goal 括号里），所以断言用「整条 goal 行」而不是关键词，
    # 否则测的是字符串巧合，不是行为。
    keyword = first.trigger[0]

    prompts = _prompts_for(
        repo, persona, "rainy_bookstore", fake_embedder, [keyword, "嗯，我在听"]
    )

    assert f"- {first.goal}" in prompts[0], "推进之前的 prompt 里应还有这条节拍"
    assert f"- {first.goal}" not in prompts[1], "已完成的节拍还留在 prompt 里，AI 会重复推进"
    for beat in scenario.beats[1:]:
        assert f"- {beat.goal}" in prompts[1], "只推进了 b1，其余节拍仍应待完成"


def test_b6_pending_beats_appear_in_prompt(repo, persona, fake_embedder):
    """B6：未完成的 beat 出现在 prompt 里，且带「不要提前推进」的约束。"""
    scenario = load_scenario("rainy_bookstore")
    prompt = _prompt_for(repo, persona, "rainy_bookstore", fake_embedder)

    assert scenario.title in prompt
    assert scenario.setting in prompt
    for beat in scenario.pending_beats():
        assert beat.goal in prompt
    for constraint in scenario.constraints:
        assert constraint in prompt
    assert "不要提前推进" in prompt


def test_b6b_all_beats_done_asks_for_wrap_up():
    """B6 补充：节拍全走完时给一句收束提示，而不是留一张空清单。"""
    from linger.compose.compiler import _scenario_block

    scenario = load_scenario("late_night_store")
    for beat in scenario.beats:
        beat.done = True
    text = _scenario_block(scenario)
    assert "收束" in text
    for beat in scenario.beats:
        assert beat.goal not in text


# --------------------------------------------------------------------------- #
# B7 · 不存在的 scenario_id → 404
# --------------------------------------------------------------------------- #


def test_b7_unknown_scenario_id_404(client, persona):
    """B7：POST /v1/sessions 用不存在的 scenario_id → 404（不是 500）。"""
    resp = client.post(
        "/v1/sessions",
        json={"persona_id": persona.id, "user_id": "u1", "scenario_id": "s_nope"},
    )
    assert resp.status_code == 404


def test_b7b_unknown_scenario_id_raises_value_error(repo, persona, fake_embedder):
    """B7 补充：orchestrator 层抛 ValueError（HTTP 层据此翻 404）。"""
    orch = Orchestrator(repo, FakeLLM(), fake_embedder)
    with pytest.raises(ValueError):
        orch.create_session(persona.id, "u1", "s_nope")


# --------------------------------------------------------------------------- #
# B8 · 进度持久化：新 Repository 实例能读回
# --------------------------------------------------------------------------- #


def test_b8_progress_survives_new_repository(tmp_path):
    """B8：写入进度后，用**新建的** Repository 实例读回，进度还在。"""
    db = tmp_path / "progress.db"
    first = SQLiteRepository(db)
    first.save_scenario_progress("s_1", {"scenario_id": "rainy_bookstore", "done_beats": ["b1", "b2"]})
    first.close()

    second = SQLiteRepository(db)
    try:
        data = second.get_scenario_progress("s_1")
        assert data == {"scenario_id": "rainy_bookstore", "done_beats": ["b1", "b2"]}
        assert second.get_scenario_progress("s_missing") is None
    finally:
        second.close()


def test_b8b_progress_survives_new_orchestrator(tmp_path):
    """B8 补充：重建 Orchestrator 后节拍进度不丢（这是「持久化」的真正含义）。"""
    db = tmp_path / "orch.db"
    repo1 = SQLiteRepository(db)
    persona = PersonaFactory().create("linxi", repo1)
    orch1 = Orchestrator(repo1, FakeLLM(), FakeEmbedder())
    session = orch1.create_session(persona.id, "u1", "rainy_bookstore")
    keyword = load_scenario("rainy_bookstore").beats[0].trigger[0]
    list(orch1.send_message(session.id, keyword))
    repo1.close()

    repo2 = SQLiteRepository(db)
    try:
        data = repo2.get_scenario_progress(session.id)
        assert data is not None
        assert data["done_beats"], "重建后节拍进度丢了"
        assert data["scenario_id"] == "rainy_bookstore"
    finally:
        repo2.close()


# --------------------------------------------------------------------------- #
# B9 · done 事件的 state.scenario
# --------------------------------------------------------------------------- #


def test_b9_done_event_carries_scenario_state(repo, persona, fake_embedder):
    """B9：done.state.scenario 含 done_beats / pending_beats。"""
    scenario = load_scenario("rainy_bookstore")
    keyword = scenario.beats[0].trigger[0]
    orch = Orchestrator(repo, FakeLLM(), fake_embedder)
    session = orch.create_session(persona.id, "u1", "rainy_bookstore")

    done = next(e for e in orch.send_message(session.id, keyword) if e.type == "done")
    state = done.data["state"]["scenario"]

    assert state["scenario_id"] == "rainy_bookstore"
    assert state["title"] == scenario.title
    assert state["done_beats"] == [scenario.beats[0].id]
    assert scenario.beats[0].id not in state["pending_beats"]
    assert set(state["done_beats"]) | set(state["pending_beats"]) == {b.id for b in scenario.beats}


def test_b9b_done_event_scenario_state_over_http(client, persona):
    """B9 补充：SSE 的 done 事件里同样能看到场景状态。"""
    created = client.post(
        "/v1/sessions",
        json={"persona_id": persona.id, "user_id": "u1", "scenario_id": "rainy_bookstore"},
    )
    assert created.status_code == 201
    session_id = created.json()["session_id"]

    events: list[tuple[str, dict]] = []
    with client.stream(
        "POST", f"/v1/sessions/{session_id}/messages", json={"content": "今天怎么这么早关门？"}
    ) as resp:
        event_type = None
        for line in resp.iter_lines():
            if line.startswith("event: "):
                event_type = line[len("event: "):].strip()
            elif line.startswith("data: "):
                events.append((event_type, json.loads(line[len("data: "):])))
    done = [p for t, p in events if t == "done"][0]
    assert set(done["data"]["state"]["scenario"]) >= {"scenario_id", "done_beats", "pending_beats"}


# --------------------------------------------------------------------------- #
# B10 · 不传 scenario_id 时行为与 M1 一致
# --------------------------------------------------------------------------- #


def test_b10_no_scenario_keeps_plain_conversation(repo, persona, fake_embedder):
    """B10：不传 scenario_id 时是普通对话，不炸，也没有场景块内容。"""
    llm = FakeLLM("嗯……")
    orch = Orchestrator(repo, llm, fake_embedder)
    session = orch.create_session(persona.id, "u1")

    done = next(e for e in orch.send_message(session.id, "今天好累。") if e.type == "done")

    assert "没有特别设定" in llm.prompts[-1]
    assert done.data["state"]["scenario"] == {"scenario_id": None}


# --------------------------------------------------------------------------- #
# B11 · GET /v1/scenarios
# --------------------------------------------------------------------------- #


def test_b11_list_scenarios_endpoint(client):
    """B11：GET /v1/scenarios 返回两个场景，含 id/title/setting 摘要。"""
    resp = client.get("/v1/scenarios")
    assert resp.status_code == 200
    scenarios = resp.json()["scenarios"]
    ids = {s["id"] for s in scenarios}
    assert {"rainy_bookstore", "late_night_store"} <= ids
    for s in scenarios:
        for key in ("id", "title", "setting", "user_role", "beat_count"):
            assert key in s, f"场景摘要缺 {key}"
        assert s["beat_count"] >= 3
    assert len(scenarios) >= 2


def test_b11b_get_session_exposes_progress(client, persona):
    """B11 补充：GET /v1/sessions/{id} 带上场景进度摘要。"""
    created = client.post(
        "/v1/sessions",
        json={"persona_id": persona.id, "user_id": "u1", "scenario_id": "rainy_bookstore"},
    )
    session_id = created.json()["session_id"]
    with client.stream(
        "POST", f"/v1/sessions/{session_id}/messages", json={"content": "今天怎么这么早关门？"}
    ) as resp:
        list(resp.iter_lines())

    body = client.get(f"/v1/sessions/{session_id}").json()
    assert body["scenario_id"] == "rainy_bookstore"
    progress = body["scenario"]
    assert progress["scenario_id"] == "rainy_bookstore"
    assert set(progress) >= {"scenario_id", "title", "done_beats", "pending_beats"}


# --------------------------------------------------------------------------- #
# B12 · 向后兼容：compose(scenario="一段文本") 仍能工作
# --------------------------------------------------------------------------- #


def test_b12_compose_accepts_plain_string(persona):
    """B12：老接口传字符串时退化成原样拼接，不炸。"""
    prompt = compose(
        persona,
        AffectState(),
        RelationState(persona_id=persona.id, user_id="u1"),
        [],
        [],
        scenario="雨天的书店，傍晚，只剩你和常客。",
    )
    assert "此刻的场景" in prompt.system
    assert "雨天的书店，傍晚，只剩你和常客。" in prompt.system


def test_b12b_old_str_scenario_still_works_through_orchestrator(repo, persona, fake_embedder):
    """B12 补充：老调用 create_session(..., scenario="文本") 不炸（M1 用例依赖它）。"""
    llm = FakeLLM("嗯……")
    orch = Orchestrator(repo, llm, fake_embedder)
    session = orch.create_session(persona.id, "u1", "雨天的书店，傍晚，只剩你和常客。")
    list(orch.send_message(session.id, "今天面试砸了。"))
    assert "雨天的书店" in llm.prompts[-1]


def test_b12c_compiler_renders_opening_narration_first_turn(repo, persona, fake_embedder):
    """B12 补充：开场旁白只在第一轮注入（第一轮过去它就成了历史）。"""
    llm = FakeLLM("嗯……")
    orch = Orchestrator(repo, llm, fake_embedder)
    scenario = load_scenario("rainy_bookstore")
    session = orch.create_session(persona.id, "u1", "rainy_bookstore")

    list(orch.send_message(session.id, "你好"))
    assert scenario.opening_narration in llm.prompts[-1], "开场旁白应在第一轮注入"

    list(orch.send_message(session.id, "再来一轮"))
    assert scenario.opening_narration not in llm.prompts[-1], "开场旁白不该每轮重复"
