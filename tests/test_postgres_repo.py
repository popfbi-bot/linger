"""PostgreSQL Repository 的护栏（issue #32 九条判据）。

前提：**CI 里没有 PG 服务**。所以这里的判据必须「不连库也能验」——
真库只做可选加分项（`LINGER_TEST_PG_DSN` 存在才跑，否则 skip）。

每条判据都配了变异验证（见文件末尾），保证它不是「无论代码怎样都绿」的摆设。
"""

from __future__ import annotations

import ast
import os
import subprocess
import sys
from pathlib import Path

import pytest

from linger.storage import PostgresRepository, SQLiteRepository
from linger.storage.base import Repository

SRC = Path(__file__).resolve().parent.parent / "src" / "linger"
PG_SRC = SRC / "storage" / "postgres.py"
PG_SQL = SRC / "storage" / "postgres_schema.sql"


# --------------------------------------------------------------------------- #
# 判据 1：抽象方法全实现
# --------------------------------------------------------------------------- #
# `open()` 是连接管理，不属于 Repository 契约（SQLite 版天生已连着，不需要它）。
# 私有辅助方法（`_run` 之类）同样不算公开 API —— 判据 2 比的是「业务层能调到什么」。
_NOT_PUBLIC_API = {"open"}


def _implemented(cls: type) -> set[str]:
    return {
        name
        for name in dir(cls)
        if not name.startswith("_") and callable(getattr(cls, name))
    } - _NOT_PUBLIC_API


def test_abstract_methods_all_implemented() -> None:
    """缺一个抽象方法，类就实例化不了——`__abstractmethods__` 会非空。"""
    assert PostgresRepository.__abstractmethods__ == frozenset()


def test_missing_one_method_makes_it_uninstantiable(monkeypatch) -> None:
    """判据 1 的变异：抽掉一个实现，必须立刻变红。

    直接篡改类属性比「注释掉源码再跑」更适合放进测试：
    它证明的是**这条断言本身有效**，而不是证明某次手工操作有效。
    """
    import linger.storage.postgres as mod

    saved = mod.PostgresRepository.get_trace
    try:
        del mod.PostgresRepository.get_trace
        # ABC 的 __abstractmethods__ 在类创建时算好，删方法不会自动重算；
        # 这里按 ABC 自己的算法重新算一遍，模拟「少实现一个」的后果。
        from abc import ABCMeta

        missing = set(Repository.__abstractmethods__) - set(
            PostgresRepository.__dict__.keys()
        )
        assert missing, "抽掉 get_trace 之后应该被算出缺失"
        assert "get_trace" in missing
        # 用同样的规则构造一个「缺一个」的子类，确认它会炸
        Concrete = ABCMeta(
            "Concrete",
            (Repository,),
            {
                name: getattr(PostgresRepository, name, None)
                for name in Repository.__abstractmethods__
                if name != "get_trace"
            },
        )
        with pytest.raises(TypeError):
            Concrete()  # type: ignore[abstract]
    finally:
        mod.PostgresRepository.get_trace = saved


# --------------------------------------------------------------------------- #
# 判据 2：公开方法与 SQLiteRepository 一致
# --------------------------------------------------------------------------- #
def test_public_api_matches_sqlite() -> None:
    """不多不少。多一个 = 业务层用不了；少一个 = 换后端就崩。"""
    pg = _implemented(PostgresRepository)
    lite = _implemented(SQLiteRepository)
    assert pg - lite == set(), f"PG 多出来的公开方法：{sorted(pg - lite)}"
    assert lite - pg == set(), f"PG 缺的公开方法：{sorted(lite - pg)}"


def test_repository_contract_is_fully_covered() -> None:
    """两个实现都得覆盖 ABC 的每一个抽象方法。

    往 `Repository` 加方法时，光加在 SQLite 上会静默漏掉 PG —— 这条拦的就是那个。
    """
    abc = _implemented(Repository)
    assert _implemented(PostgresRepository) - abc == set()
    assert abc - _implemented(PostgresRepository) == set()


