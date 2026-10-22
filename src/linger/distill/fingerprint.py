"""风格指纹（确定性，无 LLM）。

这是入口 C 的**核心资产**：从真实文本统计出「这个人怎么说话」。

为什么必须是确定性算法，而不是让模型总结：
    模型说「这个人说话短」是模糊的、不可复核的、每次问都不一样。
    统计出来的「句长中位数 12、emoji 率 0.08、口癖『行吧』出现在 7 条消息里」
    是事实——用户能拿它对着自己的聊天记录核对，也能看出算法是不是错了。
    推断性的东西（身份、性格、价值观）才交给模型，见 `compose.py`。

输出直接拼成 `models.Voice`，所以**字段名就是契约**，不能随手改名。

最易错的一处是 `verbal_tics`：按字频取会选出「的」「了」和打字习惯
（同一条消息里连打 10 遍的口头重复）。所以这里要求**跨消息重复**——
一个片段必须出现在 ≥3 条**不同**的消息里才算口癖。
"""

from __future__ import annotations

from collections import Counter
from typing import Any

# 句长区间的宽容系数：下界 0.7、上界 1.6，再夹到 [3, 40]。
# 为什么不直接用中位数当上下界：人说话的长度分布很散，
# 给 LLM 一个点会让它每句都写成同一个长度；给一个区间才像人。
_LEN_LO_FACTOR = 0.7
_LEN_HI_FACTOR = 1.6
_LEN_FLOOR, _LEN_CEIL = 3, 40
# 上界的下限：太窄的区间（如 [3, 4]）会让模型说不出完整句子。
_LEN_HI_MIN = 8

_TIC_MIN, _TIC_MAX = 2, 4  # 口癖片段长度
_TIC_MIN_MESSAGES = 3  # 必须出现在 ≥3 条不同消息里
_TIC_TOP = 5

_WORD_MIN_LEN = 2
_WORD_TOP = 10
_WORD_MIN_COUNT = 3

_EMOJI_TOP = 5

# 标点收尾判据
_ELLIPSIS_MIN = 0.30
_PERIOD_MIN = 0.60

# 语体判据：含这些字就是口语
_ORAL_CHARS = ("的", "了", "吧", "啊", "呢")


def extract_fingerprint(lines: list[tuple[str, str]], target: str | None = None) -> dict[str, Any]:
    """从 [(speaker, content), ...] 算出目标对象的语体指纹。

    `target` 为 None 时取「发言条数最多的一方」——这是「我不知道该复刻谁」的默认答案：
    对面那个人通常话更多，也通常是用户想复刻的人。

    返回的 dict 可以直接喂给 `Voice(**fp)`（键名与 `Voice` 字段一致），
    但 `emoji_rate` 之外还带 `punctuation_policy` / `register` 等派生描述。
    """
    from .parse import TranscriptError

    if not lines:
        raise TranscriptError("聊天记录里没有可以分析的发言")

    speaker = target or dominant_speaker(lines)
    texts = [content for who, content in lines if who == speaker and content.strip()]
    if not texts:
        raise TranscriptError(f"聊天记录里没有「{speaker}」的发言")

    return {
        "avg_sentence_len": _sentence_len(texts),
        "emoji_rate": _emoji_rate(texts),
        "emoji_set": _emoji_set(texts),
        "verbal_tics": _verbal_tics(texts),
        "high_freq_words": _high_freq_words(texts),
        "punctuation_policy": _punctuation_policy(texts),
        "register": _register(texts),
    }


def dominant_speaker(lines: list[tuple[str, str]]) -> str:
    """发言条数最多的一方。同数取先出现的（顺序即用户的叙述习惯）。"""
    counts: Counter[str] = Counter()
    order: list[str] = []
    for who, content in lines:
        if not content.strip():
            continue
        if who not in counts:
            order.append(who)
        counts[who] += 1
    if not counts:
        from .parse import TranscriptError

        raise TranscriptError("聊天记录里没有可以分析的发言")
    best = max(counts.values())
    for who in order:
        if counts[who] == best:
            return who
    return order[0]


