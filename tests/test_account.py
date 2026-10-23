"""M3-H 账号体系的后端判据（issue #33 的 1~7 条）。

分三组，对应三种「能证明它坏了」的方式：

    A. 存储层（`repo` 夹具，临时 SQLite）：密码哈希、唯一性、认领去重。
       这一组不需要 HTTP，所以判据可以精确到「同一句话的两条记忆被合并成一条」。
    B. HTTP 层（`auth_client` 夹具）：状态码、响应体、令牌失效。
    C. 向后兼容（判据 5）：不带令牌时，所有端点的响应与「没有账号体系」时一致。

C 组是本棒的**红线**：现有 708 个用例大量不带令牌，它们全绿本身就是
最有力的证明；这里再补一条「同一个请求，带与不带空 Authorization 结果相同」，
把「我们改了什么」钉在版本里。
"""

from __future__ import annotations

import hashlib
import time
from datetime import date

import pytest
from _fakes import FakeEmbedder, FakeLLM
from fastapi.testclient import TestClient

from linger.accounts.security import (
    TOKEN_TTL_SECONDS,
    decode_token,
    hash_password,
    issue_token,
    verify_password,
)
from linger.accounts.service import (
    AUTH_FAILED_MESSAGE,
    authenticate,
    claim_anonymous_data,
    register,
)
from linger.models import DiaryEntry, MemoryNode, Session
from linger.persona.templates import load_template
from linger.server.app import create_app

GOOD_PASSWORD = "correct horse battery staple"
SECRET = "test-secret-for-tokens"


# --------------------------------------------------------------------------- #
# 夹具
# --------------------------------------------------------------------------- #
@pytest.fixture
def auth_client(repo):
    """带固定密钥的客户端。

    固定密钥是必须的：`create_app` 不传 secret 时每个进程随机生成一个，
    测试里就没法自己造令牌（也就没法验「伪造令牌 → 401」）。
    """
    app = create_app(
        repo=repo, llm=FakeLLM(), embedder=FakeEmbedder(), auth_secret=SECRET
    )
    with TestClient(app) as c:
        yield c


@pytest.fixture
def world(repo, persona):
    """一份「匿名期间的真实数据」：一个匿名 id 的会话 + 记忆 + 日记。"""
    sid = "s_anon_device"
    repo.save_session(Session(id=sid, persona_id=persona.id, user_id="u_device"))
    repo.add_memories(
        [
            MemoryNode(
                persona_id=persona.id,
                user_id="u_device",
                content="我妈妈下周要去医院复查",
                layer=2,
            ),
            MemoryNode(
                persona_id=persona.id,
                user_id="u_device",
                content="她喜欢在雨天听我念书",
                layer=3,
            ),
        ]
    )
    repo.save_diary(
        DiaryEntry(
            persona_id=persona.id,
            user_id="u_device",
            date=date(2025, 3, 1),
            content="今天她把妈妈的事说了，说得很轻。",
        )
    )
    return {"session_id": sid, "anonymous": "u_device", "persona": persona}


def _register(client, email="a@b.com", password=GOOD_PASSWORD):
    resp = client.post("/v1/auth/register", json={"email": email, "password": password})
    assert resp.status_code == 201, resp.text
    return resp.json()


def _bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


# =========================================================================== #
# 判据 1：注册 → 201 + 稳定 user_id；同邮箱重复 → 409
# =========================================================================== #
def test_register_returns_stable_user_id(auth_client):
    body = _register(auth_client)
    assert body["user_id"].startswith("u_")
    assert body["email"] == "a@b.com"
    assert body["token"]

    # 「稳定」= 同一个账号反复登录拿到的 user_id 一模一样
    again = auth_client.post(
        "/v1/auth/login", json={"email": "a@b.com", "password": GOOD_PASSWORD}
    )
    assert again.status_code == 200
    assert again.json()["user_id"] == body["user_id"]


def test_register_same_email_twice_is_409(auth_client):
    _register(auth_client)
    resp = auth_client.post(
        "/v1/auth/register", json={"email": "a@b.com", "password": "another password"}
    )
    assert resp.status_code == 409


