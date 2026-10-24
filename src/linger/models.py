"""领域模型 —— 四态引擎的唯一真相源。

设计约束（docs/ARCHITECTURE.md §3.1.1）：
    这里的每个字段都必须能被某个引擎读取，或者能被编译进 prompt。
    不能被执行引擎消费的字段，不该存在于这里。
"""

from __future__ import annotations

import uuid
from datetime import date as date_cls
from datetime import datetime, timezone
from typing import Any, Literal

from pydantic import BaseModel, Field


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:10]}"


def clamp(v: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, v))


# --------------------------------------------------------------------------- #
# 情绪（PAD 三维）
# --------------------------------------------------------------------------- #


class PAD(BaseModel):
    """Pleasure / Arousal / Dominance。

    P ∈ [-1, 1]，A ∈ [0, 1]，D ∈ [0, 1]。
    用连续三维而不是离散标签，因为它需要能做数学运算（衰减、叠加、插值）。
    """

    pleasure: float = 0.25
    arousal: float = 0.35
    dominance: float = 0.40

    def clamped(self) -> PAD:
        return PAD(
            pleasure=clamp(self.pleasure, -1.0, 1.0),
            arousal=clamp(self.arousal, 0.0, 1.0),
            dominance=clamp(self.dominance, 0.0, 1.0),
        )

    def distance(self, other: PAD) -> float:
        return (
            abs(self.pleasure - other.pleasure)
            + abs(self.arousal - other.arousal)
            + abs(self.dominance - other.dominance)
        )


class AffectState(BaseModel):
    pad: PAD = Field(default_factory=PAD)
    label: str = "中性"
    updated_at: datetime = Field(default_factory=now_utc)


class Impact(BaseModel):
    """一次事件对情绪的冲击。salience = 这件事对"她"有多重要（0~1）。"""

    kind: Literal["praise", "criticism", "neglect", "intimacy", "event"] = "event"
    dp: float = 0.0
    da: float = 0.0
    dd: float = 0.0
    salience: float = 0.5


# --------------------------------------------------------------------------- #
# 人格
# --------------------------------------------------------------------------- #


class BigFive(BaseModel):
    openness: float = 0.5
    conscientiousness: float = 0.5
    extraversion: float = 0.5
    agreeableness: float = 0.5
    neuroticism: float = 0.5


class Voice(BaseModel):
    register: str = "口语、短句"
    avg_sentence_len: list[int] = Field(default_factory=lambda: [8, 20])
    emoji_rate: float = 0.2
    emoji_set: list[str] = Field(default_factory=list)
    verbal_tics: list[str] = Field(default_factory=list)
    forbidden_phrases: list[str] = Field(default_factory=list)
    punctuation_policy: str = ""
    addressing_rules: dict[str, str] = Field(default_factory=dict)


class Knowledge(BaseModel):
    knows: list[str] = Field(default_factory=list)
    knows_not: list[str] = Field(default_factory=list)
    leak_policy: str = "被直接追问身份时承认，但不跳出人设情绪"


class Sensitivity(BaseModel):
    praise: float = 1.0
    criticism: float = 1.0
    neglect: float = 1.0
    intimacy: float = 1.0


class AffectConfig(BaseModel):
    baseline: PAD = Field(default_factory=PAD)
    sensitivity: Sensitivity = Field(default_factory=Sensitivity)


class Initiative(BaseModel):
    silence_tolerance_turns: int = 2
    max_proactive_per_day: int = 2
    proactive_topics: list[str] = Field(default_factory=list)
    never_proactive_when: list[str] = Field(default_factory=list)


class MemoryPolicy(BaseModel):
    remember: list[str] = Field(default_factory=list)
    forget: list[str] = Field(default_factory=list)
    sensitivity: str = "normal"


class PersonaMeta(BaseModel):
    name: str
    aliases: list[str] = Field(default_factory=list)
    age: int | None = None
    identity: str = ""
    archetype: str = ""


