"""人格市场的状态机。**所有判定都在这一个文件里**，HTTP 层只做翻译。

设计上刻意分成「纯函数判据 + 存储调用」两层：
判据（能不能上架 / 谁能审 / 安装给谁）不碰 IO，于是测试可以只调服务层跑完整条状态机，
不必起 HTTP（判据 9）。
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any

from ..distill.compose import PROVENANCE_CLONED
from ..models import PersonaCard, new_id, now_utc
from ..storage.base import Repository
from .models import (
    STATUS_APPROVED,
    STATUS_PENDING,
    STATUS_REJECTED,
    STATUS_UNLISTED,
    InstalledCopy,
    MarketListing,
)

#: 判据 2 的固定文案。测试逐字断言，所以它是一份契约而不是一句提示。
CLONED_LISTING_MESSAGE = (
    "蒸馏产物不可上架：它复刻的是一个真实的人的说话方式，那个人没有同意被全世界使用"
)

ACTIONS = {"approve": STATUS_APPROVED, "reject": STATUS_REJECTED}


class MarketError(Exception):
    """市场操作的失败。`kind` 是给 HTTP 层翻译状态码用的唯一依据。"""

    def __init__(self, kind: str, message: str) -> None:
        super().__init__(message)
        self.kind = kind
        self.message = message


def blocks_listing(card: PersonaCard) -> bool:
    """判据 2 的闸门：这张卡能不能上架。

    **认常量不认字面量**：与 `distill/compose.py::PROVENANCE_CLONED` 比较，
    改一侧常量值，闸门跟着变，而不是这边还停在旧字符串上悄悄放行。
    `" cloned "` 这种带空格的写法也算 cloned（归一后再比）。

    缺 `type` 的老卡不算 cloned——不误杀、不 500：它可能只是模板实例化产物，
    归属检查会拦住没有 owner 的那些。
    """
    prov = card.provenance or {}
    return str(prov.get("type") or "").strip().lower() == PROVENANCE_CLONED


def _owner_of(card: PersonaCard) -> str:
    return str((card.provenance or {}).get("owner") or "").strip()


def create_listing(
    repo: Repository,
    *,
    user_id: str,
    persona_id: str,
    title: str = "",
    summary: str = "",
) -> tuple[MarketListing, bool]:
    """上架（判据 1 / 2 / 3）。返回 `(条目, 是否本次新建)`。

    判定顺序**是设计的一部分**：
      1. 人格不存在 / 已删 → `not_found`
      2. 归属不是自己 → `not_owner`
      3. 是 cloned → `cloned_forbidden`
      4. 已经有待审/在架的条目 → 复用（`created=False`）

    为什么 cloned 判定排在归属**之后**：别人的 cloned 卡应该得到 `not_owner`，
    而不是 `cloned_forbidden`——不把「别人手里有一张蒸馏卡」这件事告诉询问者。
    """
    card = repo.get_persona(persona_id)
    if card is None:
        raise MarketError("not_found", f"人格不存在: {persona_id}")

    owner = _owner_of(card)
    if not owner or owner != user_id:
        raise MarketError("not_owner", "只能上架自己名下的人格")

    if blocks_listing(card):
        raise MarketError("cloned_forbidden", CLONED_LISTING_MESSAGE)

    existing = repo.find_live_listing(persona_id)
    if existing is not None and existing.author_id == user_id:
        return existing, False

    listing = MarketListing(
        id=new_id("l"),
        persona_id=persona_id,
        author_id=user_id,
        title=title.strip() or card.name,
        summary=summary.strip(),
        snapshot=card.model_dump(mode="json"),
        status=STATUS_PENDING,
        listed_at=now_utc(),
    )
    return repo.create_listing(listing), True


def public_listings(repo: Repository, limit: int = 50) -> list[MarketListing]:
    """判据 4：公共列表只返回 approved。

    过滤写在**这里**而不是「调用方记得传 status」：那正是最容易被忘记的地方。
    """
    return [it for it in repo.list_listings(limit=limit) if it.status == STATUS_APPROVED]


def own_listings(repo: Repository, user_id: str) -> list[MarketListing]:
    """作者自己的全部条目（任何状态）。判据 4 说的「另一个端点」就是它。"""
    return [it for it in repo.list_listings(limit=1000) if it.author_id == user_id]


def get_visible_listing(
    repo: Repository, listing_id: str, *, viewer_id: str | None
) -> MarketListing:
    """单条详情（判据 4 的可见性规则）。

    非 approved 对别人是 **404 而不是 403**：403 等于确认「这个 id 存在」
    ——那等于把「谁送审了、被拒了」变成可探测的事实。
    """
    listing = repo.get_listing(listing_id)
    if listing is None:
        raise MarketError("not_found", f"条目不存在: {listing_id}")
    if listing.status != STATUS_APPROVED and listing.author_id != viewer_id:
        raise MarketError("not_found", f"条目不存在: {listing_id}")
    return listing


def is_reviewer(email: str, reviewer_emails: frozenset[str]) -> bool:
    return email.strip().lower() in reviewer_emails


def reviewer_emails(configured: str | None = None) -> frozenset[str]:
    """审核员名单（判据 5）。

    **每次现读环境变量**，不读 `settings` 快照：`settings` 是 import 时
    `Settings()` 一次求值的结果，进程起来之后再改环境变量（部署换班、
    测试里 monkeypatch）它不会跟着变。而「谁能审」是安全判定，
    读到一个过期的空名单会让审核权**静默消失**——这正是最难查的那种坏。
    `configured` 只是给测试与调用方显式覆盖用的入口。

    解析时对大小写 / 空白 / 空项做了归一：`"  A@B.com ,, "` 不会造出一个叫 "" 的审核员。
    **不配名单 = 没人能审**（条目一直待审），不是「所有人都能审」——
    宁可没人批，也不能谁都能批。
    """
    raw = os.environ.get("LINGER_MARKET_REVIEWER_EMAILS") if configured is None else configured
    raw = (raw or "").strip()
    return frozenset(x.strip().lower() for x in raw.split(",") if x.strip())


def review(
    repo: Repository,
    *,
    reviewer_id: str,
    reviewer_email: str,
    reviewer_emails: frozenset[str],
    listing_id: str,
    action: str,
    note: str = "",
) -> MarketListing:
    """审核（判据 5）。两个 guard 都在这里，不靠「作者一般不在名单里」这种概率。

    顺序：动作合法 → 有审核权 → 不是本人 → 状态机。
    重复审核 → `bad_state`（409），不是幂等 200：否则前端无法区分
    「我批了」和「早就批了」，「谁在什么时刻批的」这个审计问题就答不出来。
    """
    if action not in ACTIONS:
        raise MarketError("bad_action", f"未知审核动作: {action}")
    if not is_reviewer(reviewer_email, reviewer_emails):
        raise MarketError("forbidden", "没有审核权：审核员名单里没有你")
    if action == "approve" and not reviewer_emails:
        # 不配名单 = 没人能审。这一条被上面的 is_reviewer 覆盖，留作显式意图。
        raise MarketError("forbidden", "没有审核员名单，没人能审")

    target = ACTIONS[action]
    listing = repo.get_listing(listing_id)
    if listing is None:
        raise MarketError("not_found", f"条目不存在: {listing_id}")
    if listing.author_id == reviewer_id:
        raise MarketError("self_review", "不能审核自己上架的条目")

    # 并发安全靠条件更新：`UPDATE … WHERE id AND status=<原状态>`，
    # 不靠「先读后写」——两个审核员同时点 approve 时，后到者拿到 False 而不是覆盖前者的记录。
    ok = repo.review_listing(
        listing_id,
        from_status=STATUS_PENDING,
        to_status=target,
        reviewer_id=reviewer_id,
        note=note,
        reviewed_at=now_utc(),
    )
    if not ok:
        raise MarketError("bad_state", "该条目不在待审状态，不能重复审核")
    updated = repo.get_listing(listing_id)
    assert updated is not None  # 刚更新过；真为 None 说明存储层坏了
    return updated


@dataclass
class InstallOutcome:
    persona: PersonaCard
    listing_id: str
    author_id: str
    #: True = 本次真建了新副本；False = 复用已有副本（判据 6 选了幂等复用）
    copied: bool


def install(repo: Repository, *, user_id: str, listing_id: str) -> InstallOutcome:
    """安装（判据 6）：在当前账号下生成一份**新的人格副本**。

    选**幂等复用**而不是「装两次给两份」：
      · 副本是「一份关系」，不是「一次下载」——记忆 / 情绪 / 关系都按
        `(persona_id, user_id)` 存。两份副本 = 用户的记忆被切碎到两个 id 下。
      · 幂等可观测：`reused` 让前端能说「已经装过了」。两份内容相同、id 不同的
        副本在数据上可区分、在用户眼里不可区分——那是最难查的 bug。

    并发窗口由 `market_install` 主键 `(listing_id, user_id)` 兜住：
    两个请求同时过了前置查询，后到者的 `save_install` 返回 False，
    于是删掉自己刚建的副本、复用先到者那份。
    """
    listing = repo.get_listing(listing_id)
    if listing is None:
        raise MarketError("not_found", f"条目不存在: {listing_id}")
    if listing.status != STATUS_APPROVED:
        raise MarketError("not_approved", "该条目未通过审核，不能安装")

    existing = repo.get_install(listing_id, user_id)
    if existing is not None:
        card = repo.get_persona(existing.persona_id)
        if card is not None:
            return InstallOutcome(card, listing_id, listing.author_id, copied=False)
        # 副本被用户删了 → 重装要给他一份新的，不是把删除当成「已安装」。
        repo.drop_install(listing_id, user_id)

    # 纵深防御（判据 2 的第二道）：即使有人绕过服务层把一条 cloned 快照写进库，
    # 安装这一侧也不放行。红线只钉在一处太脆。
    try:
        source = PersonaCard.model_validate(listing.snapshot)
    except Exception as exc:  # pragma: no cover - 快照是自己写进去的
        raise MarketError("bad_snapshot", f"条目快照损坏: {exc}") from exc
    if blocks_listing(source):
        raise MarketError("cloned_forbidden", CLONED_LISTING_MESSAGE)

    copy = PersonaCard.model_validate(listing.snapshot)
    copy.id = new_id("p")
    copy.provenance = {
        **(copy.provenance or {}),
        "owner": user_id,
        "origin": {
            "listing_id": listing.id,
            "persona_id": listing.persona_id,
            "author_id": listing.author_id,
        },
    }
    repo.save_persona(copy)

    saved = repo.save_install(
        InstalledCopy(
            listing_id=listing_id,
            user_id=user_id,
            persona_id=copy.id,
            installed_at=now_utc(),
        )
    )
    if not saved:
        # 并发失败方：删掉自己刚建的副本，复用先到者那份。
        repo.delete_persona(copy.id)
        # 必须先删副本再读安装记录：唯一键冲突说明「先到者已经写完了」，
        # 但读的时刻要与删的动作有确定的先后，否则读到的是自己那条（已删）。
        winner = repo.get_install(listing_id, user_id)
        if winner is None:  # pragma: no cover - 唯一键与查询不一致
            raise MarketError("bad_state", "安装记录冲突，请重试")
        card = repo.get_persona(winner.persona_id)
        if card is None:  # pragma: no cover
            raise MarketError("bad_state", "安装记录指向的副本不存在")
        return InstallOutcome(card, listing_id, listing.author_id, copied=False)

    return InstallOutcome(copy, listing_id, listing.author_id, copied=True)


def unlist(repo: Repository, *, user_id: str, listing_id: str) -> MarketListing:
    """下架（判据 7）：作者把自己 approved 的条目改为 unlisted。

    **不连坐已安装的副本**：下架只改 `market_listing.status` 一行，
    persona / memory / session 一律不碰。这条由「不写」保证，不是靠记得跳过。
    """
    listing = repo.get_listing(listing_id)
    if listing is None:
        raise MarketError("not_found", f"条目不存在: {listing_id}")
    if listing.author_id != user_id:
        raise MarketError("not_owner", "只能下架自己上架的条目")
    ok = repo.review_listing(
        listing_id,
        from_status=STATUS_APPROVED,
        to_status=STATUS_UNLISTED,
        reviewer_id=user_id,
        note="作者下架",
        reviewed_at=now_utc(),
    )
    if not ok:
        raise MarketError("bad_state", "只有在架（approved）的条目可以下架")
    updated = repo.get_listing(listing_id)
    assert updated is not None
    return updated


def listing_brief(listing: MarketListing) -> dict[str, Any]:
    """公共列表的一项。**不含人格卡正文**，作者 id 由 HTTP 层脱敏。"""
    return {
        "id": listing.id,
        "title": listing.title,
        "summary": listing.summary,
        "persona_name": (listing.snapshot.get("meta") or {}).get("name", ""),
        "listed_at": listing.listed_at.isoformat() if listing.listed_at else None,
    }
