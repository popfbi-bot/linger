"""评分：用例分 → 类别分 → 总分，以及「红还是绿」的判定。

阈值哲学（判据 7 要写清楚的那一条）：

    评测的价值不在「测得多细」，在「红的时候团队还愿意看一眼」。
    所以阈值一律取整、并在 README 里写明来历：用 12 条用例在**未改动实现**的
    基线上跑出来的分，向下取整到 0.05 的整数倍。基线本来就贴着天花板，
    留出的余量是给「同一件事换个说法」的，不是给「明显退化」的。

退出的规则只有一条：**任一类别低于它自己的下限就非 0**。
不做「总分加权」——总分会被强势类别拉平，一个类别彻底崩了却还在绿区，
那是最难查的一种红。
"""

from __future__ import annotations

from collections import OrderedDict
from dataclasses import dataclass, field

from .case import Case, Check, TurnLog

# --------------------------------------------------------------------------- #
# 阈值（来历见 evals/README.md「阈值是怎么定的」）
# --------------------------------------------------------------------------- #

# 单个用例及格线。低于它，这个用例在报告里标 FAIL。
CASE_THRESHOLD = 0.80

# 每个类别的下限。任一类别低于它 → 退出码 1。
CATEGORY_FLOOR: dict[str, float] = OrderedDict(
    [
        ("persona", 0.85),
        ("memory", 0.80),
        ("boundary", 0.85),
    ]
)

# 总分下限。只是兜底——真正拦人的是上面两条。
TOTAL_FLOOR = 0.85


@dataclass
class CheckResult:
    name: str
    score: float
    weight: float
    why: str = ""

    @property
    def passed(self) -> bool:
        # 单条判据的「挂没挂」只用于展示；用例是否 FAIL 看加权后的总分。
        return self.score >= 0.5


@dataclass
class CaseResult:
    case: Case
    checks: list[CheckResult] = field(default_factory=list)
    skipped: bool = False
    skip_reason: str = ""
    error: str = ""

    @property
    def score(self) -> float:
        if self.skipped or self.error:
            return 0.0
        total_w = sum(c.weight for c in self.checks)
        if total_w <= 0:
            return 0.0
        return round(sum(c.score * c.weight for c in self.checks) / total_w, 4)

    @property
    def failed_checks(self) -> list[CheckResult]:
        return [c for c in self.checks if not c.passed]


def evaluate_case(case: Case, log: TurnLog) -> CaseResult:
    """跑一个用例的全部判据。判据抛异常记为该条 0 分并写明——不让它炸掉整轮评测。"""
    result = CaseResult(case=case)
    for check in case.checks:
        try:
            raw = check.fn(log)
        except Exception as exc:  # noqa: BLE001 - 判据自己的 bug 不该拖垮其它判据
            result.checks.append(
                CheckResult(f"{check.name}（判据自身报错）", 0.0, check.weight,
                            f"{type(exc).__name__}: {exc}")
            )
            continue
        result.checks.append(
            CheckResult(check.name, _clamp(raw), check.weight, check.why)
        )
    return result


def _clamp(x: object) -> float:
    try:
        v = float(x)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return 0.0
    if v != v:  # NaN
        return 0.0
    return max(0.0, min(1.0, round(v, 4)))


def category_scores(results: list[CaseResult]) -> dict[str, float]:
    """类别分 = 该类别下**有效用例**的平均分。skip / error 的不计入分母。

    为什么跳过的不计入：缺 key 是环境事实，不是代码退化。把它算成 0 分
    等于「没有 key 就永远红」——那正是判据 5 / 6 明令禁止的形态。
    """
    buckets: dict[str, list[float]] = {}
    for r in results:
        if r.skipped or r.error:
            continue
        buckets.setdefault(r.case.category, []).append(r.score)
    return {
        cat: round(sum(scores) / len(scores), 4)
        for cat, scores in buckets.items()
        if scores
    }


def total_score(results: list[CaseResult]) -> float:
    valid = [r.score for r in results if not r.skipped and not r.error]
    if not valid:
        return 0.0
    return round(sum(valid) / len(valid), 4)


