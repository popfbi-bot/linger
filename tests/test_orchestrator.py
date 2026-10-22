"""Orchestrator 的行为判据（docs §2.3 / §3.3.3 / §6.2）。

每个用例都是「先弄坏 → 证明它红 → 再修好 → 证明它绿」。
红绿对比记录见 PR 描述。

测试里有两条容易被误读的约定：
    · 关系等级 0 时编译器禁用一切记忆引用（坑 #3），所以凡是要看记忆注入的用例，
      都先把关系推到 level ≥ 1。
    · 「写记忆」指把用户的话提炼成 L2~L5 的记忆条目（禁令 3），
      不是把 L0 的原始轮次写进 turn 表——那是每题开场就做的事。
"""

from __future__ import annotations

import threading
import time
from datetime import datetime, timezone

import pytest
from _fakes import FakeLLM

from linger.models import MemoryNode
from linger.orchestrator import Orchestrator
from linger.relation.engine import tick
from linger.server import ServerEvent

# --------------------------------------------------------------------------- #
# 工具
# --------------------------------------------------------------------------- #


def build(repo, persona, embedder, llm=None, **kw) -> tuple[Orchestrator, FakeLLM]:
    llm = llm or FakeLLM()
    return Orchestrator(repo, llm, embedder, **kw), llm


def add_memory(repo, persona, embedder, text: str, layer: int = 3) -> MemoryNode:
    node = MemoryNode(
        persona_id=persona.id,
        user_id="u1",
        layer=layer,
        type="fact",
        content=text,
        source_type="user_said",
        embedding=embedder.embed(text),
    )
    repo.add_memories([node])
    return node


def warm_up(repo, persona, embedder, user_id: str = "u1") -> None:
    """把关系推到 level ≥ 1，否则编译器会按「首轮不许提记忆」把记忆块屏蔽掉。"""
    rel = repo.get_relation(persona.id, user_id)
    if rel is None:
        from linger.models import RelationState

        rel = RelationState(persona_id=persona.id, user_id=user_id)
    for _ in range(2):
        rel = tick(rel, is_deep=True, disclosed=False, resonated=True)
    repo.save_relation(rel)
    assert rel.level >= 1


def memory_writes(monkeypatch, repo) -> list[datetime]:
    """记录每一次写记忆发生的时刻。"""
    stamps: list[datetime] = []
    original = repo.add_memories

    def spy(nodes):
        stamps.append(datetime.now(timezone.utc))
        return original(nodes)

    monkeypatch.setattr(repo, "add_memories", spy)
    return stamps


# --------------------------------------------------------------------------- #
# 1. 写记忆的时机（禁令 3）——反例测试
# --------------------------------------------------------------------------- #


def test_memory_written_after_reply_not_before(repo, persona, real_embedder, monkeypatch):
    """反例：把写记忆挪到生成回复之前，这条必须红。

    判据不是「有没有写」，而是「写的时刻是否晚于回复落库」。
    """
    orch, _ = build(repo, persona, real_embedder)
    session = orch.create_session(persona.id, "u1")

    stamps = memory_writes(monkeypatch, repo)
    original_append = repo.append_turn

    def append_and_track(session_id, role, content, affect=None):
        turn_id = original_append(session_id, role, content, affect)
        if role == "assistant":
            stamps.append(("assistant_turn_written", turn_id))
        return turn_id

    monkeypatch.setattr(repo, "append_turn", append_and_track)

    list(orch.send_message(session.id, "我妈妈下周要去医院复查。"))

    kinds = [s[0] if isinstance(s, tuple) else "memory" for s in stamps]
    assert "assistant_turn_written" in kinds, "回复必须落库"
    first_memory = kinds.index("memory")
    reply_at = kinds.index("assistant_turn_written")
    assert reply_at < first_memory, f"记忆写入早于回复落库: {kinds}"
    assert repo.list_memories(persona.id, "u1"), "这一轮本该提炼出记忆"


def test_memory_writer_sees_only_persisted_user_turns(repo, persona, real_embedder):
    """写记忆拿到的输入必须是「已经入库的用户轮次」，不能是尚在生成的回复。"""
    seen: list[list[str]] = []

    def writer(session, card, turns):
        seen.append([(t.role, t.content) for t in turns])
        return []

    orch, _ = build(repo, persona, real_embedder, memory_writer=writer)
    session = orch.create_session(persona.id, "u1")
    list(orch.send_message(session.id, "我妈妈下周要去医院复查。"))

    assert seen == [[("user", "我妈妈下周要去医院复查。")]]
    assert [t.role for t in repo.list_turns(session.id)] == ["user", "assistant"]


