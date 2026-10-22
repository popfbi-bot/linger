"""情绪引擎：PAD 状态机（docs/ARCHITECTURE.md §3.2）。

三条不可省的规则：
    1. 衰减 —— 情绪会自己回到基线，不靠"重置"。
    2. 速率限制 —— 单轮变化不超过 ±0.25，这是"像人"的关键。
    3. 情绪不覆盖事实 —— 低落时也不能说错话（由编译器保证，不在此处）。
"""

from __future__ import annotations

from ..config import settings
from ..models import AffectState, Impact, PAD, Sensitivity, clamp, now_utc


def label_for(pad: PAD) -> str:
    """把 PAD 投影成可读标签（给 UI 和 prompt 用，不参与计算）。"""
    p, a, d = pad.pleasure, pad.arousal, pad.dominance
    if p > 0.3 and a > 0.5:
        return "雀跃"
    if p > 0.3:
        return "平静欣慰"
    if p < -0.3 and a > 0.5:
        return "焦躁"
    if p < -0.3:
        return "低落"
    if d < 0.3:
        return "犹豫"
    return "中性"


def step(
    prev: AffectState,
    baseline: PAD,
    impacts: list[Impact],
    sensitivity: Sensitivity | None = None,
    decay: float | None = None,
    max_delta: float | None = None,
) -> AffectState:
    """执行一次状态转移。纯函数，无 IO —— 便于单测和回放。"""
    sens = sensitivity or Sensitivity()
    decay = settings.affect_decay if decay is None else decay
    max_delta = settings.affect_max_delta if max_delta is None else max_delta

    # 1) 惯性衰减：向基线回归
    p = baseline.pleasure + decay * (prev.pad.pleasure - baseline.pleasure)
    a = baseline.arousal + decay * (prev.pad.arousal - baseline.arousal)
    d = baseline.dominance + decay * (prev.pad.dominance - baseline.dominance)

    # 2) 叠加冲击（按人格敏感度加权）
    for im in impacts:
        w = {
            "praise": sens.praise,
            "criticism": sens.criticism,
            "neglect": sens.neglect,
            "intimacy": sens.intimacy,
            "event": 1.0,
        }[im.kind]
        k = w * im.salience
        p += im.dp * k
        a += im.da * k
        d += im.dd * k

    p, a, d = clamp(p, -1, 1), clamp(a, 0, 1), clamp(d, 0, 1)

    # 3) 速率限制：单轮不允许跳变
    p = clamp(p, prev.pad.pleasure - max_delta, prev.pad.pleasure + max_delta)
    a = clamp(a, prev.pad.arousal - max_delta, prev.pad.arousal + max_delta)
    d = clamp(d, prev.pad.dominance - max_delta, prev.pad.dominance + max_delta)

    pad = PAD(pleasure=p, arousal=a, dominance=d).clamped()
    return AffectState(pad=pad, label=label_for(pad), updated_at=now_utc())


def expression_hints(state: AffectState) -> dict[str, object]:
    """情绪 → 生成约束。

    情绪必须改变可观测的输出特征，否则它只是装饰（§3.2.4）。
    """
    p, a, d = state.pad.pleasure, state.pad.arousal, state.pad.dominance

    length_scale = 1.0
    if p < -0.2:
        length_scale -= 0.35
    if p > 0.3:
        length_scale += 0.10
    if a > 0.6:
        length_scale -= 0.10

    return {
        "label": state.label,
        "length_scale": round(clamp(length_scale, 0.5, 1.3), 2),
        "ask_questions": p >= -0.2 and a >= 0.3,
        "emoji_scale": round(clamp(0.5 + 0.7 * max(p, 0) + 0.3 * (a - 0.5), 0.0, 1.5), 2),
        "allow_encouragement": p >= -0.2,
        "assertive": d > 0.6,
        "hesitant": d < 0.35,
    }


def hints_to_prompt(hints: dict[str, object], state: AffectState) -> str:
    """把情绪约束写成一段自然语言指令，交给编译器注入。"""
    lines = [f"你此刻的心情：{state.label}（不想让人担心，也不必假装没事）。"]
    if not hints["allow_encouragement"]:
        lines.append("不要说「一切都会好起来」「往好处想」这类话。此刻允许沉默、允许只说一句。")
    if hints["ask_questions"]:
        lines.append("可以问一句具体的事，别问「你还好吗」这种空问题。")
    else:
        lines.append("少提问。多听。")
    if hints["assertive"]:
        lines.append("可以给明确的判断，别一味顺从。")
    if hints["hesitant"]:
        lines.append("语气可以犹豫一点，用「要不要」「可能吧」这类说法。")
    return "\n".join(lines)
