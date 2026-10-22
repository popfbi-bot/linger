"""用 ds.txt 里的地址和令牌启动灵犀（真模型模式）。

    python examples/serve_ds.py [配置文件路径]

为什么单独一个脚本：
    `serve.py` 走环境变量，适合 CI 与脚本化；这个脚本直接读一个文件，
    主公把「地址 + 令牌」丢进去就能起服务，不用每次敲一长串 export。

ds.txt 的格式随便写，脚本会自己认（认不出来会明确报「认到了什么」）：
    1) 两行：  第一行地址，第二行令牌
    2) 键值对：BASE_URL=... / API_KEY=... / 令牌=...（大小写、下划线、中文冒号都认）
    3) 同行：  地址和令牌写在一行，用空格或逗号隔开
    4) JSON：  {"base_url": "...", "api_key": "...", "model": "..."}

认值规则：
    · 地址：找第一个像 URL 的字符串（http/https 开头）
    · 令牌：找第一个 `sk-` 开头的串；找不到就退回找最长的那行非 URL 文本
    · 模型名：文件里写了就用，没写默认 deepseek-chat

**不打印令牌**：启动时只显示「令牌已配置（长度 N）」。
"""

from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path

DEFAULT_CONFIG = Path(r"D:\BK\Music\ds.txt")

URL_RE = re.compile(r"https?://[^\s,，、\"'）)】\]]+", re.I)
KEY_RE = re.compile(r"(?:sk-|gk_)[A-Za-z0-9_\-]{8,}")
MODEL_RE = re.compile(r"deepseek[\w.\-]*|qwen[\w.\-]*|moonshot[\w.\-]*|gpt-[\w.\-]+", re.I)

# 已知别名 → 网关上的真实模型名。
# 只做「补全/纠正明显不存在的名字」；**有效的模型即便限流也不改**——
# 限流是暂时的，交给探测阶段的自动降级处理，别把主公的配置改花。
_MODEL_ALIASES: dict[str, str] = {
    # 网关上叫 deepseek-v4.1-flash-1017，缺后缀会 404 model_not_found
    "deepseek-v4.1-flash": "deepseek-v4.1-flash-1017",
    "deepseek-v4.1": "deepseek-v4.1-flash-1017",
    # 这几个名字在本网关上不存在，映射到确实存在的对话模型
    "deepseek-v4-flash": "deepseek-v4-pro",
    "deepseek-chat": "deepseek-v4-pro",
    "deepseek-reasoner": "deepseek-v4-pro",
}


def _resolve_model(name: str) -> str:
    """把常见的模型别名映射到网关上的真实名字。"""
    key = (name or "").strip().lower()
    return _MODEL_ALIASES.get(key, name)


class ConfigError(RuntimeError):
    """配置读不出来。"""


def _read_text(path: Path) -> str:
    if not path.exists():
        raise ConfigError(f"配置文件不存在：{path}")
    raw = path.read_text(encoding="utf-8", errors="replace").strip()
    if not raw:
        raise ConfigError(
            f"配置文件是空的：{path}\n"
            f"请写入两行——第一行 API 地址（如 https://api.deepseek.com/v1），第二行令牌（如 sk-xxx）"
        )
    return raw


def _from_json(raw: str) -> dict[str, str]:
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ConfigError(f"看起来像 JSON 但解析失败：{exc}") from exc
    if not isinstance(data, dict):
        raise ConfigError("JSON 顶层必须是对象")

    def pick(*names: str) -> str:
        for n in names:
            for k, v in data.items():
                if k.lower().replace("-", "_") == n:
                    return str(v).strip()
        return ""

    return {
        "base_url": pick("base_url", "url", "api_base", "endpoint", "地址", "base"),
        "api_key": pick("api_key", "key", "token", "令牌", "密钥"),
        "model": pick("model", "模型"),
    }


