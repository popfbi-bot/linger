"""M1 HTTP 服务层的验收判据（验收判据即本文件的用例）。

每条用例都对应清单里的一项，**先弄坏实现证明它红，再还原验证绿**。

分组：
    A. 入口与契约
    B. 会话与消息（SSE 流式）
    C. 记忆管理与 trace 回溯
    D. 可运行性
"""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient
from starlette.testclient import TestClient as _TC  # noqa: F401  (确认 starlette 提供)

from linger.server.app import create_app

# 夹具 `client` / `session_id` 见 conftest.py（与 test_frontend.py 共用）

# --------------------------------------------------------------------------- #
# SSE 读取工具
# --------------------------------------------------------------------------- #


def _read_sse(client, session_id: str, content: str = "今天面试砸了。") -> list[tuple[str, dict]]:
    """消费一次 SSE 流，返回 [(event_type, data_dict), ...]。"""
    events: list[tuple[str, dict]] = []
    with client.stream(
        "POST",
        f"/v1/sessions/{session_id}/messages",
        json={"content": content},
    ) as resp:
        assert resp.status_code == 200
        assert "text/event-stream" in resp.headers["content-type"]
        event_type: str | None = None
        for line in resp.iter_lines():
            if line.startswith("event: "):
                event_type = line[len("event: ") :].strip()
            elif line.startswith("data: "):
                payload = json.loads(line[len("data: ") :])
                events.append((event_type, payload))
    return events


# --------------------------------------------------------------------------- #
# A. 入口与契约
# --------------------------------------------------------------------------- #


def test_a1_app_module_importable():
    """A1：`linger.server.app` 必须可导入并暴露 app。"""
    from linger.server import app as app_module

    assert hasattr(app_module, "app")
    assert hasattr(app_module, "main"), "必须提供 main()（pyproject 的入口声明）"


def test_a2_health(client):
    """A2：/health 返回 200 且含 status。"""
    resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.json()["status"] == "ok"


# --------------------------------------------------------------------------- #
# B. 会话与消息
# --------------------------------------------------------------------------- #


def test_b1_create_persona(client):
    """B1：POST /v1/personas 从模板实例化，返回 id 与 name。"""
    resp = client.post("/v1/personas", json={"template_id": "linxi"})
    assert resp.status_code in (200, 201)
    body = resp.json()
    assert body["id"]
    assert body["name"]


def test_b1b_create_persona_unknown_template_404(client):
    """B1 补充：模板不存在返回 404，不是 500。"""
    resp = client.post("/v1/personas", json={"template_id": "not_exist_xyz"})
    assert resp.status_code == 404


def test_b2_create_session(client, persona):
    """B2：POST /v1/sessions 返回 session_id。"""
    resp = client.post(
        "/v1/sessions",
        json={"persona_id": persona.id, "user_id": "u_api"},
    )
    assert resp.status_code == 201
    assert resp.json()["session_id"]


def test_b2b_create_session_unknown_persona_404(client):
    """B2 补充：人格不存在返回 404。"""
    resp = client.post("/v1/sessions", json={"persona_id": "p_nope", "user_id": "u1"})
    assert resp.status_code == 404


def test_b3_sse_content_type(client, session_id):
    """B3：消息端点以 text/event-stream 返回。"""
    with client.stream(
        "POST", f"/v1/sessions/{session_id}/messages", json={"content": "你好"}
    ) as resp:
        assert resp.status_code == 200
        assert "text/event-stream" in resp.headers["content-type"]


def test_b4_sse_event_order(client, session_id):
    """B4：事件顺序固定为 thinking → affect → delta* → done。

    注意：thinking 可能出现 **两次**——M0 的 orchestrator 会在「召回」
    和「编译 prompt」两个阶段各发一次（stage 分别为 recall / compose）。
    契约是：第一个是 thinking，affect 恰好一次且在首个 delta 之前，
    done 恰好一次且在最后。
    """
    events = _read_sse(client, session_id)
    types = [t for t, _ in events]
    assert types, "SSE 不应为空"
    assert types[0] == "thinking"
    assert types[-1] == "done"
    assert types.count("affect") == 1
    assert types.count("done") == 1
    assert "delta" in types
    # affect 必须在首个 delta 之前，delta 必须在 done 之前
    assert types.index("affect") < types.index("delta")
    assert types.index("delta") < types.index("done")
    # thinking 只出现在 affect 之前与 delta 之前（即 recall / compose 两阶段）
    assert types.count("thinking") in (1, 2)


def test_b5_sse_data_is_valid_json_with_type_and_data(client, session_id):
    """B5：data 行是合法 JSON，且同时含 type 与 data。"""
    events = _read_sse(client, session_id)
    for event_type, payload in events:
        assert payload["type"] == event_type, "JSON 里的 type 应与 event: 行一致"
        assert isinstance(payload["data"], dict)


