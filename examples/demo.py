"""最小可运行演示：模板人格 → 会话 → 流式回复 → trace 回溯。

免密钥（LINGER_LLM 默认 mock），直接跑：

    python examples/demo.py

它会做四件事：
    1. 用 HashEmbedder + SQLite + MockProvider 把链路搭起来（零外部依赖）；
    2. 从模板 linxi 实例化人格，开一个会话，聊三轮（低落 / 高兴 / 承压）；
    3. 每轮打印流式文本、情绪标签、关系等级、用到的记忆条数；
    4. 最后打印该会话的 trace：候选 / 过闸门 / 注入条数。

两点刻意的安排：

    · 先用同一个 user 聊两轮过去的事。关系等级 0 时编译器禁用一切记忆引用
      （坑 #3 首轮装熟），不预热就只能看到闸门在拒绝。
    · 第 3 轮再提一次「复查」：用户的多轮话题往往是「顺着上一句往下说」，
      而不是复述关键词。闸门 0.35 对这种延续句是偏紧的——这是可调参数
      （settings.recall_gate），不是链路问题。
    · 这台机器上 MockProvider 只读 prompt、不看 system，且 MockProvider 与
      AffectState 默认都叫 "mock"，所以模型回复里能看出「状态在变」，看不出
      「记忆在起作用」。按文档，人格活在 system prompt 里，所以演示会直出
      本轮实际注入的记忆块。
"""

from __future__ import annotations

import sys
from pathlib import Path

# 还没装包，直接把 src 挂上来
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from linger.config import settings  # noqa: E402
from linger.llm import get_provider  # noqa: E402
from linger.memory.embedding import get_embedder  # noqa: E402
from linger.orchestrator import Orchestrator  # noqa: E402
from linger.storage.sqlite import SQLiteRepository  # noqa: E402

TEMPLATE = "linxi"
USER_ID = "u_demo"
PAST_TURNS = [
    "其实我住在这个城市三年了。",
    "我妈妈下周要去医院复查。",
]
USER_TURNS = [
    "今天面试砸了，答得一团糟，估计是没戏了。",
    "我妈妈复查结果出来了，是好消息。",
    "我妈妈复查的医院那边，我这阵子压力也上来了。",
]


def _memory_block(system: str) -> str:
    for block in system.split("## ")[1:]:
        if block.startswith("你记得的事"):
            return "## " + block.strip()
    return ""


def main() -> int:
    repo = SQLiteRepository(settings.db_path)
    orch = Orchestrator(repo, get_provider(), get_embedder(settings.embedder, settings.embedding_dim))

    persona = orch.persona_factory.create(TEMPLATE, repo)
    session = orch.create_session(persona.id, USER_ID)

    print(f"人格：{persona.name}（{persona.meta.identity}）  模型：{orch.llm.name}")
    print(f"存储：{settings.db_path}  会话：{session.id}\n")

    for text in PAST_TURNS:
        list(orch.send_message(session.id, text))
    relation = repo.get_relation(persona.id, USER_ID)
    print(f"（先聊了 {len(PAST_TURNS)} 轮过去的事，关系 level {relation.level}、"
          f"记忆 {len(repo.list_memories(persona.id, USER_ID))} 条）\n")

    last_turn_id = 0
    for i, text in enumerate(USER_TURNS, 1):
        print(f"── 第 {i} 轮 ──")
        print(f"用户：{text}")

        affect, level, used = "", 0, 0
        print("林汐：", end="", flush=True)
        for event in orch.send_message(session.id, text):
            if event.type == "delta":
                print(event.data["text"], end="", flush=True)
            elif event.type == "affect":
                affect = event.data["label"]
                level = event.data["relation_level"]
            elif event.type == "done":
                used = len(event.data["used_memories"])
                last_turn_id = event.data["turn_id"]

        print(f"\n情绪：{affect}   关系等级：{level}   用到记忆：{used} 条")
        print("本轮注入的记忆块：")
        print(_memory_block(repo.get_trace(last_turn_id)["prompt"]))
        print()

    # ---- trace 回溯 ----
    trace = repo.get_trace(last_turn_id)
    print("── trace（最后一轮）──")
    if not trace:
        print("没有 trace，链路没跑通。")
        return 1
    print(f"query：{trace['query']}")
    print(f"候选 {trace['candidates']} 条 → 过闸门 {trace['passed_gate']} 条 → 注入 {len(trace['selected'])} 条")
    for m in trace["selected"]:
        print(f"  · [L{m['layer']}] {m['score']:.3f} {m['why']}  {m['text'][:24]}")
    print(f"情绪：{trace['affect_before']}")
    print(f"      → {trace['affect_after']}")
    print(f"关系：level {trace['relation']['level']}  intimacy {trace['relation']['intimacy']}")
    print(f"上下文 tokens：{trace['tokens_by_block']}")
    print(f"\n累计记忆 {len(repo.list_memories(persona.id, USER_ID))} 条"
          "（写入都发生在每轮回复落库之后）")
    repo.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
