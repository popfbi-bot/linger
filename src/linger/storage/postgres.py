"""PostgreSQL 实现 —— 多实例部署用。

**换存储，不重构**：方法名、参数、语义与 `SQLiteRepository` 一一对应，
业务层零改动（判据 2/8）。差别只在方言与连接方式：

1. 占位符是 `%s`，不是 SQLite 的 `?`。psycopg 不做 `?` 改写，
   写错一个占位符就是运行时 SyntaxError，所以这条由测试静态扫描钉住（判据 3）。
2. 结构字段落 JSONB。psycopg 把 dict / list 直接适配成 JSONB，
   读回来是 `dict` / `list`，不需要手工 `json.dumps`——这比 SQLite 版更干净。
3. 时间是 TIMESTAMPTZ，读写都是 `datetime`。只写 `timestamp` 列时 psycopg
   会把 UTC 的 tzinfo 吃掉，所以本模块一律显式 `timezone.utc`（见 `_aware`）。
4. **惰性连接**：构造只记 DSN，不建连接、不读 schema。否则 `import` 一下、
   或在 CI（没有 PG）里实例化一次，服务就被一个连不上的库拖死（判据 5）。
   首次真正用到连接时才 `open()`：建池 → 灌 schema → 开驱动。

`psycopg` 不是硬依赖（判据 6）：本模块 import 时不碰它，
缺驱动时抛的是「装了 [postgres] extras 就能跑」这种可读错误，
而不是裸 `ImportError` 把整个进程掀翻。
"""

from __future__ import annotations

import json
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

from ..accounts.models import Account
from ..market.models import (
    STATUS_APPROVED,
    STATUS_PENDING,
    InstalledCopy,
    MarketListing,
)
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

SCHEMA_PATH = Path(__file__).with_name("postgres_schema.sql")
SCHEMA_SQL = SCHEMA_PATH.read_text(encoding="utf-8")

# psycopg3 的 Connection 是泛型（行工厂类型），这里只当「一行能按名取值」用。
Row = Any

# 取序列下个值。表名写死在 SQL 里（两条语句，两个常量），不做模板拼接：
# 一旦 SQL 由 f-string 生成，"SQL 里不许拼值" 这条静态扫描就失去意义了。
_NEXT_TURN_ID = "SELECT nextval(pg_get_serial_sequence('turn', 'id')) AS id"
_NEXT_MEMORY_EDIT_ID = "SELECT nextval(pg_get_serial_sequence('memory_edit_log', 'id')) AS id"


def _jd(v: Any) -> str:
    """JSONB 参数的显式序列化。

    psycopg 其实会把 dict/list 自动适配成 JSONB，但显式 `json.dumps` 有两个好处：
    一是「这个值就是要落成 JSON」在代码里看得见，二是避开 list[str] 被当成
    PG 数组（TEXT[]）适配的歧义——我们建的是 JSONB 列，类型不一致会报错。
    """
    return json.dumps(v, ensure_ascii=False)


def _aware(v: datetime | None) -> datetime | None:
    """钉住时区。

    写进 TIMESTAMPTZ 的值必须带 tzinfo：裸 datetime 会被 PG 按会话时区解释，
    跨机器就是「同一行数据，两台服务器读出两个时间」。
    """
    if v is None:
        return None
    return v if v.tzinfo is not None else v.replace(tzinfo=timezone.utc)


