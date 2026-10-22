"""陪伴档案（M2-A）：让 AI 记得「用户处在什么人生阶段、在关系里是谁」。"""

from .profile import (
    LIFE_STAGES,
    correct_life_stage,
    detect_life_stage,
    life_stage_valid,
    needs_for,
    risks_for,
    roles_of,
    update_profile,
)

__all__ = [
    "LIFE_STAGES",
    "correct_life_stage",
    "detect_life_stage",
    "life_stage_valid",
    "needs_for",
    "risks_for",
    "roles_of",
    "update_profile",
]
