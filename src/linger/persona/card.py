"""人格卡校验（docs/ARCHITECTURE.md §3.1.3 Step3）。

直接让模型吐一份人格卡，八成会有内在矛盾：性格外向却用极短句、温柔却满嘴感叹号。
所以"生成"和"校验"必须是两个独立步骤，这里承担后者。

校验分两级：
    error   —— 结构不合法，拒绝入库
    warning —— 语义可疑，可自动修正或提示用户
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from pydantic import ValidationError

from ..models import PersonaCard


@dataclass
class Issue:
    field: str
    level: str  # error | warning | info
    message: str
    fixed: bool = False
    suggestion: str = ""


@dataclass
class ValidationResult:
    card: PersonaCard | None
    issues: list[Issue] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.card is not None and not any(i.level == "error" for i in self.issues)

    def warnings(self) -> list[dict[str, Any]]:
        return [
            {"field": i.field, "issue": i.message, "fixed": i.fixed, "suggestion": i.suggestion}
            for i in self.issues
            if i.level in ("warning", "info")
        ]


def validate(data: dict[str, Any], autofix: bool = True) -> ValidationResult:
    try:
        card = PersonaCard.model_validate(data)
    except ValidationError as exc:
        first = exc.errors()[0]
        return ValidationResult(None, [Issue(".".join(map(str, first["loc"])), "error", first["msg"])])

    issues: list[Issue] = []
    big5 = card.traits
    lo, hi = card.voice.avg_sentence_len

    # 内向却长篇大论
    if big5.extraversion < 0.35 and hi > 25:
        if autofix:
            card.voice.avg_sentence_len = [max(4, lo // 2), 14]
            issues.append(
                Issue("voice.avg_sentence_len", "warning",
                      f"与 extraversion={big5.extraversion} 冲突，已自动调整为 {card.voice.avg_sentence_len}",
                      fixed=True)
            )
        else:
            issues.append(Issue("voice.avg_sentence_len", "warning", "内向人格的句长偏长"))

    # 外向却只会短句
    if big5.extraversion > 0.70 and hi < 12:
        if autofix:
            card.voice.avg_sentence_len = [10, 24]
            issues.append(
                Issue("voice.avg_sentence_len", "warning",
                      f"与 extraversion={big5.extraversion} 冲突，已放宽到 {card.voice.avg_sentence_len}",
                      fixed=True)
            )

    # 高宜人性但没有禁用词表 → 容易退化成客服
    if big5.agreeableness > 0.70 and not card.voice.forbidden_phrases:
        issues.append(
            Issue("voice.forbidden_phrases", "info",
                  "高宜人性人格建议配置禁用词，否则容易滑向客服腔",
                  suggestion="加上「我完全理解你的感受」「希望对你有帮助」等")
        )

    # 表情符号与语体冲突
    if card.voice.emoji_rate > 0.5 and "书面" in card.voice.register:
        if autofix:
            card.voice.emoji_rate = 0.3
            issues.append(Issue("voice.emoji_rate", "warning", "书面语体配上高表情率很违和，已降到 0.3", fixed=True))

    # 知识边界为空 → 角色无所不知（坑 #9）
    if not card.knowledge.knows_not:
        issues.append(
            Issue("knowledge.knows_not", "warning",
                  "未声明知识边界，角色会显得无所不知，破坏沉浸感",
                  suggestion="至少填 2~3 个「她不该懂」的领域")
        )

    # 情绪基线越界
    b = card.affect.baseline
    if not (-1 <= b.pleasure <= 1):
        issues.append(Issue("affect.baseline.pleasure", "error", "P 必须在 [-1,1]"))

    return ValidationResult(card, issues)
