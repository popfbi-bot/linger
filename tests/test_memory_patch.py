"""PATCH /v1/memories/{memory_id} 的验收判据。

记忆面板过去只能看不能改：AI 猜错的时候（source_type="ai_inferred"），
用户没有任何办法纠正——她记错一件事，会一直错下去。
这组用例守的就是这个缺口，外加三条「编辑不该顺手改掉的东西」：
来源/分层/创建时间、统计计数、以及 embedding 必须重算。

分组：
    E. 错误路径（404 / 422）
    F. 正常更新与落库
    G. 不该被编辑改动的字段
"""

from __future__ import annotations

import pytest

from linger.models import MemoryNode, now_utc

# 夹具 `client` / `session_id` / `repo` / `persona` / `fake_embedder` 见 conftest.py


# --------------------------------------------------------------------------- #
# 造数据：直接写库比走一轮对话更确定（不依赖提炼规则的脾气）
# --------------------------------------------------------------------------- #
def _seed(repo, persona, embedder, **overrides) -> MemoryNode:
    fields = {
        "persona_id": persona.id,
        "user_id": "u_api",  # 与 session_id 夹具里的 user_id 一致，才能被 GET 列出来
        "layer": 3,
        "type": "fact",
        "content": "我妈下周要去医院",
        "source_type": "ai_inferred",
        "mention_count": 3,
        "recall_count": 2,
        "strength": 0.7,
        "importance": 0.8,
    }
    fields.update(overrides)
    node = MemoryNode(**fields)
    node.embedding = embedder.embed(node.content)
    repo.add_memories([node])
    return node


class RecordingEmbedder:
    """记下 embed() 的每次调用，同时给真向量。

    判据第 6 条要两个证据：被调用过（行为）、向量真的等于新文本的向量（结果）。
    只断言前者会被「调用了但结果没写回去」骗过。
    """

    dim = 1024

    def __init__(self, inner) -> None:
        self._inner = inner
        self.calls: list[str] = []

    def embed(self, text: str) -> list[float]:
        self.calls.append(text)
        return self._inner.embed(text)


@pytest.fixture
def recording_client(repo, persona, fake_embedder):
    """注入 RecordingEmbedder 的客户端：断言「更新时确实重算了向量」。"""
    from _fakes import FakeLLM
    from fastapi.testclient import TestClient

    from linger.server.app import create_app

    rec = RecordingEmbedder(fake_embedder)
    app = create_app(repo=repo, llm=FakeLLM(), embedder=rec)
    with TestClient(app) as c:
        c.embedder = rec  # 让用例可以直接读调用记录
        c.inner_embedder = fake_embedder
        yield c


# --------------------------------------------------------------------------- #
# E. 错误路径
# --------------------------------------------------------------------------- #
def test_e1_unknown_memory_404(client):
    """E1：记忆不存在 → 404（不是 500，也不是静默 200）。"""
    resp = client.patch("/v1/memories/m_not_exist", json={"content": "随便改点什么"})
    assert resp.status_code == 404
    assert "m_not_exist" in resp.json()["detail"]


def test_e2_empty_body_422(client, repo, persona, fake_embedder):
    """E2：空请求体 {} → 422。什么都不改就不该回 200 假装成功。"""
    node = _seed(repo, persona, fake_embedder)
    resp = client.patch(f"/v1/memories/{node.id}", json={})
    assert resp.status_code == 422


def test_e3_blank_content_422(client, repo, persona, fake_embedder):
    """E3：content 纯空白 → 422。

    空白如果被当有效内容，它会落进检索但永远召不回，
    而用户以为自己改好了——M1 修过同类问题，这里不能复燃。
    """
    node = _seed(repo, persona, fake_embedder)
    for blank in ("", "   ", "\n\t "):
        resp = client.patch(f"/v1/memories/{node.id}", json={"content": blank})
        assert resp.status_code == 422, f"{blank!r} 不该被接受"


