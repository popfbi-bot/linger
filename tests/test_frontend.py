"""M1.5-A 前端最小界面的验收判据（docs/ROADMAP.md「M1.5 验收清单」A 组）。

前端没法用 pytest 直接验证渲染效果，但能验证的东西比想象中多：
    1. 页面与静态资源真的能被服务端发出来（HTTP 层）
    2. 页面里有该有的区块（DOM 结构）
    3. JS 调用的端点与后端一致（契约一致性）
    4. 没有外部 CDN 依赖（离线可跑）
    5. SSE 解析逻辑是对的（用 Node 跑？不——用 Python 复刻一份解析器做对照）

最后一条是本文件的重点：`parseSSE` 是纯字符串函数，判据可以精确到
「一个 chunk 把事件切成两半」这种情况。
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

STATIC = Path(__file__).resolve().parent.parent / "src" / "linger" / "server" / "static"


# --------------------------------------------------------------------------- #
# 静态资源存在性（可从文件系统验证）
# --------------------------------------------------------------------------- #
def test_static_files_exist():
    for name in ("index.html", "app.js", "style.css"):
        path = STATIC / name
        assert path.is_file(), f"缺静态文件: {name}"
        assert path.stat().st_size > 500, f"{name} 内容过少，像是占位"


# --------------------------------------------------------------------------- #
# HTTP 层：页面与资源真的能被服务端发出来
# --------------------------------------------------------------------------- #
def test_index_served(client):
    """GET / 返回 200 且是 HTML。"""
    resp = client.get("/")
    assert resp.status_code == 200
    assert "text/html" in resp.headers["content-type"]
    assert "灵犀" in resp.text or "Linger" in resp.text


def test_static_assets_served(client):
    """静态资源可访问，且 content-type 正确。"""
    js = client.get("/static/app.js")
    assert js.status_code == 200
    assert "javascript" in js.headers["content-type"]

    css = client.get("/static/style.css")
    assert css.status_code == 200
    assert "css" in css.headers["content-type"]


def test_missing_static_404(client):
    assert client.get("/static/nope.js").status_code == 404


# --------------------------------------------------------------------------- #
# DOM 结构：五个区块都在
# --------------------------------------------------------------------------- #
@pytest.fixture(scope="module")
def html() -> str:
    return (STATIC / "index.html").read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def js() -> str:
    return (STATIC / "app.js").read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def css() -> str:
    return (STATIC / "style.css").read_text(encoding="utf-8")


def test_five_sections_present(html):
    """五个可见区块：人格选择 / 对话区 / 情绪指示器 / 记忆面板 / trace 面板。"""
    assert 'id="persona-list"' in html, "缺人格选择区"
    assert 'id="messages"' in html, "缺对话区"
    assert 'id="affect-badge"' in html and 'id="affect-label"' in html, "缺情绪指示器"
    assert 'id="memory-list"' in html, "缺记忆面板"
    assert 'id="trace-panel"' in html, "缺 trace 面板"


def test_input_and_send_present(html):
    assert 'id="input"' in html
    assert 'id="btn-send"' in html


# --------------------------------------------------------------------------- #
# 无外部依赖（离线可跑）
# --------------------------------------------------------------------------- #
def test_no_external_cdn(html, js, css):
    """页面与资源里不能引用外部 CDN。

    允许的例外只有注释里提到的名字（比如「无外部 CDN」这句话本身）。
    """
    combined = html + js + css
    forbidden = [
        r'<script[^>]+src=["\']https?://',
        r'<link[^>]+href=["\']https?://',
        r'@import\s+url\(["\']?https?://',
        r'cdn\.jsdelivr\.net',
        r'unpkg\.com',
        r'cdnjs\.cloudflare\.com',
        r'fonts\.googleapis\.com',
    ]
    for pattern in forbidden:
        assert not re.search(pattern, combined), f"发现外部依赖: {pattern}"


# --------------------------------------------------------------------------- #
# 契约一致性：JS 调用的端点必须在后端存在
# --------------------------------------------------------------------------- #
def test_js_calls_existing_endpoints(js):
    """JS 里出现的关键路径，必须真的注册在 app 上。"""
    from linger.server.app import app

    paths = {getattr(r, "path", "") for r in app.routes}
    required = [
        "/v1/personas/templates",
        "/v1/personas",
        "/v1/sessions",
        "/v1/sessions/{session_id}/messages",
        "/v1/sessions/{session_id}/memories",
        "/v1/traces/{turn_id}",
        "/health",
    ]
    for p in required:
        assert p in paths, f"后端缺端点: {p}"
        # JS 里应该出现这个路径的「实例化形式」（把 {xxx} 换成模板字符串）
        fragment = p.split("{")[0]
        assert fragment in js, f"JS 没调用端点: {p}"


def test_js_handles_all_event_types(js):
    """四种 SSE 事件都必须有处理分支。"""
    for t in ("thinking", "affect", "delta", "done"):
        assert f'"{t}"' in js or f"'{t}'" in js, f"JS 未处理事件: {t}"


def test_js_uses_fetch_stream_not_eventsource(js):
    """消息端点是 POST，EventSource 只能 GET —— 必须用 fetch + reader。"""
    assert "getReader()" in js, "必须用 ReadableStream 读 SSE"
    assert "EventSource" not in js.replace("EventSource 只支持 GET", ""), (
        "不该用 EventSource（它不支持 POST）"
    )


# --------------------------------------------------------------------------- #
# SSE 解析逻辑（用 Python 复刻 JS 的 parseSSE，验证边界）
# --------------------------------------------------------------------------- #
def _parse_sse(buffer: str):
    """与 app.js::parseSSE 同逻辑的 Python 版本，用来钉住行为。"""
    import json

    events = []
    rest = buffer
    while True:
        idx = rest.find("\n\n")
        if idx == -1:
            break
        frame = rest[:idx]
        rest = rest[idx + 2:]

        event_name = None
        data_lines = []
        for line in frame.split("\n"):
            if line.startswith("event:"):
                event_name = line[6:].strip()
            elif line.startswith("data:"):
                data_lines.append(line[5:].strip())
        if not data_lines:
            continue
        payload = json.loads("\n".join(data_lines))
        events.append((event_name or payload.get("type"), payload.get("data", {})))
    return events, rest


def test_sse_parse_single_frame():
    text = 'event: delta\ndata: {"type":"delta","data":{"text":"你好"}}\n\n'
    events, rest = _parse_sse(text)
    assert rest == ""
    assert events == [("delta", {"text": "你好"})]


def test_sse_parse_multiple_frames():
    text = (
        'event: thinking\ndata: {"type":"thinking","data":{"stage":"recall"}}\n\n'
        'event: delta\ndata: {"type":"delta","data":{"text":"嗯"}}\n\n'
    )
    events, rest = _parse_sse(text)
    assert rest == ""
    assert [e[0] for e in events] == ["thinking", "delta"]


def test_sse_parse_split_across_chunks():
    """一个事件被切成两半时，前半不能提前触发，后半拼上后要能正常解析。

    这是纯前端 SSE 实现最容易错的地方，所以单独钉一条。
    """
    full = 'event: delta\ndata: {"type":"delta","data":{"text":"半句话"}}\n\n'
    cut = full.index('"text"') + 4

    head, tail = full[:cut], full[cut:]

    events1, rest1 = _parse_sse(head)
    assert events1 == [], "帧不完整时不应产出事件"
    assert rest1 == head, "不完整的帧要留给下一个 chunk"

    events2, rest2 = _parse_sse(rest1 + tail)
    assert rest2 == ""
    assert events2 == [("delta", {"text": "半句话"})]


def test_sse_parse_ignores_comment_lines():
    """SSE 允许以 ':' 开头的注释行（保活用），解析器不能把它当数据。"""
    text = ': keep-alive\n\nevent: delta\ndata: {"type":"delta","data":{"text":"x"}}\n\n'
    events, _ = _parse_sse(text)
    assert [e[0] for e in events] == ["delta"]


def test_sse_parse_handles_multiline_data():
    """data 可以分多行，按 SSE 规范要用换行拼接。"""
    text = 'event: delta\ndata: {"type":"delta",\ndata: "data":{"text":"多行"}}\n\n'
    events, _ = _parse_sse(text)
    assert events == [("delta", {"text": "多行"})]


# --------------------------------------------------------------------------- #
# 情绪映射：六种标签都要有视觉映射
# --------------------------------------------------------------------------- #
def test_all_mood_labels_mapped(js, css):
    """affect/engine.py 的六种标签，JS 与 CSS 都必须覆盖。"""
    labels = ["雀跃", "平静欣慰", "中性", "犹豫", "焦躁", "低落"]
    for label in labels:
        assert label in js, f"JS 缺情绪映射: {label}"

    classes = ["is-joy", "is-calm", "is-neutral", "is-hesitate", "is-anxious", "is-low"]
    for cls in classes:
        assert cls in css, f"CSS 缺情绪样式: {cls}"


def test_affect_label_matches_engine():
    """JS 里的标签集合必须与引擎实际产出的一致（防止有一天引擎加了新标签）。"""
    from linger.affect.engine import label_for
    from linger.models import PAD

    produced = set()
    for p in (-0.8, -0.5, 0.0, 0.2, 0.4, 0.8):
        for a in (0.1, 0.4, 0.7, 0.95):
            for d in (0.1, 0.5, 0.9):
                produced.add(label_for(PAD(pleasure=p, arousal=a, dominance=d)))

    js = (STATIC / "app.js").read_text(encoding="utf-8")
    missing = [lbl for lbl in produced if lbl not in js]
    assert not missing, f"引擎会产出但前端没映射的标签: {missing}"