def test_register_email_is_case_and_space_insensitive(auth_client):
    """` A@B.com ` 与 `a@b.com` 是同一个人，否则一个人能注册出两个账号。

    这是判据 1 的一部分：唯一性必须建立在**归一化之后**的邮箱上。
    """
    _register(auth_client, email="a@b.com")
    resp = auth_client.post(
        "/v1/auth/register", json={"email": "  A@B.CoM  ", "password": GOOD_PASSWORD}
    )
    assert resp.status_code == 409


def test_register_rejects_bad_email_and_short_password(auth_client):
    assert (
        auth_client.post(
            "/v1/auth/register", json={"email": "not-an-email", "password": GOOD_PASSWORD}
        ).status_code
        == 422
    )
    assert (
        auth_client.post(
            "/v1/auth/register", json={"email": "c@d.com", "password": "short"}
        ).status_code
        == 422
    )


# =========================================================================== #
# 判据 2：密码不明文 → 库里存的既不是明文，也不是它的可逆编码
# =========================================================================== #
def test_password_is_not_stored_in_plaintext(repo, auth_client):
    _register(auth_client, password="MySecret123")

    row = repo.find_account("a@b.com")
    stored = row["password_hash"]
    assert stored != "MySecret123"
    # 「可逆编码」也算明文：base64 / hex / rot13 都不是哈希
    import base64
    import binascii

    forbidden = {
        "MySecret123",
        base64.b64encode(b"MySecret123").decode(),
        base64.urlsafe_b64encode(b"MySecret123").decode().rstrip("="),
        binascii.hexlify(b"MySecret123").decode(),
        hashlib.md5(b"MySecret123").hexdigest(),  # 无盐快哈希同样不合格
        hashlib.sha256(b"MySecret123").hexdigest(),
    }
    assert not (forbidden & {stored, stored.split("$")[-1]}), "密码以可逆/无盐形式落库"
    # 明文字符串绝不能出现在整行里的任何字段
    assert all("MySecret123" not in str(v) for v in row.values())


def test_password_hash_is_salted_and_self_describing():
    a = hash_password(GOOD_PASSWORD)
    b = hash_password(GOOD_PASSWORD)
    assert a != b, "两个用户用同一个密码，库里必须不一样（盐要独立）"
    assert a.startswith("scrypt$"), "哈希串要自描述算法与参数"
    assert verify_password(GOOD_PASSWORD, a) and verify_password(GOOD_PASSWORD, b)
    assert not verify_password(GOOD_PASSWORD + "x", a)


def test_stored_hash_roundtrips_through_api(auth_client, repo):
    """库里那串哈希真的能被登录校验通过——不是「长得像哈希」的摆设。"""
    _register(auth_client)
    stored = repo.find_account("a@b.com")["password_hash"]
    assert verify_password(GOOD_PASSWORD, stored)
    assert authenticate(repo, "a@b.com", GOOD_PASSWORD).user_id


# =========================================================================== #
# 判据 3：登录 200 + token；密码错 401；**邮箱不存在与密码错一致**
# =========================================================================== #
def test_login_success_returns_token(auth_client):
    body = _register(auth_client)
    resp = auth_client.post(
        "/v1/auth/login", json={"email": "a@b.com", "password": GOOD_PASSWORD}
    )
    assert resp.status_code == 200
    assert resp.json()["token"]
    assert resp.json()["user_id"] == body["user_id"]


def test_login_wrong_password_is_401(auth_client):
    _register(auth_client)
    resp = auth_client.post(
        "/v1/auth/login", json={"email": "a@b.com", "password": "wrong password"}
    )
    assert resp.status_code == 401


