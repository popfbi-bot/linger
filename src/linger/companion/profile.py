"""陪伴档案：把「用户处在什么人生阶段、在关系里是谁」变成持久状态（M2-A）。

为什么要有这一层：
    每轮的 `roles` / `toxic_signals` 都是当轮推断，换个会话就归零。
    陪伴需要的是「她记得你现在处在什么阶段」——这必须是状态，不是每轮重算的文本（P1）。

三条边界（越界就会变成另一个产品）：
    · 识别是**确定性**的：关键词匹配，不用模型。判据必须可复现、能写进测试。
    · 识别**只提示、不惩罚**：档案的任何字段都不进 `unresolved_conflicts`。
    · 识别**宁可不说**：关键词没命中就留空，不猜、不默认。
"""

from __future__ import annotations

import re

from ..models import CompanionProfile, now_utc
from ..social.knowledge import SocialKnowledge

# 八个人生阶段，顺序即从早到晚（阶段冲突时取最早出现的那个）
LIFE_STAGES: tuple[str, ...] = (
    "胎儿期", "婴儿期", "幼儿期", "童年期", "青春期", "青年期", "中年期", "老年期",
)

# 阶段关键词：只收真实口语里会出现的说法。
# 刻意避开「孩子」「上学」这类裸词——单说「孩子」可能是「我还是个孩子」。
_LIFE_STAGE_KEYWORDS: dict[str, tuple[str, ...]] = {
    "胎儿期": ("怀孕", "孕期", "胎教", "产检", "待产", "胎儿", "孕妇"),
    "婴儿期": (
        "宝宝刚出生", "刚出生", "喂奶", "断奶", "婴儿", "新生儿", "尿不湿",
        "坐月子", "月子", "几个月大", "满月", "哄睡",
    ),
    "幼儿期": (
        "幼儿园", "上幼儿园", "刚会走", "学走路", "两岁", "三岁", "四岁", "宝宝上",
        "戒奶", "分床睡", "早教",
    ),
    "童年期": (
        "小学", "上小学", "一年级", "二年级", "三年级", "四年级", "五年级", "六年级",
        "孩子上小学", "家长会", "暑假作业",
    ),
    "青春期": (
        "初中", "高中", "中考", "高考", "初一", "初二", "初三", "高一", "高二", "高三",
        "早恋", "叛逆期", "青春痘", "班主任",
    ),
    "青年期": (
        "刚毕业", "大学毕业", "考研", "找工作", "刚工作", "实习", "租房", "北漂", "沪漂",
        "入职", "刚入职", "初恋", "刚恋爱", "刚结婚", "新婚", "结婚", "备婚", "婚礼",
        "选专业", "校招", "面试", "职场新人", "应届",
    ),
    "中年期": (
        "孩子上学", "孩子上小学", "孩子上初中", "孩子上高中", "孩子高考", "孩子结婚",
        "上有老下有小", "房贷", "车贷", "公司", "开会", "老板", "裁员", "升职", "体检",
        "我妈", "我爸", "婆婆", "公公", "岳父", "岳母", "带娃", "奶粉钱", "幼儿园学费",
        "离婚", "复婚", "中年",
    ),
    "老年期": (
        "退休", "退休了", "养老", "孙子", "孙女", "外孙", "外孙女", "老伴", "广场舞",
        "老年大学", "养生", "血压", "六十岁", "七十岁", "八十岁", "银发",
    ),
}

# 阶段间的年龄/身份互斥：出现高优先级的「硬证据」时，把低阶段的关键词压下去。
# 例：「退休了，天天在家带孙子」里「在家」「天天」不构成任何阶段，
#     但「孩子」二字可能被中年期抢走——所以先看谁命中更多、再看谁更晚。
_HARD_EVIDENCE = {
    "老年期": ("退休", "孙子", "孙女", "老伴", "老年大学", "广场舞"),
    "中年期": ("孩子上小学", "孩子上初中", "孩子上高中", "房贷", "我妈", "我爸"),
}