class PostgresRepository(Repository):
    def __init__(
        self,
        conninfo: str | None = None,
        *,
        schema: str | None = None,
        pool_size: int = 5,
    ) -> None:
        """只记参数，不连库（判据 5：构造 / import 都不能碰网络）。"""
        from ..config import settings

        self.conninfo = conninfo or settings.pg_dsn
        self.schema = schema or SCHEMA_SQL
        self.pool_size = pool_size
        # 已建立的连接数。构造后恒为 0——它就是「构造没连库」这条判据的直接观测点。
        self.connected = 0
        self._pool: Any = None


    # ---- 连接管理 ------------------------------------------------------ #
    def _driver(self) -> tuple[Any, Any]:
        """按需加载 psycopg。缺驱动时给一句能照做的错话，而不是裸 ImportError。"""
        try:
            from psycopg.rows import dict_row
            from psycopg_pool import ConnectionPool
        except ImportError as exc:  # pragma: no cover - 装了 extras 就不会走到
            raise RuntimeError(
                "[postgres] 后端需要 psycopg，请安装可选依赖：pip install 'lingxi[postgres]'"
            ) from exc
        return ConnectionPool, dict_row

    def _ensure_pool(self) -> Any:
        """首次使用时才建池并灌 schema。"""
        if self._pool is None:
            pool_cls, row_factory = self._driver()
            pool = pool_cls(
                self.conninfo,
                min_size=1,
                max_size=self.pool_size,
                open=False,
                # dict_row：psycopg 的默认行是 tuple，只能按下标取值。
                # 统一成 dict 后，读取路径和 SQLite 版（sqlite3.Row）写法一致。
                kwargs={"autocommit": True, "row_factory": row_factory},
            )
            pool.open()
            self.connected = 1
            try:
                with pool.connection() as conn:
                    conn.execute(self.schema)
            except Exception:
                # 建表失败就别把半死的池留在手上，否则下一次调用会复用它
                pool.close()
                self._pool = None
                self.connected = 0
                raise
            self._pool = pool
        return self._pool

    def open(self) -> None:
        """显式建连（可选）。不调也行，首次查询会自动建。"""
        self._ensure_pool()

    def _run(self, sql: str, args: tuple | list = ()) -> list[Row]:
        """执行一条语句并取回全部行（autocommit，逐条即事务）。"""
        with self._ensure_pool().connection() as conn:
            cur = conn.execute(sql, tuple(args))
            return cur.fetchall() if cur.description is not None else []

    def _one(self, sql: str, args: tuple | list = ()) -> Row | None:
        rows = self._run(sql, tuple(args))
        return rows[0] if rows else None

    def _next_id(self, sql: str) -> int:
        """预取下一条自增 id。`sql` 由调用方以字面量传入（见表名常量的用法）。

        SQLite 版靠 `lastrowid` 拿 `append_turn` 的返回值，psycopg 没有等价物。
        而 `append_turn` 的签名要求返回 int，所以这里先取一次序列值，
        再显式带 id 插入——两句话，换一个所有实现共用的接口。

        参数是 SQL 而不是表名，是为了让 SQL 本身保持字面量：
        模块里没有一条 SQL 是由 f-string 拼出来的（判据 3 的静态扫描据此成立）。
        """
        row = self._one(sql)
        return int(row["id"]) if row is not None else 0

    # ---- 人格 ---------------------------------------------------------- #
    def save_persona(self, card: PersonaCard) -> None:
        """upsert。`status` 刻意不在 UPDATE 里：改名改卡不能把软删的人格救回来。"""
        self._run(
            "INSERT INTO persona (id, version, name, card, created_at) VALUES (%s,%s,%s,%s,%s) "
            "ON CONFLICT (id) DO UPDATE SET version=excluded.version, name=excluded.name, "
            "card=excluded.card",
            (
                card.id,
                card.version,
                card.name,
                _jd(card.model_dump(mode="json")),
                _aware(datetime.now(timezone.utc)),
            ),
        )

    def get_persona(self, persona_id: str) -> PersonaCard | None:
        """只认 active。被软删的人格返回 None —— 用已删人格开会话自然 404。"""
        row = self._one(
            "SELECT card FROM persona WHERE id=%s AND status='active'", (persona_id,)
        )
        return None if row is None else PersonaCard.model_validate(row["card"])

    def list_personas(self, limit: int = 50) -> list[PersonaCard]:
        """只列 active，新的在前。"""
        rows = self._run(
            "SELECT card FROM persona WHERE status='active' "
            "ORDER BY created_at DESC, id LIMIT %s",
            (limit,),
        )
        return [PersonaCard.model_validate(r["card"]) for r in rows]

    def find_persona_by_template(self, template_id: str) -> PersonaCard | None:
        """按 provenance.template 找**仍然 active** 的已实例化人格。

        与 SQLite 版同样扫表而不是加列：人格是「人」的量级。
        但 PG 这边可以直接让数据库过滤 JSONB，不必把几百张卡拉到 Python 里
        逐条解析——`card -> 'provenance' ->> 'template'` 正好走 JSONB 取值。
        `ORDER BY created_at ASC` 与 SQLite 版一致：最早的那个才是「原来那位」。

        **过滤 status 是设计而非疏漏**：删掉林汐再点林汐 = 重置这段关系，
        查不到已删那份，实例化才会重建、记忆从零。
        """
        row = self._one(
            "SELECT card FROM persona WHERE status='active' "
            "AND card -> 'provenance' ->> 'template' = %s "
            "ORDER BY created_at ASC, id LIMIT 1",
            (template_id,),
        )
        return None if row is None else PersonaCard.model_validate(row["card"])

    def delete_persona(self, persona_id: str) -> PersonaCard | None:
        """软删除，返回被删的卡；不存在或已删返回 None。

        只动 persona.status 一行，不级联（删除人格 ≠ 删除历史）。
        `UPDATE ... WHERE status='active' RETURNING` 一句话解决并发下的
        「先查后改」竞态：两个人同时删，只有一个能拿到 RETURNING 的行。
        """
        row = self._one(
            "UPDATE persona SET status='deleted' WHERE id=%s AND status='active' "
            "RETURNING card",
            (persona_id,),
        )
        return None if row is None else PersonaCard.model_validate(row["card"])

    # ---- 会话 ---------------------------------------------------------- #
    def save_session(self, s: Session) -> None:
        """upsert。冲突时连身份列一起改写——认领靠这条路径改 `user_id`（同 SQLite 版）。"""
        self._run(
            "INSERT INTO session (id, persona_id, user_id, scenario_id, started_at, last_active_at) "
            "VALUES (%s,%s,%s,%s,%s,%s) ON CONFLICT (id) DO UPDATE SET "
            "user_id=excluded.user_id, scenario_id=excluded.scenario_id, "
            "last_active_at=excluded.last_active_at",
            (
                s.id,
                s.persona_id,
                s.user_id,
                s.scenario_id,
                _aware(s.started_at),
                _aware(s.last_active_at),
            ),
        )

    def _row_to_session(self, r: Row) -> Session:
        return Session(
            id=r["id"],
            persona_id=r["persona_id"],
            user_id=r["user_id"],
            scenario_id=r["scenario_id"],
            started_at=r["started_at"] or datetime.now(timezone.utc),
            last_active_at=r["last_active_at"] or datetime.now(timezone.utc),
        )

    def get_session(self, session_id: str) -> Session | None:
        row = self._one("SELECT * FROM session WHERE id=%s", (session_id,))
        return None if row is None else self._row_to_session(row)

    def list_sessions(self, persona_id: str, user_id: str, limit: int = 100) -> list[Session]:
        rows = self._run(
            "SELECT * FROM session WHERE persona_id=%s AND user_id=%s "
            "ORDER BY last_active_at DESC LIMIT %s",
            (persona_id, user_id, limit),
        )
        return [self._row_to_session(r) for r in rows]

    # ---- 对话轮（L0） -------------------------------------------------- #
    def append_turn(
        self, session_id: str, role: str, content: str, affect: dict[str, float] | None = None
    ) -> int:
        turn_id = self._next_id(_NEXT_TURN_ID)
        self._run(
            "INSERT INTO turn (id, session_id, role, content, affect_snapshot, created_at) "
            "VALUES (%s,%s,%s,%s,%s,%s)",
            (
                turn_id,
                session_id,
                role,
                content,
                _jd(affect) if affect else None,
                _aware(datetime.now(timezone.utc)),
            ),
        )
        return turn_id

    def list_turns(self, session_id: str, limit: int = 40) -> list[Turn]:
        """取最近 limit 条，再翻正序返回——对外永远是「时间正序的上下文窗口」。"""
        rows = self._run(
            "SELECT * FROM turn WHERE session_id=%s ORDER BY id DESC LIMIT %s",
            (session_id, limit),
        )
        out = [
            Turn(
                id=r["id"],
                session_id=r["session_id"],
                role=r["role"],
                content=r["content"],
                affect_snapshot=r["affect_snapshot"],
                created_at=r["created_at"] or datetime.now(timezone.utc),
            )
            for r in rows
        ]
        out.reverse()
        return out

    # ---- 情绪 ---------------------------------------------------------- #
    def get_affect(self, persona_id: str, user_id: str) -> AffectState | None:
        row = self._one(
            "SELECT * FROM affect_state WHERE persona_id=%s AND user_id=%s",
            (persona_id, user_id),
        )
        if row is None:
            return None
        return AffectState(
            pad=PAD(
                pleasure=row["pleasure"],
                arousal=row["arousal"],
                dominance=row["dominance"],
            ),
            label=row["label"] or "中性",
            updated_at=row["updated_at"] or datetime.now(timezone.utc),
        )

    def save_affect(self, persona_id: str, user_id: str, state: AffectState) -> None:
        self._run(
            "INSERT INTO affect_state "
            "(persona_id,user_id,pleasure,arousal,dominance,label,updated_at) "
            "VALUES (%s,%s,%s,%s,%s,%s,%s) ON CONFLICT (persona_id,user_id) DO UPDATE SET "
            "pleasure=excluded.pleasure, arousal=excluded.arousal, dominance=excluded.dominance, "
            "label=excluded.label, updated_at=excluded.updated_at",
            (
                persona_id,
                user_id,
                state.pad.pleasure,
                state.pad.arousal,
                state.pad.dominance,
                state.label,
                _aware(state.updated_at),
            ),
        )

    # ---- 关系 ---------------------------------------------------------- #
    def get_relation(self, persona_id: str, user_id: str) -> RelationState | None:
        row = self._one(
            "SELECT * FROM relation_state WHERE persona_id=%s AND user_id=%s",
            (persona_id, user_id),
        )
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
            first_met_at=row["first_met_at"] or datetime.now(timezone.utc),
            level_up_at=row["level_up_at"],
        )

    def save_relation(self, s: RelationState) -> None:
        """upsert。`first_met_at` 不更新：第一次见面只有一次。"""
        self._run(
            "INSERT INTO relation_state (persona_id,user_id,level,intimacy,active_days,turns,"
            "deep_turns,resonance_events,self_disclosure,unresolved_conflicts,first_met_at,"
            "level_up_at) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) "
            "ON CONFLICT (persona_id,user_id) DO UPDATE SET level=excluded.level, "
            "intimacy=excluded.intimacy, active_days=excluded.active_days, turns=excluded.turns, "
            "deep_turns=excluded.deep_turns, resonance_events=excluded.resonance_events, "
            "self_disclosure=excluded.self_disclosure, "
            "unresolved_conflicts=excluded.unresolved_conflicts, "
            "level_up_at=excluded.level_up_at",
            (
                s.persona_id,
                s.user_id,
                s.level,
                s.intimacy,
                s.active_days,
                s.turns,
                s.deep_turns,
                s.resonance_events,
                s.self_disclosure,
                s.unresolved_conflicts,
                _aware(s.first_met_at),
                _aware(s.level_up_at),
            ),
        )

    # ---- 记忆（L2~L5） ------------------------------------------------- #
    def add_memories(self, nodes: list[MemoryNode]) -> None:
        """批量 upsert。任一条失败整批回滚——记忆是成套写进去的，半套没有意义。"""
        if not nodes:
            return
        sql = (
            "INSERT INTO memory_node (id,persona_id,user_id,layer,type,content,entities,"
            "embedding,importance,strength,recall_count,mention_count,emotion,source_type,"
            "source_turn_ids,confidence,status,pinned,created_at,last_recalled_at,updated_at) "
            "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) "
            "ON CONFLICT (id) DO UPDATE SET layer=excluded.layer, type=excluded.type, "
            "content=excluded.content, entities=excluded.entities, embedding=excluded.embedding, "
            "importance=excluded.importance, strength=excluded.strength, "
            "recall_count=excluded.recall_count, mention_count=excluded.mention_count, "
            "emotion=excluded.emotion, source_type=excluded.source_type, "
            "source_turn_ids=excluded.source_turn_ids, confidence=excluded.confidence, "
            "status=excluded.status, pinned=excluded.pinned, "
            "last_recalled_at=excluded.last_recalled_at, updated_at=excluded.updated_at"
        )
        params = [
            (
                n.id,
                n.persona_id,
                n.user_id,
                n.layer,
                n.type,
                n.content,
                _jd(n.entities),
                _jd(n.embedding or []),
                n.importance,
                n.strength,
                n.recall_count,
                n.mention_count,
                _jd(n.emotion),
                n.source_type,
                _jd(n.source_turn_ids),
                n.confidence,
                n.status,
                n.pinned,
                _aware(n.created_at),
                _aware(n.last_recalled_at),
                _aware(n.updated_at),
            )
            for n in nodes
        ]
        with self._ensure_pool().connection() as conn:
            with conn.cursor() as cur:
                # 一次 executemany 走一条隐式事务：psycopg 会把它当单个事务提交
                cur.executemany(sql, params)

    def _row_to_memory(self, r: Row) -> MemoryNode:
        return MemoryNode(
            id=r["id"],
            persona_id=r["persona_id"],
            user_id=r["user_id"],
            layer=r["layer"],
            type=r["type"],
            content=r["content"],
            entities=r["entities"] or [],
            embedding=r["embedding"] or [],
            importance=r["importance"],
            strength=r["strength"],
            recall_count=r["recall_count"],
            mention_count=r["mention_count"],
            emotion=r["emotion"] or {},
            source_type=r["source_type"],
            source_turn_ids=r["source_turn_ids"] or [],
            confidence=r["confidence"],
            status=r["status"],
            pinned=bool(r["pinned"]),
            created_at=r["created_at"] or datetime.now(timezone.utc),
            updated_at=r["updated_at"],
            last_recalled_at=r["last_recalled_at"],
        )

    def list_memories(
        self, persona_id: str, user_id: str, layer: int | None = None, limit: int = 500
    ) -> list[MemoryNode]:
        """按 (persona, user) 取有效记忆，新的在前；`deleted` 永不返回。"""
        sql = "SELECT * FROM memory_node WHERE persona_id=%s AND user_id=%s AND status<>'deleted'"
        args: list[Any] = [persona_id, user_id]
        if layer is not None:
            sql += " AND layer=%s"
            args.append(layer)
        sql += " ORDER BY created_at DESC, id LIMIT %s"
        args.append(limit)
        return [self._row_to_memory(r) for r in self._run(sql, args)]

    def get_memory(self, memory_id: str) -> MemoryNode | None:
        row = self._one(
            "SELECT * FROM memory_node WHERE id=%s AND status<>'deleted'", (memory_id,)
        )
        return None if row is None else self._row_to_memory(row)

    def update_memory(self, node: MemoryNode) -> None:
        self.add_memories([node])

    def delete_memory(self, memory_id: str) -> None:
        """软删除。物理清理走单独的管理流程（与 SQLite 版同语义）。"""
        self._run("UPDATE memory_node SET status='deleted' WHERE id=%s", (memory_id,))

    # ---- 记忆编辑审计（M2-C） ------------------------------------------ #
    def add_memory_edit(self, record: MemoryEditRecord) -> None:
        edit_id = self._next_id(_NEXT_MEMORY_EDIT_ID)
        self._run(
            "INSERT INTO memory_edit_log (id, memory_id, old_content, new_content, edited_at) "
            "VALUES (%s,%s,%s,%s,%s)",
            (
                edit_id,
                record.memory_id,
                record.old_content,
                record.new_content,
                _aware(record.edited_at),
            ),
        )

    def list_memory_edits(self, memory_id: str, limit: int = 20) -> list[MemoryEditRecord]:
        rows = self._run(
            "SELECT memory_id, old_content, new_content, edited_at FROM memory_edit_log "
            "WHERE memory_id=%s ORDER BY id DESC LIMIT %s",
            (memory_id, limit),
        )
        return [
            MemoryEditRecord(
                memory_id=r["memory_id"],
                old_content=r["old_content"],
                new_content=r["new_content"],
                edited_at=r["edited_at"] or datetime.now(timezone.utc),
            )
            for r in rows
        ]

    # ---- 场景进度（M1.5） ---------------------------------------------- #
    def save_scenario_progress(self, session_id: str, data: dict) -> None:
        self._run(
            "INSERT INTO scenario_progress (session_id, payload, updated_at) VALUES (%s,%s,%s) "
            "ON CONFLICT (session_id) DO UPDATE SET payload=excluded.payload, "
            "updated_at=excluded.updated_at",
            (session_id, _jd(data), _aware(datetime.now(timezone.utc))),
        )

    def get_scenario_progress(self, session_id: str) -> dict | None:
        row = self._one(
            "SELECT payload FROM scenario_progress WHERE session_id=%s", (session_id,)
        )
        return None if row is None else row["payload"]

    # ---- 陪伴档案（M2-A） ---------------------------------------------- #
    def save_companion_profile(self, p: CompanionProfile) -> None:
        """整体覆盖：档案是「当前认知」，不是流水账。"""
        self._run(
            "INSERT INTO companion_profile "
            "(persona_id,user_id,life_stage,roles,needs,risks,updated_at) "
            "VALUES (%s,%s,%s,%s,%s,%s,%s) ON CONFLICT (persona_id,user_id) DO UPDATE SET "
            "life_stage=excluded.life_stage, roles=excluded.roles, needs=excluded.needs, "
            "risks=excluded.risks, updated_at=excluded.updated_at",
            (
                p.persona_id,
                p.user_id,
                p.life_stage,
                _jd(p.roles),
                _jd(p.needs),
                _jd(p.risks),
                _aware(p.updated_at),
            ),
        )

    def get_companion_profile(self, persona_id: str, user_id: str) -> CompanionProfile | None:
        row = self._one(
            "SELECT * FROM companion_profile WHERE persona_id=%s AND user_id=%s",
            (persona_id, user_id),
        )
        if row is None:
            return None
        return CompanionProfile(
            persona_id=row["persona_id"],
            user_id=row["user_id"],
            life_stage=row["life_stage"] or "",
            roles=row["roles"] or [],
            needs=row["needs"] or [],
            risks=row["risks"] or [],
            updated_at=row["updated_at"] or datetime.now(timezone.utc),
        )

    # ---- 日记（M3-D） -------------------------------------------------- #
    def save_diary(self, entry: DiaryEntry) -> None:
        """upsert，冲突键 = (persona_id, user_id, date)，即「一天一篇」。

        冲突时**保留原 id**：id 是这篇日记的身份，日期没变就不该换身份。
        `created_at` 刷新——它记的是「什么时候写下的」，重写就是重写。
        `date` 列是 DATE，写 `date` 对象而不是 isoformat 字符串：
        字符串落 DATE 列会被 PG 按会话时区猜，跨时区就可能差一天。
        """
        self._run(
            "INSERT INTO diary_entry (id, persona_id, user_id, date, content, mood_label, "
            "highlight_memories, created_at) VALUES (%s,%s,%s,%s,%s,%s,%s,%s) "
            "ON CONFLICT (persona_id, user_id, date) DO UPDATE SET "
            "content=excluded.content, mood_label=excluded.mood_label, "
            "highlight_memories=excluded.highlight_memories, created_at=excluded.created_at",
            (
                entry.id,
                entry.persona_id,
                entry.user_id,
                entry.date,
                entry.content,
                entry.mood_label,
                _jd(entry.highlight_memories),
                _aware(entry.created_at),
            ),
        )

    def _row_to_diary(self, r: Row) -> DiaryEntry:
        return DiaryEntry(
            id=r["id"],
            persona_id=r["persona_id"],
            user_id=r["user_id"],
            date=r["date"],
            content=r["content"] or "",
            mood_label=r["mood_label"] or "",
            highlight_memories=r["highlight_memories"] or [],
            created_at=r["created_at"] or datetime.now(timezone.utc),
        )

    def get_diary(self, persona_id: str, user_id: str, day: date) -> DiaryEntry | None:
        row = self._one(
            "SELECT * FROM diary_entry WHERE persona_id=%s AND user_id=%s AND date=%s",
            (persona_id, user_id, day),
        )
        return None if row is None else self._row_to_diary(row)

    def list_diary(self, persona_id: str, user_id: str, limit: int = 30) -> list[DiaryEntry]:
        """新的在前。用户隔离是硬要求：u_a 的心事不能被 u_b 读到。"""
        rows = self._run(
            "SELECT * FROM diary_entry WHERE persona_id=%s AND user_id=%s "
            "ORDER BY date DESC LIMIT %s",
            (persona_id, user_id, limit),
        )
        return [self._row_to_diary(r) for r in rows]

    # ---- 账号（M3-H） -------------------------------------------------- #
    def create_account(
        self, email: str, user_id: str, password_hash: str, display_name: str = ""
    ) -> Account | None:
        """邮箱已存在 → None。

        `ON CONFLICT (email) DO NOTHING` + `RETURNING user_id`：
        有返行就是插入成功，没返行就是被 UNIQUE 挡下。
        注意**不能**用 `ON CONFLICT (user_id)`——user_id 是随机生成的，
        撞它的概率可以忽略，但真撞上时正确的行为是报错而不是「静默当作邮箱重复」。
        """
        created = datetime.now(timezone.utc)
        rows = self._run(
            "INSERT INTO account (user_id, email, password_hash, display_name, token_epoch, created_at) "
            "VALUES (%s,%s,%s,%s,%s,%s) ON CONFLICT (email) DO NOTHING RETURNING user_id",
            (user_id, email, password_hash, display_name, 1, _aware(created)),
        )
        if not rows:
            return None
        return Account(
            email=email,
            user_id=user_id,
            display_name=display_name,
            token_epoch=1,
            created_at=created,
        )

    def find_account(self, email: str) -> dict[str, Any] | None:
        row = self._one("SELECT * FROM account WHERE email=%s", (email,))
        if row is None:
            return None
        return {
            "user_id": row["user_id"],
            "email": row["email"],
            "password_hash": row["password_hash"],
            "display_name": row["display_name"] or "",
            "token_epoch": int(row["token_epoch"] or 1),
            "created_at": row["created_at"] or datetime.now(timezone.utc),
        }

    def account_exists(self, user_id: str) -> bool:
        row = self._one("SELECT 1 AS hit FROM account WHERE user_id=%s LIMIT 1", (user_id,))
        return row is not None

    def list_users_for_persona(self, persona_id: str) -> list[str]:
        """从 session 聚合。`idx_session_owner` 是 (persona_id, user_id, last_active_at DESC)，
        而这里要按 user_id 分组再按活跃时间排——索引帮不上忙，
        但一个人格下的会话数量是「人 × 会话数」的量级，可接受。
        真到需要时再加 `(persona_id, user_id, last_active_at)` 上的覆盖查询。
        """
        rows = self._run(
            "SELECT user_id, MAX(last_active_at) AS seen FROM session "
            "WHERE persona_id=%s GROUP BY user_id ORDER BY seen DESC",
            (persona_id,),
        )
        return [r["user_id"] for r in rows]

    # ---- 回溯 ---------------------------------------------------------- #
    def save_trace(self, trace: TraceRecord) -> None:
        self._run(
            "INSERT INTO trace (turn_id, payload, created_at) VALUES (%s,%s,%s) "
            "ON CONFLICT (turn_id) DO UPDATE SET payload=excluded.payload, "
            "created_at=excluded.created_at",
            (
                trace.turn_id,
                _jd(trace.model_dump(mode="json")),
                _aware(datetime.now(timezone.utc)),
            ),
        )

    def get_trace(self, turn_id: int) -> dict[str, Any] | None:
        row = self._one("SELECT payload FROM trace WHERE turn_id=%s", (turn_id,))
        return None if row is None else row["payload"]

    # ---- 关闭 ---------------------------------------------------------- #
    # ---- 人格市场（M4-B） ---------------------------------------------- #
    def _listing(self, row: Row) -> MarketListing:
        return MarketListing(
            id=row["id"],
            persona_id=row["persona_id"],
            author_id=row["author_id"],
            title=row["title"],
            summary=row["summary"],
            snapshot=row["snapshot"],
            status=row["status"],
            listed_at=row["listed_at"],
            reviewed_at=row["reviewed_at"],
            reviewer_id=row["reviewer_id"],
            review_note=row["review_note"],
        )

    def create_listing(self, listing: MarketListing) -> MarketListing:
        """撞上部分唯一索引（同人格已有待审/在架）时，把已有那条还回去。"""
        try:
            self._run(
                "INSERT INTO market_listing (id, persona_id, author_id, title, summary, snapshot,"
                " status, listed_at, reviewed_at, reviewer_id, review_note)"
                " VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                (
                    listing.id,
                    listing.persona_id,
                    listing.author_id,
                    listing.title,
                    listing.summary,
                    _jd(listing.snapshot),
                    listing.status,
                    _aware(listing.listed_at),
                    _aware(listing.reviewed_at),
                    listing.reviewer_id,
                    listing.review_note,
                ),
            )
        except Exception as exc:  # psycopg 的 UniqueViolation，不 import 也能判
            if "unique" not in str(exc).lower() and "duplicate" not in str(exc).lower():
                raise
            existing = self.find_live_listing(listing.persona_id)
            if existing is None:  # pragma: no cover
                raise
            return existing
        return listing

    def get_listing(self, listing_id: str) -> MarketListing | None:
        row = self._one("SELECT * FROM market_listing WHERE id=%s", (listing_id,))
        return None if row is None else self._listing(row)

    def list_listings(self, limit: int = 50) -> list[MarketListing]:
        rows = self._run(
            "SELECT * FROM market_listing ORDER BY listed_at DESC NULLS LAST, id LIMIT %s",
            (limit,),
        )
        return [self._listing(r) for r in rows]

    def find_live_listing(self, persona_id: str) -> MarketListing | None:
        row = self._one(
            "SELECT * FROM market_listing WHERE persona_id=%s AND status IN (%s,%s)"
            " ORDER BY listed_at DESC NULLS LAST LIMIT 1",
            (persona_id, STATUS_PENDING, STATUS_APPROVED),
        )
        return None if row is None else self._listing(row)

    def review_listing(
        self,
        listing_id: str,
        *,
        from_status: str,
        to_status: str,
        reviewer_id: str,
        note: str,
        reviewed_at: Any,
    ) -> bool:
        """条件更新。返回 False = 状态已不是 from_status（重复审核 / 并发后到者）。"""
        with self._ensure_pool().connection() as conn:
            cur = conn.execute(
                "UPDATE market_listing SET status=%s, reviewer_id=%s, review_note=%s,"
                " reviewed_at=%s WHERE id=%s AND status=%s",
                (to_status, reviewer_id, note, _aware(reviewed_at), listing_id, from_status),
            )
            return cur.rowcount == 1

    def save_install(self, record: InstalledCopy) -> bool:
        """主键冲突返回 False（并发后到者）。"""
        try:
            self._run(
                "INSERT INTO market_install (listing_id, user_id, persona_id, installed_at)"
                " VALUES (%s,%s,%s,%s)",
                (
                    record.listing_id,
                    record.user_id,
                    record.persona_id,
                    _aware(record.installed_at),
                ),
            )
        except Exception as exc:
            if "unique" not in str(exc).lower() and "duplicate" not in str(exc).lower():
                raise
            return False
        return True

    def get_install(self, listing_id: str, user_id: str) -> InstalledCopy | None:
        row = self._one(
            "SELECT * FROM market_install WHERE listing_id=%s AND user_id=%s",
            (listing_id, user_id),
        )
        if row is None:
            return None
        return InstalledCopy(
            listing_id=row["listing_id"],
            user_id=row["user_id"],
            persona_id=row["persona_id"],
            installed_at=row["installed_at"],
        )

    def drop_install(self, listing_id: str, user_id: str) -> None:
        self._run(
            "DELETE FROM market_install WHERE listing_id=%s AND user_id=%s",
            (listing_id, user_id),
        )

    def close(self) -> None:
        """真的关池，且可重入（HTTP 层的 shutdown 与测试 teardown 都会调）。"""
        pool, self._pool = self._pool, None
        self.connected = 0
        if pool is not None:
            pool.close()
