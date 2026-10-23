-- PostgreSQL schema（架构 §4.2 的落地版）。
--
-- 与 sqlite.py 的 _SCHEMA 表名集合一一对应：11 张表，不多不少。
-- 「换存储，不重构」——所以字段与语义照抄 SQLite 版，只在类型上换成 PG 的写法：
--   * 结构化字段（entities / embedding / emotion / card / payload …）用 JSONB，
--     JSONB 列才是「真 JSON」：PG 会校验格式，也能被 `->>` / `@>` 直接查询。
--   * 时间一律 TIMESTAMPTZ。TIMESTAMP 会丢掉时区，跨时区一读完就错，
--     这是刻意不用它的原因。
--   * turn / memory_edit_log 的自增主键用 BIGSERIAL（SQLite 的 AUTOINCREMENT）。
--
-- 向量没上 pgvector：现在是 256 维哈希嵌入 + 暴力余弦（见 sqlite.py 的实现），
-- 加一个扩展依赖换不来收益。真上 bge（1024 维）时再迁 VECTOR + hnsw，
-- 那时的迁移脚本会一并补上，现在不预支复杂度。

CREATE TABLE IF NOT EXISTS persona (
  id         TEXT PRIMARY KEY,
  version    INTEGER NOT NULL DEFAULT 1,
  name       TEXT NOT NULL,
  card       JSONB NOT NULL,
  status     TEXT DEFAULT 'active',
  created_at TIMESTAMPTZ NOT NULL
);

CREATE TABLE IF NOT EXISTS session (
  id             TEXT PRIMARY KEY,
  persona_id     TEXT NOT NULL,
  user_id        TEXT NOT NULL,
  scenario_id    TEXT,
  started_at     TIMESTAMPTZ NOT NULL,
  last_active_at TIMESTAMPTZ NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_session_owner ON session (persona_id, user_id, last_active_at DESC);

-- L0。affect_snapshot 是该轮结束时的 PAD，JSONB 可为空（用户轮没有情绪快照）。
CREATE TABLE IF NOT EXISTS turn (
  id              BIGSERIAL PRIMARY KEY,
  session_id      TEXT NOT NULL,
  role            TEXT NOT NULL,
  content         TEXT NOT NULL,
  affect_snapshot JSONB,
  created_at      TIMESTAMPTZ NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_turn_session ON turn (session_id, id);

-- 情绪：每人格每用户一条。主键是二元组，不是 id —— 情绪是「当前状态」，不是流水。
CREATE TABLE IF NOT EXISTS affect_state (
  persona_id  TEXT NOT NULL,
  user_id     TEXT NOT NULL,
  pleasure    REAL,
  arousal     REAL,
  dominance   REAL,
  label       TEXT,
  updated_at  TIMESTAMPTZ,
  PRIMARY KEY (persona_id, user_id)
);

CREATE TABLE IF NOT EXISTS relation_state (
  persona_id           TEXT NOT NULL,
  user_id              TEXT NOT NULL,
  level                INTEGER,
  intimacy             REAL,
  active_days          INTEGER,
  turns                INTEGER,
  deep_turns           INTEGER,
  resonance_events     INTEGER,
  self_disclosure      INTEGER,
  unresolved_conflicts INTEGER,
  first_met_at         TIMESTAMPTZ,
  level_up_at          TIMESTAMPTZ,
  PRIMARY KEY (persona_id, user_id)
);

CREATE TABLE IF NOT EXISTS memory_node (
  id               TEXT PRIMARY KEY,
  persona_id       TEXT NOT NULL,
  user_id          TEXT NOT NULL,
  layer            INTEGER NOT NULL,
  type             TEXT NOT NULL,
  content          TEXT NOT NULL,
  entities         JSONB,
  embedding        JSONB,
  importance       REAL,
  strength         REAL,
  recall_count     INTEGER,
  mention_count    INTEGER,
  emotion          JSONB,
  source_type      TEXT,
  source_turn_ids  JSONB,
  confidence       REAL,
  status           TEXT,
  pinned           BOOLEAN,
  created_at       TIMESTAMPTZ,
  last_recalled_at TIMESTAMPTZ,
  -- 只由「用户编辑」推进；召回回写不动它。NULL = 从未编辑过。
  updated_at       TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS idx_mem_owner ON memory_node (persona_id, user_id, status);

CREATE TABLE IF NOT EXISTS scenario_progress (
  session_id TEXT PRIMARY KEY,
  payload    JSONB NOT NULL,
  updated_at TIMESTAMPTZ NOT NULL
);

CREATE TABLE IF NOT EXISTS companion_profile (
  persona_id TEXT NOT NULL,
  user_id    TEXT NOT NULL,
  life_stage TEXT NOT NULL DEFAULT '',
  roles      JSONB,
  needs      JSONB,
  risks      JSONB,
  updated_at TIMESTAMPTZ NOT NULL,
  PRIMARY KEY (persona_id, user_id)
);

CREATE TABLE IF NOT EXISTS trace (
  turn_id    BIGINT PRIMARY KEY,
  payload    JSONB NOT NULL,
  created_at TIMESTAMPTZ NOT NULL
);

CREATE TABLE IF NOT EXISTS memory_edit_log (
  id          BIGSERIAL PRIMARY KEY,
  memory_id   TEXT NOT NULL,
  old_content TEXT NOT NULL,
  new_content TEXT NOT NULL,
  edited_at   TIMESTAMPTZ NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_mem_edit_memory ON memory_edit_log (memory_id, id);

-- 日记：一天一篇。唯一键是三元组，不是 id ——
-- 重复生成必须命中冲突去覆盖，否则「写下今天」会变成「每次点都多一篇」。
CREATE TABLE IF NOT EXISTS diary_entry (
  id                 TEXT PRIMARY KEY,
  persona_id         TEXT NOT NULL,
  user_id            TEXT NOT NULL,
  date               DATE NOT NULL,
  content            TEXT NOT NULL,
  mood_label         TEXT,
  highlight_memories JSONB,
  created_at         TIMESTAMPTZ NOT NULL,
  UNIQUE (persona_id, user_id, date)
);
CREATE INDEX IF NOT EXISTS idx_diary_owner ON diary_entry (persona_id, user_id, date);
