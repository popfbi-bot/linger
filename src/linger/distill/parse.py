"""聊天记录解析（「一键赋角」入口 C，docs/ARCHITECTURE.md §3.1.3）。

输入是一段用户粘贴的聊天记录，输出 `[(speaker, content), ...]`。

为什么这一层必须独立且宽容：
    用户贴进来的东西来自微信、Telegram、Discord、纯手打，格式一言难尽。
    解析层如果挑剔，用户只会看到「格式不对」然后走掉——他不知道自己错在哪。
    所以这里的原则是**尽力认出说话人，认不出就归给上一条的发言人**，
    真的什么都没有才报错。

四种角色标记都认（顺序即优先级）：
    名字: 内容     半角冒号（还容忍 "：" 后跟多个空格、":" 后跟 tab）
    名字：内容     全角冒号
    [名字] 内容    方括号
    名字 内容      空格分隔——兜底，最容易误判，只在没有其他标记的行上试

**为什么兜底放最后**：`我 觉得 还行` 这种句子会被空格规则切成
说话人「我」+ 内容「觉得 还行」。只有在整段里一个冒号/方括号标记都没有时，
才让这条规则生效——这样「格式混合」的脏数据不会因为一行没冒号就全体跑偏。
"""

from __future__ import annotations

import re
from typing import Literal

# 名字的长度上限。真实昵称 1~16 字，超过 20 的「名字」十有八九是句子。
NAME_MAX = 20

# `名字: 内容` / `名字：内容`
_COLON = re.compile(rf"^\s*([^:：]{{1,{NAME_MAX}}}?)\s*[:：]\s*(.*)$")
# `[名字] 内容` / `【名字】内容`
_BRACKET = re.compile(rf"^\s*[\[【]\s*([^\]】]{{1,{NAME_MAX}}}?)\s*[\]】]\s*(.*)$")
# `名字 内容`（半角空格分隔，兜底）
_SPACE = re.compile(rf"^\s*(\S{{1,{NAME_MAX}}})\s+(\S.*)$")

# 代词**可以**是名字：`我: 那你呢` 里的「我」就是用户自己（导出记录里到处都是）。
# 所以不在这里否决代词——否决它会让最常见的聊天记录格式整段失效。

# 冒号被当成正文的情况：「我觉得：这事不对」。左半边含这些词就不当名字。
_COLON_NOT_NAME_HINTS = ("觉得", "说", "想", "问", "答", "是", "的", "了", "吧", "啊", "呢", "吗")

_STYLE_COLON = "colon"
_STYLE_BRACKET = "bracket"
_STYLE_SPACE = "space"

_Style = Literal["colon", "bracket", "space"]


class TranscriptError(ValueError):
    """聊天记录不可用（空 / 不足 3 行 / 认不出任何人）。HTTP 层映射为 422。"""


def parse_transcript(transcript: str) -> list[tuple[str, str]]:
    """解析聊天记录 → [(speaker, content), ...]。

    空行跳过、首尾空白裁掉、只有名字没内容的行跳过。
    没有标记的段落归给上一条的说话人。
    """
    rows = _clean_rows(transcript or "")
    if len(rows) < 1:
        raise TranscriptError("聊天记录不能为空")

    style = _detect_style([raw for raw, _ in rows])

    lines: list[tuple[str, str]] = []
    current: str | None = None
    for raw, stripped in rows:
        marked = _split_row(raw, style)
        if marked is None:
            # 连续段落：归给上一条的说话人
            if current is None:
                # 第一句就没有说话人 → 认不出是谁，跳过（不猜）
                continue
            lines.append((current, stripped))
            continue
        speaker, content = marked
        current = speaker
        if content.strip():
            lines.append((speaker, content.strip()))
        # 只有名字没内容 → 跳过，但保留 current（下一行若没标记仍归他）
    return lines


# --------------------------------------------------------------------------- #
# 内部
# --------------------------------------------------------------------------- #
def _clean_rows(transcript: str) -> list[tuple[str, str]]:
    """裁掉首尾空白、丢掉空行。返回 (原始行, 裁后行)。

    保留原始行是因为空格分隔规则要看**行内**的空白，
    `strip()` 之后的版本看不出「名字 内容」中间隔了几个空格。
    """
    out: list[tuple[str, str]] = []
    for line in transcript.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        s = line.strip()
        if not s:
            continue
        out.append((line.rstrip(), s))
    return out


def _detect_style(raw_rows: list[str]) -> _Style:
    """整段先定一种「主标记风格」。

    混合格式（几行冒号、几行方括号）不该让任何一行被空格规则切坏：
    只要有任何冒号/方括号标记，就不用空格兜底。
    """
    has_colon = any(_COLON.match(r.strip()) and _colon_name_ok(_COLON.match(r.strip()).group(1)) for r in raw_rows)
    if has_colon:
        return _STYLE_COLON
    if any(_BRACKET.match(r.strip()) for r in raw_rows):
        return _STYLE_BRACKET
    return _STYLE_SPACE


def _colon_name_ok(name: str) -> bool:
    """「我觉得：这事不对」的左半边不是名字。

    名字很短、不含语气词、不含动词。这条规则挡住最常见的误判。
    """
    name = name.strip()
    if not name or " " in name:
        return False
    return not any(h in name for h in _COLON_NOT_NAME_HINTS)


def _split_row(raw: str, style: _Style) -> tuple[str, str] | None:
    """按主风格拆一行；拆不出来返回 None（= 这是连续段落）。"""
    s = raw.strip()

    if style == _STYLE_COLON:
        m = _COLON.match(s)
        if m and _colon_name_ok(m.group(1)):
            return m.group(1).strip(), m.group(2)
        return None

    if style == _STYLE_BRACKET:
        m = _BRACKET.match(s)
        if m:
            return m.group(1).strip(), m.group(2)
        return None

    m = _SPACE.match(s)
    if m and _space_name_ok(m.group(1), m.group(2)):
        return m.group(1).strip(), m.group(2)
    return None


def _space_name_ok(name: str, rest: str) -> bool:
    """空格兜底的名字判定。宁可漏认（归给上一位），不要错认。

    两条否决：
      · 名字里带标点 —— 「行吧，你先忙」前半段不是名字
      · 名字以语气词结尾 —— 「真的 吗」不是「真的」在说话

    刻意**不**否决代词：`我 那你呢` 里的「我」就是用户自己，
    真实导出记录里这个词出现得比任何昵称都多。误判成「我」说的，
    比认不出来更接近真相。
    """
    name = name.strip()
    if not name:
        return False
    if any(ch in name for ch in "，,。…！!？?、；;~～"):
        return False
    if name[-1] in "的了吧啊呢吗呀哦嘛":
        return False
    return bool(rest.strip())


__all__ = ["NAME_MAX", "TranscriptError", "parse_transcript"]
