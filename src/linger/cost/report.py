"""成本聚合（M4-D）：**只在查端点时**算（判据 5）。

写路径是 O(1) 单条 INSERT；这里是唯一做 SUM / GROUP BY 的地方。
这条分工是判据 5 的全部内容：对话关键路径上跑全表聚合，延迟会随历史
记录线性增长，而看板一天被打开几十次，对话一分钟就有几十轮。

## 「未知」不是 0

每个成本字段都可能是 `None`，含义是**这张表里没有它的单价**。
聚合时这一条规则被贯彻到底：

    · 某组里有任何一次调用的 token 数未知 → 该组的成本 **None**
      （用已知部分算个和，出来的数是"少算了"的假数字）
    · 某组里的模型没有单价 → 该组的成本 **None**，并把模型列进 `unknown_models`
    · 全部已知 → 才给数字

所以前端的渲染规则只有一条：`null` 显示「未知」，`0` 显示 `$0`。
这两者在屏幕上必须长得不一样，否则看板就是在骗人。
"""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timedelta, timezone
from typing import Any

from .pricing import estimate_cost, known_models, load_prices, unit_price

MAX_DAYS = 365


def _as_utc(v: datetime) -> datetime:
    return v if v.tzinfo is not None else v.replace(tzinfo=timezone.utc)


def window_start(days: int, now: datetime | None = None) -> datetime:
    """`days` 天的窗口起点。

    口径是「含今天在内的 N 个自然日」，从第 N-1 天零点算起：
    `days=1` 就是今天从 00:00 起。用「往前推 N×24 小时」会让「今日」
    在早上 9 点时只算到昨天 9 点——那不是用户说的「今日」。
    """
    now = _as_utc(now or datetime.now(timezone.utc))
    first_day = now.date() - timedelta(days=max(1, days) - 1)
    return datetime.combine(first_day, datetime.min.time(), tzinfo=timezone.utc)


def _bucket() -> dict[str, Any]:
    return {
        "calls": 0,
        "failed": 0,
        "tokens_in": 0,
        "tokens_out": 0,
        "tokens_total": 0,
        "elapsed_ms": 0,
        "_unknown_tokens": False,
        "_cost_in": 0.0,
        "_cost_out": 0.0,
        "_unknown_cost": False,
        "_models": set(),
    }


def _add(bucket: dict[str, Any], record: Any, prices: dict[str, Any]) -> None:
    bucket["calls"] += 1
    if not record.ok:
        bucket["failed"] += 1
    bucket["elapsed_ms"] += int(record.elapsed_ms or 0)
    bucket["_models"].add((record.provider, record.model))

    if record.tokens_in is None or record.tokens_out is None:
        bucket["_unknown_tokens"] = True
    else:
        bucket["tokens_in"] += int(record.tokens_in)
        bucket["tokens_out"] += int(record.tokens_out)
        bucket["tokens_total"] += int(record.tokens_in) + int(record.tokens_out)

    cost = estimate_cost(
        prices, record.provider, record.model, record.tokens_in, record.tokens_out
    )
    if cost["total"] is None:
        bucket["_unknown_cost"] = True
    else:
        bucket["_cost_in"] += cost["input"]
        bucket["_cost_out"] += cost["output"]


def _finish(bucket: dict[str, Any], prices: dict[str, Any]) -> dict[str, Any]:
    calls = bucket["calls"]
    unknown_models = [
        {"provider": p, "model": m}
        for p, m in sorted(bucket["_models"])
        if unit_price(prices, p, m) is None
    ]
    # 「未知」的**唯一真相**就在这一句：知道成本、且每一次的单价都知道，
    # 才给数字；否则 None。不在 `_add` 里就把钱加成 0.0——那样两处判断
    # 迟早会漂移，而「漂移成 0」正好是最坏的那种漂。
    cost = None if bucket["_unknown_cost"] else round(bucket["_cost_in"] + bucket["_cost_out"], 8)
    return {
        "calls": calls,
        "failed": bucket["failed"],
        # 失败率的分母是**全部**调用，不是成功调用：重试风暴下前者才说明问题。
        "failure_rate": round(bucket["failed"] / calls, 4) if calls else 0.0,
        "tokens_in": None if bucket["_unknown_tokens"] else bucket["tokens_in"],
        "tokens_out": None if bucket["_unknown_tokens"] else bucket["tokens_out"],
        "tokens_total": None if bucket["_unknown_tokens"] else bucket["tokens_total"],
        "elapsed_ms": bucket["elapsed_ms"],
        # 这三项一起看才有意义：有成本却不知道成本属于哪一半，是没法做决策的。
        "cost": cost,
        "cost_input": None if bucket["_unknown_cost"] else round(bucket["_cost_in"], 8),
        "cost_output": None if bucket["_unknown_cost"] else round(bucket["_cost_out"], 8),
        "currency": "USD",
        "unknown_models": unknown_models,
    }


