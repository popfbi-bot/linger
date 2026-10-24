"""事件评估换小模型（#38）：把「这一轮算什么」交给模型判断。

## 为什么必须换

规则版 `is_deep_turn` 只有两条通道：`len > 120` 和十几词的 `_NEG_EVENT` 词表。
实测用户说「房贷压得我喘不过来」「连着加了三个礼拜班」「我真想撂挑子」——
一句都不命中，引擎以为他在寒暄。**用户把心掏出来，关系却不动**。

## 三条硬约束

1. **模型优先，规则兜底。** 模型给得出结论就用模型的；说不出、超时、吐垃圾，
   回落到原来那套规则。兜底不是「保守地判 false」，是「回落到原口径」——
   它虽窄，但不会把寒暄误判成倾诉，也不会因为挂了模型就把关系系统冻住。
2. **失败静默。** 旁路评估拖慢主链路就是事故：这里不抛、不刷日志、
   不往 SSE 里塞错误。调用方拿到的永远是一个结论对象。
3. **模型不直接管事。** bool 字段（`deep` / `disclosure`）与规则**取或**。
   模型加的是敏感度，不是否决权——否则一次抽风就能让关系倒退。

## 数值不来自模型

`dp/da/dd/salience` 仍由 `evaluator.evaluate()` 那套规则算。理由：模型给的浮点数
没法测、会抖动、会越界；而「情绪移动多少」本来是产品刻度，不是语言理解问题。
模型只回答**它擅长的那部分**——这句话属于哪类事件、有多深、是不是在说自己。
这是本实现与 issue 示例 JSON 的一处刻意偏差，交付说明里写明了。

## 为什么自己起线程

`evaluate` 已经跑在编排层的状态线程里（`_recall_and_update` 的并行任务）。
再套一层带守护线程的韧性层，就是「线程里的线程 + 双重超时」，出问题时
没人说得清是哪一层的钟停了。这里只做这件事需要的两件事：
**一个硬截止时间**（守护线程 + join）和**一个熔断开关**（失败后冷却）。
"""

from __future__ import annotations

import hashlib
import json
import re
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Any

from ..models import Impact
from . import evaluator

# 提示词里靠这个标记认领「这是我发的请求」。
# 与日记/蒸馏/人格生成同法：不新增信封类型，只加一个可检索的锚。
EVAL_MARKER = "[[LINGER_EVAL]]"

# 判据要求「超时 ≤ 5 秒」，且评估是旁路——默认 3 秒，留 2 秒余量给主链路。
DEFAULT_TIMEOUT = 3.0

# 失败之后歇多久不再问模型。冷却期内一律走规则——一个坏掉的模型
# 不该在每轮对话里都被敲一次，每次都白等一个超时。
DEFAULT_COOLDOWN = 60.0

# 结果缓存的条数上限。一个进程里同时活跃的对话是个位数，
# 256 条足够覆盖「同一段倾诉被反复评估」，也不至于变成内存泄漏。
_CACHE_MAX = 256

_KINDS = ("event", "praise", "criticism", "neglect", "intimacy")

_SYSTEM = f"""{EVAL_MARKER}
你是对话理解模块，只做一件事：判断用户这句话是什么性质，以及走不走心。

字段含义：
- kind：这句话属于哪类事件。只能是 event / praise / criticism / neglect /
  intimacy 之一。event = 说了件具体的事；praise = 在认可你；
  criticism = 在挑你的刺；neglect = 敷衍地应你；intimacy = 想靠近你。
- is_deep：是不是「走心的倾诉」。说出具体的难处、长期的消耗、身体或家里的状况、
  关系的裂痕、对自己处境的坦白、情绪到了要溢出来的程度 —— 都算。
  寒暄、问事、玩笑、抱怨天气、简短应答（「嗯」「哦」「随便」）、
  以及不相干的事实陈述 —— 都不算。
- is_self_disclosure：是不是在讲「关于他自己的私事」——家里、身体、感情、
  工作困境、过去。只是在回应你，不算。
- reason：一句话说明判断依据，给人看，会进 trace。

只输出一个 JSON 对象，不要解释，不要 markdown：
{{"kind": "event", "is_deep": true, "is_self_disclosure": true, "reason": "…"}}

示例：
「房贷压得我喘不过来」
→ {{"kind": "event", "is_deep": true, "is_self_disclosure": true, "reason": "讲自己的经济压力"}}
「今天收摊挺早」
→ {{"kind": "event", "is_deep": false, "is_self_disclosure": false, "reason": "陈述一件日常事实"}}
「连着加了三个礼拜班，回家倒头就睡」
→ {{"kind": "event", "is_deep": true, "is_self_disclosure": true, "reason": "长期的消耗"}}
「算了，说了你也不懂」
→ {{"kind": "neglect", "is_deep": false, "is_self_disclosure": true, "reason": "收回自己，但透露了不被理解"}}
「在吗」
→ {{"kind": "intimacy", "is_deep": false, "is_self_disclosure": false, "reason": "只是打招呼"}}"""

