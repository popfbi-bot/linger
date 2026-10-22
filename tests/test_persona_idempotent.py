"""模板实例化幂等 + 用户隔离（修复「换入口就失忆」）。

修之前的现象（实测）：
    点两次 linxi → 库里两个人格（p_9f93... / p_2a77...）
    会话 1 有 1 条记忆，换个会话（同模板同用户）→ 0 条
    用户看到的是「刷新页面再点林汐，她就不记得我了」

根因：`PersonaFactory.create` 每次都 load_template 生成新 id，
而所有状态按 (persona_id, user_id) 存 → 记忆被切碎到多个 persona_id 下。

修法：按 `provenance.template` 查已实例化的人格，命中就复用。
「共用人格会不会串状态」的担心不成立——状态在 user 维度本来就隔离。
"""

from __future__ import annotations

import tempfile
from pathlib import Path

import pytest
from _fakes import FakeEmbedder

from linger.llm.mock import MockProvider
from linger.models import PersonaCard
from linger.orchestrator import PersonaFactory
from linger.server.app import create_app
from linger.storage.sqlite import SQLiteRepository


@pytest.fixture
def empty_repo():
    with tempfile.TemporaryDirectory() as tmp:
        r = SQLiteRepository(Path(tmp) / "t.db")
        yield r
        r.close()


@pytest.fixture
def fresh_client(empty_repo):
    """空库客户端：不依赖 conftest 里那个已塞好 linxi 的 repo。"""
    from fastapi.testclient import TestClient

    app = create_app(repo=empty_repo, llm=MockProvider(), embedder=FakeEmbedder())
    with TestClient(app) as c:
        c.orch = app.state.orch
        yield c


# --------------------------------------------------------------------------- #
# 1. 幂等
# --------------------------------------------------------------------------- #
def test_same_template_returns_same_id(fresh_client):
    a = fresh_client.post("/v1/personas", json={"template_id": "linxi"}).json()
    b = fresh_client.post("/v1/personas", json={"template_id": "linxi"}).json()
    assert a["id"] == b["id"]


def test_only_one_row_per_template(fresh_client, empty_repo):
    for _ in range(3):
        fresh_client.post("/v1/personas", json={"template_id": "linxi"})
    assert len(empty_repo.list_personas()) == 1


def test_different_templates_still_distinct(fresh_client, empty_repo):
    a = fresh_client.post("/v1/personas", json={"template_id": "linxi"}).json()
    b = fresh_client.post("/v1/personas", json={"template_id": "laojiang"}).json()
    assert a["id"] != b["id"]
    assert len(empty_repo.list_personas()) == 2


def test_factory_marks_template_provenance(empty_repo):
    card = PersonaFactory().create("linxi", empty_repo)
    assert card.provenance.get("template") == "linxi"
    # 再存一次读回来，键还在（幂等靠它）
    again = empty_repo.find_persona_by_template("linxi")
    assert again is not None and again.id == card.id


def test_factory_without_repo_still_fresh():
    """repo=None 时无处可查，只能每次新卡（不该崩）。"""
    f = PersonaFactory()
    a, b = f.create("linxi"), f.create("linxi")
    assert a.id != b.id


# --------------------------------------------------------------------------- #
# 2. 记忆不再断裂（本次修复的核心判据）
# --------------------------------------------------------------------------- #
def test_memory_survives_new_session(fresh_client):
    """同一个用户换一个会话，还能看到之前的记忆。"""
    p = fresh_client.post("/v1/personas", json={"template_id": "linxi"}).json()

    s1 = fresh_client.post(
        "/v1/sessions", json={"persona_id": p["id"], "user_id": "u_same"}
    ).json()["session_id"]
    list(fresh_client.orch.send_message(s1, "我妈妈下周要去医院复查。"))
    m1 = fresh_client.get(f"/v1/sessions/{s1}/memories").json()["memories"]
    assert len(m1) >= 1, "第一轮就该提炼出记忆"

    # 用户「刷新页面」：重新点模板、重新开会话
    p2 = fresh_client.post("/v1/personas", json={"template_id": "linxi"}).json()
    assert p2["id"] == p["id"], "换会话不该换人格"

    s2 = fresh_client.post(
        "/v1/sessions", json={"persona_id": p2["id"], "user_id": "u_same"}
    ).json()["session_id"]
    m2 = fresh_client.get(f"/v1/sessions/{s2}/memories").json()["memories"]
    assert len(m2) == len(m1), "同一个人的记忆必须还在"


# --------------------------------------------------------------------------- #
# 3. 用户隔离（共用人格不串状态）
# --------------------------------------------------------------------------- #
def test_different_users_isolated(fresh_client):
    p = fresh_client.post("/v1/personas", json={"template_id": "linxi"}).json()

    sa = fresh_client.post(
        "/v1/sessions", json={"persona_id": p["id"], "user_id": "u_alice"}
    ).json()["session_id"]
    list(fresh_client.orch.send_message(sa, "我妈妈下周要去医院复查。"))
    assert len(fresh_client.get(f"/v1/sessions/{sa}/memories").json()["memories"]) >= 1

    sb = fresh_client.post(
        "/v1/sessions", json={"persona_id": p["id"], "user_id": "u_bob"}
    ).json()["session_id"]
    assert fresh_client.get(f"/v1/sessions/{sb}/memories").json()["memories"] == []


# --------------------------------------------------------------------------- #
# 4. 自建人格不被模板查询误命中
# --------------------------------------------------------------------------- #
def test_generated_persona_not_matched_by_template(empty_repo):
    """一句话生成的人格没有 provenance.template，不该被当模板实例。"""
    card = PersonaCard.model_validate(
        {
            "meta": {"name": "自建角色"},
            "provenance": {"type": "generated"},
        }
    )
    empty_repo.save_persona(card)
    assert empty_repo.find_persona_by_template("linxi") is None