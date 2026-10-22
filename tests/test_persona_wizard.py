"""M3-D 五步向导（「一键赋角」入口 D）的验收判据。

单独成文件（不追加到既有测试）：既有文件末尾无换行，
追加会制造与功能无关的 EOL 差异。

守四件事：

1. `POST /v1/personas/draft/preview` 存在，且**不落库**；
2. 草稿可以残缺——`{}` 与「只填了一个 trait」都必须 200（向导第 1 步之前就是这个状态）；
3. 校验/纠错用的是**同一份实现**：`resolve_conflicts` 的 R1 在这里同样生效，
   clamp 同样生效，`preview` 恰好 3 条；
4. 前端接线：入口、五步容器、`/v1/personas/draft/preview` 的调用都在，
   且第 1 步名字为空时「下一步」是 disabled。

前三条用 HTTP 层测（注入假件，确定性），第四条读静态文件（前端没法在 pytest 里渲染，
但「接线接上了没有」是可以精确断言的）。
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

STATIC = Path(__file__).resolve().parent.parent / "src" / "linger" / "server" / "static"

# 判据 4 / 10 用到的常量
PREVIEW_ENDPOINT = "/v1/personas/draft/preview"
PREVIEW_COUNT = 3


# --------------------------------------------------------------------------- #
# 夹具：一个「按剧本吐台词」的假模型
# --------------------------------------------------------------------------- #
_SIX_LINES = "第一句。\n第二句。\n第三句。\n第四句。\n第五句。\n第六句。"


class _ScriptedLLM:
    """样例请求吐固定 6 行——用来证明「3 条」是**后端截断**的结果，
    而不是碰巧模型只写了 3 行。"""

    name = "scripted"

    def __init__(self, preview: str = _SIX_LINES) -> None:
        self.preview = preview
        self.calls = 0
        self.last_system = ""

    def generate(self, system, messages, ctx=None):
        self.calls += 1
        self.last_system = system
        yield self.preview


@pytest.fixture
def wizard_client(repo, persona):
    """给一个 TestClient + 它的假模型（用于读调用次数 / prompt）。"""
    from _fakes import FakeEmbedder
    from fastapi.testclient import TestClient

    from linger.server.app import create_app

    llm = _ScriptedLLM()
    app = create_app(repo=repo, llm=llm, embedder=FakeEmbedder())
    with TestClient(app) as c:
        c.llm = llm
        yield c


def _preview(client, draft):
    return client.post(PREVIEW_ENDPOINT, json={"draft": draft})


# --------------------------------------------------------------------------- #
# 1. 端点存在 + 三个键（判据 1）
# --------------------------------------------------------------------------- #
def test_endpoint_registered():
    from linger.server.app import app

    paths = {getattr(r, "path", "") for r in app.routes}
    assert PREVIEW_ENDPOINT in paths


def test_no_dependency_install():
    """不引入第三方依赖：本模块只用标准库 + pydantic（已有）。"""
    import ast

    src = Path(__file__).resolve().parent.parent / "src" / "linger" / "persona" / "wizard.py"
    tree = ast.parse(src.read_text(encoding="utf-8"))
    mods = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            mods.update(a.name.split(".")[0] for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            mods.add(node.module.split(".")[0])
    assert mods <= {"__future__", "json", "re", "dataclasses", "typing"}, mods


def test_response_has_three_keys(wizard_client):
    """判据 1：200，body 含 persona / warnings / preview。"""
    resp = _preview(wizard_client, {"meta": {"name": "沈砚"}})
    assert resp.status_code == 200
    body = resp.json()
    assert set(body) == {"persona", "warnings", "preview"}


def test_persona_is_complete_card(wizard_client):
    """补全后的卡是完整人格卡：五个 traits、voice、affect、id 都在。"""
    p = _preview(wizard_client, {"meta": {"name": "沈砚"}}).json()["persona"]
    assert set(p["traits"]) == {
        "openness",
        "conscientiousness",
        "extraversion",
        "agreeableness",
        "neuroticism",
    }
    for key in ("voice", "affect", "initiative", "knowledge", "meta", "id", "version"):
        assert key in p
    assert p["meta"]["name"] == "沈砚"


# --------------------------------------------------------------------------- #
# 2. 不落库（判据 2）
# --------------------------------------------------------------------------- #
def test_preview_does_not_persist(wizard_client, repo):
    """判据 2：调用前后 GET /v1/personas 条数不变。"""
    before = len(wizard_client.get("/v1/personas").json()["personas"])
    assert _preview(wizard_client, {"meta": {"name": "沈砚"}}).status_code == 200
    assert _preview(wizard_client, {}).status_code == 200
    after = len(wizard_client.get("/v1/personas").json()["personas"])
    assert after == before, "预览阶段不许写库"
    assert len(repo.list_personas()) == before


def test_preview_does_not_consume_ids(wizard_client, repo):
    """预览不落库的另一种证法：库里不会出现「（未命名）」这种占位人格。"""
    _preview(wizard_client, {})
    names = {c.name for c in repo.list_personas()}
    assert "（未命名）" not in names


# --------------------------------------------------------------------------- #
# 3. 预览恰好 3 条（判据 3）
# --------------------------------------------------------------------------- #
def test_preview_has_exactly_three_lines(wizard_client):
    """判据 3：模型写了 6 行，返回只能是 3 条。"""
    preview = _preview(wizard_client, {"meta": {"name": "沈砚"}}).json()["preview"]
    assert len(preview) == PREVIEW_COUNT
    assert preview == ["第一句。", "第二句。", "第三句。"]


def test_preview_strips_numbering(wizard_client):
    wizard_client.llm.preview = "1. 坐下。\n2、先喝口水。\n3) 话慢慢说。\n4. 第四句。"
    preview = _preview(wizard_client, {"meta": {"name": "沈砚"}}).json()["preview"]
    assert preview == ["坐下。", "先喝口水。", "话慢慢说。"]


def test_preview_failure_is_not_fatal(wizard_client):
    """台词写不出来不该让整次预览失败——向导还要继续走。"""
    class _Boom(_ScriptedLLM):
        def generate(self, system, messages, ctx=None):
            raise RuntimeError("模型挂了")

    wizard_client.app.state.orch.llm = _Boom()
    resp = _preview(wizard_client, {"meta": {"name": "沈砚"}})
    assert resp.status_code == 200
    assert resp.json()["preview"] == []
    assert resp.json()["persona"]["meta"]["name"] == "沈砚"


# --------------------------------------------------------------------------- #
# 4. 部分字段也能预览（判据 4）
# --------------------------------------------------------------------------- #
def test_partial_draft_only_traits(wizard_client):
    """判据 4：只传一个 trait → 200，名字是占位值（不是空串、不是 422）。"""
    resp = _preview(wizard_client, {"traits": {"extraversion": 0.3}})
    assert resp.status_code == 200
    p = resp.json()["persona"]
    assert p["traits"]["extraversion"] == 0.3
    assert p["meta"]["name"] == "（未命名）"
    assert p["meta"]["name"] != ""


def test_partial_draft_keeps_defaults(wizard_client):
    """没填的字段用 PersonaCard 的默认值补，不是清零。"""
    p = _preview(wizard_client, {"meta": {"name": "沈砚"}}).json()["persona"]
    assert p["traits"] == {
        "openness": 0.5,
        "conscientiousness": 0.5,
        "extraversion": 0.5,
        "agreeableness": 0.5,
        "neuroticism": 0.5,
    }
    assert p["voice"]["avg_sentence_len"] == [8, 20]


def test_empty_draft_is_200(wizard_client):
    """判据 5：空 draft `{}` → 200（向导第 1 步之前就是这个状态）。"""
    resp = _preview(wizard_client, {})
    assert resp.status_code == 200
    body = resp.json()
    assert body["persona"]["meta"]["name"] == "（未命名）"
    assert len(body["preview"]) == PREVIEW_COUNT


def test_missing_draft_key_is_200(wizard_client):
    """连 `draft` 都不给也不该 422：等价于空草稿。"""
    resp = wizard_client.post(PREVIEW_ENDPOINT, json={})
    assert resp.status_code == 200
    assert resp.json()["persona"]["meta"]["name"] == "（未命名）"


def test_no_body_at_all_is_200(wizard_client):
    """完全空 body 同样按空草稿处理。"""
    resp = wizard_client.post(PREVIEW_ENDPOINT)
    assert resp.status_code == 200


def test_blank_name_falls_back_to_placeholder(wizard_client):
    """名字是纯空白 → 占位名（不是 ""，也不是 422）。"""
    p = _preview(wizard_client, {"meta": {"name": "   "}}).json()["persona"]
    assert p["meta"]["name"] == "（未命名）"


def test_none_fields_are_ignored(wizard_client):
    """前端清空输入框会送 null；null 视为「没填」，不能把默认值冲掉。"""
    p = _preview(
        wizard_client,
        {"meta": {"name": "沈砚", "age": None, "identity": ""}, "traits": {"openness": None}},
    ).json()["persona"]
    assert p["meta"]["age"] is None
    assert p["traits"]["openness"] == 0.5


def test_unknown_keys_are_dropped(wizard_client):
    """草稿里多送的键不该悄悄生效（用户没看见的东西不改变结果）。"""
    resp = _preview(
        wizard_client,
        {
            "meta": {"name": "沈砚", "不存在的键": "值"},
            "voice": {"emoji_set": ["😀"]},
            "整个不存在的段": {"a": 1},
        },
    )
    assert resp.status_code == 200
    p = resp.json()["persona"]
    assert p["voice"]["emoji_set"] == []
    assert "不存在的键" not in p["meta"]


def test_bad_meta_type_is_422_not_500(wizard_client):
    """结构坏到补不出卡（meta 不是对象）→ 422，绝不 500。"""
    resp = _preview(wizard_client, {"meta": ["不是对象"]})
    # meta 非 dict 会被裁掉，于是回落成占位名 —— 依然 200
    assert resp.status_code == 200
    resp2 = wizard_client.post(PREVIEW_ENDPOINT, json={"draft": ["不是对象"]})
    assert resp2.status_code == 422


# --------------------------------------------------------------------------- #
# 5. 冲突消解生效（判据 6）
# --------------------------------------------------------------------------- #
def test_r1_applies_to_wizard_draft(wizard_client):
    """判据 6：extraversion=0.85 + 句长 [5,8] → 上界变 14，warnings 非空。"""
    body = _preview(
        wizard_client,
        {"traits": {"extraversion": 0.85}, "voice": {"avg_sentence_len": [5, 8]}},
    ).json()
    assert body["persona"]["voice"]["avg_sentence_len"] == [5, 14]
    assert body["warnings"]
    assert any("外向" in w for w in body["warnings"])


def test_r3_applies_to_wizard_draft(wizard_client):
    """同一份 resolve_conflicts：句长倒挂也要被交换（R3 先于 R1）。"""
    body = _preview(
        wizard_client,
        {"traits": {"extraversion": 0.85}, "voice": {"avg_sentence_len": [20, 8]}},
    ).json()
    assert body["persona"]["voice"]["avg_sentence_len"] == [8, 20]
    assert any("交换" in w for w in body["warnings"])


def test_r2_applies_to_wizard_draft(wizard_client):
    """R2：语体写着冷淡却 emoji 0.8 → 降到 0.05。"""
    body = _preview(
        wizard_client,
        {"voice": {"register": "冷淡、疏离", "emoji_rate": 0.8}},
    ).json()
    assert body["persona"]["voice"]["emoji_rate"] == 0.05
    assert any("表情" in w for w in body["warnings"])


def test_clean_draft_has_no_warnings(wizard_client):
    """干净草稿不弹 warning（警告多了等于没警告）。"""
    body = _preview(
        wizard_client,
        {
            "meta": {"name": "沈砚"},
            "traits": {"extraversion": 0.4},
            "voice": {"register": "口语、短句", "avg_sentence_len": [8, 20], "emoji_rate": 0.0},
        },
    ).json()
    assert body["warnings"] == []


# --------------------------------------------------------------------------- #
# 6. 非法值被 clamp（判据 7）
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    ("draft", "path", "expected"),
    [
        ({"traits": {"extraversion": 1.5}}, ("traits", "extraversion"), 1.0),
        ({"traits": {"agreeableness": -0.4}}, ("traits", "agreeableness"), 0.0),
        ({"voice": {"emoji_rate": 3.2}}, ("voice", "emoji_rate"), 1.0),
        ({"voice": {"avg_sentence_len": [0, 90]}}, ("voice", "avg_sentence_len"), [1, 60]),
    ],
)
def test_out_of_range_is_clamped(wizard_client, draft, path, expected):
    """判据 7：越界值被 clamp 并留 warning。"""
    body = _preview(wizard_client, draft).json()
    node = body["persona"]
    for key in path:
        node = node[key]
    assert node == expected
    assert body["warnings"], "clamp 必须留下说明，否则用户不知道自己填的值被改了"


def test_bad_sentence_len_pair_reset(wizard_client):
    """不是 [下界, 上界] 的形状 → 重置成默认区间并警告。"""
    body = _preview(wizard_client, {"voice": {"avg_sentence_len": "8-20"}}).json()
    assert body["persona"]["voice"]["avg_sentence_len"] == [8, 20]
    assert any("avg_sentence_len" in w for w in body["warnings"])


# --------------------------------------------------------------------------- #
# 7. 落库：向导第 5 步复用 POST /v1/personas（判据 11）
# --------------------------------------------------------------------------- #
def test_wizard_confirmed_persona_is_created(wizard_client, repo):
    """第 5 步「创建」= 把预览拿到的 persona 原样交给 POST /v1/personas。"""
    before = len(repo.list_personas())
    draft = {
        "meta": {"name": "沈砚", "age": 41, "identity": "退役剑客", "archetype": "嘴硬心软"},
        "traits": {"extraversion": 0.85},
        "voice": {"avg_sentence_len": [5, 8]},
    }
    preview = _preview(wizard_client, draft).json()
    resp = wizard_client.post("/v1/personas", json={"persona": preview["persona"]})
    assert resp.status_code == 201
    created = resp.json()
    assert created["name"] == "沈砚"
    assert len(repo.list_personas()) == before + 1
    # 落库的是**冲突消解后**的卡，不是用户填的原始值
    card = repo.get_persona(created["id"])
    assert card.voice.avg_sentence_len == [5, 14]
    assert card.meta.age == 41


# --------------------------------------------------------------------------- #
# 8. mock provider：免密钥环境也得是 3 条（判据 3 的补充）
# --------------------------------------------------------------------------- #
def test_mock_provider_returns_three_lines_for_wizard():
    """mock 认的是条数说明：向导要 3 句就给 3 行，M3-A/B/C 仍给 6 行。"""
    from linger.llm.mock import MockProvider
    from linger.persona.generator import generate_persona
    from linger.persona.wizard import preview_draft

    wizard = preview_draft({"meta": {"name": "沈砚"}}, MockProvider())
    assert len(wizard.preview) == PREVIEW_COUNT

    full = generate_persona("一个嘴硬心软的退役剑客", MockProvider())
    assert len(full.preview) == 6, "M3-A 仍是 6 条，不能被向导改动波及"


def test_wizard_prompt_asks_for_three_lines():
    from linger.llm.mock import MockProvider
    from linger.persona.wizard import preview_draft

    class _Recorder(MockProvider):
        last_system = ""

        def generate(self, system, messages, ctx=None):
            type(self).last_system = system
            yield from super().generate(system, messages, ctx)

    preview_draft({"meta": {"name": "沈砚"}}, _Recorder())
    assert "写 3 句台词" in _Recorder.last_system
    # 复用 M3-A 的标记，不新加一个
    from linger.persona.generator import PREVIEW_MARKER

    assert PREVIEW_MARKER in _Recorder.last_system


def test_mock_provider_wizard_preview_dedup():
    """mock 的 3 条必须是**前 3 条**，不是随机挑——预览要稳定。"""
    from linger.llm.mock import MockProvider
    from linger.persona.wizard import preview_draft

    a = preview_draft({}, MockProvider()).preview
    b = preview_draft({}, MockProvider()).preview
    assert a == b
    assert len(a) == PREVIEW_COUNT


# --------------------------------------------------------------------------- #
# 9. 前端：入口 + 五步 + 预览调用 + 第 1 步校验（判据 8 / 9）
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


def test_html_has_wizard_entry(html):
    """判据 8：页面有向导入口，并与另外两条入口并列。"""
    assert 'id="btn-show-wizard"' in html
    assert "一步步来" in html
    # 三条入口都在同一个「人格」区块里
    for entry in ("btn-show-create", "btn-show-distill", "btn-show-wizard"):
        assert f'id="{entry}"' in html


def test_html_has_five_step_containers(html):
    """判据 8：五个步骤容器都在，且 step 编号连续 1~5。"""
    assert 'id="wizard-steps"' in html
    steps = re.findall(r'<section class="wizard-step[^"]*" data-step="(\d)"', html)
    assert steps == ["1", "2", "3", "4", "5"], steps


def test_html_step_titles(html):
    """五步标题与 issue 里给的表一致。"""
    for title in ("他是谁", "他的性子", "他怎么说", "他的边界", "看一眼"):
        assert title in html


def test_html_has_progress_and_controls(html):
    for el_id in (
        "wizard-progress",
        "wizard-prev",
        "wizard-next",
        "wizard-create",
        "wizard-cancel",
        "wizard-preview",
        "wizard-preview-lines",
        "wizard-preview-warnings",
    ):
        assert f'id="{el_id}"' in html, f"缺元素: #{el_id}"


def test_html_step1_fields(html):
    """第 1 步四个字段都在（name 必填）。"""
    for el_id in ("wiz-name", "wiz-age", "wiz-identity", "wiz-archetype"):
        assert f'id="{el_id}"' in html, f"缺元素: #{el_id}"


def test_html_bigfive_sliders(html):
    """第 2 步：五个滑杆，0~1，且每个都有显示当前值的 output。"""
    for trait in (
        "openness",
        "conscientiousness",
        "extraversion",
        "agreeableness",
        "neuroticism",
    ):
        assert f'id="wiz-{trait}"' in html, f"缺滑杆: wiz-{trait}"
    # 五个滑杆都在 0~1 之间
    ranges = re.findall(r'id="wiz-(?:openness|conscientiousness|extraversion|agreeableness|neuroticism)"[^>]*', html)
    assert len(ranges) == 5
    for tag in ranges:
        assert 'min="0"' in tag and 'max="1"' in tag, tag
    # 显示当前值：五个滑杆之外，第 3 步的表情滑杆也有一个 value 显示，共 6 个
    assert html.count('class="wizard-value"') == 6


def test_html_step3_and_step4_fields(html):
    for el_id in (
        "wiz-register",
        "wiz-sent-lo",
        "wiz-sent-hi",
        "wiz-emoji",
        "wiz-tics",
        "wiz-forbidden",
        "wiz-knows-not",
        "wiz-leak-policy",
    ):
        assert f'id="{el_id}"' in html, f"缺元素: #{el_id}"


def test_js_calls_draft_preview(js):
    """判据 8：app.js 调 /v1/personas/draft/preview。"""
    assert PREVIEW_ENDPOINT in js
    assert "refreshWizardPreview" in js


def test_js_wizard_step_gate(js):
    """判据 9：第 1 步名字为空时，「下一步」disabled。"""
    assert "wizardHasName" in js, "缺名字校验函数"
    # 校验与 disabled 必须在同一处
    block = js[js.index("function renderWizardStep") :]
    block = block[: block.index("\n  async function")]
    assert "el.wizardNext.disabled = true" in block
    assert "先给他起个名字" in block
    assert "wizardHasName()" in block


def test_js_wizard_keeps_state_across_steps(js):
    """换步不丢数据：切步只切 hidden，值统一收进 state.wizardDraft。"""
    assert "collectWizardDraft" in js
    assert "state.wizardDraft" in js
    block = js[js.index("function renderWizardStep") :]
    block = block[: block.index("\n  async function")]
    # 切步是 hidden 切换，不是重建 DOM
    assert "section.hidden = !current" in block
    assert "innerHTML = \"\"" not in block, "切步不该清空步骤容器"


def test_js_preview_requests_on_next_step(js):
    """实时预览：每次进入下一步（第 2 步起）请求一次。"""
    block = js[js.index("async function wizardNext") :]
    block = block[: block.index("\n  function wizardPrev")]
    assert "refreshWizardPreview()" in block
    assert "state.wizardStep >= 2" in block


def test_js_step1_does_not_request_preview(js):
    """第 1 步还没填名字时不请求。"""
    block = js[js.index("async function refreshWizardPreview") :]
    block = block[: block.index("\n  function renderWizardPreview")]
    assert "draft.meta.name" in block
    assert "return" in block


def test_js_create_uses_personas_endpoint(js):
    """第 5 步「创建」调 POST /v1/personas 并 openSession。"""
    block = js[js.index("async function wizardCreate") :]
    block = block[: block.index("\n  // 输入即入草稿")]
    assert '"/v1/personas"' in block
    assert "openSession(created.id" in block
    assert "persona: preview.persona" in block


def test_js_cancel_resets(js):
    block = js[js.index("function wizardCancel") :]
    block = block[: block.index("\n  async function wizardCreate")]
    assert "resetWizard()" in block
    assert "el.wizardPanel.hidden = true" in block


def test_css_has_wizard_styles(js, css):
    """样式复用已有变量与圆角，并与 JS 用到的类名对齐。"""
    for cls in (
        "wizard-panel",
        "wizard-step",
        "wizard-progress",
        "wizard-range",
        "wizard-value",
        "wizard-preview",
        "wizard-final-row",
    ):
        assert cls in js, f"JS 未使用样式类: {cls}"
        assert f".{cls}" in css, f"CSS 缺样式: .{cls}"
    assert "--accent-soft" in css
