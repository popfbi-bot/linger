"""人格市场（issue #34 的 1~8 条判据）。

本文件按判据分组编号，一条判据一组。**判据 2 是红线**（cloned 不许上架），
它的测试放在 A 组，并且刻意做成「错误分支真的走到」的形态：
不只断言状态码，还断言库里零行、作者视图为空、公共列表为空——
只测「响应不是 2xx」的写法，在「先插再回滚」的实现下也会绿。
"""

from __future__ import annotations

import pytest
from _fakes import FakeEmbedder, FakeLLM
from fastapi.testclient import TestClient

from linger.distill.compose import PROVENANCE_CLONED
from linger.market import service as market_service
from linger.market.models import STATUS_APPROVED, STATUS_PENDING
from linger.market.service import CLONED_LISTING_MESSAGE, MarketError
from linger.models import PersonaCard, now_utc
from linger.persona.templates import load_template
from linger.server.app import create_app

SECRET = "test-secret-market"
REVIEWER_EMAIL = "reviewer@linger.test"
GOOD_PASSWORD = "correct horse battery staple"


@pytest.fixture
def market_client(repo, monkeypatch):
    """带审核员名单的客户端。名单用环境变量注入——它就是部署配置。"""
    monkeypatch.setenv("LINGER_MARKET_REVIEWER_EMAILS", f"  {REVIEWER_EMAIL.upper()} ,, ")
    app = create_app(repo=repo, llm=FakeLLM(), embedder=FakeEmbedder(), auth_secret=SECRET)
    with TestClient(app) as c:
        yield c


def _register(client, email):
    """注册一个账号并返回 `(user_id, headers)`。

    邮箱已存在时走登录：同一组测试里要反复拿同一个作者的身份（下架、
    重新上架、改审核名单），每处都写一遍登录太吵。
    """
    resp = client.post("/v1/auth/register", json={"email": email, "password": GOOD_PASSWORD})
    if resp.status_code == 409:
        resp = client.post("/v1/auth/login", json={"email": email, "password": GOOD_PASSWORD})
    assert resp.status_code in (200, 201), resp.text
    body = resp.json()
    return body["user_id"], {"Authorization": f"Bearer {body['token']}"}


def _card(owner: str, *, prov_type: str = "authored", name: str = "小满") -> PersonaCard:
    """一张归属明确的卡。`owner` 写 `provenance.owner`——归属的唯一容器。"""
    card = load_template("linxi")
    card.id = f"p_{name}_{prov_type}"
    card.meta.name = name
    card.provenance = {"type": prov_type, "owner": owner}
    return card