def detect_life_stage(text: str) -> str:
    """从用户这一句话识别人生阶段；识别不出来返回 ""。

    打分规则（简单到能一眼看懂，也简单到能被测试钉住）：
        1. 每个阶段累加命中关键词的长度（长词更具体，权重更高）；
        2. 出现 `_HARD_EVIDENCE` 里的硬证据时，给它所在阶段 +20；
        3. 分高者胜；并列时取**更晚**的阶段（口语里「现在」通常比「从前」更晚）。

    另有一道降噪：这句话如果不像「在说自己」就不判（见 `_is_self_narrative`）。
    """
    t = (text or "").strip()
    if not t:
        return ""
    if not _is_self_narrative(t):
        return ""

    scores: dict[str, float] = {}
    for stage, keywords in _LIFE_STAGE_KEYWORDS.items():
        score = sum(len(k) for k in keywords if k in t)
        for hard in _HARD_EVIDENCE.get(stage, ()):
            if hard in t:
                score += 20
        if score:
            scores[stage] = score

    if not scores:
        return ""
    best = max(scores.values())
    winners = [s for s, v in scores.items() if v == best]
    return max(winners, key=LIFE_STAGES.index)


# 第一人称 / 身边人 / 无主语的自我陈述。
# 中文口语大量省略主语（「刚结婚，有点累」「孩子上小学了」），
# 所以不能要求出现「我」；但也不能什么都收，否则「电影里主角退休了」会误判。
_SELF_MARKERS = ("我", "我们", "自己", "咱")
_KIN_MARKERS = (
    "孩子", "宝宝", "儿子", "女儿", "妈", "爸", "老公", "老婆", "对象",
    "婆婆", "公公", "丈母娘", "家里", "老伴", "孙子", "孙女", "外孙",
)
# 第三人称虚构语境：出现这些词时，关键词大概率不属于用户自己
_FICTION_MARKERS = ("电影", "电视剧", "小说", "剧里", "主角", "演员", "新闻里", "书里", "游戏里")


def _is_self_narrative(t: str) -> bool:
    """这句话是不是在讲「用户自己的处境」。

    三种算数：有第一人称；提到家人（「我」被省略了）；句首就是状态/动作（无主语口语）。
    一种不算：明确在讲别人的故事（电影/小说/新闻）。
    """
    if any(m in t for m in _FICTION_MARKERS) and not any(m in t for m in _SELF_MARKERS):
        return False
    if any(m in t for m in _SELF_MARKERS) or any(m in t for m in _KIN_MARKERS):
        return True
    # 无主语口语：「刚结婚」「孩子上小学」「退休了」——首字即状态，没人在讲故事
    return bool(_OPENING_MARKERS.match(t))


# 无主语口语句的开头（「刚结婚」「退休了」「孩子上小学了」）
_OPENING_MARKERS = re.compile(
    r"^(刚|刚刚|最近|现在|今天|昨天|明天|下周|上周|马上|快|已经|终于|还在|正在|"
    r"退休|结婚|离婚|怀孕|生|孩子|宝宝|儿子|女儿|妈|爸|老公|老婆|婆婆|公公)"
)


def life_stage_valid(stage: str) -> bool:
    """是不是一个合法的人生阶段名（PATCH 校正用，非法值要能 422）。"""
    return stage in LIFE_STAGES


# --------------------------------------------------------------------------- #
# 需求与风险
# --------------------------------------------------------------------------- #


def needs_for(
    life_stage: str, knowledge: SocialKnowledge | None = None, *, generic: bool = True, limit: int = 5
) -> list[str]:
    """按人生阶段取陪伴需求。

    来源是 `SocialKnowledge.companion_pitfalls(life_stage)`——**不硬编码字符串**，
    语料改了，档案跟着变。阶段为空时退回通用需求——
    但 `generic=False` 时不退：通用需求不该在「什么都没识别出来」时凭空出现，
    那会让空档案变得非空，进而让编译器白注入一段无信息量的 prompt。
    """
    kb = knowledge or _kb()
    if kb is None:
        return []
    if life_stage:
        return kb.companion_pitfalls(life_stage, limit=limit)
    if not generic:
        return []
    return (kb.companion_profiles() or kb.positive_demands())[:limit]


