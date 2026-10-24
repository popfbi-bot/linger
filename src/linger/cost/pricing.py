"""单价表（M4-D）与成本估算。

**价格会变，这里是快照。** 表里的数字取自各家公开定价页的某一版，
它不是「事实」，只是一个**可覆盖的默认值**：

    LINGER_PRICES  {"openai:gpt-4o-mini": {"input": 0.15, "output": 0.6}}

单位是「每千 token 的美元」，键是 `provider:model`，也认单写 `model`。
覆盖是**合并**而不是替换——只想改一个模型的价格，不该顺手把别的全清空。

## 一条比价格更重要的规则

**未知的单价返回 `None`，绝不返回 0。**

0 会被读成「这次调用免费」，而真相是「我们不知道它花了多少」。
把「不知道」渲染成「免费」，是比缺失更糟的谎言：它会让人放心地继续烧钱，
月底账单出来才知道自己一直在看一块假看板。所以：

    · 单价未知 → cost=None，同时 seen_models 里带上 (provider, model, 那个 None)
    · 「这张表里压根没有它」和「表里有它但价格是空的」都是未知 —— 两种都 None
    · 只有真有一份写明的价格，才把数字乘出来
"""

from __future__ import annotations

import json
from typing import Any

from ..config import settings

# 每千 token 的美元单价。快照日期：2024-10（各家公开定价页）。
# 这不是「最新价格」的权威来源，改价请改这里或置 LINGER_PRICES 环境变量。
DEFAULT_PRICES: dict[str, dict[str, float]] = {
    # OpenAI
    "openai:gpt-4o": {"input": 0.0025, "output": 0.01},
    "openai:gpt-4o-mini": {"input": 0.00015, "output": 0.0006},
    "openai:gpt-4.1-mini": {"input": 0.0004, "output": 0.0016},
    # 本地 / 自建模型：算力是自己的，边际成本近似 0。这是**真的 0**（写明的价格），
    # 与「不知道所以给 None」是两件事——后者才是谎言。
    "mock:mock": {"input": 0.0, "output": 0.0},
    "local:local": {"input": 0.0, "output": 0.0},
}

Price = dict[str, float]


def _merge(base: dict[str, Price], override: dict[str, Any]) -> dict[str, Price]:
    """把覆盖表合进来。值不是 dict 的条目整条丢弃——坏配置不该 500。"""
    out = {k: dict(v) for k, v in base.items()}
    for key, value in (override or {}).items():
        if isinstance(value, dict):
            merged = dict(out.get(key, {}))
            merged.update({k: float(v) for k, v in value.items() if v is not None})
            out[key] = merged
        else:
            # 显式写 null / 写了个数字：表示「这个键的价格未知」
            out.pop(key, None)
    return out


def load_prices() -> dict[str, Price]:
    """读配置里的覆盖表，合到默认表上。

    每次都从 settings 读、不做进程内缓存：单价表是配置，不是热路径数据，
    而缓存会让「改了环境变量却看起来没生效」变成一个查半小时的谜。
    """
    raw = getattr(settings, "prices", "") or ""
    if not raw.strip():
        return {k: dict(v) for k, v in DEFAULT_PRICES.items()}
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return {k: dict(v) for k, v in DEFAULT_PRICES.items()}
    if not isinstance(parsed, dict):
        return {k: dict(v) for k, v in DEFAULT_PRICES.items()}
    return _merge(DEFAULT_PRICES, parsed)


def _lookup_key(provider: str, model: str | None) -> tuple[str, str]:
    return (provider or "unknown"), (model or provider or "unknown")


def unit_price(prices: dict[str, Price], provider: str, model: str | None) -> Price | None:
    """查单价。`provider:model` 优先，退化到单写 `model`，都没有 → None。"""
    prov, mod = _lookup_key(provider, model)
    for key in (f"{prov}:{mod}", mod):
        entry = prices.get(key)
        if entry is None:
            continue
        # 缺一半的价格算未知：只有 input 没有 output 时，硬凑一个 0
        # 会让输出侧（通常更贵的那侧）看起来免费。
        if "input" not in entry or "output" not in entry:
            return None
        return {"input": float(entry["input"]), "output": float(entry["output"])}
    return None


def estimate_cost(
    prices: dict[str, Price], provider: str, model: str | None, tokens_in: Any, tokens_out: Any
) -> dict[str, Any]:
    """一次调用的成本估算。

    返回 `{"input": x, "output": y, "total": z, "currency": "USD"}`，
    未知时三处都是 `None`（**不是 0**）。token 数未知（None）同样让整条估算变 None：
    「0 个 token」和「不知道用了多少 token」不是一个意思。
    """
    price = unit_price(prices, provider, model)
    if price is None or tokens_in is None or tokens_out is None:
        return {"input": None, "output": None, "total": None, "currency": "USD"}
    cost_in = round(tokens_in / 1000 * price["input"], 8)
    cost_out = round(tokens_out / 1000 * price["output"], 8)
    return {
        "input": cost_in,
        "output": cost_out,
        "total": round(cost_in + cost_out, 8),
        "currency": "USD",
    }


def known_models(prices: dict[str, Price], *, providers: list[str] | None = None) -> list[dict[str, Any]]:
    """单价表的可读快照，给 `/v1/costs` 与前端展示（「我按什么价在算」）。

    `providers` 非空时只列这些 provider：看板要能回答「我实付的价格是否已知」，
    而一张列着几十个模型却全是 dot 的空表，看的其实是「有没有我」这个问题。
    """
    out: list[dict[str, Any]] = []
    for key in sorted(prices):
        provider, _, model = key.partition(":")
        if not model:
            provider, model = key, key
        if providers is not None and provider not in providers:
            continue
        entry = prices[key]
        out.append(
            {
                "provider": provider,
                "model": model,
                "input_per_1k": entry.get("input"),
                "output_per_1k": entry.get("output"),
                "currency": "USD",
            }
        )
    return out