def test_login_unknown_email_and_wrong_password_are_indistinguishable(auth_client):
    """判据 3 的核心：否则等于送一个用户枚举接口。

    三处都必须一致——状态码、响应体、**还有耗时**。
    只对齐状态码是不够的：密码错要多算一次 scrypt，若「邮箱不存在」直接返回，
    两条路径的耗时差一个数量级，枚举照样做得出来。
    """
    _register(auth_client)
    wrong_pw = auth_client.post(
        "/v1/auth/login", json={"email": "a@b.com", "password": "wrong password"}
    )
    no_such = auth_client.post(
        "/v1/auth/login", json={"email": "ghost@b.com", "password": "wrong password"}
    )

    assert wrong_pw.status_code == no_such.status_code == 401
    assert wrong_pw.json() == no_such.json() == {"detail": AUTH_FAILED_MESSAGE}
    assert wrong_pw.headers.get("www-authenticate") == no_such.headers.get("www-authenticate")

    # 耗时：单次抖动大，取多轮最小值（最小值最接近「纯计算」的成本）
    def fastest(email: str) -> float:
        best = 9e9
        for _ in range(3):
            t0 = time.perf_counter()
            auth_client.post(
                "/v1/auth/login", json={"email": email, "password": "wrong password"}
            )
            best = min(best, time.perf_counter() - t0)
        return best

    t_existing = fastest("a@b.com")
    t_missing = fastest("ghost@b.com")
    ratio = max(t_existing, t_missing) / max(min(t_existing, t_missing), 1e-6)
    assert ratio < 3.0, (
        f"邮箱存在({t_existing:.4f}s) 与不存在({t_missing:.4f}s) 的耗时差 {ratio:.1f} 倍，"
        "足以枚举用户——不存在的邮箱也必须烧一次假哈希"
    )


# =========================================================================== #
# 判据 4：带 token 能识别 user_id；伪造 / 过期 → 401（不是 500）
# =========================================================================== #
def test_me_returns_user_id_with_valid_token(auth_client):
    body = _register(auth_client)
    resp = auth_client.get("/v1/auth/me", headers=_bearer(body["token"]))
    assert resp.status_code == 200
    assert resp.json()["user_id"] == body["user_id"]
    assert resp.json()["email"] == "a@b.com"


