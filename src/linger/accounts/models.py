"""账号的数据结构。

**这里没有 password 字段**：能读到 `Account` 的地方（HTTP 层、测试）
拿不到密码哈希，也就不可能不小心把它序列化进响应体。
哈希只在 `accounts.service` 与 Repository 之间传递。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone


@dataclass
class Account:
    """一个账号 = 一个稳定的 `user_id`。

    `user_id` 是**账号诞生时定下来的**，之后再也不变：所有记忆、关系、
    日记都挂在它下面。改邮箱、改密码都不许改它——那是把一个人的记忆
    搬到另一个人名下。

    邮箱一律存小写化后的值（`accounts.security.normalize_email`）：
    `A@b.com` 与 `a@b.com` 是同一个邮箱的两个写法，不是两个账号。
    """

    email: str
    user_id: str
    created_at: datetime
    # 只用来给「已登录」面板回显，不参与身份判定（身份只有 user_id 一份）。
    display_name: str = ""
    # 换密时推进：老令牌里带着这个值，一改就全部失效。
    token_epoch: int = 1
    extra: dict = field(default_factory=dict)

    def public_dict(self) -> dict:
        """对外可见的部分。**不含**哈希、盐、任何能反推密码的东西。"""
        return {
            "user_id": self.user_id,
            "email": self.email,
            "display_name": self.display_name,
            "created_at": self.created_at.astimezone(timezone.utc).isoformat(),
        }