def speakers(lines: list[tuple[str, str]]) -> list[str]:
    """出现过的说话人（按首次出现顺序）。前端/测试用它做展示与断言。"""
    order: list[str] = []
    for who, _ in lines:
        if who not in order:
            order.append(who)
    return order


# --------------------------------------------------------------------------- #
# 各项算法
# --------------------------------------------------------------------------- #
def _sentence_len(texts: list[str]) -> list[int]:
    """字符长度中位数 → [max(3, m*0.7), min(40, max(8, m*1.6))]。"""
    lengths = sorted(len(t.strip()) for t in texts)
    n = len(lengths)
    if n == 0:
        return [8, 20]
    mid = n // 2
    median = lengths[mid] if n % 2 else (lengths[mid - 1] + lengths[mid]) / 2
    lo = max(_LEN_FLOOR, int(median * _LEN_LO_FACTOR))
    hi = min(_LEN_CEIL, max(_LEN_HI_MIN, int(median * _LEN_HI_FACTOR)))
    # 下界也要夹到上界以内。超长消息时 lo 会超过 _LEN_CEIL（中位数 200 → 140），
    # 靠 R3 交换救回来只是碰巧——指纹自己就必须给出合法区间，
    # 不能依赖下游某条规则的执行顺序。
    lo = min(lo, hi)
    return [lo, hi]


def _emoji_rate(texts: list[str]) -> float:
    hit = sum(1 for t in texts if _emojis_in(t))
    return round(hit / len(texts), 2)


def _emoji_set(texts: list[str]) -> list[str]:
    counter: Counter[str] = Counter()
    for t in texts:
        counter.update(_emojis_in(t))
    # 按出现次数降序；同次数保持首次出现顺序（Counter 的插入序即稳定排序）
    return [e for e, _ in counter.most_common(_EMOJI_TOP)]


def _emojis_in(text: str) -> list[str]:
    return [ch for ch in text if is_emoji(ch)]


def is_emoji(ch: str) -> bool:
    """纯标准库判码位，不引第三方库。

    区间取「常见 emoji 面」：
        0x2600~0x27BF  杂项符号与装饰符（☀ ✂ ✅ ✨ ❤ 等）
        0x1F300~0x1FAFF 表情/交通/补充符号等主面（😀🌿🚀🤝 等）
        0xFE0F          variation selector（把 ☀ 变成 emoji 的那个尾巴——单独出现不算）
    刻意不覆盖 0x2190~0x21FF（←→↑↓）：那是箭头，不是表情。
    """
    cp = ord(ch)
    return 0x1F300 <= cp <= 0x1FAFF or 0x2600 <= cp <= 0x27BF


def _verbal_tics(texts: list[str]) -> list[str]:
    """口癖：长度 2~4 的片段，出现在 ≥3 条**不同**消息里。

    三步：
      1. 每条消息单独切 2~4 字滑窗，得到「这条消息里有这个片段」的集合；
      2. 统计片段在**多少条消息**里出现过（不是出现多少次）；
      3. 按「消息数」降序，同数按「总频次」降序，取前 5。

    第 2 步是重点：同一条消息里重复 10 次，消息数仍然是 1，
    所以进不了口癖——那是打字习惯（或者复读），不是口癖。
    """
    if len(texts) < _TIC_MIN_MESSAGES:
        return []

    msg_counts: Counter[str] = Counter()  # 出现在多少条消息里
    total: Counter[str] = Counter()  # 总出现次数（仅用于排序的次级键）

    for text in texts:
        clean = _strip_emoji(text)
        seen_here: set[str] = set()
        for n in range(_TIC_MIN, _TIC_MAX + 1):
            for i in range(len(clean) - n + 1):
                gram = clean[i : i + n]
                if not gram.strip() or _is_punct_run(gram) or _is_bad_edge(gram):
                    continue
                seen_here.add(gram)
                total[gram] += 1
        for gram in seen_here:
            msg_counts[gram] += 1

    candidates = [
        (gram, cnt) for gram, cnt in msg_counts.items() if cnt >= _TIC_MIN_MESSAGES
    ]
    candidates.sort(key=lambda kv: (-kv[1], -total[kv[0]], kv[0]))

    picked: list[str] = []
    for gram, _ in candidates:
        # 同一片段不重复；且不选已被选中片段包含的子串（「行吧」与「行吧你」只留长的那个）
        if any(gram in p or p in gram for p in picked):
            continue
        picked.append(gram)
        if len(picked) >= _TIC_TOP:
            break
    return picked