def test_b6_done_event_has_four_elements(client, session_id):
    """B6：done 事件含 turn_id / used_memories / state / usage。"""
    events = _read_sse(client, session_id)
    done = [p for t, p in events if t == "done"][0]
    data = done["data"]
    for key in ("turn_id", "used_memories", "state", "usage"):
        assert key in data, f"done 事件缺 {key}"
    assert isinstance(data["turn_id"], int)


def test_b7_state_contains_four_sections(client, session_id):
    """B7：state 内含 affect / relation / red_flags / toxic_signals。"""
    events = _read_sse(client, session_id)
    done = [p for t, p in events if t == "done"][0]
    state = done["data"]["state"]
    for key in ("affect", "relation", "red_flags", "toxic_signals"):
        assert key in state, f"state 缺 {key}"
    assert "label" in state["affect"]
    assert "level" in state["relation"]


def test_b8_unknown_session_404(client):
    """B8：会话不存在返回 404，不是 500。"""
    resp = client.post(
        "/v1/sessions/s_not_exist/messages",
        json={"content": "你好"},
    )
    assert resp.status_code == 404


def test_b9_empty_content_rejected(client, session_id):
    """B9：空 content 返回 422 或 400，不能静默接受。"""
    resp = client.post(f"/v1/sessions/{session_id}/messages", json={"content": ""})
    assert resp.status_code in (400, 422)

    resp2 = client.post(f"/v1/sessions/{session_id}/messages", json={})
    assert resp2.status_code in (400, 422)


def test_b10_affect_event_carries_emotion(client, session_id):
    """B10：affect 事件带情绪标签与 PAD（前端做表情/气泡的依据）。"""
    events = _read_sse(client, session_id)
    affect = [p for t, p in events if t == "affect"][0]
    data = affect["data"]
    for key in ("label", "pleasure", "arousal", "dominance"):
        assert key in data


# --------------------------------------------------------------------------- #
# C. 记忆管理与 trace 回溯
# --------------------------------------------------------------------------- #


def test_c1_list_memories(client, session_id):
    """C1：会话记忆列表可查，且结构完整。"""
    _read_sse(client, session_id, "我妈妈下周要去医院复查。")
    resp = client.get(f"/v1/sessions/{session_id}/memories")
    assert resp.status_code == 200
    body = resp.json()
    assert "memories" in body
    for m in body["memories"]:
        assert "id" in m and "content" in m and "layer" in m


def test_c2_delete_memory_then_404(client, session_id):
    """C2：删除记忆后再查为 404。"""
    _read_sse(client, session_id, "我妈妈下周要去医院复查。")
    memories = client.get(f"/v1/sessions/{session_id}/memories").json()["memories"]
    if not memories:
        pytest.skip("本轮未提炼出记忆（不构成失败，但本用例前提不成立）")
    mid = memories[0]["id"]

    resp = client.delete(f"/v1/memories/{mid}")
    assert resp.status_code == 200

    after = client.get(f"/v1/sessions/{session_id}/memories").json()["memories"]
    assert all(m["id"] != mid for m in after), "删除后不应再出现在列表里"

    # 再删一次应 404
    assert client.delete(f"/v1/memories/{mid}").status_code == 404


def test_c3_export_memories(client, session_id):
    """C3：记忆可导出（JSON 或 Markdown）。"""
    _read_sse(client, session_id, "我妈妈下周要去医院复查。")
    resp = client.get(f"/v1/sessions/{session_id}/memories/export")
    assert resp.status_code == 200
    assert "memories" in resp.json()

    md = client.get(f"/v1/sessions/{session_id}/memories/export?fmt=markdown")
    assert md.status_code == 200
    assert "text/markdown" in md.headers["content-type"]


def test_c4_trace_endpoint(client, session_id):
    """C4：GET /v1/traces/{turn_id} 返回完整 trace。"""
    events = _read_sse(client, session_id)
    done = [p for t, p in events if t == "done"][0]
    turn_id = done["data"]["turn_id"]

    resp = client.get(f"/v1/traces/{turn_id}")
    assert resp.status_code == 200
    trace = resp.json()
    for key in ("query", "candidates", "passed_gate", "selected", "tokens_by_block"):
        assert key in trace, f"trace 缺 {key}"


def test_c5_missing_trace_404(client):
    """C5：trace 不存在返回 404。"""
    assert client.get("/v1/traces/999999").status_code == 404


def test_c6_missing_memory_404(client):
    """C6：记忆不存在返回 404。"""
    assert client.delete("/v1/memories/m_not_exist").status_code == 404