class PersonaCard(BaseModel):
    id: str = Field(default_factory=lambda: new_id("p"))
    version: int = 1
    meta: PersonaMeta
    traits: BigFive = Field(default_factory=BigFive)
    custom_traits: dict[str, float] = Field(default_factory=dict)
    voice: Voice = Field(default_factory=Voice)
    values: dict[str, Any] = Field(default_factory=dict)
    knowledge: Knowledge = Field(default_factory=Knowledge)
    affect: AffectConfig = Field(default_factory=AffectConfig)
    initiative: Initiative = Field(default_factory=Initiative)
    memory_policy: MemoryPolicy = Field(default_factory=MemoryPolicy)
    provenance: dict[str, Any] = Field(default_factory=dict)

    @property
    def name(self) -> str:
        return self.meta.name

    def style_anchor(self) -> str:
        """语体锚点：编译器把它放在 prompt 最前，作为可缓存前缀。"""
        v = self.voice
        lo, hi = v.avg_sentence_len
        parts = [
            f"你是{self.meta.name}"
            + (f"，{self.meta.age} 岁" if self.meta.age else "")
            + (f"，{self.meta.identity}" if self.meta.identity else "")
            + "。",
            f"你说话{v.register}，句子大约 {lo}~{hi} 个字。",
        ]
        if v.verbal_tics:
            parts.append("你偶尔会说：" + "、".join(f"「{t}」" for t in v.verbal_tics) + "。")
        if v.punctuation_policy:
            parts.append(f"标点习惯：{v.punctuation_policy}。")
        if v.forbidden_phrases:
            parts.append("这些说法你不会用：" + "、".join(f"「{p}」" for p in v.forbidden_phrases) + "。")
        if self.knowledge.knows_not:
            parts.append(
                "你不懂的领域：" + "、".join(self.knowledge.knows_not) + "。遇到就直接说不懂，别硬撑。"
            )
        if self.knowledge.leak_policy:
            parts.append(self.knowledge.leak_policy + "。")
        return "\n".join(parts)


# --------------------------------------------------------------------------- #
# 记忆
# --------------------------------------------------------------------------- #

SourceType = Literal["user_said", "ai_inferred", "system", "user_corrected"]


class MemoryNode(BaseModel):
    id: str = Field(default_factory=lambda: new_id("m"))
    persona_id: str
    user_id: str
    layer: int = 2  # 2 情景 / 3 语义 / 4 关系 / 5 程序
    type: str = "event"
    content: str
    entities: list[str] = Field(default_factory=list)
    importance: float = 0.5
    strength: float = 1.0
    recall_count: int = 0
    mention_count: int = 1
    emotion: dict[str, float] = Field(default_factory=dict)
    source_type: SourceType = "user_said"
    source_turn_ids: list[int] = Field(default_factory=list)
    confidence: float = 0.9
    status: str = "active"  # active | superseded | archived
    pinned: bool = False
    created_at: datetime = Field(default_factory=now_utc)
    # 最近一次「被改写」的时间。只由用户编辑（PATCH /v1/memories/{id}）推进，
    # 召回回写不动它——召回是「用」，编辑是「改」，两件事不能共用一个时间戳。
    # None = 从未编辑过（created_at 即真相）。
    updated_at: datetime | None = None
    last_recalled_at: datetime | None = None
    embedding: list[float] | None = None

    @property
    def is_inferred(self) -> bool:
        return self.source_type == "ai_inferred"


# --------------------------------------------------------------------------- #
# 关系
# --------------------------------------------------------------------------- #


class RelationState(BaseModel):
    persona_id: str
    user_id: str
    level: int = 0
    intimacy: float = 0.0
    active_days: int = 0
    turns: int = 0
    deep_turns: int = 0
    resonance_events: int = 0
    self_disclosure: int = 0
    unresolved_conflicts: int = 0
    first_met_at: datetime = Field(default_factory=now_utc)
    level_up_at: datetime | None = None


# --------------------------------------------------------------------------- #
# 会话
# --------------------------------------------------------------------------- #


class Turn(BaseModel):
    id: int = 0
    session_id: str
    role: Literal["user", "assistant", "system"]
    content: str
    affect_snapshot: dict[str, float] | None = None
    created_at: datetime = Field(default_factory=now_utc)


class Session(BaseModel):
    id: str = Field(default_factory=lambda: new_id("s"))
    persona_id: str
    user_id: str
    scenario_id: str | None = None
    started_at: datetime = Field(default_factory=now_utc)
    last_active_at: datetime = Field(default_factory=now_utc)


class TraceRecord(BaseModel):
    """单轮回溯。没有它，记忆系统就是黑箱（见 §5.4）。"""

    turn_id: int
    persona_id: str
    user_id: str
    query: str
    candidates: int = 0
    passed_gate: int = 0
    selected: list[dict[str, Any]] = Field(default_factory=list)
    affect_before: dict[str, float] = Field(default_factory=dict)
    affect_after: dict[str, float] = Field(default_factory=dict)
    impacts: list[dict[str, Any]] = Field(default_factory=list)
    # 这一轮走的是模型还是规则、以及模型给的理由（#38 判据 4/5）。
    # 没有它，「模型在工作」与「一直静默走兜底」在事后无法区分。
    eval: dict[str, Any] = Field(default_factory=dict)
    relation: dict[str, Any] = Field(default_factory=dict)
    tokens_by_block: dict[str, int] = Field(default_factory=dict)
    prompt: str = ""
    created_at: datetime = Field(default_factory=now_utc)


