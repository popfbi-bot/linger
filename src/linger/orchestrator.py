"""运行时编排：一轮对话的完整链路（docs/ARCHITECTURE.md §2.3 / §6.2）。

流水线顺序是契约，不能重排：

    记忆检索 ┐
             ├─(并行)→ Prompt 编译 → LLM 流式 → 回复落库 → 写记忆 → trace
    状态更新 ┘

场景运行时（M1.5）：`session.scenario_id` 是场景 **id**（不是文本）。
每轮开始读出未完成节拍交给编译器，回复落库之后才按关键词推进并持久化——
和记忆写入同一时机，绝不提前（否则用户一句没说的话也能推进剧情）。

三条硬约束（每条都对应一个已知的坑）：
    · 只注入通过相关性闸门的记忆（坑 #8 检索反噬）。
    · 记忆检索与状态更新并行，且并行任务之间不共享可变状态（mutate 只发生在召回之后）。
    · 回复落库后才写新记忆，绝不在生成回复的同一轮里写（§3.3.3 禁令 3）。
"""

from __future__ import annotations

import re
from collections.abc import Callable, Iterator
from concurrent.futures import ThreadPoolExecutor

from .affect.engine import expression_hints, step
from .affect.evaluator import evaluate, is_deep_turn, is_self_disclosure
from .companion import update_profile as update_companion_profile
from .compose.compiler import compose, estimate_tokens
from .config import settings
from .memory.embedding import Embedder, cosine
from .memory.extractor import dedupe, extract_from_turns
from .memory.retriever import RetrievalResult, retrieve
from .models import (
    AffectState,
    MemoryNode,
    PersonaCard,
    RelationState,
    Session,
    TraceRecord,
    Turn,
    now_utc,
)
from .persona.templates import load_template
from .relation.engine import tick
from .scenario import Scenario, load_scenario
from .server import ServerEvent
from .social.redflag import RedFlagDetector, ToxicSignalDetector, get_detector, get_toxic_detector
from .social.roles import RelationRoleKnowledge, get_roles
from .storage.base import Repository

ENTITY_PATTERN = re.compile(r"[\u4e00-\u9fff]{0,6}(公司|医院|书店|学校)")

# 场景 id 的形态：字母数字下划线，且不含空格与中文标点。
# 用来区分「场景 id」与「一段自由设定的场景文本」（M1 老写法）。
_ID_PATTERN = re.compile(r"^[A-Za-z0-9_.:-]+$")


def _looks_like_id(value: str) -> bool:
    return bool(_ID_PATTERN.match(value.strip()))

# 写记忆的实现可替换（例如带冲突消解 / 小模型提炼的管线）。
# 约定：只接收已经落库的用户轮次，且只在回复落库后被调用。
MemoryWriter = Callable[[Session, PersonaCard, list[Turn]], list[MemoryNode]]


class PersonaFactory:
    """persona_id → 人格卡。P1 状态外置：人格是库里的一行，不是代码里的一个字面量。"""

    def create(self, template_id: str, repo: Repository | None = None) -> PersonaCard:
        card = load_template(template_id)
        if repo is not None:
            repo.save_persona(card)
        return card

    def get(self, repo: Repository, persona_id: str) -> PersonaCard:
        card = repo.get_persona(persona_id)
        if card is None:
            raise ValueError(f"人格不存在: {persona_id}（先用 PersonaFactory.create() 实例化模板）")
        return card


