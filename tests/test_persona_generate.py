"""M3-A 一句话生成人格的验收判据。

守两件事：
    1. **流水线顺序**：LLM → 解析 → Schema 校验 → 冲突消解 → 返回预览，
       且**绝不落库**（生成即入库会把人格库变成草稿箱）。
    2. **冲突消解是真规则**：R1~R5 每条都有专门用例，
       给定「带矛盾」的模型输出，断言修正后的结果。

测法：注入一个「按剧本吐 JSON」的假模型，不依赖真实 LLM，确定性可复现。
"""

from __future__ import annotations

import copy
import json

import pytest

# 夹具 client / session_id / repo / persona / fake_embedder 见 conftest.py

_DESC = "一个嘴硬心软的退役剑客，说话很短，爱说随便"

_BASE: dict = {
    "meta": {"name": "沈砚", "identity": "退役剑客", "archetype": "嘴硬心软", "age": 41},
    "traits": {
        "openness": 0.4,
        "conscientiousness": 0.6,
        "extraversion": 0.4,
        "agreeableness": 0.7,
        "neuroticism": 0.3,
    },
    "voice": {"register": "口语、短句", "avg_sentence_len": [8, 20], "emoji_rate": 0.1},
    "affect": {"baseline": {"pleasure": 0.2, "arousal": 0.3, "dominance": 0.5}},
    "initiative": {"max_proactive_per_day": 1},
}

_PREVIEW = "坐下。\n先喝口酒。\n话慢慢说。\n外头风大。\n别硬撑。\n我在听。"


def _payload(**over) -> str:
    """在 _BASE 上打补丁，返回 JSON 字符串。支持 a_b_c 形式的深路径。"""
    d = copy.deepcopy(_BASE)
    for key, value in over.items():
        parts = key.split("__")
        cur = d
        for p in parts[:-1]:
            cur = cur.setdefault(p, {})
        cur[parts[-1]] = value
    return json.dumps(d, ensure_ascii=False)


class _ScriptedLLM:
    """按剧本返回：生成请求吐 persona JSON，样例请求吐台词。"""

    name = "scripted"

    def __init__(self, persona_json: str, preview: str = _PREVIEW) -> None:
        self.persona_json = persona_json
        self.preview = preview
        self.generate_calls = 0

    def generate(self, system, messages, ctx=None):
        from linger.persona.generator import GENERATE_MARKER, PREVIEW_MARKER

        if GENERATE_MARKER in system:
            self.generate_calls += 1
            yield self.persona_json
            return
        if PREVIEW_MARKER in system:
            yield self.preview
            return
        yield "嗯。"


@pytest.fixture
def make_client(repo, persona):
    """返回一个工厂：给剧本 → 拿到 TestClient。"""
    from _fakes import FakeEmbedder
    from fastapi.testclient import TestClient

    from linger.server.app import create_app

    clients = []

    def _make(persona_json: str, preview: str = _PREVIEW) -> TestClient:
        llm = _ScriptedLLM(persona_json, preview)
        app = create_app(repo=repo, llm=llm, embedder=FakeEmbedder())
        c = TestClient(app)
        c.llm = llm  # 让用例能读调用次数
        clients.append(c)
        return c

    yield _make
    for c in clients:
        c.close()


# --------------------------------------------------------------------------- #
# 1. 正常路径
# --------------------------------------------------------------------------- #
def test_generate_returns_three_keys(make_client):
    with make_client(_payload()) as c:
        resp = c.post("/v1/personas/generate", json={"description": _DESC})
    assert resp.status_code == 200
    body = resp.json()
    assert set(body) == {"persona", "warnings", "preview"}
    assert body["persona"]["meta"]["name"] == "沈砚"


def test_generate_does_not_persist(make_client, repo):
    """判据 2：生成不落库。"""
    before = len(repo.list_personas())
    with make_client(_payload()) as c:
        assert c.post("/v1/personas/generate", json={"description": _DESC}).status_code == 200
    assert len(repo.list_personas()) == before, "生成阶段不许写库"


def test_preview_has_six_lines(make_client):
    with make_client(_payload()) as c:
        body = c.post("/v1/personas/generate", json={"description": _DESC}).json()
    assert len(body["preview"]) == 6
    assert all(isinstance(x, str) and x.strip() for x in body["preview"])


