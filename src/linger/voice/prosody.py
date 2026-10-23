"""情绪 → 韵律参数，以及「怎么量她的句长」。

两个纯函数，没有 IO、没有随机、没有 provider——所以它们能被单独测，
也能被 mock provider 记下来断言。

**为什么韵律要绕这么一圈，而不是直接把 PAD 丢给 provider**：
不同 provider 对韵律的理解完全不同（一家要 `rate=0.9`，另一家要 `speed=90%`）。
把「PAD → rate/pitch/energy」这一步留在我们这侧，接新 provider 时只做一层
「统一参数 → 它家参数」的映射，情绪到音色的对照表只有一份。
"""

from __future__ import annotations

from ..models import PAD

# 基准值：arousal 中间、pleasure 中性的「平静说话」。
# 上下界刻意收在 ±0.25 里——人不会因为一句话心情好就变成播音腔。
RATE_BASE = 1.0
RATE_SPAN = 0.50
PITCH_BASE = 0.0
PITCH_SPAN = 3.0
ENERGY_BASE = 0.60
ENERGY_SPAN = 0.40


def _clamp(v: float, lo: float, hi: float) -> float:
    return lo if v < lo else hi if v > hi else v


def _r(v: float) -> float:
    # 保留两位：一来音频参数不需要更高精度，二来断言里浮点尾数会咬人。
    return round(v, 2)


def prosody_for(pad: PAD) -> dict[str, float]:
    """PAD → 韵律。

    · **arousal 主控语速**：激动时说得快，低落时说得慢（这是最稳的一维）。
    · **pleasure 主控音高**：愉悦时音高上扬，不悦时下沉。
    · **arousal 兼管能量**：激动不只是快，也更大声。

    三项都必需且**都不是常量**：判据 4 的断言是「至少一项不同」，
    如果某一项恒定，那一项就等于没做。
    """
    a = _clamp(pad.arousal, 0.0, 1.0)
    p = _clamp(pad.pleasure, -1.0, 1.0)
    return {
        "rate": _r(_clamp(RATE_BASE + RATE_SPAN * (a - 0.5), 0.6, 1.5)),
        "pitch": _r(PITCH_SPAN * p),
        "energy": _r(_clamp(ENERGY_BASE + ENERGY_SPAN * (a - 0.5), 0.2, 1.0)),
    }


def avg_sentence_chars(text: str) -> float:
    """平均句长（按字算，标点与空白不计）。

    用「字数 / 句数」而不是「词数」：中文没有空格分词，而 `len()` 在中文上
    恰好就是字数——这比引一个分词库诚实得多。句号类标点（。！？…）都算断句，
    连续标点（「……」「？！」）只算一次。
    """
    body = text or ""
    breaks = 0
    prev_break = False
    for ch in body:
        is_break = ch in "。！？!?.…；;\n"
        if is_break and not prev_break:
            breaks += 1
        prev_break = is_break
    chars = sum(1 for ch in body if not ch.isspace() and ch not in "。，、；：？！…—\"'“”‘’（）()《》,.;:!?")
    if breaks == 0:
        # 没有句末标点：整段就是一句
        return float(chars)
    return round(chars / breaks, 1)


def voice_params(voice: dict | None, text: str) -> dict[str, float]:
    """人格 voice → TTS 的 voice 参数（判据 5）。

    只做一件事：**把她平时说得多长，和这一句实际说得多长，对比出来**。
    · 语域（`register`）原样透传——它是给人看的描述，不是数值，provider 拿它
      挑音色档案（比如「口语、短句」→ 更轻的发音风格）。
    · 句长 = 人格的 `avg_sentence_len` 均值 与 本句实际平均句长 的比值，
      夹在 0.8~1.2 之间当 `cadence`。她平时就爱说短句，这一句又很短，
      cadence 就该 < 1；说到长篇大论才 > 1。

    这不是玄学：`cadence` 只影响「句中停顿的疏密」，1.0 是「按她的习惯来」。
    """
    v = voice or {}
    register = str(v.get("register") or "")
    span = v.get("avg_sentence_len") or []
    target = 15.0
    if isinstance(span, (list, tuple)) and span:
        nums = [float(x) for x in span if isinstance(x, (int, float))]
        if nums:
            target = sum(nums) / len(nums)
    actual = avg_sentence_chars(text)
    cadence = 1.0 if target <= 0 or actual <= 0 else _clamp(round(actual / target, 2), 0.8, 1.2)
    return {"register": register, "cadence": cadence, "sentence_chars": round(actual, 1)}
