"""SQLite 实现 —— dev 与单机部署用。

字段与 docs/ARCHITECTURE.md §4.2 的 PostgreSQL schema 一一对应，
差别只在：向量用 JSON 文本存（256 维暴力计算），时间用 ISO 字符串。
迁到 PG 时数据可直接搬过去。
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ..models import PAD, AffectState, MemoryNode, PersonaCard, RelationState, Session, TraceRecord, Turn
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
CREATE TABLE IF NOT EXISTS trace (
  turn_id INTEGER PRIMARY KEY,
  payload TEXT NOT NULL,
  created_at TEXT NOT NULL
);
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
        self.conn.commit()

    # ---- 人格 ---------------------------------------------------------- #
    def save_persona(self, card: PersonaCard) -> None:
        self.conn.execute(
            "INSERT INTO persona (id, version, name, card, created_at) VALUES (?,?,?,?,?) "
            "ON CONFLICT(id) DO UPDATE SET version=excluded.version, name=excluded.name, card=excluded.card",
            (card.id, card.version, card.name, _j(card.model_dump(mode="json")), _dt(datetime.now(timezone.utc))),
        )
        self.conn.commit()

    def get_persona(self, persona_id: str) -> PersonaCard | None:
        row = self.conn.execute("SELECT card FROM persona WHERE id=?", (persona_id,)).fetchone()
        return None if row is None else PersonaCard.model_validate(json.loads(row["card"]))

    def list_personas(self, limit: int = 50) -> list[PersonaCard]:
        rows = self.conn.execute("SELECT card FROM persona ORDER BY created_at DESC LIMIT ?", (limit,)).fetchall()
        return [PersonaCard.model_validate(json.loads(r["card"])) for r in rows]

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
                "source_turn_ids,confidence,status,pinned,created_at,last_recalled_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (n.id, n.persona_id, n.user_id, n.layer, n.type, n.content, _j(n.entities),
                 _j(n.embedding or []), n.importance, n.strength, n.recall_count, n.mention_count,
                 _j(n.emotion), n.source_type, _j(n.source_turn_ids), n.confidence, n.status,
                 int(n.pinned), _dt(n.created_at), _dt(n.last_recalled_at)),
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
