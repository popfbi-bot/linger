"""人格市场前端（issue #39）：让用户点得到、上架得了。

#34 已经把市场后端验收合并，但用户走不到：#34 的服务端创建人格只补 `type`、
不写 `provenance.owner`，而归属检查要求 `owner == 当前用户`——所以用户自己造
的人格上架必然 403。这一棒补的就是这段路。

判据 1（未登录可只读浏览）——两个层次都测：
  · HTTP 层：公共列表与详情匿名可读，**不返回 401**；
  · DOM 层：假 fetch 里**不带令牌**跑真实 app.js，市场列表照样渲染。
    判据 1 的反面不是「接口 200」，而是「前端在未登录时不给看」。

判据 3（创建时带 owner）——用真实 node 跑真实 app.js 的四个创建入口，
断言请求体里有 `owner`（登录时）／没有 `owner`（未登录时）。

判据 4（上架入口只在自己的卡上）——**DOM 断言**：喂一份四张卡的列表
（自己的 / 别人的 / 无主的 / 自己的蒸馏卡），数按钮。

为什么用 node 跑真前端而不是 grep 源码：
  grep 只能证明「这句话存在」，证明不了「它被调用」。按钮是 app.js 造的，
  `tests/js/persona_cards_harness.js` 只提供假 DOM 与假 fetch，
  没有承担任何业务判断——判断被删掉的变异体在这套 harness 下同样会红。
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest
from _fakes import FakeEmbedder, FakeLLM
from conftest import needs_node
from fastapi.testclient import TestClient

from linger.market.models import STATUS_APPROVED, MarketListing
from linger.models import now_utc
from linger.persona.templates import load_template
from linger.server.app import create_app

SECRET = "test-secret-market-frontend"
REVIEWER_EMAIL = "reviewer@linger.test"
GOOD_PASSWORD = "correct horse battery staple"

STATIC = Path(__file__).resolve().parent.parent / "src" / "linger" / "server" / "static"
APP_JS = STATIC / "app.js"
HARNESS = Path(__file__).resolve().parent / "js" / "persona_cards_harness.js"



# --------------------------------------------------------------------------- #
# 夹具与小工具
# --------------------------------------------------------------------------- #
@pytest.fixture
def market_client(repo, monkeypatch):
    """带审核员名单的客户端。名单是环境变量——它就是部署配置。"""
    monkeypatch.setenv("LINGER_MARKET_REVIEWER_EMAILS", REVIEWER_EMAIL)
    app = create_app(repo=repo, llm=FakeLLM(), embedder=FakeEmbedder(), auth_secret=SECRET)
    with TestClient(app) as c:
        yield c


@pytest.fixture
def js() -> str:
    return APP_JS.read_text(encoding="utf-8")


@pytest.fixture
def html() -> str:
    return (STATIC / "index.html").read_text(encoding="utf-8")


def _register(client, email: str) -> tuple[str, dict[str, str]]:
    resp = client.post("/v1/auth/register", json={"email": email, "password": GOOD_PASSWORD})
    if resp.status_code == 409:
        resp = client.post("/v1/auth/login", json={"email": email, "password": GOOD_PASSWORD})
    assert resp.status_code in (200, 201), resp.text
    body = resp.json()
    return body["user_id"], {"Authorization": f"Bearer {body['token']}"}


def _approved_listing(repo, author_id: str, *, name: str = "小满") -> MarketListing:
    """直接在库里造一条已过审的条目——审核链路 #34 已验过，这里不重跑。"""
    card = load_template("linxi")
    card.id = f"p_{name}"
    card.meta.name = name
    card.provenance = {"type": "authored", "owner": author_id}
    repo.save_persona(card)
    return repo.create_listing(
        MarketListing(
            id=f"l_{name}",
            persona_id=card.id,
            author_id=author_id,
            title=name,
            summary="",
            snapshot=card.model_dump(mode="json"),
            status=STATUS_APPROVED,
            listed_at=now_utc(),
        )
    )