def verdict(results: list[CaseResult]) -> tuple[bool, list[str]]:
    """返回 (是否通过, 失败原因列表)。原因要能直接贴进 CI 日志。"""
    reasons: list[str] = []

    cats = category_scores(results)
    for cat, floor in CATEGORY_FLOOR.items():
        if cat not in cats:
            # 该类全 skip（例如真实模型用例）时不算失败——但要说出来。
            continue
        if cats[cat] < floor:
            reasons.append(f"类别 {cat} 得分 {cats[cat]:.2f} 低于下限 {floor:.2f}")

    for r in results:
        if r.error:
            reasons.append(f"用例 {r.case.id} 执行失败：{r.error}")
        elif not r.skipped and r.score < CASE_THRESHOLD:
            names = "、".join(c.name for c in r.failed_checks) or "（无单条判据挂，但加权后低于及格线）"
            reasons.append(f"用例 {r.case.id} 得分 {r.score:.2f} 低于及格线 {CASE_THRESHOLD:.2f}｜挂：{names}")

    # 总分兜底只在**确实跑过用例**时生效。
    # 全部 skip（缺 key 的精简跑法）时总分是 0.00，那是「没测」不是「测挂了」——
    # 拿它判红，就等于「没有 key 就永远红」，正是判据 5/6 禁止的形态。
    if any(not r.skipped and not r.error for r in results):
        total = total_score(results)
        if total < TOTAL_FLOOR:
            reasons.append(f"总分 {total:.2f} 低于下限 {TOTAL_FLOOR:.2f}")

    return (not reasons), reasons


# --------------------------------------------------------------------------- #
# 报告
# --------------------------------------------------------------------------- #


def render(results: list[CaseResult], *, verbose: bool = False) -> str:
    """人读的报告。CI 日志里第一屏就要能看出「哪一类掉了、掉在哪个判据」。"""
    lines: list[str] = []
    lines.append("=" * 68)
    lines.append("Linger 评测报告")
    lines.append("=" * 68)

    for r in results:
        if r.error:
            lines.append(f"[ERROR] {r.case.id:<38} {r.case.title}")
            lines.append(f"        执行失败：{r.error}")
            continue
        if r.skipped:
            lines.append(f"[SKIP ] {r.case.id:<38} {r.case.title}")
            lines.append(f"        {r.skip_reason}")
            continue

        flag = "PASS" if r.score >= CASE_THRESHOLD else "FAIL"
        lines.append(f"[{flag}] {r.case.id:<36} {r.score:.2f}  {r.case.title}")
        for c in r.checks:
            mark = "ok " if c.passed else "!! "
            lines.append(f"        {mark}{c.name:<28} {c.score:.2f}  (权重 {c.weight:g})")
            if not c.passed and c.why:
                lines.append(f"           └ {c.why}")

    lines.append("-" * 68)
    cats = category_scores(results)
    for cat, floor in CATEGORY_FLOOR.items():
        if cat not in cats:
            lines.append(f"{cat:<10} （无有效用例）")
            continue
        flag = "ok" if cats[cat] >= floor else "!!"
        lines.append(f"{cat:<10} {cats[cat]:.2f}   下限 {floor:.2f}  {flag}")
    lines.append(f"{'总分':<9} {total_score(results):.2f}   下限 {TOTAL_FLOOR:.2f}")

    skipped = [r for r in results if r.skipped]
    errors = [r for r in results if r.error]
    lines.append("-" * 68)
    lines.append(
        f"用例 {len(results)} 条｜有效 {len([r for r in results if not r.skipped and not r.error])}"
        f"｜skip {len(skipped)}｜error {len(errors)}"
    )

    ok, reasons = verdict(results)
    if ok:
        lines.append("结论：通过")
    else:
        lines.append("结论：不通过")
        for reason in reasons:
            lines.append(f"  - {reason}")

    if verbose:
        lines.append("")
        lines.append("（--verbose：未展开逐轮原文；需要时用 --dump <case_id>）")

    lines.append("=" * 68)
    return "\n".join(lines)