def test_preview_strips_numbering(make_client):
    """模型常自作主张加编号，预览要剥掉。"""
    numbered = "1. 坐下。\n2. 先喝口酒。\n3. 话慢慢说。\n4. 外头风大。\n5. 别硬撑。\n6. 我在听。"
    with make_client(_payload(), preview=numbered) as c:
        body = c.post("/v1/personas/generate", json={"description": _DESC}).json()
    assert body["preview"][0] == "坐下。"
    assert not any(p[0].isdigit() for p in body["preview"])


# --------------------------------------------------------------------------- #
# 2. 输入校验
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("desc", ["", "   ", "a", "长" * 201])
def test_bad_description_422(make_client, desc):
    with make_client(_payload()) as c:
        resp = c.post("/v1/personas/generate", json={"description": desc})
    assert resp.status_code == 422


# --------------------------------------------------------------------------- #
# 3. 模型输出不可用（全部 422，不许 500）
# --------------------------------------------------------------------------- #
def test_non_json_output_422(make_client):
    with make_client("我觉得这个设定挺好的，你直接开聊吧") as c:
        resp = c.post("/v1/personas/generate", json={"description": _DESC})
    assert resp.status_code == 422
    assert "JSON" in resp.json()["detail"]


def test_missing_name_422(make_client):
    payload = json.loads(_payload())
    del payload["meta"]["name"]
    with make_client(json.dumps(payload, ensure_ascii=False)) as c:
        resp = c.post("/v1/personas/generate", json={"description": _DESC})
    assert resp.status_code == 422
    assert "meta.name" in resp.json()["detail"] or "人格名" in resp.json()["detail"]


def test_blank_name_422(make_client):
    payload = json.loads(_payload())
    payload["meta"]["name"] = "   "
    with make_client(json.dumps(payload, ensure_ascii=False)) as c:
        resp = c.post("/v1/personas/generate", json={"description": _DESC})
    assert resp.status_code == 422


def test_markdown_fence_is_stripped(make_client):
    """模型爱包 ```json，要能剥掉。"""
    wrapped = "```json\n" + _payload() + "\n```"
    with make_client(wrapped) as c:
        resp = c.post("/v1/personas/generate", json={"description": _DESC})
    assert resp.status_code == 200


# --------------------------------------------------------------------------- #
# 4. Schema 校验（clamp + warning）
# --------------------------------------------------------------------------- #
def test_trait_out_of_range_clamped(make_client):
    with make_client(_payload(traits__extraversion=1.5)) as c:
        body = c.post("/v1/personas/generate", json={"description": _DESC}).json()
    assert body["persona"]["traits"]["extraversion"] == 1.0
    assert any("extraversion" in w for w in body["warnings"])


def test_sentence_len_not_pair_reset(make_client):
    with make_client(_payload(voice__avg_sentence_len=12)) as c:
        body = c.post("/v1/personas/generate", json={"description": _DESC}).json()
    assert body["persona"]["voice"]["avg_sentence_len"] == [8, 20]
    assert any("avg_sentence_len" in w for w in body["warnings"])


def test_emoji_rate_clamped(make_client):
    with make_client(_payload(voice__emoji_rate=3.2)) as c:
        body = c.post("/v1/personas/generate", json={"description": _DESC}).json()
    assert body["persona"]["voice"]["emoji_rate"] == 1.0
    assert any("emoji_rate" in w for w in body["warnings"])


# --------------------------------------------------------------------------- #
# 5. 冲突消解 R1~R5（每条一个用例，判据 7~11）
# --------------------------------------------------------------------------- #
def test_r1_extravert_short_sentence(make_client):
    """R1：外向度 0.82 但句长上界 8 → 上界调到 14。"""
    payload = _payload(traits__extraversion=0.82, voice__avg_sentence_len=[5, 8])
    with make_client(payload) as c:
        body = c.post("/v1/personas/generate", json={"description": _DESC}).json()
    assert body["persona"]["voice"]["avg_sentence_len"] == [5, 14]
    assert any("外向" in w for w in body["warnings"])


def test_r2_cold_register_high_emoji(make_client):
    """R2：语体冷淡却 emoji_rate 0.8 → 降到 0.05。"""
    payload = _payload(voice__register="冷淡、疏离、寡言", voice__emoji_rate=0.8)
    with make_client(payload) as c:
        body = c.post("/v1/personas/generate", json={"description": _DESC}).json()
    assert body["persona"]["voice"]["emoji_rate"] == 0.05
    assert any("emoji" in w.lower() or "表情" in w for w in body["warnings"])


