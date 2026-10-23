"""账号的业务动作：注册、认证、认领匿名数据。

**这一层不碰 HTTP**：不知道 409/401 是什么，只抛 `AccountError(kind=...)`，
由 `server/app.py` 翻成状态码。这样「账号逻辑」可以在没有 FastAPI 的
环境里单独测（本文件末尾的判据就是靠这一点）。

三条设计取舍写在前面：

1. **登录失败的两种原因对外一模一样**（判据 3）。不只是状态码与文案相同，
   连耗时也拉平：邮箱不存在时烧一次假 scrypt，见 `security.dummy_verify`。
2. **认领按内容去重**（判据 6）。迁移后同一条记忆不能变成两份——
   先把目标下已有的内容读出来做白名单，再逐条判。光靠 id 去重不够：
   匿名期间在同一台设备上重复聊出同一句话，会得到两条不同 id 的同内容记忆。
3. **不搬 turn / session**。会话仍是匿名 id 名下的历史；
   它不在产品的记忆可见面上（记忆 / 日记 / 档案 / 情绪 / 关系才是），
   搬它会连带搬 scenario_progress 与 trace，改动面远大于收益。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from ..models import new_id
from .models import Account
from .security import (
    TokenError,
    decode_token,
    dummy_verify,
    hash_password,
    issue_token,
    normalize_email,
    valid_email,
    verify_password,
)

if TYPE_CHECKING:  # 运行时不 import：storage.base 依赖 accounts.models，反向 import 会成环
    from ..storage.base import Repository

# 密码长度下限。8 位是「不挡真人、挡住随手一试」的平衡点；
# 上限 128 是为了不让一次登录变成一次哈希 DoS（scrypt 成本随输入线性增长）。
PASSWORD_MIN = 8
PASSWORD_MAX = 128

# 认证失败的**唯一**文案。邮箱不存在、密码错、账号已删——同一句话。
# 写死成常量而不是各处手写：它一旦被改出两种写法，判据 3 就破了。
AUTH_FAILED_MESSAGE = "邮箱或密码不正确"


class AccountError(Exception):
    """账号动作的失败。`kind` 是调用方唯一该看的东西。

    kind 的取值与含义：
      · `email_taken`  → 409，这个邮箱已经注册过
      · `invalid_email` / `weak_password` / `invalid_input` → 422，用户输入不合法
      · `auth_failed`  → 401，**邮箱不存在与密码错共用它**（判据 3）
    """

    def __init__(self, kind: str, message: str = "") -> None:
        self.kind = kind
        self.message = message or {
            "email_taken": "这个邮箱已经注册过了",
            "invalid_email": "邮箱格式不正确",
            "weak_password": f"密码至少 {PASSWORD_MIN} 位，最多 {PASSWORD_MAX} 位",
            "invalid_input": "输入不合法",
            "auth_failed": AUTH_FAILED_MESSAGE,
        }.get(kind, kind)
        super().__init__(self.message)


@dataclass
class ClaimResult:
    """认领的结果。三个数字都要能被前端说成人话。"""

    user_id: str
    # 「这次核对过的源 user_id」，不是「这次动过的」。理由见 claim_anonymous_data。
    claimed_from: list[str] = field(default_factory=list)
    # 没有可搬数据的 user_id（已被另一个账号拿走、或 session 已被搬空）。
    # 与 skipped_duplicates 分开：那是「东西还在、只是重复」，这是「东西不在了」。
    # 混成一个数字，用户看不懂「为什么点了没反应」。
    considered: list[str] = field(default_factory=list)
    memories: int = 0
    diaries: int = 0
    # 被跳过的部分要说清楚，否则「我明明有 5 条记忆，怎么只过来 3 条」没人答得上来。
    skipped_duplicates: int = 0

    def as_dict(self) -> dict[str, Any]:
        return {
            "user_id": self.user_id,
            "claimed_from": list(self.claimed_from),
            "considered": list(self.considered),
            "claimed": {"memories": self.memories, "diaries": self.diaries},
            "skipped_duplicates": self.skipped_duplicates,
        }


# --------------------------------------------------------------------------- #
# 注册 / 认证
# --------------------------------------------------------------------------- #
def register(
    repo: Repository,
    email: str,
    password: str,
    display_name: str = "",
    *,
    user_id: str | None = None,
) -> Account:
    """注册。返回的 `Account.user_id` 就是这个人此后的稳定身份。

    `user_id` 可注入：测试要断言「注册出来的 id 长什么样」，
    生产永远走 `new_id("u")`。

    邮箱先归一化（小写、去空白）再落库——`A@B.com` 与 `a@b.com`
    是同一个人，否则同一个人能注册出两个账号，然后困惑为什么记忆不见了。
    """
    normalized = normalize_email(email)
    if not valid_email(normalized):
        raise AccountError("invalid_email")
    if not isinstance(password, str) or not (PASSWORD_MIN <= len(password) <= PASSWORD_MAX):
        raise AccountError("weak_password")
    name = (display_name or "").strip()[:40]

    account = repo.create_account(
        normalized,
        user_id or new_id("u"),
        hash_password(password),
        name,
    )
    if account is None:
        # 存储层的 UNIQUE 说「有」。不查一次确认——那个返回值就是唯一可信的信号。
        raise AccountError("email_taken")
    return account


def authenticate(repo: Repository, email: str, password: str) -> Account:
    """邮箱 + 密码 → Account。失败一律 `AccountError("auth_failed")`。

    两条失败路径在这里被**拉平**：账号不存在时烧一次假哈希再抛，
    与真校验走同样的 scrypt 代价。只对齐状态码与文案是不够的——
    响应用时差一个数量级，用户枚举照样能做（判据 3）。
    """
    row = repo.find_account(normalize_email(email))
    if row is None:
        dummy_verify(password or "")
        raise AccountError("auth_failed")
    if not verify_password(password or "", row["password_hash"]):
        raise AccountError("auth_failed")
    return Account(
        email=row["email"],
        user_id=row["user_id"],
        display_name=row.get("display_name") or "",
        token_epoch=int(row.get("token_epoch") or 1),
        created_at=row["created_at"],
    )


def token_for(account: Account, secret: str) -> str:
    """给账号签一个令牌。单独一个函数，是为了让「令牌里放什么」只有一处定义。"""
    return issue_token(
        account.user_id,
        secret=secret,
        email=account.email,
        epoch=account.token_epoch,
    )


def resolve_token(repo: Repository, token: str, secret: str) -> Account | None:
    """令牌 → 账号。任何问题都返回 None（调用方给 401，不是 500）。

    这里**不抛**：过期、伪造、账号已不存在，对调用方都是同一件事
    「你现在不是任何人」。抛异常只会诱导调用方写 `except Exception: 500`。
    """
    try:
        payload = decode_token(token, secret=secret)
    except TokenError:
        return None
    row = repo.find_account(str(payload.get("email") or ""))
    # 邮箱对不上（换了邮箱）时按 user_id 认：身份只认 user_id 一份。
    if row is None or row["user_id"] != payload.get("sub"):
        return None
    try:
        decode_token(token, secret=secret, expected_epoch=int(row.get("token_epoch") or 1))
    except TokenError:
        return None
    return Account(
        email=row["email"],
        user_id=row["user_id"],
        display_name=row.get("display_name") or "",
        token_epoch=int(row.get("token_epoch") or 1),
        created_at=row["created_at"],
    )


# --------------------------------------------------------------------------- #
# 认领
# --------------------------------------------------------------------------- #
def _content_key(content: str) -> str:
    """去重用的键：去掉首尾与连续空白，统一大小写不敏感。

    「我妈妈下周要去医院复查」与「我妈妈下周要去医院复查 」是同一句话；
    中间多了一个空格也是。**不做模糊匹配**（不搞编辑距离）：
    两条语义相近但确实不同的记忆（「喜欢猫」/「喜欢猫但不养」）
    合并掉会丢掉信息，那比多一条更糟。
    """
    return " ".join((content or "").split()).casefold()


def _candidate_sources(repo: Repository, persona_ids: list[str], account_user_id: str) -> list[str]:
    """该账号可能想认领的匿名 user_id：某个人格会话里出现过的、**且不是任何账号**。

    按 first-seen 顺序去重（多个 persona 会重复列出同一个匿名 id）。

    「不是任何账号」这道过滤必须在**源列表**上做，不能只在搬会话时做：
    否则另一个账号的记忆/日记仍会被搬到这里——那是最严重的一类越权。
    匿名 id 是前端随机串，与账号 id 不会重（账号 id 由注册生成），
    所以这条过滤只挡得住攻击，挡不住正常路径。
    """
    out: list[str] = []
    for pid in persona_ids:
        for uid in repo.list_users_for_persona(pid):
            if not uid or uid == account_user_id or uid in out:
                continue
            if repo.account_exists(uid):
                continue
            out.append(uid)
    return out


def _carry_sessions(
    repo: Repository,
    persona_id: str,
    source_user_id: str,
    account_user_id: str,
    result: ClaimResult,
) -> bool:
    """把某个匿名 id 在**这个人格下**的会话改挂到账号名下。返回是否动了数据。

    两道防线（都是为「认领接口不知道匿名的长什么样」这件事兜底）：

    1. `session.persona_id != persona_id` → 跳过。源 id 是前端给的字符串，
       只按 user_id 匹配就能把别人在同一 user_id 字符串下的会话搬走。
    2. 会话的 user_id 若是**另一个账号**，跳过。判断依据是「这个 user_id
       在 account 表里有行」——账号 id 只有注册时才会被创建。
       反过来说：匿名 id 都是前端 random 出来的（`u_xxxxxxxx`），不会撞上。
    """
    moved = False
    for session in repo.list_sessions(persona_id, source_user_id, limit=10_000):
        if session.persona_id != persona_id or session.user_id != source_user_id:
            continue
        if _is_account_user(repo, session.user_id):
            continue
        session.user_id = account_user_id
        repo.save_session(session)
        moved = True
    return moved


def _is_account_user(repo: Repository, user_id: str) -> bool:
    """这个 user_id 是不是某个**账号**？是的话不许被认领搬走。

    匿名 id 由前端随机生成，账号 id 由注册时生成，两者不会撞；
    但「不会撞」是概率，不是证明。所以这里显式查一次：
    已经在某个账号名下的会话，不该被另一个账号以「认领」的名义拿走。
    """
    return repo.account_exists(user_id)


def claim_anonymous_data(
    repo: Repository,
    account_user_id: str,
    persona_ids: list[str],
    source_user_ids: list[str] | None = None,
    *,
    carry_sessions: bool = True,
) -> ClaimResult:
    """把匿名 `user_id` 名下属于这些 persona 的记忆与日记迁到账号名下。

    ## 为什么必须由调用方给 persona 列表

    记忆按 `(persona_id, user_id)` 存，`user_id` 只在一个 persona 内才唯一地
    指向「一段关系」。凭空扫全库所有 persona 会把别人的人格数据搬进来
    （取到别人的记忆就是最坏的事）。所以迁移的范围必须在请求里说清楚，
    服务端只在这个范围内动**跟这个人格有会话**的匿名 id。

    ## 去重是「按内容」，不是「按 id」

    id 去重挡不住「匿名期间同一天聊了两遍同一句话」——
    那是两条不同 id、同内容的记忆。所以：
      1. 先把目标 user_id 下已有的内容读成一个集合（白名单）；
      2. 逐条判，命中就跳过（计入 `skipped_duplicates`），不命中才写。

    ## 幂等时怎么回答「我点了第二次，为什么什么都没发生」

    第一次认领成功之后，源 id 名下的记忆与日记都空了、会话也改了名字，
    所以第二次**没有可核对的对象**。这时如实回答 `skipped_duplicates: 0` +
    `considered: [...]`（我核对过这些 id，它们名下已经没有数据了），
    而不是硬编一个「跳过 N 条」——那是在骗调用方。

    真正该被 `skipped_duplicates` 统计的是「东西还在、只是内容重复」，
    例如匿名期间同一句话被记了两遍（见测试 `test_claim_dedups_by_content_not_by_id`）。

    写入用 `update_memory` 改 `user_id` 且**保留 id**：记忆的身份不该因为
    换了主人就变。改 id 会让 `memory_edit_log` 里的审计记录悬空。
    日记是例外——它必须换 id，见下面的注释。

    ## 会话：默认**跟着搬**，但只搬本 persona 内的会话

    这一点在实践中被证明是必须的，不是可选的美化：

    · 不搬会话 → 记忆搬过来了，但 `GET /v1/sessions/{sid}/memories`
      （记忆面板真正用的端点）走的是**会话**的 `(persona_id, user_id)`，
      于是新设备上打开那个会话，面板是空的——「记忆自然跟过来」只在
      `GET /v1/personas/{pid}/diary` 这类按人查的端点上成立。
    · 搬，但要挡住跨人格劫持：认领请求里的 `source_user_ids` 由前端提供，
      而 `user_id` 是前端随便写的字符串。只按 `(pid, src_uid)` 匹配就能把
      「恰好用了同一个 user_id 字符串」的另一个人的会话搬进来。
      所以这里额外要求 **会话的 persona_id 等于待改的 persona_id**，
      并且**拒绝把已经在另一个账号名下的会话搬走**。

    `persona_ids` 由调用方给、`source_user_ids` 由调方给，两者都必须显式，
    没有「扫全库」的默认路径。
    """
    if source_user_ids is None:
        sources = _candidate_sources(repo, persona_ids, account_user_id)
    else:
        # 调用方显式给了源列表：仍要过一遍「不是账号」这道筛。
        # 只信前端给的字符串是不够的——那正是攻击者能控制的东西。
        sources = [
            u
            for u in source_user_ids
            if u and u != account_user_id and not repo.account_exists(u)
        ]
    result = ClaimResult(user_id=account_user_id)

    # 自动发现候选源的整个过程中，**每一个被检查过的 id 都记下来**。
    #
    # 为什么不能等"真的搬了东西"才记 `claimed_from`：认领是一次性的——
    # 第一次搬干净之后，源 id 名下就空了，`list_users_for_persona` 再也
    # 列不出它（会话已经被改挂到账号名下）。如果那时才记，第二次认领
    # 连"我去哪里找过、什么也没找到"都说不出来：重复项统计是 0，
    # 用户看到的是"我点了两次，第二次什么都没发生"，分不清是幂等还是坏了。
    #
    # 所以语义定成「这次考虑过的源」，而不是「这次动过的源」。
    # 前者对调用方更有用：它是"我核对过这些地方"的凭据。
    for pid in persona_ids:
        if source_user_ids is None:
            for uid in repo.list_users_for_persona(pid):
                if uid and uid != account_user_id and not repo.account_exists(uid):
                    if uid not in result.claimed_from:
                        result.claimed_from.append(uid)

    for pid in persona_ids:
        # 白名单：目标名下已有的内容（每轮重读，因为上一轮迁移会往里加东西）
        seen_memories = {_content_key(n.content) for n in repo.list_memories(pid, account_user_id, limit=10_000)}
        seen_diaries = {e.date for e in repo.list_diary(pid, account_user_id, limit=10_000)}

        for src in sources:
            nodes = repo.list_memories(pid, src, limit=10_000)
            entries = repo.list_diary(pid, src, limit=10_000)
            sessions = [
                s
                for s in repo.list_sessions(pid, src, limit=10_000)
                if s.persona_id == pid and s.user_id == src
            ]
            if not (nodes or entries or sessions):
                if src not in result.considered:
                    result.considered.append(src)
                continue
            if src not in result.claimed_from:
                result.claimed_from.append(src)

            for node in nodes:
                key = _content_key(node.content)
                if key in seen_memories:
                    result.skipped_duplicates += 1
                    continue
                node.user_id = account_user_id
                repo.update_memory(node)
                seen_memories.add(key)
                result.memories += 1

            for entry in entries:
                if entry.date in seen_diaries:
                    # 一天一篇是产品语义：同一天两边都有日记时保留账号名下那份，
                    # 与 storage 层的 upsert 冲突策略保持一致（不覆盖先到的）。
                    result.skipped_duplicates += 1
                    continue
                entry.user_id = account_user_id
                # 换主不换身份：`id` 是日记的主键，而跨用户 upsert 不会改写 id
                # （storage 的冲突键是三元组），沿用原 id 就会 PK 撞车。
                # 新 id 不损失任何东西：日记的「同一篇」由 (persona, user, date) 定义。
                entry.id = new_id("d")
                repo.save_diary(entry)
                seen_diaries.add(entry.date)
                result.diaries += 1

            if carry_sessions:
                _carry_sessions(repo, pid, src, account_user_id, result)

    return result


def bearer_token(authorization: str | None) -> str | None:
    """从 `Authorization: Bearer <token>` 里取令牌；没有就 None。

    **不抛异常**：没带头是常态（判据 5：匿名玩法照旧），不是错误。
    大小写不敏感的 `bearer` 前缀：HTTP 头的大小写本就是自由的。
    """
    if not authorization:
        return None
    scheme, _, value = authorization.strip().partition(" ")
    if scheme.lower() != "bearer":
        return None
    value = value.strip()
    return value or None


__all__ = [
    "AUTH_FAILED_MESSAGE",
    "PASSWORD_MAX",
    "PASSWORD_MIN",
    "AccountError",
    "ClaimResult",
    "authenticate",
    "bearer_token",
    "claim_anonymous_data",
    "register",
    "resolve_token",
    "token_for",
]