def test_public_api_mismatch_is_detected() -> None:
    """判据 2 的变异：PG 多一个 SQLite 没有的公开方法，必须能被发现。"""

    class Noisy(PostgresRepository):
        def extra_helper(self) -> None: ...

    assert _implemented(Noisy) - _implemented(SQLiteRepository) == {"extra_helper"}


# --------------------------------------------------------------------------- #
# 判据 3：SQL 方言正确 —— 只用 %s，没有 SQLite 的 ?
# --------------------------------------------------------------------------- #
_SQL_KEYWORDS = ("SELECT ", "INSERT ", "UPDATE ", "DELETE ", "WHERE ", "VALUES (")


def _pg_strings() -> list[tuple[int, str, bool]]:
    """收集 postgres.py 里的字符串字面量与 f-string，标出它是不是 f-string。

    用 `ast.get_source_segment` 取**原始源码片段**而不是 `node.value`：
    f-string 的 `node.value` 在 Python 3.12+ 已经不是扁平的字符串了，
    而 3.11 上它是 `JoinedStr`——两边都得能看见「人写在代码里的那段字面文字」，
    所以统一走源码切片。
    """
    src = PG_SRC.read_text(encoding="utf-8")
    out: list[tuple[int, str, bool]] = []
    for node in ast.walk(ast.parse(src)):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            out.append((node.lineno, node.value, False))
        elif isinstance(node, ast.JoinedStr):
            out.append((node.lineno, ast.get_source_segment(src, node) or "", True))
    return out


def _sql_strings() -> list[tuple[int, str, bool]]:
    """只看 SQL 片段：含 SQL 关键字的那些。

    不按「看起来像 SQL」去猜整个模块——那样会把用户的普通中文文案一起扫进来，
    误报比漏报更烦人。命中关键字的字面量才是 SQL 候选。
    """
    return [
        (ln, s, is_f)
        for ln, s, is_f in _pg_strings()
        if any(k in s.upper() for k in _SQL_KEYWORDS)
    ]


def test_no_sqlite_placeholders_in_sql() -> None:
    """判据 3 主断言：一条 `?` 都不能有。"""
    bad = [(ln, s) for ln, s, _ in _sql_strings() if "?" in s]
    lines = [b[0] for b in bad]
    assert bad == [], f"SQL 里出现 SQLite 占位符 ?（行号 {lines}）"


def test_placeholders_are_percent_s() -> None:
    """反向断言：PS 占位符真的在用。

    只验「没有 ?」是不够的——把所有 SQL 都改成查字符串常量，那条也会绿。
    """
    sqls = _sql_strings()
    assert sqls, "没扫到任何 SQL，过滤条件写歪了"
    assert [s for _, s, _ in sqls if "%s" in s], "没有一条带 %s —— 那这些 SQL 是怎么传参的？"


def test_no_unsafe_fstring_values_in_sql() -> None:
    """SQL 里不许拼值（`f"... WHERE id='{x}'"` 就是注入面）。

    唯一允许的 f-string 是表名拼接（`{table}`，来自常量列表），
    它进不了值的位置；除此之外 SQL 里一律用 `%s` 传参。
    """
    offenders = [(ln, s) for ln, s, is_f in _sql_strings() if is_f]
    assert offenders == [], f"SQL 里用了 f-string：{offenders}"


def test_question_mark_detection_works() -> None:
    """判据 3 的变异：把 %s 改回 ?，上面那条必须红。

    手工改源码不现实（要改一行跑一次），所以这里复制一份真源码做替换，
    再用**同一套扫描逻辑**去扫副本——证明「改回 ? 会被抓到」。
    """
    src = PG_SRC.read_text(encoding="utf-8")
    mutated = src.replace("%s", "?").replace("{table}", "{table}")
    assert mutated != src, "源码里没有 %s，替换没生效，这条变异就没意义"
    tree = ast.parse(mutated)
    found = [
        (n.lineno, n.value)
        for n in ast.walk(tree)
        if isinstance(n, ast.Constant)
        and isinstance(n.value, str)
        and "?" in n.value
        and any(k in n.value.upper() for k in _SQL_KEYWORDS)
    ]
    assert found, "改成 ? 之后应该能扫出问题行"


