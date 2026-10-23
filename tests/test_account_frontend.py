"""M3-H 前端判据（issue #33 的判据 7 与 8）。

前端在 pytest 里能验的只有「契约」这一层，但那已经够挡住绝大多数回归：

- **判据 7（跨设备）用后端验**：它的本质是「同一个 user_id → 同一份记忆」，
  与有没有界面无关。这里用两个 TestClient 模拟两台设备，逐字段比对读到的记忆。
- **判据 8（界面）用静态扫描验**：登录后 user_id 要换、要能看出已登录、
  未登录时不能弹登录墙。这三件事都能在 `app.js` / `index.html` 里定位到具体语句，
  所以用「脚本语言无关的断言」而不是「跑一个浏览器」——CI 里没有浏览器，
  而一个跑不起来的判据等于没有判据。
"""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest
from _fakes import FakeEmbedder, FakeLLM
from fastapi.testclient import TestClient

from linger.models import DiaryEntry, MemoryNode
from linger.server.app import create_app

STATIC = Path(__file__).resolve().parent.parent / "src" / "linger" / "server" / "static"
SECRET = "test-secret-for-frontend"
GOOD_PASSWORD = "correct horse battery staple"


@pytest.fixture(scope="module")
def js() -> str:
    return (STATIC / "app.js").read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def html() -> str:
    return (STATIC / "index.html").read_text(encoding="utf-8")


@pytest.fixture
def device(repo):
    """一台「设备」= 一个 TestClient。它们共享同一个库，模拟「同一台服务器」。"""

    def _make():
        app = create_app(
            repo=repo, llm=FakeLLM(), embedder=FakeEmbedder(), auth_secret=SECRET
        )
        return TestClient(app).__enter__()

    return _make


# =========================================================================== #
# 判据 7：跨设备可见——同一账号在两个不同 user_id 下读到相同记忆集合
# =========================================================================== #
def test_same_account_sees_same_memories_on_two_devices(repo, persona, device):
    """设备 A 匿名聊出来，登录并认领；设备 B 用另一个匿名 id 登录，读到同一份。"""
    dev_a = device()
    dev_b = device()

    # ---- 设备 A：匿名开始，产生数据 ----
    dev_a.post(
        "/v1/sessions", json={"persona_id": persona.id, "user_id": "u_device_a"}
    ).json()["session_id"]
    repo.add_memories(
        [
            MemoryNode(persona_id=persona.id, user_id="u_device_a", content="我妈妈下周要去复查"),
            MemoryNode(persona_id=persona.id, user_id="u_device_a", content="她怕打雷"),
        ]
    )
    repo.save_diary(
        DiaryEntry(
            persona_id=persona.id,
            user_id="u_device_a",
            date=date(2025, 4, 1),
            content="今天她说起妈妈。",
        )
    )

    # ---- 设备 A 登录 + 认领 ----
    reg = dev_a.post(
        "/v1/auth/register", json={"email": "same@b.com", "password": GOOD_PASSWORD}
    ).json()
    headers = {"Authorization": f"Bearer {reg['token']}"}
    account_id = reg["user_id"]
    dev_a.post("/v1/auth/claim", json={"persona_ids": [persona.id]}, headers=headers)

    # ---- 设备 A 用账号 id 重新开会话（前端登录后就是这么做的）----
    sid_a2 = dev_a.post(
        "/v1/sessions", json={"persona_id": persona.id, "user_id": account_id}, headers=headers
    ).json()["session_id"]

    # ---- 设备 B：另一台机器，另一个本地匿名 id，登录同一个账号 ----
    login = dev_b.post(
        "/v1/auth/login", json={"email": "same@b.com", "password": GOOD_PASSWORD}
    ).json()
    assert login["user_id"] == account_id, "同一账号在两台设备上必须是同一个 user_id"
    headers_b = {"Authorization": f"Bearer {login['token']}"}
    sid_b = dev_b.post(
        "/v1/sessions", json={"persona_id": persona.id, "user_id": account_id}, headers=headers_b
    ).json()["session_id"]

    mem_a = dev_a.get(f"/v1/sessions/{sid_a2}/memories", headers=headers).json()
    mem_b = dev_b.get(f"/v1/sessions/{sid_b}/memories", headers=headers_b).json()
    assert mem_a["count"] == 2
    assert {m["content"] for m in mem_a["memories"]} == {m["content"] for m in mem_b["memories"]}

    diary_a = dev_a.get(
        f"/v1/personas/{persona.id}/diary", params={"user_id": account_id}, headers=headers
    ).json()
    diary_b = dev_b.get(
        f"/v1/personas/{persona.id}/diary", params={"user_id": account_id}, headers=headers_b
    ).json()
    assert diary_a["count"] == diary_b["count"] == 1


