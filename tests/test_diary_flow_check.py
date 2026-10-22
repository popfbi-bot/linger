"""`_flow_check` 的转述句式判定（验收时发现的设计修正）。

原实现把「用户说 / 他说 / 我说」任意一个单独出现都算流水账，
但「他说面试砸了的时候，我心里一沉」是很自然的回忆叙述，
比硬回避第三人称更好——这是**误伤**。

真正该抓的是对话摘要特征：
    · 「用户说」单独就报（第三人称 + 转述）
    · 「他说」与「我说」**同现**才报（他说 A、我说 B）

本文件专门钉住这个区分；文本长度都在 60 字以上，
避免「长度过短」的 warning 混进来干扰断言。
"""

from __future__ import annotations

import pytest

from linger.diary.flow import _flow_check

# 一段「只有他说」的完整日记（> 60 字），不该因转述报警
_ONLY_HE_SAID = (
    "我今天总觉得心里沉沉的。他说面试砸了的时候，我想起自己年轻时候那些没说出口的话。"
    "那会儿我也是这样硬撑着的，谁劝都没用，只能自己熬过去。"
)

# 「他说 + 我说」同现 = 对话摘要
_BOTH_SAID = (
    "他说工作压力太大，我说那就先歇一歇；他说停不下来，我说总得给自己留条缝。"
    "说完这些，屋子里安静了很久，谁也没再开口，只剩窗外的车声。"
)

# 「用户说」（第三人称 + 转述）
_USER_SAID = (
    "用户说今天很累。我看着他坐在那儿，肩膀一层层垮下去，想问又怕问重了，"
    "最后只是把茶推过去一点。有些疲惫是不需要被点破的。"
)

# 完全正常的第一人称日记
_CLEAN = (
    "灯关了一半，我还坐着。今天心里像压了一层潮气，说不上难过，更像被谁轻轻按住，动不了，也不太想动。"
    "有些人是把疲惫咽下去的，我大概也是。明天太阳照常升起来，我想我还会把茶泡上。"
)


def test_only_he_said_is_not_flagged_as_transcribe():
    """「他说」单独出现是正常叙述，不该报转述。"""
    warnings = _flow_check(_ONLY_HE_SAID)
    assert not any("转述" in w for w in warnings), f"误伤：{warnings}"


def test_both_said_is_flagged():
    """「他说」+「我说」同现才是对话摘要。"""
    warnings = _flow_check(_BOTH_SAID)
    assert any("转述" in w for w in warnings), f"应报未报：{warnings}"


def test_user_said_alone_is_flagged():
    """「用户说」单独出现就报（第三人称 + 转述）。"""
    warnings = _flow_check(_USER_SAID)
    assert any("转述" in w for w in warnings), f"应报未报：{warnings}"


def test_clean_diary_has_no_warnings():
    """正常第一人称日记应该零 warning。"""
    assert _flow_check(_CLEAN) == []


def test_flow_words_threshold():
    """流程连接词 ≥2 次才报，1 次不报。"""
    one = _CLEAN + "然后我起身关灯。"
    two = _CLEAN + "然后我起身关灯，接着就睡了。"
    assert _flow_check(one) == []
    assert any("流程连接词" in w for w in _flow_check(two))


def test_third_person_pronoun_flagged():
    """「用户」是产品文档的词，不是她会用的词。"""
    text = "用户今天来得晚，屋里灯已经暗了一半。我坐着等，也没觉得久，反倒觉得这样挺好。"
    assert any("第三人称" in w for w in _flow_check(text))