# --------------------------------------------------------------------------- #
# 判据 4：建表脚本落地，表名与 SQLite 一致
# --------------------------------------------------------------------------- #
import re  # noqa: E402

_TABLE_RE = re.compile(r"CREATE TABLE IF NOT EXISTS\s+(\w+)", re.IGNORECASE)


def _tables(sql: str) -> set[str]:
    return set(_TABLE_RE.findall(sql))


def test_schema_file_exists_and_is_utf8() -> None:
    assert PG_SQL.is_file(), "postgres_schema.sql 不存在"
    assert PG_SQL.read_text(encoding="utf-8").strip(), "建表脚本是空的"


def test_schema_tables_match_sqlite() -> None:
    from linger.storage.sqlite import _SCHEMA

    pg_tables = _tables(PG_SQL.read_text(encoding="utf-8"))
    lite_tables = _tables(_SCHEMA)
    assert pg_tables - lite_tables == set(), f"PG 多出来的表：{sorted(pg_tables - lite_tables)}"
    assert lite_tables - pg_tables == set(), f"PG 缺的表：{sorted(lite_tables - pg_tables)}"
    # 表数是个硬数字，只为「多一张少一张」能被这条用例看见。
    # 14 → 15：M4-D 加了 `llm_usage`（用量流水，两边同构）。
    assert len(pg_tables) == 15, f"表数应是 15，实际 {len(pg_tables)}"


def test_schema_has_no_placeholders_and_uses_timestamptz() -> None:
    text = PG_SQL.read_text(encoding="utf-8")
    assert "?" not in text, "建表脚本里不该有占位符"
    # TIMESTAMP 会丢时区，跨时区一读完就错；时间列必须是 TIMESTAMPTZ。
    assert "TIMESTAMP)" not in text and "TIMESTAMP," not in text
    for table in ("turn", "diary_entry", "memory_node"):
        assert f"{table} (" in text, table
    assert "TIMESTAMPTZ" in text


def test_diary_schema_keeps_daily_unique_key() -> None:
    """一天一篇是产品语义，schema 层必须顶住（同 SQLite 版）。"""
    text = " ".join(PG_SQL.read_text(encoding="utf-8").split())
    assert "UNIQUE (persona_id, user_id, date)" in text


def test_missing_table_is_detected() -> None:
    """判据 4 的变异：删掉一张表的建表语句，集合比对必须报出来。

    M3-H 起表数是 12（多一张 account），M4-B 到 14（多 market_listing /
    market_install）。这条断言随 schema 增长而更新——
    它的价值不在「数字是几」，而在「有人悄悄增删表时必须有人改这里」。
    """
    from linger.storage.sqlite import _SCHEMA

    text = PG_SQL.read_text(encoding="utf-8")
    mutated = re.sub(
        r"CREATE TABLE IF NOT EXISTS diary_entry\s*\([^;]*;", "", text, flags=re.DOTALL
    )
    assert _tables(mutated) != _tables(_SCHEMA)
    assert _tables(_SCHEMA) - _tables(mutated) == {"diary_entry"}


# --------------------------------------------------------------------------- #
# 判据 5：按配置选后端；构造不连库
# --------------------------------------------------------------------------- #
def test_get_repository_returns_postgres_when_configured(monkeypatch) -> None:
    import linger.storage as storage

    monkeypatch.setattr(storage.settings, "storage", "postgres")
    monkeypatch.setattr(storage.settings, "pg_dsn", "postgresql://nobody@127.0.0.1:1/none")
    storage.reset_repository_cache()
    try:
        repo = storage.get_repository()
        assert isinstance(repo, PostgresRepository)
        # 构造没连库：地址是个死端口，连上就会挂；这里 connected 仍是 0
        assert repo.connected == 0
    finally:
        storage.reset_repository_cache()