# --------------------------------------------------------------------------- #
# 陪伴档案（M2-A）
# --------------------------------------------------------------------------- #


class CompanionProfile(BaseModel):
    """「用户是谁」的持久化投影。

    每轮的 `roles` / `toxic_signals` 只是当轮的临时推断，换个会话就没了。
    陪伴档案把它们（以及人生阶段）落成状态：可存储、可查看、可由用户校正。

    铁律：档案只用于**理解**对方，不用于评价对方——
    它的任何字段都不允许进入 `RelationState.unresolved_conflicts`。
    """

    persona_id: str
    user_id: str
    # 胎儿期/婴儿期/幼儿期/童年期/青春期/青年期/中年期/老年期；"" = 未确定
    life_stage: str = ""
    roles: list[str] = Field(default_factory=list)
    needs: list[str] = Field(default_factory=list)
    risks: list[str] = Field(default_factory=list)
    updated_at: datetime = Field(default_factory=now_utc)

    @property
    def is_empty(self) -> bool:
        """空档案：什么都没识别出来。

        编译器据此跳过 `companion` 块，保持 prompt 前缀稳定（§3.6 缓存纪律）。
        """
        return not (self.life_stage or self.roles or self.needs or self.risks)


# --------------------------------------------------------------------------- #
# 日记（M3-D，架构 §3.3.2 巩固管线第 6 步）
# --------------------------------------------------------------------------- #


class DiaryEntry(BaseModel):
    """她写下的「今天」。

    架构把日记定为产品级巧思：它同时是上下文压缩、可解释性与情感价值。
    所以这里存的不是「对话摘要」，而是**她的第一人称感受**——
    `content` 写的是情绪，不是流水账（判据由 `diary.flow._flow_check` 兜底）。

    主键语义是 `(persona_id, user_id, date)`：一天只有一篇。
    重复生成是覆盖，不是新增——否则「每天一篇」会退化成「每点一次一篇」。
    """

    id: str = Field(default_factory=lambda: new_id("d"))
    persona_id: str
    user_id: str
    date: date_cls  # 只到天。时分秒属于 turn，不属于日记。
    content: str = ""
    mood_label: str = ""
    highlight_memories: list[str] = Field(default_factory=list)
    created_at: datetime = Field(default_factory=now_utc)


# --------------------------------------------------------------------------- #
# 记忆编辑审计（M2-C）
# --------------------------------------------------------------------------- #


class MemoryEditRecord(BaseModel):
    """一次记忆编辑的审计留痕。

    用户改记忆是**纠正口径**，不是覆盖历史。这条记录保存修改前后的内容，
    让「她之前记的是什么、你改成了什么」永远可回溯——这是陪伴信任的底线：
    AI 猜错可以，猜错后偷偷改掉当没发生，不行。
    """

    memory_id: str
    old_content: str
    new_content: str
    edited_at: datetime = Field(default_factory=now_utc)

# --------------------------------------------------------------------------- #
# LLM 用量记录（M4-D 成本看板）
# --------------------------------------------------------------------------- #


class UsageRecord(BaseModel):
    """一次 LLM 调用的一行账。

    这是**流水**，不是聚合：一条 INSERT 一行，不做任何统计（判据 5）。
    聚合只在 `/v1/costs` 查的时候做，否则每轮对话都要跑一次全表 SUM，
    延迟会随历史记录线性增长。

    两件事在这里被刻意做成「必须显式表达」：

    1. `ok=False` 的记录也进这张表。只记成功调用，重试风暴就隐形了——
       而重试风暴正是烧钱的主因。失败率 = 1 - sum(ok) / count(*)。
    2. `tokens_in` / `tokens_out` 为 `None` 表示**不知道**，不是 0。
       provider 没报 usage 又估不出来时，宁可留空，也不写一个会被读成
       「这次免费」的 0。
    """

    id: int = 0
    created_at: datetime = Field(default_factory=now_utc)
    provider: str = "unknown"
    model: str = "unknown"
    purpose: str = "chat"  # chat / generate / distill / diary
    ok: bool = True
    degraded: bool = False
    error: str | None = None
    elapsed_ms: int = 0
    chunks: int = 0
    attempts: int = 0
    skipped: list[str] = Field(default_factory=list)
    tokens_in: int | None = None
    tokens_out: int | None = None
    tokens_cached: int = 0
    estimated: bool = True  # True = 数字是估的，不是 provider 报的

