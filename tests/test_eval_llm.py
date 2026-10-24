"""#38 验收：事件评估换小模型——模型优先 + 规则兜底 + 失败静默降级。

判据编号与 issue 正文一致：

    1  模型可用时「我刚结婚，房贷压得慌，天天加班到半夜。」判 is_deep=true（老规则 False）
    2  模型可用时「算了，说了你也不懂。」判 is_self_disclosure=true
    3  降级：抛异常 / 超时 / 返回非 JSON / 字段缺失 —— 四种都静默退回规则且不抛
    4  降级后 trace 的 eval_source == "rule"；正常时 == "model"
    5  reason 出现在 trace 里
    6  超时上限 ≤ 5 秒（可控假 provider 实测，不真等）
    7  LINGER_EVAL_LLM=mock 时完全不发网络请求
    8  is_deep_turn / is_self_disclosure 仍在且行为不变
    9  模型判 deep=true 时 tick() 的 deep_turns 真的 +1（端到端）
   10  模型判 disclosure=true 时 self_disclosure 真的 +1（端到端）
   11  本文件即 tests/test_eval_llm.py（测试数远大于 666 的基线，见交付说明）
   12  `python -m pytest` 退出码 0
   13  三种破坏的红证据（摘降级链 / 超时改 60 秒 / eval_source 写死），见交付说明

全程不联网：假 provider 是本文件里自己写的小类。
"""

from __future__ import annotations

import json
import threading
import time
from collections.abc import Iterator
from typing import Any

import pytest
from _fakes import FakeEmbedder, FakeLLM

from linger.affect import evaluator
from linger.affect.event_model import (
    EVAL_MARKER,
    EventJudge,
    build_provider,
    get_event_judge,
    parse_reply,
    reset_event_judge,
)
from linger.llm import MockProvider
from linger.memory.embedding import HashEmbedder
from linger.models import PersonaCard
from linger.orchestrator import Orchestrator
from linger.server.app import create_app
from linger.storage.sqlite import SQLiteRepository

# issue 判据 1/2 里点名的原句（逐字照抄，不改标点）
_HOUSE = "我刚结婚，房贷压得慌，天天加班到半夜。"
_WITHDRAW = "算了，说了你也不懂。"


# --------------------------------------------------------------------------- #
# 可控假 provider
# --------------------------------------------------------------------------- #


class JudgeProvider:
    """按剧本回答事件评估请求（判据 6 要求「可控」，不是随机数）。

    - `answer`：要吐的原文（可以是坏 JSON / 缺字段）
    - `raises`：吐字前先抛
    - `delay`：每个 chunk 之间停多久（造超时用）
    - `block`：一直不返回，直到 `release` 被 set（造「挂死的模型」）
    """

    name = "judge"

    def __init__(
        self,
        answer: str = '{"kind":"event","is_deep":true,"is_self_disclosure":true,"reason":"ok"}',
        *,
        raises: BaseException | None = None,
        delay: float = 0.0,
        block_marker: threading.Event | None = None,
    ) -> None:
        self.answer = answer
        self.raises = raises
        self.delay = delay
        self.block_marker = block_marker
        self.calls = 0
        self.systems: list[str] = []

    def generate(
        self, system: str, messages: list[dict[str, str]], ctx: dict[str, Any] | None = None
    ) -> Iterator[str]:
        self.calls += 1
        self.systems.append(system)
        if self.raises is not None:
            raise self.raises
        if self.block_marker is not None:
            self.block_marker.wait(10)
        for i in range(0, len(self.answer), 4):
            if self.delay:
                time.sleep(self.delay)
            yield self.answer[i : i + 4]


def _judge(provider: Any, **kw: Any) -> EventJudge:
    return EventJudge(provider, **kw)


def _payload(**kw: Any) -> str:
    base: dict[str, Any] = {
        "kind": "event",
        "is_deep": True,
        "is_self_disclosure": True,
        "reason": "模型给的理由",
    }
    base.update(kw)
    return json.dumps(base, ensure_ascii=False)


# --------------------------------------------------------------------------- #
# 判据 8：规则函数仍然存在、行为一个字没变
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("text", [_HOUSE, _WITHDRAW, "我真想撂挑子不干了"])
def test_rule_misses_these_real_sentences(text: str) -> None:
    """先把 bug 本身钉住：规则版对这几句返回 False。

    这不是「测旧代码」，是**验收前提**：如果哪天有人为了省事把它改宽了，
    这条会变绿——届时 #38 的收益一起消失，需要重新想模型该补什么。
    """
    assert evaluator.is_deep_turn(text) is False