def _totals(bucket: dict[str, Any], prices: dict[str, Any], *, calls: int) -> dict[str, Any]:
    """窗口总计。

    一个例外要单独说：**窗口里一次调用都没有时，成本是 `None` 而不是 0**。
    这不是「花了 0 元」，而是「这段时间没有可算的账」——刚部署的服务、
    mock 空跑的开发环境都是这个状态。屏幕上 `$0.00` 和「还没有数据」
    必须长得不一样，否则第一眼看到的就是一句没根据的「很省钱」。
    """
    out = _finish(bucket, prices)
    if calls == 0:
        out.update({"cost": None, "cost_input": None, "cost_output": None})
    return out


def build_report(
    records: list[Any],
    *,
    days: int = 7,
    now: datetime | None = None,
    prices: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """把明细聚合成 `/v1/costs` 的响应。

    两个维度（判据 2）：`by_provider` 与 `by_day`。额外给 `by_purpose`——
    陪聊和后台蒸馏烧的是同一笔钱，但要查的地方完全不同，混在一起看不出来。

    `series`（每日 token 与失败率）是判据 6 的前端口径。**它按窗口内每个自然日
    补齐**，包括没有调用的那天（0 调用）。不补齐的话，前端画出来的「近 7 日」
    会因为周末没聊天而少几天，看起来像服务挂了。
    """
    days = max(1, min(MAX_DAYS, int(days)))
    now_utc = _as_utc(now or datetime.now(timezone.utc))
    start = window_start(days, now_utc)
    prices = load_prices() if prices is None else prices

    in_window = [r for r in records if _as_utc(r.created_at) >= start]

    total = _bucket()
    providers: dict[str, dict[str, Any]] = defaultdict(_bucket)
    purposes: dict[str, dict[str, Any]] = defaultdict(_bucket)
    by_day: dict[str, dict[str, Any]] = defaultdict(_bucket)

    for r in in_window:
        day = _as_utc(r.created_at).date().isoformat()
        _add(total, r, prices)
        _add(providers[str(r.provider)], r, prices)
        _add(purposes[str(r.purpose)], r, prices)
        _add(by_day[day], r, prices)

    series = []
    for offset in range(days):
        day = (start.date() + timedelta(days=offset)).isoformat()
        raw = by_day.get(day)
        row = _finish(raw or _bucket(), prices)
        calls = row["calls"]
        series.append(
            {
                "date": day,
                "calls": calls,
                # 没调用的一天：成本未知（None），不是「免费」。tokens 同理。
                "failed": row["failed"],
                "failure_rate": row["failure_rate"],
                "tokens_total": None if calls == 0 else row["tokens_total"],
                "cost": None if calls == 0 else row["cost"],
            }
        )

    used_providers = sorted(providers)
    return {
        "window": {
            "days": days,
            "since": start.isoformat(),
            "until": now_utc.isoformat(),
            "currency": "USD",
        },
        "totals": _totals(total, prices, calls=len(in_window)),
        "by_provider": [
            {"provider": name, **_finish(bucket, prices)}
            for name, bucket in sorted(providers.items())
        ],
        "by_day": [
            {"date": day, **_finish(bucket, prices)} for day, bucket in sorted(by_day.items())
        ],
        "by_purpose": [
            {"purpose": name, **_finish(bucket, prices)}
            for name, bucket in sorted(purposes.items())
        ],
        "series": series,
        # 「今日」与「近 7 日」是判据 6 的前端要显示的那两个数，
        # 后端先算好：让每个前端各算一遍，就是让它们有机会算出不同的结果。
        "today": series[-1] if series else None,
        "last_7_days": _totals(
            _merge_days(by_day, start, days), prices, calls=len(in_window)
        )
        if days >= 7
        else None,
        # 单价快照。前端据此说明「我按什么价在算」，也让「未知」有据可查。
        "pricing": known_models(prices, providers=used_providers or None),
        "pricing_snapshot": "2024-10",
    }


def _merge_days(
    by_day: dict[str, dict[str, Any]], start: datetime, days: int
) -> dict[str, Any]:
    """近 7 日（或窗口最后 7 天）的合并桶。"""
    merged = _bucket()
    first = max(0, days - 7)
    for offset in range(first, days):
        day = (start.date() + timedelta(days=offset)).isoformat()
        bucket = by_day.get(day)
        if not bucket:
            continue
        for key in ("calls", "failed", "tokens_in", "tokens_out", "tokens_total", "elapsed_ms"):
            merged[key] += bucket[key]
        for key in ("_cost_in", "_cost_out"):
            merged[key] += bucket[key]
        merged["_unknown_tokens"] = merged["_unknown_tokens"] or bucket["_unknown_tokens"]
        merged["_unknown_cost"] = merged["_unknown_cost"] or bucket["_unknown_cost"]
        merged["_models"] |= bucket["_models"]
    return merged
