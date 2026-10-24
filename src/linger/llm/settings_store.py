"""模型凭据的本地存储（M4 收口后补：让用户不用改环境变量就能换真模型）。

## 为什么要有这个文件

之前换真模型只有一条路：改环境变量再重启。对一个「下载下来就能用」的产品，
这条路的摩擦太大——用户得知道 `LINGER_LLM=openai` 这个词，还得会设环境变量。

所以加一个界面入口。但**存密钥这件事必须说清楚**，否则就是在偷偷埋雷：

1. **存在哪**：`<db_path 同目录>/llm_settings.json`，默认 `./data/llm_settings.json`。
   `data/` 已在 `.gitignore` 里（第 15 行 `/data/`），**不会进版本库**。
2. **明文还是加密**：**明文**。这不是偷懒——加密需要一把主密钥，而主密钥要么存在
   同一台机器的另一个文件里（等于把钥匙和锁放一起），要么让用户每次启动输一遍
   （那还不如设环境变量）。**本地单机产品的诚实做法是明文 + 严格的文件权限**，
   并且在界面上告诉用户别把这个文件分享出去。
3. **文件权限**：尽力设成 `0600`（Windows 上 `chmod` 只影响只读位，做不到就退化为
   默认 ACL，但仍落在用户自己的 profile 目录下）。
4. **永不回显**：`GET` 只返回「配了没」与末四位提示。界面拿不到完整 key，
   浏览器的 localStorage 里也不会有。

## 谁能改

只有**环回地址**发起的请求能改（见 `app.py` 的 `_require_local`）。默认服务就监听
`127.0.0.1`；但如果有人用 `--host 0.0.0.0` 把它挂到局域网上，那这个端点就变成了
「任何人都能往里塞自己的 key」或者「任何人都能改你的模型」——所以那道检查是必须的，
不能只靠「默认配置是安全的」。
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

FILE_NAME = "llm_settings.json"


@dataclass
class LLMSettings:
    """用户填的那几项。`api_key` 为空表示「没配」。"""

    provider: str = ""
    base_url: str = ""
    model: str = ""
    api_key: str = ""

    def masked(self) -> dict[str, Any]:
        """给界面看的形态：**不含完整 key**。"""
        key = (self.api_key or "").strip()
        return {
            "provider": self.provider,
            "base_url": self.base_url,
            "model": self.model,
            "api_key_set": bool(key),
            "api_key_hint": _mask(key) if key else "",
        }


def _mask(key: str) -> str:
    """`sk-1234…abcd` 这种。太短的 key 一律只给末 2 位，避免「提示」本身泄露。"""
    k = key.strip()
    if len(k) <= 8:
        return "…" + k[-2:] if len(k) > 2 else "…"
    return f"{k[:4]}…{k[-4:]}"


def settings_path() -> Path:
    """与数据库同目录——一个产品的一个数据目录，别分散。"""
    from ..config import settings

    base = Path(settings.db_path).expanduser().resolve().parent
    return base / FILE_NAME


def load() -> LLMSettings:
    """读文件。坏文件不炸：当成「没配」，而不是让服务起不来。"""
    p = settings_path()
    if not p.exists():
        return LLMSettings()
    try:
        raw = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return LLMSettings()
    if not isinstance(raw, dict):
        return LLMSettings()
    return LLMSettings(
        provider=str(raw.get("provider") or "").strip(),
        base_url=str(raw.get("base_url") or "").strip(),
        model=str(raw.get("model") or "").strip(),
        api_key=str(raw.get("api_key") or "").strip(),
    )


def save(cfg: LLMSettings) -> Path:
    """写文件并尽力收紧权限。

    用 `newline="\\n"` 显式钉住行尾——这个仓库在 Windows 上吃过 CRLF 的亏，
    数据文件虽然不怕，但规矩一致比「这次恰好没事」可靠。
    """
    p = settings_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "provider": cfg.provider,
        "base_url": cfg.base_url,
        "model": cfg.model,
        "api_key": cfg.api_key,
    }
    with open(p, "w", encoding="utf-8", newline="\n") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
        f.write("\n")
    try:
        os.chmod(p, 0o600)
    except OSError:  # Windows 上可能不支持；不影响功能，仍然落在用户自己的目录
        pass
    return p


def clear() -> None:
    """删文件（等于回到环境变量的配置）。"""
    try:
        settings_path().unlink()
    except FileNotFoundError:
        pass


def apply_to_settings() -> LLMSettings:
    """把文件里的值覆盖到全局 `settings` 上，返回实际生效的配置。

    调用点有两个：进程启动时（lifespan）和用户保存后。**文件优先于环境变量**
    ——界面里显式填的东西应该赢过 shell 里的残留，否则用户改了界面却没生效会更困惑。
    """
    from ..config import settings

    cfg = load()
    if cfg.provider:
        settings.llm_provider = cfg.provider
    if cfg.base_url:
        settings.openai_base_url = cfg.base_url
    if cfg.model:
        settings.openai_model = cfg.model
    if cfg.api_key:
        settings.openai_api_key = cfg.api_key
    return cfg


def effective() -> dict[str, Any]:
    """当前**实际生效**的配置（合并了文件与环境变量），供界面展示。"""
    from ..config import settings

    return {
        "provider": settings.llm_provider,
        "base_url": settings.openai_base_url,
        "model": settings.openai_model,
        "api_key_set": bool((settings.openai_api_key or "").strip()),
        "api_key_hint": _mask(settings.openai_api_key) if settings.openai_api_key else "",
    }
