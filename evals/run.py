"""评测运行器：`python -m evals.run`

退出码只有三种含义：
    0  通过（含「全部 skip」——缺 key 不是失败，判据 5/6）
    1  有类别或用例低于阈值（**这才是唯一的红**）
    2  运行器自己出错（配置写错、找不到用例）

第三种必须和第二种分开：CI 上「评测挂了」和「评测没跑起来」要一眼能分清，
否则红过一次之后就没人再看了。

可选参数：
    --category persona|memory|boundary   只跑一类（本地调 prompt 时用）
    --dump <case_id>                     打印该用例逐轮原文，调漂移时用
    --json                               机器可读结果（给后续接看板留的口子）
"""

from __future__ import annotations

import argparse
import json
import sys
import traceback

from .case import Case
from .cases import CATEGORIES, get_cases
from .chase import run_case
from .score import CaseResult, evaluate_case, render, total_score, verdict


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(prog="python -m evals.run", description="Linger 评测运行器")
    p.add_argument("--category", choices=sorted(CATEGORIES), help="只跑某一类")
    p.add_argument("--case", action="append", default=[], help="只跑指定 case id（可多次）")
    p.add_argument("--dump", metavar="CASE_ID", help="打印该用例逐轮原文")
    p.add_argument("--json", action="store_true", help="输出 JSON 报告")
    p.add_argument("--verbose", action="store_true", help="报告里带更多细节")
    return p.parse_args(argv)


def _select(args: argparse.Namespace) -> tuple[Case, ...]:
    cases = get_cases(args.category)
    if args.case:
        wanted = set(args.case)
        cases = tuple(c for c in cases if c.id in wanted)
    return cases


def _run_all(cases: tuple[Case, ...]) -> list[CaseResult]:
    from .judge import judge_available

    results: list[CaseResult] = []
    llm_cache: dict[str, object] = {}

    for case in cases:
        if case.needs_llm and not judge_available():
            results.append(
                CaseResult(
                    case=case,
                    skipped=True,
                    skip_reason="需要真实模型（LLM-as-judge），未配置 OPENAI_API_KEY —— skip 不是失败",
                )
            )
            continue

        try:
            provider = _provider(case, llm_cache)
            log = run_case(case, llm=provider)
            results.append(evaluate_case(case, log))
        except Exception as exc:  # noqa: BLE001 - 单用例炸掉不该吞掉其它用例的结果
            results.append(
                CaseResult(
                    case=case,
                    error=f"{type(exc).__name__}: {exc}",
                )
            )
    return results


def _provider(case: Case, cache: dict[str, object]):
    """每个用例一个新 provider 实例。

    为什么不复用：MockProvider 带自己的 RNG 状态，跨用例复用会让
    「第 12 个用例的分」取决于「前面跑了几个」——那是隐藏顺序依赖，
    正是判据 2 要消灭的东西。
    """
    from linger.llm import get_provider

    if "llm" not in cache:
        cache["llm"] = get_provider
    return get_provider()


def _dump(case: Case) -> int:
    log = run_case(case)
    print("=" * 68)
    print(f"用例 {case.id} —— {case.title}")
    print(f"人格 {case.persona}｜类别 {case.category}")
    print("=" * 68)
    for i, (user, reply) in enumerate(zip(log.user_turns, log.replies), start=1):
        used = log.used_memories[i - 1] if i - 1 < len(log.used_memories) else ()
        print(f"\n[第 {i} 轮]")
        print(f"  用户：{user}")
        print(f"  她：  {reply.replace(chr(10), ' / ')}")
        print(f"  召回：{[m['text'] for m in used] or '（无）'}")
    print()
    return 0


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)

    try:
        cases = _select(args)
    except Exception as exc:  # noqa: BLE001
        print(f"运行器配置错误：{exc}", file=sys.stderr)
        return 2

    if not cases:
        print("没有匹配的用例——检查 --category / --case 参数", file=sys.stderr)
        return 2

    if args.dump:
        target = next((c for c in cases if c.id == args.dump), None)
        if target is None:
            print(f"找不到用例 {args.dump}", file=sys.stderr)
            return 2
        try:
            return _dump(target)
        except Exception:  # noqa: BLE001
            traceback.print_exc()
            return 2

    try:
        results = _run_all(cases)
    except Exception:  # noqa: BLE001
        traceback.print_exc()
        return 2

    if args.json:
        print(json.dumps(_as_json(results), ensure_ascii=False, indent=2))
    else:
        print(render(results, verbose=args.verbose))

    ok, _reasons = verdict(results)
    return 0 if ok else 1


def _as_json(results: list[CaseResult]) -> dict:
    return {
        "total": total_score(results),
        "cases": [
            {
                "id": r.case.id,
                "category": r.case.category,
                "score": r.score,
                "skipped": r.skipped,
                "error": r.error,
                "checks": [
                    {"name": c.name, "score": c.score, "weight": c.weight, "passed": c.passed}
                    for c in r.checks
                ],
            }
            for r in results
        ],
    }


if __name__ == "__main__":
    sys.exit(main())
