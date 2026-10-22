"""五步向导（「一键赋角」入口 D，docs/ARCHITECTURE.md §3.1.3）。

与入口 B 的分工写在这里，因为它决定了本模块的所有取舍：

    B（一句话生成）= 「说一句，模型猜」。快，但用户看不见中间产物，改不了单个字段。
    D（五步向导）  = 「一项项填，我看得见每个字段」。慢，但精确。

于是向导的核心动作不是「生成」，而是**把一份填到一半的草稿变成一张合法人格卡**：

    draft（部分字段） → 补默认值 → Schema 校验/clamp → 冲突消解 → 3 条样例 → 返回（不落库）

三条刻意的设计：

1. **draft 允许残缺**。向导第 1 步之前用户什么都还没填，前端送来的就是 `{}`。
   这里给 422 等于「打开面板第一屏就报错」——所以缺名字用「（未命名）」占位，
   而不是拒绝请求。真正的校验（名字必填）在前端第 1 步的「下一步」按钮上，
   那才是用户能改的地方。

2. **不重复实现校验与纠错**。clamp / 区间校验复用 M3-A 的 `_build_card`
   （与入口 C 同一做法），矛盾修正复用 `resolve_conflicts`——
   「什么样的人格卡算有矛盾」只能有一处定义，否则三个入口会互相打脸。

3. **预览只要 3 条**。向导会反复预览（每进下一步一次），6 条既慢又贵，
   而且用户在这一步只需要判断「语气对不对」，3 条就够。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any

from ..models import PersonaCard
from .generator import PREVIEW_MARKER, PersonaGenerateError, _build_card, resolve_conflicts

# 名字缺失时的占位。用一个看得见的括号名，而不是空串——
# 用户在第 5 步确认时能立刻发现「我还没起名字」，而不是落库一个 ""
UNNAMED = "（未命名）"

# 向导预览条数：3 条够听出语气，6 条太慢太贵（B 入口才是 6 条）
WIZARD_PREVIEW_COUNT = 3


class PersonaWizardError(ValueError):
    """草稿不可用（结构不合法）。HTTP 层映射为 422，绝不 500。"""


@dataclass
class PreviewResult:
    """一次预览的结果。persona 是补全后的完整卡，**尚未落库**。"""

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
def preview_draft(draft: dict[str, Any] | None, llm) -> PreviewResult:
    """部分填写的草稿 → 完整人格卡预览（不落库）。

    draft 可以是 `{}`：那是「向导还没开始填」的合法状态。
    """
    card, warnings = build_card_from_draft(draft)
    warnings.extend(resolve_conflicts(card))

    # 来源标记：向导是**用户一项项填的**，模型只生成了试听台词，
    # 所以它是 authored（用户写的），不是 generated（模型生成的）。
    # 这个区别现在看无所谓，将来有用：合规上「AI 生成内容」与「用户手填内容」
    # 的标注要求不同；人格市场也要按来源决定能不能上架。
    # 落库侧（POST /v1/personas）已经会尊重调用方传入的 type。
    card.provenance = {"type": "authored", "source": "wizard"}

    preview = _sample_preview(card, llm)
    return PreviewResult(persona=card, warnings=warnings, preview=preview)


def build_card_from_draft(draft: dict[str, Any] | None) -> tuple[PersonaCard, list[str]]:
    """草稿 → 合法人格卡 + 修正说明。不调模型，纯确定性。

    步骤：
      1. 只留 PersonaCard 认识的键（前端多送什么都不会炸）
      2. 往默认值上逐层合并——**缺的字段用 PersonaCard 的默认值补**，
         而不是自己再抄一份默认值（抄一份就会两处漂移）
      3. 名字为空 → 占位名（不抛错，见模块注释）
      4. 交给 M3-A 的 `_build_card` 做 clamp 与区间校验（唯一真相源）
    """
    data = _normalize(draft)

    # 默认值补全：先序列化一份空的 PersonaCard，再逐层合并草稿
    base = json.loads(PersonaCard(meta={"name": ""}).model_dump_json())
    merged = _merge(base, data)
    # id / version / provenance 由服务端在落库时决定，预览阶段不接受外部指定
    merged.pop("id", None)
    merged.pop("version", None)
    merged.pop("provenance", None)

    meta = merged.setdefault("meta", {})
    if not str(meta.get("name") or "").strip():
        meta["name"] = UNNAMED

    try:
        return _build_card(merged)
    except PersonaGenerateError as exc:
        raise PersonaWizardError(str(exc)) from None


# --------------------------------------------------------------------------- #
# 归一化：只留合法键，逐层合并
# --------------------------------------------------------------------------- #
_FIELD_NAMES = set(PersonaCard.model_fields)

# 草稿里「每一项」的合法键。Voice / Knowledge 用白名单是刻意的：
# 向导只暴露这几个字段，多送的键（如 emoji_set）不该悄悄生效——
# 悄悄生效意味着「用户没看见的东西改变了结果」。
_NESTED_NAMES: dict[str, set[str]] = {
    "meta": {"name", "aliases", "age", "identity", "archetype"},
    "traits": {"openness", "conscientiousness", "extraversion", "agreeableness", "neuroticism"},
    "voice": {
        "register",
        "avg_sentence_len",
        "emoji_rate",
        "verbal_tics",
        "forbidden_phrases",
        "punctuation_policy",
    },
    "knowledge": {"knows", "knows_not", "leak_policy"},
    "affect": {"baseline", "sensitivity"},
    "initiative": {"silence_tolerance_turns", "max_proactive_per_day", "proactive_topics"},
}


def _normalize(draft: dict[str, Any] | None) -> dict[str, Any]:
    """裁掉草稿里不该出现的键。非 dict 字段直接丢（让默认值接手）。"""
    if draft is None:
        return {}
    if not isinstance(draft, dict):
        raise PersonaWizardError("draft 必须是对象")
    out: dict[str, Any] = {}
    for key, value in draft.items():
        if key not in _FIELD_NAMES:
            continue
        allowed = _NESTED_NAMES.get(key)
        if allowed is None:
            out[key] = value
            continue
        if not isinstance(value, dict):
            continue
        out[key] = {k: v for k, v in value.items() if k in allowed}
    return out


def _merge(base: Any, patch: Any) -> Any:
    """深合并：patch 里给了的覆盖 base，没给的保留默认。

    None 视为「没填」：前端把输入框清空时送 null，不该把 name 变成 null。
    """
    if isinstance(base, dict) and isinstance(patch, dict):
        out = dict(base)
        for k, v in patch.items():
            if v is None:
                continue
            out[k] = _merge(base.get(k), v) if isinstance(base.get(k), dict) else v
        return out
    return patch


# --------------------------------------------------------------------------- #
# 预览台词（3 条）
# --------------------------------------------------------------------------- #
def _preview_system(card: PersonaCard) -> str:
    """与 M3-A 用同一个 PREVIEW_MARKER。

    为什么复用标记而不新加一个：mock 靠标记分流，同一件事用两个标记只会让
    mock 里多一个分支去维护。条数差异由下面**截断到 3 条**解决——
    标记表示「这是样例请求」，条数表示「这次要几条」，两件事分开。
    """
    tics = "、".join(card.voice.verbal_tics) or "（无固定口头禅）"
    lo, hi = card.voice.avg_sentence_len
    return f"""{PREVIEW_MARKER}
你就是「{card.meta.name}」{('，' + card.meta.identity) if card.meta.identity else ''}。
说话风格：{card.voice.register}，句子大约 {lo}~{hi} 个字。
口头禅：{tics}。

用这个人的口吻写 3 句台词，每行一句，不要编号，不要解释，不要引号。
台词要能听出这是个什么人——不要写通用寒暄。"""


_NUMBERING = re.compile(r"^\d+[.、)\]]\s*")


def _sample_preview(card: PersonaCard, llm) -> list[str]:
    """3 条台词。生成失败不阻断预览——宁可少几条，也不让向导一步都走不下去。"""
    try:
        raw = "".join(llm.generate(_preview_system(card), [{"role": "user", "content": "写吧"}]))
    except Exception:
        return []
    lines: list[str] = []
    for line in (raw or "").splitlines():
        s = _NUMBERING.sub("", line.strip().strip("「」\"'")).strip()
        if s:
            lines.append(s)
        if len(lines) >= WIZARD_PREVIEW_COUNT:
            break
    return lines


__all__ = [
    "UNNAMED",
    "WIZARD_PREVIEW_COUNT",
    "PersonaWizardError",
    "PreviewResult",
    "build_card_from_draft",
    "preview_draft",
]
