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


@dataclass
class Settings:
    # 存储
    storage: str = field(default_factory=lambda: _env("LINGER_STORAGE", "sqlite"))
    db_path: Path = field(
        default_factory=lambda: Path(_env("LINGER_DB_PATH", "./data/linger.db"))
    )

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