# 只认整个回复里的第一个 JSON 对象。模型爱加 ```json 围栏和前后的解释，
# 用「有没有这个键」判断比要求它绝对听话要可靠。
_JSON_OBJECT = re.compile(r"\{[^{}]*\}")
_DEEP_KEY = re.compile(r'"?is_deep"?\s*:\s*(true|false)', re.IGNORECASE)
_DISC_KEY = re.compile(r'"?is_self_disclosure"?\s*:\s*(true|false)', re.IGNORECASE)


def _prompt_fingerprint() -> str:
    return hashlib.sha256(_SYSTEM.encode("utf-8")).hexdigest()[:12]


@dataclass
class Verdict:
    """一轮事件的评估结论。模型说得出就填模型的部分，永远带规则的部分。"""

    is_deep: bool
    is_self_disclosure: bool
    source: str = "rule"  # "model" | "rule"
    kind: str | None = None
    reason: str = ""
    impacts: list[Impact] = field(default_factory=list)

    def trace(self) -> dict[str, Any]:
        """进 trace 的形状：够复盘「她为什么觉得这句深」，又不会撑爆一行。"""
        return {
            "eval_source": self.source,
            "reason": self.reason,
            "is_deep": self.is_deep,
            "is_self_disclosure": self.is_self_disclosure,
            "kind": self.kind,
        }


def parse_reply(text: str) -> dict[str, Any] | None:
    """把模型输出解析成 payload；看不懂返回 None（**不是**「全 False」）。

    「看不懂」与「明确说不走心」必须分开：前者要走兜底，后者是结论。
    把解析失败当成 False，等于让一个失灵的模型悄悄让关系系统变聋。
    """
    raw = (text or "").strip()
    if not raw:
        return None
    for candidate in _JSON_OBJECT.findall(raw):
        try:
            obj = json.loads(candidate)
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict):
            deep = obj.get("is_deep")
            if isinstance(deep, bool):
                return obj
    # 退一步：模型可能只写了 `is_deep: true`（没引号，也不是合法 JSON）
    m = _DEEP_KEY.search(raw)
    if m:
        d = _DISC_KEY.search(raw)
        return {
            "is_deep": m.group(1).lower() == "true",
            "is_self_disclosure": bool(d and d.group(1).lower() == "true"),
        }
    return None


def _coerce_kind(value: Any) -> str | None:
    """只认白名单里的 kind。模型自创一个 `"profound"` 不算洞见，算垃圾。"""
    return value if isinstance(value, str) and value in _KINDS else None