class Orchestrator:
    def __init__(
        self,
        repo: Repository,
        llm,
        embedder: Embedder,
        persona_factory: PersonaFactory | None = None,
        memory_writer: MemoryWriter | None = None,
        recall_k: int | None = None,
        recall_gate: float | None = None,
        budget_tokens: int | None = None,
        redflag_detector: RedFlagDetector | None = None,
        toxic_detector: ToxicSignalDetector | None = None,
        role_knowledge: RelationRoleKnowledge | None = None,
    ) -> None:
        self.repo = repo
        self.llm = llm
        self.embedder = embedder
        self.persona_factory = persona_factory or PersonaFactory()
        self.memory_writer = memory_writer or self._default_memory_writer
        self.recall_k = settings.recall_k if recall_k is None else recall_k
        self.recall_gate = settings.recall_gate if recall_gate is None else recall_gate
        self.budget_tokens = settings.context_budget_tokens if budget_tokens is None else budget_tokens
        self.redflag_detector = redflag_detector or get_detector()
        self.toxic_detector = toxic_detector or get_toxic_detector()
        self.role_knowledge = role_knowledge or get_roles()

    # ------------------------------------------------------------------ #
    # 会话
    # ------------------------------------------------------------------ #
    def create_session(
        self,
        persona_id: str,
        user_id: str,
        scenario_id: str | None = None,
        scenario: str | None = None,
    ) -> Session:
        """开一个会话。

        `scenario_id` 语义是场景 id（如 `rainy_bookstore`），不存在抛 `ValueError`。
        向后兼容：老调用用关键字 `scenario=` 传一段场景文本，此时按原样当作自由设定，
        `Session.scenario_id` 里存的仍是那段文本，编译器退化成拼接行为。
        """
        persona = self.persona_factory.get(self.repo, persona_id)
        scenario_id = scenario_id if scenario_id is not None else scenario
        resolved = self._resolve_scenario(scenario_id)
        session = Session(
            persona_id=persona_id,
            user_id=user_id,
            scenario_id=(resolved.id if isinstance(resolved, Scenario) else scenario_id),
        )
        self.repo.save_session(session)
        if isinstance(resolved, Scenario):
            self.repo.save_scenario_progress(session.id, resolved.progress())
        if self.repo.get_affect(persona_id, user_id) is None:
            self.repo.save_affect(
                persona_id,
                user_id,
                AffectState(pad=persona.affect.baseline, label="中性", updated_at=now_utc()),
            )
        if self.repo.get_relation(persona_id, user_id) is None:
            self.repo.save_relation(RelationState(persona_id=persona_id, user_id=user_id))
        return session

    # ------------------------------------------------------------------ #
    # 场景
    # ------------------------------------------------------------------ #
    def _resolve_scenario(self, scenario_id: str | None) -> Scenario | str | None:
        """场景 id → Scenario；非 id 的老式自由文本原样返回。

        先按 id 查，查不到再退一步：文本里带空格/标点（不像 id）就当自由设定。
        这样 M1 的 `create_session(..., scenario="雨天的书店，傍晚…")` 不会炸。
        """
        if not scenario_id:
            return None
        try:
            return load_scenario(scenario_id)
        except FileNotFoundError:
            if _looks_like_id(scenario_id):
                raise ValueError(f"场景不存在: {scenario_id}") from None
            return scenario_id

    def _load_session_scenario(self, session: Session) -> Scenario | str | None:
        if not session.scenario_id:
            return None
        try:
            return load_scenario(session.scenario_id)
        except FileNotFoundError:
            # 会话里存的是老式自由文本（M1 写法），或场景 yaml 被删了
            return session.scenario_id if not _looks_like_id(session.scenario_id) else None

    # ------------------------------------------------------------------ #
    # 单轮
    # ------------------------------------------------------------------ #
    def send_message(self, session_id: str, content: str) -> Iterator[ServerEvent]:
        session = self.repo.get_session(session_id)
        if session is None:
            raise ValueError(f"会话不存在: {session_id}")
        persona = self.persona_factory.get(self.repo, session.persona_id)

        # L0：用户发言即刻入库。它是记忆唯一的来源锚点（禁令 2）。
        # 写入它不算「写记忆」——记忆指 L2~L5 的提炼产物。
        user_turn_id = self.repo.append_turn(session_id, "user", content)
        user_turn = Turn(id=user_turn_id, session_id=session_id, role="user", content=content)

        # 场景进度：每轮开始读出未完成节拍。放在 LLM 之前，
        # 与 affect/relation 一样属于「这一轮的状态输入」。
        scenario = self._load_session_scenario(session)
        if isinstance(scenario, Scenario):
            scenario.restore(self.repo.get_scenario_progress(session.id))

        recall, state = self._recall_and_update(
            session, persona, content, user_turn_id, scenario
        )

        yield ServerEvent.thinking(
            "recall",
            count=len(recall.selected),
            candidates=recall.candidates,
            passed_gate=recall.passed_gate,
        )
        yield ServerEvent.affect(state.affect.label, state.affect.pad.model_dump(),
                                 relation_level=state.relation.level)
        yield ServerEvent.thinking("compose", tokens=state.prompt.total_tokens)

        # ---- LLM 流式 ----
        reply_parts: list[str] = []
        for delta in self.llm.generate(state.prompt.system, state.messages, ctx=state.llm_ctx()):
            if not delta:
                continue
            reply_parts.append(delta)
            yield ServerEvent.delta(delta)
        reply = "".join(reply_parts)

        # ---- 回复落库 → 之后才写记忆（禁令 3）----
        assistant_turn_id = self.repo.append_turn(
            session_id, "assistant", reply, affect=state.affect.pad.model_dump()
        )
        self.memory_writer(session, persona, [user_turn])

        # 陪伴档案：与写记忆同一时机（回复落库之后）。提前写就等于
        # 「用户还没被回应，系统先给他贴了标签」——这是 §3.3.3 禁令的同一条。
        companion = update_companion_profile(
            self.repo,
            persona.id,
            session.user_id,
            content,
            detected_roles=state.roles,
            toxic_signals=state.toxic_signals,
        )

        # 节拍推进：与写记忆同一时机（回复落库之后）。绝不提前——
        # 否则用户还没说的话就能推进剧情。
        scenario_state = self._advance_scenario(session, scenario, content)

        self._strengthen(recall)
        session.last_active_at = now_utc()
        self.repo.save_session(session)

        usage = {"in": state.prompt.total_tokens, "out": estimate_tokens(reply), "cached": 0}
        self.repo.save_trace(self._build_trace(assistant_turn_id, session, recall, state, usage))

        yield ServerEvent.done(
            turn_id=assistant_turn_id,
            used_memories=[
                {
                    "id": s.node.id,
                    "text": s.node.content,
                    "layer": s.node.layer,
                    "score": s.score,
                    "part": s.parts.get("sim"),
                }
                for s in recall.selected
            ],
            state={
                "affect": {"label": state.affect.label, **state.affect.pad.model_dump()},
                "relation": {
                    "level": state.relation.level,
                    "intimacy": state.relation.intimacy,
                    "unresolved_conflicts": state.relation.unresolved_conflicts,
                },
                "red_flags": state.red_flags,
                "toxic_signals": state.toxic_signals,
                "roles": state.roles,
                "companion": companion.model_dump(mode="json"),
                "scenario": scenario_state,
            },
            usage=usage,
        )

    # ------------------------------------------------------------------ #
    # 流水线内部
    # ------------------------------------------------------------------ #
    def _advance_scenario(
        self, session: Session, scenario: Scenario | str | None, content: str
    ) -> dict:
        if not isinstance(scenario, Scenario):
            return {"scenario_id": None}
        if scenario.advance(content):
            self.repo.save_scenario_progress(session.id, scenario.progress())
        return scenario.state()

    def _recall_and_update(
        self,
        session: Session,
        persona: PersonaCard,
        content: str,
        user_turn_id: int,
        scenario: Scenario | str | None = None,
    ) -> tuple[RetrievalResult, _StateSnapshot]:
        """记忆检索 ∥ 状态更新。

        并行任务只读写自己的入参：检索线程拿的是记忆与向量的快照，
        状态线程算出新的 affect/relation 后**不落库**，等汇总后统一写。
        这样「查」与「算」互不阻塞（§6.2），也不会出现一边改一边读的竞争。
        """
        affect_before = self.repo.get_affect(persona.id, session.user_id) or AffectState(
            pad=persona.affect.baseline, label="中性"
        )
        relation_before = self.repo.get_relation(persona.id, session.user_id) or RelationState(
            persona_id=persona.id, user_id=session.user_id
        )
        memories = self.repo.list_memories(persona.id, session.user_id)
        query_vec = self.embedder.embed(content)
        history = self.repo.list_turns(session.id)

        with ThreadPoolExecutor(max_workers=2) as pool:
            recall_future = pool.submit(self._recall, content, query_vec, memories)
            state_future = pool.submit(self._update_state, persona, affect_before, relation_before, content)
            recall = recall_future.result()
            affect_after, relation_after, impacts, red_flags, toxic_signals, roles = state_future.result()

        # 开场旁白只在第一轮注入：此后它已经是历史，再讲一遍就是复读。
        # `history` 里已经包含本轮的 user turn，所以「第一轮」= 只有这一条。
        opening = len(history) <= 1
        prompt = compose(
            persona,
            affect_after,
            relation_after,
            recall.nodes,
            self._history_messages(history),
            scenario=scenario,
            budget_tokens=self.budget_tokens,
            opening=opening,
            roles=roles,
            role_knowledge=self.role_knowledge,
            companion=self.repo.get_companion_profile(persona.id, session.user_id),
        )

        state = _StateSnapshot(
            persona=persona,
            affect=affect_after,
            relation=relation_after,
            affect_before=affect_before,
            impacts=impacts,
            red_flags=red_flags,
            toxic_signals=toxic_signals,
            roles=roles,
            prompt=prompt,
            messages=[{"role": "user", "content": content}],
        )

        # 汇总后统一落库：状态的唯一写入点
        self.repo.save_affect(persona.id, session.user_id, affect_after)
        self.repo.save_relation(relation_after)
        return recall, state

    def _recall(self, query: str, query_vec: list[float], memories: list[MemoryNode]) -> RetrievalResult:
        return retrieve(
            query,
            query_vec,
            memories,
            k=self.recall_k,
            gate=self.recall_gate,
            tau_days=settings.recency_tau_days,
            entities=set(ENTITY_PATTERN.findall(query)),
        )

    def _update_state(
        self,
        persona: PersonaCard,
        affect_before: AffectState,
        relation_before: RelationState,
        content: str,
    ) -> tuple[AffectState, RelationState, list, list[str], list[dict], list[dict]]:
        impacts = evaluate(content)
        affect_after = step(
            affect_before,
            persona.affect.baseline,
            impacts,
            sensitivity=persona.affect.sensitivity,
        )
        # 红灯在状态更新这一侧检测：它属于「这一轮对方做错了什么」，
        # 和情绪冲击一样是对状态的输入，而不是对回复的修饰。
        red_flags = self.redflag_detector.detect(content, relation_before.level)
        # 有毒信号识别：用户正在经历什么（被消耗/被贬低/被忽视）。
        # 只提示，不惩罚——绝不计入 conflict / unresolved_conflicts。
        toxic_signals = [
            {"category": s.category, "text": s.text, "keyword": s.keyword}
            for s in self.toxic_detector.detect(content)
        ]
        # 关系角色识别：用户在关系里是谁（照顾者/受害者/追的人/讨好者……）。
        # 只识别、不惩罚——绝不计入 conflict / unresolved_conflicts。
        roles = [
            {"name": r.name, "dimension": r.dimension, "description": r.description}
            for r in self.role_knowledge.detect(content)
        ]
        relation_after = tick(
            relation_before.model_copy(deep=True),
            is_deep=is_deep_turn(content),
            disclosed=is_self_disclosure(content),
            resonated=affect_after.pad.distance(affect_before.pad) >= 0.1,
            conflict=bool(red_flags),
        )
        return affect_after, relation_after, impacts, [h.brief for h in red_flags], toxic_signals, roles

    def _history_messages(self, turns: list[Turn]) -> list[dict[str, str]]:
        return [{"role": t.role, "content": t.content} for t in turns if t.role in ("user", "assistant")]

    def _default_memory_writer(
        self, session: Session, persona: PersonaCard, turns: list[Turn]
    ) -> list[MemoryNode]:
        """本轮新增的用户轮 → 提炼 → 去重 → 落库。

        只吃「已经在库里」的用户轮次：这个方法在回复落库之后才被调用，
        所以它永远不可能为尚未生成的回复写记忆。
        """
        candidates = extract_from_turns(persona.id, session.user_id, turns, self.embedder)
        if not candidates:
            return []

        existing = self.repo.list_memories(persona.id, session.user_id)
        fresh = dedupe(candidates, existing, self.embedder)
        if fresh:
            self.repo.add_memories(fresh)

        # 被判定为重复的条目累加了 mention_count，需要把合并结果写回
        touched = [e for e in existing if any(cosine(e.embedding or [], c.embedding or []) >= 0.88
                                               for c in candidates)]
        for node in touched:
            self.repo.update_memory(node)
        return fresh

    def _strengthen(self, recall: RetrievalResult) -> None:
        """强化：被召回的条目提高 recall_count。

        「召回了」与「真的出现在回复里」是两件事，后者要后置校验（§3.3.2 ⑤），
        骨架阶段先按召回计，把判别点留在 trace 里。
        """
        for scored in recall.selected:
            node = scored.node
            node.recall_count += 1
            node.strength = round(node.strength * 1.15, 3)
            node.last_recalled_at = now_utc()
            self.repo.update_memory(node)

    def _build_trace(
        self,
        turn_id: int,
        session: Session,
        recall: RetrievalResult,
        state: _StateSnapshot,
        usage: dict[str, int],
    ) -> TraceRecord:
        """P3 可解释：一轮用了什么、为什么、花了多少 token，都写在这里。"""
        return TraceRecord(
            turn_id=turn_id,
            persona_id=session.persona_id,
            user_id=session.user_id,
            query=recall.query,
            candidates=recall.candidates,
            passed_gate=recall.passed_gate,
            selected=[
                {
                    "id": s.node.id,
                    "text": s.node.content,
                    "layer": s.node.layer,
                    "score": s.score,
                    "sim": s.parts.get("sim"),
                    "why": s.why(),
                }
                for s in recall.selected
            ],
            affect_before=state.affect_before.pad.model_dump(),
            affect_after=state.affect.pad.model_dump(),
            impacts=[i.model_dump() for i in state.impacts],
            relation={
                "level": state.relation.level,
                "intimacy": state.relation.intimacy,
                "unresolved_conflicts": state.relation.unresolved_conflicts,
                "red_flags": state.red_flags,
                "toxic_signals": state.toxic_signals,
            },
            tokens_by_block=state.prompt.tokens_by_block,
            prompt=state.prompt.system,
        )


class _StateSnapshot:
    """并行任务汇合后的只读快照。并行期间不共享，避免竞争。"""

    def __init__(self, **kw) -> None:
        self.__dict__.update(kw)

    @property
    def hints(self) -> dict:
        return expression_hints(self.affect)

    def llm_ctx(self) -> dict:
        persona: PersonaCard = self.persona
        return {
            "hints": self.hints,
            "tic": persona.voice.verbal_tics[0] if persona.voice.verbal_tics else None,
            "level": self.relation.level,
            "affect": self.affect.pad.model_dump(),
        }
