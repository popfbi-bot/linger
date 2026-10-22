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


# --------------------------------------------------------------------------- #
# 有毒关系 / 陪伴缺失 信号识别（只提示，不惩罚）
# --------------------------------------------------------------------------- #
# 这份通道和红灯检测是两回事：
#   红灯检测：用户做了越界行为 → 关系降级（用户有错）
#   有毒信号：用户正在经历伤害（被消耗/被贬低/被忽视）→ AI 需要识别并共情
# 因此 toxic 信号绝不计入 unresolved_conflicts，不降低关系。
# 它只作为 orchestrator 的提示，让 AI 的回应更有洞察。

# 陪伴缺失 / 有毒关系的行为关键词 → 类型标签
# 覆盖用户倾诉时的真实口语表达，而不只是语料原文。
# 精度优先：宁可漏报，不可误报（误报会让 AI 过度解读普通倾诉）。
_TOXIC_KEYWORDS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("情感勒索", ("都是为了你", "翅膀硬了", "不听我的", "不帮我", "不够意思", "不怎样就是不爱我", "你要是", "你就不是")),
    ("否定打压", ("骂我没用", "你没用", "没人要你", "离开我没人要", "没出息", "嫌弃", "不满意", "PUA", "pua", "打压", "贬低", "羞辱", "自卑", "你这样的", "也就我要你")),
    ("控制", ("查你手机", "查你定位", "管你穿", "跟谁玩", "花什么钱", "必须听我的", "不许", "不准", "限制我", "翻我")),
    ("冷暴力", ("不回消息", "消失", "冷战", "不沟通", "冷暴力", "不理我", "已读不回", "玩消失")),
    ("消耗", ("情绪垃圾桶", "吸干", "总是更累", "更丧", "抱怨", "倾诉", "负能量")),
    ("忽视", ("没人陪", "一个人去医院", "没人说话", "没人可以说话", "被遗忘", "独自过节", "一个人吃", "没人懂", "翻遍通讯录", "找不到人", "一个人", "没人", "没人在", "空落落")),
    ("背叛", ("背后捅刀", "抢你机会", "挖你墙脚", "转头告诉别人", "落井下石", "背叛", "出卖")),
    ("剥削", ("利用你", "你有用", "帮忙", "随叫随到", "工具人", "利用我", "当工具")),
    ("担子过重", ("顶梁柱", "全指望你", "出人头地", "房贷车贷", "不敢倒下", "全家的", "喘不过气", "压力好大")),
    ("羞耻孤独", ("怕受伤", "不敢说", "丢人", "失败", "没人可以依靠", "不敢告诉", "报喜不报忧")),
)

# 用户描述自己（或亲近的人）正在经历的信号词——「我」「他」「她」「我妈」「我爸」等
_SELF_REFERENCE = ("我", "他", "她", "他们", "我妈", "我爸", "朋友", "同事", "对象", "老公", "老婆", "孩子")

# 求助 / 反思信号：出现时说明用户正在寻求理解，必须识别（而不是降噪）
_TOXIC_HELPING = ("怎么办", "怎么面对", "怎么离开", "该不该", "帮我", "救救我", "好难过", "好累", "受不了", "好痛苦", "快撑不住", "撑不下去", "好孤独", "好孤单")

# 强孤独信号：中文口语常省略主语（「一个人在医院」「没人说话」默认主语是「我」）。
# 出现这些词时，即使没有显式「我/他/她」，也视为第一人称经历。
_STRONG_LONELINESS = ("一个人", "没人", "没人在", "没人陪", "没人懂", "翻遍通讯录", "找不到人", "独自", "孤零零")


@dataclass(frozen=True)
class ToxicSignal:
    """一次有毒关系 / 陪伴缺失信号命中。只用于提示，不用于关系惩罚。"""

    category: str       # 情感勒索 / 否定打压 / 控制 / 冷暴力 / 消耗 / 忽视 / 背叛 / 剥削 / 担子过重 / 羞耻孤独
    text: str           # 命中的知识库条目原文
    keyword: str        # 触发的关键词

    @property
    def brief(self) -> str:
        return f"{self.category}（信号：{self.keyword}）"