class EventJudge:
    """模型优先、规则兜底的事件评估器。

    `provider` 为 None 时**不装模型**，结论就是纯规则——
    这是 `LINGER_EVAL_LLM=mock` 与未接线调用方的形态，也是所有旧测试的形态。
    """

    def __init__(
        self,
        provider: Any | None = None,
        *,
        timeout: float = DEFAULT_TIMEOUT,
        cooldown: float = DEFAULT_COOLDOWN,
        now=time.monotonic,
    ) -> None:
        self.provider = provider
        self.timeout = timeout
        self.cooldown = cooldown
        self._now = now
        self._cache: OrderedDict[tuple[str, str], dict[str, Any]] = OrderedDict()
        self._lock = threading.Lock()
        self._disabled_until = 0.0
        self.last_error = ""
        self.calls = 0
        self.failures = 0

    # -- 熔断 ---------------------------------------------------------- #
    def _tripped(self) -> bool:
        return bool(self.provider) and self._now() < self._disabled_until

    def _trip(self) -> None:
        self.failures += 1
        if self._now() >= self._disabled_until:
            # 只在真正「进入冷却」那一次说话，冷却期内的每轮失败都不吭声。
            print(
                f"[linger] 事件评估模型不可用，静默走规则兜底 "
                f"{self.cooldown:.0f}s：{self.last_error or '未知原因'}"
            )
        self._disabled_until = self._now() + self.cooldown

    # -- 主入口 -------------------------------------------------------- #
    def evaluate(self, user_text: str) -> Verdict:
        """永不抛。任何异常都降级为规则结论。"""
        text = (user_text or "").strip()
        if not text:
            return Verdict(is_deep=False, is_self_disclosure=False, impacts=[])

        # 规则部分：情绪冲击与两个 bool 的兜底值。**永远算**——
        # 它是 `resonated` 与旧行为的锚，不能因为「模型说了」就跳过。
        impacts = evaluator.evaluate(text)
        rule_deep = evaluator.is_deep_turn(text)
        rule_disc = evaluator.is_self_disclosure(text)
        payload = self._ask(text) if self.provider and not self._tripped() else None

        if payload is None:
            return Verdict(
                is_deep=rule_deep,
                is_self_disclosure=rule_disc,
                source="rule",
                impacts=impacts,
            )

        kind = _coerce_kind(payload.get("kind"))
        reason = payload.get("reason")
        return Verdict(
            # 取或：模型加敏感度，不给否决权
            is_deep=rule_deep or bool(payload.get("is_deep")),
            is_self_disclosure=rule_disc or bool(payload.get("is_self_disclosure")),
            source="model",
            kind=kind,
            reason=reason if isinstance(reason, str) else "",
            impacts=impacts,
        )

    def _ask(self, text: str) -> dict[str, Any] | None:
        key = (text, _prompt_fingerprint())
        with self._lock:
            if key in self._cache:
                self._cache.move_to_end(key)
                return self._cache[key]

        self.calls += 1
        try:
            raw = self._collect(text)
        except Exception as exc:  # noqa: BLE001 - 旁路评估不许把主链路带下去
            self.last_error = f"{type(exc).__name__}: {exc}"[:120]
            self._trip()
            return None

        payload = parse_reply(raw)
        if payload is None:
            self.last_error = f"输出无法解析：{raw[:60]!r}"
            self._trip()
            return None

        self.failures = 0
        self._disabled_until = 0.0
        self.last_error = ""
        with self._lock:
            self._cache[key] = payload
            while len(self._cache) > _CACHE_MAX:
                self._cache.popitem(last=False)
        return payload

    def _collect(self, text: str) -> str:
        """在独立守护线程里跑，带硬截止时间。

        为什么不用 provider 自己的超时：假 provider 不联网、也不会自己计时，
        测试里根本造不出超时。硬截止时间放在这一层，被测的和上线的走同一条路。
        """
        box: dict[str, Any] = {}

        def run() -> None:
            try:
                box["text"] = "".join(self.provider.generate(_SYSTEM, [{"role": "user", "content": text}]))
            except BaseException as exc:  # noqa: BLE001
                box["error"] = exc

        worker = threading.Thread(target=run, name="linger-eval", daemon=True)
        worker.start()
        worker.join(self.timeout)
        if worker.is_alive():
            raise TimeoutError(f"事件评估超过 {self.timeout}s")
        if "error" in box:
            raise box["error"]
        return box.get("text", "")

    # -- 观测 ---------------------------------------------------------- #
    def stats(self) -> dict[str, Any]:
        return {
            "provider": getattr(self.provider, "name", None),
            "calls": self.calls,
            "failures": self.failures,
            "cooling": self._tripped(),
            "last_error": self.last_error,
            "cached": len(self._cache),
        }


def get_event_judge() -> EventJudge:
    """按配置装配（进程内单例）。

    **单例是有意的**：缓存与熔断是跨轮状态。每轮新建一个 EventJudge，
    就等于每轮都重新敲一次坏掉的模型、每次都要白等一个超时。

    装配失败（比如配了 openai 却没给 key）不抛——没有模型就用规则：
    一个配错的环境变量不该让整个服务起不来。
    """
    global _default_judge
    if _default_judge is None:
        from ..config import settings

        _default_judge = EventJudge(
            build_provider(settings.eval_provider), timeout=settings.eval_timeout
        )
    return _default_judge


def build_provider(name: str) -> Any | None:
    """按名字造评估 provider。`rule` / `mock` / 空 → None（纯规则，零网络）。"""
    name = (name or "rule").strip()
    if name in ("", "rule", "off", "mock"):
        return None
    if name == "same":
        from ..config import settings

        name = settings.llm_provider

    from ..llm import _build

    try:
        return _build(name)
    except (ValueError, RuntimeError) as exc:
        print(f"[linger] 事件评估模型 {name} 不可用，走规则兜底：{exc}")
        return None


def reset_event_judge() -> None:
    """丢掉单例（测试与「改了配置想立刻生效」时用）。"""
    global _default_judge
    _default_judge = None


_default_judge: EventJudge | None = None


__all__ = [
    "EVAL_MARKER",
    "EventJudge",
    "Verdict",
    "build_provider",
    "get_event_judge",
    "parse_reply",
    "reset_event_judge",
]