def test_rule_functions_survive_with_unchanged_behaviour() -> None:
    """判据 8：两个兜底函数仍在，且口径没被顺手改宽。"""
    assert evaluator.is_deep_turn("x" * 121) is True
    assert evaluator.is_deep_turn("x" * 120) is False
    assert evaluator.is_deep_turn("我病了") is True  # 命中 _NEG_EVENT
    assert evaluator.is_deep_turn("今天天气不错") is False
    assert evaluator.is_self_disclosure("我妈今天去医院了") is True
    assert evaluator.is_self_disclosure("我以前也这样") is True
    assert evaluator.is_self_disclosure("今天天气不错") is False


# --------------------------------------------------------------------------- #
# 判据 1、2：模型把规则漏掉的句子接住
# --------------------------------------------------------------------------- #


def test_judgement_1_house_loan_is_deep() -> None:
    """判据 1：模型可用时这句判 deep=true（老规则 False——这就是本任务要修的）。"""
    judge = _judge(JudgeProvider(_payload(is_deep=True)))
    verdict = judge.evaluate(_HOUSE)
    assert verdict.is_deep is True
    assert verdict.source == "model"


def test_judgement_2_withdraw_is_self_disclosure() -> None:
    """判据 2：模型可用时这句判 self_disclosure=true（规则版 False）。"""
    judge = _judge(JudgeProvider(_payload(kind="neglect", is_deep=False, is_self_disclosure=True)))
    verdict = judge.evaluate(_WITHDRAW)
    assert verdict.is_self_disclosure is True
    assert verdict.source == "model"
    assert verdict.kind == "neglect"


def test_model_true_cannot_be_erased_by_model_false() -> None:
    """取或：模型说 false 不能删掉规则认出来的硬证据（「崩了」在词表里）。"""
    judge = _judge(JudgeProvider(_payload(is_deep=False, is_self_disclosure=False)))
    verdict = judge.evaluate("我今天真的崩溃了")
    assert verdict.is_deep is True
    assert verdict.source == "model"


def test_output_with_fence_and_prose_is_understood() -> None:
    """模型爱加 ```json 围栏和前后解释——解析层认不出等于没接线。"""
    judge = _judge(JudgeProvider('好的：\n```json\n' + _payload() + '\n```\n（理由写在字段里）'))
    assert judge.evaluate(_HOUSE).is_deep is True


def test_unknown_kind_is_dropped_not_passed_through() -> None:
    """模型自创的 kind 不算洞见，算垃圾——只认白名单。"""
    judge = _judge(JudgeProvider(_payload(kind="profound")))
    assert judge.evaluate(_HOUSE).kind is None


# --------------------------------------------------------------------------- #
# 判据 3：四种坏法都静默退回规则
# --------------------------------------------------------------------------- #


def _assert_rule_fallback(verdict, text: str = _HOUSE) -> None:
    assert verdict.source == "rule"
    assert verdict.is_deep is evaluator.is_deep_turn(text)
    assert verdict.is_self_disclosure is evaluator.is_self_disclosure(text)


def test_judgement_3a_provider_exception_falls_back() -> None:
    """判据 3①：模型抛异常 → 静默退回规则，调用方一个字都没被抛到。"""
    judge = _judge(JudgeProvider(raises=RuntimeError("boom")))
    _assert_rule_fallback(judge.evaluate(_HOUSE))
    # 规则认得出的句子，降级后仍然是 deep —— 降级不等于变聋
    assert judge.evaluate("我今天真的崩溃了").is_deep is True


def test_judgement_3b_timeout_falls_back() -> None:
    """判据 3②：超时 → 静默退回规则。"""
    judge = _judge(JudgeProvider(delay=0.5), timeout=0.15)
    _assert_rule_fallback(judge.evaluate(_HOUSE))


def test_judgement_3c_non_json_falls_back() -> None:
    """判据 3③：返回非 JSON → 静默退回规则。"""
    judge = _judge(JudgeProvider("我觉得这句话挺深的"))
    _assert_rule_fallback(judge.evaluate(_HOUSE))


