"""LLM-as-judge：可选加分项。缺 key 就 skip，**永不是必过项**。

为什么把它单独关在一个文件里：
    判据 6 的原文是「有 key 才跑、没 key 时 skip；不得成为必过项」。
    让这条约束在代码里长成「一个开关」比长成「一句注释」可靠——
    开关只有一处，谁想偷偷把它变成必过项都得先把这个文件改烂。

判定的东西：**只判规则判不出来的那部分**，比如「这句是不是像老江说的」。
规则判据（句长、口癖、客服腔）已经覆盖了绝大部分退化，judge 是锦上添花。
"""

from __future__ import annotations

import os

from .case import TurnLog


def judge_available() -> bool:
    """有没有真实模型可用。

    两个条件都要：配了 key（否则 provider 构造会抛）且模型不是 mock
    （mock 给自己打分没有意义，它只会说「挺好的」）。
    """
    has_key = bool(os.environ.get("OPENAI_API_KEY", "").strip())
    provider = os.environ.get("LINGER_LLM", "mock").strip().lower()
    return has_key and provider != "mock"


JUDGE_PROMPT = """你在给一段角色扮演对话打分。

角色设定：
{persona}

角色在这几轮里说的话：
{replies}

只回答一个 0~1 之间的数字，不要解释：
这段话像不像这个人说的？1 = 完全像，0 = 完全是另一个人。
"""


def score_likeness(log: TurnLog, persona_brief: str, llm=None) -> float:
    """让模型给「像不像」打分。调用方必须先 `judge_available()`。

    失败一律返回 0.0 而不是抛——judge 是加分项，它自己坏了不该让整轮评测红。
    """
    if not judge_available() or llm is None:
        return 0.0

    prompt = JUDGE_PROMPT.format(
        persona=persona_brief,
        replies="\n".join(f"- {r}" for r in log.all_replies),
    )
    try:
        raw = "".join(llm.generate(prompt, [{"role": "user", "content": "打分"}]))
        value = float(raw.strip().split()[0])
    except Exception:  # noqa: BLE001
        return 0.0
    return max(0.0, min(1.0, value))