# --------------------------------------------------------------------------- #
# 2. 相关性闸门
# --------------------------------------------------------------------------- #


def test_recall_gate_blocks_irrelevant_memory(repo, persona, fake_embedder):
    """低于闸门的记忆不得进入注入——哪怕它重要度拉满、被钉住。"""
    orch, _ = build(repo, persona, fake_embedder)
    session = orch.create_session(persona.id, "u1")
    warm_up(repo, persona, fake_embedder)

    relevant = add_memory(repo, persona, fake_embedder, "面试砸了，答得一团糟")
    irrelevant = add_memory(repo, persona, fake_embedder, "他养了两只猫")
    irrelevant.importance = 0.95
    irrelevant.pinned = True
    repo.update_memory(irrelevant)

    events = list(orch.send_message(session.id, "面试砸了"))
    done = next(e for e in events if e.type == "done")

    used = {m["id"] for m in done.data["used_memories"]}
    assert relevant.id in used
    assert irrelevant.id not in used


def test_trace_reports_gate_counts(repo, persona, fake_embedder):
    """P3 可解释：trace 必须能回答「为什么没用上那条记忆」。"""
    orch, _ = build(repo, persona, fake_embedder)
    session = orch.create_session(persona.id, "u1")
    warm_up(repo, persona, fake_embedder)

    add_memory(repo, persona, fake_embedder, "面试砸了，答得一团糟")
    add_memory(repo, persona, fake_embedder, "他养了两只猫")

    done = next(e for e in orch.send_message(session.id, "面试砸了") if e.type == "done")
    trace = repo.get_trace(done.data["turn_id"])

    assert trace["candidates"] == 2
    assert trace["passed_gate"] == 1
    assert len(trace["selected"]) == 1
    assert trace["query"] == "面试砸了"
    assert set(done.data["used_memories"][0]) >= {"id", "text", "score", "layer"}


# --------------------------------------------------------------------------- #
# 3. 并行：检索与状态更新
# --------------------------------------------------------------------------- #


class _Gate:
    """把某个步骤卡住，直到测试放行。"""

    def __init__(self) -> None:
        self.entered = threading.Event()
        self.release = threading.Event()


def test_parallel_recall_and_state_update(repo, persona, fake_embedder):
    """检索被卡住时，状态更新必须已经跑完——串行的话它只能一直等。"""
    add_memory(repo, persona, fake_embedder, "面试砸了，答得一团糟", layer=3)
    warm_up(repo, persona, fake_embedder)
    orch, _ = build(repo, persona, fake_embedder)
    session = orch.create_session(persona.id, "u1")

    gate = _Gate()
    progress: dict[str, float] = {}

    real_recall = orch._recall
    real_update = orch._update_state

    def blocking_recall(*args):
        gate.entered.set()
        gate.release.wait(timeout=5)
        progress["recall_done"] = time.monotonic()
        return real_recall(*args)

    def watched_update(*args):
        result = real_update(*args)
        progress["state_done"] = time.monotonic()
        return result

    orch._recall = blocking_recall
    orch._update_state = watched_update

    worker = threading.Thread(target=lambda: list(orch.send_message(session.id, "面试砸了")))
    worker.start()
    try:
        assert gate.entered.wait(timeout=2), "流水线没走到检索这一步"

        deadline = time.monotonic() + 2
        while "state_done" not in progress and time.monotonic() < deadline:
            time.sleep(0.01)

        assert "state_done" in progress, "检索被阻塞期间状态更新没有独立完成——这是串行"
        assert "recall_done" not in progress
    finally:
        gate.release.set()
        worker.join(timeout=5)

    assert not worker.is_alive()
    assert "recall_done" in progress
    # 汇总后状态落库：并行算出来的结果被采纳了，不是各算各的
    stored = repo.get_affect(persona.id, "u1")
    assert stored.pad.pleasure != persona.affect.baseline.pleasure


def test_parallel_tasks_share_no_mutable_state(repo, persona, real_embedder):
    """并行跑完两轮，情绪必须是逐轮叠加的，不能因为抢写而互相覆盖。"""
    orch, _ = build(repo, persona, real_embedder)
    session = orch.create_session(persona.id, "u1")

    first = next(e for e in orch.send_message(session.id, "今天面试砸了，估计是没戏了。") if e.type == "done")
    after_first = first.data["state"]["affect"]["pleasure"]
    second = next(e for e in orch.send_message(session.id, "今天面试砸了，估计是没戏了。") if e.type == "done")
    after_second = second.data["state"]["affect"]["pleasure"]

    assert after_second < after_first, "第二轮没有从第一轮的状态出发，说明状态被并行任务覆盖了"