def test_judgement_3d_missing_field_falls_back() -> None:
    """判据 3④：JSON 合法但缺 is_deep → 静默退回规则。

    这是最容易写错的一条：`json.loads` 成功就以为拿到了答案，
    于是 `payload.get("is_deep")` 得到 None，被 `bool(None)` 变成 False ——
    **一个缺字段的模型会让关系系统静默变聋**。
    """
    judge = _judge(JudgeProvider('{"kind":"event","reason":"只有理由"}'))
    _assert_rule_fallback(judge.evaluate(_HOUSE))


def test_empty_input_never_calls_model() -> None:
    """空输入连模型都不该叫——白花一次配额，还可能把空串判成走心。"""
    provider = JudgeProvider()
    judge = _judge(provider)
    verdict = judge.evaluate("   ")
    assert verdict.is_deep is False and verdict.impacts == []
    assert provider.calls == 0


def test_broken_model_never_raises_on_any_input() -> None:
    """判据 3 的集合形态：无论输入什么，评估都不抛。"""
    judge = _judge(JudgeProvider(raises=ValueError("坏掉")))
    for text in ("", "嗯", _HOUSE, "x" * 300, "？", "\n\t"):
        judge.evaluate(text)  # 只要不抛就算过


# --------------------------------------------------------------------------- #
# 判据 6 + 有界：超时、挂死、冷却、缓存
# --------------------------------------------------------------------------- #


def test_judgement_6_timeout_cap_is_within_five_seconds() -> None:
    """判据 6：默认超时上限 ≤ 5 秒，且实测确实在这个量级内返回。"""
    judge = EventJudge(None)
    assert judge.timeout <= 5.0


def test_slow_model_is_cut_off_at_timeout() -> None:
    """超时真的生效：总耗时被 timeout 卡住，不是等模型自己说完。"""
    judge = _judge(JudgeProvider(delay=0.5), timeout=0.2)
    started = time.monotonic()
    verdict = judge.evaluate(_HOUSE)
    elapsed = time.monotonic() - started
    assert verdict.source == "rule"
    assert elapsed < 1.0, f"超时没生效，耗时 {elapsed:.2f}s"


def test_hanging_model_does_not_hang_the_caller() -> None:
    """挂死不返回的模型——调用方必须自己能醒来（守护线程 + join 截止）。"""
    blocker = threading.Event()  # 永不 set
    judge = _judge(JudgeProvider(block_marker=blocker), timeout=0.15)
    started = time.monotonic()
    judge.evaluate(_HOUSE)
    assert time.monotonic() - started < 1.0


def test_failure_enters_cooldown_and_skips_model() -> None:
    """坏掉的模型不该在每轮对话里都被敲一次，每次都白等一个超时。"""
    provider = JudgeProvider(raises=RuntimeError("boom"))
    judge = _judge(provider, cooldown=60.0)
    judge.evaluate(_HOUSE)
    assert provider.calls == 1
    assert judge.stats()["cooling"] is True

    for _ in range(5):
        judge.evaluate("还是房贷的事")
    assert provider.calls == 1, "冷却期内又去敲模型了"


def test_cooldown_expires_and_model_is_retried() -> None:
    """冷却到期要重新试——否则一次网络抖动就把模型永久下线。"""
    now = [0.0]
    provider = JudgeProvider(raises=RuntimeError("boom"))
    judge = EventJudge(provider, cooldown=10.0, now=lambda: now[0])
    judge.evaluate(_HOUSE)
    assert provider.calls == 1

    now[0] = 11.0
    provider.raises = None
    verdict = judge.evaluate(_HOUSE)
    assert provider.calls == 2
    assert verdict.source == "model"
    assert judge.stats()["cooling"] is False


def test_repeated_text_hits_cache() -> None:
    """同一句话反复评估只问一次模型：既省钱，也让评测可复现。"""
    provider = JudgeProvider(_payload())
    judge = _judge(provider)
    for _ in range(3):
        assert judge.evaluate(_HOUSE).is_deep is True
    assert provider.calls == 1
    assert judge.stats()["cached"] == 1


def test_cache_key_depends_on_prompt() -> None:
    """改提示词必须让旧缓存失效——否则改了判据分数不动，人会以为模型没生效。"""
    import linger.affect.event_model as em

    provider = JudgeProvider(_payload())
    judge = _judge(provider)
    judge.evaluate(_HOUSE)
    original = em._SYSTEM
    try:
        em._SYSTEM = original + "\n（追加一条新判据）"
        judge.evaluate(_HOUSE)
    finally:
        em._SYSTEM = original
    assert provider.calls == 2