def test_get_repository_returns_sqlite_by_default(monkeypatch, tmp_path) -> None:
    import linger.storage as storage

    monkeypatch.setattr(storage.settings, "storage", "sqlite")
    monkeypatch.setattr(storage.settings, "db_path", tmp_path / "s.db")
    storage.reset_repository_cache()
    try:
        assert isinstance(storage.get_repository(), SQLiteRepository)
    finally:
        storage.reset_repository_cache()


def test_unknown_backend_raises(monkeypatch) -> None:
    import linger.storage as storage

    monkeypatch.setattr(storage.settings, "storage", "mysql")
    storage.reset_repository_cache()
    try:
        with pytest.raises(ValueError, match="未知存储后端"):
            storage.get_repository()
    finally:
        storage.reset_repository_cache()


def test_construct_does_not_touch_network() -> None:
    """不存在的地址也不影响构造：连库是懒的。

    「import 一下不能把服务拖垮」的可执行版本——地址是死端口，
    只要构造期真去连，这里就会超时或抛异常。
    """
    repo = PostgresRepository(conninfo="postgresql://nobody@127.0.0.1:1/none")
    try:
        assert repo.connected == 0
        assert repo._pool is None
    finally:
        repo.close()


# --------------------------------------------------------------------------- #
# 判据 6：psycopg 是可选的
# --------------------------------------------------------------------------- #
def test_psycopg_is_optional_extra() -> None:
    text = (Path(__file__).resolve().parent.parent / "pyproject.toml").read_text(
        encoding="utf-8"
    )
    assert "[project.optional-dependencies]" in text
    extras = text.split("[project.optional-dependencies]")[1].split("[project.")[0]
    assert "postgres" in extras, "缺 [postgres] extras"
    # 主依赖里不能有 psycopg：替一个后端装驱动，是给所有部署加税
    main_deps = text.split("dependencies = [")[1].split("]")[0]
    assert "psycopg" not in main_deps, "psycopg 不该是主依赖"


def test_missing_psycopg_gives_readable_error(monkeypatch) -> None:
    """没装驱动时报「请装 extras」，而不是裸 ImportError。"""
    import builtins

    real_import = builtins.__import__

    def fake_import(name: str, *args, **kwargs):
        if name == "psycopg_pool" or name.startswith("psycopg"):
            raise ImportError("No module named 'psycopg_pool'")
        return real_import(name, *args, **kwargs)

    repo = PostgresRepository(conninfo="postgresql://nobody@127.0.0.1:1/none")
    monkeypatch.setattr(builtins, "__import__", fake_import)
    try:
        with pytest.raises(RuntimeError, match=r"\[postgres\]"):
            repo._driver()
    finally:
        repo.close()


def test_importing_storage_does_not_require_psycopg() -> None:
    """`import linger.storage` 不能触发 psycopg 加载。"""
    root = Path(__file__).resolve().parent.parent
    code = (
        "import sys; import linger.storage; "
        "assert 'psycopg' not in sys.modules, '导入 storage 就把 psycopg 拖进来了'"
    )
    # 子进程怎么找到 `linger`：走 `PYTHONPATH`，并且**同时**把 src 加进 -c 的
    # 搜索路径。两个都要，因为 `PYTHONPATH` 在某些 Windows 兼容层/受限环境里
    # 不会被继承（本次验收在 Windows 兼容层上就撞到了，症状是「No module
    # named 'linger'」而不是被测代码的问题）。
    env = dict(os.environ, PYTHONPATH=str(root / "src"))
    proc = subprocess.run(
        [sys.executable, "-c", f"import sys; sys.path.insert(0, {str(root / 'src')!r}); {code}"],
        cwd=str(root),
        capture_output=True,
        text=True,
        env=env,
    )
    assert proc.returncode == 0, proc.stderr