class ToxicSignalDetector:
    """从用户倾诉中识别「有毒关系 / 陪伴缺失」信号。

    与 RedFlagDetector 的边界：
        RedFlagDetector   → 用户做错了什么（进关系惩罚）
        ToxicSignalDetector → 用户正在经历什么（只提示，不进关系惩罚）
    """

    def __init__(self, knowledge: SocialKnowledge | None = None) -> None:
        self.knowledge = knowledge

    def _kb(self) -> SocialKnowledge | None:
        if self.knowledge is not None:
            return self.knowledge
        try:
            from . import get_knowledge
            return get_knowledge()
        except Exception:
            return None

    def detect(self, text: str, limit: int = 3) -> list[ToxicSignal]:
        """返回命中的有毒信号。空列表表示没有识别到明显信号。

        只做识别提示：调用方（orchestrator）可以把这些信号作为上下文注入，
        但绝不能把它们计入关系惩罚。

        精度优先：
            - 第一人称/亲近者经历（我、我妈、他……）才识别
            - 求助信号（怎么办/好难过/快撑不住）出现时优先报，AI 必须接住
        """
        t = (text or "").strip()
        if not t:
            return []
        kb = self._kb()
        if kb is None:
            return []

        # 必须是「第一人称/亲近者经历」的描述，而不是在评论新闻。
        # 中文口语常省略主语，所以满足以下任一即视为第一人称：
        #   1. 显式自指词（我/我妈/他/朋友…）
        #   2. 求助信号（好难过/快撑不住/怎么办…）
        #   3. 强孤独信号（一个人/没人/独自…）
        self_ref = any(s in t for s in _SELF_REFERENCE)
        helping = any(m in t for m in _TOXIC_HELPING)
        lonely = any(m in t for m in _STRONG_LONELINESS)
        if not (self_ref or helping or lonely):
            return []

        signals: list[ToxicSignal] = []
        seen: set[str] = set()
        for category, keywords in _TOXIC_KEYWORDS:
            fired = [k for k in keywords if k in t]
            if not fired:
                continue
            entry = self._best_entry(kb, category, keywords)
            if entry is None:
                continue
            if category in seen:
                continue
            seen.add(category)
            signals.append(ToxicSignal(category=category, text=entry, keyword=fired[0]))
            if len(signals) >= limit:
                break

        # 求助信号但关键词没命中：降级报一个「忽视/孤独」类，避免 AI 完全没接住
        if not signals and helping:
            entry = self._best_entry(kb, "忽视", ("没人", "一个人", "孤独"))
            if entry:
                signals.append(ToxicSignal(category="求助陪伴", text=entry, keyword="需要陪伴"))
        return signals

    @staticmethod
    def _best_entry(kb: SocialKnowledge, category: str, keywords: tuple[str, ...]) -> str | None:
        """在 companion_negative 维度里挑一条和关键词重合最多的条目。"""
        items = [i for i in kb.items if i.dimension == "companion_negative"]
        if not items:
            return None
        best: str | None = None
        best_score = 0
        for item in items:
            score = sum(1 for k in keywords if k in item.text)
            if score > best_score:
                best, best_score = item.text, score
        return best


_default_detector: RedFlagDetector | None = None


def get_detector() -> RedFlagDetector:
    global _default_detector
    if _default_detector is None:
        _default_detector = RedFlagDetector()
    return _default_detector


def detect_red_flags(text: str, level: int = 0) -> list[Hit]:
    """模块级快捷方式。"""
    return get_detector().detect(text, level)


_default_toxic: ToxicSignalDetector | None = None


def get_toxic_detector() -> ToxicSignalDetector:
    global _default_toxic
    if _default_toxic is None:
        _default_toxic = ToxicSignalDetector()
    return _default_toxic


def detect_toxic_signals(text: str, limit: int = 3) -> list[ToxicSignal]:
    """模块级快捷方式。"""
    return get_toxic_detector().detect(text, limit)


__all__ = [
    "Hit",
    "RedFlagDetector",
    "detect_red_flags",
    "get_detector",
    "ToxicSignal",
    "ToxicSignalDetector",
    "detect_toxic_signals",
    "get_toxic_detector",
]
