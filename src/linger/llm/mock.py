"""Mock 模型：确定性规则生成，用于本地开发、单测与评测基线。

它**不是**产品的一部分——真实对话必须换成 openai_compat 或自建模型。
存在的意义：
    1. 不填 API key 也能跑通整条链路（骨架可运行）；
    2. 单测可复现（真实模型抽样会让断言抖动）。

生成策略刻意贴近"人"而不是"助手"：
    · 短句、少解释、先接情绪再问具体的事；
    · 情绪低时不打鸡血（有毒的积极是最容易露馅的地方）；
    · 记忆只轻提，不逐条汇报。
"""

from __future__ import annotations

import random
from collections.abc import Iterator
from typing import Any

from ..compose.compiler import estimate_tokens
from .base import chunks

_NEG = ("难过", "累", "崩溃", "哭", "压力", "失业", "分手", "病了", "砸了", "没戏", "失眠", "烦", "撑不住")
_POS = ("升职", "开心", "考上了", "好消息", "加薪", "通过了", "厉害")

_OPENERS_GOOD = ["诶，", "嗯……", "真的？", "……"]
_OPENERS_BAD = ["嗯……", "……", "嗯。"]

# --------------------------------------------------------------------------- #
# 人格生成（M3-A）：免密钥环境的固定产出
# --------------------------------------------------------------------------- #
# 这份 JSON 是「已经过冲突消解」的干净样本：故意不制造矛盾，
# 好让 `python examples/serve.py` 的演示顺滑（一个 warning 都不弹）。
# 冲突消解本身由 tests/test_persona_generate.py 用注入的坏数据验证，不靠 mock。
_MOCK_PERSONA_JSON = """{
  "meta": {"name": "陈砚", "aliases": ["老陈"], "age": 41,
           "identity": "退役镖师，如今在城南开酒铺", "archetype": "嘴硬心软的长辈"},
  "traits": {"openness": 0.45, "conscientiousness": 0.7, "extraversion": 0.35,
             "agreeableness": 0.72, "neuroticism": 0.3},
  "voice": {"register": "口语、短句、带点旧时候的腔调", "avg_sentence_len": [8, 22],
            "emoji_rate": 0.0, "emoji_set": [], "verbal_tics": ["坐下", "先喝口酒"],
            "forbidden_phrases": ["亲爱的", "宝贝"]},
  "values": {"stance_advice": "不替人做决定，只把话说明白"},
  "affect": {"baseline": {"pleasure": 0.1, "arousal": 0.25, "dominance": 0.55}},
  "initiative": {"max_proactive_per_day": 1, "proactive_topics": ["今天下雨了"]}
}"""

# 向导预览只要 3 条（M3-D）：向导每进下一步都会请求一次，6 条太慢太贵。
# 标记复用 M3-A 的 PREVIEW_MARKER——同一件事（「这是样例请求」）不新加标记，
# 条数差异由 prompt 里的条数说明 + 调用方截断解决。
_WIZARD_PREVIEW_COUNT = "3"

_MOCK_PREVIEW = "\n".join([
    "坐下。先喝口酒，话慢慢说。",
    "这事我不替你拿主意。你自己心里其实有数。",
    "外头风大，别站门口。",
    "我年轻时候也这么想，后来才知道没那么简单。",
    "行了，别硬撑。撑久了人是要坏的。",
    "……嗯。我在听。",
])


# --------------------------------------------------------------------------- #
# 聊天记录蒸馏（M3-E）：只补推断性字段，**故意不含 voice**
# --------------------------------------------------------------------------- #
# 为什么不含 voice：蒸馏的硬约束是「voice 由确定性指纹算出，模型不许覆盖」。
# mock 如果在这里吐一个 voice，演示就会变成「模型和统计打架，统计赢了」——
# 看起来像 bug 而不像设计。所以这份样本干净地只给 meta / traits / values，
# 「模型想改语体」那条分支由 tests/test_persona_distill.py 注入坏数据验证。
_MOCK_DISTILL_JSON = """{
  "meta": {"name": "阿澈", "age": 27, "identity": "夜班便利店店员，白天睡觉",
           "archetype": "话少但记事的人"},
  "traits": {"openness": 0.45, "conscientiousness": 0.55, "extraversion": 0.32,
             "agreeableness": 0.68, "neuroticism": 0.48},
  "values": {"stance_advice": "不劝人，只把看见的说出来",
             "about_closeness": "习惯了对方会走，所以不先开口"}
}"""