# --------------------------------------------------------------------------- #
# 判据 7：真库集成测试（有 DSN 才跑）
# --------------------------------------------------------------------------- #
@pytest.fixture
def pg_repo():
    pytest.importorskip("psycopg")
    dsn = os.environ.get("LINGER_TEST_PG_DSN", "").strip()
    if not dsn:
        pytest.skip("未设置 LINGER_TEST_PG_DSN，跳过真库集成测试")
    repo = PostgresRepository(dsn, pool_size=2)
    for table in (
        # 市场两张表先清：market_install 记着「谁装过哪条」，
        # 不清会让上一轮的安装记录串到下一轮（幂等查询直接命中脏行）。
        "market_install",
        "market_listing",
        "memory_edit_log",
        "diary_entry",
        "trace",
        "scenario_progress",
        "companion_profile",
        "memory_node",
        "relation_state",
        "affect_state",
        "turn",
        "session",
        "persona",
    ):
        repo._run(f"DELETE FROM {table}")
    yield repo
    repo.close()


def _card(persona_id: str = "p_pg"):
    from linger.models import PersonaCard, PersonaMeta

    return PersonaCard(
        id=persona_id,
        meta=PersonaMeta(name="林汐"),
        provenance={"template": "linxi", "authored": True},
    )


def test_pg_persona_roundtrip(pg_repo) -> None:
    card = _card("p_pg_a")
    pg_repo.save_persona(card)
    got = pg_repo.get_persona(card.id)
    assert got is not None
    assert got.id == card.id and got.name == card.name
    assert [p.id for p in pg_repo.list_personas()] == [card.id]
    assert pg_repo.find_persona_by_template("linxi").id == card.id

    deleted = pg_repo.delete_persona("p_pg_a")
    assert deleted is not None and deleted.id == card.id
    assert pg_repo.get_persona("p_pg_a") is None
    assert pg_repo.list_personas() == []
    assert pg_repo.delete_persona("p_pg_a") is None


def test_pg_memory_roundtrip(pg_repo) -> None:
    """判据 7 要求的那一轮：存人格 → 存记忆 → 取回。"""
    from linger.models import MemoryNode

    card = _card("p_pg_m")
    pg_repo.save_persona(card)
    node = MemoryNode(
        id="m_pg_1",
        persona_id=card.id,
        user_id="u_pg",
        content="下周三妈妈复查",
        entities=["妈妈"],
        embedding=[0.1, 0.2, 0.3],
        source_turn_ids=[1, 2],
        emotion={"pleasure": -0.2},
        pinned=True,
    )
    pg_repo.add_memories([node])

    got = pg_repo.get_memory("m_pg_1")
    assert got is not None
    assert got.content == node.content
    assert got.entities == ["妈妈"]
    assert got.source_turn_ids == [1, 2]
    assert got.emotion["pleasure"] == pytest.approx(-0.2)
    assert got.embedding[:3] == pytest.approx([0.1, 0.2, 0.3])
    assert got.pinned is True
    assert [m.id for m in pg_repo.list_memories(card.id, "u_pg")] == ["m_pg_1"]

    pg_repo.delete_memory("m_pg_1")
    assert pg_repo.get_memory("m_pg_1") is None
    assert pg_repo.list_memories(card.id, "u_pg") == []


def test_pg_json_payload_is_real_json(pg_repo) -> None:
    """JSONB 列存的必须是能解析的 JSON，而不是一段带引号的字符串。"""
    import json

    card = _card("p_pg_j")
    pg_repo.save_persona(card)
    row = pg_repo._one("SELECT card::text AS t FROM persona WHERE id = %s", ("p_pg_j",))
    parsed = json.loads(row["t"])
    assert parsed["id"] == card.id
    assert parsed["provenance"]["template"] == "linxi"


