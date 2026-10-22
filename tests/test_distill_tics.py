"""口癖提取的两道过滤（验收修正）。

验收时发现：跨消息重复的「行吧」被提出来了，但同时冒出一个「吧，」——
首尾挂着标点的片段。显示给用户就是「她的口癖：吧，」，很怪。

原实现只有 `_is_punct_run`（挡「全是标点」），挡不住「吧，」「，行吧」这类
**首尾挂着标点**的片段。补了 `_is_bad_edge`。

本文件钉住：
    · 口癖首尾必须是文字（能直接念出来的词）
    · 纯标点/纯空白仍被排除（原行为不回归）
    · 跨消息约束仍然生效（原判据不回归）
"""

from __future__ import annotations

import pytest

from linger.distill.fingerprint import extract_fingerprint

_PUNCT = ("，", "。", "…", "、", "！", "？", ",", ".", "!", "?", " ", "~")


def _tics(rows: list[tuple[str, str]]) -> list[str]:
    return extract_fingerprint(rows)["verbal_tics"]


# --------------------------------------------------------------------------- #
# 1. 首尾不能挂标点（本次修正）
# --------------------------------------------------------------------------- #
def test_tics_have_no_leading_or_trailing_punct():
    rows = [
        ("A", "行吧，那就这样"),
        ("A", "行吧，你说的算"),
        ("A", "行吧，随便你"),
        ("A", "好的"),
    ]
    tics = _tics(rows)
    assert "行吧" in tics
    for t in tics:
        assert not t.startswith(_PUNCT), f"口癖首字是标点：{t!r}"
        assert not t.endswith(_PUNCT), f"口癖末字是标点：{t!r}"


def test_dirty_edge_fragment_is_rejected():
    """「吧，」这种片段不该出现在口癖里。"""
    rows = [("A", "行吧，就这样吧"), ("A", "行吧，随便吧"), ("A", "行吧，算了")]
    assert "吧，" not in _tics(rows)


# --------------------------------------------------------------------------- #
# 2. 原行为不回归：纯标点不算口癖
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "rows",
    [
        [("A", "。。。。"), ("A", "。。。。。"), ("A", "！！！")],
        [("A", "……"), ("A", "……"), ("A", "……")],
        [("A", "..."), ("A", "???"), ("A", "!!!")],
    ],
)
def test_punct_only_never_becomes_tic(rows):
    """纯标点样本能算出指纹（有内容），但口癖必须为空。"""
    assert _tics(rows) == []


def test_blank_transcript_raises():
    """全是空白的记录压根没有可分析的内容——该报错，不是静默返回空口癖。

    这与「纯标点」不同：标点是内容（只是没信息量），空白不是内容。
    """
    from linger.distill.parse import TranscriptError

    with pytest.raises(TranscriptError):
        extract_fingerprint([("A", "   "), ("A", "  "), ("A", " ")])


# --------------------------------------------------------------------------- #
# 3. 原判据不回归：必须跨消息
# --------------------------------------------------------------------------- #
def test_same_message_repeat_is_not_a_tic():
    """同一条消息里连打 5 遍是打字习惯，不是口癖。"""
    rows = [
        ("A", "行吧行吧行吧行吧行吧"),
        ("A", "今天不错"),
        ("A", "明天也行"),
        ("A", "后天可以"),
    ]
    assert _tics(rows) == []


def test_cross_message_repeat_is_a_tic():
    rows = [
        ("A", "行吧，那就这样"),
        ("A", "行吧，你说的算"),
        ("A", "行吧，随便你"),
        ("A", "好的"),
    ]
    assert "行吧" in _tics(rows)


def test_two_messages_not_enough():
    """门槛是 ≥3 条不同消息。"""
    rows = [("A", "行吧，这样"), ("A", "行吧，那样"), ("A", "好吧")]
    assert "行吧" not in _tics(rows)