def _from_text(raw: str) -> dict[str, str]:
    """从自由文本里认地址 / 令牌 / 模型。"""
    out: dict[str, str] = {"base_url": "", "api_key": "", "model": ""}

    # 键值对优先：BASE_URL=xxx / 令牌：xxx
    kv = re.compile(
        r"(?im)^\s*([A-Za-z_\u4e00-\u9fff \-]{2,20}?)\s*[：:=＝]\s*(\S+)\s*$"
    )
    for key, value in kv.findall(raw):
        k = key.strip().lower().replace(" ", "_").replace("-", "_")
        if k in ("base_url", "url", "api_base", "endpoint", "地址", "接口地址", "base") and not out["base_url"]:
            out["base_url"] = value.strip()
        elif k in ("api_key", "key", "token", "令牌", "密钥", "secret") and not out["api_key"]:
            out["api_key"] = value.strip()
        elif k in ("model", "模型") and not out["model"]:
            out["model"] = value.strip()

    # 地址：第一个像 URL 的串
    if not out["base_url"]:
        m = URL_RE.search(raw)
        if m:
            out["base_url"] = m.group(0).rstrip("/")

    # 令牌：sk- 开头的串
    if not out["api_key"]:
        m = KEY_RE.search(raw)
        if m:
            out["api_key"] = m.group(0)

    # 令牌兜底：最长的非 URL 行
    if not out["api_key"]:
        candidates = [
            ln.strip()
            for ln in raw.splitlines()
            if ln.strip() and not ln.strip().startswith("#") and not URL_RE.search(ln)
        ]
        if candidates:
            out["api_key"] = max(candidates, key=len).strip()

    # 模型名：只在**非 URL** 的文本里找。
    # （否则 "https://api.deepseek.com/v1" 里的 deepseek 会被当成模型名）
    if not out["model"]:
        non_url = URL_RE.sub(" ", raw)
        m = MODEL_RE.search(non_url)
        if m:
            out["model"] = m.group(0).lower()

    return out


def load_config(path: Path) -> dict[str, str]:
    raw = _read_text(path)
    stripped = raw.strip()
    if stripped.startswith("{"):
        cfg = _from_json(stripped)
    else:
        cfg = _from_text(stripped)
        if not cfg["api_key"] or not cfg["base_url"]:
            # 再试一次 JSON 风格（可能带 BOM 或前后有杂讯）
            try:
                alt = _from_json(stripped)
                cfg = {k: cfg.get(k) or alt.get(k, "") for k in cfg}
            except ConfigError:
                pass
    if not cfg["base_url"]:
        raise ConfigError(
            f"没认出 API 地址。文件里应该有类似 https://api.deepseek.com/v1 的地址。\n"
            f"当前读到：{raw[:120]!r}"
        )
    if not cfg["api_key"]:
        raise ConfigError(
            f"没认出令牌。文件里应该有类似 sk-xxxx 的令牌。\n"
            f"当前读到：{raw[:120]!r}"
        )
    if not cfg["model"]:
        cfg["model"] = "deepseek-chat"
    cfg["model"] = _resolve_model(cfg["model"])
    return cfg


def apply_env(cfg: dict[str, str]) -> None:
    """把配置写进环境变量（linger 的 config 在 import 时读环境变量）。"""
    os.environ["LINGER_LLM"] = "openai"
    os.environ["OPENAI_BASE_URL"] = cfg["base_url"]
    os.environ["OPENAI_API_KEY"] = cfg["api_key"]
    os.environ["OPENAI_MODEL"] = cfg["model"]


