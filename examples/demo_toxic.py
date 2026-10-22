"""演示：陪伴缺失 / 有毒关系信号识别。

这份演示展示了第六份语料（陪伴缺失与有毒关系_负面清单）如何被消费：

    1. 知识库把 180 条负面信号解析成结构化条目（上篇按人生阶段，下篇按有毒关系类型）；
    2. ToxicSignalDetector 从用户倾诉中识别「情感勒索 / 否定打压 / 冷暴力 / 忽视」等信号；
    3. orchestrator 把 toxic_signals 放进 done 事件——只提示，不惩罚，
       绝不会影响 unresolved_conflicts 或关系升级；
    4. 知识库查询接口可直接用于场景运行时（按人生阶段 / 按有毒关系类型）。

免密钥直接跑：

    python examples/demo_toxic.py
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from linger.config import settings  # noqa: E402
from linger.llm import get_provider  # noqa: E402
from linger.memory.embedding import get_embedder  # noqa: E402
from linger.orchestrator import Orchestrator  # noqa: E402
from linger.social import get_knowledge  # noqa: E402
from linger.social.redflag import detect_toxic_signals  # noqa: E402
from linger.social.roles import detect_relation_roles, get_roles  # noqa: E402
from linger.storage.sqlite import SQLiteRepository  # noqa: E402

TOXIC_TURNS = [
    "我妈总说「我都是为了你」，不听她的就是翅膀硬了。",
    "他天天骂我没用，说离开他没人要我，我好自卑。",
    "每次找他他都不回消息，一冷战就消失好几天。",
    "一个人在医院排队，翻遍通讯录没人可以说话。",
]


def main() -> int:
    kb = get_knowledge()
    print("=" * 64)
    print("人际知识库：陪伴缺失与有毒关系（companion_negative 维度）")
    print("=" * 64)
    print(f"总条目：{kb.total}（stage 边界 {sum(1 for i in kb.items if i.dimension=='stage')} + "
          f"陪伴负面 {sum(1 for i in kb.items if i.dimension=='companion_negative')}）")
    print("\n按人生阶段的陪伴缺失条目数：")
    for stage in ("胎儿期", "婴儿期", "幼儿期", "童年期", "青春期", "青年期", "中年期", "老年期"):
        n = len(kb.companion_pitfalls(stage, limit=999))
        print(f"  {stage}：{n} 条")
    print("\n有毒关系类型：")
    for t in kb.toxic_relation_types():
        n = len(kb.toxic_relation_items(t, limit=999))
        print(f"  {t}：{n} 条")
    print(f"  如何走出孤独与有毒关系：{len(kb.companion_recovery())} 条")
    print()

    # ---- 信号识别（纯知识库侧）----
    print("=" * 64)
    print("ToxicSignalDetector：从倾诉中识别信号（只提示，不惩罚）")
    print("=" * 64)
    for text in TOXIC_TURNS:
        hits = detect_toxic_signals(text)
        briefs = "、".join(h.brief for h in hits) or "（无）"
        print(f"\n用户：{text}")
        print(f"识别：{briefs}")
        for h in hits:
            print(f"  知识：{h.text[:50]}…")

    # ---- 角色识别（纯知识库侧）----
    print("\n" + "=" * 64)
    print("RelationRoleKnowledge：用户在关系里是谁（只识别，不惩罚）")
    print("=" * 64)
    role_kb = get_roles()
    print(f"角色位：{role_kb.total} 个，维度：{' / '.join(role_kb.dimensions)}")
    for text in TOXIC_TURNS:
        roles = detect_relation_roles(text)
        if not roles:
            continue
        print(f"\n用户：{text}")
        for r in roles:
            print(f"  角色：〔{r.dimension}〕{r.name} —— {r.description[:40]}…")
        comp = role_kb.complementary_roles(roles[0].name)
        if comp:
            print(f"  AI 可补位：{'、'.join(c['name'] for c in comp)}")

    # ---- orchestrator 集成：toxic_signals 进 done 事件，但关系不惩罚 ----
    print("\n" + "=" * 64)
    print("orchestrator：toxic_signals 进 done 事件，关系不受影响")
    print("=" * 64)
    repo = SQLiteRepository(settings.db_path)
    orch = Orchestrator(repo, get_provider(), get_embedder(settings.embedder, settings.embedding_dim))
    persona = orch.persona_factory.create("linxi", repo)
    session = orch.create_session(persona.id, "u_toxic")

    for i, text in enumerate(TOXIC_TURNS, 1):
        print(f"\n── 第 {i} 轮 ──")
        print(f"用户：{text}")
        toxic_signals = []
        roles = []
        print("林汐：", end="", flush=True)
        for event in orch.send_message(session.id, text):
            if event.type == "delta":
                print(event.data["text"], end="", flush=True)
            elif event.type == "done":
                st = event.data["state"]
                toxic_signals = st.get("toxic_signals", [])
                roles = st.get("roles", [])
                print()
                print(f"  关系：level {st['relation']['level']}  "
                      f"unresolved_conflicts {st['relation']['unresolved_conflicts']}")
                print(f"  toxic_signals：{[s['category'] for s in toxic_signals]}")
                if roles:
                    print(f"  roles：{[r['name'] for r in roles]}")
                print(f"  （有毒信号与角色只提示，unresolved_conflicts 保持 {st['relation']['unresolved_conflicts']}）")

    repo.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())