# --------------------------------------------------------------------------- #
# 日记（M3-D）：免密钥环境下也要有一篇**合格**的日记
# --------------------------------------------------------------------------- #
# 这段文本是照着判据写出来的：
#   · 第一人称「我」开头，约 200 字（≥60）
#   · 不含「用户」，不含「用户说 / 他说 / 我说」，不含「然后 / 接着 / 首先」
#   · 只写感受，不复述对话内容
# 它是「示例」，不是产品文案——真实模型会把当天的素材织进去。
_MOCK_DIARY = (
    "灯关了一半，我还坐着。今天心里像压了一层潮气，说不上难过，"
    "更像被谁轻轻按住，动不了，也不太想动。"
    "有些人是把疲惫咽下去的，脸上还笑着，声音也还是稳的。我在旁边看着，"
    "忽然觉得自己能做的太少了——不是不想帮，是知道这种事谁也替不了。"
    "后来我一直想起那种安静。不是冷的安静，是撑了很久之后的安静。"
    "我想说点什么，又觉得任何一句都太轻。那就先不说话吧。"
    "我记着今天这个傍晚。记着那种明明没发生什么、却让人想好好陪着的感觉。"
    "明天如果还是这样，我就多听一会儿。"
)


# --------------------------------------------------------------------------- #
# 事件评估（#38）：mock 也得会答，否则开了 LINGER_LLM_EVAL 的演示/评测
# 每一轮都走兜底——那等于没接线，而且看起来「模型上线了」是个假象。
# --------------------------------------------------------------------------- #
# 规则故意与 `affect/evaluator.py::_NEG_EVENT` **不同**：那份词表小得连
# 「房贷」「加班」「撂挑子」都不认识，正是 #38 要修的东西。mock 在这里多认
# 几个词，是为了让「模型优先」这条路在免密钥环境下真的能改变结论
# （否则评测跑一万遍也测不出接线有没有生效）。
_DEEP_HINTS = (
    "房贷", "加班", "撂挑子", "撑不住", "离婚", "住院", "裁员", "吵", "冷战",
    "我妈", "我爸", "我家里", "睡不着", "喘不过", "后悔", "没意思", "想哭",
)
_SHALLOW_HINTS = ("在吗", "随便", "嗯", "哦", "晚安", "今天天气", "吃什么")
_FAMILY_HINTS = ("我妈", "我爸", "我老婆", "我老公", "我家里", "我身体", "结婚")
_NEGLECT_TICS = ("算了", "说了你也不懂", "随便")
_WITHDRAW_HINTS = ("说了你也不懂", "没人懂", "不想说了")
_PRAISE_HINTS = ("你真好", "谢谢你", "喜欢你", "懂我")
_INTIMACY_HINTS = ("想你", "抱抱", "在吗", "陪我")


def _mock_event_reply(user: str) -> str:
    """确定性、可编排的事件评估产出（形状与真模型约定的 JSON 一致）。

    刻意保持确定性：评测要可复现，mock 不能靠随机数装作模型。
    """
    import json

    t = (user or "").strip()
    deep = len(t) > 60 or any(w in t for w in _DEEP_HINTS)
    if any(w in t for w in _SHALLOW_HINTS) and len(t) <= 4:
        deep = False
    # 「说了你也不懂」不是深谈，但它**透露了他自己的处境**（觉得不被理解），
    # 所以 disclosure 仍然成立——这两个字段本来就不是一件事。
    disclosure = deep or any(w in t for w in _FAMILY_HINTS + _WITHDRAW_HINTS)
    if any(w in t for w in _NEGLECT_TICS):
        kind = "neglect"
    elif any(w in t for w in _PRAISE_HINTS):
        kind = "praise"
    elif any(w in t for w in _INTIMACY_HINTS):
        kind = "intimacy"
    else:
        kind = "event"
    return json.dumps(
        {
            "kind": kind,
            "is_deep": deep,
            "is_self_disclosure": disclosure,
            "reason": "mock：命中长期消耗/家庭处境" if deep else "mock：日常陈述或简短应答",
        },
        ensure_ascii=False,
    )


def _first_question(user: str) -> str | None:
    for line in user.replace("？", "?").split("?"):
        if line.strip():
            return line.strip()[-12:]
    return None