def test_two_different_accounts_do_not_see_each_other(repo, persona, device):
    """跨设备的另一面：**不同**账号之间必须继续隔离。

    「记忆能跟过来」很容易做成「谁的记忆都能看」，这条是那条红线的陪跑判据。
    """
    dev = device()
    one = dev.post(
        "/v1/auth/register", json={"email": "one@b.com", "password": GOOD_PASSWORD}
    ).json()
    two = dev.post(
        "/v1/auth/register", json={"email": "two@b.com", "password": GOOD_PASSWORD}
    ).json()
    repo.add_memories(
        [MemoryNode(persona_id=persona.id, user_id=one["user_id"], content="只有一号知道的事")]
    )

    sid = dev.post(
        "/v1/sessions",
        json={"persona_id": persona.id, "user_id": two["user_id"]},
        headers={"Authorization": f"Bearer {two['token']}"},
    ).json()["session_id"]
    got = dev.get(
        f"/v1/sessions/{sid}/memories",
        headers={"Authorization": f"Bearer {two['token']}"},
    ).json()
    assert got["count"] == 0


# =========================================================================== #
# 判据 8：前端——登录后换 user_id、看得出已登录、未登录不弹墙
# =========================================================================== #
def test_login_panel_exists_in_dom(html):
    for anchor in (
        'id="auth-panel"',
        'id="auth-email"',
        'id="auth-password"',
        'id="btn-auth-submit"',
        'id="account-state"',
    ):
        assert anchor in html, f"缺登录界面元素：{anchor}"


def test_login_panel_is_hidden_by_default(html):
    """**不弹登录墙**：面板初始 hidden，未登录的人看到的就是原来那个页面。"""
    assert 'id="auth-panel" class="auth-panel" hidden' in html


def test_js_switches_user_id_after_login(js):
    """登录后必须把 userId 换成账号的——这是「换设备记忆跟过来」的前端那一半。

    断言得具体到语句：只查「有没有 account 这个词」挡不住「登录了但还在用匿名 id」。
    """
    assert "state.userId = payload.user_id" in js, "登录成功后没把 userId 换成账号的"
    assert 'localStorage.setItem("linger.user_id", payload.user_id)' in js, (
        "换了内存里的 userId 却没落盘：刷新一下又回匿名"
    )
    assert "linger.token" in js, "没有把令牌存下来"


def test_js_sends_bearer_token(js):
    """所有请求都要能带上令牌，否则登录了等于没登录。"""
    assert "Authorization: `Bearer ${token}`" in js
    assert "authHeaders()" in js


def test_js_shows_logged_in_state(js):
    """界面上要能看出「已登录」：顶栏文字 + 邮件。"""
    assert "已登录 · " in js
    assert "pill-account" in js
    assert "未登录 · 数据在本机" in js


def test_js_has_no_login_wall(js):
    """未登录时不许跳转/拦截。全站唯一强制登录的是 `/v1/auth/me`（只用于恢复登录态）。"""
    assert "location.href" not in js, "前端不该靠跳转做登录流程"
    assert "window.location" not in js
    # 匿名入口必须仍在：开会话时 user_id 来自 state，而不是「必须已登录」
    assert "user_id: state.userId" in js


def test_js_claims_after_login(js):
    """登录后要主动认领，否则用户会觉得「登录之后我之前聊的没了」。"""
    assert "/v1/auth/claim" in js
    assert "persona_ids" in js


def test_js_restores_session_on_load(js):
    """刷新页面时用 `/v1/auth/me` 确认令牌还算不算数；失败就安静退回匿名。"""
    assert "/v1/auth/me" in js
    assert "restoreAccount" in js


def test_auth_endpoints_called_by_frontend_exist(client):
    """JS 里写的端点必须真在服务端存在——两边的契约不能靠人记。"""
    for path in ("/v1/auth/register", "/v1/auth/login", "/v1/auth/me", "/v1/auth/claim"):
            # 未认证的 GET/POST 会 401/405/422，但**不会是 404**；
        # 404 才是「端点不存在」的信号（FastAPI 对未注册路径返回 404）。
        resp = client.get(path)
        assert resp.status_code != 404, f"{path} 在服务端不存在"
