"""日记生成与「流水账」检测（docs/ARCHITECTURE.md §3.3.2 第 6 步 / §10 失败模式 17）。

这个模块只做三件事：
    1. 用**已有数据**（当天 turn、记忆、情绪标签）拼一个 prompt；
    2. 把模型输出收成一篇 `DiaryEntry`；
    3. 用确定性规则查一遍「这像不像流水账」，命中就给 warning。

为什么第 3 步不用模型自评：模型对自己写的东西几乎总是满意。
「今天用户说了 A，我说了 B」这种句式是**可数的**——数句式比问模型可靠，
也便宜、可复现。命中不阻断生成：日记照出，但风险必须交给调用方（前端）看见。
"""

from __future__ import annotations

import re
from datetime import date as date_cls
from typing import Any

from ..models import PersonaCard, Turn, now_utc
from ..models import DiaryEntry as DiaryModel

# mock provider 靠这个标记识别「这是日记请求」。
# 真实模型忽略它（它只是 system prompt 第一行）。
DIARY_MARKER = "[LINGER_DIARY]"

# 目标 200 字；120~350 是最宽容的合格区间。
TARGET_LEN = 200
SOFT_MIN, SOFT_MAX = 120, 350
# 硬下限：短于此已经不是「一篇日记」，是没写。
HARD_MIN = 60
# 上游给多少正文就截到这里：一眼能看到「她今天怎么想」，又不至于糊满右栏。
HARD_MAX = 400

# 喂给模型的上下文上限（不是 token 预算，是「别把整晚聊天全塞进去」的朴素上限）
_MAX_TURNS = 40
_MAX_TURN_CHARS = 120
_MAX_MEMORIES = 6

# 流水账特征（判据 12）
_FLOW_WORDS = ("然后", "接着", "首先")
_THIRD_PERSON = "用户"


class DiaryGenerateError(ValueError):
    """今天没有可写的对话（或模型什么都没写）。HTTP 层映射为 422。

    刻意不生成一篇「今天什么都没发生」：那是**编**日记，
    比不给更糟——用户会以为她真的记得一个空白的日子。
    """


# --------------------------------------------------------------------------- #
# 确定性质量检查
# --------------------------------------------------------------------------- #
def _flow_check(text: str) -> list[str]:
    """检查文本是否有「流水账」特征，返回 warnings（空 = 干净）。

    四条判据都只看文本本身，不问模型：
      · 转述句式   —— 把日记写成了会议纪要
      · 流程连接词 —— 「然后 / 接着 / 首先」出现 ≥2 次，就是时间线复述
      · 第三人称   —— 「用户」是产品文档里的词，不是她会用的词
      · 过短       —— 长度 < 60，已经不是一篇日记

    **转述句式的判定要小心误伤**（验收时踩到的坑）：
      · 「用户说」单独出现就报——第三人称 + 转述，日记里不该有；
      · 「他说」/「我说」**单独**出现不报——
        「他说面试砸了的时候，我心里一沉」是很自然的回忆叙述，
        比硬回避第三人称更好，不该被当成流水账；
      · 只有「他说」与「我说」**同时**出现才是对话摘要（他说 A、我说 B）。

    只读不改：命中也不阻断生成。日记的价值在「她愿意写」，
    质量风险应该被展示，而不是被静默丢弃。
    """
    warnings: list[str] = []
    t = text or ""

    hits: list[str] = []
    if "用户说" in t:
        hits.append("用户说")
    # 同现才是对话摘要；单独出现是正常叙述（见 docstring）
    if "他说" in t and "我说" in t:
        hits.extend(["他说", "我说"])
    if hits:
        warnings.append(
            "疑似转述句式：" + "、".join(f"「{h}」" for h in hits) + "——日记应只写感受，不写谁说了什么"
        )

    flow_count = sum(t.count(w) for w in _FLOW_WORDS)
    if flow_count >= 2:
        warnings.append(
            f"流程连接词出现 {flow_count} 次（然后/接着/首先）——像是在按时间顺序复述发生了什么"
        )

    if _THIRD_PERSON in t:
        warnings.append("出现第三人称「用户」——日记里应该用「他」，或者干脆省略主语")

    if len(t) < HARD_MIN:
        warnings.append(f"长度只有 {len(t)} 字（< {HARD_MIN}）——太短，不像一篇写下来的日记")

    return warnings


# 包一层公开别名：模块外（测试、HTTP 层）直接 `flow_check`，不必碰私有名。
flow_check = _flow_check