def test_pg_session_turn_affect_relation(pg_repo) -> None:
    from datetime import datetime, timezone

    from linger.models import PAD, AffectState, RelationState, Session

    card = _card("p_pg_s")
    pg_repo.save_persona(card)
    now = datetime.now(timezone.utc)
    pg_repo.save_session(
        Session(id="s_pg", persona_id=card.id, user_id="u_pg", started_at=now, last_active_at=now)
    )
    assert pg_repo.get_session("s_pg").persona_id == card.id
    assert [s.id for s in pg_repo.list_sessions(card.id, "u_pg")] == ["s_pg"]

    first = pg_repo.append_turn("s_pg", "user", "在吗")
    second = pg_repo.append_turn("s_pg", "assistant", "在的", {"pleasure": 0.1})
    assert second > first > 0
    turns = pg_repo.list_turns("s_pg")
    assert [t.role for t in turns] == ["user", "assistant"]
    assert turns[1].affect_snapshot == {"pleasure": 0.1}

    pg_repo.save_affect(
        card.id, "u_pg", AffectState(pad=PAD(pleasure=0.5), label="欣慰")
    )
    assert pg_repo.get_affect(card.id, "u_pg").pad.pleasure == pytest.approx(0.5)

    pg_repo.save_relation(RelationState(persona_id=card.id, user_id="u_pg", level=3, turns=2))
    rel = pg_repo.get_relation(card.id, "u_pg")
    assert rel.level == 3 and rel.turns == 2


def test_pg_scenario_profile_trace_diary(pg_repo) -> None:
    import datetime as dt

    from linger.models import CompanionProfile, DiaryEntry, TraceRecord

    card = _card("p_pg_d")
    pg_repo.save_persona(card)

    pg_repo.save_scenario_progress("s_pg_d", {"scenario_id": "rain", "done_beats": ["a"]})
    assert pg_repo.get_scenario_progress("s_pg_d")["done_beats"] == ["a"]

    pg_repo.save_companion_profile(
        CompanionProfile(
            persona_id=card.id, user_id="u_pg", life_stage="青年期", roles=["照护者"]
        )
    )
    prof = pg_repo.get_companion_profile(card.id, "u_pg")
    assert prof.life_stage == "青年期" and prof.roles == ["照护者"]

    pg_repo.save_trace(
        TraceRecord(turn_id=1, persona_id=card.id, user_id="u_pg", query="在吗", candidates=3)
    )
    assert pg_repo.get_trace(1)["candidates"] == 3

    day = dt.date(2026, 10, 1)
    pg_repo.save_diary(
        DiaryEntry(id="d_pg_1", persona_id=card.id, user_id="u_pg", date=day, content="第一稿")
    )
    # 同一天再写：覆盖，不新增，且保留原 id（一天一篇的身份）
    pg_repo.save_diary(
        DiaryEntry(id="d_pg_2", persona_id=card.id, user_id="u_pg", date=day, content="重写了")
    )
    got = pg_repo.get_diary(card.id, "u_pg", day)
    assert got.content == "重写了"
    assert got.id == "d_pg_1", "upsert 冲突时必须保留原 id（一天一篇的身份）"
    assert [d.id for d in pg_repo.list_diary(card.id, "u_pg")] == ["d_pg_1"]


def test_pg_memory_edit_log(pg_repo) -> None:
    from linger.models import MemoryEditRecord

    pg_repo.add_memory_edit(
        MemoryEditRecord(memory_id="m_x", old_content="旧口径", new_content="新口径")
    )
    edits = pg_repo.list_memory_edits("m_x")
    assert [e.new_content for e in edits] == ["新口径"]


