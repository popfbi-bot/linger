"""Prompt 编译器（docs/ARCHITECTURE.md §3.6）。

它不是字符串拼接，而是带预算和缓存边界的模板渲染器。

两条硬约定：
    1. 拼接顺序必须「稳定 → 动态」。稳定块在前，同一人格的所有用户共享前缀缓存，
       成本可降 50~70%。顺序变更需评审（见 §11 坑 #12）。
    2. 关系等级 0 时禁用一切记忆引用（见 §11 坑 #3 首轮装熟）。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone

from ..affect.engine import expression_hints, hints_to_prompt
from ..models import AffectState, MemoryNode, PersonaCard, RelationState
from ..relation.engine import LEVEL_NAMES, addressing, boundaries
from ..scenario.models import Scenario

# 上下文预算分配（§3.6.2）
BUDGET = {
    "persona": 0.15,
    "constraints": 0.08,
    "relation": 0.08,
    "scenario": 0.08,
    "affect": 0.05,
    "memory": 0.30,
    "history": 0.26,
}

LAYER_LABEL = {2: "L2", 3: "L3", 4: "L4", 5: "L5"}
SOURCE_LABEL = {
    "user_said": "他亲述",
    "ai_inferred": "我的推断",
    "system": "系统记录",
    "user_corrected": "他更正过",
}


def estimate_tokens(text: str) -> int:
    """粗估：CJK 按 1 token/字，其余按 0.35。够用，且不引入 tokenizer 依赖。"""
    cjk = sum(1 for ch in text if "\u4e00" <= ch <= "\u9fff")
    other = len(text) - cjk
    return int(cjk + other * 0.35) + 1


def _ago(created: datetime, now: datetime) -> str:
    if created.tzinfo is None:
        created = created.replace(tzinfo=timezone.utc)
    days = max(0, (now - created).days)
    if days == 0:
        return "今天"
    if days == 1:
        return "昨天"
    if days < 30:
        return f"{days}天前"
    return f"{days // 30}个月前"


@dataclass
class Block:
    name: str
    text: str
    cacheable: bool = False
    cache_break_after: bool = False
    tokens: int = 0

    def __post_init__(self) -> None:
        self.tokens = estimate_tokens(self.text)


@dataclass
class ComposedPrompt:
    system: str
    blocks: list[Block] = field(default_factory=list)
    tokens_by_block: dict[str, int] = field(default_factory=dict)

    @property
    def total_tokens(self) -> int:
        return sum(self.tokens_by_block.values())


def _persona_block(card: PersonaCard) -> str:
    return card.style_anchor()


def _constraints_block(card: PersonaCard) -> str:
    lines = [
        "## 你的行为底线",
        "1. 你是 AI，不是真人。被直接追问身份时要承认，但不要因此改变语气。",
        "2. 不编造没发生过的事。不记得就说不记得，可以说「我不太记得了，你再说一遍？」",
        "3. 不替对方做医疗、法律、投资方面的判断。",
        "4. 不承诺你做不到的事（见面、打电话、替他转告别人）。",
    ]
    if card.values.get("stance_advice"):
        lines.append(f"5. 给建议的原则：{card.values['stance_advice']}")
    if card.voice.forbidden_phrases:
        lines.append("6. 以下说法一次都不要出现：" + "、".join(f"「{p}」" for p in card.voice.forbidden_phrases))
    return "\n".join(lines)


def _ai_boundaries_block(rel: RelationState) -> str:
    """AI 自己的行为底线（人际层）。

    和 _constraints_block 是一对：那一块约束「你是 AI」，
    这一块约束「你是个懂分寸的人」。两者都紧跟 persona 放在前缀缓存区里，
    因为它们不随对话变化。
    """
    from ..social import ai_boundaries, stage_for_level

    try:
        return ai_boundaries(stage_for_level(rel.level))
    except Exception:
        # 知识库损坏不阻断主链路
        return ""


def _relation_block(card: PersonaCard, rel: RelationState) -> str:
    level_name = LEVEL_NAMES[min(rel.level, len(LEVEL_NAMES) - 1)]
    call = addressing(rel.level, card.meta.aliases)
    return "\n".join(
        [
            "## 你们的关系",
            f"等级 {rel.level}（{level_name}），认识 {max(0, (datetime.now(timezone.utc) - rel.first_met_at).days)} 天。",
            f"称呼对方：{call}",
            boundaries(rel.level),
        ]
    )


def _scenario_block(
    scenario: Scenario | str | None,
    include_opening: bool = False,
) -> str:
    """渲染场景状态：设定 + **未完成节拍** + 约束。

    三条要点：
    · 已完成的节拍**不写进来**——否则 AI 会把同一节拍反复推进，
      这是场景运行时最典型的翻车方式。
    · 开场旁白只在第一轮出现。第二轮起它已经是「历史」，再讲一遍就成了复读。
    · 传字符串（M1 老接口）时退化成原样拼接，不炸。
    """
    if not scenario:
        return "## 此刻的场景\n（没有特别设定。就当作普通的日常对话。）"
    if isinstance(scenario, str):
        return "## 此刻的场景\n" + scenario.strip()

    lines = ["## 此刻的场景"]

    where = "；".join(x for x in (scenario.setting, scenario.world) if x)
    lines.append(f"【{scenario.title}】{where}" if where else f"【{scenario.title}】")

    if include_opening and scenario.opening_narration:
        lines.append("")
        lines.append(scenario.opening_narration.strip())

    if scenario.user_role:
        lines.append(f"对方在这个场景里是：{scenario.user_role.strip()}")

    # 节拍状态以模型为准：调用方负责先把持久化进度 restore 进来
    rest = scenario.pending_beats()
    lines.append("")
    if rest:
        lines.append("### 还没发生的事（不要提前推进）")
        for beat in rest:
            lines.append(f"- {beat.goal}")
    else:
        lines.append("### 节拍已走完")
        lines.append("该发生的事都发生过了。可以自然地收束这段场景——别硬拗新的悬念。")

    if scenario.constraints:
        lines.append("")
        lines.append("### 场景约束")
        for c in scenario.constraints:
            lines.append(f"- {c}")

    return "\n".join(lines)


def _affect_block(state: AffectState) -> str:
    hints = expression_hints(state)
    body = hints_to_prompt(hints, state)
    if hints["length_scale"] < 0.9:
        body += "\n回复要比平时短一些。"
    return "## 你此刻的状态\n" + body


def _memory_block(memories: list[MemoryNode], rel: RelationState, now: datetime) -> str:
    if rel.level == 0:
        # 首轮装熟是最典型的沉浸感杀手（坑 #3）
        return "## 你记得的事\n（你们刚认识。不要提任何「以前」的事，不要装作熟稔。）"

    if not memories:
        return "## 你记得的事\n（此刻没有想起什么具体的。不要硬编。）"

    lines = ["## 你记得的事"]
    for m in memories:
        tag = f"[{LAYER_LABEL.get(m.layer, 'L')} · {_ago(m.created_at, now)} · {SOURCE_LABEL.get(m.source_type, '')}]"
        lines.append(f"{tag}\n{m.content}")
        if m.source_type == "ai_inferred":
            lines.append("（这是你猜的，不是他说的。别当成事实讲出来。）")
    return "\n".join(lines)


def _social_block(rel: RelationState, limit: int = 8) -> str:
    """人际行为边界：从社会知识库注入「这个阶段什么该做、什么不该做」。

    关系引擎只有数值（等级/亲密度），但 AI 需要知道——
    「朋友阶段什么样的关心恰到好处、什么样的关心越界」。
    这份知识来自 data/ 下的五份语料，由 social.knowledge 索引。
    """
    from ..social import get_knowledge, stage_for_level

    stage = stage_for_level(rel.level)
    try:
        return get_knowledge().boundaries(stage, limit=limit)
    except Exception:
        # 语料缺失时优雅降级：不影响主链路
        return ""


def _roles_block(roles: list[dict], role_knowledge) -> str:
    """关系角色洞察：对方此刻在关系里是谁，你该补哪个位。

    只识别、不评判：角色是「对方正在经历的关系位置」，不是标签。
    输出互补角色时用「你此刻可以是」而非「你应该」——避免 AI 变成另一个角色扮演机器。
    """
    if not roles:
        return ""
    lines = ["## 关系角色洞察（只用于理解对方，不要说出来）"]
    for r in roles[:2]:
        lines.append(f"- 对方此刻像是「{r['name']}」（{r['dimension']}）：{r['description']}")

    # 互补角色：AI 该补的位
    complements: list[dict[str, str]] = []
    for r in roles:
        complements.extend(role_knowledge.complementary_roles(r["name"], limit=2))
    # 去重
    seen: set[str] = set()
    unique: list[dict[str, str]] = []
    for c in complements:
        if c["name"] in seen:
            continue
        seen.add(c["name"])
        unique.append(c)
        if len(unique) >= 3:
            break

    if unique:
        lines.append("")
        lines.append("你此刻可以扮演：")
        for c in unique:
            lines.append(f"- {c['name']}：{c['description']}")

    # 关系剧本（三角 / 追逃 / 内在小孩…）
    dynamics = role_knowledge.dynamics([r["name"] for r in roles])
    if dynamics:
        lines.append("")
        lines.append("注意：")
        lines.extend(f"- {d}" for d in dynamics)

    return "\n".join(lines)


def compose(
    card: PersonaCard,
    affect: AffectState,
    relation: RelationState,
    memories: list[MemoryNode],
    history: list[dict[str, str]],
    scenario: Scenario | str | None = None,
    budget_tokens: int = 8000,
    now: datetime | None = None,
    opening: bool = False,
    roles: list[dict] | None = None,
    role_knowledge=None,
) -> ComposedPrompt:
    now = now or datetime.now(timezone.utc)

    social_text = _social_block(relation)
    ai_bounds_text = _ai_boundaries_block(relation)
    roles_text = _roles_block(roles or [], role_knowledge) if roles else ""
    blocks = [
        Block("persona", _persona_block(card), cacheable=True),
        Block("constraints", _constraints_block(card), cacheable=True),
        Block("ai_boundaries", ai_bounds_text, cacheable=True, cache_break_after=True)
        if ai_bounds_text
        else None,
        Block("relation", _relation_block(card, relation)),
        Block("social", social_text) if social_text else None,
        Block("scenario", _scenario_block(scenario, opening)),
        Block("affect", _affect_block(affect)),
        Block("roles", roles_text) if roles_text else None,
        Block("memory", _memory_block(memories, relation, now)),
    ]
    blocks = [b for b in blocks if b is not None]
    if not any(b.cache_break_after for b in blocks):
        # 人际底线块缺失时，缓存断点仍要落在第一段稳定前缀之后
        blocks[0].cache_break_after = True

    # 记忆块超预算时，从尾部（分数最低）裁掉
    mem_budget = int(budget_tokens * BUDGET["memory"])
    if blocks[-1].tokens > mem_budget:
        kept: list[MemoryNode] = []
        used = estimate_tokens("## 你记得的事")
        for m in memories:
            t = estimate_tokens(m.content) + 12
            if used + t > mem_budget:
                break
            used += t
            kept.append(m)
        blocks[-1] = Block("memory", _memory_block(kept, relation, now))

    system = ""
    for b in blocks:
        system += b.text.strip() + "\n\n"
        if b.cache_break_after:
            system += "<!-- cache-breakpoint -->\n\n"

    # 历史裁剪：只保留最近若干轮，保证总预算不超
    hist_budget = int(budget_tokens * BUDGET["history"])
    kept_hist: list[dict[str, str]] = []
    used = 0
    for msg in reversed(history):
        t = estimate_tokens(msg.get("content", ""))
        if used + t > hist_budget:
            break
        used += t
        kept_hist.append(msg)
    kept_hist.reverse()

    return ComposedPrompt(
        system=system.strip(),
        blocks=blocks,
        tokens_by_block={b.name: b.tokens for b in blocks} | {"history": used},
    )
