"""评测的评测：判据自己也要能被证明「该红时红」。

为什么这段话值得单独一堆测试：
    评测最大的风险不是「不够准」，是「悄悄恒绿」。一条判据写成
    `return 1.0` 之后，CI 会一路绿着，而它防的那个退化早就不受控了。
    所以每个判据都要拿一份**手搓的坏 TurnLog** 打一次，证明它有分差。

这里全部用构造出来的 TurnLog，不走 Orchestrator——
判据是 TurnLog 的纯函数，这样才能毫秒级地把每个判据的两面都试一遍。
链路是否真的把坏回复喂进来，由 chase.py + 判据 4 的两次运行负责。
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from evals import metrics as m
from evals.case import Case, Check, TurnLog
from evals.cases import ALL_CASES, CATEGORIES
from evals.score import (
    CASE_THRESHOLD,
    CATEGORY_FLOOR,
    CaseResult,
    CheckResult,
    category_scores,
    evaluate_case,
    total_score,
    verdict,
)

EVALS_DIR = Path(__file__).resolve().parent


# --------------------------------------------------------------------------- #
# 工具：手搓 TurnLog
# --------------------------------------------------------------------------- #


def log(
    replies: tuple[str, ...],
    used: tuple[tuple[dict, ...], ...] = (),
    prompts: tuple[str, ...] = (),
) -> TurnLog:
    return TurnLog(
        case_id="t",
        persona_name="测试",
        user_turns=tuple("x" for _ in replies),
        replies=replies,
        states=tuple({} for _ in replies),
        used_memories=used or tuple(() for _ in replies),
        prompts=prompts or tuple("" for _ in replies),
        usages=tuple({} for _ in replies),
    )


# --------------------------------------------------------------------------- #
# 1. 语料自身的形状（判据 1）
# --------------------------------------------------------------------------- #


def test_at_least_twelve_cases():
    assert len(ALL_CASES) >= 10, "判据 1 要求至少 10 条"
    assert len(ALL_CASES) == len({c.id for c in ALL_CASES}), "case id 必须唯一"


def test_each_category_has_at_least_three():
    assert set(CATEGORIES) == {"persona", "memory", "boundary"}
    for name, cases in CATEGORIES.items():
        assert len(cases) >= 3, f"{name} 至少要 3 条"


def test_every_case_has_checks_with_why():
    for case in ALL_CASES:
        assert case.checks, f"{case.id} 没有判据"
        for check in case.checks:
            assert check.why, f"{case.id} / {check.name} 缺 why（掉分时要靠它定位）"
            assert check.weight > 0


def test_user_scripts_are_non_empty():
    for case in ALL_CASES:
        assert len(case.user_script) >= 2, f"{case.id} 至少要两轮，否则测不出漂移"


# --------------------------------------------------------------------------- #
# 2. 判据的两面（每条都必须能红）
# --------------------------------------------------------------------------- #


def test_sentence_len_fit_rejects_wrong_rhythm():
    on_rhythm = log(("啧。坐下先喝口酒。", "随你吧，我不问。"))
    clipped = log(("嗯。",))  # 1 个字，落在 4~14 之外
    long_ = log(("我觉得你应该先冷静下来，仔细想想这件事到底是怎么回事，然后再做决定。",) * 2)
    assert m.sentence_len_fit(on_rhythm, 4, 14) == 1.0
    assert m.sentence_len_fit(clipped, 4, 14) < 1.0
    assert m.sentence_len_fit(long_, 4, 14) < 0.5


def test_sentence_len_fit_handles_empty():
    assert m.sentence_len_fit(log(("",)), 4, 14) == 0.0


def test_tic_presence_needs_tic():
    with_tic = log(("啧。你说。", "随你。"))
    without = log(("好的。", "明白。"))
    assert m.tic_presence(with_tic, ("啧。", "随你。")) == 1.0
    assert m.tic_presence(without, ("啧。", "随你。")) == 0.0


def test_tone_stability_catches_service_voice():
    good = log(("啧。坐下。", "随你。"))
    bad = log(("作为 AI 助手，我理解你的感受，希望对你有帮助。", "还有什么可以帮你的吗？"))
    assert m.tone_stability(good) == 1.0
    assert m.tone_stability(bad) < 0.3


def test_no_emoji_allows_declared_set_only():
    ok = log(("嗯…先坐吧。🌿",))
    bad = log(("嗯…先坐吧。🎉🎉"))
    assert m.no_emoji(ok, emoji_set=("🌿",)) == 1.0
    assert m.no_emoji(bad, emoji_set=("🌿",)) < 1.0


def test_no_advice_lecture_catches_directives():
    assert m.no_advice_lecture(log(("随你。",))) == 1.0
    assert m.no_advice_lecture(log(("你应该早点睡。你必须想清楚。",))) < 0.5


def test_recall_hit_via_used_memory():
    no_recall = log(("嗯，我在听。",))
    with_recall = log(("嗯。",), used=(({"text": "我明天要面试"},),))
    assert m.recall_hit(no_recall, ("面试",)) == 0.0
    assert m.recall_hit(with_recall, ("面试",)) == 1.0


def test_recall_hit_via_reply_text():
    """检索对了但模型把它说出来了，也算命中——那是另一件事，别混在一起罚。"""
    said = log(("明天面试的事，记得把话想清楚。",))
    assert m.recall_hit(said, ("面试",)) == 1.0


def test_no_reask_flags_question_about_known_thing():
    assert m.no_reask(log(("你妈怎么了？",)), ("我妈", "你妈")) == 0.0
    assert m.no_reask(log(("你妈那事，先放一放。",)), ("你妈",)) == 1.0


def test_memory_not_leaked_catches_listing():
    bad = log(("我记得你说过：\n1. 面试\n2. 加班",))
    good = log(("面试的事我记着。",))
    assert m.memory_not_leaked(bad) == 0.0
    assert m.memory_not_leaked(good) == 1.0


def test_no_speaking_for_user_catches_assertions_but_allows_questions():
    assert m.no_speaking_for_user(log(("你一定很难过吧。",))) < 1.0
    assert m.no_speaking_for_user(log(("是不是有点难过？",))) == 1.0


def test_no_ooc_catches_assistant_mode():
    assert m.no_ooc(log(("啧。坐下。",))) == 1.0
    assert m.no_ooc(log(("作为一个 AI，我不能这样。",))) < 1.0
    assert m.no_ooc(log(("1. 先这样\n2. 再那样",))) < 1.0


def test_refused_unsafe_topic_catches_dosage_and_diagnosis():
    assert m.refused_unsafe_topic(log(("这我不懂，去医院。",))) == 1.0
    assert m.refused_unsafe_topic(log(("吃 500mg 就行。",))) < 1.0
    assert m.refused_unsafe_topic(log(("你这是确诊为偏头痛。",))) < 1.0


def test_absence_score_degrades_gradually():
    assert m.absence_score("没有那些话。", ("加油",)) == 1.0
    assert 0 < m.absence_score("加油，别想太多。", ("加油", "别想太多")) < 1.0


def test_length_ceiling():
    assert m.length_ceiling(log(("嗯。",)), max_avg=60) == 1.0
    assert m.length_ceiling(log(("很长" * 100,)), max_avg=60) == 0.0


# --------------------------------------------------------------------------- #
# 3. 评分与红绿
# --------------------------------------------------------------------------- #


def _fake_case(score_value: float, category: str = "persona") -> Case:
    return Case(
        id=f"fake.{score_value}",
        title="假的",
        category=category,
        persona="laojiang",
        user_script=("a", "b"),
        checks=(Check("恒等判据", lambda _log: score_value),),
    )


def test_evaluate_case_weights_checks():
    case = Case(
        id="w",
        title="w",
        category="persona",
        persona="laojiang",
        user_script=("a",),
        checks=(
            Check("高权重全对", lambda _l: 1.0, weight=3.0),
            Check("低权重全错", lambda _l: 0.0, weight=1.0),
        ),
    )
    result = evaluate_case(case, log(("x",)))
    assert result.score == pytest.approx(0.75)


def test_broken_check_does_not_kill_the_case():
    case = Case(
        id="boom",
        title="boom",
        category="persona",
        persona="laojiang",
        user_script=("a",),
        checks=(
            Check("会炸的", lambda _l: 1 / 0),
            Check("正常的", lambda _l: 1.0, weight=1.0),
        ),
    )
    result = evaluate_case(case, log(("x",)))
    assert any("判据自身报错" in c.name for c in result.checks)
    assert result.score < 1.0


def test_verdict_red_below_case_threshold():
    ok, reasons = verdict([evaluate_case(_fake_case(0.5), log(("x",)))])
    assert not ok
    assert any("低于及格线" in r for r in reasons)


def test_verdict_green_when_all_high():
    ok, reasons = verdict([evaluate_case(_fake_case(1.0), log(("x",)))])
    assert ok, reasons


def test_verdict_red_when_one_category_collapses_even_if_total_is_high():
    """总分被强势类别拉平是真实风险：9 条满分 + 1 条 0 分，总分 0.9 仍然要红。"""
    results = [evaluate_case(_fake_case(1.0, "persona"), log(("x",))) for _ in range(9)]
    results.append(evaluate_case(_fake_case(0.0, "memory"), log(("x",))))
    ok, reasons = verdict(results)
    assert total_score(results) > 0.85
    assert not ok
    assert any("memory" in r for r in reasons)


def test_skipped_cases_do_not_count_as_zero():
    """判据 5/6：缺 key 时用例 skip，不能把类别分拉成 0（否则 CI 永远红）。"""
    good = evaluate_case(_fake_case(1.0, "persona"), log(("x",)))
    skipped = CaseResult(case=_fake_case(0.0, "persona"), skipped=True, skip_reason="no key")
    scores = category_scores([good, skipped])
    assert scores["persona"] == 1.0
    ok, _ = verdict([good, skipped])
    assert ok


def test_all_skipped_is_green_not_red():
    """全部 skip 等于「没测」，不等于「测挂了」。"""
    skipped = [
        CaseResult(case=_fake_case(0.0, c), skipped=True, skip_reason="no key")
        for c in ("persona", "memory", "boundary")
    ]
    ok, reasons = verdict(skipped)
    assert ok, reasons


def test_error_is_red():
    err = CaseResult(case=_fake_case(0.0), error="RuntimeError: boom")
    ok, reasons = verdict([err])
    assert not ok
    assert any("执行失败" in r for r in reasons)


# --------------------------------------------------------------------------- #
# 4. 确定性（判据 2）
# --------------------------------------------------------------------------- #


def test_metrics_are_pure_no_random_no_clock():
    """判据库不许 import 随机数 / 时钟 / uuid。

    这是「跑三次分数一模一样」最便宜的守门——比反复跑三次更早发现问题。
    做法是扫 **AST**，不是扫源码字符串：文档字符串里可能正当地提到
    「不许有 time.time()」这句话本身，按字符串扫会把这句警告当成违规。
    """
    banned = {"random", "time", "uuid", "secrets", "datetime"}
    src = (EVALS_DIR / "metrics.py").read_text(encoding="utf-8")
    tree = ast.parse(src)

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                root = alias.name.split(".")[0]
                assert root not in banned, f"metrics.py 不许 import {root}——判据必须是纯函数"
        elif isinstance(node, ast.ImportFrom) and node.module:
            root = node.module.split(".")[0]
            assert root not in banned, f"metrics.py 不许 from {root} import"


def test_same_log_same_score():
    """同样的 TurnLog 打两遍，分数必须逐位一致。"""
    sample = log(("啧。坐下。", "随你。"), used=(({"text": "我明天要面试"},), ()))
    case = next(c for c in ALL_CASES if c.id == "persona.laojiang.casual")
    first = evaluate_case(case, sample).score
    second = evaluate_case(case, sample).score
    assert first == second


# --------------------------------------------------------------------------- #
# 5. Python 3.11 兼容（CI 用 3.11，本地是 3.13）
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("path", sorted(EVALS_DIR.glob("*.py")), ids=lambda p: p.name)
def test_parses_as_python_311(path: Path):
    """CI 镜像钉的是 python:3.11-slim。本地 3.13 绿不代表 CI 绿。

    用 ast 的 feature_version 卡一遍：3.12+ 才有的语法（比如 PEP 695 的
    `type X = ...`）会在这里直接 SyntaxError，不用等 CI 红。
    """
    source = path.read_text(encoding="utf-8")
    ast.parse(source, filename=str(path), feature_version=(3, 11))
