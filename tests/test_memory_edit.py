"""记忆编辑审计（M2-C）的验收判据。

PATCH /v1/memories/{id} 之后，必须留下 before/after 审计记录，
前端 trace 面板才能回溯「修改前后的差异」。这组用例守：
    - 编辑后 edits 接口返回旧/新内容
    - _memory_brief 的 edited 标记（未编辑 false，编辑后 true）
    - 二次编辑历史追加（新的在前）
    - 不存在的记忆 404 / 无历史空列表 / 422 不写日志
"""

from __future__ import annotations

import pytest

from linger.models import MemoryNode


def _seed(repo, persona, embedder, **overrides) -> MemoryNode:
    fields = {
        "persona_id": persona.id,
        "user_id": "u_api",
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


# --------------------------------------------------------------------------- #
# 编辑后审计
# --------------------------------------------------------------------------- #
def test_patch_writes_edit_log(client, repo, persona, fake_embedder):
    """PATCH 后，edits 接口能返回修改前后的内容。"""
    node = _seed(repo, persona, fake_embedder)
    resp = client.patch(f"/v1/memories/{node.id}", json={"content": "我妈妈下周要去医院复查"})
    assert resp.status_code == 200
    assert resp.json()["edited"] is True

    edits = client.get(f"/v1/memories/{node.id}/edits")
    assert edits.status_code == 200
    body = edits.json()
    assert body["memory_id"] == node.id
    assert len(body["edits"]) == 1
    assert body["edits"][0]["old_content"] == "我妈下周要去医院"
    assert body["edits"][0]["new_content"] == "我妈妈下周要去医院复查"
    assert body["edits"][0]["edited_at"]


def test_edited_flag_false_before_edit(client, session_id, repo, persona, fake_embedder):
    """未编辑过的记忆，列表接口里 edited 为 false，updated_at 回落 created_at。"""
    node = _seed(repo, persona, fake_embedder)
    resp = client.get(f"/v1/sessions/{session_id}/memories")
    assert resp.status_code == 200
    mems = resp.json()["memories"]
    target = next(m for m in mems if m["id"] == node.id)
    assert target["edited"] is False
    assert target["updated_at"] == target["created_at"]


def test_second_edit_appends_history(client, repo, persona, fake_embedder):
    """二次编辑后，历史追加为两条，新的在前。"""
    node = _seed(repo, persona, fake_embedder)
    for content in ("第一次改", "第二次改"):
        resp = client.patch(f"/v1/memories/{node.id}", json={"content": content})
        assert resp.status_code == 200

    edits = client.get(f"/v1/memories/{node.id}/edits").json()
    assert len(edits["edits"]) == 2
    # 新的在前
    assert edits["edits"][0]["new_content"] == "第二次改"
    assert edits["edits"][1]["new_content"] == "第一次改"
    # 第一次的 old 是原始内容
    assert edits["edits"][1]["old_content"] == "我妈下周要去医院"


def test_edit_log_does_not_affect_memory_content(client, repo, persona, fake_embedder):
    """编辑日志是附加记录，不改记忆本身的字段（统计/来源/分层）。"""
    node = _seed(repo, persona, fake_embedder, mention_count=5, recall_count=9)
    client.patch(f"/v1/memories/{node.id}", json={"content": "改过"})
    stored = repo.get_memory(node.id)
    assert stored.content == "改过"
    assert stored.mention_count == 5
    assert stored.recall_count == 9
    assert stored.source_type == "ai_inferred"
    assert stored.layer == 3


# --------------------------------------------------------------------------- #
# 边界行为
# --------------------------------------------------------------------------- #
def test_edits_unknown_memory_404(client):
    """不存在的记忆，edits 接口返回 404（与 PATCH/DELETE 一致）。"""
    resp = client.get("/v1/memories/m_not_exist/edits")
    assert resp.status_code == 404


def test_edits_empty_history(client, repo, persona, fake_embedder):
    """没有编辑历史时返回空列表，不是错误。"""
    node = _seed(repo, persona, fake_embedder)
    resp = client.get(f"/v1/memories/{node.id}/edits")
    assert resp.status_code == 200
    assert resp.json()["edits"] == []


def test_blank_patch_does_not_write_log(client, repo, persona, fake_embedder):
    """纯空白编辑被 422 拒绝，不写日志。"""
    node = _seed(repo, persona, fake_embedder)
    resp = client.patch(f"/v1/memories/{node.id}", json={"content": "   "})
    assert resp.status_code == 422
    edits = client.get(f"/v1/memories/{node.id}/edits").json()
    assert edits["edits"] == []