# --------------------------------------------------------------------------- #
# D. 可运行性
# --------------------------------------------------------------------------- #


def test_d1_get_session_state(client, session_id, persona):
    """D 组补充：会话状态可查（含 affect 与 relation）。"""
    resp = client.get(f"/v1/sessions/{session_id}")
    assert resp.status_code == 200
    body = resp.json()
    assert body["session_id"] == session_id
    assert "affect" in body
    assert "relation" in body


def test_d2_list_personas(client, persona):
    """D 组补充：人格列表可查。"""
    resp = client.get("/v1/personas")
    assert resp.status_code == 200
    assert any(p["id"] == persona.id for p in resp.json()["personas"])


def test_d3_main_entrypoint_exists():
    """D 组补充：main() 存在（pyproject 声明了 linger.server.app:main）。"""
    from linger.server import app as app_module

    assert callable(app_module.main)


# --------------------------------------------------------------------------- #
# E. NPC 评审补入的判据（PR #6/#7/#8 交叉比对后确认的缺口）
# --------------------------------------------------------------------------- #


def test_e1_persona_templates_listed(client):
    """E1：可实例化的模板清单可查。

    前端要建人格，得先知道有哪些模板可点——否则只能靠猜 template_id。
    """
    resp = client.get("/v1/personas/templates")
    assert resp.status_code == 200
    templates = resp.json()
    assert isinstance(templates, list) and templates
    ids = {t["id"] for t in templates}
    assert "linxi" in ids
    for t in templates:
        assert "id" in t and "name" in t


def test_e2_rejected_message_does_not_write_turn(client, session_id, repo):
    """E2：被拒的请求不能留下半轮对话。

    否则记忆会长在空输入上——这是静默失败最难查的一种。
    """
    before = len(repo.list_turns(session_id))
    resp = client.post(f"/v1/sessions/{session_id}/messages", json={"content": "   "})
    assert resp.status_code in (400, 422)
    assert len(repo.list_turns(session_id)) == before, "被拒的消息不应写入 turn"


def test_e3_trace_matches_done_turn_id(client, session_id):
    """E3：trace 的 turn_id 必须与 done 事件里的一致。

    否则前端拿 done.turn_id 查 trace 会 404，回溯功能直接断掉。
    """
    events = _read_sse(client, session_id)
    done = [p for t, p in events if t == "done"][0]
    turn_id = done["data"]["turn_id"]
    trace = client.get(f"/v1/traces/{turn_id}").json()
    assert trace["turn_id"] == turn_id


def test_e4_second_turn_also_traceable(client, session_id):
    """E4：第二轮也要能查到 trace（turn_id 单调递增，不是只有第一轮有）。"""
    _read_sse(client, session_id, "第一轮。")
    events = _read_sse(client, session_id, "第二轮。")
    done = [p for t, p in events if t == "done"][0]
    turn_id = done["data"]["turn_id"]
    assert client.get(f"/v1/traces/{turn_id}").status_code == 200


def test_e5_turn_persists_into_session_state(client, session_id):
    """E5：聊完之后，会话状态里的 affect/relation 要反映这一轮。

    状态没落库 = 情绪白算了。
    """
    before = client.get(f"/v1/sessions/{session_id}").json()
    _read_sse(client, session_id, "今天面试砸了，答得一团糟。")
    after = client.get(f"/v1/sessions/{session_id}").json()
    assert after["affect"] is not None
    assert after["relation"] is not None
    # 情绪不应与初始值完全相同（这一轮有冲击）
    assert after["affect"] != before.get("affect") or after["relation"] != before.get("relation")


def test_e6_openapi_schema_builds(client):
    """E6：OpenAPI schema 能生成——路由/响应模型定义无误。"""
    resp = client.get("/openapi.json")
    assert resp.status_code == 200
    paths = resp.json()["paths"]
    for p in ("/health", "/v1/personas", "/v1/sessions", "/v1/sessions/{session_id}/messages"):
        assert p in paths, f"OpenAPI 缺 {p}"


def test_e7_blank_template_not_500(client):
    """E7：空白 template_id 返回 4xx，不是 500。"""
    resp = client.post("/v1/personas", json={"template_id": "   "})
    assert 400 <= resp.status_code < 500


def test_e8_bad_turn_id_not_500(client):
    """E8：非数字 turn_id 返回 4xx，不是 500。"""
    resp = client.get("/v1/traces/not-a-number")
    assert 400 <= resp.status_code < 500


def test_e9_memories_unknown_session_404(client):
    """E9：未知会话的记忆列表返回 404。"""
    assert client.get("/v1/sessions/s_nope/memories").status_code == 404