def test_cache_is_bounded() -> None:
    """缓存有上限：长会话不该把每一句都留在内存里。"""
    import linger.affect.event_model as em

    judge = _judge(JudgeProvider(_payload(is_deep=False, is_self_disclosure=False)))
    for i in range(em._CACHE_MAX + 30):
        judge.evaluate(f"第 {i} 句闲聊")
    assert judge.stats()["cached"] <= em._CACHE_MAX


# --------------------------------------------------------------------------- #
# 判据 7：mock / 未配 时不发任何网络请求
# --------------------------------------------------------------------------- #


def test_judgement_7_mock_makes_no_network_calls(monkeypatch) -> None:
    """判据 7：`LINGER_EVAL_LLM=mock` 时零网络——连 provider 都不造。

    用「任何 socket 连接都炸」来证明，而不是只看 `provider is None`：
    后者只能证明这一层没造对象，证不了过程里没人偷偷联网。
    """
    import socket

    def boom(*a: Any, **kw: Any):
        raise AssertionError("LINGER_EVAL_LLM=mock 却发起了网络请求")

    monkeypatch.setattr(socket, "create_connection", boom)
    monkeypatch.setattr(socket.socket, "connect", boom)
    monkeypatch.setattr(socket, "getaddrinfo", boom)

    assert build_provider("mock") is None
    judge = EventJudge(build_provider("mock"))
    assert judge.evaluate(_HOUSE).source == "rule"


def test_default_config_is_rule_only() -> None:
    """默认配置就是纯规则：装上包就跑，不该顺手连任何模型。"""
    from linger.config import settings

    assert settings.eval_provider == "mock"
    assert build_provider(settings.eval_provider) is None


def test_mock_provider_answers_the_eval_prompt_with_mock_switch() -> None:
    """mock 也得会答：否则「接了模型」只是纸上的一句话。

    注意这里直接用 MockProvider 当 provider（`LINGER_EVAL_LLM=mock` 语义是
    「走规则、不联网」，两者不是一回事）——它验证的是**接线形状**对不对。
    """
    judge = EventJudge(MockProvider())
    assert judge.evaluate(_HOUSE).is_deep is True
    assert judge.evaluate(_WITHDRAW).is_self_disclosure is True
    assert judge.evaluate("今天收摊挺早").is_deep is False


def test_bad_provider_name_degrades_to_rule() -> None:
    """配错模型名 → 规则兜底，不是启动崩溃。"""
    assert build_provider("不存在的模型") is None


def test_same_config_reuses_chat_provider(monkeypatch) -> None:
    """`same` 表示跟对话用同一个 provider——给只想配一套的人留出口。"""
    from linger import config

    monkeypatch.setattr(config.settings, "llm_provider", "mock")
    assert build_provider("same") is not None


def test_get_event_judge_is_singleton() -> None:
    """单例不是洁癖：缓存与熔断是跨轮状态，每轮新建等于每轮白等一个超时。"""
    reset_event_judge()
    try:
        assert get_event_judge() is get_event_judge()
    finally:
        reset_event_judge()


# --------------------------------------------------------------------------- #
# 判据 4、5、9、10：端到端（走产品的 Orchestrator，不是只测那个类）
# --------------------------------------------------------------------------- #


def _turn(orch: Orchestrator, session_id: str, text: str) -> dict:
    events = list(orch.send_message(session_id, text))
    return next(e for e in events if e.type == "done").data


def test_judgement_4_trace_records_model_source(
    repo: SQLiteRepository, persona: PersonaCard
) -> None:
    """判据 4 正面：模型正常时 trace 的 eval_source == "model"。"""
    orch = Orchestrator(repo, FakeLLM(), FakeEmbedder(), event_judge=_judge(JudgeProvider(_payload())))
    session = orch.create_session(persona.id, "u_model")
    done = _turn(orch, session.id, _HOUSE)

    trace = repo.get_trace(done["turn_id"])
    assert trace is not None
    assert trace["eval"]["eval_source"] == "model"