@pytest.mark.parametrize(
    "token,label",
    [
        ("", "缺令牌"),
        ("not-a-token", "根本不是令牌"),
        ("aaa.bbb", "两段但都是垃圾"),
        ("!!!.???", "非法 base64"),
    ],
)
def test_me_rejects_garbage_tokens_as_401(auth_client, token, label):
    resp = auth_client.get("/v1/auth/me", headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 401, f"{label} 应是 401，实际 {resp.status_code}"


def test_forged_token_signature_is_401_not_500(auth_client):
    """伪造 = 改 payload 不重签。必须 401。"""
    body = _register(auth_client)
    head, _, _sig = body["token"].partition(".")
    forged = f"{head}.{'A' * 43}"
    resp = auth_client.get("/v1/auth/me", headers=_bearer(forged))
    assert resp.status_code == 401


def test_token_signed_with_other_secret_is_401(auth_client):
    """别人拿自己的密钥签一个 `sub` 是我的令牌——不能进。"""
    body = _register(auth_client)
    evil = issue_token(body["user_id"], secret="not-the-server-secret")
    assert auth_client.get("/v1/auth/me", headers=_bearer(evil)).status_code == 401


def test_expired_token_is_401(auth_client):
    body = _register(auth_client)
    expired = issue_token(
        body["user_id"], secret=SECRET, email="a@b.com", now=int(time.time()) - TOKEN_TTL_SECONDS - 10
    )
    resp = auth_client.get("/v1/auth/me", headers=_bearer(expired))
    assert resp.status_code == 401


def test_tampered_payload_is_401(auth_client):
    """直接改 payload 里的 sub（不重签）——签名立刻对不上。"""
    import base64
    import json

    body = _register(auth_client)
    head, _, sig = body["token"].partition(".")
    payload = json.loads(base64.urlsafe_b64decode(head + "=" * (-len(head) % 4)))
    payload["sub"] = "u_someone_else"
    raw = base64.urlsafe_b64encode(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    ).rstrip(b"=").decode()
    resp = auth_client.get("/v1/auth/me", headers=_bearer(f"{raw}.{sig}"))
    assert resp.status_code == 401


def test_token_payload_has_no_secret_material(auth_client):
    """令牌是 base64，不是加密：里面**不能**有密码或哈希。"""
    body = _register(auth_client)
    payload = decode_token(body["token"], secret=SECRET)
    assert set(payload) == {"sub", "email", "epoch", "iat", "exp"}
    assert "password" not in str(payload).lower()


# =========================================================================== #
# 判据 5：不登录时匿名玩法照旧（红线）
# =========================================================================== #
def test_anonymous_session_and_chat_still_work(auth_client, persona):
    """不带任何 Authorization：与「账号体系之前」完全一样。"""
    resp = auth_client.post(
        "/v1/sessions", json={"persona_id": persona.id, "user_id": "u_anon"}
    )
    assert resp.status_code == 201
    assert resp.json()["user_id"] == "u_anon"  # 原样采用，没有被替换成任何人
    sid = resp.json()["session_id"]

    detail = auth_client.get(f"/v1/sessions/{sid}")
    assert detail.status_code == 200

    mem = auth_client.get(f"/v1/sessions/{sid}/memories")
    assert mem.status_code == 200

    comp = auth_client.get(f"/v1/companion/{persona.id}/u_anon")
    assert comp.status_code == 200
    assert comp.json()["user_id"] == "u_anon"

    diary = auth_client.get(f"/v1/personas/{persona.id}/diary", params={"user_id": "u_anon"})
    assert diary.status_code == 200


def test_empty_authorization_header_keeps_anonymous_behaviour(auth_client, persona):
    """空 Authorization（有些代理会塞一个空头）不能把请求变成 401。"""
    resp = auth_client.post(
        "/v1/sessions",
        json={"persona_id": persona.id, "user_id": "u_anon"},
        headers={"Authorization": ""},
    )
    assert resp.status_code == 201


def _without_timestamps(payload):
    """去掉时间戳再比：空档案是**当场构造**的，`updated_at` 取 now，
    两次调用必然差几微秒。那不是「行为被改变了」，是时钟在走。
    判据要盯的是身份与内容，不是此刻是几点。
    """
    if isinstance(payload, dict):
        return {
            k: _without_timestamps(v)
            for k, v in payload.items()
            if k not in {"updated_at", "created_at", "started_at", "last_active_at"}
        }
    if isinstance(payload, list):
        return [_without_timestamps(v) for v in payload]
    return payload


def test_anonymous_list_of_endpoints_matches_without_any_auth_header(auth_client, persona, world):
    """同一条端点序列，走两遍：一遍完全不带头，一遍带空头。结果必须逐字段相同。"""
    paths = [
        ("get", f"/v1/sessions/{world['session_id']}", None),
        ("get", f"/v1/sessions/{world['session_id']}/memories", None),
        ("get", f"/v1/companion/{persona.id}/{world['anonymous']}", None),
        ("get", f"/v1/personas/{persona.id}/diary", {"user_id": world["anonymous"]}),
    ]
    for method, path, params in paths:
        plain = auth_client.request(method.upper(), path, params=params)
        blank = auth_client.request(
            method.upper(), path, params=params, headers={"Authorization": ""}
        )
        assert plain.status_code == blank.status_code, path
        assert _without_timestamps(plain.json()) == _without_timestamps(blank.json()), (
            f"{path} 的响应被 Authorization 头改变了"
        )


def test_authenticated_user_can_still_use_endpoints(auth_client, persona):
    """登录后一切照旧可用——账号只提供身份，不拿走任何功能。"""
    body = _register(auth_client)
    headers = _bearer(body["token"])
    uid = body["user_id"]

    resp = auth_client.post(
        "/v1/sessions", json={"persona_id": persona.id, "user_id": uid}, headers=headers
    )
    assert resp.status_code == 201
    sid = resp.json()["session_id"]
    assert auth_client.get(f"/v1/sessions/{sid}/memories", headers=headers).status_code == 200


def test_authenticated_write_to_other_user_id_is_rejected(auth_client, persona):
    """登录了却还在往别人的 user_id 里写——那是跨账号写，必须挡。

    返回 409（不是静默改写）：静默改会让「你以为在跟她聊、其实写进了另一个账号」
    这种 bug 永远查不出来。
    """
    body = _register(auth_client)
    resp = auth_client.post(
        "/v1/sessions",
        json={"persona_id": persona.id, "user_id": "u_someone_else"},
        headers=_bearer(body["token"]),
    )
    assert resp.status_code == 409


# =========================================================================== #
# 判据 6：认领——记忆/日记迁到账号名下，且**不重复**
# =========================================================================== #
def test_claim_moves_memories_and_diaries(auth_client, world):
    body = _register(auth_client)
    headers = _bearer(body["token"])

    resp = auth_client.post(
        "/v1/auth/claim",
        json={"persona_ids": [world["persona"].id]},
        headers=headers,
    )
    assert resp.status_code == 200, resp.text
    result = resp.json()
    assert result["claimed"] == {"memories": 2, "diaries": 1}
    assert world["anonymous"] in result["claimed_from"]

    # 账号名下看得到
    memories = auth_client.get(
        f"/v1/personas/{world['persona'].id}/diary",
        params={"user_id": body["user_id"]},
        headers=headers,
    )
    assert memories.json()["count"] == 1


def test_claim_is_idempotent_no_duplicates(auth_client, repo, world):
    """判据 6 的核心：认领两次，库里还是那两条记忆、那一篇日记。"""
    body = _register(auth_client)
    headers = _bearer(body["token"])
    pid = world["persona"].id

    first = auth_client.post("/v1/auth/claim", json={"persona_ids": [pid]}, headers=headers).json()
    second = auth_client.post("/v1/auth/claim", json={"persona_ids": [pid]}, headers=headers).json()

    assert first["claimed"]["memories"] == 2
    assert second["claimed"]["memories"] == 0, "第二次不该再搬任何东西"
    # 第二次如实回答：那些 id 名下已经没有数据可搬（会话也改了名字），
    # 所以是 considered（我核对过，那里是空的），不是 skipped_duplicates（重复）。
    # 硬编一个「跳过 N 条」是在骗调用方——见 service 里的注释。
    assert second["skipped_duplicates"] == 0
    assert second["claimed_from"] == []
    assert world["anonymous"] in second["considered"] or second["considered"] == []

    nodes = repo.list_memories(pid, body["user_id"])
    assert len(nodes) == 2, f"记忆变成了 {len(nodes)} 条：{ [n.content for n in nodes] }"
    assert len(repo.list_diary(pid, body["user_id"])) == 1


def test_claim_reports_skips_when_source_data_still_present(repo, persona):
    """源数据还在（比如上一轮只搬了一半就失败），重试时重复项必须被数出来。

    与上一条的区别：上一条是「搬干净之后再点一次」，源为空，只能回答 considered；
    这一条是「源还在」，那么每一条都该算 skipped_duplicates。
    两种情形必须都能从响应里读出来，否则调用方无法区分「幂等」与「坏了」。
    """

    account = register(repo, "retry@b.com", GOOD_PASSWORD)
    repo.add_memories(
        [
            MemoryNode(persona_id=persona.id, user_id=account.user_id, content="我在准备考研"),
        ]
    )
    repo.save_session(Session(id="s_retry", persona_id=persona.id, user_id="u_half"))
    repo.add_memories(
        [MemoryNode(persona_id=persona.id, user_id="u_half", content="我在准备考研")]
    )

    result = claim_anonymous_data(
        repo, account.user_id, [persona.id], source_user_ids=["u_half"]
    )
    assert result.memories == 0
    assert result.skipped_duplicates == 1
    assert result.claimed_from == ["u_half"]


def test_claim_dedups_by_content_not_by_id(repo, persona):
    """匿名期间同一句话被记了两遍（两个不同 id）——认领后只能剩一条。

    这条是「按内容去重」与「按 id 去重」的分水岭：
    按 id 去重会留下两条内容一样的记忆，用户看到的就是「你们俩说过两遍」。
    """
    repo.save_session(Session(id="s_x", persona_id=persona.id, user_id="u_dev"))
    repo.add_memories(
        [
            MemoryNode(persona_id=persona.id, user_id="u_dev", content="我养了一只叫团子的猫"),
            MemoryNode(persona_id=persona.id, user_id="u_dev", content="我养了一只叫团子的猫 "),
            MemoryNode(persona_id=persona.id, user_id="u_dev", content="我养了一只叫团子的猫"),
        ]
    )
    account = register(repo, "dup@b.com", GOOD_PASSWORD)
    result = claim_anonymous_data(repo, account.user_id, [persona.id])

    assert result.memories == 1, "三条同内容记忆应只搬一条"
    assert result.skipped_duplicates == 2
    contents = [n.content for n in repo.list_memories(persona.id, account.user_id)]
    assert len(contents) == 1, contents


def test_claim_keeps_existing_account_memory_and_skips_duplicate(repo, persona):
    """账号名下已经有一条同内容记忆：认领不能再插一条。"""
    repo.save_session(Session(id="s_x", persona_id=persona.id, user_id="u_dev"))
    repo.add_memories(
        [MemoryNode(persona_id=persona.id, user_id="u_dev", content="我在准备考研")]
    )
    account = register(repo, "keep@b.com", GOOD_PASSWORD)
    repo.add_memories(
        [MemoryNode(persona_id=persona.id, user_id=account.user_id, content="我在准备考研")]
    )

    result = claim_anonymous_data(repo, account.user_id, [persona.id])
    assert result.memories == 0 and result.skipped_duplicates == 1
    assert len(repo.list_memories(persona.id, account.user_id)) == 1


def test_claim_requires_login(auth_client):
    resp = auth_client.post("/v1/auth/claim", json={"persona_ids": ["p_any"]})
    assert resp.status_code == 401


def test_claim_scoped_to_requested_personas(repo, persona):
    """只搬请求里列出的 persona。别的 persona 下的数据一个字节都不许动。

    这条挡的是「认领变成本站任意用户数据搬运工」——
    persona_ids 是调用方给的，服务端绝不能自己扩展范围。
    """
    other = load_template("laojiang")
    repo.save_persona(other)
    repo.save_session(Session(id="s_a", persona_id=persona.id, user_id="u_dev"))
    repo.save_session(Session(id="s_b", persona_id=other.id, user_id="u_dev"))
    repo.add_memories(
        [
            MemoryNode(persona_id=persona.id, user_id="u_dev", content="林汐这边的事"),
            MemoryNode(persona_id=other.id, user_id="u_dev", content="老姜那边的事"),
        ]
    )
    account = register(repo, "scope@b.com", GOOD_PASSWORD)
    claim_anonymous_data(repo, account.user_id, [persona.id])

    assert len(repo.list_memories(persona.id, account.user_id)) == 1
    assert repo.list_memories(other.id, account.user_id) == []
    assert len(repo.list_memories(other.id, "u_dev")) == 1, "别的 persona 的数据被搬走了"


def test_claim_carries_sessions_so_memory_panel_is_not_empty(auth_client, repo, world):
    """会话跟着搬——否则新设备打开会话，记忆面板是空的。

    `GET /v1/sessions/{sid}/memories` 走的是**会话**的 (persona_id, user_id)。
    会话不搬，记忆搬了也看不见。
    """
    body = _register(auth_client)
    headers = _bearer(body["token"])
    pid = world["persona"].id

    auth_client.post("/v1/auth/claim", json={"persona_ids": [pid]}, headers=headers)

    moved = repo.get_session(world["session_id"])
    assert moved.user_id == body["user_id"]
    resp = auth_client.get(f"/v1/sessions/{world['session_id']}/memories", headers=headers)
    assert resp.json()["count"] == 2


def test_claim_refuses_sessions_of_another_account(repo, persona):
    """已经在另一个账号名下的会话，不许被「认领」拿走。

    源 user_id 是前端给的字符串，只按字符串匹配就等于「知道别人 id 就能抢」。
    """
    victim = register(repo, "victim@b.com", GOOD_PASSWORD)
    repo.save_session(Session(id="s_victim", persona_id=persona.id, user_id=victim.user_id))
    repo.add_memories(
        [MemoryNode(persona_id=persona.id, user_id=victim.user_id, content="这是我的私事")]
    )

    attacker = register(repo, "attacker@b.com", GOOD_PASSWORD)
    claim_anonymous_data(
        repo, attacker.user_id, [persona.id], source_user_ids=[victim.user_id]
    )

    assert repo.get_session("s_victim").user_id == victim.user_id, "别人的会话被搬走了"
    assert repo.list_memories(persona.id, attacker.user_id) == []


def test_claim_ignores_unknown_source_ids(repo, persona):
    """前端瞎报一个不存在的 user_id：不能被当成错误，也不能搬来任何东西。"""
    register(repo, "quiet@b.com", GOOD_PASSWORD)
    account = authenticate(repo, "quiet@b.com", GOOD_PASSWORD)
    result = claim_anonymous_data(
        repo, account.user_id, [persona.id], source_user_ids=["u_never_existed"]
    )
    assert result.memories == 0 and result.diaries == 0
