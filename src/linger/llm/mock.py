"""Mock 模型：确定性规则生成，用于本地开发、单测与评测基线。

它**不是**产品的一部分——真实对话必须换成 openai_compat 或自建模型。
存在的意义：
    1. 不填 API key 也能跑通整条链路（骨架可运行）；
    2. 单测可复现（真实模型抽样会让断言抖动）。

生成策略刻意贴近"人"而不是"助手"：
    · 短句、少解释、先接情绪再问具体的事；
    · 情绪低时不打鸡血（有毒的积极是最容易露馅的地方）；
    · 记忆只轻提，不逐条汇报。
"""

from __future__ import annotations

import random
from collections.abc import Iterator
from typing import Any

from .base import chunks

_NEG = ("难过", "累", "崩溃", "哭", "压力", "失业", "分手", "病了", "砸了", "没戏", "失眠", "烦", "撑不住")
_POS = ("升职", "开心", "考上了", "好消息", "加薪", "通过了", "厉害")

_OPENERS_GOOD = ["诶，", "嗯……", "真的？", "……"]
_OPENERS_BAD = ["嗯……", "……", "嗯。"]


def _first_question(user: str) -> str | None:
    for line in user.replace("？", "?").split("?"):
        if line.strip():
            return line.strip()[-12:]
    return None


class MockProvider:
    name = "mock"

    def __init__(self, seed: int = 7) -> None:
        self._rng = random.Random(seed)

    def generate(
        self,
        system: str,
        messages: list[dict[str, str]],
        ctx: dict[str, Any] | None = None,
    ) -> Iterator[str]:
        ctx = ctx or {}
        user = ""
        for m in reversed(messages):
            if m.get("role") == "user":
                user = m.get("content", "")
                break

        text = self._compose(user, ctx)
        yield from chunks(text)

    # ------------------------------------------------------------------ #
    def _compose(self, user: str, ctx: dict[str, Any]) -> str:
        hints = ctx.get("hints", {})
        tic = ctx.get("tic")
        low_mood = not hints.get("allow_encouragement", True)
        short = hints.get("length_scale", 1.0) < 0.9
        mem_hint = ctx.get("memory_hint")

        neg = any(w in user for w in _NEG)
        pos = any(w in user for w in _POS)
        is_question = "?" in user or "？" in user or "吗" in user

        if neg or low_mood:
            lines = [self._rng.choice(_OPENERS_BAD) + ("先坐吧。" if not short else "嗯。")]
            if not short:
                lines.append("砸到什么程度？是答不上来那种，还是下来才想起该怎么答那种。"
                             if "面试" in user else "是哪种难受？说具体点。")
            else:
                lines.append("……我在。")
            return "\n".join(lines) if not short else "\n".join(lines)

        if pos:
            head = self._rng.choice(_OPENERS_GOOD)
            body = "那今天可以晚点睡。" if not short else "嗯，挺好。"
            return f"{head}{body}"

        if is_question:
            topic = _first_question(user) or "这个"
            base = f"这个我不太懂。你真要问，我只能说……{topic}这事，可能没那么要紧。"
            if short:
                base = "我不太懂这个。"
            return (tic + "，" if tic and self._rng.random() < 0.4 else "") + base

        # 闲聊：轻提一句记忆，不逐条汇报
        if mem_hint and ctx.get("level", 0) >= 2:
            return f"{tic + '，' if tic else ''}嗯。{mem_hint}\n后来呢？"

        if tic and self._rng.random() < 0.5:
            return f"{tic}……你说。"
        return "嗯，我在听。"