def test_judgement_4b_trace_records_rule_source_on_fallback(
    repo: SQLiteRepository, persona: PersonaCard
) -> None:
    """判据 4 反面：降级后 trace 的 eval_source == "rule"。

    这一条是「留痕」的全部意义：不然你以为模型在工作，其实一直走规则。
    """
    judge = _judge(JudgeProvider(raises=RuntimeError("down")))
    orch = Orchestrator(repo, FakeLLM(), FakeEmbedder(), event_judge=judge)
    session = orch.create_session(persona.id, "u_rule")
    done = _turn(orch, session.id, _HOUSE)

    trace = repo.get_trace(done["turn_id"])
    assert trace is not None
    assert trace["eval"]["eval_source"] == "rule"


def test_judgement_5_reason_appears_in_trace(
    repo: SQLiteRepository, persona: PersonaCard
) -> None:
    """判据 5：reason 进 trace，出问题能查「她为什么觉得这句深」。"""
    judge = _judge(JudgeProvider(_payload(reason="用户在讲婚姻与经济压力")))
    orch = Orchestrator(repo, FakeLLM(), FakeEmbedder(), event_judge=judge)
    session = orch.create_session(persona.id, "u_reason")
    done = _turn(orch, session.id, _HOUSE)

    trace = repo.get_trace(done["turn_id"])
    assert trace is not None
    assert trace["eval"]["reason"] == "用户在讲婚姻与经济压力"
    # 同一条信息也要能通过 SSE 拿到，不然前端只能靠翻库
    assert done["state"]["eval"]["reason"] == "用户在讲婚姻与经济压力"


def test_judgement_9_deep_turns_really_increments(
    repo: SQLiteRepository, persona: PersonaCard
) -> None:
    """判据 9：模型判 deep=true → tick() 的 deep_turns 真的 +1（端到端）。"""
    orch = Orchestrator(repo, FakeLLM(), FakeEmbedder(), event_judge=_judge(JudgeProvider(_payload())))
    session = orch.create_session(persona.id, "u_deep")
    _turn(orch, session.id, _HOUSE)

    rel = repo.get_relation(persona.id, "u_deep")
    assert rel is not None and rel.deep_turns == 1


def test_judgement_10_self_disclosure_really_increments(
    repo: SQLiteRepository, persona: PersonaCard
) -> None:
    """判据 10：模型判 disclosure=true → self_disclosure 真的 +1（端到端）。"""
    judge = _judge(JudgeProvider(_payload(is_deep=False, is_self_disclosure=True)))
    orch = Orchestrator(repo, FakeLLM(), FakeEmbedder(), event_judge=judge)
    session = orch.create_session(persona.id, "u_disc")
    _turn(orch, session.id, _WITHDRAW)

    rel = repo.get_relation(persona.id, "u_disc")
    assert rel is not None and rel.self_disclosure == 1
    assert rel.deep_turns == 0


def test_without_judge_behaviour_is_untouched(
    repo: SQLiteRepository, persona: PersonaCard
) -> None:
    """不注入判官 = 老行为：那句话不算走心（deep_turns 不动）。"""
    orch = Orchestrator(repo, FakeLLM(), FakeEmbedder())
    session = orch.create_session(persona.id, "u_old")
    _turn(orch, session.id, _HOUSE)

    rel = repo.get_relation(persona.id, "u_old")
    assert rel is not None and rel.deep_turns == 0


def test_broken_model_does_not_break_the_conversation(
    repo: SQLiteRepository, persona: PersonaCard
) -> None:
    """判据 3 在真实链路上的形态：模型全挂，对话照常出回复、照常落库。"""
    judge = _judge(JudgeProvider(raises=RuntimeError("model down")))
    orch = Orchestrator(repo, FakeLLM("嗯，我在听。"), FakeEmbedder(), event_judge=judge)
    session = orch.create_session(persona.id, "u_down")

    _turn(orch, session.id, "我今天真的崩溃了")
    turns = repo.list_turns(session.id)
    assert [t.role for t in turns] == ["user", "assistant"]
    assert turns[1].content == "嗯，我在听。"
    # 规则兜底仍然认出了「崩溃」——降级不等于变聋
    rel = repo.get_relation(persona.id, "u_down")
    assert rel is not None and rel.deep_turns == 1