def _run_frontend(payload: dict) -> dict:
    """把真实 app.js 交给 node 跑一遍，返回它在假 DOM 上造出来的卡片与按钮。"""
    full = {"app_js": str(APP_JS), **payload}
    proc = subprocess.run(
        ["node", str(HARNESS)],
        input=json.dumps(full),
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert proc.returncode == 0, f"前端脚本跑挂了：\n{proc.stderr[:3000]}"
    return json.loads(proc.stdout)


def _card(pid: str, owner: str, *, prov_type: str = "authored", name: str = "") -> dict:
    """人格摘要的形状与服务端 `_persona_brief` 一致。"""
    return {
        "id": pid,
        "name": name or pid,
        "identity": "",
        "archetype": "",
        "version": 1,
        "owner": owner,
        "provenance": {"type": prov_type},
    }


# =========================================================================== #
# 判据 1：未登录可只读浏览，不弹登录墙
# =========================================================================== #
def test_anon_can_read_public_list(market_client, repo):
    """公共列表匿名可读：200，不是 401/403，也不需要任何令牌。"""
    author, _ = _register(market_client, "author@linger.test")
    listing = _approved_listing(repo, author)

    resp = market_client.get("/v1/market/listings")
    assert resp.status_code == 200, "逛市场不该先注册"
    body = resp.json()
    assert [it["id"] for it in body["listings"]] == [listing.id]
    # 只读浏览不等于把卡全放出去：列表项不含人格卡正文（#34 已定）
    assert "persona" not in body["listings"][0]


def test_anon_can_read_listing_detail(market_client, repo):
    """详情匿名可读，且作者 id 是脱敏的。"""
    author, _ = _register(market_client, "author@linger.test")
    listing = _approved_listing(repo, author)

    resp = market_client.get(f"/v1/market/listings/{listing.id}")
    assert resp.status_code == 200
    body = resp.json()
    assert body["persona_name"] == "小满"
    assert body["author_id"].count("***") == 1, "公开的是人格，不是账号"


@needs_node
def test_anon_frontend_still_renders_market():
    """判据 1 的前端侧：**不带令牌**跑真实 app.js，市场列表照样渲染。

    「不弹登录墙」在 #33 已有 `test_js_has_no_login_wall` 钉着（不许跳转、不许拦）。
    这条补另一半：多了市场面板之后，未登录的人**看得见**它。
    """
    out = _run_frontend(
        {
            "personas": [],
            "account": None,
            "local_storage": {"linger.user_id": "u_anonymous"},
            "anon_market": [
                {"id": "l_1", "title": "小满", "persona_name": "小满", "author_id": "u_888***dd"}
            ],
        }
    )
    assert [m["id"] for m in out["market"]] == ["l_1"]
    assert "/v1/market/mine" not in out["fetchCalls"], (
        "未登录不去请求「我的条目」——那是 401，徒增一条报错"
    )


def test_js_does_not_gate_market_behind_login(js):
    """源码层护栏：公共列表的加载不能以登录为前提。"""
    assert "location.href" not in js
    start = js.index("async function loadMarket(")
    end = js.index("async function loadMarketMine(")
    assert "state.account" not in js[start:end], "公共列表不该依赖登录态"


# =========================================================================== #
# 判据 3：创建人格时带上 owner —— 前端真的发了
# =========================================================================== #
@needs_node
def test_frontend_creates_persona_with_owner_when_logged_in():
    """登录时，每个创建入口发出的请求体都要带 `owner = 当前账号`。

    服务端也会补（登录时），但前端主动声明让「这张卡归谁」成为请求里可见的事实，
    而不是落库时的副作用。
    """
    out = _run_frontend(
        {
            "personas": [],
            "account": {"user_id": "u_account_1", "email": "a@b.com"},
            "local_storage": {"linger.user_id": "u_account_1", "linger.token": "tok"},
        }
    )
    calls = [c for c in out["createCalls"] if c["url"] == "/v1/personas"]
    assert calls, "前端的创建入口一个都没被触发，harness 白跑了"
    # 两条覆盖到实函数：一句话生成（B）与向导（D）。A 只有一行、C 与 B 同构，
    # 但它们共用同一个 ownerClaim()，所以漏了谁这条也会红。
    entries = {c["entry"].split(":")[0] for c in calls}
    assert {"B", "D"} <= entries, f"至少该走到 B 与 D 两条入口，实际：{entries}"
    for call in calls:
        assert call["body"].get("owner") == "u_account_1", f"{call['entry']} 没带 owner"


@needs_node
def test_frontend_does_not_invent_owner_when_anonymous():
    """未登录时**不写** owner：匿名卡没有归属是后端既定语义，前端不许编一个。"""
    out = _run_frontend(
        {
            "personas": [],
            "account": None,
            "local_storage": {"linger.user_id": "u_anonymous"},
        }
    )
    calls = [c for c in out["createCalls"] if c["url"] == "/v1/personas"]
    assert calls, "前端的创建入口一个都没被触发"
    for call in calls:
        assert "owner" not in call["body"], f"{call['entry']} 匿名时不该带 owner"


# =========================================================================== #
# 判据 3（服务端侧）：owner 落库，且只认当前账号
# =========================================================================== #
def test_server_stamps_owner_on_create(repo):
    """登录创建 → `provenance.owner` 就是账号 id，并且摘要里带出来。

    摘要带 owner 是判据 4 的前提：前端拿到它才判得出「这是不是我的卡」。
    """
    app = create_app(repo=repo, llm=FakeLLM(), embedder=FakeEmbedder(), auth_secret=SECRET)
    with TestClient(app) as c:
        uid, headers = _register(c, "stamp@linger.test")
        created = c.post(
            "/v1/personas", json={"template_id": "linxi", "owner": uid}, headers=headers
        )
        assert created.status_code == 201, created.text
        body = created.json()
        assert body["owner"] == uid
        assert body["provenance"]["owner"] == uid

        card = repo.get_persona(body["id"])
        assert card is not None
        assert (card.provenance or {}).get("owner") == uid


def test_server_refuses_claiming_someone_elses_ownership(repo):
    """声称别人的人格 → 403，且**不留半张卡**（在落库之前拦）。"""
    before = len(repo.list_personas())
    app = create_app(repo=repo, llm=FakeLLM(), embedder=FakeEmbedder(), auth_secret=SECRET)
    with TestClient(app) as c:
        _uid, headers = _register(c, "claim@linger.test")
        resp = c.post(
            "/v1/personas",
            json={"persona": {"meta": {"name": "冒名"}}, "owner": "u_somebody_else"},
            headers=headers,
        )
        assert resp.status_code == 403
        assert "归属" in resp.json()["detail"]
        assert len(repo.list_personas()) == before, "被拒的创建不该留下行"


def test_anonymous_create_has_no_owner(client):
    """匿名创建不开小差：没有 owner。"""
    body = client.post("/v1/personas", json={"template_id": "linxi"}).json()
    assert body["owner"] == ""
    assert "owner" not in body["provenance"]


def test_template_persona_can_be_listed_after_login(repo):
    """从「点模板」到「上架」的完整实现，走一遍才算真的补上了。

    #34 之后这里一直是 403——用户点了模板、想上架，只得到「只能上架自己名下的人格」。
    """
    app = create_app(repo=repo, llm=FakeLLM(), embedder=FakeEmbedder(), auth_secret=SECRET)
    with TestClient(app) as c:
        uid, headers = _register(c, "tpl@linger.test")
        persona = c.post(
            "/v1/personas", json={"template_id": "laojiang", "owner": uid}, headers=headers
        ).json()
        assert persona["owner"] == uid

        listed = c.post("/v1/market/listings", json={"persona_id": persona["id"]}, headers=headers)
        assert listed.status_code == 201, listed.text
        assert listed.json()["status"] == "pending_review"


def test_first_owner_wins_and_later_visitors_cannot_steal(repo):
    """模板行只被认领一次：后来者点同一个模板，不会把别人的卡改成自己的。"""
    app = create_app(repo=repo, llm=FakeLLM(), embedder=FakeEmbedder(), auth_secret=SECRET)
    with TestClient(app) as c:
        first, first_h = _register(c, "first@linger.test")
        second, second_h = _register(c, "second@linger.test")
        p1 = c.post(
            "/v1/personas", json={"template_id": "linxi", "owner": first}, headers=first_h
        ).json()
        p2 = c.post(
            "/v1/personas", json={"template_id": "linxi", "owner": second}, headers=second_h
        ).json()
        assert p1["id"] == p2["id"], "模板实例化幂等，这里是前提不是判据"
        assert p2["owner"] == first, "后来的用户不能夺走已有主人的卡"


# =========================================================================== #
# 判据 4：上架入口只在自己的卡上出现（DOM 断言）
# =========================================================================== #
@needs_node
def test_list_button_only_on_own_cards():
    """四张卡 => 只有一张有「上架」。判据 4 的核心证据。

    另外三张各代表一种「不该有入口」的现实：
      · 别人的（模板共享行的另一个主人）
      · 无主的（匿名期造出来的卡）
      · 自己的 cloned（蒸馏产物，红线）
    """
    out = _run_frontend(
        {
            "personas": [
                _card("p_own", "u_me", name="我的"),
                _card("p_other", "u_someone", name="别人的"),
                _card("p_orphan", "", name="无主的"),
                _card("p_clone", "u_me", prov_type="cloned", name="蒸馏的"),
            ],
            "account": {"user_id": "u_me", "email": "me@b.com"},
            "local_storage": {"linger.user_id": "u_me", "linger.token": "tok"},
        }
    )
    by_id = {c["id"]: c["actions"] for c in out["cards"]}
    assert by_id["p_own"] == ["list", "delete"], "自己的卡：上架 + 删除"
    assert by_id["p_other"] == ["delete"], "别人的卡上不该出现上架入口"
    assert "list" not in by_id["p_orphan"], "无主的卡默认不可上架"
    assert "list" not in by_id["p_clone"], "蒸馏产物是红线，不给入口"


@needs_node
def test_no_list_button_at_all_when_anonymous():
    """未登录：一张卡都没有上架入口，但卡片本身照常显示（不弹墙）。"""
    out = _run_frontend(
        {
            "personas": [_card("p_looks_mine", "u_me"), _card("p_orphan", "")],
            "account": None,
            "local_storage": {"linger.user_id": "u_me"},
        }
    )
    assert [c["id"] for c in out["cards"]] == ["p_looks_mine", "p_orphan"]
    for card in out["cards"]:
        assert "list" not in card["actions"], "未登录时上架必然 403，前端不该给入口"


def test_js_ownership_check_reads_server_field(js):
    """归属判断依据服务端给的 `owner`，不是「列表里的都是我的」。

    后者在模板共享人格的场景下是错的（那一行可能是别人先认领的），
    而这正是判据 4 要防的那一种「看起来对、其实显示给错人」。
    """
    start = js.index("function isOwnPersona(")
    end = js.index("function ownerClaim(")
    body = js[start:end]
    assert "p.owner" in body
    assert "state.account" in body or "state.userId" in body
    assert "personaCache.includes" not in body


# =========================================================================== #
# 市场面板本身：安装 / 下架 / 我的条目
# =========================================================================== #
def test_anon_cannot_install_but_logged_in_can(market_client, repo):
    """安装要登录（后端既定），但**看**不要。前端因此把按钮换成一句提示。"""
    author, _ = _register(market_client, "author@linger.test")
    listing = _approved_listing(repo, author)

    assert market_client.post(f"/v1/market/listings/{listing.id}/install").status_code == 401

    fan, fan_h = _register(market_client, "fan@linger.test")
    installed = market_client.post(f"/v1/market/listings/{listing.id}/install", headers=fan_h)
    assert installed.status_code == 201
    assert installed.json()["reused"] is False


def test_installed_copy_is_owned_by_installer(market_client, repo):
    """装来的副本归**安装者**：他也得能再上架、能删，而不是操纵不了。"""
    author, _ = _register(market_client, "author@linger.test")
    listing = _approved_listing(repo, author)
    fan, fan_h = _register(market_client, "fan@linger.test")

    body = market_client.post(f"/v1/market/listings/{listing.id}/install", headers=fan_h).json()
    card = repo.get_persona(body["persona"]["id"])
    assert card is not None
    assert (card.provenance or {}).get("owner") == fan
    assert (card.provenance or {}).get("origin", {}).get("listing_id") == listing.id


def test_mine_lists_every_status(market_client):
    """「我的条目」显示任何状态——用户要看见「我送上去的怎么样了」。"""
    author, headers = _register(market_client, "author@linger.test")
    persona = market_client.post(
        "/v1/personas", json={"template_id": "linxi", "owner": author}, headers=headers
    ).json()
    listed = market_client.post(
        "/v1/market/listings", json={"persona_id": persona["id"]}, headers=headers
    ).json()

    mine = market_client.get("/v1/market/mine", headers=headers).json()
    assert [it["id"] for it in mine["listings"]] == [listed["id"]]
    assert mine["listings"][0]["status"] == "pending_review"


def test_unlist_hides_it_from_public_list(market_client, repo):
    """下架之后公共列表里就没了——点「下架」看到的结果应当是这个。"""
    author, headers = _register(market_client, "author@linger.test")
    listing = _approved_listing(repo, author)

    assert (
        market_client.delete(f"/v1/market/listings/{listing.id}", headers=headers).status_code
        == 200
    )
    assert market_client.get("/v1/market/listings").json()["count"] == 0


def test_frontend_calls_only_documented_endpoints(js, client):
    """前端打的市场端点必须都在后端存在（少一个就是「点了没反应」）。"""
    paths = {getattr(r, "path", "") for r in client.app.routes}
    for path in (
        "/v1/market/listings",
        "/v1/market/listings/{listing_id}",
        "/v1/market/listings/{listing_id}/install",
        "/v1/market/mine",
    ):
        assert path in paths, f"后端缺端点：{path}"
    for literal in ("/v1/market/listings", "/v1/market/mine", "/install"):
        assert literal in js, f"前端没有调用 {literal}"


def test_list_request_does_not_carry_owner(js):
    """上架只报 persona_id：归属由服务端按库里的卡判，前端自称不算数。"""
    start = js.index("async function listPersona(")
    end = js.index("async function unlistListing(")
    body = js[start:end]
    assert "persona_id" in body
    assert "owner" not in body


# =========================================================================== #
# 范围：既有前端契约不许被这一棒碰坏
# =========================================================================== #
def test_existing_frontend_contracts_kept(js, html):
    """#33 的两条既有行为：登录面板初始 hidden、登录后 user_id 换成账号的。"""
    start = html.index('id="auth-panel"')
    assert "hidden" in html[start : start + 200], "登录面板必须初始收起（不弹登录墙）"
    assert "state.userId = payload.user_id" in js
    assert 'localStorage.setItem("linger.user_id", payload.user_id)' in js


def test_market_panel_is_always_visible(html):
    """市场是常驻区块，不是浮层：既没有 `hidden`，也不用点任何东西才出现。"""
    key = 'id="market-card"'
    tag_start = html.rindex("<", 0, html.index(key))
    tag = html[tag_start : html.index(">", html.index(key)) + 1]
    assert "hidden" not in tag, "市场面板一进来就该在页面上"


def test_persona_brief_exposes_owner_and_provenance(client, persona):  # noqa: ARG001
    """判据 4 的数据前提：人格摘要必须带 owner 与 provenance。

    没有它们，前端只能猜——而「列表里的都是我的」这个猜法在模板共享人格时是错的。
    """
    body = client.get("/v1/personas").json()
    first = body["personas"][0]
    assert "owner" in first
    assert "provenance" in first
    assert first["provenance"]["type"] == "authored"
