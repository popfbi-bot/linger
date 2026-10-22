"""聊天记录蒸馏的生成层（「一键赋角」入口 C）。

分工是这个模块的全部意义：

    确定性层 `fingerprint.py`   → voice（句长 / emoji / 口癖 / 高频词 / 标点 / 语体）
    生成层   本模块             → meta / traits / values（推断性字段）
    冲突消解  `persona.generator.resolve_conflicts` → 修内在矛盾（复用，不重写）

**硬约束：最终人格卡的 voice 必须是指纹算出的那一份。**

为什么这条约束值得单独立成一条铁律：
    模型看到「这个人说话很短」，会自作主张把 register 写成「书面语、精炼」
    或者干脆改句长区间——它不知道自己是在**覆盖统计事实**。
    统计结果和模型想象冲突时，真相在统计这一侧。
    所以这里先记下指纹的 voice，模型返回后无条件盖回去，
    并且留一条 warning 告诉用户「模型想改语体，已保留统计结果」——
    用户有权知道机器之间意见不一致，这是可解释性的一部分。

流水线顺序同 M3-A（`persona/generator.py`）的契约：
    指纹 → LLM 补字段 → 解析 JSON → Schema 校验 → 盖回 voice → 冲突消解 → 样例
"""

from __future__ import annotations

import copy
import json
import re
from dataclasses import dataclass, field
from typing import Any

from ..models import PersonaCard
from ..persona.generator import PREVIEW_MARKER, PersonaGenerateError, resolve_conflicts
from .fingerprint import dominant_speaker, extract_fingerprint

# mock provider 靠这个标记识别「这是蒸馏请求」。真实模型忽略它。
DISTILL_MARKER = "[LINGER_PERSONA_DISTILL]"

# 合规红线：克隆产物必须打这个标，且不允许上架模板市场。
PROVENANCE_CLONED = "cloned"

# 喂给模型的样本条数：3~5 条足够让模型读出「这是什么人」，多了是噪声。
_SAMPLE_MIN, _SAMPLE_MAX = 3, 5
_PREVIEW_COUNT = 6

_JSON_FENCE = re.compile(r"```(?:json)?\s*(.*?)```", re.S)


class PersonaDistillError(ValueError):
    """蒸馏结果不可用。与 PersonaGenerateError 同源（都是 422）。"""


@dataclass
class DistillResult:
    """一次蒸馏的结果。persona 尚未落库。"""

    persona: PersonaCard
    fingerprint: dict[str, Any] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)
    preview: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "persona": json.loads(self.persona.model_dump_json()),
            "fingerprint": dict(self.fingerprint),
            "warnings": list(self.warnings),
            "preview": list(self.preview),
        }


# --------------------------------------------------------------------------- #
# 入口
# --------------------------------------------------------------------------- #
def distill_persona(
    lines: list[tuple[str, str]],
    target_name: str | None,
    llm,
) -> DistillResult:
    """[(speaker, content)] → 人格卡提案（**不落库**）。

    `target_name` 为空时取发言条数最多的一方（见 `fingerprint.dominant_speaker`）。
    """
    speaker = (target_name or "").strip() or dominant_speaker(lines)

    # Step 1：确定性指纹。它先于模型跑，因为它是**事实**，模型的产出要向它让步。
    fp = extract_fingerprint(lines, speaker)

    # Step 2：模型只补推断性字段
    samples = _samples_for(lines, speaker)
    raw = "".join(llm.generate(_distill_system(speaker, fp, samples), [{"role": "user", "content": "开始"}]))
    data = _extract_json(raw)

    # Step 3：拼卡。voice 用指纹，不用模型给的。
    card, warnings = _build_card(data, speaker, fp)
    warnings.extend(_voice_override_notes(data, fp))
    if not card.meta.name:
        card.meta.name = speaker

    # Step 4：冲突消解——复用 M3-A，不重写。
    # 它对 voice 的唯一动作是 R1（外向 × 上界 ≤10 → 抬到 14）：只会**放宽**区间，
    # 不会把统计出来的句长改小。指纹的上界恒 ≥8，所以 R1 即使命中也是在
    # 8~10 这个窄带里往上抬——统计事实始终还在区间内。
    warnings.extend(resolve_conflicts(card))

    # Step 5：合规标记（落库时 app.py 也会再兜一次，见 create_persona）
    card.provenance = {
        **(card.provenance or {}),
        "type": PROVENANCE_CLONED,
        "cloned_from": speaker,
    }

    # Step 6：样例台词
    preview = _sample_preview(card, llm)
    return DistillResult(persona=card, fingerprint=fp, warnings=warnings, preview=preview)


