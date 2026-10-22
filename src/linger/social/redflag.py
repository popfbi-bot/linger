"""红灯检测：把用户这一句话，和当前关系阶段的负面清单做碰撞。

为什么需要它：
    知识库目前只被编译器消费（喂给模型一段「别做什么」），
    但关系引擎本身还是聋的——用户一句话踩了雷，`unresolved_conflicts` 纹丝不动。
    docs/ARCHITECTURE.md §3.4 说得很清楚：未消解的冲突会压低亲密度。
    所以把「用户做错了什么」这件事，从文本变成 tick() 的一个入参。

匹配策略（骨架阶段，刻意朴素）：
    语料条目是自然语言长句（「刚认识就打听对方收入」），用户输入是短句（「你月薪多少」）。
    两边都没有可直接对齐的词面，所以走两步：
        1. 从条目里抽「行为关键词」（收入、打量、前任……），用户输入命中即算擦边；
        2. 真正的红灯要求「擦边 + 该条目的动作动词也出现」，避免把「我前任的事我早放下了」这种
           正常倾诉误判成打探前任。
    不做语义模型：判据必须确定、可复现、能写进测试。
"""

from __future__ import annotations

from dataclasses import dataclass

from .knowledge import SocialKnowledge

# 条目 → 行为关键词。只收录「一出现就说明大概率在干这件事」的词。
# 顺序即优先级：同一个词的多个触发点只报一次。
_BEHAVIOR_KEYWORDS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("打探收入", ("收入", "工资", "月薪", "年薪", "挣多少", "赚多少", "多少钱一个月", "存款")),
    ("外貌评判", ("身材", "打量", "胖", "瘦", "体重", "脸好看", "长得", "颜值", "腿")),
    ("打探前任", ("前任", "前女友", "前男友", "以前的对象", "谈过几个")),
    ("越界身体接触", ("蹭过去", "搂腰", "抱一下", "摸头", "牵手", "凑太近", "靠近点", "碰你")),
    ("查户口式提问", ("几岁", "年龄", "哪里人", "住哪", "做什么工作", "什么学校", "户口")),
    ("深夜打扰", ("凌晨", "三点", "半夜", "打电话给我", "还没睡吗")),
    ("敷衍冷淡", ("随便", "都行吧", "哦", "嗯嗯", "懒得", "无所谓")),
    ("空头承诺", ("一定", "保证", "下次带你去", "改天约", "说好了", "发誓", "绝不")),
    ("情绪倒垃圾", ("负能量", "全世界都", "没意思", "活着", "全怪")),
    ("轻视贬低", ("你不懂", "幼稚", "就你会", "有什么用", "别瞎想", "想多了")),
)

# 动作意图：出现这些词，说明他在「做」而不是在「提到」
_ACTION_VERBS = (
    "问", "打听", "查", "说", "嫌", "评论", "评价", "碰", "摸", "搂", "抱", "盯", "看",
    "保证", "发誓", "要求", "陪", "比", "扫", "嫌", "骂", "嘲",
)

# 疑问语气：直接提问本身就是动作（「你月薪多少」没有动词，但显然在打听）
_QUESTION_MARKERS = ("?", "？", "多少", "几个", "几岁", "哪里", "什么工作", "是不是", "有没有")

# 用户侧的降噪：这些词出现时，说明他在「反思/复述」而不是在做
_REFLECTIVE_MARKERS = ("我是不是", "会不会", "是不是不该", "我错了", "对不起", "我该不该")


@dataclass(frozen=True)
class Hit:
    """一次命中。category 用于聚合，text 是命中的知识库条目原文。"""

    category: str
    text: str
    keyword: str

    @property
    def brief(self) -> str:
        return f"{self.category}（触发：{self.keyword}）"


class RedFlagDetector:
    """用户输入 × 阶段负面清单 → 命中的红灯清单。"""

    def __init__(self, knowledge: SocialKnowledge | None = None) -> None:
        self.knowledge = knowledge

    # ------------------------------------------------------------------ #
    def _kb(self) -> SocialKnowledge | None:
        if self.knowledge is not None:
            return self.knowledge
        try:
            from . import get_knowledge

            return get_knowledge()
        except Exception:
            # 语料缺失不阻断主链路：检测退化为「永不命中」
            return None

    def detect(self, text: str, level: int = 0, all_stages: bool = False) -> list[Hit]:
        """返回命中的红灯。空列表表示这一轮没有踩雷。

        level：当前关系等级（用它映射到阶段，只查该阶段的红线）。
        all_stages：忽略阶段，查全部负面清单（调试 / 评测用）。
        """
        t = (text or "").strip()
        if not t:
            return []

        kb = self._kb()
        if kb is None:
            return []

        from . import stage_for_level

        stage = stage_for_level(level)
        if all_stages:
            items = [i for i in kb.items if i.red_flag]
        else:
            items = [i for i in kb.by_stage(stage) if i.red_flag]
            if not items:
                # 该阶段没有负面条目（语料只覆盖了部分阶段）时，下探到全部负面清单：
                # 「升温期用户越界」不该因为语料没标注就免罚。
                items = [i for i in kb.items if i.red_flag]
        if not items:
            return []

        reflective = any(m in t for m in _REFLECTIVE_MARKERS)
        hits: list[Hit] = []
        seen: set[str] = set()

        acted = self._has_action(t)
        for category, keywords in _BEHAVIOR_KEYWORDS:
            fired = [k for k in keywords if k in t]
            if not fired:
                continue
            entry = self._best_entry(items, keywords)
            if entry is None:
                continue
            if not acted:
                # 只是提到，没有动作意味：可能是无害提及，不算红灯
                continue
            if reflective:
                # 「我是不是不该问你的收入」——他在道歉，不是在打探
                continue
            if category in seen:
                continue
            seen.add(category)
            hits.append(Hit(category=category, text=entry, keyword=fired[0]))

        return hits

    # ------------------------------------------------------------------ #
    def _best_entry(self, items: list, keywords: tuple[str, ...]) -> str | None:
        """在该阶段负面条目里挑一条和最相关（关键词重合最多）的，作为命中依据。"""
        best: str | None = None
        best_score = 0
        for item in items:
            score = sum(1 for k in keywords if k in item.text)
            if score > best_score:
                best, best_score = item.text, score
        return best

    @staticmethod
    def _has_action(text: str) -> bool:
        return any(v in text for v in _ACTION_VERBS) or RedFlagDetector._is_question(text)

    @staticmethod
    def _is_question(text: str) -> bool:
        return any(m in text for m in _QUESTION_MARKERS)

    # ------------------------------------------------------------------ #
    def is_conflict(self, text: str, level: int = 0) -> bool:
        """给 tick() 用的布尔口。一轮里命中一条就算这一轮有冲突。"""
        return bool(self.detect(text, level))


_default_detector: RedFlagDetector | None = None


def get_detector() -> RedFlagDetector:
    global _default_detector
    if _default_detector is None:
        _default_detector = RedFlagDetector()
    return _default_detector


def detect_red_flags(text: str, level: int = 0) -> list[Hit]:
    """模块级快捷方式。"""
    return get_detector().detect(text, level)


__all__ = ["Hit", "RedFlagDetector", "detect_red_flags", "get_detector"]
