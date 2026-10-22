"""一句话生成人格（「一键赋角」入口 B，docs/ARCHITECTURE.md §3.1.3）。

流水线顺序是契约，不能重排：

    LLM 生成 → 解析 JSON → Schema 校验 → 冲突消解 → 生成样例 → 返回预览

**绝不落库**：本模块产出的是「提案」，用户在界面上确认后才由调用方
调 `POST /v1/personas` 落库。生成即入库会让用户的实验污染人格库。

为什么冲突消解是独立一层（§3.1.3「Step3 是质量分水岭」）：
    直接让模型吐一份人格卡，绝大多数会有内在矛盾——性格外向却只说两个字、
    语体写着冷淡却满屏 emoji、高神经质却情绪基线很高。
    这些矛盾单看点不觉得，一问一答立刻露馅。所以必须有确定性的校验与修正层，
    而不是靠「再问一次模型」。
"""

from __future__ import annotations

import copy
import json
import re
from dataclasses import dataclass, field
from typing import Any

from ..models import PersonaCard, clamp

# mock provider 靠这两个标记识别「这是生成请求 / 这是样例请求」。
# 真实模型忽略它们（它们只是 system prompt 里的一行说明）。
GENERATE_MARKER = "[LINGER_PERSONA_GENERATE]"
PREVIEW_MARKER = "[LINGER_PERSONA_PREVIEW]"

# 「冷淡语体」词——用于 R2 判断（语体冷淡却高 emoji 率是矛盾）
_COLD_REGISTER_WORDS = ("冷淡", "疏离", "寡言", "沉默", "冷硬", "克制", "惜字如金")

# 合法区间
_SENTENCE_MIN, _SENTENCE_MAX = 1, 60
_PREVIEW_COUNT = 6


class PersonaGenerateError(ValueError):
    """生成结果不可用（JSON 坏了 / 缺关键字段）。HTTP 层映射为 422。"""


@dataclass
class GenerateResult:
    """一次生成的结果。persona 尚未落库。"""

    persona: PersonaCard
    warnings: list[str] = field(default_factory=list)
    preview: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "persona": json.loads(self.persona.model_dump_json()),
            "warnings": list(self.warnings),
            "preview": list(self.preview),
        }


# --------------------------------------------------------------------------- #
# 入口
# --------------------------------------------------------------------------- #
def generate_persona(description: str, llm) -> GenerateResult:
    """一句话描述 → 人格卡提案。

    description 的合法性由 HTTP 层校验；这里只做非空兜底。
    llm 只需实现 `generate(system, messages, ctx=None) -> Iterator[str]`。
    """
    desc = (description or "").strip()
    if not desc:
        raise PersonaGenerateError("描述不能为空")

    raw = "".join(llm.generate(_generate_system(), [{"role": "user", "content": desc}]))
    data = _extract_json(raw)
    card, warnings = _build_card(data)
    warnings.extend(resolve_conflicts(card))
    preview = _sample_preview(card, llm)
    return GenerateResult(persona=card, warnings=warnings, preview=preview)


# --------------------------------------------------------------------------- #
# Step 1：prompt
# --------------------------------------------------------------------------- #
def _generate_system() -> str:
    return f"""{GENERATE_MARKER}
你在设计一个人格卡。根据用户的一句话描述，产出一份 JSON。

只输出 JSON，不要 Markdown 代码块，不要任何解释。字段如下：

{{
  "meta": {{"name": "名字", "aliases": [], "age": 30, "identity": "身份", "archetype": "原型"}},
  "traits": {{"openness": 0.5, "conscientiousness": 0.5, "extraversion": 0.5,
             "agreeableness": 0.5, "neuroticism": 0.5}},
  "voice": {{"register": "语体（如：口语、短句）", "avg_sentence_len": [8, 20],
            "emoji_rate": 0.1, "emoji_set": [], "verbal_tics": [], "forbidden_phrases": []}},
  "values": {{}},
  "affect": {{"baseline": {{"pleasure": 0.2, "arousal": 0.3, "dominance": 0.4}}}},
  "initiative": {{"max_proactive_per_day": 1, "proactive_topics": []}}
}}

硬约束：
- traits 五项都在 0~1 之间，且必须与描述一致（嘴硬心软 = agreeableness 高、extraversion 中等）。
- avg_sentence_len 是 [下界, 上界]，都在 1~60，且下界不大于上界。
- emoji_rate 在 0~1。语体冷淡的人 emoji_rate 必须很低。
- 性格外向（extraversion >= 0.7）的人，句长上界不能低于 12。
- 名字要像真人名，不要用「AI」「助手」这类词。"""


