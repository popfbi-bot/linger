"""M3-C 人格管理（列表 + 删除）的验收判据。

单独成文件（不追加到既有测试）：既有文件末尾无换行，
追加会制造与功能无关的 EOL 差异。

守四件事：
    1. `DELETE /v1/personas/{id}` 是软删：不存在 / 已删都给 404，成功给 200 + deleted/name
    2. **所有读取路径都过滤 status**（这是最容易漏的一处，三条路径各有专门用例）
    3. 软删除不级联：persona 行还在，memory_node / turn / session 一行不少
    4. 模板人格删除后再点同一模板 → 新 id（「删除模板人格 = 重置这段关系」）

测法：临时 SQLite + 假 LLM / 假嵌入器，不依赖网络与真实模型。
"""

from __future__ import annotations

import tempfile
from pathlib import Path

import pytest
from _fakes import FakeEmbedder

from linger.llm.mock import MockProvider
from linger.models import PersonaCard
from linger.server.app import create_app
from linger.storage.sqlite import SQLiteRepository

USER = "u_manage"


# --------------------------------------------------------------------------- #
# 夹具：一个干净库 + 一个客户端（不复用 conftest 里已塞好 linxi 的 repo）
# --------------------------------------------------------------------------- #
@pytest.fixture
def mrepo():
    with tempfile.TemporaryDirectory() as tmp:
        r = SQLiteRepository(Path(tmp) / "manage.db")
        yield r
        r.close()


@pytest.fixture
def mclient(mrepo):
    from fastapi.testclient import TestClient

    app = create_app(repo=mrepo, llm=MockProvider(), embedder=FakeEmbedder())
    with TestClient(app) as c:
        c.orch = app.state.orch
        yield c


def _make_persona(mclient, name: str = "沈砚") -> dict:
    """走 HTTP 入口 A（模板实例化）造一个人格，返回 brief。"""
    card = PersonaCard.model_validate(
        {
            "meta": {"name": name, "identity": "退役剑客", "archetype": "嘴硬心软"},
            "provenance": {"type": "generated"},
        }
    )
    resp = mclient.post("/v1/personas", json={"persona": card.model_dump(mode="json")})
    assert resp.status_code == 201, resp.text
    return resp.json()


def _table_counts(repo: SQLiteRepository) -> tuple[int, int, int]:
    """(turn 行数, memory_node 行数, session 行数) —— 直接数行，不看接口。"""
    q = lambda sql: repo.conn.execute(sql).fetchone()[0]  # noqa: E731
    return (
        q("SELECT COUNT(*) FROM turn"),
        q("SELECT COUNT(*) FROM memory_node"),
        q("SELECT COUNT(*) FROM session"),
    )


# --------------------------------------------------------------------------- #
# 1. 端点注册与正常删除（判据 1）
# --------------------------------------------------------------------------- #
def test_delete_endpoint_registered():
    paths = {(getattr(r, "path", ""), m) for r in __import__(
        "linger.server.app", fromlist=["app"]).app.routes for m in getattr(r, "methods", ())}
    assert ("/v1/personas/{persona_id}", "DELETE") in paths


def test_delete_returns_200_with_deleted_and_name(mclient, mrepo):
    p = _make_persona(mclient, "沈砚")
    resp = mclient.delete(f"/v1/personas/{p['id']}")
    assert resp.status_code == 200
    body = resp.json()
    assert body == {"deleted": p["id"], "name": "沈砚"}
    # 软删：行还在，只是状态变了
    row = mrepo.conn.execute("SELECT status FROM persona WHERE id=?", (p["id"],)).fetchone()
    assert row is not None, "软删除不该物理删除 persona 行"
    assert row["status"] == "deleted"


# --------------------------------------------------------------------------- #
# 2. 404 的两种来源（判据 2、3）
# --------------------------------------------------------------------------- #
def test_delete_unknown_persona_404(mclient):
    assert mclient.delete("/v1/personas/p_does_not_exist").status_code == 404


def test_delete_twice_404(mclient):
    """已删的再删 → 404（不是幂等 200：用户看不出自己删了两遍）。"""
    p = _make_persona(mclient, "阿吉")
    assert mclient.delete(f"/v1/personas/{p['id']}").status_code == 200
    assert mclient.delete(f"/v1/personas/{p['id']}").status_code == 404


