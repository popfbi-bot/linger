"""事件评估器：把一句用户输入翻译成情绪冲击向量。

生产环境这里应该换成小模型（结构化输出 JSON），见 §3.6.4 Pass 1。
骨架阶段用规则兜底，好处是**确定性**——评测可复现，不会因为模型抽样而抖动。
"""

from __future__ import annotations

import re

from ..models import Impact

_PRAISE = ("你真好", "谢谢你", "喜欢你", "厉害", "温柔", "懂我", "有你真好", "说得对")
_CRITICISM = ("你怎么这样", "无聊", "烦", "没用", "讨厌", "不想聊了", "假", "敷衍")
_NEGLECT = ("哦", "嗯", "随便", "算了", "没事", "不知道")
_INTIMACY = ("想你", "抱抱", "晚安", "爱你", "在吗", "陪我")
_NEG_EVENT = ("难过", "累", "崩溃", "哭", "压力", "失业", "分手", "病了", "砸了", "没戏", "失眠")
_POS_EVENT = ("升职", "开心", "考上了", "好消息", "加薪", "通过了", "好起来")


def _hits(text: str, words: tuple[str, ...]) -> int:
    return sum(1 for w in words if w in text)


def evaluate(user_text: str) -> list[Impact]:
    t = (user_text or "").strip()
    if not t:
        return []

    impacts: list[Impact] = []
    n = len(t)

    # 短促回复 → 被冷落感（人格敏感度高时更明显）
    if n <= 3 and _hits(t, _NEGLECT):
        impacts.append(Impact(kind="neglect", dp=-0.06, da=-0.05, dd=-0.03, salience=0.4))

    if _hits(t, _PRAISE):
        w = min(_hits(t, _PRAISE), 2) / 2
        impacts.append(Impact(kind="praise", dp=0.12 + 0.06 * w, da=0.08, dd=0.03, salience=0.5 + 0.2 * w))

    if _hits(t, _CRITICISM):
        impacts.append(Impact(kind="criticism", dp=-0.20, da=0.16, dd=-0.05, salience=0.8))

    if _hits(t, _INTIMACY):
        impacts.append(Impact(kind="intimacy", dp=0.10, da=0.10, dd=0.02, salience=0.6))

    neg = _hits(t, _NEG_EVENT)
    pos = _hits(t, _POS_EVENT)
    if neg:
        salience = min(0.95, 0.45 + 0.15 * neg + min(n, 120) / 400)
        impacts.append(Impact(kind="event", dp=-0.16 - 0.05 * neg, da=0.10, dd=-0.04, salience=salience))
    if pos:
        salience = min(0.9, 0.45 + 0.15 * pos)
        impacts.append(Impact(kind="event", dp=0.16 + 0.05 * pos, da=0.12, dd=0.05, salience=salience))

    return impacts


def is_deep_turn(user_text: str) -> bool:
    """是否算一次「深度对话」——关系升级的并集条件之一（§13.3 修正 1）。"""
    return len(user_text or "") > 120 or bool(_hits(user_text, _NEG_EVENT))


def is_self_disclosure(user_text: str) -> bool:
    """用户是否在分享私事（家庭、身体、情感、工作困境）。"""
    markers = ("我妈", "我爸", "我女朋友", "我男朋友", "我老婆", "我老公", "我孩子", "我身体", "我家里")
    return bool(_hits(user_text, markers)) or bool(re.search(r"我(以前|小时候|其实)", user_text or ""))