def _preview_system(card: PersonaCard) -> str:
    tics = "、".join(card.voice.verbal_tics) or "（无固定口头禅）"
    return f"""{PREVIEW_MARKER}
你就是「{card.meta.name}」{('，' + card.meta.identity) if card.meta.identity else ''}。
说话风格：{card.voice.register}。
口头禅：{tics}。

用这个人的口吻写 {_PREVIEW_COUNT} 句台词，每行一句，不要编号，不要解释，不要引号。
台词要能听出这是个什么人——不要写通用寒暄。"""


# --------------------------------------------------------------------------- #
# Step 2：解析
# --------------------------------------------------------------------------- #
_FENCE = re.compile(r"```(?:json)?\s*(.*?)```", re.S)


def _extract_json(raw: str) -> dict[str, Any]:
    """从模型输出里抠出 JSON 对象。

    先剥 Markdown 围栏（模型最常见的多余包装），再找第一个 `{` 到最后一个 `}`。
    仍解析不了就抛 PersonaGenerateError——HTTP 层给 422，绝不能 500。
    """
    text = (raw or "").strip()
    if not text:
        raise PersonaGenerateError("模型没有返回内容")

    m = _FENCE.search(text)
    if m:
        text = m.group(1).strip()

    if not text.startswith("{"):
        start, end = text.find("{"), text.rfind("}")
        if start == -1 or end <= start:
            raise PersonaGenerateError("模型返回的不是 JSON")
        text = text[start : end + 1]

    try:
        data = json.loads(text)
    except json.JSONDecodeError as e:
        raise PersonaGenerateError(f"模型返回的 JSON 无法解析：{e.msg}") from None

    if not isinstance(data, dict):
        raise PersonaGenerateError("模型返回的 JSON 不是对象")
    return data


# --------------------------------------------------------------------------- #
# Step 3：Schema 校验（clamp + 缺省）
# --------------------------------------------------------------------------- #
def _build_card(data: dict[str, Any]) -> tuple[PersonaCard, list[str]]:
    warnings: list[str] = []
    d = copy.deepcopy(data)

    # id 由服务端生成，不接受模型指定（否则可能撞库/伪造）
    d.pop("id", None)
    d.pop("version", None)

    meta = d.setdefault("meta", {})
    if not isinstance(meta, dict):
        raise PersonaGenerateError("meta 字段不是对象")
    name = str(meta.get("name") or "").strip()
    if not name:
        raise PersonaGenerateError("生成结果缺少人格名（meta.name）")
    meta["name"] = name

    # traits：五项 clamp 到 [0,1]
    traits = d.setdefault("traits", {})
    if not isinstance(traits, dict):
        traits = d["traits"] = {}
    for k in ("openness", "conscientiousness", "extraversion", "agreeableness", "neuroticism"):
        v = _as_float(traits.get(k), 0.5)
        c = clamp(v, 0.0, 1.0)
        if c != v:
            warnings.append(f"traits.{k}={v:g} 越界，已修正为 {c:g}")
        traits[k] = c

    # voice
    voice = d.setdefault("voice", {})
    if not isinstance(voice, dict):
        voice = d["voice"] = {}

    asl = voice.get("avg_sentence_len")
    asl_ok = (
        isinstance(asl, (list, tuple))
        and len(asl) == 2
        and all(isinstance(x, (int, float)) for x in asl)
    )
    if not asl_ok:
        warnings.append(f"avg_sentence_len={asl!r} 不是 [下界, 上界]，已重置为 [8, 20]")
        asl = [8, 20]
    lo, hi = int(asl[0]), int(asl[1])
    clo, chi = clamp(lo, _SENTENCE_MIN, _SENTENCE_MAX), clamp(hi, _SENTENCE_MIN, _SENTENCE_MAX)
    if (clo, chi) != (lo, hi):
        warnings.append(f"avg_sentence_len=[{lo}, {hi}] 超出 {_SENTENCE_MIN}~{_SENTENCE_MAX}，已修正为 [{clo}, {chi}]")
    voice["avg_sentence_len"] = [clo, chi]

    rate = _as_float(voice.get("emoji_rate"), 0.1)
    crate = clamp(rate, 0.0, 1.0)
    if crate != rate:
        warnings.append(f"emoji_rate={rate:g} 越界，已修正为 {crate:g}")
    voice["emoji_rate"] = crate

    # affect.baseline 的 PAD 各维
    affect = d.setdefault("affect", {})
    if isinstance(affect, dict):
        base = affect.setdefault("baseline", {})
        if isinstance(base, dict):
            for k, (lo_b, hi_b) in (("pleasure", (-1.0, 1.0)), ("arousal", (0.0, 1.0)), ("dominance", (0.0, 1.0))):
                v = _as_float(base.get(k), 0.25 if k == "pleasure" else 0.4)
                c = clamp(v, lo_b, hi_b)
                if c != v:
                    warnings.append(f"affect.baseline.{k}={v:g} 越界，已修正为 {c:g}")
                base[k] = c

    try:
        card = PersonaCard(**d)
    except Exception as e:  # pydantic 校验失败同样是「提案不可用」
        raise PersonaGenerateError(f"人格卡字段不合法：{e}") from None

    return card, warnings


