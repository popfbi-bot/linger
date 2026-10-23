"""密码与令牌：只用标准库，自己签。

## 密码

`hashlib.scrypt`（PG 自带，标准库也有）带随机盐慢哈希，存成
`scrypt$n$r$p$<salt_b64>$<hash_b64>` 一种自描述字符串：

- 参数写进哈希串，以后调大 `n` 也不会让老密码验不过——
  验证时按**这一条自己的参数**重算，而不是按当前默认值；
- 盐每条独立随机（16 字节），两个用户用同一个密码，库里两行也不一样；
- `hmac.compare_digest` 比较，不给计时侧信道。

为什么不用 bcrypt/argon2：issue 明确不许引；而 scrypt 在
「内存硬 + 参数可调」这两点上并不比它们差，Python 标准库自带。

## 令牌

自签，不是 JWT：`base64url(payload_json) + "." + base64url(HMAC-SHA256)`。
payload 里带 `sub`（user_id）、`email`、`epoch`（换密作废）、`exp`。

防篡改靠 HMAC 的密钥只有服务端知道（`LINGER_AUTH_SECRET`）；
校验用 `compare_digest`，先重算再比，不先查库——
**伪造令牌在碰数据库之前就被拒掉**（判据 4：伪造/过期 → 401，不是 500）。

刻意不叫 JWT，也不实现 `alg` 协商：`alg: none` 这类降级攻击
在这套实现里没有立锥之地，因为算法根本没得选。
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import time
from typing import Any

# ---- 参数 ---------------------------------------------------------------- #
# scrypt 成本参数。n=2**14 / r=8 / p=1 是 RFC 7914 给交互式登录的推荐档，
# 单次约几十毫秒——够挡住离线爆破，又不至于让登录变成惩罚。
SCRYPT_N = 2**14
SCRYPT_R = 8
SCRYPT_P = 1
SCRYPT_MAXMEM = 64 * 1024 * 1024
SALT_BYTES = 16
KEY_LEN = 32
SCRYPT_PREFIX = "scrypt"

# 令牌有效期：30 天。陪伴产品的使用节奏是「隔几天想起来聊一次」，
# 一天就过期的令牌等于每次都要重新登录，那比没有账号更烦人。
TOKEN_TTL_SECONDS = 30 * 24 * 3600


class TokenError(Exception):
    """令牌不可用：格式坏、签名不对、过期、缺字段。**一条都不用区分**。

    区分它们只会给攻击者送情报；HTTP 层统一 401。
    """


def _b64e(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _b64d(text: str) -> bytes:
    pad = "=" * (-len(text) % 4)
    return base64.urlsafe_b64decode(text + pad)


# --------------------------------------------------------------------------- #
# 邮箱
# --------------------------------------------------------------------------- #
def normalize_email(email: str) -> str:
    """大小写与首尾空白不算身份：`A@B.com ` 与 `a@b.com` 是同一个账号。

    只做这一件事，**不猜**（不去掉 Gmail 的点、不拆 `+tag`）：
    那些是某一家服务商的规则，套到别人身上会把两个真账号合并成一个。
    """
    return (email or "").strip().lower()


def valid_email(email: str) -> bool:
    """粗校验，只挡明显不是邮箱的输入。

    完整的 RFC 5322 正则既长又会误杀（合法但少见的地址），
    而且这一层挡不住的（能收信吗）本来也不该在这里挡——
    本棒没有邮箱验证，所以这里只保证「像邮箱」。
    """
    e = normalize_email(email)
    if len(e) < 3 or len(e) > 254 or e.count("@") != 1:
        return False
    local, _, domain = e.partition("@")
    if not local or not domain or "." not in domain:
        return False
    return not any(c.isspace() for c in e) and not domain.startswith(".") and not domain.endswith(".")


# --------------------------------------------------------------------------- #
# 密码
# --------------------------------------------------------------------------- #
def hash_password(password: str, *, salt: bytes | None = None) -> str:
    """→ `scrypt$n$r$p$salt_b64$hash_b64`。盐不给就现生成。"""
    if not password:
        raise ValueError("密码不能为空")
    salt = salt or secrets.token_bytes(SALT_BYTES)
    key = hashlib.scrypt(
        password.encode("utf-8"),
        salt=salt,
        n=SCRYPT_N,
        r=SCRYPT_R,
        p=SCRYPT_P,
        maxmem=SCRYPT_MAXMEM,
        dklen=KEY_LEN,
    )
    return (
        f"{SCRYPT_PREFIX}${SCRYPT_N}${SCRYPT_R}${SCRYPT_P}"
        f"${_b64e(salt)}${_b64e(key)}"
    )


def _parse_stored(stored: str) -> tuple[int, int, int, bytes, bytes] | None:
    parts = (stored or "").split("$")
    if len(parts) != 6 or parts[0] != SCRYPT_PREFIX:
        return None
    try:
        n, r, p = int(parts[1]), int(parts[2]), int(parts[3])
        return n, r, p, _b64d(parts[4]), _b64d(parts[5])
    except (ValueError, TypeError):
        return None


# 假哈希：邮箱不存在时也跑一遍 scrypt。
#
# 为什么必须有它：如果「邮箱不存在」直接 401 返回，而「密码错」要先算
# 一次 scrypt（几十毫秒）再 401，两条路径的**响应时间**差一个数量级——
# 状态码一样、文案一样，用户枚举照样能从计时上做出来（判据 3 的用意）。
# 所以不存在的邮箱也拿这条假哈希烧掉同样的时间。
_DUMMY_HASH = hash_password("linger-dummy-password-for-timing", salt=b"\x00" * SALT_BYTES)


def dummy_verify(password: str) -> None:
    """恒定时间补偿：结果一定是 False，但代价与真校验一致。"""
    verify_password(password, _DUMMY_HASH)


def verify_password(password: str, stored: str) -> bool:
    """校验密码。哈希串坏掉时返回 False（不抛）——坏数据不该变成 500。"""
    parsed = _parse_stored(stored)
    if parsed is None:
        return False
    n, r, p, salt, expected = parsed
    try:
        key = hashlib.scrypt(
            (password or "").encode("utf-8"),
            salt=salt,
            n=n,
            r=r,
            p=p,
            maxmem=SCRYPT_MAXMEM,
            dklen=len(expected),
        )
    except (ValueError, MemoryError):
        return False
    return hmac.compare_digest(key, expected)


# --------------------------------------------------------------------------- #
# 令牌
# --------------------------------------------------------------------------- #
def _sign(secret: str, body: str) -> str:
    mac = hmac.new(secret.encode("utf-8"), body.encode("ascii"), hashlib.sha256).digest()
    return _b64e(mac)


def issue_token(
    user_id: str,
    *,
    secret: str,
    email: str = "",
    epoch: int = 1,
    ttl: int = TOKEN_TTL_SECONDS,
    now: int | None = None,
) -> str:
    """签发令牌。`now` 可注入，测试才能造一个「已经过期」的令牌。"""
    issued = int(time.time()) if now is None else int(now)
    payload = {
        "sub": user_id,
        "email": email,
        "epoch": int(epoch),
        "iat": issued,
        "exp": issued + int(ttl),
    }
    body = _b64e(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    )
    return f"{body}.{_sign(secret, body)}"


def decode_token(
    token: str,
    *,
    secret: str,
    expected_epoch: int | None = None,
    now: int | None = None,
) -> dict[str, Any]:
    """校验并解出 payload。任何不对都抛 `TokenError`（不抛别的）。

    顺序是刻意的：**先验签名，再看内容**。签名不过就没必要看 exp——
    一个攻击者塞进来的 `exp` 不值得被解析。
    """
    if not token or not isinstance(token, str):
        raise TokenError("缺少令牌")
    parts = token.split(".")
    if len(parts) != 2:
        raise TokenError("令牌格式不正确")
    body, sig = parts
    if not hmac.compare_digest(_sign(secret, body), sig):
        raise TokenError("令牌签名无效")
    try:
        payload = json.loads(_b64d(body).decode("utf-8"))
    except (ValueError, TypeError, UnicodeDecodeError) as exc:
        raise TokenError("令牌内容不可解析") from exc
    if not isinstance(payload, dict) or not payload.get("sub"):
        raise TokenError("令牌内容不完整")
    current = int(time.time()) if now is None else int(now)
    exp = payload.get("exp")
    if not isinstance(exp, int) or current >= exp:
        raise TokenError("令牌已过期")
    if expected_epoch is not None and int(payload.get("epoch") or 0) != int(expected_epoch):
        # 换过密码：旧令牌一律作废（拿到老令牌的人不该继续有访问权）
        raise TokenError("令牌已失效")
    return payload