def _autofix_model(cfg: dict[str, str], endpoint: str) -> str:
    """模型名不可用时，从网关的模型列表里挑一个能对话的。

    挑法：按偏好排序，逐个试，第一个通的就用它。
    偏好 deepseek-v4 系列 → qwen3.8-plus/max → glm → 其它对话模型，
    排除 embedding / rerank / asr / guard 这类非对话模型。
    """
    import httpx

    base = endpoint[: -len("/chat/completions")] if endpoint.endswith("/chat/completions") else endpoint
    try:
        with httpx.Client(timeout=20.0) as client:
            r = client.get(
                base + "/models",
                headers={"Authorization": f"Bearer {cfg['api_key']}"},
            )
            r.raise_for_status()
            items = r.json().get("data", [])
    except Exception:
        return ""

    names = [m.get("id") for m in items if isinstance(m, dict) and m.get("id")]
    if not names:
        return ""

    def rank(n: str) -> tuple:
        low = n.lower()
        if any(k in low for k in ("embed", "rerank", "asr", "guard", "agentic", "instruct")):
            return (9, n)
        if low.startswith("deepseek-v4-pro"):
            return (0, n)
        if low.startswith("deepseek-v4"):
            return (1, n)
        if "qwen3.8" in low:
            return (2, n)
        if "qwen3.7" in low or "qwen3.6" in low:
            return (3, n)
        if low.startswith("glm-5"):
            return (4, n)
        if low.startswith("minimax"):
            return (5, n)
        return (6, n)

    candidates = sorted(names, key=rank)[:3]
    for name in candidates:
        try:
            with httpx.Client(timeout=12.0) as client:
                rr = client.post(
                    endpoint,
                    headers={
                        "Authorization": f"Bearer {cfg['api_key']}",
                        "Content-Type": "application/json",
                    },
                    json={
                        "model": name,
                        "messages": [{"role": "user", "content": "hi"}],
                        "max_tokens": 16,
                    },
                )
            if rr.status_code == 200:
                return name
        except Exception:
            continue
    return ""


def main() -> int:
    path = Path(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_CONFIG
    try:
        cfg = load_config(path)
    except ConfigError as exc:
        print("配置读取失败：", exc, file=sys.stderr)
        return 2

    apply_env(cfg)

    print("=" * 60)
    print("  灵犀 Linger（真模型模式）")
    print("=" * 60)
    print(f"  配置文件：{path}")
    print(f"  接口地址：{cfg['base_url']}")
    print(f"  模型    ：{cfg['model']}")
    print(f"  令牌    ：已配置（长度 {len(cfg['api_key'])}）")
    print()

    # 先探一次连通性——不通就别起服务，省得主公对着一个死界面发呆
    try:
        import httpx

        sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
        from linger.llm.openai_compat import _chat_completions_url

        endpoint = _chat_completions_url(cfg["base_url"])
        print(f"  端点    ：{endpoint}")
        print("  正在测试接口连通性…", end=" ", flush=True)

        def _probe(model: str) -> tuple[int, str]:
            with httpx.Client(timeout=12.0) as client:
                rr = client.post(
                    endpoint,
                    headers={
                        "Authorization": f"Bearer {cfg['api_key']}",
                        "Content-Type": "application/json",
                    },
                    json={
                        "model": model,
                        "messages": [{"role": "user", "content": "hi"}],
                        "max_tokens": 16,
                    },
                )
            if rr.status_code == 200:
                return 200, ""
            return rr.status_code, rr.text[:200]

        status, detail = _probe(cfg["model"])
        if status == 200:
            print("通了 ✓")
        elif "model_not_found" in detail or "capacity_exceeded" in detail or status in (404, 429):
            # 模型名不对，或这个模型当前容量已满 —— 从网关模型列表里挑一个能用的
            reason = "模型名不存在" if "model_not_found" in detail else "模型容量已满"
            print(f"{reason}（{cfg['model']}），正在从网关模型列表里找一个…")
            fixed = _autofix_model(cfg, endpoint)
            if fixed:
                cfg["model"] = fixed
                os.environ["OPENAI_MODEL"] = fixed
                status, detail = _probe(fixed)
                print(f"  已切换到 {fixed} → " + ("通了 ✓" if status == 200 else f"仍失败 {status}"))
            else:
                print("  没找到可用的模型。")
        else:
            print(f"返回 {status}")
            print("  " + detail)
            print("  地址/令牌可能有误，服务仍会启动但回复会失败。")
    except Exception as exc:  # noqa: BLE001
        print(f"失败：{exc}")
        print("  服务仍会启动，但回复会失败。")

    print()

    # 交给 serve.py 起服务
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from serve import main as serve_main

    return serve_main()


if __name__ == "__main__":
    raise SystemExit(main())