def test_conversation_is_not_slowed_past_the_cap(
    repo: SQLiteRepository, persona: PersonaCard
) -> None:
    """超时上限在真实链路上也成立：模型再慢，一轮对话不被拖过太远。"""
    judge = _judge(JudgeProvider(delay=0.5), timeout=0.15)
    orch = Orchestrator(repo, FakeLLM(), FakeEmbedder(), event_judge=judge)
    session = orch.create_session(persona.id, "u_slow")

    started = time.monotonic()
    _turn(orch, session.id, _HOUSE)
    assert time.monotonic() - started < 2.0


def test_impacts_still_come_from_rules(
    repo: SQLiteRepository, persona: PersonaCard
) -> None:
    """情绪冲击仍由规则算：模型不碰 dp/da/dd/salience。

    这是与 issue 示例 JSON 的一处刻意偏差——模型给浮点数没法测、会抖动、
    会越界，而「情绪移动多少」本来就是产品刻度，不是语言理解问题。
    """
    judge = _judge(JudgeProvider(_payload()))
    orch = Orchestrator(repo, FakeLLM(), FakeEmbedder(), event_judge=judge)
    session = orch.create_session(persona.id, "u_impacts")
    done = _turn(orch, session.id, "我今天真的崩溃了")

    trace = repo.get_trace(done["turn_id"])
    assert trace is not None
    assert trace["impacts"] == [i.model_dump() for i in evaluator.evaluate("我今天真的崩溃了")]


def test_end_to_end_with_mock_model_and_rules(
    repo: SQLiteRepository, persona: PersonaCard
) -> None:
    """最短证据链：Orchestrator → EventJudge → llm.generate(EVAL_MARKER)
    → mock 的 JSON → parse_reply → tick(is_deep=True) → deep_turns++。"""
    judge = EventJudge(MockProvider())
    orch = Orchestrator(repo, MockProvider(), HashEmbedder(256), event_judge=judge)
    session = orch.create_session(persona.id, "u_e2e")
    done = _turn(orch, session.id, _HOUSE)

    rel = repo.get_relation(persona.id, "u_e2e")
    assert rel is not None and rel.deep_turns == 1
    assert done["state"]["eval"]["eval_source"] == "model"


# --------------------------------------------------------------------------- #
# 装配：应用层接线 + 提示词契约
# --------------------------------------------------------------------------- #


def test_create_app_accepts_injected_judge(tmp_path) -> None:
    """应用层能注入判官（供测试与部署替换），且不因此启动失败。"""
    app = create_app(
        repo=SQLiteRepository(tmp_path / "a.db"), llm=FakeLLM(), event_judge=_judge(JudgeProvider())
    )
    assert app is not None


def test_orchestrator_does_not_build_its_own_model(monkeypatch) -> None:
    """Orchestrator 不自己去取单例：否则每个单测都会顺手起一个模型客户端。

    做法：把「取单例」这个动作换成炸弹，再构造一个不注入判官的 Orchestrator。
    它要是自己去取，这里当场炸。
    """
    import linger.server.app as app_module

    def boom():
        raise AssertionError("Orchestrator 不该自己去取事件评估单例")

    monkeypatch.setattr(app_module, "get_event_judge", boom)
    orch = Orchestrator(SQLiteRepository.__new__(SQLiteRepository), FakeLLM(), FakeEmbedder())
    assert orch.event_judge.provider is None


def test_prompt_carries_marker_and_field_contract() -> None:
    """提示词必须带标记（mock 靠它认领请求）与字段契约（模型靠它答对）。"""
    provider = JudgeProvider(_payload())
    _judge(provider).evaluate(_HOUSE)
    system = provider.systems[0]
    assert EVAL_MARKER in system
    for key in ("is_deep", "is_self_disclosure", "reason", "kind"):
        assert key in system


def test_parse_reply_is_three_valued() -> None:
    """解析层必须能区分「看不懂」与「明确 false」，否则降级链是假的。"""
    assert parse_reply(_payload(is_deep=True)) is not None
    assert parse_reply(_payload(is_deep=False)) is not None
    assert parse_reply("") is None
    assert parse_reply('{"kind": "event"}') is None
    assert parse_reply('[1,2,3]') is None


def test_verdict_trace_shape_is_stable() -> None:
    """trace 的字段名是契约（前端/看板会读），不能随手改名。"""
    judge = _judge(JudgeProvider(_payload()))
    trace = judge.evaluate(_HOUSE).trace()
    assert set(trace) == {"eval_source", "reason", "is_deep", "is_self_disclosure", "kind"}