def _strip_emoji(text: str) -> str:
    return "".join(ch for ch in text if not is_emoji(ch))


def _is_punct_run(gram: str) -> bool:
    """纯标点片段不算口癖（「。。。」「……」是打字习惯）。"""
    return all((not ch.isalnum()) and not _is_cjk(ch) for ch in gram)


def _is_bad_edge(gram: str) -> bool:
    """首尾是标点或空白的片段不算口癖。

    `_is_punct_run` 只挡「全是标点」，挡不住「吧，」「，行吧」——
    这类片段首尾挂着标点，提取出来显示给用户会很怪（「她的口癖：吧，」）。
    口癖必须是「能直接念出来的词」，所以首尾都得是文字。
    """
    if not gram:
        return True

    def _text(ch: str) -> bool:
        return ch.isalnum() or _is_cjk(ch)

    return (
        gram[0].isspace()
        or gram[-1].isspace()
        or not _text(gram[0])
        or not _text(gram[-1])
    )


def _is_cjk(ch: str) -> bool:
    return "\u4e00" <= ch <= "\u9fff"


def _high_freq_words(texts: list[str]) -> list[str]:
    """高频词：中文 2~4 字滑窗，英文按空白切；出现 ≥3 次，取前 10。

    与口癖的区别是**门槛在次数不在消息数**：
    「高频词」回答「他老说哪些词」，「口癖」回答「哪句话是他的标签」。
    两者共用滑窗，但口癖多了跨消息约束，所以口癖通常更短更具体。
    """
    counter: Counter[str] = Counter()
    for text in texts:
        clean = _strip_emoji(text)
        # 英文/数字：按空白与标点切
        for token in _ascii_tokens(clean):
            if len(token) >= _WORD_MIN_LEN:
                counter[token.lower()] += 1
        # 中文：2~4 字滑窗
        for n in (2, 3, 4):
            for i in range(len(clean) - n + 1):
                gram = clean[i : i + n]
                if all(_is_cjk(ch) for ch in gram):
                    counter[gram] += 1

    words = [(w, c) for w, c in counter.items() if c >= _WORD_MIN_COUNT]
    words.sort(key=lambda kv: (-kv[1], -len(kv[0]), kv[0]))
    return [w for w, _ in words[:_WORD_TOP]]


def _ascii_tokens(text: str) -> list[str]:
    out: list[str] = []
    buf: list[str] = []
    for ch in text:
        if ch.isascii() and (ch.isalnum() or ch in "'-_"):
            buf.append(ch)
        else:
            if buf:
                out.append("".join(buf))
                buf = []
    if buf:
        out.append("".join(buf))
    return out


def _punctuation_policy(texts: list[str]) -> str:
    """一句话描述标点习惯。三条规则，顺序即优先级。"""
    n = len(texts)
    if n == 0:
        return "标点随意"
    ellipsis = sum(1 for t in texts if t.rstrip().endswith(("…", "...")))
    period = sum(1 for t in texts if t.rstrip().endswith("。"))
    if ellipsis / n >= _ELLIPSIS_MIN:
        return "常以省略号收尾"
    if period / n >= _PERIOD_MIN:
        return "句子收得干脆"
    return "标点随意"


def _register(texts: list[str]) -> str:
    """口语 / 书面。含「的了吧啊呢」任一 → 口语。"""
    joined = "".join(texts)
    return "口语" if any(ch in joined for ch in _ORAL_CHARS) else "书面"


__all__ = ["dominant_speaker", "extract_fingerprint", "is_emoji", "speakers"]
