"""SQLite 实现 —— dev 与单机部署用。

字段与 docs/ARCHITECTURE.md §4.2 的 PostgreSQL schema 一一对应，
差别只在：向量用 JSON 文本存（256 维暴力计算），时间用 ISO 字符串。
迁到 PG 时数据可直接搬过去。
"""

from __future__ import annotations

import json
import sqlite3
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

from ..models import (
    PAD,
    AffectState,
    CompanionProfile,
    DiaryEntry,
    MemoryEditRecord,
    MemoryNode,
    PersonaCard,
    RelationState,
    Session,
    TraceRecord,
    Turn,
)
from .base import Repository

_SCHEMA = """
CREATE TABLE IF NOT EXISTS persona (
  id TEXT PRIMARY KEY,
  version INTEGER NOT NULL DEFAULT 1,
  name TEXT NOT NULL,
  card TEXT NOT NULL,
  status TEXT DEFAULT 'active',
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS session (
  id TEXT PRIMARY KEY,
  persona_id TEXT NOT NULL,
  user_id TEXT NOT NULL,
  scenario_id TEXT,
  started_at TEXT NOT NULL,
  last_active_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS turn (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  session_id TEXT NOT NULL,
  role TEXT NOT NULL,
  content TEXT NOT NULL,
  affect_snapshot TEXT,
  created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_turn_session ON turn (session_id, id);
CREATE TABLE IF NOT EXISTS affect_state (
  persona_id TEXT NOT NULL,
  user_id TEXT NOT NULL,
  pleasure REAL, arousal REAL, dominance REAL,
  label TEXT, updated_at TEXT,
  PRIMARY KEY (persona_id, user_id)
);
CREATE TABLE IF NOT EXISTS relation_state (
  persona_id TEXT NOT NULL,
  user_id TEXT NOT NULL,
  level INTEGER, intimacy REAL, active_days INTEGER, turns INTEGER,
  deep_turns INTEGER, resonance_events INTEGER, self_disclosure INTEGER,
  unresolved_conflicts INTEGER, first_met_at TEXT, level_up_at TEXT,
  PRIMARY KEY (persona_id, user_id)
);
CREATE TABLE IF NOT EXISTS memory_node (
  id TEXT PRIMARY KEY,
  persona_id TEXT NOT NULL,
  user_id TEXT NOT NULL,
  layer INTEGER NOT NULL,
  type TEXT NOT NULL,
  content TEXT NOT NULL,
  entities TEXT, embedding TEXT,
  importance REAL, strength REAL, recall_count INTEGER, mention_count INTEGER,
  emotion TEXT, source_type TEXT, source_turn_ids TEXT, confidence REAL,
  status TEXT, pinned INTEGER, created_at TEXT, last_recalled_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_mem_owner ON memory_node (persona_id, user_id, status);
CREATE TABLE IF NOT EXISTS scenario_progress (
  session_id TEXT PRIMARY KEY,
  payload TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS companion_profile (
  persona_id TEXT NOT NULL,
  user_id TEXT NOT NULL,
  life_stage TEXT NOT NULL DEFAULT '',
  roles TEXT, needs TEXT, risks TEXT,
  updated_at TEXT NOT NULL,
  PRIMARY KEY (persona_id, user_id)
);
CREATE TABLE IF NOT EXISTS trace (
  turn_id INTEGER PRIMARY KEY,
  payload TEXT NOT NULL,
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS memory_edit_log (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  memory_id TEXT NOT NULL,
  old_content TEXT NOT NULL,
  new_content TEXT NOT NULL,
  edited_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_mem_edit_memory ON memory_edit_log (memory_id, id);
-- 日记：一天一篇。唯一键是三元组，不是 id —— 重复生成必须命中冲突去覆盖，
-- 否则「写下今天」会变成「每次点都多一篇」。
CREATE TABLE IF NOT EXISTS diary_entry (
  id TEXT PRIMARY KEY,
  persona_id TEXT NOT NULL,
  user_id TEXT NOT NULL,
  date TEXT NOT NULL,
  content TEXT NOT NULL,
  mood_label TEXT,
  highlight_memories TEXT,
  created_at TEXT NOT NULL,
  UNIQUE (persona_id, user_id, date)
);
CREATE INDEX IF NOT EXISTS idx_diary_owner ON diary_entry (persona_id, user_id, date);
"""