# --------------------------------------------------------------------------- #
# 3. 读取路径过滤 status（判据 4、5、6、8）
# --------------------------------------------------------------------------- #
def test_deleted_persona_absent_from_list(mclient):
    """判据 4：删除后 GET /v1/personas 不含该 id。"""
    keep = _make_persona(mclient, "留下的人")
    gone = _make_persona(mclient, "要走的人")
    mclient.delete(f"/v1/personas/{gone['id']}")

    ids = {p["id"] for p in mclient.get("/v1/personas").json()["personas"]}
    assert gone["id"] not in ids
    assert keep["id"] in ids


def test_repo_get_persona_returns_none_after_delete(mclient, mrepo):
    """判据 5：repo 层直接断言 get_persona → None。"""
    p = _make_persona(mclient, "林汐")
    assert mrepo.get_persona(p["id"]) is not None
    mclient.delete(f"/v1/personas/{p['id']}")
    assert mrepo.get_persona(p["id"]) is None


def test_session_on_deleted_persona_404(mclient):
    """判据 6：删除后用它开会话 → 404。"""
    p = _make_persona(mclient, "陆沉")
    mclient.delete(f"/v1/personas/{p['id']}")
    resp = mclient.post("/v1/sessions", json={"persona_id": p["id"], "user_id": USER})
    assert resp.status_code == 404


def test_find_persona_by_template_skips_deleted(mclient, mrepo):
    """判据 8：find_persona_by_template 对已删除的人格返回 None。"""
    card = mclient.post("/v1/personas", json={"template_id": "linxi"}).json()
    assert mrepo.find_persona_by_template("linxi") is not None
    mclient.delete(f"/v1/personas/{card['id']}")
    assert mrepo.find_persona_by_template("linxi") is None


# --------------------------------------------------------------------------- #
# 4. 删除模板人格 = 重置这段关系（判据 7）
# --------------------------------------------------------------------------- #
def test_readd_template_after_delete_gets_new_id(mclient):
    """判据 7：删除模板人格后再 POST 同一 template_id → 新 id。

    这条同时证明 find_persona_by_template 过滤了 status：
    它若还认已删的那一份，实例化就会把旧 id 还回来。
    """
    first = mclient.post("/v1/personas", json={"template_id": "linxi"}).json()
    mclient.delete(f"/v1/personas/{first['id']}")
    second = mclient.post("/v1/personas", json={"template_id": "linxi"}).json()
    assert second["id"] != first["id"], "删掉的模板人格不该被复用"


def test_readd_template_starts_with_empty_memory(mclient):
    """重置的语义要落到记忆上：新人格的记忆从零，不接旧的。"""
    first = mclient.post("/v1/personas", json={"template_id": "linxi"}).json()
    s1 = mclient.post(
        "/v1/sessions", json={"persona_id": first["id"], "user_id": USER}
    ).json()["session_id"]
    list(mclient.orch.send_message(s1, "我妈妈下周要去医院复查。"))
    old_memories = mclient.get(f"/v1/sessions/{s1}/memories").json()["memories"]
    assert old_memories, "前提：旧人格下确实有记忆"

    mclient.delete(f"/v1/personas/{first['id']}")
    second = mclient.post("/v1/personas", json={"template_id": "linxi"}).json()
    s2 = mclient.post(
        "/v1/sessions", json={"persona_id": second["id"], "user_id": USER}
    ).json()["session_id"]
    assert mclient.get(f"/v1/sessions/{s2}/memories").json()["memories"] == []


# --------------------------------------------------------------------------- #
# 5. 软删除不级联（判据 9）
# --------------------------------------------------------------------------- #
def test_delete_keeps_turn_and_memory_rows(mclient, mrepo):
    """删除人格不影响已有会话的历史：turn / memory_node / session 行数不变。"""
    p = _make_persona(mclient, "老姜")
    sid = mclient.post(
        "/v1/sessions", json={"persona_id": p["id"], "user_id": USER}
    ).json()["session_id"]
    list(mclient.orch.send_message(sid, "我在一家书店上班，最近老是失眠。"))

    before = _table_counts(mrepo)
    assert before[0] > 0 and before[1] > 0, "前提：本轮该留下 turn 与 memory"

    assert mclient.delete(f"/v1/personas/{p['id']}").status_code == 200
    after = _table_counts(mrepo)

    assert after == before, "软删除不该级联删 turn / memory_node / session"