def risks_for(toxic_signals: list[dict] | None, knowledge: SocialKnowledge | None = None,
              limit: int = 5) -> list[str]:
    """汇总当前关系里的风险类型。

    风险来自两处：
        · 本轮 `ToxicSignalDetector` 已命中的类别（最具体，排前面）；
        · `SocialKnowledge.toxic_relation_items()` 里对应该类别的知识条目（用来解释「这是什么」）。
    没有信号就是空——不预警、不吓人。
    """
    signals = toxic_signals or []
    if not signals:
        return []
    kb = knowledge or _kb()
    out: list[str] = []
    for sig in signals:
        category = (sig or {}).get("category", "")
        if not category or category in out:
            continue
        out.append(category)
        if len(out) >= limit:
            break
    if kb is not None and out:
        # 用知识库解释风险类型，让「风险」不只是个标签
        for cat in list(out):
            items = [i for i in kb.items if i.dimension == "companion_negative" and i.toxic and cat in i.text]
            if items and len(out) < limit:
                out.append(items[0].text)
    return out[:limit]


def roles_of(detected_roles: list[dict] | None, limit: int = 4) -> list[str]:
    """本轮识别到的关系角色名（去重、保序）。"""
    out: list[str] = []
    for r in detected_roles or []:
        name = (r or {}).get("name", "")
        if name and name not in out:
            out.append(name)
        if len(out) >= limit:
            break
    return out


# --------------------------------------------------------------------------- #
# 更新入口
# --------------------------------------------------------------------------- #


def update_profile(
    repo,
    persona_id: str,
    user_id: str,
    content: str,
    detected_roles: list[dict] | None = None,
    toxic_signals: list[dict] | None = None,
    knowledge: SocialKnowledge | None = None,
) -> CompanionProfile:
    """一轮结束后更新陪伴档案，返回更新后的档案。

    **调用时机是契约**：必须与写记忆同一时机——回复落库之后（§3.3.3）。
    提前调用意味着「用户还没被回应，系统就已经给他贴了标签」。

    合并策略是「累加」而不是「覆盖」：
        阶段：本轮识别到了就更新；识别不到保留旧值（用户不会每句话都在讲自己几岁）。
        角色/需求/风险：本轮结果并进旧值，各自封顶，避免档案无限膨胀。
    """
    previous = repo.get_companion_profile(persona_id, user_id) or CompanionProfile(
        persona_id=persona_id, user_id=user_id
    )
    kb = knowledge or _kb()

    stage = detect_life_stage(content) or previous.life_stage
    # 阶段为空且没有通用需求时，needs 为空——档案保持「空」，
    # 编译器据此跳过注入（空档案注入 = 每轮浪费一段 prompt）

    profile = CompanionProfile(
        persona_id=persona_id,
        user_id=user_id,
        life_stage=stage,
        roles=_merge(roles_of(detected_roles), previous.roles, cap=4),
        needs=needs_for(stage, kb, generic=bool(stage) or previous.life_stage != ""),
        risks=_merge(risks_for(toxic_signals, kb), previous.risks, cap=5),
        updated_at=now_utc(),
    )
    repo.save_companion_profile(profile)
    return profile


def correct_life_stage(repo, persona_id: str, user_id: str, life_stage: str) -> CompanionProfile:
    """用户手动校正人生阶段（PATCH）。校正后需求跟着重算。

    人工校正优先于识别：后续轮次里识别出的阶段会再覆盖它，
    但只有识别真的命中时才会——用户说「我是中年期」而被判成青年期的概率很低。
    """
    profile = repo.get_companion_profile(persona_id, user_id) or CompanionProfile(
        persona_id=persona_id, user_id=user_id
    )
    profile.life_stage = life_stage
    profile.needs = needs_for(life_stage)
    profile.updated_at = now_utc()
    repo.save_companion_profile(profile)
    return profile


def _merge(fresh: list[str], old: list[str], cap: int) -> list[str]:
    """本轮结果在前，旧值补位，去重封顶。"""
    out: list[str] = []
    for item in list(fresh) + list(old):
        if item and item not in out:
            out.append(item)
        if len(out) >= cap:
            break
    return out


def _kb() -> SocialKnowledge | None:
    try:
        from ..social import get_knowledge

        return get_knowledge()
    except Exception:
        # 语料缺失不阻断主链路：档案退化成「只有阶段」
        return None