def test_e4_too_long_content_422(client, repo, persona, fake_embedder):
    """E4：content 超 500 字 → 422；正好 500 字仍然放行。"""
    node = _seed(repo, persona, fake_embedder)
    resp = client.patch(f"/v1/memories/{node.id}", json={"content": "啊" * 501})
    assert resp.status_code == 422

    ok = client.patch(f"/v1/memories/{node.id}", json={"content": "啊" * 500})
    assert ok.status_code == 200

    # 边界证伪：如果实现里是 `>=`，下面这条会红
    assert client.patch(
        f"/v1/memories/{node.id}", json={"content": "啊" * 499}
    ).status_code == 200


def test_e5_blank_content_rejected_before_delete(client, repo, persona, fake_embedder):
    """E5：422 时不许留下半截状态——原文必须还在。"""
    node = _seed(repo, persona, fake_embedder, content="我妈下周要去医院复查")
    client.patch(f"/v1/memories/{node.id}", json={"content": "   "})
    still = repo.get_memory(node.id)
    assert still is not None
    assert still.content == "我妈下周要去医院复查"


# --------------------------------------------------------------------------- #
# F. 正常更新与落库
# --------------------------------------------------------------------------- #
def test_f1_update_returns_200_with_new_content(client, repo, persona, fake_embedder):
    """F1：正常更新 → 200，返回体含新 content，且是 _memory_brief 的格式。"""
    node = _seed(repo, persona, fake_embedder)
    resp = client.patch(
        f"/v1/memories/{node.id}", json={"content": "我妈妈下周要去医院复查"}
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["content"] == "我妈妈下周要去医院复查"
    assert body["id"] == node.id
    for key in ("layer", "type", "source_type", "importance", "strength",
                "mention_count", "recall_count", "status", "pinned",
                "created_at", "updated_at"):
        assert key in body, f"_memory_brief 缺 {key}"


def test_f2_content_is_stripped(client, repo, persona, fake_embedder):
    """F2：首尾空白被裁掉再落库——不然「改了」和「没改」长得一样。"""
    node = _seed(repo, persona, fake_embedder)
    resp = client.patch(
        f"/v1/memories/{node.id}", json={"content": "  我妈妈下周要去医院复查  "}
    )
    assert resp.status_code == 200
    assert resp.json()["content"] == "我妈妈下周要去医院复查"


def test_f3_embedding_recomputed(recording_client, repo, persona, fake_embedder):
    """F3：更新后 embedding 已重算（判据 6）。

    改了口径而向量还是旧的，这条记忆会被召回到毫不相干的对话里。
    两个断言缺一不可：embed() 被调用；且节点上的向量等于新文本的向量。
    """
    node = _seed(repo, persona, fake_embedder, content="我妈下周要去医院")
    old_vec = list(repo.get_memory(node.id).embedding)

    new_text = "我妈下周要去医院复查，顺便拿报告"
    resp = recording_client.patch(f"/v1/memories/{node.id}", json={"content": new_text})
    assert resp.status_code == 200

    assert new_text in recording_client.embedder.calls, "embed() 没被调用（向量没重算）"
    stored = repo.get_memory(node.id)
    assert stored.embedding == pytest.approx(fake_embedder.embed(new_text))
    assert stored.embedding != pytest.approx(old_vec)


def test_f4_get_session_memories_sees_update(client, session_id, repo, persona, fake_embedder):
    """F4：更新能被 GET /v1/sessions/{id}/memories 看到——落库生效，不是只改内存（判据 9）。"""
    node = _seed(repo, persona, fake_embedder)
    new_text = "我妈妈下周要去医院复查"
    assert client.patch(f"/v1/memories/{node.id}", json={"content": new_text}).status_code == 200

    listed = client.get(f"/v1/sessions/{session_id}/memories").json()["memories"]
    matched = [m for m in listed if m["id"] == node.id]
    assert matched, "编辑后这条记忆从列表里消失了"
    assert matched[0]["content"] == new_text


def test_f5_updated_at_refreshed(client, repo, persona, fake_embedder):
    """F5：updated_at 被刷新（判据 10），且严格晚于 created_at。"""
    node = _seed(repo, persona, fake_embedder, created_at=now_utc())
    old = repo.get_memory(node.id).updated_at

    resp = client.patch(f"/v1/memories/{node.id}", json={"content": "我妈妈下周要去医院复查"})
    assert resp.status_code == 200
    after = repo.get_memory(node.id).updated_at
    assert after is not None
    assert old is None or after > old
    assert after > repo.get_memory(node.id).created_at


def test_f6_update_is_idempotent_for_other_fields(client, repo, persona, fake_embedder):
    """F6：连改两次，每次都生效（不是只能改一次）。"""
    node = _seed(repo, persona, fake_embedder)
    for text in ("第一次改", "第二次改"):
        assert client.patch(f"/v1/memories/{node.id}", json={"content": text}).status_code == 200
        assert repo.get_memory(node.id).content == text


# --------------------------------------------------------------------------- #
# G. 不该被编辑改动的字段
# --------------------------------------------------------------------------- #
def test_g1_source_layer_created_at_preserved(client, repo, persona, fake_embedder):
    """G1：source_type / layer / created_at 不变（判据 7）。

    用户改的是内容，不是来源和分层。若把 created_at 写成 now_utc()，
    等于替用户伪造历史——AI 推出来的条目会假装成今天刚发生的事。
    """
    created = now_utc()
    node = _seed(
        repo, persona, fake_embedder, created_at=created, source_type="ai_inferred", layer=3
    )
    resp = client.patch(f"/v1/memories/{node.id}", json={"content": "我妈妈下周要去医院复查"})
    assert resp.status_code == 200

    stored = repo.get_memory(node.id)
    assert stored.source_type == "ai_inferred"
    assert stored.layer == 3
    assert stored.created_at == created
    body = resp.json()
    assert body["source_type"] == "ai_inferred"
    assert body["layer"] == 3
    assert body["created_at"] == created.isoformat()


def test_g2_statistics_not_reset(client, repo, persona, fake_embedder):
    """G2：mention_count / recall_count / strength 不因编辑清零（判据 8）。"""
    node = _seed(repo, persona, fake_embedder, mention_count=3, recall_count=2, strength=0.7)
    resp = client.patch(f"/v1/memories/{node.id}", json={"content": "我妈妈下周要去医院复查"})
    assert resp.status_code == 200

    stored = repo.get_memory(node.id)
    assert stored.mention_count == 3
    assert stored.recall_count == 2
    assert stored.strength == pytest.approx(0.7)
    body = resp.json()
    assert body["mention_count"] == 3
    assert body["recall_count"] == 2
    assert body["strength"] == pytest.approx(0.7)


def test_g3_other_fields_preserved(client, repo, persona, fake_embedder):
    """G3：importance / type / pinned / source_turn_ids 一并保住。

    只改 content 是契约；顺手归一化的实现会在这里红。
    """
    node = _seed(
        repo, persona, fake_embedder,
        type="promise", importance=0.85, pinned=True, source_turn_ids=[7, 9],
    )
    assert client.patch(
        f"/v1/memories/{node.id}", json={"content": "我妈妈下周要去医院复查"}
    ).status_code == 200

    stored = repo.get_memory(node.id)
    assert stored.type == "promise"
    assert stored.importance == pytest.approx(0.85)
    assert stored.pinned is True
    assert stored.source_turn_ids == [7, 9]
    assert stored.entities == []


def test_g4_deleted_memory_cannot_be_patched(client, repo, persona, fake_embedder):
    """G4：已删除的记忆改不动（404）——删除是软删，别让它借 PATCH 复活。"""
    node = _seed(repo, persona, fake_embedder)
    assert client.delete(f"/v1/memories/{node.id}").status_code == 200
    resp = client.patch(f"/v1/memories/{node.id}", json={"content": "复活"})
    assert resp.status_code == 404
