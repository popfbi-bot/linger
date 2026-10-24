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

    # 事件评估（#38）：判断「这一轮算什么、走不走心」。
    # `mock`/`rule` = 纯规则，**零网络**（默认，CI 与免密钥环境走这条）；
    # `openai` 等 = 按名字造 provider；`same` = 跟对话用同一个。
    # 刻意与 `LLM` 分开：判 bool 的小模型不该跟对话抢配额，也不该共用熔断状态。
    eval_provider: str = field(default_factory=lambda: _env("LINGER_EVAL_LLM", "mock"))
    eval_timeout: float = field(default_factory=lambda: _env_float("LINGER_EVAL_TIMEOUT", 3.0))

    # 语音（M4-C）。听：`LINGER_ASR`；说：`LINGER_TTS`。
    #
    # 默认都是 mock——不配任何东西就能跑通完整链路（含情绪韵律），CI 也不联网。
    # 真实 provider 目前只有配置位，没有实现：`get_asr()/get_tts()` 见到不认识
    # 或未接入的名字会**降级到 mock 并记录下来**（`/v1/voice/health` 能看到），
    # 而不是崩掉——配错一个环境变量不该让整个服务起不来。
    asr_provider: str = field(default_factory=lambda: _env("LINGER_ASR", "mock"))
    tts_provider: str = field(default_factory=lambda: _env("LINGER_TTS", "mock"))
    # 单次上传音频的上限（字节）。8MB ≈ 一分钟 16bit/44.1kHz 单声道 WAV，
    # 也远小于任何反向代理的默认 body 上限——卡在应用层是为了给出 413 而不是连接被切断。
    voice_max_audio_bytes: int = field(
        default_factory=lambda: _env_int("LINGER_VOICE_MAX_AUDIO_BYTES", 8 * 1024 * 1024)
    )

    # 成本看板（M4-D）的单价覆盖表。JSON 字符串，键是 `provider:model`（或单写 `model`），
    # 值是 `{"input": 每千 token 美元, "output": ...}`。**合并**到内置快照上，
    # 不是替换——只想改一个模型的价，不该顺手把别的全清空。
    # 名字叫 PRICES 而不是 COST_PRICES：环境变量里 `LINGER_COST_*` 留给
    # 「看板开关」这类将来的东西，价格本身只需要一个入口。
    prices: str = field(default_factory=lambda: _env("LINGER_PRICES"))

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

    # 人格市场的审核员名单（M4-B）不在这里。
    #
    # 「谁是审核员」是部署事实，而 `settings` 是 import 时求值一次的**快照**：
    # 进程起来之后再换班（或测试里 monkeypatch），它不会跟着变。
    # 读一个过期的空名单等于**审核权静默消失**——那是安全判定不能有的失败形态。
    # 所以名单由 `market.service.reviewer_emails()` 每次现读
    # `LINGER_MARKET_REVIEWER_EMAILS`，不经过这里。

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