def test_history_readable_via_old_session_after_delete(mclient):
    """会话本身还能读：删的是人格，不是那段时间。"""
    p = _make_persona(mclient, "阿灯")
    sid = mclient.post(
        "/v1/sessions", json={"persona_id": p["id"], "user_id": USER}
    ).json()["session_id"]
    list(mclient.orch.send_message(sid, "我叫阿灯，喜欢下雨。"))
    mclient.delete(f"/v1/personas/{p['id']}")

    resp = mclient.get(f"/v1/sessions/{sid}")
    assert resp.status_code == 200
    assert resp.json()["session_id"] == sid


# --------------------------------------------------------------------------- #
# 6. 存储层直接断言
# --------------------------------------------------------------------------- #
def test_repo_delete_persona_returns_card_once(mrepo):
    card = PersonaCard.model_validate({"meta": {"name": "只删一次"}})
    mrepo.save_persona(card)
    assert mrepo.delete_persona(card.id) is not None
    assert mrepo.delete_persona(card.id) is None


def test_repo_delete_unknown_returns_none(mrepo):
    assert mrepo.delete_persona("p_nobody") is None


def test_repo_list_personas_skips_deleted(mrepo):
    keep, gone = (
        PersonaCard.model_validate({"meta": {"name": n}}) for n in ("留", "删")
    )
    mrepo.save_persona(keep)
    mrepo.save_persona(gone)
    mrepo.delete_persona(gone.id)
    assert [c.id for c in mrepo.list_personas()] == [keep.id]


def test_repo_delete_does_not_touch_other_personas(mrepo):
    a, b = (PersonaCard.model_validate({"meta": {"name": n}}) for n in ("甲", "乙"))
    mrepo.save_persona(a)
    mrepo.save_persona(b)
    mrepo.delete_persona(a.id)
    assert mrepo.get_persona(b.id) is not None


# --------------------------------------------------------------------------- #
# 7. 前端契约（判据 10）
# --------------------------------------------------------------------------- #
STATIC = Path(__file__).resolve().parent.parent / "src" / "linger" / "server" / "static"


@pytest.fixture(scope="module")
def html() -> str:
    return (STATIC / "index.html").read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def js() -> str:
    return (STATIC / "app.js").read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def css() -> str:
    return (STATIC / "style.css").read_text(encoding="utf-8")


def test_html_has_my_persona_section(html):
    """容器 id：my-persona-list（另有计数 #my-persona-count）。"""
    assert 'id="my-persona-list"' in html
    assert 'id="my-persona-count"' in html
    assert "我的人格" in html


def test_html_keeps_m3a_create_panel(html):
    """M3-A 的生成面板不许被这次改动碰掉。"""
    for el_id in ("btn-show-create", "persona-desc", "btn-generate", "persona-draft"):
        assert f'id="{el_id}"' in html


def test_js_calls_list_and_delete(js):
    assert '/v1/personas"' in js or "/v1/personas`" in js, "JS 没调人格列表"
    assert "DELETE" in js, "JS 没发 DELETE"
    assert "/v1/personas/${encodeURIComponent(persona.id)}" in js, "DELETE 路径不对"
    assert "loadMyPersonas" in js and "deletePersona" in js


def test_js_confirms_before_delete(js):
    """删除必须二次确认——不可从界面撤销。"""
    assert "confirm(" in js


def test_js_reuses_open_session(js):
    """点击人格 → openSession(personaId, name)（M3-A 抽出来的那个）。"""
    assert "openSession(p.id" in js


def test_js_locks_input_when_active_persona_deleted(js):
    assert "lockAfterPersonaDeleted" in js
    assert "人格已删除" in js
    assert "el.input.disabled = true" in js


def test_my_persona_styles_exist(js, css):
    for cls in (
        "my-persona-list",
        "my-persona-item",
        "my-persona-open",
        "my-persona-name",
        "my-persona-meta",
        "my-persona-delete",
        "my-persona-empty",
    ):
        assert cls in js, f"JS 未使用样式类: {cls}"
        assert f".{cls}" in css, f"CSS 缺样式: .{cls}"