def _dt(v: datetime | None) -> str | None:
    return None if v is None else v.astimezone(timezone.utc).isoformat()


def _pd(s: str | None) -> datetime | None:
    return None if not s else datetime.fromisoformat(s)


def _j(v: Any) -> str:
    return json.dumps(v, ensure_ascii=False)


def _pj(s: str | None, default: Any) -> Any:
    return default if not s else json.loads(s)


class SQLiteRepository(Repository):
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(self.path), check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(_SCHEMA)
        self._migrate()
        self.conn.commit()

    def _migrate(self) -> None:
        """给老库补列。

        `CREATE TABLE IF NOT EXISTS` 对已存在的表一个字都不改，
        所以新增列必须显式 ALTER——否则升级后老库一读就炸。
        """
        cols = {r["name"] for r in self.conn.execute("PRAGMA table_info(memory_node)")}
        if "updated_at" not in cols:
            self.conn.execute("ALTER TABLE memory_node ADD COLUMN updated_at TEXT")

    # ---- 人格 ---------------------------------------------------------- #
    def save_persona(self, card: PersonaCard) -> None:
        self.conn.execute(
            "INSERT INTO persona (id, version, name, card, created_at) VALUES (?,?,?,?,?) "
            "ON CONFLICT(id) DO UPDATE SET version=excluded.version, name=excluded.name, card=excluded.card",
            (card.id, card.version, card.name, _j(card.model_dump(mode="json")), _dt(datetime.now(timezone.utc))),
        )
        self.conn.commit()

    def get_persona(self, persona_id: str) -> PersonaCard | None:
        """只认 active。被软删的人格在这里返回 None——
        于是「用已删人格开会话」自然 404，不需要在 HTTP 层再补一道判断。
        """
        row = self.conn.execute(
            "SELECT card FROM persona WHERE id=? AND status='active'", (persona_id,)
        ).fetchone()
        return None if row is None else PersonaCard.model_validate(json.loads(row["card"]))

    def list_personas(self, limit: int = 50) -> list[PersonaCard]:
        """只列 active。删除后的人格不该再出现在「我的人格」里。"""
        rows = self.conn.execute(
            "SELECT card FROM persona WHERE status='active' ORDER BY created_at DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return [PersonaCard.model_validate(json.loads(r["card"])) for r in rows]

    def find_persona_by_template(self, template_id: str) -> PersonaCard | None:
        """按 provenance.template 找**仍然 active** 的已实例化模板人格。

        扫表而不是加列：人格数量是「人」的量级（几十到几百），
        一次全表解析 JSON 的开销远小于为它做一次 schema 迁移。
        真到需要索引时再补 `template_id` 列与迁移脚本。

        **必须过滤 status，这是刻意设计而非疏漏**：删除模板人格 = 重置这段关系。
        用户删掉林汐再点林汐，得到的是一个全新人格（新 id、记忆从零），
        而不是把旧记忆接回来。所以这里查不到已删的那一份，实例化会重新建一个。
        """
        rows = self.conn.execute(
            "SELECT card FROM persona WHERE status='active' ORDER BY created_at ASC"
        ).fetchall()
        for r in rows:
            card = PersonaCard.model_validate(json.loads(r["card"]))
            if (card.provenance or {}).get("template") == template_id:
                return card
        return None

    def delete_persona(self, persona_id: str) -> PersonaCard | None:
        """软删除：status='deleted'，返回被删的人格卡；不存在或已删则 None。

        只动 persona.status 一行，**不级联**删 memory_node / turn / session。
        删除人格 ≠ 删除历史：会话与记忆是另一个维度的数据，
        用户说的只是「这个人格我不要了」，不是「把那段时间抹掉」。
        （也留下误删后翻库恢复的余地。）

        已删的再删返回 None 而不是幂等成功：用户看不出自己删了两遍，
        而前端拿 404 才能把列表刷成真实状态。
        """
        row = self.conn.execute(
            "SELECT card, status FROM persona WHERE id=?", (persona_id,)
        ).fetchone()
        if row is None or row["status"] != "active":
            return None
        self.conn.execute("UPDATE persona SET status='deleted' WHERE id=?", (persona_id,))
        self.conn.commit()
        return PersonaCard.model_validate(json.loads(row["card"]))

    # ---- 会话 ---------------------------------------------------------- #
    def save_session(self, s: Session) -> None:
        self.conn.execute(
            "INSERT INTO session (id, persona_id, user_id, scenario_id, started_at, last_active_at) "
            "VALUES (?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET last_active_at=excluded.last_active_at",
            (s.id, s.persona_id, s.user_id, s.scenario_id, _dt(s.started_at), _dt(s.last_active_at)),
        )
        self.conn.commit()

    def get_session(self, session_id: str) -> Session | None:
        row = self.conn.execute("SELECT * FROM session WHERE id=?", (session_id,)).fetchone()
        if row is None:
            return None
        return Session(
            id=row["id"],
            persona_id=row["persona_id"],
            user_id=row["user_id"],
            scenario_id=row["scenario_id"],
            started_at=_pd(row["started_at"]) or datetime.now(timezone.utc),
            last_active_at=_pd(row["last_active_at"]) or datetime.now(timezone.utc),
        )

    def list_sessions(self, persona_id: str, user_id: str, limit: int = 100) -> list[Session]:
        rows = self.conn.execute(
            "SELECT * FROM session WHERE persona_id=? AND user_id=? "
            "ORDER BY last_active_at DESC LIMIT ?",
            (persona_id, user_id, limit),
        ).fetchall()
        return [
            Session(
                id=r["id"],
                persona_id=r["persona_id"],
                user_id=r["user_id"],
                scenario_id=r["scenario_id"],
                started_at=_pd(r["started_at"]) or datetime.now(timezone.utc),
                last_active_at=_pd(r["last_active_at"]) or datetime.now(timezone.utc),
            )
            for r in rows
        ]

    # ---- 对话轮 -------------------------------------------------------- #
    def append_turn(
        self, session_id: str, role: str, content: str, affect: dict[str, float] | None = None
    ) -> int:
        cur = self.conn.execute(
            "INSERT INTO turn (session_id, role, content, affect_snapshot, created_at) VALUES (?,?,?,?,?)",
            (session_id, role, content, _j(affect) if affect else None, _dt(datetime.now(timezone.utc))),
        )
        self.conn.commit()
        return int(cur.lastrowid or 0)

    def list_turns(self, session_id: str, limit: int = 40) -> list[Turn]:
        rows = self.conn.execute(
            "SELECT * FROM turn WHERE session_id=? ORDER BY id DESC LIMIT ?", (session_id, limit)
        ).fetchall()
        out = [
            Turn(
                id=r["id"],
                session_id=r["session_id"],
                role=r["role"],
                content=r["content"],
                affect_snapshot=_pj(r["affect_snapshot"], None),
                created_at=_pd(r["created_at"]) or datetime.now(timezone.utc),
            )
            for r in rows
        ]
        out.reverse()
        return out

    # ---- 情绪 ---------------------------------------------------------- #
    def get_affect(self, persona_id: str, user_id: str) -> AffectState | None:
        row = self.conn.execute(
            "SELECT * FROM affect_state WHERE persona_id=? AND user_id=?", (persona_id, user_id)
        ).fetchone()
        if row is None:
            return None
        return AffectState(
            pad=PAD(pleasure=row["pleasure"], arousal=row["arousal"], dominance=row["dominance"]),
            label=row["label"] or "中性",
            updated_at=_pd(row["updated_at"]) or datetime.now(timezone.utc),
        )

    def save_affect(self, persona_id: str, user_id: str, state: AffectState) -> None:
        self.conn.execute(
            "INSERT INTO affect_state (persona_id,user_id,pleasure,arousal,dominance,label,updated_at) "
            "VALUES (?,?,?,?,?,?,?) ON CONFLICT(persona_id,user_id) DO UPDATE SET "
            "pleasure=excluded.pleasure, arousal=excluded.arousal, dominance=excluded.dominance, "
            "label=excluded.label, updated_at=excluded.updated_at",
            (persona_id, user_id, state.pad.pleasure, state.pad.arousal, state.pad.dominance,
             state.label, _dt(state.updated_at)),
        )
        self.conn.commit()

    # ---- 关系 ---------------------------------------------------------- #
    def get_relation(self, persona_id: str, user_id: str) -> RelationState | None:
        row = self.conn.execute(
            "SELECT * FROM relation_state WHERE persona_id=? AND user_id=?", (persona_id, user_id)
        ).fetchone()
        if row is None:
            return None
        return RelationState(
            persona_id=row["persona_id"],
            user_id=row["user_id"],
            level=row["level"],
            intimacy=row["intimacy"],
            active_days=row["active_days"],
            turns=row["turns"],
            deep_turns=row["deep_turns"],
            resonance_events=row["resonance_events"],
            self_disclosure=row["self_disclosure"],
            unresolved_conflicts=row["unresolved_conflicts"],
            first_met_at=_pd(row["first_met_at"]) or datetime.now(timezone.utc),
            level_up_at=_pd(row["level_up_at"]),
        )

    def save_relation(self, s: RelationState) -> None:
        self.conn.execute(
            "INSERT INTO relation_state (persona_id,user_id,level,intimacy,active_days,turns,deep_turns,"
            "resonance_events,self_disclosure,unresolved_conflicts,first_met_at,level_up_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(persona_id,user_id) DO UPDATE SET "
            "level=excluded.level, intimacy=excluded.intimacy, active_days=excluded.active_days, "
            "turns=excluded.turns, deep_turns=excluded.deep_turns, resonance_events=excluded.resonance_events, "
            "self_disclosure=excluded.self_disclosure, unresolved_conflicts=excluded.unresolved_conflicts, "
            "level_up_at=excluded.level_up_at",
            (s.persona_id, s.user_id, s.level, s.intimacy, s.active_days, s.turns, s.deep_turns,
             s.resonance_events, s.self_disclosure, s.unresolved_conflicts, _dt(s.first_met_at),
             _dt(s.level_up_at)),
        )
        self.conn.commit()

    # ---- 记忆 ---------------------------------------------------------- #
    def add_memories(self, nodes: list[MemoryNode]) -> None:
        for n in nodes:
            self.conn.execute(
                "INSERT OR REPLACE INTO memory_node (id,persona_id,user_id,layer,type,content,entities,"
                "embedding,importance,strength,recall_count,mention_count,emotion,source_type,"
                "source_turn_ids,confidence,status,pinned,created_at,last_recalled_at,updated_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (n.id, n.persona_id, n.user_id, n.layer, n.type, n.content, _j(n.entities),
                 _j(n.embedding or []), n.importance, n.strength, n.recall_count, n.mention_count,
                 _j(n.emotion), n.source_type, _j(n.source_turn_ids), n.confidence, n.status,
                 int(n.pinned), _dt(n.created_at), _dt(n.last_recalled_at), _dt(n.updated_at)),
            )
        self.conn.commit()

    def _row_to_memory(self, r: sqlite3.Row) -> MemoryNode:
        return MemoryNode(
            id=r["id"], persona_id=r["persona_id"], user_id=r["user_id"], layer=r["layer"],
            type=r["type"], content=r["content"], entities=_pj(r["entities"], []),
            embedding=_pj(r["embedding"], []), importance=r["importance"], strength=r["strength"],
            recall_count=r["recall_count"], mention_count=r["mention_count"],
            emotion=_pj(r["emotion"], {}), source_type=r["source_type"],
            source_turn_ids=_pj(r["source_turn_ids"], []), confidence=r["confidence"],
            status=r["status"], pinned=bool(r["pinned"]),
            created_at=_pd(r["created_at"]) or datetime.now(timezone.utc),
            updated_at=_pd(r["updated_at"]),
            last_recalled_at=_pd(r["last_recalled_at"]),
        )

    def list_memories(
        self, persona_id: str, user_id: str, layer: int | None = None, limit: int = 500
    ) -> list[MemoryNode]:
        sql = "SELECT * FROM memory_node WHERE persona_id=? AND user_id=? AND status!='deleted'"
        args: list[Any] = [persona_id, user_id]
        if layer is not None:
            sql += " AND layer=?"
            args.append(layer)
        sql += " ORDER BY created_at DESC LIMIT ?"
        args.append(limit)
        return [self._row_to_memory(r) for r in self.conn.execute(sql, args).fetchall()]

    def get_memory(self, memory_id: str) -> MemoryNode | None:
        row = self.conn.execute(
            "SELECT * FROM memory_node WHERE id=? AND status!='deleted'", (memory_id,)
        ).fetchone()
        return None if row is None else self._row_to_memory(row)

    def update_memory(self, node: MemoryNode) -> None:
        self.add_memories([node])

    def delete_memory(self, memory_id: str) -> None:
        # 软删除：status='deleted'。硬删除走单独的管理接口（48h 内物理清理）。
        self.conn.execute("UPDATE memory_node SET status='deleted' WHERE id=?", (memory_id,))
        self.conn.commit()

    # ---- 记忆编辑审计（M2-C） ------------------------------------------ #
    def add_memory_edit(self, record: MemoryEditRecord) -> None:
        self.conn.execute(
            "INSERT INTO memory_edit_log (memory_id, old_content, new_content, edited_at) "
            "VALUES (?,?,?,?)",
            (record.memory_id, record.old_content, record.new_content, _dt(record.edited_at)),
        )
        self.conn.commit()

    def list_memory_edits(self, memory_id: str, limit: int = 20) -> list[MemoryEditRecord]:
        rows = self.conn.execute(
            "SELECT memory_id, old_content, new_content, edited_at FROM memory_edit_log "
            "WHERE memory_id=? ORDER BY id DESC LIMIT ?",
            (memory_id, limit),
        ).fetchall()
        return [
            MemoryEditRecord(
                memory_id=r["memory_id"],
                old_content=r["old_content"],
                new_content=r["new_content"],
                edited_at=_pd(r["edited_at"]) or datetime.now(timezone.utc),
            )
            for r in rows
        ]

    # ---- 场景进度 ------------------------------------------------------ #
    def save_scenario_progress(self, session_id: str, data: dict) -> None:
        self.conn.execute(
            "INSERT INTO scenario_progress (session_id, payload, updated_at) VALUES (?,?,?) "
            "ON CONFLICT(session_id) DO UPDATE SET payload=excluded.payload, "
            "updated_at=excluded.updated_at",
            (session_id, _j(data), _dt(datetime.now(timezone.utc))),
        )
        self.conn.commit()

    def get_scenario_progress(self, session_id: str) -> dict | None:
        row = self.conn.execute(
            "SELECT payload FROM scenario_progress WHERE session_id=?", (session_id,)
        ).fetchone()
        return None if row is None else json.loads(row["payload"])

    # ---- 陪伴档案 ------------------------------------------------------ #
    def save_companion_profile(self, p: CompanionProfile) -> None:
        self.conn.execute(
            "INSERT INTO companion_profile (persona_id,user_id,life_stage,roles,needs,risks,updated_at) "
            "VALUES (?,?,?,?,?,?,?) ON CONFLICT(persona_id,user_id) DO UPDATE SET "
            "life_stage=excluded.life_stage, roles=excluded.roles, needs=excluded.needs, "
            "risks=excluded.risks, updated_at=excluded.updated_at",
            (p.persona_id, p.user_id, p.life_stage, _j(p.roles), _j(p.needs), _j(p.risks),
             _dt(p.updated_at)),
        )
        self.conn.commit()

    def get_companion_profile(self, persona_id: str, user_id: str) -> CompanionProfile | None:
        row = self.conn.execute(
            "SELECT * FROM companion_profile WHERE persona_id=? AND user_id=?", (persona_id, user_id)
        ).fetchone()
        if row is None:
            return None
        return CompanionProfile(
            persona_id=row["persona_id"],
            user_id=row["user_id"],
            life_stage=row["life_stage"] or "",
            roles=_pj(row["roles"], []),
            needs=_pj(row["needs"], []),
            risks=_pj(row["risks"], []),
            updated_at=_pd(row["updated_at"]) or datetime.now(timezone.utc),
        )

    # ---- 日记（M3-D） -------------------------------------------------- #
    def save_diary(self, entry: DiaryEntry) -> None:
        """upsert。冲突键 = (persona_id, user_id, date)，即「一天一篇」。

        冲突时**保留原 id**：id 是这篇日记的身份，日期没变就不该换身份
        （前端靠 id 做高亮 diff）。`created_at` 反而要刷新——
        它记的是「这篇是什么时候写下的」，重写就是重写。

        不这么写会怎样：若冲突键是 id，同一天第二次生成会插进第二行，
        `list_diary` 变成同一天两条，用户看到两个「今天」。
        """
        self.conn.execute(
            "INSERT INTO diary_entry (id, persona_id, user_id, date, content, mood_label, "
            "highlight_memories, created_at) VALUES (?,?,?,?,?,?,?,?) "
            "ON CONFLICT(persona_id, user_id, date) DO UPDATE SET "
            "content=excluded.content, mood_label=excluded.mood_label, "
            "highlight_memories=excluded.highlight_memories, created_at=excluded.created_at",
            (entry.id, entry.persona_id, entry.user_id, entry.date.isoformat(), entry.content,
             entry.mood_label, _j(entry.highlight_memories), _dt(entry.created_at)),
        )
        self.conn.commit()

    def _row_to_diary(self, r: sqlite3.Row) -> DiaryEntry:
        return DiaryEntry(
            id=r["id"],
            persona_id=r["persona_id"],
            user_id=r["user_id"],
            date=date.fromisoformat(r["date"]),
            content=r["content"] or "",
            mood_label=r["mood_label"] or "",
            highlight_memories=_pj(r["highlight_memories"], []),
            created_at=_pd(r["created_at"]) or datetime.now(timezone.utc),
        )

    def get_diary(self, persona_id: str, user_id: str, day: date) -> DiaryEntry | None:
        row = self.conn.execute(
            "SELECT * FROM diary_entry WHERE persona_id=? AND user_id=? AND date=?",
            (persona_id, user_id, day.isoformat()),
        ).fetchone()
        return None if row is None else self._row_to_diary(row)

    def list_diary(self, persona_id: str, user_id: str, limit: int = 30) -> list[DiaryEntry]:
        rows = self.conn.execute(
            "SELECT * FROM diary_entry WHERE persona_id=? AND user_id=? "
            "ORDER BY date DESC LIMIT ?",
            (persona_id, user_id, limit),
        ).fetchall()
        return [self._row_to_diary(r) for r in rows]

    # ---- 回溯 ---------------------------------------------------------- #
    def save_trace(self, trace: TraceRecord) -> None:
        self.conn.execute(
            "INSERT OR REPLACE INTO trace (turn_id, payload, created_at) VALUES (?,?,?)",
            (trace.turn_id, _j(trace.model_dump(mode="json")), _dt(datetime.now(timezone.utc))),
        )
        self.conn.commit()

    def get_trace(self, turn_id: int) -> dict[str, Any] | None:
        row = self.conn.execute("SELECT payload FROM trace WHERE turn_id=?", (turn_id,)).fetchone()
        return None if row is None else json.loads(row["payload"])

    def close(self) -> None:
        self.conn.close()
