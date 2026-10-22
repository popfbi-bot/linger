"""M2-C 记忆编辑的前端契约测试。

单独成文件而不是追加到 test_frontend.py：既有文件末尾无换行，
往里追加会把末行从「无换行」变成「有换行」，制造与内容无关的 EOL 差异。
新功能进新文件，既有文件零改动——这也是「不改既有测试断言」纪律的自然延伸。

这里验的是「接线是否真的接上了」，不是渲染效果：
    - JS 里确实调了 PATCH /v1/memories/{id} 与 /edits
    - 两个端点确实注册在后端
    - 编辑态与历史态的 DOM 类名在 CSS 里有定义
"""

from __future__ import annotations

from pathlib import Path

import pytest

STATIC = Path(__file__).resolve().parent.parent / "src" / "linger" / "server" / "static"


@pytest.fixture(scope="module")
def js() -> str:
    return (STATIC / "app.js").read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def css() -> str:
    return (STATIC / "style.css").read_text(encoding="utf-8")


def test_js_calls_patch_and_edits(js):
    """JS 必须调用 PATCH 记忆与读取编辑历史。"""
    assert "PATCH" in js, "JS 未使用 PATCH 方法"
    assert "/v1/memories/${m.id}" in js, "JS 未调用 PATCH /v1/memories/{id}"
    assert "/edits" in js, "JS 未调用编辑历史端点"


def test_js_has_edit_and_history_actions(js):
    """记忆条目上必须有「编辑」「历史」两个动作。"""
    assert "编辑" in js, "JS 缺「编辑」按钮文案"
    assert "历史" in js, "JS 缺「历史」按钮文案"
    assert "startEditMemory" in js, "JS 缺行内编辑函数"


def test_edit_states_have_styles(js, css):
    """编辑态/历史态用到的类名，CSS 里必须有定义（否则界面是裸的）。"""
    for cls in ("memory-actions", "memory-edit-input", "memory-edits", "memory-edit-row"):
        assert cls in js, f"JS 未使用样式类: {cls}"
        assert f".{cls}" in css, f"CSS 缺样式: .{cls}"


def test_backend_endpoints_registered():
    """PATCH 记忆与编辑历史端点必须真的注册在后端。"""
    from linger.server.app import app

    paths = {getattr(r, "path", "") for r in app.routes}
    assert "/v1/memories/{memory_id}" in paths
    assert "/v1/memories/{memory_id}/edits" in paths