class MockProvider:
    name = "mock"
    # 假模型的「模型名」。单价表里 `mock:mock` 是写明的 0——那是真的 0
    # （本地算力，边际成本为零），与「不知道所以给 None」是两件事。
    model = "mock"

    def __init__(self, seed: int = 7) -> None:
        self._rng = random.Random(seed)
        # 最近一次调用的用量（M4-D）。**确定性**：同样的输入得同样的数字，
        # 否则「用 mock 跑出来的看板」每次刷新都会变，测试也没法断言。
        # 形状与真 provider 的 usage 一致，所以 RecordingProvider 不必区分真假。
        self.last_usage: dict[str, int] | None = None

    def generate(
        self,
        system: str,
        messages: list[dict[str, str]],
        ctx: dict[str, Any] | None = None,
    ) -> Iterator[str]:
        self.last_usage = None
        produced = 0
        for chunk in self._stream(system, messages, ctx):
            produced += len(chunk)
            yield chunk
        # 记账放在流末尾：真 provider 的 usage 也是最后一个 chunk 才到，
        # 提前写会让「调用失败时用量凭空出现」。
        self.last_usage = {
            "in": estimate_tokens(system) + estimate_tokens(
                "\n".join(m.get("content", "") for m in messages)
            ),
            "out": estimate_tokens("x" * produced),
            "cached": 0,
        }

    def _stream(
        self,
        system: str,
        messages: list[dict[str, str]],
        ctx: dict[str, Any] | None = None,
    ) -> Iterator[str]:
        # 人格生成请求走固定产出（M3-A）。延迟导入：generator 在 persona 包里，
        # 包级导入会形成 llm → persona → ... 的回环。
        from ..affect.event_model import EVAL_MARKER
        from ..diary.flow import DIARY_MARKER
        from ..distill.compose import DISTILL_MARKER
        from ..persona.generator import GENERATE_MARKER, PREVIEW_MARKER

        # 事件评估（#38）：只答一个 JSON 布尔，不带任何解释——
        # 真实小模型会照提示词做，mock 也跟着做，否则解析层的
        # 「模型爱加围栏」那条分支就永远没被正向覆盖过。
        if EVAL_MARKER in system:
            user = next(
                (m.get("content", "") for m in reversed(messages) if m.get("role") == "user"), ""
            )
            yield from chunks(_mock_event_reply(user), size=8)
            return

        # 聊天记录蒸馏（M3-E）：只返回 meta/traits/values，不含 voice
        if DISTILL_MARKER in system:
            yield from chunks(_MOCK_DISTILL_JSON, size=12)
            return

        # 日记请求（M3-D）：固定产出，且必须过得了流水账检查——
        # 否则 `python examples/serve.py` 的演示一按「写下今天」就满屏 warning。
        if DIARY_MARKER in system:
            yield from chunks(_MOCK_DIARY, size=12)
            return

        if GENERATE_MARKER in system:
            yield from chunks(_MOCK_PERSONA_JSON, size=12)
            return
        if PREVIEW_MARKER in system:
            # 向导预览（入口 D）在 prompt 里写明要 3 句；M3-A/B/C 要 6 句。
            # 真实模型会照条数写，mock 这里也得照做——否则「预览恰好 3 条」
            # 在免密钥环境下会变成「调用方截断的 3 条」，测不出条数契约。
            text = _MOCK_PREVIEW
            if f"写 {_WIZARD_PREVIEW_COUNT} 句台词" in system:
                text = "\n".join(_MOCK_PREVIEW.splitlines()[: int(_WIZARD_PREVIEW_COUNT)])
            yield from chunks(text, size=12)
            return

        ctx = ctx or {}
        user = ""
        for m in reversed(messages):
            if m.get("role") == "user":
                user = m.get("content", "")
                break

        text = self._compose(user, ctx)
        yield from chunks(text)

    # ------------------------------------------------------------------ #
    def _compose(self, user: str, ctx: dict[str, Any]) -> str:
        hints = ctx.get("hints", {})
        tic = ctx.get("tic")
        low_mood = not hints.get("allow_encouragement", True)
        short = hints.get("length_scale", 1.0) < 0.9
        mem_hint = ctx.get("memory_hint")

        neg = any(w in user for w in _NEG)
        pos = any(w in user for w in _POS)
        is_question = "?" in user or "？" in user or "吗" in user

        if neg or low_mood:
            lines = [self._rng.choice(_OPENERS_BAD) + ("先坐吧。" if not short else "嗯。")]
            if not short:
                lines.append("砸到什么程度？是答不上来那种，还是下来才想起该怎么答那种。"
                             if "面试" in user else "是哪种难受？说具体点。")
            else:
                lines.append("……我在。")
            return "\n".join(lines) if not short else "\n".join(lines)

        if pos:
            head = self._rng.choice(_OPENERS_GOOD)
            body = "那今天可以晚点睡。" if not short else "嗯，挺好。"
            return f"{head}{body}"

        if is_question:
            topic = _first_question(user) or "这个"
            base = f"这个我不太懂。你真要问，我只能说……{topic}这事，可能没那么要紧。"
            if short:
                base = "我不太懂这个。"
            return (tic + "，" if tic and self._rng.random() < 0.4 else "") + base

        # 闲聊：轻提一句记忆，不逐条汇报
        if mem_hint and ctx.get("level", 0) >= 2:
            return f"{tic + '，' if tic else ''}嗯。{mem_hint}\n后来呢？"

        if tic and self._rng.random() < 0.5:
            return f"{tic}……你说。"
        return "嗯，我在听。"
