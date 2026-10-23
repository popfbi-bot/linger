"""账号体系（M3-H）：注册 / 登录 / 认领 / 令牌。

三件事，三个文件，职责不混：

- `security.py` —— 密码与令牌的密码学。只用标准库：
  密码走 `hashlib.scrypt`（带盐慢哈希），令牌走 HMAC-SHA256 自签。
  刻意不引 JWT 库 / bcrypt / argon2：这一层要能在一个
  `python:3.11-slim` 里零依赖跑起来，而标准库足够。
- `models.py` —— `Account` 这个纯数据结构。
- `service.py` —— 把上面两件东西接到 Repository 上：注册、认证、
  认领匿名数据、跨用户找回。

**设计前提**（issue #33）：账号的 `user_id` 就是这个人的稳定 id。
所有状态本来就按 `(persona_id, user_id)` 存，所以换设备登录 =
同一个 `user_id` = 记忆跟着走。这条前提省掉了给 26 个 repo 方法
加 `account_id` 参数、以及 SQLite + PG 双份 SQL 迁移的改动面。
"""

# 包入口**只导出不依赖 storage 的东西**。
#
# `accounts.service` 依赖 `storage.base.Repository`，而 `storage.base` 又需要
# `Account`（账号是仓储接口的一部分）。如果在包的 __init__ 里直接 import service，
# 循环就成立了：storage.base → accounts(__init__) → service → storage.base。
# 解法是让 `accounts.models` 成为**叶子**：不能从包的 __init__ 直达它，
# 于是 `storage.base` 一律写 `from ..accounts.models import Account`。
# 除 models / security 之外的东西，用 `from linger.accounts.service import ...` 显式取。
from .models import Account
from .security import (
    TokenError,
    decode_token,
    hash_password,
    issue_token,
    normalize_email,
    valid_email,
    verify_password,
)

__all__ = [
    "Account",
    "TokenError",
    "decode_token",
    "hash_password",
    "issue_token",
    "normalize_email",
    "valid_email",
    "verify_password",
]