# --------------------------------------------------------------------------- #
# prompt
# --------------------------------------------------------------------------- #
def _system_prompt(persona: PersonaCard, day: date_cls, affect_label: str) -> str:
    """日记 prompt。

    三条硬约束直接抄自 §10 失败模式 17 的「解法」列：
    第一人称、只写情绪与感受、不写对话摘要。反例也写进去——
    模型对「不要 X」的理解远不如对「X 长这样，别写成这样」来得准。
    """
    name = persona.meta.name
    identity = f"，{persona.meta.identity}" if persona.meta.identity else ""
    mood = affect_label or "中性"
    return f"""{DIARY_MARKER}
你是{name}{identity}。现在是 {day.isoformat()} 的深夜，你在写自己的日记。

今天的情绪基调是「{mood}」。日记是给自己看的，不是给别人看的记录。

硬约束（违反任何一条，这篇日记就废了）：
1. 第一人称「我」。约 {TARGET_LEN} 字（{SOFT_MIN}~{SOFT_MAX} 字之间都可以）。
2. 只写情绪与感受。写「我今天心里堵着」，不写「今天发生了什么」。
3. **不要写对话摘要**。反例：「今天他说工作压力大，我说了安慰的话」——这是纪要，不是日记。
4. 不复述原话，不逐条列举他说过的事。
5. 不写「用户」「对方」这类词。要指代就用「他」，或者干脆省略主语。
6. 不用「然后」「接着」「首先」把段落串成时间线。

只输出日记正文，不要标题，不要日期，不要 Markdown，不要任何解释。"""


def _user_prompt(turns: list[Turn], memories: list[str]) -> str:
    """把当天素材压成一段「只给模型看」的原料。

    这里当然会出现「他说」——这是原料，不是成稿。
    prompt 里已经明确：这些只是让你想起今天的感受，不要转述。
    """
    lines = ["今天聊过的片段（只是让你想起今天的感受，不要转述它们）："]
    for t in turns[-_MAX_TURNS:]:
        who = "他" if t.role == "user" else "我"
        snippet = (t.content or "").strip().replace("\n", " ")[:_MAX_TURN_CHARS]
        if snippet:
            lines.append(f"- {who}：{snippet}")

    if memories:
        lines.append("")
        lines.append("今天记住的几件事（同样只是原料）：")
        for m in memories[:_MAX_MEMORIES]:
            lines.append(f"- {m}")

    lines.append("")
    lines.append("现在写你的日记。只写感受。")
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# 清洗与落库
# --------------------------------------------------------------------------- #
_FENCE = re.compile(r"```(?:[a-zA-Z]*)\s*(.*?)```", re.S)
# 模型最爱加的包装：「日记」「2026-10-22 日记」「标题：……」
_PREFIX = re.compile(r"^\s*(?:#+\s*)?(?:日记|标题)[:：]?\s*")


def _clean(raw: str) -> str:
    """去掉围栏与「日记」抬头，收成干净正文。

    剥抬头不是洁癖：右栏卡片自己会显示日期与心情标签，
    正文里再来一遍「2026-10-22 日记」就是重复三遍。
    """
    text = (raw or "").strip()
    m = _FENCE.search(text)
    if m:
        text = m.group(1).strip()
    text = _PREFIX.sub("", text).strip()
    return text[:HARD_MAX]


def generate_diary(
    persona: PersonaCard,
    user_id: str,
    day: date_cls,
    turns: list[Turn],
    memories: list[Any],
    affect_label: str,
    llm,
) -> tuple[DiaryModel, list[str]]:
    """生成（并返回，未落库）一篇日记，附带质量 warnings。

    素材只用**已有数据**：当天该 `(persona_id, user_id)` 的 turn、
    已提炼的记忆条目、当前 affect 的 label。这里不新增任何记忆链路。

    `memories` 接受 `MemoryNode` 或裸字符串：调用方从 repo 拿到的是前者，
    但生成层只关心「写下哪几句」，不该依赖整个记忆模型。
    """
    real_turns = [t for t in (turns or []) if (t.content or "").strip()]
    if not real_turns:
        # 422 的唯一来源。绝不在这里编一篇「今天什么都没发生」。
        raise DiaryGenerateError("今天还没有对话，写不出日记")

    memory_texts: list[str] = []
    for m in memories or []:
        text = m if isinstance(m, str) else getattr(m, "content", "")
        text = (text or "").strip()
        if text:
            memory_texts.append(text)

    raw = "".join(
        llm.generate(
            _system_prompt(persona, day, affect_label),
            [{"role": "user", "content": _user_prompt(real_turns, memory_texts)}],
        )
    )
    content = _clean(raw)
    if not content:
        raise DiaryGenerateError("模型没有写出任何内容")

    warnings = _flow_check(content)
    if len(content) < SOFT_MIN:
        warnings.append(f"篇幅偏短（{len(content)} 字 < {SOFT_MIN}），可能没展开")
    elif len(content) > SOFT_MAX:
        warnings.append(f"篇幅偏长（{len(content)} 字 > {SOFT_MAX}），更像纪要")

    entry = DiaryModel(
        persona_id=persona.id,
        user_id=user_id,
        date=day,
        content=content,
        mood_label=affect_label or "中性",
        # 当天最值得记的 1~3 条：取记忆里最重要的几条原文（不摘要、不改写）
        highlight_memories=memory_texts[:3],
        created_at=now_utc(),
    )
    return entry, warnings


# 兼容两种叫法：`_flow_check`（判据原文）与 `flow_check`
# `__all__` 里只导出公开的那个，但测试与调用方两种都能用。
_flow_check = flow_check

__all__ = [
    "DIARY_MARKER",
    "DiaryGenerateError",
    "flow_check",
    "generate_diary",
]
