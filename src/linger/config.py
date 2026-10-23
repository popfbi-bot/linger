"""运行配置。所有可调参数集中在这里，避免散落在各引擎里。

dev 默认：SQLite + 哈希嵌入 + Mock LLM —— 不依赖任何外部服务即可跑通。
生产：把 LINGER_STORAGE 换成 postgres，LINGER_EMBEDDER 换成 bge，LINGER_LLM 换成 openai。
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path


def _env(key: str, default: str = "") -> str:
    return os.environ.get(key, default).strip()


def _env_float(key: str, default: float) -> float:
    """数值配置的容错解析。

    写错一个环境变量（`LINGER_LLM_TIMEOUT=20s`）不该让整个服务起不来——
    回落到默认值，产品照常能用。参考 _env 的「strip 后为空」语义。
    """
    raw = _env(key)
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError:
        return default


def _env_int(key: str, default: int) -> int:
    raw = _env(key)
    if not raw:
        return default
    try:
        return int(float(raw))
    except ValueError:
        return default


@dataclass
class Settings:
    # 存储
    storage: str = field(default_factory=lambda: _env("LINGER_STORAGE", "sqlite"))
    db_path: Path = field(
        default_factory=lambda: Path(_env("LINGER_DB_PATH", "./data/linger.db"))
    )
    # postgres 后端的连接串。只在 LINGER_STORAGE=postgres 时被用到，
    # 所以默认空串不报错——没配 PG 的人不该被一个用不上的字段拦住。
    pg_dsn: str = field(default_factory=lambda: _env("LINGER_PG_DSN"))

    # 嵌入
    embedder: str = field(default_factory=lambda: _env("LINGER_EMBEDDER", "hash"))
    embedding_dim: int = 256

    # 模型
    llm_provider: str = field(default_factory=lambda: _env("LINGER_LLM", "mock"))
    openai_base_url: str = field(
        default_factory=lambda: _env("OPENAI_BASE_URL", "https://api.openai.com/v1")
    )
    openai_api_key: str = field(default_factory=lambda: _env("OPENAI_API_KEY"))
    openai_model: str = field(default_factory=lambda: _env("OPENAI_MODEL", "gpt-4o-mini"))

    # 模型韧性（M4-A）：超时 / 重试 / 降级 / 熔断。
    # 全部有默认值——不配也能跑，配了只影响「模型坏掉时」的表现。
    llm_fallback: str = field(default_factory=lambda: _env("LINGER_LLM_FALLBACK", "mock"))
    llm_timeout: float = field(default_factory=lambda: _env_float("LINGER_LLM_TIMEOUT", 20.0))
    llm_total_timeout: float = field(
        default_factory=lambda: _env_float("LINGER_LLM_TOTAL_TIMEOUT", 120.0)
    )
    llm_retries: int = field(default_factory=lambda: _env_int("LINGER_LLM_RETRIES", 1))
    llm_breaker_threshold: int = field(
        default_factory=lambda: _env_int("LINGER_LLM_BREAKER_THRESHOLD", 5)
    )
    llm_breaker_cooldown: float = field(
        default_factory=lambda: _env_float("LINGER_LLM_BREAKER_COOLDOWN", 60.0)
    )

    # 账号（M3-H）。令牌的自签密钥。
    #
    # 不配也能跑（默认随机生成一个进程内密钥），但要付出代价：进程一重启，
    # 旧令牌全部作废——用户会莫名其妙被登出。所以**生产必须显式配**，
    # 开发/CI 用随机值即可（CI 里没有可泄露的秘密，也不做持久会话）。
    # 不写死任何默认密钥：默认值是仓库里的公开字符串，等于没有签名。
    auth_secret: str = field(default_factory=lambda: _env("LINGER_AUTH_SECRET"))
    auth_token_ttl: int = field(
        default_factory=lambda: _env_int("LINGER_AUTH_TOKEN_TTL", 30 * 24 * 3600)
    )

    # 上下文预算（§3.6.2）
    context_budget_tokens: int = 8000

    # 记忆检索（§3.3.2 ④）
    recall_k: int = 8
    recall_gate: float = 0.35
    recency_tau_days: float = 30.0

    # 情绪（§3.2.2）
    affect_decay: float = 0.85
    affect_max_delta: float = 0.25

    # 人格自检（§3.1.4 防线2）
    self_reminder_every: int = 8


settings = Settings()