# --------------------------------------------------------------------------- #
# 4. 事件流
# --------------------------------------------------------------------------- #


def test_send_message_returns_events_stream(repo, persona, real_embedder):
    orch, llm = build(repo, persona, real_embedder, llm=FakeLLM(reply="砸到什么程度？"))
    session = orch.create_session(persona.id, "u1")

    stream = orch.send_message(session.id, "今天面试砸了。")
    assert not isinstance(stream, (list, tuple)), "必须是可迭代的事件流，不是一次性算完的列表"

    events = list(stream)
    assert all(isinstance(e, ServerEvent) for e in events)

    types = [e.type for e in events]
    assert types[0] == "thinking"
    assert "affect" in types and "done" in types
    assert types[-1] == "done"
    assert types.count("delta") >= 1

    # 顺序：thinking → affect → delta* → done
    assert types.index("thinking") < types.index("affect") < types.index("delta") < types.index("done")

    # delta 拼起来就是完整回复
    assert "".join(e.data["text"] for e in events if e.type == "delta") == "砸到什么程度？"

    done = events[-1]
    assert set(done.data) == {"turn_id", "used_memories", "state", "usage"}
    assert done.data["usage"]["in"] > 0 and done.data["usage"]["out"] > 0
    assert llm.calls == 1


# --------------------------------------------------------------------------- #
# 5. 人格锚点
# --------------------------------------------------------------------------- #


def test_persona_consistency_anchor_in_prompt(repo, persona, real_embedder):
    """人格锚点必须在 system 最前（可缓存前缀），否则长对话里人格会被稀释。"""
    orch, llm = build(repo, persona, real_embedder)
    session = orch.create_session(persona.id, "u1")

    list(orch.send_message(session.id, "今天面试砸了。"))

    system = llm.prompts[-1]
    assert "你是林汐" in system
    assert system.index("你是林汐") < len(system) // 3, "人格锚点不该出现在 prompt 中后段"

    trace = repo.get_trace(repo.list_turns(session.id)[-1].id)
    assert "你是林汐" in trace["prompt"]


# --------------------------------------------------------------------------- #
# 附加：状态与 trace
# --------------------------------------------------------------------------- #


def test_state_updates_persisted_and_reusable(repo, persona, real_embedder):
    orch, _ = build(repo, persona, real_embedder)
    session = orch.create_session(persona.id, "u1")

    assert repo.get_affect(persona.id, "u1") is not None
    assert repo.get_relation(persona.id, "u1").level == 0

    done = next(e for e in orch.send_message(session.id, "今天面试砸了，估计是没戏了。") if e.type == "done")
    stored = repo.get_affect(persona.id, "u1")
    assert stored.pad.pleasure == pytest.approx(done.data["state"]["affect"]["pleasure"])
    assert repo.get_relation(persona.id, "u1").level == done.data["state"]["relation"]["level"]


def test_unknown_session_raises(repo, persona, real_embedder):
    orch, _ = build(repo, persona, real_embedder)
    with pytest.raises(ValueError):
        list(orch.send_message("s_missing", "在吗"))


def test_scenario_reaches_the_prompt(repo, persona, real_embedder):
    orch, llm = build(repo, persona, real_embedder)
    session = orch.create_session(persona.id, "u1", scenario="雨天的书店，傍晚，只剩你和常客。")
    list(orch.send_message(session.id, "今天面试砸了。"))
    assert "雨天的书店" in llm.prompts[-1]


def test_recall_strengthens_used_memory(repo, persona, fake_embedder):
    """§3.3.2 ⑤：召回即强化，recall_count 与 strength 都要动。"""
    node = add_memory(repo, persona, fake_embedder, "面试砸了，答得一团糟")
    warm_up(repo, persona, fake_embedder)
    orch, _ = build(repo, persona, fake_embedder)
    session = orch.create_session(persona.id, "u1")

    list(orch.send_message(session.id, "面试砸了"))

    updated = next(m for m in repo.list_memories(persona.id, "u1") if m.id == node.id)
    assert updated.recall_count == 1
    assert updated.strength > 1.0
    assert updated.last_recalled_at is not None
