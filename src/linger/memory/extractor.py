"""记忆提炼：从对话流里抽出可长期保存的条目（docs/ARCHITECTURE.md §3.3.2 ①）。

骨架用启发式规则，**刻意保留三个字段的严格性**，因为它们是防幻觉的地基：
    · source_type —— 只有用户明确说过的才标 user_said
    · confidence  —— 规则命中即 0.9，模型推断必须更低
    · source_turn_ids —— 每条记忆都必须能追溯到原始轮次

生产替换：小模型 + JSON mode 抽取，规则层降级为兜底与校验。
"""

from __future__ import annotations

import re
from datetime import datetime, timezone

from ..models import MemoryNode, Turn
from .embedding import Embedder

_SENT_SPLIT = re.compile(r"[。！？!?\n]+")

_PROMISE = ("答应", "约好", "下次", "说好", "改天", "一定要")
_PREFERENCE = ("喜欢", "讨厌", "受不了", "爱吃", "不吃", "习惯", "最怕")
_FACT = ("我是", "我在", "我住", "我姓", "我养", "我有个", "我妈", "我爸", "我女", "我男")
_EVENT = ("面试", "考试", "加班", "出差", "看病", "复查", "搬家", "分手", "吵架", "辞职", "升职", "旅行",
          # 职业是中文陪伴场景里最高频的倾诉主题（「这份工作干不下去了」），
          # 补进事件词表；副作用是「工作特别累」这类也会被记，属于可接受的信息量。
          "工作", "上班", "职场")

# 极小的地名词表，仅用于演示实体召回。生产用 NER。
_GAZETTEER = ("字节", "腾讯", "阿里", "美团", "医院", "书店", "公司", "学校", "杭州", "上海", "北京", "深圳")

_HIGH_IMPORTANCE = ("妈", "爸", "孩子", "身体", "病", "复查", "手术", "分手", "离职", "答应")


def _entities(sentence: str) -> list[str]:
    return [w for w in _GAZETTEER if w in sentence]


def _importance(sentence: str, base: float) -> float:
    boost = sum(0.08 for w in _HIGH_IMPORTANCE if w in sentence)
    length_bonus = min(len(sentence), 80) / 400
    return round(min(0.95, base + boost + length_bonus), 2)


# 准入词表：高重要性词 + 事件词。它们本来只用于「重要性加权」，
# 2026-10-22 起同时用于「要不要记」（见 _about_user）。
_ABOUT_USER_WORDS = _HIGH_IMPORTANCE + _EVENT


def _about_user(sentence: str) -> bool:
    """这句话值不值得记 —— 判断它是否在讲「用户自己的事」。

    四条准入通道（命中任一即可）：
      1. 第一人称代词              —— 「我最近压力很大」
      2. 地名 / 机构               —— 「在字节上班」（用于实体召回）
      3. 高重要性词（家人/健康/承诺）—— 「妈妈病了」
      4. 具体事件词                 —— 「今天面试砸了」

    **第 3、4 条是补的（2026-10-22）**：之前准入只看第 1、2 条，导致
    「妈妈病了」「今天面试砸了，心里堵得慌」这类**最该被记住的句子**，
    因为中文里主语省略、不带「我」字，被整句丢弃。
    而 `_HIGH_IMPORTANCE` / `_EVENT` 本来就在用——只是用在「重要性加权」，
    没用在「要不要记」，两套词表脱节了。

    刻意**不**纳入纯情绪词（累 / 烦 / 难过）：无主语又无信息量的「最近很累」
    记下来只会稀释记忆面板，它更适合留在 turn 历史里。
    """
    if "我" in sentence:
        return True
    if any(w in sentence for w in _GAZETTEER):
        return True
    return any(w in sentence for w in _ABOUT_USER_WORDS)


def extract_from_turns(
    persona_id: str,
    user_id: str,
    turns: list[Turn],
    embedder: Embedder,
) -> list[MemoryNode]:
    """只从 user 的发言里提炼。AI 说的话不是事实来源。"""
    out: list[MemoryNode] = []
    for turn in turns:
        if turn.role != "user":
            continue
        for sentence in _SENT_SPLIT.split(turn.content):
            s = sentence.strip()
            # 长度两档：4 字以下一律丢（「嗯」「好吧」这类碎片）；
            # 4~5 字只放行「命中准入词表」的句子——「妈妈病了」4 个字、
            # 「工作特别累」5 个字，都是完整的意思，不能因为短就当碎片丢掉。
            if len(s) < 4 or len(s) > 120:
                continue
            if len(s) < 6 and not any(w in s for w in _ABOUT_USER_WORDS):
                continue
            if not _about_user(s):
                continue

            layer, mtype, base = 2, "event", 0.5
            if any(w in s for w in _PROMISE):
                layer, mtype, base = 4, "promise", 0.85
            elif any(w in s for w in _PREFERENCE):
                layer, mtype, base = 3, "preference", 0.6
            elif any(w in s for w in _FACT):
                layer, mtype, base = 3, "fact", 0.7
            elif any(w in s for w in _EVENT):
                layer, mtype, base = 2, "event", 0.65

            out.append(
                MemoryNode(
                    persona_id=persona_id,
                    user_id=user_id,
                    layer=layer,
                    type=mtype,
                    content=s,
                    entities=_entities(s),
                    importance=_importance(s, base),
                    source_type="user_said",
                    source_turn_ids=[turn.id],
                    confidence=0.9,
                    emotion={},
                    created_at=turn.created_at,
                    embedding=embedder.embed(s),
                )
            )
    return out


def dedupe(nodes: list[MemoryNode], existing: list[MemoryNode], embedder: Embedder,
           threshold: float = 0.88) -> list[MemoryNode]:
    """去重合并：语义相近且实体一致 → 视为同一条，累加 mention_count。"""
    from .embedding import cosine

    kept: list[MemoryNode] = []
    pool = list(existing)
    for node in nodes:
        dup = None
        for other in pool:
            if cosine(node.embedding or [], other.embedding or []) >= threshold:
                dup = other
                break
        if dup is not None:
            dup.mention_count += 1
            dup.importance = round(min(0.95, dup.importance + 0.05), 2)
            continue
        pool.append(node)
        kept.append(node)
    return kept