# =========================================================================== #
# 判据 1：上架 → 201 / pending_review / listed_at 有值
# =========================================================================== #
def test_list_persona_returns_201_pending_with_listed_at(market_client, repo):
    uid, h = _register(market_client, "author@linger.test")
    card = _card(uid)
    repo.save_persona(card)

    resp = market_client.post(
        "/v1/market/listings", json={"persona_id": card.id, "title": "小满"}, headers=h
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["status"] == STATUS_PENDING
    assert body["listed_at"], "listed_at 必须有值"
    assert body["title"] == "小满"
    assert body["created"] is True
    # 快照就是上架那一刻的人格卡字节
    detail = market_client.get(f"/v1/market/listings/{body['id']}", headers=h).json()
    assert detail["persona"]["id"] == card.id
    assert detail["persona"]["meta"]["name"] == card.name


def test_persona_without_title_falls_back_to_name(market_client, repo):
    uid, h = _register(market_client, "author@linger.test")
    card = _card(uid, name="林汐")
    repo.save_persona(card)
    resp = market_client.post("/v1/market/listings", json={"persona_id": card.id}, headers=h)
    assert resp.status_code == 201
    assert resp.json()["title"] == "林汐"


def test_listing_is_idempotent_per_persona(market_client, repo):
    """同人格再上架 → 200 + 同 id + created:false，库里仍只有一条。"""
    uid, h = _register(market_client, "author@linger.test")
    card = _card(uid)
    repo.save_persona(card)
    first = market_client.post("/v1/market/listings", json={"persona_id": card.id}, headers=h)
    second = market_client.post("/v1/market/listings", json={"persona_id": card.id}, headers=h)
    assert second.status_code == 200
    assert second.json()["id"] == first.json()["id"]
    assert second.json()["created"] is False
    assert len(repo.list_listings()) == 1


def test_listing_requires_login(market_client, repo):
    uid, _ = _register(market_client, "author@linger.test")
    card = _card(uid)
    repo.save_persona(card)
    resp = market_client.post("/v1/market/listings", json={"persona_id": card.id})
    assert resp.status_code == 401


# =========================================================================== #
# 判据 2（红线）：cloned 不许上架
# =========================================================================== #
def test_cloned_persona_is_refused_with_403(market_client, repo):
    uid, h = _register(market_client, "author@linger.test")
    card = _card(uid, prov_type=PROVENANCE_CLONED)
    repo.save_persona(card)

    resp = market_client.post("/v1/market/listings", json={"persona_id": card.id}, headers=h)
    assert resp.status_code == 403, resp.text
    assert resp.json()["detail"] == CLONED_LISTING_MESSAGE
    assert "蒸馏产物不可上架" in resp.json()["detail"]


def test_cloned_refusal_really_takes_the_error_branch(market_client, repo):
    """错误分支真的走到：库里零行 + 作者视图空 + 公共列表空，三条同时验。

    只在成功路径上加个 assert 的写法，实现改成「先插再回滚」也照样绿。
    """
    uid, h = _register(market_client, "author@linger.test")
    card = _card(uid, prov_type=PROVENANCE_CLONED)
    repo.save_persona(card)
    before = len(repo.list_listings())

    market_client.post("/v1/market/listings", json={"persona_id": card.id}, headers=h)

    assert len(repo.list_listings()) == before, "被拒的上架不该在库里留下任何行"
    assert market_client.get("/v1/market/mine", headers=h).json()["count"] == 0
    assert market_client.get("/v1/market/listings").json()["count"] == 0


def test_cloned_gate_runs_before_writing_anything(repo):
    """服务层直调：闸门在**任何写库动作之前**返回。

    用「写库就炸」的替身来证明顺序：如果实现是先建条目再检查，
    这里会在 save 的地方抛 AssertionError，而不是抛 MarketError。
    """
    uid = "u_author"
    card = _card(uid, prov_type=PROVENANCE_CLONED)
    repo.save_persona(card)

    class ExplodingRepo:
        def get_persona(self, persona_id):
            return card

        def find_live_listing(self, persona_id):  # pragma: no cover - 不该被调到
            raise AssertionError("闸门之前不该查已有条目")

        def create_listing(self, listing):  # pragma: no cover - 不该被调到
            raise AssertionError("cloned 卡不该走到写库")

    with pytest.raises(MarketError) as ei:
        market_service.create_listing(
            ExplodingRepo(), user_id=uid, persona_id=card.id  # type: ignore[arg-type]
        )
    assert ei.value.kind == "cloned_forbidden"


def test_non_cloned_provenance_can_be_listed(market_client, repo):
    """反向断言：闸门不是「一律拒绝」。authored / generated 都能 201。"""
    uid, h = _register(market_client, "author@linger.test")
    for i, kind in enumerate(("authored", "generated")):
        card = _card(uid, prov_type=kind, name=f"人{i}")
        repo.save_persona(card)
        resp = market_client.post("/v1/market/listings", json={"persona_id": card.id}, headers=h)
        assert resp.status_code == 201, (kind, resp.text)


def test_cloned_with_whitespace_is_still_cloned(market_client, repo):
    uid, h = _register(market_client, "author@linger.test")
    card = _card(uid)
    card.provenance = {"type": "  Cloned ", "owner": uid}
    repo.save_persona(card)
    resp = market_client.post("/v1/market/listings", json={"persona_id": card.id}, headers=h)
    assert resp.status_code == 403


def test_card_without_provenance_type_is_not_cloned(market_client, repo):
    """缺 type 的老卡不算 cloned：不误杀、也不 500。"""
    uid, h = _register(market_client, "author@linger.test")
    card = _card(uid)
    card.provenance = {"owner": uid}
    repo.save_persona(card)
    resp = market_client.post("/v1/market/listings", json={"persona_id": card.id}, headers=h)
    assert resp.status_code == 201


def test_install_refuses_cloned_snapshot_written_behind_the_service(repo):
    """纵深：即使有人绕过服务层把一条 cloned 快照写进库，安装这一侧也 403。

    红线只钉在一处太脆——上架是入口，安装是出口。
    这里直接在存储层造一条 `approved` 的 cloned 条目，模拟「闸门被绕过」。
    """
    from linger.market.models import MarketListing

    author = "u_author"
    card = _card(author, prov_type=PROVENANCE_CLONED)
    repo.save_persona(card)
    repo.create_listing(
        MarketListing(
            id="l_poisoned",
            persona_id=card.id,
            author_id=author,
            title="毒",
            summary="",
            snapshot=card.model_dump(mode="json"),
            status=STATUS_APPROVED,
            listed_at=now_utc(),
        )
    )

    with pytest.raises(MarketError) as ei:
        market_service.install(repo, user_id="u_someone", listing_id="l_poisoned")
    assert ei.value.kind == "cloned_forbidden"
    assert not [p for p in repo.list_personas() if (p.provenance or {}).get("owner") == "u_someone"]


def _client_for(repo):
    app = create_app(repo=repo, llm=FakeLLM(), embedder=FakeEmbedder(), auth_secret=SECRET)
    return TestClient(app)


# =========================================================================== #
# 判据 3：上架别人的 → 403
# =========================================================================== #
def test_listing_someone_elses_persona_is_403(market_client, repo):
    owner_id, _ = _register(market_client, "owner@linger.test")
    _, other = _register(market_client, "other@linger.test")
    card = _card(owner_id)
    repo.save_persona(card)

    resp = market_client.post("/v1/market/listings", json={"persona_id": card.id}, headers=other)
    assert resp.status_code == 403
    assert "只能上架自己名下的人格" in resp.json()["detail"]
    assert len(repo.list_listings()) == 0, "被拒的上架不该留下行"


def test_persona_without_owner_is_not_listable(market_client, repo):
    """没写 owner 的卡（模板实例化产物）默认不可上架：归属不明 = 不归你。"""
    uid, h = _register(market_client, "author@linger.test")
    card = load_template("linxi")  # provenance 里没有 owner
    repo.save_persona(card)
    resp = market_client.post("/v1/market/listings", json={"persona_id": card.id}, headers=h)
    assert resp.status_code == 403


def test_ownership_check_precedes_cloned_check(market_client, repo):
    """别人的 cloned 卡给 not_owner，不是 cloned_forbidden。

    不把「别人手里有一张蒸馏卡」这件事告诉询问者。
    """
    owner_id, _ = _register(market_client, "owner@linger.test")
    _, other = _register(market_client, "other@linger.test")
    card = _card(owner_id, prov_type=PROVENANCE_CLONED)
    repo.save_persona(card)
    resp = market_client.post("/v1/market/listings", json={"persona_id": card.id}, headers=other)
    assert resp.status_code == 403
    assert "只能上架自己名下" in resp.json()["detail"]


def test_listing_missing_persona_is_404(market_client):
    _, h = _register(market_client, "author@linger.test")
    resp = market_client.post("/v1/market/listings", json={"persona_id": "p_nope"}, headers=h)
    assert resp.status_code == 404


# =========================================================================== #
# 判据 4：公共列表只返回 approved
# =========================================================================== #
def _make_listing(repo, author_id, *, status, name):
    card = _card(author_id, name=name)
    repo.save_persona(card)
    from linger.market.models import MarketListing

    return repo.create_listing(
        MarketListing(
            id=f"l_{name}",
            persona_id=card.id,
            author_id=author_id,
            title=name,
            summary="",
            snapshot=card.model_dump(mode="json"),
            status=status,
            listed_at=now_utc(),
        )
    )


def test_public_list_shows_only_approved(market_client, repo):
    uid, h = _register(market_client, "author@linger.test")
    _make_listing(repo, uid, status=STATUS_PENDING, name="待审")
    _make_listing(repo, uid, status="rejected", name="被拒")
    _make_listing(repo, uid, status=STATUS_APPROVED, name="已批")

    body = market_client.get("/v1/market/listings").json()
    assert body["count"] == 1
    assert [x["title"] for x in body["listings"]] == ["已批"]


def test_public_list_does_not_leak_persona_body(market_client, repo):
    uid, _ = _register(market_client, "author@linger.test")
    _make_listing(repo, uid, status=STATUS_APPROVED, name="已批")
    item = market_client.get("/v1/market/listings").json()["listings"][0]
    assert "persona" not in item
    assert "snapshot" not in item


def test_public_list_masks_author_id(market_client, repo):
    uid, _ = _register(market_client, "author@linger.test")
    _make_listing(repo, uid, status=STATUS_APPROVED, name="已批")
    item = market_client.get("/v1/market/listings").json()["listings"][0]
    assert item["author_id"] != uid
    assert "***" in item["author_id"]


def test_pending_listing_detail_hidden_from_strangers(market_client, repo):
    uid, h = _register(market_client, "author@linger.test")
    _, other = _register(market_client, "other@linger.test")
    listing = _make_listing(repo, uid, status=STATUS_PENDING, name="待审")

    assert market_client.get(f"/v1/market/listings/{listing.id}", headers=h).status_code == 200
    stranger = market_client.get(f"/v1/market/listings/{listing.id}", headers=other)
    assert stranger.status_code == 404, "非 approved 对别人是 404 不是 403——403 等于确认它存在"
    assert market_client.get(f"/v1/market/listings/{listing.id}").status_code == 404


def test_mine_shows_all_statuses_for_author(market_client, repo):
    uid, h = _register(market_client, "author@linger.test")
    _make_listing(repo, uid, status=STATUS_PENDING, name="待审")
    _make_listing(repo, uid, status="rejected", name="被拒")
    _make_listing(repo, uid, status=STATUS_APPROVED, name="已批")
    body = market_client.get("/v1/market/mine", headers=h).json()
    assert body["count"] == 3
    assert market_client.get("/v1/market/mine").status_code == 401


# =========================================================================== #
# 判据 5：审核 + 重复审核 → 4xx
# =========================================================================== #
def _listing_for(market_client, repo, email="author@linger.test"):
    uid, h = _register(market_client, email)
    card = _card(uid)
    repo.save_persona(card)
    resp = market_client.post("/v1/market/listings", json={"persona_id": card.id}, headers=h)
    assert resp.status_code == 201, resp.text
    return resp.json()["id"], uid, h


def test_approve_moves_to_approved_with_reviewer_recorded(market_client, repo):
    listing_id, _, _ = _listing_for(market_client, repo)
    _, rv = _register(market_client, REVIEWER_EMAIL)
    resp = market_client.post(
        f"/v1/market/listings/{listing_id}/review", json={"action": "approve"}, headers=rv
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == STATUS_APPROVED
    assert resp.json()["reviewed_at"]
    assert "***" in resp.json()["reviewer_id"]
    assert repo.get_listing(listing_id).status == STATUS_APPROVED


def test_reject_moves_to_rejected_and_stays_out_of_public(market_client, repo):
    listing_id, _, _ = _listing_for(market_client, repo)
    _, rv = _register(market_client, REVIEWER_EMAIL)
    resp = market_client.post(
        f"/v1/market/listings/{listing_id}/review",
        json={"action": "reject", "note": "描述不清"},
        headers=rv,
    )
    assert resp.status_code == 200
    assert resp.json()["status"] == "rejected"
    assert market_client.get("/v1/market/listings").json()["count"] == 0


@pytest.mark.parametrize("action", ["approve", "reject"])
def test_repeated_review_is_409(market_client, repo, action):
    listing_id, _, _ = _listing_for(market_client, repo)
    _, rv = _register(market_client, REVIEWER_EMAIL)
    first = market_client.post(
        f"/v1/market/listings/{listing_id}/review", json={"action": action}, headers=rv
    )
    assert first.status_code == 200
    again = market_client.post(
        f"/v1/market/listings/{listing_id}/review", json={"action": action}, headers=rv
    )
    assert again.status_code == 409


def test_review_after_reject_cannot_be_approved(market_client, repo):
    listing_id, _, _ = _listing_for(market_client, repo)
    _, rv = _register(market_client, REVIEWER_EMAIL)
    market_client.post(
        f"/v1/market/listings/{listing_id}/review", json={"action": "reject"}, headers=rv
    )
    resp = market_client.post(
        f"/v1/market/listings/{listing_id}/review", json={"action": "approve"}, headers=rv
    )
    assert resp.status_code == 409


def test_conditional_update_is_atomic_in_storage(repo):
    """并发安全靠条件更新，不靠先读后写。

    直接对存储层做两次同状态迁移：第二次必须返回 False，而不是把它再改一遍。
    """
    uid = "u_author"
    listing = _make_listing(repo, uid, status=STATUS_PENDING, name="x")
    kw = dict(reviewer_id="u_rv", note="", reviewed_at=now_utc())
    assert repo.review_listing(listing.id, from_status=STATUS_PENDING, to_status="approved", **kw)
    assert not repo.review_listing(
        listing.id, from_status=STATUS_PENDING, to_status="rejected", **kw
    ), "状态已变，第二次条件更新必须失败"
    assert repo.get_listing(listing.id).status == "approved"


def test_non_reviewer_cannot_review(market_client, repo):
    listing_id, _, author_h = _listing_for(market_client, repo)
    resp = market_client.post(
        f"/v1/market/listings/{listing_id}/review", json={"action": "approve"}, headers=author_h
    )
    assert resp.status_code == 403
    assert "审核权" in resp.json()["detail"]


def test_author_cannot_review_own_listing_even_if_reviewer(market_client, repo):
    """作者不能审自己：独立 guard，不靠「作者一般不在名单里」这种概率。"""
    listing_id, uid, h = _listing_for(market_client, repo, email=REVIEWER_EMAIL)
    resp = market_client.post(
        f"/v1/market/listings/{listing_id}/review", json={"action": "approve"}, headers=h
    )
    assert resp.status_code == 403
    assert "不能审核自己" in resp.json()["detail"]


def test_no_reviewer_list_means_nobody_can_review(market_client, repo, monkeypatch):
    """不配名单 = 没人能审（缺省安全方向：宁可没人批，不能谁都能批）。"""
    listing_id, _, _ = _listing_for(market_client, repo)
    _, rv = _register(market_client, REVIEWER_EMAIL)
    monkeypatch.setenv("LINGER_MARKET_REVIEWER_EMAILS", "")
    resp = market_client.post(
        f"/v1/market/listings/{listing_id}/review", json={"action": "approve"}, headers=rv
    )
    assert resp.status_code == 403


def test_reviewer_email_list_is_normalized():
    """空白 / 大小写 / 空项都要归一，否则会造出一个叫 "" 的审核员。"""
    emails = market_service.reviewer_emails("  A@B.com ,, c@d.com ")
    assert emails == frozenset({"a@b.com", "c@d.com"})
    assert market_service.reviewer_emails("") == frozenset()


def test_unknown_review_action_is_422(market_client, repo):
    listing_id, _, _ = _listing_for(market_client, repo)
    _, rv = _register(market_client, REVIEWER_EMAIL)
    resp = market_client.post(
        f"/v1/market/listings/{listing_id}/review", json={"action": "delete"}, headers=rv
    )
    assert resp.status_code == 422


# =========================================================================== #
# 判据 6：安装 → 新副本；装两次 → 幂等复用
# =========================================================================== #
def _approved_listing(market_client, repo):
    listing_id, author_id, h = _listing_for(market_client, repo)
    _, rv = _register(market_client, REVIEWER_EMAIL)
    market_client.post(
        f"/v1/market/listings/{listing_id}/review", json={"action": "approve"}, headers=rv
    )
    return listing_id, author_id


def test_install_creates_a_fresh_copy(market_client, repo):
    listing_id, author_id = _approved_listing(market_client, repo)
    installers = _register(market_client, "fan@linger.test")
    uid, h = installers
    resp = market_client.post(f"/v1/market/listings/{listing_id}/install", headers=h)
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["persona"]["id"] != body["provenance"]["origin"]["persona_id"]
    assert body["provenance"]["origin"]["listing_id"] == listing_id
    assert body["provenance"]["origin"]["author_id"] == author_id
    assert body["provenance"]["owner"] == uid
    assert body["copied"] is True and body["reused"] is False
    assert repo.get_persona(body["persona"]["id"]) is not None


def test_install_twice_reuses_one_copy(market_client, repo):
    listing_id, _ = _approved_listing(market_client, repo)
    _, h = _register(market_client, "fan@linger.test")
    first = market_client.post(f"/v1/market/listings/{listing_id}/install", headers=h)
    second = market_client.post(f"/v1/market/listings/{listing_id}/install", headers=h)
    assert first.status_code == 201
    assert second.status_code == 200
    assert second.json()["persona"]["id"] == first.json()["persona"]["id"]
    assert second.json()["reused"] is True
    assert len(repo.list_personas()) == 2, "原人格 + 一份副本，不该多出第二份"


def test_install_does_not_touch_the_original(market_client, repo):
    listing_id, author_id = _approved_listing(market_client, repo)
    listing_before = repo.get_listing(listing_id)
    original_before = repo.get_persona(listing_before.persona_id).model_dump(mode="json")
    _, h = _register(market_client, "fan@linger.test")
    market_client.post(f"/v1/market/listings/{listing_id}/install", headers=h)

    original_after = repo.get_persona(listing_before.persona_id).model_dump(mode="json")
    assert original_after == original_before, "安装不能改原作者的人格"
    assert repo.get_listing(listing_id).status == STATUS_APPROVED
    assert (repo.get_persona(listing_before.persona_id).provenance or {})["owner"] == author_id


def test_install_by_two_accounts_gives_each_their_own_copy(market_client, repo):
    listing_id, _ = _approved_listing(market_client, repo)
    _, h1 = _register(market_client, "fan1@linger.test")
    _, h2 = _register(market_client, "fan2@linger.test")
    a = market_client.post(f"/v1/market/listings/{listing_id}/install", headers=h1).json()
    b = market_client.post(f"/v1/market/listings/{listing_id}/install", headers=h2).json()
    assert a["persona"]["id"] != b["persona"]["id"], "两个人各自一份副本"


def test_install_is_idempotent_even_when_the_precheck_misses(repo):
    """幂等必须落在主键上，不能只落在「先查一次」上。

    这里把前置查询弄瞎（模拟并发窗口：两个请求同时通过了前置查询），
    第二次 save_install 会撞主键 → 服务层必须删掉自己刚建的副本、复用先到者那份。
    """
    from linger.market.models import MarketListing

    author = "u_author"
    card = _card(author, name="并发")
    repo.save_persona(card)
    listing = repo.create_listing(
        MarketListing(
            id="l_race",
            persona_id=card.id,
            author_id=author,
            title="并发",
            summary="",
            snapshot=card.model_dump(mode="json"),
            status=STATUS_APPROVED,
            listed_at=now_utc(),
        )
    )
    first = market_service.install(repo, user_id="u_fan", listing_id=listing.id)
    assert first.copied is True

    original_get_install = repo.get_install
    calls = {"n": 0}

    def blind_once(*args, **kwargs):
        """只弄瞎**前置**那一次查询；并发失败后的兜底查询必须是真的。

        「前置查询看不见已有安装」正是并发窗口的形态：两个请求同时进来，
        都以为自己是第一个。
        """
        calls["n"] += 1
        if calls["n"] == 1:
            return None
        return original_get_install(*args, **kwargs)

    repo.get_install = blind_once
    try:
        second = market_service.install(repo, user_id="u_fan", listing_id=listing.id)
    finally:
        repo.get_install = original_get_install

    assert second.copied is False, "撞主键后必须复用在先那份"
    assert second.persona.id == first.persona.id
    assert repo.get_install(listing.id, "u_fan").persona_id == first.persona.id
    copies = [p for p in repo.list_personas() if (p.provenance or {}).get("owner") == "u_fan"]
    assert len(copies) == 1, "失败方自己建的副本必须被删掉，不能留孤儿"


def test_install_unapproved_listing_is_409(market_client, repo):
    listing_id, _, _ = _listing_for(market_client, repo)  # 还在 pending
    _, h = _register(market_client, "fan@linger.test")
    resp = market_client.post(f"/v1/market/listings/{listing_id}/install", headers=h)
    assert resp.status_code == 409


def test_install_requires_login(market_client, repo):
    listing_id, _ = _approved_listing(market_client, repo)
    assert market_client.post(f"/v1/market/listings/{listing_id}/install").status_code == 401


def test_install_survives_author_deleting_their_persona(market_client, repo):
    """作者删了自己的卡，已安装的副本照样能开会话（副本是独立的人格）。"""
    listing_id, _ = _approved_listing(market_client, repo)
    _, h = _register(market_client, "fan@linger.test")
    copy = market_client.post(f"/v1/market/listings/{listing_id}/install", headers=h).json()
    listing = repo.get_listing(listing_id)
    repo.delete_persona(listing.persona_id)

    resp = market_client.post(
        "/v1/sessions", json={"persona_id": copy["persona"]["id"], "user_id": copy["provenance"]["owner"]}
    )
    assert resp.status_code == 201, resp.text
    detail = market_client.get(f"/v1/market/listings/{listing_id}")
    assert detail.status_code == 200, "快照让条目在作者删卡后仍然完整"


def test_install_after_copy_deleted_gives_a_new_one(market_client, repo):
    """复用的前提是副本还在。副本被用户删了 → 重装要给他一份新的。"""
    listing_id, _ = _approved_listing(market_client, repo)
    uid, h = _register(market_client, "fan@linger.test")
    first = market_client.post(f"/v1/market/listings/{listing_id}/install", headers=h).json()
    repo.delete_persona(first["persona"]["id"])

    second = market_client.post(f"/v1/market/listings/{listing_id}/install", headers=h)
    assert second.status_code == 201
    assert second.json()["persona"]["id"] != first["persona"]["id"]


# =========================================================================== #
# 判据 7：下架 → unlisted，公共列表消失，已装副本不受影响
# =========================================================================== #
def test_unlist_hides_listing_but_keeps_installed_copies(market_client, repo):
    listing_id, author_id = _approved_listing(market_client, repo)
    _, fan_h = _register(market_client, "fan@linger.test")
    copy = market_client.post(f"/v1/market/listings/{listing_id}/install", headers=fan_h).json()
    copy_before = repo.get_persona(copy["persona"]["id"]).model_dump(mode="json")

    _, author_h = _register(market_client, "author@linger.test")  # 同邮箱登录拿令牌
    resp = market_client.delete(f"/v1/market/listings/{listing_id}", headers=author_h)
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "unlisted"
    assert market_client.get("/v1/market/listings").json()["count"] == 0

    copy_after = repo.get_persona(copy["persona"]["id"]).model_dump(mode="json")
    assert copy_after == copy_before, "已安装的副本必须逐字节不变"


def test_uninstall_is_not_a_cascade(repo):
    """下架不连坐：直接比安装记录与副本数量，前后相等。"""
    author = "u_author"
    card = _card(author, name="级联")
    repo.save_persona(card)
    from linger.market.models import MarketListing

    listing = repo.create_listing(
        MarketListing(
            id="l_cascade",
            persona_id=card.id,
            author_id=author,
            title="级联",
            summary="",
            snapshot=card.model_dump(mode="json"),
            status=STATUS_APPROVED,
            listed_at=now_utc(),
        )
    )
    market_service.install(repo, user_id="u_fan", listing_id=listing.id)
    installs_before = repo.get_install(listing.id, "u_fan")
    personas_before = len(repo.list_personas())

    market_service.unlist(repo, user_id=author, listing_id=listing.id)

    assert repo.get_install(listing.id, "u_fan") == installs_before
    assert len(repo.list_personas()) == personas_before


def test_relisting_after_unlist_creates_a_new_entry(market_client, repo):
    listing_id, _ = _approved_listing(market_client, repo)
    _, author_h = _register(market_client, "author@linger.test")
    market_client.delete(f"/v1/market/listings/{listing_id}", headers=author_h)

    again = market_client.post(
        "/v1/market/listings",
        json={"persona_id": repo.get_listing(listing_id).persona_id},
        headers=author_h,
    )
    assert again.status_code == 201
    assert again.json()["id"] != listing_id, "下架后重新上架是新条目，审计才说得清"


def test_only_author_can_unlist(market_client, repo):
    listing_id, _ = _approved_listing(market_client, repo)
    _, other = _register(market_client, "other@linger.test")
    assert market_client.delete(f"/v1/market/listings/{listing_id}", headers=other).status_code == 403


def test_cannot_unlist_pending(market_client, repo):
    listing_id, _, author_h = _listing_for(market_client, repo)
    assert market_client.delete(f"/v1/market/listings/{listing_id}", headers=author_h).status_code == 409


# =========================================================================== #
# 判据 8：market_listing 承载上架状态，不塞进 persona
# =========================================================================== #
def test_market_tables_exist_in_sqlite(repo):
    names = {
        r[0]
        for r in repo.conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
    }
    assert {"market_listing", "market_install"} <= names


def test_listing_state_is_not_a_column_on_persona(repo):
    """反面断言：`persona` 表里没有任何上架字段。"""
    cols = {r[1] for r in repo.conn.execute("PRAGMA table_info(persona)").fetchall()}
    for forbidden in ("status_listing", "listed", "listed_at", "market_status", "listing_status"):
        assert forbidden not in cols


def test_market_tables_exist_in_postgres_schema():
    from linger.storage.postgres import SCHEMA_SQL

    assert "CREATE TABLE IF NOT EXISTS market_listing" in SCHEMA_SQL
    assert "CREATE TABLE IF NOT EXISTS market_install" in SCHEMA_SQL
    # 幂等落地：主键是 (listing_id, user_id)
    assert "PRIMARY KEY (listing_id, user_id)" in SCHEMA_SQL


def test_sqlite_live_listing_index_is_enforced(repo):
    """部分唯一索引真的生效：同人格不能有两条待审/在架。"""
    author = "u_author"
    card = _card(author, name="唯一")
    repo.save_persona(card)
    from linger.market.models import MarketListing

    def mk(id_, status):
        return MarketListing(
            id=id_,
            persona_id=card.id,
            author_id=author,
            title=id_,
            summary="",
            snapshot=card.model_dump(mode="json"),
            status=status,
            listed_at=now_utc(),
        )

    repo.create_listing(mk("l_1", STATUS_PENDING))
    got = repo.create_listing(mk("l_2", STATUS_PENDING))
    assert got.id == "l_1", "撞唯一索引时应返回已有那条，而不是插出第二条"
    assert len(repo.list_listings()) == 1


# =========================================================================== #
# 判据 9 的一部分：服务层不需要 HTTP 也能跑完整条状态机
# =========================================================================== #
def test_service_layer_needs_no_http(repo):
    author, fan = "u_author", "u_fan"
    card = _card(author, name="状态机")
    repo.save_persona(card)

    listing, created = market_service.create_listing(
        repo, user_id=author, persona_id=card.id, title="状态机"
    )
    assert created and listing.status == STATUS_PENDING
    assert market_service.public_listings(repo) == []

    reviewed = market_service.review(
        repo,
        reviewer_id="u_rv",
        reviewer_email="rv@x.test",
        reviewer_emails=frozenset({"rv@x.test"}),
        listing_id=listing.id,
        action="approve",
    )
    assert reviewed.status == STATUS_APPROVED
    assert [x.id for x in market_service.public_listings(repo)] == [listing.id]

    outcome = market_service.install(repo, user_id=fan, listing_id=listing.id)
    assert outcome.copied is True

    unlisted = market_service.unlist(repo, user_id=author, listing_id=listing.id)
    assert unlisted.status == "unlisted"
    assert market_service.public_listings(repo) == []
    assert repo.get_persona(outcome.persona.id) is not None