# --------------------------------------------------------------------------- #
# prompt
# --------------------------------------------------------------------------- #
def _distill_system(speaker: str, fp: dict[str, Any], samples: list[str]) -> str:
    lo, hi = fp["avg_sentence_len"]
    tics = "、".join(fp["verbal_tics"]) or "（暂未识别）"
    words = "、".join(fp["high_freq_words"]) or "（暂未识别）"
    sample_text = "\n".join(f"- {s}" for s in samples) or "（无）"
    return f"""{DISTILL_MARKER}
下面是一段真实聊天记录里「{speaker}」的发言。已经有人用统计方法算出了他的说话方式：

- 句长 {lo}~{hi} 字，emoji 率 {fp['emoji_rate']}，语体：{fp['register']}
- 口癖：{tics}
- 高频词：{words}
- 标点习惯：{fp['punctuation_policy']}

样本发言：
{sample_text}

你的任务是**只填三样东西**：
  1. meta：这个人是谁（name / identity / archetype / age）
  2. traits：BigFive 五项（0~1）
  3. values：他看重什么（1~3 条，短句）

只输出 JSON，不要 Markdown 代码块，不要解释：

{{
  "meta": {{"name": "{speaker}", "identity": "身份", "archetype": "原型", "age": 28}},
  "traits": {{"openness": 0.5, "conscientiousness": 0.5, "extraversion": 0.5,
             "agreeableness": 0.5, "neuroticism": 0.5}},
  "values": {{"stance_advice": "一句话"}}
}}

硬约束：
- **不要输出 voice**。说话方式已经统计出来了，你的猜测不算数。
- 性格必须能从上面的样本里读出来，不要凭空加戏。
- 名字就用「{speaker}」，不要改。"""


def _preview_system(card: PersonaCard) -> str:
    tics = "、".join(card.voice.verbal_tics) or "（无固定口头禅）"
    lo, hi = card.voice.avg_sentence_len
    return f"""{PREVIEW_MARKER}
你就是「{card.meta.name}」{('，' + card.meta.identity) if card.meta.identity else ''}。
说话风格：{card.voice.register}，句子大约 {lo}~{hi} 个字。
口头禅：{tics}。

用这个人的口吻写 {_PREVIEW_COUNT} 句台词，每行一句，不要编号，不要解释，不要引号。
台词要能听出这是个什么人——不要写通用寒暄。"""


def _samples_for(lines: list[tuple[str, str]], speaker: str) -> list[str]:
    """挑 3~5 条样本消息。

    优先挑中等长度的：太短（「嗯」「好」）没有信息量，
    太长会把 prompt 吃满。都不合格时退回原始顺序的前几条。
    """
    texts = [c.strip() for who, c in lines if who == speaker and c.strip()]
    if not texts:
        return []
    middling = [t for t in texts if 4 <= len(t) <= 60]
    pool = middling or texts
    # 均匀抽样，避免永远只看到开头几句（开头往往只是打招呼）
    if len(pool) > _SAMPLE_MAX:
        step = len(pool) / _SAMPLE_MAX
        pool = [pool[int(i * step)] for i in range(_SAMPLE_MAX)]
    return pool[:_SAMPLE_MAX] if len(pool) >= _SAMPLE_MIN else pool