def _as_float(v: Any, default: float) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


# --------------------------------------------------------------------------- #
# Step 4：冲突消解（确定性规则，R1~R5）
# --------------------------------------------------------------------------- #
def resolve_conflicts(card: PersonaCard) -> list[str]:
    """修正人格卡的内在矛盾，返回修正说明。

    顺序有讲究：R3（句长倒挂）必须先于 R1（外向-句长），
    否则 R1 会基于一个倒挂的区间做判断，改出来的结果仍然错。
    """
    warnings: list[str] = []
    t, v = card.traits, card.voice

    # R3：句长倒挂
    lo, hi = v.avg_sentence_len
    if lo > hi:
        v.avg_sentence_len = [hi, lo]
        warnings.append(f"句长下界 {lo} 大于上界 {hi}，已交换为 [{hi}, {lo}]")
        lo, hi = v.avg_sentence_len

    # R1：外向 × 极短句
    if t.extraversion >= 0.7 and hi <= 10:
        v.avg_sentence_len = [lo, 14]
        warnings.append(
            f"外向度 {t.extraversion:.2f} 但句长上界只有 {hi}，已调到 14——外向的人不会只说极短句"
        )

    # R2：冷淡语体 × 高 emoji 率
    cold = [w for w in _COLD_REGISTER_WORDS if w in (v.register or "")]
    if cold and v.emoji_rate >= 0.5:
        old = v.emoji_rate
        v.emoji_rate = 0.05
        warnings.append(
            f"语体写着「{cold[0]}」却 emoji_rate={old:.2f}，已降到 0.05——冷淡的人不会满屏表情"
        )

    # R4：高神经质 × 高愉悦基线
    if t.neuroticism >= 0.8 and card.affect.baseline.pleasure > 0.5:
        old = card.affect.baseline.pleasure
        card.affect.baseline.pleasure = 0.2
        warnings.append(
            f"神经质 {t.neuroticism:.2f} 但情绪基线愉悦度 {old:.2f}，已降到 0.20"
        )

    # R5：极内向 × 高频主动
    if t.extraversion <= 0.2 and card.initiative.max_proactive_per_day > 2:
        old = card.initiative.max_proactive_per_day
        card.initiative.max_proactive_per_day = 1
        warnings.append(
            f"外向度 {t.extraversion:.2f} 却每天主动 {old} 次，已降到 1 次"
        )

    return warnings


# --------------------------------------------------------------------------- #
# Step 5：样例台词
# --------------------------------------------------------------------------- #
def _sample_preview(card: PersonaCard, llm) -> list[str]:
    """6 条台词。生成失败不阻断主链路——宁可少几条，也不让整次生成失败。"""
    try:
        raw = "".join(llm.generate(_preview_system(card), [{"role": "user", "content": "写吧"}]))
    except Exception:
        return []
    lines: list[str] = []
    for line in (raw or "").splitlines():
        s = line.strip().strip("「」\"'")
        # 去掉可能残留的 "1. " / "1、" 编号
        s = re.sub(r"^\d+[.、)\]]\s*", "", s).strip()
        if s:
            lines.append(s)
        if len(lines) >= _PREVIEW_COUNT:
            break
    return lines


__all__ = [
    "GENERATE_MARKER",
    "PREVIEW_MARKER",
    "GenerateResult",
    "PersonaGenerateError",
    "generate_persona",
    "resolve_conflicts",
]