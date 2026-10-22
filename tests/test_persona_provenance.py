"""四条创建入口的 `provenance.type` 必须能区分来源（验收补修）。

发现方式：跑全功能用户旅程，把四条入口各造一个人格，打印来源标记：

    A 模板    -> authored
    B 一句话  -> generated
    C 蒸馏    -> cloned
    D 向导    -> generated    ← 和 B 一样，错误

向导是**用户一项项填的**，模型只生成了试听台词（那 3 条预览），
所以它的来源应该是 `authored`（用户写的），不是 `generated`（模型生成的）。

现在看无所谓，将来有用：
    · 合规上「AI 生成内容」与「用户手填内容」的标注要求不同；
    · 人格市场要按来源决定能不能上架（cloned 不能，架构 §3.1.3 已写死）。

本文件钉住四条入口的来源标记各不相同、且语义正确。

夹具说明：conftest 的默认 `client` 用的是 `FakeLLM`，它只会吐一句固定台词，
走不通「模型吐 JSON」的入口 B/C。所以这里**覆盖同名 fixture**，换一个
按 marker 吐 JSON 的模型；repo 仍是 conftest 那一个，落库断言照旧成立。
A（模板）与 D（向导）不依赖模型吐 JSON，顺带一起走同一个 client。
"""

from __future__ import annotations

import json

import pytest

from _fakes import FakeEmbedder

# repo / persona 夹具见 conftest.py

_TRANSCRIPT = "林汐: 行吧，你先忙\n我: 那你呢\n林汐: 我没事啊…\n我: 真的吗\n林汐: 真的。\n"

# 入口 B：一句话生成，模型要给完整卡
_B_PAYLOAD = json.dumps(
    {
        "meta": {"name": "沈砚", "identity": "退役剑客", "archetype": "嘴硬心软", "age": 41},
        "traits": {
            "openness": 0.4,
            "conscientiousness": 0.6,
            "extraversion": 0.4,
            "agreeableness": 0.7,
            "neuroticism": 0.3,
        },
        "voice": {"register": "口语、短句", "avg_sentence_len": [8, 20], "emoji_rate": 0.1},
        "values": {"stance_advice": "不劝人"},
    },
    ensure_ascii=False,
)

# 入口 C：蒸馏，模型只补 meta / traits / values（voice 由指纹强制覆盖）
_C_PAYLOAD = json.dumps(
    {
        "meta": {"name": "林汐", "identity": "书店店员", "archetype": "安静的人", "age": 24},
        "traits": {
            "openness": 0.5,
            "conscientiousness": 0.5,
            "extraversion": 0.35,
            "agreeableness": 0.7,
            "neuroticism": 0.4,
        },
        "values": {"stance_advice": "不劝人"},
    },
    ensure_ascii=False,
)


class _ProvenanceLLM:
    """按 marker 分流：生成请求吐卡 JSON，蒸馏请求吐补字段 JSON。"""

    name = "provenance-scripted"

    def generate(self, system, messages, ctx=None):
        from linger.distill.compose import DISTILL_MARKER
        from linger.persona.generator import GENERATE_MARKER, PREVIEW_MARKER

        if GENERATE_MARKER in system:
            yield _B_PAYLOAD
            return
        if DISTILL_MARKER in system:
            yield _C_PAYLOAD
            return
        if PREVIEW_MARKER in system:
            yield "嗯。\n好。\n行吧。"
            return
        yield "嗯。"


@pytest.fixture
def client(repo, persona):  # noqa: ARG001 - persona 由 conftest 存进 repo，这里只需要副作用
    """覆盖 conftest 的同名夹具：换能吐 JSON 的模型，repo 仍共享。"""
    from fastapi.testclient import TestClient

    from linger.server.app import create_app

    app = create_app(repo=repo, llm=_ProvenanceLLM(), embedder=FakeEmbedder())
    with TestClient(app) as c:
        yield c


def _provenance_type(repo, persona_id: str) -> str:
    card = repo.get_persona(persona_id)
    assert card is not None
    return str((card.provenance or {}).get("type") or "")


# --------------------------------------------------------------------------- #
# 入口 D：向导 = authored
# --------------------------------------------------------------------------- #
def test_wizard_preview_marks_authored(client):
    """预览阶段就带上 authored —— 前端不用再自己拼 provenance。"""
    body = client.post(
        "/v1/personas/draft/preview",
        json={"draft": {"meta": {"name": "沈砚"}, "traits": {"extraversion": 0.3}}},
    ).json()
    assert body["persona"]["provenance"]["type"] == "authored"
    assert body["persona"]["provenance"]["source"] == "wizard"


def test_wizard_persisted_as_authored(client, repo):
    """落库后仍然是 authored，不会被默认值改写成 generated。"""
    draft = client.post(
        "/v1/personas/draft/preview",
        json={"draft": {"meta": {"name": "沈砚", "identity": "旧书店老板"}}},
    ).json()
    created = client.post("/v1/personas", json={"persona": draft["persona"]}).json()
    assert _provenance_type(repo, created["id"]) == "authored"


def test_wizard_empty_draft_also_authored(client):
    """空草稿也是向导产物，不该因为没有名字就变成别的来源。"""
    body = client.post("/v1/personas/draft/preview", json={"draft": {}}).json()
    assert body["persona"]["provenance"]["type"] == "authored"


# --------------------------------------------------------------------------- #
# 四条入口互不相同（这是本任务真正的判据）
# --------------------------------------------------------------------------- #
def test_four_entries_have_distinct_provenance(client, repo):
    types: dict[str, str] = {}

    # A 模板
    a = client.post("/v1/personas", json={"template_id": "linxi"}).json()
    types["A"] = _provenance_type(repo, a["id"])

    # B 一句话生成
    b_draft = client.post(
        "/v1/personas/generate", json={"description": "一个嘴硬心软的退役剑客"}
    ).json()
    b = client.post("/v1/personas", json={"persona": b_draft["persona"]}).json()
    types["B"] = _provenance_type(repo, b["id"])

    # C 聊天记录蒸馏
    c_draft = client.post(
        "/v1/personas/distill",
        json={"transcript": _TRANSCRIPT, "target_name": "林汐", "authorized": True},
    ).json()
    c = client.post("/v1/personas", json={"persona": c_draft["persona"]}).json()
    types["C"] = _provenance_type(repo, c["id"])

    # D 五步向导
    d_draft = client.post(
        "/v1/personas/draft/preview", json={"draft": {"meta": {"name": "沈砚"}}}
    ).json()
    d = client.post("/v1/personas", json={"persona": d_draft["persona"]}).json()
    types["D"] = _provenance_type(repo, d["id"])

    assert types == {
        "A": "authored",    # 模板：作者写的
        "B": "generated",   # 一句话：模型生成的
        "C": "cloned",      # 蒸馏：从真实聊天记录复刻的
        "D": "authored",    # 向导：用户手填的（与 A 同族，与 B 不同）
    }, f"来源标记不符合语义：{types}"

    # 关键区分：向导 ≠ 模型生成。这两个混了，将来合规标注会错。
    assert types["D"] != types["B"]


def test_cloned_is_distinguishable_from_generated(client, repo):
    """cloned 必须单独可辨——架构 §3.1.3 要靠它禁止上架人格市场。"""
    c_draft = client.post(
        "/v1/personas/distill",
        json={"transcript": _TRANSCRIPT, "target_name": "林汐", "authorized": True},
    ).json()
    c = client.post("/v1/personas", json={"persona": c_draft["persona"]}).json()
    assert _provenance_type(repo, c["id"]) == "cloned"