# --------------------------------------------------------------------------- #
# 解析 + 校验
# --------------------------------------------------------------------------- #
def _extract_json(raw: str) -> dict[str, Any]:
    """剥 Markdown 围栏 → 找第一个 `{` 到最后一个 `}` → json.loads。

    坏 JSON 一律 PersonaDistillError（HTTP 层 422），不许 500。
    """
    text = (raw or "").strip()
    if not text:
        raise PersonaDistillError("模型没有返回内容")

    m = _JSON_FENCE.search(text)
    if m:
        text = m.group(1).strip()
    if not text.startswith("{"):
        start, end = text.find("{"), text.rfind("}")
        if start == -1 or end <= start:
            raise PersonaDistillError("模型返回的不是 JSON")
        text = text[start : end + 1]

    try:
        data = json.loads(text)
    except json.JSONDecodeError as e:
        raise PersonaDistillError(f"模型返回的 JSON 无法解析：{e.msg}") from None
    if not isinstance(data, dict):
        raise PersonaDistillError("模型返回的 JSON 不是对象")
    return data


def _build_card(
    data: dict[str, Any], speaker: str, fp: dict[str, Any]
) -> tuple[PersonaCard, list[str]]:
    """把「模型补的字段」+「指纹算的 voice」拼成一张合法人格卡。

    校验复用 M3-A 的 `_build_card`：它已经把 traits 的 clamp、voice 的区间校验、
    affect 基线都写好了。这里只是先用自己的 voice 覆盖模型给的，再交给它。
    """
    from ..persona.generator import _build_card as base_build_card

    d = copy.deepcopy(data)
    d.pop("id", None)
    d.pop("version", None)
    d.pop("provenance", None)  # 合规标记由服务端写，不接受模型/客户端指定

    # 模型不许改 voice —— 这里是硬约束的落点
    d["voice"] = _voice_from_fingerprint(fp)

    meta = d.setdefault("meta", {})
    if not isinstance(meta, dict):
        raise PersonaDistillError("meta 字段不是对象")
    if not str(meta.get("name") or "").strip():
        meta["name"] = speaker

    try:
        return base_build_card(d)
    except PersonaGenerateError as exc:
        raise PersonaDistillError(str(exc)) from None


def _voice_from_fingerprint(fp: dict[str, Any]) -> dict[str, Any]:
    """指纹 → Voice 字面量。

    只取 Voice 认识的键。`high_freq_words` 不是 Voice 字段，
    但它属于「我们统计出了什么」，所以留在 `fingerprint` 返回体里给前端展示。
    """
    return {
        "register": fp["register"],
        "avg_sentence_len": list(fp["avg_sentence_len"]),
        "emoji_rate": fp["emoji_rate"],
        "emoji_set": list(fp["emoji_set"]),
        "verbal_tics": list(fp["verbal_tics"]),
        "punctuation_policy": fp["punctuation_policy"],
    }


def _voice_override_notes(data: dict[str, Any], fp: dict[str, Any]) -> list[str]:
    """模型想改语体时留一条 warning。没想改就不留（警告多了等于没警告）。"""
    model_voice = data.get("voice")
    if not isinstance(model_voice, dict):
        return []
    authoritative = _voice_from_fingerprint(fp)
    diffs = [
        k
        for k, v in model_voice.items()
        if k in _VOICE_KEYS and v != authoritative.get(k)
    ]
    if not diffs:
        return []
    return [
        "模型想改语体（" + "、".join(sorted(diffs)) + "），已保留统计结果"
        "——说话方式是从原文数出来的，不是猜的"
    ]


_VOICE_KEYS = (
    "register",
    "avg_sentence_len",
    "emoji_rate",
    "emoji_set",
    "verbal_tics",
    "punctuation_policy",
)


# --------------------------------------------------------------------------- #
# 样例台词
# --------------------------------------------------------------------------- #
def _sample_preview(card: PersonaCard, llm) -> list[str]:
    """6 条台词。生成失败不阻断主链路。"""
    try:
        raw = "".join(llm.generate(_preview_system(card), [{"role": "user", "content": "写吧"}]))
    except Exception:
        return []
    lines: list[str] = []
    for line in (raw or "").splitlines():
        s = line.strip().strip("「」\"'")
        s = re.sub(r"^\d+[.、)\]]\s*", "", s).strip()
        if s:
            lines.append(s)
        if len(lines) >= _PREVIEW_COUNT:
            break
    return lines


__all__ = [
    "DISTILL_MARKER",
    "PROVENANCE_CLONED",
    "DistillResult",
    "PersonaDistillError",
    "distill_persona",
]
