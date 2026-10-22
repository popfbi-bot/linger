"""M2-B 前端陪伴档案面板的验收判据（docs/ROADMAP.md「M2 验收清单」M2-B 组）。

与 M1.5-A 的分工：
    M1.5-A 钉住「页面能跑起来」；本文件钉住「陪伴档案面板真的存在且契约一致」。

这里能验证的：
    1. DOM 里真的有「陪伴档案」区块及其子元素
    2. 阶段下拉的 8 个选项与后端认的 8 个阶段**完全一致**（列表漂移会红）
    3. JS 调用的端点与后端契约一致（路径形状、method、body 字段）
    4. 档案为空时走引导文案分支，非空时走数据分支
    5. 仍然零 CDN 依赖

「渲染出来的样子对不对」不在这里验——那是人眼的事。
"""

from __future__ import annotations

import re
from pathlib import Path

STATIC = Path(__file__).resolve().parent.parent / "src" / "linger" / "server" / "static"

HTML = (STATIC / "index.html").read_text(encoding="utf-8")
JS = (STATIC / "app.js").read_text(encoding="utf-8")
CSS = (STATIC / "style.css").read_text(encoding="utf-8")

# 后端认的 8 个人生阶段（与 social/knowledge.py 的 _LIFE_STAGES 同源）
LIFE_STAGES = (
    "胎儿期", "婴儿期", "幼儿期", "童年期",
    "青春期", "青年期", "中年期", "老年期",
)


# --------------------------------------------------------------------------- #
# 1. DOM 结构
# --------------------------------------------------------------------------- #
def test_companion_card_exists():
    assert 'id="companion-card"' in HTML
    assert "陪伴档案" in HTML


def test_companion_dom_ids_present():
    """JS 里引用的每个 id 都必须在 HTML 里存在（否则脚本运行时报 null）。"""
    for dom_id in (
        "companion-empty",
        "companion-body",
        "companion-stage",
        "companion-roles",
        "companion-needs",
        "companion-risks",
        "companion-roles-row",
        "companion-needs-row",
        "companion-risks-row",
    ):
        assert f'id="{dom_id}"' in HTML, f"HTML 缺 #{dom_id}"


def test_companion_card_has_empty_state_and_body():
    """空态引导文案与数据体都要在，且默认隐藏数据体。"""
    assert 'id="companion-empty"' in HTML
    assert 'id="companion-body"' in HTML
    # 数据体默认 hidden，空态默认可见
    body_tag = re.search(r'<div class="companion-body" id="companion-body"[^>]*>', HTML)
    assert body_tag, "找不到 companion-body 标签"
    assert "hidden" in body_tag.group(0), "companion-body 默认应隐藏"
    assert "她还不了解你" in HTML, "空态要有引导文案，不是空白面板"


# --------------------------------------------------------------------------- #
# 2. 阶段列表与后端一致（漂移会红）
# --------------------------------------------------------------------------- #
def test_stage_options_match_backend():
    """下拉的选项必须正好是后端认的 8 个阶段，一个不多一个不少。"""
    block = re.search(r'<select id="companion-stage".*?</select>', HTML, re.S)
    assert block, "找不到阶段下拉"
    options = re.findall(r'<option value="([^"]*)"', block.group(0))
    stages = [o for o in options if o]  # 排除空值（未确定）
    assert stages == list(LIFE_STAGES), f"阶段列表与后端不一致: {stages}"


def test_stage_options_have_empty_choice():
    """必须有「（未确定）」选项——用户可以清空阶段。"""
    block = re.search(r'<select id="companion-stage".*?</select>', HTML, re.S)
    assert re.search(r'<option value="">', block.group(0))


# --------------------------------------------------------------------------- #
# 3. JS ↔ 后端契约一致
# --------------------------------------------------------------------------- #
def test_js_calls_companion_get():
    assert "/v1/companion/" in JS
    # GET 用 api(path) 默认方法，不能带 method: "POST"
    get_calls = re.findall(r"api\(\s*`/v1/companion/[^`]*`\s*\)", JS)
    assert get_calls, "JS 里找不到对 /v1/companion/ 的 GET 调用"


def test_js_calls_companion_patch_with_life_stage():
    """校正阶段走 PATCH，body 字段名是 life_stage。"""
    assert 'method: "PATCH"' in JS
    assert "life_stage" in JS
    patch = re.search(r'`/v1/companion/[^`]*`,\s*\{ method: "PATCH".*?\}\s*\)', JS, re.S)
    assert patch, "找不到对 /v1/companion/ 的 PATCH 调用"
    assert "life_stage" in patch.group(0), "PATCH body 必须带 life_stage"


def test_js_url_encodes_path_params():
    """路径参数要 encodeURIComponent——persona_id 含特殊字符时不会拼坏 URL。"""
    for m in re.finditer(r"`/v1/companion/[^`]*`", JS):
        seg = m.group(0)
        assert "encodeURIComponent" in seg, f"路径未编码: {seg}"


def test_js_refreshes_companion_after_done():
    """done 之后必须刷新档案（判据：面板在 done 事件后自动刷新）。"""
    done_idx = JS.find('case "done"')
    assert done_idx > 0, "找不到 done 事件处理"
    tail = JS[done_idx:]
    assert "refreshCompanion" in tail, "done 之后没有刷新陪伴档案"


def test_js_bindles_stage_change():
    assert "companionStage" in JS
    assert 'addEventListener("change"' in JS


# --------------------------------------------------------------------------- #
# 4. 分支逻辑：空档案 vs 有档案
# --------------------------------------------------------------------------- #
def test_render_companion_has_empty_and_filled_branches():
    assert "function renderCompanion" in JS
    body = re.search(r"function renderCompanion\(profile\)\s*\{(.*?)\n  \}", JS, re.S)
    assert body, "找不到 renderCompanion"
    text = body.group(1)
    assert "companionEmpty.hidden" in text, "空态切换逻辑缺失"
    assert "companionBody.hidden" in text, "数据体切换逻辑缺失"
    assert "life_stage" in text
    assert "roles" in text and "needs" in text and "risks" in text


def test_render_companion_escapes_html():
    """档案内容来自模型识别，必须转义后再插入（防止注入）。

    转义发生在 renderTags 里（真正拼 innerHTML 的地方），
    renderCompanion 只负责分发——判据要钉在真正拼 DOM 的那一层。
    """
    tags = re.search(r"function renderTags\(.*?\{(.*?)\n  \}", JS, re.S)
    assert tags, "找不到 renderTags"
    text = tags.group(1)
    assert "innerHTML" in text, "renderTags 应该用 innerHTML 拼标签"
    assert "escapeHtml" in text, "档案内容未转义（模型识别的文本直接拼进 DOM 了）"


# --------------------------------------------------------------------------- #
# 5. 零 CDN 依赖（延续 M1.5 纪律）
# --------------------------------------------------------------------------- #
def test_no_external_cdn():
    for pat in (r'src="https?://', r'href="https?://', r"@import\s+url\(http"):
        assert not re.search(pat, HTML), f"页面引了外部资源: {pat}"
        assert not re.search(pat, CSS), f"样式引了外部资源: {pat}"


# --------------------------------------------------------------------------- #
# 6. 样式存在（面板不能是裸 DOM）
# --------------------------------------------------------------------------- #
def test_companion_css_present():
    for cls in (".companion-body", ".companion-select", ".tag-list", ".tag"):
        assert cls in CSS, f"样式缺 {cls}"


def test_risk_tag_has_distinct_style():
    """风险标签要和普通标签视觉可区分。"""
    assert ".tag.is-risk" in CSS
    assert ".tag.is-need" in CSS
