from .knowledge import SocialItem, SocialKnowledge, _DATA_DIR, _strip_bold

__all__ = ["SocialItem", "SocialKnowledge", "_DATA_DIR"]

_default_kb: SocialKnowledge | None = None


def get_knowledge() -> SocialKnowledge:
    """全局单例（惰性加载，只读一次）。"""
    global _default_kb
    if _default_kb is None:
        _default_kb = SocialKnowledge()
    return _default_kb


def stage_for_level(level: int) -> str:
    """关系等级 → 知识阶段（供编译器查询行为边界）。"""
    if level <= 1:
        return "初遇"
    if level <= 3:
        return "升温"
    return "相处"