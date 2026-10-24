"""跑一次对话，把产物收成 TurnLog。

刻意**走产品自己的 Orchestrator**，而不是自己拼 prompt 调模型：
    评测要抓的正是「改一句 prompt / 换一个 provider」这类退化。
    如果评测绕开编排层自己拼 prompt，那它测的就是评测自己——改坏编排层永远不会红。

隔离：
    · 每个用例一个临时 SQLite，用完就删；
    · 只用内置人格模板，不读 ~/.linger；
    · 不联网（LINGER_LLM=mock 时 provider 不套韧性层，也就没有线程与超时）。
"""

from __future__ import annotations

import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from linger.memory.embedding import HashEmbedder
from linger.orchestrator import Orchestrator
from linger.persona.templates import load_template
from linger.storage.sqlite import SQLiteRepository

from .case import Case, TurnLog


def get_llm() -> Any:
    """按 LINGER_LLM 装 provider。默认 mock。

    生产评测想跑真实模型时设 `LINGER_LLM=openai` + key 即可；
    但**必过的判据不依赖它**——缺 key 时 provider 构造会抛，调用方负责 skip。
    """
    from linger.llm import get_provider

    return get_provider()


@contextmanager
def _fresh_repo() -> Iterator[SQLiteRepository]:
    with tempfile.TemporaryDirectory(prefix="linger-eval-") as tmp:
        repo = SQLiteRepository(Path(tmp) / "eval.db")
        try:
            yield repo
        finally:
            repo.close()


def run_case(case: Case, llm: Any | None = None) -> TurnLog:
    """跑一个用例，返回 TurnLog。LLM 出错时直接抛——由调用方决定 skip 还是 fail。"""
    provider = llm or get_llm()
    embedder = HashEmbedder(256)

    with _fresh_repo() as repo:
        card = load_template(case.persona)
        repo.save_persona(card)

        orch = Orchestrator(repo, provider, embedder)
        session = orch.create_session(card.id, f"eval_{case.id}")

        replies: list[str] = []
        states: list[dict] = []
        used: list[tuple[dict, ...]] = []
        prompts: list[str] = []
        usages: list[dict] = []

        for text in case.user_script:
            events = list(orch.send_message(session.id, text))
            done = next(e for e in events if e.type == "done")
            replies.append(_last_assistant_turn(repo, session.id))
            states.append(done.data.get("state", {}))
            used.append(tuple(done.data.get("used_memories", ())))
            usages.append(dict(done.data.get("usage", {})))
            trace = repo.get_trace(done.data["turn_id"])
            prompts.append(trace.get("prompt", "") if trace else "")

        return TurnLog(
            case_id=case.id,
            persona_name=card.meta.name,
            user_turns=tuple(case.user_script),
            replies=tuple(replies),
            states=tuple(states),
            used_memories=tuple(used),
            prompts=tuple(prompts),
            usages=tuple(usages),
        )


def _last_assistant_turn(repo: SQLiteRepository, session_id: str) -> str:
    """从库里取最后一条 assistant 轮。

    不拼 delta 事件：流式片段与最终落库内容应当一致，而**落库那份**才是
    后面真的会进 history、下次会喂回给模型的文本。评测要钉的是那一份。
    """
    turns = repo.list_turns(session_id)
    for turn in reversed(turns):
        if turn.role == "assistant":
            return turn.content
    return ""
