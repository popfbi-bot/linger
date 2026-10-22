"""M3-A 一句话生成人格的前端契约。

单独成文件（不追加到 test_frontend.py）：既有文件末尾无换行，
追加会制造与功能无关的 EOL 差异。

验的是「接线接上了」：
    - 页面有生成面板的三个元素
    - JS 调了 /v1/personas/generate 与 /v1/personas（persona 落库）
    - 预览态/警告态/生成态的类名在 CSS 里都有定义
"""

from __future__ import annotations

from pathlib import Path

import pytest

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


def test_html_has_create_panel(html):
    """生成入口的三个元素都在。"""
    for el_id in ("btn-show-create", "persona-desc", "btn-generate", "persona-draft"):
        assert f'id="{el_id}"' in html, f"缺元素: #{el_id}"


def test_js_calls_generate_endpoint(js):
    assert "/v1/personas/generate" in js, "JS 没调生成端点"
    assert "generateDraft" in js, "JS 缺生成函数"


def test_js_confirms_draft_via_create(js):
    """确认后要落库：调 POST /v1/personas 且 body 带 persona。"""
    assert "confirmDraft" in js, "JS 缺确认函数"
    assert "{ persona }" in js or "persona," in js, "确认时应把 persona 传给创建端点"


def test_draft_states_have_styles(js, css):
    for cls in ("persona-draft", "draft-line", "draft-warnings", "draft-warn", "persona-create-panel"):
        assert cls in js, f"JS 未使用样式类: {cls}"
        assert f".{cls}" in css, f"CSS 缺样式: .{cls}"


def test_generate_endpoint_registered():
    from linger.server.app import app

    paths = {getattr(r, "path", "") for r in app.routes}
    assert "/v1/personas/generate" in paths