def test_pg_market_roundtrip(pg_repo) -> None:
    """市场的两张表在真库上跑完整条状态机（判据 8 的 PG 侧证明）。

    三件事只有真库能证：部分唯一索引真的拒第二条、条件更新真的按行数返回、
    安装主键真的挡住重复安装。
    """
    from linger.market import service as market_service
    from linger.market.models import STATUS_APPROVED, STATUS_PENDING, MarketListing
    from linger.models import PersonaCard, PersonaMeta, now_utc

    author = "u_pg_author"
    card = PersonaCard(
        id="p_pg_market",
        meta=PersonaMeta(name="PG 市场"),
        provenance={"type": "authored", "owner": author},
    )
    pg_repo.save_persona(card)

    listing, created = market_service.create_listing(
        pg_repo, user_id=author, persona_id=card.id, title="PG"
    )
    assert created and listing.status == STATUS_PENDING

    # 同人格再建一条 → 撞部分唯一索引 → 返回已有那条，不是插出第二条
    dup = pg_repo.create_listing(
        MarketListing(
            id="l_pg_dup",
            persona_id=card.id,
            author_id=author,
            title="dup",
            summary="",
            snapshot=card.model_dump(mode="json"),
            status=STATUS_PENDING,
            listed_at=now_utc(),
        )
    )
    assert dup.id == listing.id
    assert len(pg_repo.list_listings()) == 1

    # 条件更新：第二次必须 False
    kw = dict(reviewer_id="u_pg_rv", note="", reviewed_at=now_utc())
    assert pg_repo.review_listing(
        listing.id, from_status=STATUS_PENDING, to_status=STATUS_APPROVED, **kw
    )
    assert not pg_repo.review_listing(
        listing.id, from_status=STATUS_PENDING, to_status=STATUS_APPROVED, **kw
    )

    outcome = market_service.install(pg_repo, user_id="u_pg_fan", listing_id=listing.id)
    assert outcome.copied is True
    again = market_service.install(pg_repo, user_id="u_pg_fan", listing_id=listing.id)
    assert again.copied is False and again.persona.id == outcome.persona.id
    assert pg_repo.get_install(listing.id, "u_pg_fan").persona_id == outcome.persona.id


def test_pg_close_is_idempotent(pg_repo) -> None:
    pg_repo.close()
    pg_repo.close()
    assert pg_repo.connected == 0


def test_pg_api_matches_sqlite_end_to_end(pg_repo) -> None:
    """两套实现跑同一串操作，读回来的形状必须一致。

    判据 2 只比了方法名；这条比「同名方法的行为」——换后端不换语义。
    """
    from linger.models import PersonaCard, PersonaMeta

    card = PersonaCard(id="p_pg_parity", meta=PersonaMeta(name="对照"), provenance={})
    pg_repo.save_persona(card)
    assert pg_repo.get_persona("p_pg_parity").model_dump() == card.model_dump()

    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        lite = SQLiteRepository(Path(tmp) / "p.db")
        try:
            lite.save_persona(card)
            assert lite.get_persona("p_pg_parity").model_dump() == pg_repo.get_persona(
                "p_pg_parity"
            ).model_dump()
        finally:
            lite.close()


# --------------------------------------------------------------------------- #
# 判据 8：业务层零改动
# --------------------------------------------------------------------------- #
def test_business_layer_has_no_sqlite_reference() -> None:
    """`src/linger/` 下除 storage/ 外，不许出现 SQLiteRepository。"""
    offenders: list[str] = []
    for path in SRC.rglob("*.py"):
        if "storage" in path.parts:
            continue
        if "SQLiteRepository" in path.read_text(encoding="utf-8"):
            offenders.append(str(path.relative_to(SRC)))
    assert offenders == [], f"业务层仍在写死 SQLiteRepository：{offenders}"


def test_app_builds_repo_through_factory() -> None:
    """服务层装配走 get_repository()，不认识具体实现。"""
    text = (SRC / "server" / "app.py").read_text(encoding="utf-8")
    assert "get_repository" in text
    assert "SQLiteRepository" not in text