def test_r3_sentence_len_inverted(make_client):
    """R3：句长倒挂 [20, 8] → 交换成 [8, 20]。"""
    payload = _payload(voice__avg_sentence_len=[20, 8])
    with make_client(payload) as c:
        body = c.post("/v1/personas/generate", json={"description": _DESC}).json()
    assert body["persona"]["voice"]["avg_sentence_len"] == [8, 20]
    assert any("交换" in w for w in body["warnings"])


def test_r4_neurotic_high_pleasure(make_client):
    """R4：神经质 0.9 但基线愉悦 0.8 → 降到 0.2。"""
    payload = _payload(traits__neuroticism=0.9, affect__baseline__pleasure=0.8)
    with make_client(payload) as c:
        body = c.post("/v1/personas/generate", json={"description": _DESC}).json()
    assert body["persona"]["affect"]["baseline"]["pleasure"] == 0.2
    assert any("神经质" in w for w in body["warnings"])


def test_r5_introvert_high_proactive(make_client):
    """R5：外向 0.1 却每天主动 5 次 → 降到 1。"""
    payload = _payload(traits__extraversion=0.1, initiative__max_proactive_per_day=5)
    with make_client(payload) as c:
        body = c.post("/v1/personas/generate", json={"description": _DESC}).json()
    assert body["persona"]["initiative"]["max_proactive_per_day"] == 1
    assert any("主动" in w for w in body["warnings"])


def test_no_warning_on_clean_card(make_client):
    """干净的人格卡不该弹任何 warning（否则警告就失去信号价值）。"""
    with make_client(_payload()) as c:
        body = c.post("/v1/personas/generate", json={"description": _DESC}).json()
    assert body["warnings"] == []


def test_r3_runs_before_r1(make_client):
    """顺序测试：倒挂且外向时，R3 先交换，R1 再基于正确区间判断。

    若顺序反了（R1 先跑），[20, 8] 的上界是 8 <= 10 会先被改成 [20, 14]，
    再交换成 [14, 20] —— 结果里会出现 14 这个不该出现的值。
    """
    payload = _payload(traits__extraversion=0.85, voice__avg_sentence_len=[20, 8])
    with make_client(payload) as c:
        body = c.post("/v1/personas/generate", json={"description": _DESC}).json()
    assert body["persona"]["voice"]["avg_sentence_len"] == [8, 20]


# --------------------------------------------------------------------------- #
# 6. 确认落库（判据 13）
# --------------------------------------------------------------------------- #
def test_confirm_creates_persona(make_client, repo):
    """生成 → 用户确认 → 落库 → 列表多一条。"""
    before = len(repo.list_personas())
    with make_client(_payload()) as c:
        draft = c.post("/v1/personas/generate", json={"description": _DESC}).json()
        resp = c.post("/v1/personas", json={"persona": draft["persona"]})
        assert resp.status_code == 201
        created = resp.json()
        assert created["name"] == "沈砚"
    assert len(repo.list_personas()) == before + 1


def test_confirm_reassigns_id(make_client):
    """客户端传回来的 id 不可信，服务端必须重发。"""
    with make_client(_payload()) as c:
        draft = c.post("/v1/personas/generate", json={"description": _DESC}).json()
        forged = {**draft["persona"], "id": "p_forged_by_client"}
        created = c.post("/v1/personas", json={"persona": forged}).json()
    assert created["id"] != "p_forged_by_client"


def test_confirm_marks_provenance(make_client, repo):
    """落库后 provenance.type = generated，便于区分来源。"""
    with make_client(_payload()) as c:
        draft = c.post("/v1/personas/generate", json={"description": _DESC}).json()
        created = c.post("/v1/personas", json={"persona": draft["persona"]}).json()
    card = repo.get_persona(created["id"])
    assert card.provenance.get("type") == "generated"


def test_empty_body_422(make_client):
    """既没 template_id 也没 persona → 422（不许静默成功）。"""
    with make_client(_payload()) as c:
        resp = c.post("/v1/personas", json={})
    assert resp.status_code == 422


def test_invalid_persona_card_422(make_client):
    """persona 字段乱传 → 422，不是 500。"""
    with make_client(_payload()) as c:
        resp = c.post("/v1/personas", json={"persona": {"meta": "不是对象"}})
    assert resp.status_code == 422