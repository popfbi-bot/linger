"""M3-D 日记：数据模型 / 存储 / 生成 / 端点 / 前端接线。

单独成文件（不追加到既有测试）：既有文件末尾无换行符，
追加会制造与功能无关的 EOL 差异。

每条判据都按「先弄坏实现证明它红，再还原验证绿」写。
红→绿证据（判据 5 / 6 / 12）在 PR 描述里，对应本文件的
`test_idempotent_same_day` / `test_no_turns_422` / `test_flow_check_*`。
"""

from __future__ import annotations

import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from _fakes import FakeEmbedder, FakeLLM  # noqa: E402

from linger.diary import DiaryGenerateError, generate_diary  # noqa: E402
from linger.diary.flow import _flow_check  # noqa: E402
from linger.llm.mock import MockProvider  # noqa: E402
from linger.models import AffectState, DiaryEntry, PAD, Turn, now_utc  # noqa: E402
from linger.persona.templates import load_template  # noqa: E402
from linger.server.app import create_app  # noqa: E402
from linger.storage.base import Repository  # noqa: E402
from linger.storage.sqlite import SQLiteRepository  # noqa: E402

STATIC = Path(__file__).resolve().parent.parent / "src" / "linger" / "server" / "static"

GOOD_DIARY = (
    "灯关了一半，我还坐着。今天心里像压了一层潮气，说不上难过，"
    "更像是被谁轻轻按住，动不了，也不太想动。"
    "有些人是把疲惫咽下去的，声音还稳着，我却在旁边听得心里发紧。"
    "我能做的太少了，可我还是想再多陪一会儿。"
    "窗外没什么声音，屋子里的灯也只开了一半，我就那样坐着，让时间慢慢过去。"
)

BAD_DIARY = (
    "今天用户说了工作压力大，然后我说了安慰的话，接着他说谢谢我，"
    "首先我问他具体怎么了，然后他讲了很久，我也说了很多。"
)


# --------------------------------------------------------------------------- #
# 夹具
# --------------------------------------------------------------------------- #


@pytest.fixture
def diary_client(repo, persona):
    """带 linxi 人格的客户端。FakeLLM 的回复是合格日记（≥60 字、无流水账特征）。"""
    app = create_app(repo=repo, llm=FakeLLM(reply=GOOD_DIARY), embedder=FakeEmbedder())
    with TestClient(app) as c:
        c.persona_id = persona.id
        yield c


@pytest.fixture
def chatted(diary_client):
    """开一个会话并聊一轮（有 turn，才能生成日记）。返回 (client, persona_id, user_id)。"""
    pid = diary_client.persona_id
    resp = diary_client.post("/v1/sessions", json={"persona_id": pid, "user_id": "u_a"})
    assert resp.status_code == 201
    sid = resp.json()["session_id"]
    with diary_client.stream(
        "POST", f"/v1/sessions/{sid}/messages", json={"content": "今天面试砸了，很难受。"}
    ) as r:
        assert r.status_code == 200
        for _ in r.iter_lines():
            pass
    return diary_client, pid, "u_a"


def _generate(client, persona_id: str, user_id: str = "u_a", day: date | None = None):
    body = {"user_id": user_id}
    if day is not None:
        body["date"] = day.isoformat()
    return client.post(f"/v1/personas/{persona_id}/diary/generate", json=body)


def _turn(role: str, content: str) -> Turn:
    return Turn(session_id="s_x", role=role, content=content)


# =========================================================================== #
# 判据 1：DiaryEntry 模型字段齐全
# =========================================================================== #


def test_diary_entry_fields_complete():
    fields = set(DiaryEntry.model_fields)
    for name in (
        "id",
        "persona_id",
        "user_id",
        "date",
        "content",
        "mood_label",
        "highlight_memories",
        "created_at",
    ):
        assert name in fields, f"DiaryEntry 缺字段: {name}"


def test_diary_entry_id_has_d_prefix():
    e = DiaryEntry(persona_id="p", user_id="u", date=date(2026, 10, 22))
    assert e.id.startswith("d_")


def test_diary_entry_date_is_plain_date():
    """date 只到天：标准库 datetime.date，不是 datetime。"""
    e = DiaryEntry(persona_id="p", user_id="u", date=date(2026, 10, 22))
    assert isinstance(e.date, date)
    assert not isinstance(e.date, datetime)


def test_diary_entry_defaults():
    e = DiaryEntry(persona_id="p", user_id="u", date=date(2026, 10, 22))
    assert e.mood_label == ""
    assert e.highlight_memories == []
    # 默认工厂：两条日记不能共享同一个 list（经典可变默认值坑）
    other = DiaryEntry(persona_id="p", user_id="u", date=date(2026, 10, 23))
    assert e.highlight_memories is not other.highlight_memories


# =========================================================================== #
# 判据 2：diary_entry 表；三个方法在抽象与实现两侧都有
# =========================================================================== #


def test_diary_table_exists(repo):
    row = repo.conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='diary_entry'"
    ).fetchone()
    assert row is not None, "diary_entry 表不存在"


def test_diary_table_unique_key_is_owner_plus_date(repo):
    """唯一键必须是三元组，不是自增 id（判据 5 的结构性保证）。"""
    sql = repo.conn.execute(
        "SELECT sql FROM sqlite_master WHERE name='diary_entry'"
    ).fetchone()["sql"]
    normalized = " ".join(sql.replace("\n", " ").split())
    assert "UNIQUE (persona_id, user_id, date)" in normalized


@pytest.mark.parametrize("name", ["save_diary", "get_diary", "list_diary"])
def test_repository_abstract_has_diary_methods(name):
    assert hasattr(Repository, name), f"Repository 抽象缺 {name}"
    assert getattr(Repository, name).__isabstractmethod__, f"{name} 不是抽象方法"


@pytest.mark.parametrize("name", ["save_diary", "get_diary", "list_diary"])
def test_sqlite_implements_diary_methods(name):
    impl = getattr(SQLiteRepository, name)
    assert callable(impl)
    assert not getattr(impl, "__isabstractmethod__", False), f"{name} 仍是抽象桩"


def test_sqlite_repository_is_instantiable(repo):
    """抽象方法没实现全，SQLiteRepository 根本实例化不了——这里再确认一次。"""
    assert isinstance(repo, Repository)


def test_abstract_diary_only_repository_cannot_instantiate():
    """只补 diary 三个方法还不够：证明它们确实是 ABC 的一部分。"""

    class Half(Repository):
        def save_diary(self, entry): ...

        def get_diary(self, persona_id, user_id, day): ...

        def list_diary(self, persona_id, user_id, limit=30): ...

    with pytest.raises(TypeError):
        Half()  # noqa: F841  其余抽象方法未实现


def test_save_diary_roundtrip(repo, persona):
    entry = DiaryEntry(
        persona_id=persona.id,
        user_id="u_a",
        date=date(2026, 10, 22),
        content=GOOD_DIARY,
        mood_label="低落",
        highlight_memories=["面试砸了"],
    )
    repo.save_diary(entry)
    got = repo.get_diary(persona.id, "u_a", date(2026, 10, 22))
    assert got is not None
    assert got.content == GOOD_DIARY
    assert got.mood_label == "低落"
    assert got.highlight_memories == ["面试砸了"]


def test_get_diary_missing_returns_none(repo, persona):
    assert repo.get_diary(persona.id, "u_nobody", date(2026, 10, 22)) is None


# =========================================================================== #
# 判据 3：生成成功 → 200，content 含「我」，长度 60~400
# =========================================================================== #


def test_generate_returns_200(chatted):
    client, pid, uid = chatted
    resp = _generate(client, pid, uid)
    assert resp.status_code == 200
    body = resp.json()
    assert "entry" in body and "warnings" in body


def test_generate_content_first_person(chatted):
    client, pid, uid = chatted
    content = _generate(client, pid, uid).json()["entry"]["content"]
    assert "我" in content


def test_generate_content_length_in_range(chatted):
    client, pid, uid = chatted
    content = _generate(client, pid, uid).json()["entry"]["content"]
    assert 60 <= len(content) <= 400, f"长度 {len(content)} 不在 60~400"


def test_generate_entry_fields(chatted):
    client, pid, uid = chatted
    entry = _generate(client, pid, uid).json()["entry"]
    assert entry["persona_id"] == pid
    assert entry["user_id"] == uid
    assert entry["date"] == date.today().isoformat()
    assert entry["id"].startswith("d_")
    assert entry["highlight_memories"] is not None


def test_generate_writes_to_storage(chatted, repo):
    client, pid, uid = chatted
    _generate(client, pid, uid)
    assert repo.get_diary(pid, uid, date.today()) is not None


def test_generate_default_date_is_today(chatted):
    client, pid, uid = chatted
    entry = _generate(client, pid, uid).json()["entry"]
    assert entry["date"] == date.today().isoformat()


# =========================================================================== #
# 判据 4：mood_label 取自当天 affect 的 label（不是空串）
# =========================================================================== #


@pytest.mark.parametrize("label", ["低落", "焦躁", "雀跃", "平静欣慰", "犹豫"])
def test_mood_label_comes_from_affect(chatted, repo, label):
    client, pid, uid = chatted
    repo.save_affect(pid, uid, AffectState(pad=PAD(), label=label, updated_at=now_utc()))
    entry = _generate(client, pid, uid).json()["entry"]
    assert entry["mood_label"] == label


def test_mood_label_never_blank(chatted):
    """没有 affect 记录时也必须是一个具体标签，不能是空串。"""
    client, pid, uid = chatted
    entry = _generate(client, pid, uid).json()["entry"]
    assert entry["mood_label"] != ""


# =========================================================================== #
# 判据 5：幂等 —— 同一天生成两次，条数不变，created_at 刷新
# =========================================================================== #


def test_idempotent_same_day(chatted, repo):
    client, pid, uid = chatted
    first = _generate(client, pid, uid).json()["entry"]
    second = _generate(client, pid, uid).json()["entry"]

    listing = client.get(f"/v1/personas/{pid}/diary", params={"user_id": uid}).json()
    assert listing["count"] == 1, "同一天生成两次变成了两条——唯一键没生效"
    assert len(repo.list_diary(pid, uid)) == 1
    # created_at 必须刷新：重写就是重写
    assert second["created_at"] > first["created_at"]
    # id 不换：日期没变，这篇日记还是同一篇
    assert second["id"] == first["id"]


def test_idempotent_across_instances(repo, persona):
    """两次独立会话（不同进程/请求）同样只有一条。"""
    e1 = DiaryEntry(persona_id=persona.id, user_id="u_a", date=date(2026, 10, 22), content="第一篇")
    e2 = DiaryEntry(persona_id=persona.id, user_id="u_a", date=date(2026, 10, 22), content="第二篇")
    repo.save_diary(e1)
    repo.save_diary(e2)
    assert len(repo.list_diary(persona.id, "u_a")) == 1
    assert repo.get_diary(persona.id, "u_a", date(2026, 10, 22)).content == "第二篇"


def test_same_day_different_users_are_two_entries(repo, persona):
    """幂等的范围是 (persona, user, date)，不是 (persona, date)。"""
    repo.save_diary(
        DiaryEntry(persona_id=persona.id, user_id="u_a", date=date(2026, 10, 22), content="A")
    )
    repo.save_diary(
        DiaryEntry(persona_id=persona.id, user_id="u_b", date=date(2026, 10, 22), content="B")
    )
    assert len(repo.list_diary(persona.id, "u_a")) == 1
    assert len(repo.list_diary(persona.id, "u_b")) == 1


# =========================================================================== #
# 判据 6：当天无对话 → 422
# =========================================================================== #


def test_no_turns_422(diary_client):
    """从没聊过：不许编一篇「今天什么都没发生」。"""
    resp = _generate(diary_client, diary_client.persona_id, "u_silent")
    assert resp.status_code == 422
    assert resp.json()["detail"]


def test_no_turns_422_does_not_write(diary_client, repo):
    pid = diary_client.persona_id
    _generate(diary_client, pid, "u_silent")
    assert repo.list_diary(pid, "u_silent") == []


def test_generate_diary_raises_on_empty_turns(persona):
    with pytest.raises(DiaryGenerateError):
        generate_diary(persona, "u_a", date(2026, 10, 22), [], [], "低落", FakeLLM())


def test_generate_diary_raises_on_blank_turns(persona):
    turns = [_turn("user", "   "), _turn("assistant", "")]
    with pytest.raises(DiaryGenerateError):
        generate_diary(persona, "u_a", date(2026, 10, 22), turns, [], "低落", FakeLLM())


def test_empty_llm_output_is_diary_error(persona):
    """模型什么都没写也是「这次生成不能要」，不是落一篇空日记。"""
    with pytest.raises(DiaryGenerateError):
        generate_diary(
            persona, "u_a", date(2026, 10, 22), [_turn("user", "今天很累")], [], "低落",
            FakeLLM(reply="   \n  "),
        )


def test_other_day_without_turns_422(chatted):
    """今天聊过，但问到没有对话的另一天 → 依然 422。"""
    client, pid, uid = chatted
    resp = _generate(client, pid, uid, day=date(2020, 1, 1))
    assert resp.status_code == 422


# =========================================================================== #
# 判据 7：date 非法 → 422
# =========================================================================== #


@pytest.mark.parametrize("bad", ["2026-13-45", "2026-02-30", "not-a-date", "20261022", "2026-1"])
def test_invalid_date_422(chatted, bad):
    client, pid, uid = chatted
    resp = client.post(
        f"/v1/personas/{pid}/diary/generate", json={"user_id": uid, "date": bad}
    )
    assert resp.status_code == 422, f"{bad} 没有给 422"


def test_missing_user_id_422(chatted):
    client, pid, _ = chatted
    resp = client.post(f"/v1/personas/{pid}/diary/generate", json={})
    assert resp.status_code == 422


def test_blank_user_id_422(chatted):
    client, pid, _ = chatted
    resp = client.post(f"/v1/personas/{pid}/diary/generate", json={"user_id": "  "})
    assert resp.status_code == 422


def test_single_endpoint_invalid_date_422(chatted):
    client, pid, uid = chatted
    resp = client.get(f"/v1/personas/{pid}/diary/2026-13-45", params={"user_id": uid})
    assert resp.status_code == 422


# =========================================================================== #
# 判据 8：persona_id 不存在 → 404
# =========================================================================== #


def test_generate_unknown_persona_404(diary_client):
    assert _generate(diary_client, "p_不存在", "u_a").status_code == 404


def test_list_unknown_persona_404(diary_client):
    resp = diary_client.get("/v1/personas/p_不存在/diary", params={"user_id": "u_a"})
    assert resp.status_code == 404


def test_single_unknown_persona_404(diary_client):
    resp = diary_client.get(
        f"/v1/personas/p_不存在/diary/{date.today().isoformat()}", params={"user_id": "u_a"}
    )
    assert resp.status_code == 404


def test_deleted_persona_404(chatted, repo):
    """软删的人格与不存在同码：日记端点也不能给一个不存在的「她」写日记。"""
    client, pid, uid = chatted
    repo.delete_persona(pid)
    assert _generate(client, pid, uid).status_code == 404


# =========================================================================== #
# 判据 9：单条查询 —— 存在 200 / 不存在 404
# =========================================================================== #


def test_single_existing_date_200(chatted):
    client, pid, uid = chatted
    created = _generate(client, pid, uid).json()["entry"]
    resp = client.get(f"/v1/personas/{pid}/diary/{created['date']}", params={"user_id": uid})
    assert resp.status_code == 200
    assert resp.json()["entry"]["id"] == created["id"]


def test_single_missing_date_404(chatted):
    client, pid, uid = chatted
    _generate(client, pid, uid)
    resp = client.get(f"/v1/personas/{pid}/diary/2019-05-05", params={"user_id": uid})
    assert resp.status_code == 404


def test_single_missing_user_404(chatted):
    client, pid, uid = chatted
    today = _generate(client, pid, uid).json()["entry"]["date"]
    assert client.get(f"/v1/personas/{pid}/diary/{today}", params={"user_id": "u_b"}).status_code == 404


def test_list_empty_is_200(diary_client):
    """「她还没写过」不是错误：列表给空，不 404。"""
    resp = diary_client.get(
        "/v1/personas/%s/diary" % diary_client.persona_id, params={"user_id": "u_fresh"}
    )
    assert resp.status_code == 200
    assert resp.json()["count"] == 0


# =========================================================================== #
# 判据 10：列表按 date 倒序
# =========================================================================== #


def test_list_sorted_by_date_desc(repo, persona):
    for d in (date(2026, 10, 20), date(2022, 1, 3), date(2026, 10, 22), date(2025, 6, 1)):
        repo.save_diary(DiaryEntry(persona_id=persona.id, user_id="u_a", date=d, content="x"))
    dates = [e.date for e in repo.list_diary(persona.id, "u_a")]
    assert dates == sorted(dates, reverse=True)


def test_list_endpoint_sorted_desc(repo, persona):
    app = create_app(repo=repo, llm=FakeLLM(reply=GOOD_DIARY), embedder=FakeEmbedder())
    for d in (date(2024, 3, 3), date(2026, 1, 1), date(2025, 7, 7)):
        repo.save_diary(
            DiaryEntry(persona_id=persona.id, user_id="u_a", date=d, content=GOOD_DIARY)
        )
    with TestClient(app) as c:
        diary = c.get(
            f"/v1/personas/{persona.id}/diary", params={"user_id": "u_a"}
        ).json()["diary"]
    assert [d["date"] for d in diary] == ["2026-01-01", "2025-07-07", "2024-03-03"]


def test_list_respects_limit(repo, persona):
    for i in range(5):
        repo.save_diary(
            DiaryEntry(
                persona_id=persona.id, user_id="u_a", date=date(2026, 1, 1) + timedelta(days=i),
                content="x",
            )
        )
    assert len(repo.list_diary(persona.id, "u_a", limit=2)) == 2


# =========================================================================== #
# 判据 11：用户隔离
# =========================================================================== #


def test_user_isolation_list(chatted):
    client, pid, uid = chatted
    _generate(client, pid, uid)
    other = client.get(f"/v1/personas/{pid}/diary", params={"user_id": "u_b"}).json()
    assert other["count"] == 0


def test_user_isolation_single(chatted):
    client, pid, uid = chatted
    today = _generate(client, pid, uid).json()["entry"]["date"]
    resp = client.get(f"/v1/personas/{pid}/diary/{today}", params={"user_id": "u_b"})
    assert resp.status_code == 404


def test_user_isolation_in_storage(repo, persona):
    repo.save_diary(
        DiaryEntry(persona_id=persona.id, user_id="u_a", date=date(2026, 10, 22), content="A 的心事")
    )
    assert repo.get_diary(persona.id, "u_b", date(2026, 10, 22)) is None
    assert repo.list_diary(persona.id, "u_b") == []


# =========================================================================== #
# 判据 12：流水账检测
# =========================================================================== #


def test_flow_check_flags_transcription():
    assert _flow_check(BAD_DIARY)


def test_flow_check_clean_on_feelings():
    assert _flow_check(GOOD_DIARY) == []


@pytest.mark.parametrize("word", ["用户说"])
def test_flow_check_flags_each_transcribe_pattern(word):
    """「用户说」单独出现就报（第三人称 + 转述）。

    验收修正（原设计错误）：「他说」「我说」**单独**出现不算流水账——
    「他说面试砸了的时候，我心里一沉」是很自然的回忆叙述，
    比硬回避第三人称更好。原 issue 把三者一律算转述，属于误伤。
    同现才报，见下一条。
    """
    text = GOOD_DIARY + f"{word}了一句什么，我心里一沉。"
    assert any("转述" in w for w in _flow_check(text))


def test_flow_check_flags_he_and_i_said_together():
    """「他说」+「我说」同现 = 对话摘要（他说 A、我说 B）。"""
    text = GOOD_DIARY + "他说了一句什么，我说了一句什么，屋里就安静了。"
    assert any("转述" in w for w in _flow_check(text))


def test_flow_check_neither_alone_is_flagged():
    """「他说」/「我说」单独出现不报——正常叙述不是流水账。"""
    for word in ("他说", "我说"):
        text = GOOD_DIARY + f"{word}了一句什么，我心里一沉。"
        assert not any("转述" in w for w in _flow_check(text)), f"误伤：{word}"


@pytest.mark.parametrize("connector", ["然后", "接着", "首先"])
def test_flow_check_flags_stacked_connectors(connector):
    text = GOOD_DIARY + f"{connector}我想了想，{connector}又坐下。"
    assert any("连接词" in w for w in _flow_check(text))


def test_flow_check_single_connector_is_fine():
    """一个连接词不算堆叠：判据是 ≥2 次。"""
    assert _flow_check(GOOD_DIARY + "然后我关灯。") == []


def test_flow_check_flags_third_person():
    assert any("第三人称" in w for w in _flow_check(GOOD_DIARY + "用户走了。"))


def test_flow_check_flags_too_short():
    assert any("太短" in w for w in _flow_check("我很累。"))


def test_flow_check_empty_text():
    assert _flow_check("")


def test_generate_warns_on_bad_llm_output(repo, persona):
    """模型写出流水账：日记照出（不阻断），但 warnings 必须非空。"""
    app = create_app(repo=repo, llm=FakeLLM(reply=BAD_DIARY), embedder=FakeEmbedder())
    with TestClient(app) as c:
        sid = c.post(
            "/v1/sessions", json={"persona_id": persona.id, "user_id": "u_a"}
        ).json()["session_id"]
        with c.stream(
            "POST", f"/v1/sessions/{sid}/messages", json={"content": "今天面试砸了。"}
        ) as r:
            for _ in r.iter_lines():
                pass
        resp = c.post(
            f"/v1/personas/{persona.id}/diary/generate", json={"user_id": "u_a"}
        )
    assert resp.status_code == 200
    assert resp.json()["warnings"], "流水账没有产出 warning"
    assert resp.json()["entry"]["content"]


def test_generate_clean_output_has_no_flow_warnings(repo, persona):
    app = create_app(repo=repo, llm=FakeLLM(reply=GOOD_DIARY), embedder=FakeEmbedder())
    with TestClient(app) as c:
        sid = c.post(
            "/v1/sessions", json={"persona_id": persona.id, "user_id": "u_a"}
        ).json()["session_id"]
        with c.stream(
            "POST", f"/v1/sessions/{sid}/messages", json={"content": "今天面试砸了。"}
        ) as r:
            for _ in r.iter_lines():
                pass
        resp = c.post(f"/v1/personas/{persona.id}/diary/generate", json={"user_id": "u_a"})
    assert resp.json()["warnings"] == []


# =========================================================================== #
# 生成模块细节：highlight_memories / prompt 约束 / 素材来源
# =========================================================================== #


def test_highlight_memories_are_memory_texts(repo, persona):
    from linger.models import MemoryNode

    app = create_app(repo=repo, llm=FakeLLM(reply=GOOD_DIARY), embedder=FakeEmbedder())
    nodes = [
        MemoryNode(persona_id=persona.id, user_id="u_a", content=f"记忆{i}", importance=1 - i * 0.1)
        for i in range(5)
    ]
    repo.add_memories(nodes)
    with TestClient(app) as c:
        sid = c.post(
            "/v1/sessions", json={"persona_id": persona.id, "user_id": "u_a"}
        ).json()["session_id"]
        with c.stream(
            "POST", f"/v1/sessions/{sid}/messages", json={"content": "今天面试砸了。"}
        ) as r:
            for _ in r.iter_lines():
                pass
        entry = c.post(
            f"/v1/personas/{persona.id}/diary/generate", json={"user_id": "u_a"}
        ).json()["entry"]
    assert 1 <= len(entry["highlight_memories"]) <= 3
    # 本意是「highlight 里放的是记忆原文」，不是「必须叫『记忆N』」。
    # 对话本身也会提炼出记忆（如「今天面试砸了」），所以按内容比对更稳。
    stored = {n.content for n in repo.list_memories(persona.id, "u_a")}
    assert all(m in stored for m in entry["highlight_memories"])


def test_highlight_accepts_plain_strings(persona):
    entry, _ = generate_diary(
        persona, "u_a", date(2026, 10, 22), [_turn("user", "今天很累")],
        ["妈妈下周复查", "他在做项目"], "低落", FakeLLM(reply=GOOD_DIARY),
    )
    assert entry.highlight_memories == ["妈妈下周复查", "他在做项目"]


def test_prompt_carries_diary_marker(persona):
    llm = FakeLLM(reply=GOOD_DIARY)
    generate_diary(
        persona, "u_a", date(2026, 10, 22), [_turn("user", "今天很累")], [], "低落", llm
    )
    assert "[LINGER_DIARY]" in llm.prompts[0]


def test_prompt_forbids_summary(persona):
    llm = FakeLLM(reply=GOOD_DIARY)
    generate_diary(
        persona, "u_a", date(2026, 10, 22), [_turn("user", "今天很累")], [], "低落", llm
    )
    system = llm.prompts[0]
    for word in ("第一人称", "只写情绪与感受", "不要写对话摘要", "不复述原话", "用户"):
        assert word in system, f"prompt 缺约束: {word}"


def test_prompt_includes_affect_label(persona):
    llm = FakeLLM(reply=GOOD_DIARY)
    generate_diary(
        persona, "u_a", date(2026, 10, 22), [_turn("user", "今天很累")], [], "焦躁", llm
    )
    assert "焦躁" in llm.prompts[0]


def test_cleaning_strips_fence_and_title(persona):
    raw = "```\n日记：\n" + GOOD_DIARY + "\n```"
    entry, _ = generate_diary(
        persona, "u_a", date(2026, 10, 22), [_turn("user", "今天很累")], [], "低落",
        FakeLLM(reply=raw),
    )
    assert entry.content.startswith("灯关了一半")
    assert "```" not in entry.content


def test_content_capped_at_hard_max(persona):
    entry, _ = generate_diary(
        persona, "u_a", date(2026, 10, 22), [_turn("user", "今天很累")], [], "低落",
        FakeLLM(reply="我很累。" * 400),
    )
    assert len(entry.content) <= 400


# =========================================================================== #
# mock provider：免密钥环境也能写出合格的日记
# =========================================================================== #


def test_mock_provider_diary_is_clean():
    from linger.llm.mock import _MOCK_DIARY

    assert "我" in _MOCK_DIARY
    assert 60 <= len(_MOCK_DIARY) <= 400
    assert _flow_check(_MOCK_DIARY) == []


def test_mock_provider_recognises_diary_marker():
    text = "".join(
        MockProvider().generate(
            "[LINGER_DIARY]\n你是林汐……", [{"role": "user", "content": "写日记"}]
        )
    )
    assert "我" in text
    assert _flow_check(text) == []


def test_mock_provider_chat_branch_unaffected():
    """日记分支不能影响既有对话分支。"""
    text = "".join(
        MockProvider().generate("你是林汐。", [{"role": "user", "content": "今天升职了！"}])
    )
    assert text
    assert "[LINGER_DIARY]" not in text


# =========================================================================== #
# 判据 13：前端接线
# =========================================================================== #


@pytest.fixture(scope="module")
def html() -> str:
    return (STATIC / "index.html").read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def js() -> str:
    return (STATIC / "app.js").read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def css() -> str:
    return (STATIC / "style.css").read_text(encoding="utf-8")


def test_html_has_diary_card(html):
    assert 'id="diary-card"' in html
    assert 'id="diary-list"' in html
    assert 'id="btn-write-diary"' in html
    assert "她的日记" in html


def test_html_diary_below_memories(html):
    """「她记得的事」在「她的日记」之前（右栏顺序：记忆 → 日记）。"""
    assert html.index("她记得的事") < html.index("她的日记")


def test_html_diary_empty_state(html, js):
    assert "还没有日记" in html or "还没有日记" in js


def test_js_calls_three_diary_endpoints(js):
    assert "/diary/generate" in js
    assert "refreshDiary" in js
    assert "renderDiary" in js
    assert "diary?" in js or "/diary`" in js or "/diary?" in js


def test_js_has_write_button_handler(js):
    assert "btnWriteDiary" in js or "btn-write-diary" in js
    assert "写下今天" in js
    assert "正在写…" in js


def test_js_shows_422_detail(js):
    assert "422" in js, "前端没有区分 422"


def test_diary_styles_exist(js, css):
    for cls in ("diary-list", "diary-item", "diary-head", "diary-date", "diary-mood",
                "diary-content", "diary-highlight", "diary-empty"):
        assert cls in js, f"JS 未使用样式类: {cls}"
        assert f".{cls}" in css, f"CSS 缺样式: .{cls}"
    # 新写的那篇要高亮：状态类也必须有样式，否则「高亮」是空话
    assert "is-new" in js
    assert ".diary-item.is-new" in css


def test_diary_endpoints_registered():
    from linger.server.app import app

    paths = {getattr(r, "path", "") for r in app.routes}
    assert "/v1/personas/{persona_id}/diary/generate" in paths
    assert "/v1/personas/{persona_id}/diary" in paths
    assert "/v1/personas/{persona_id}/diary/{day}" in paths


def test_m3a_and_m3c_blocks_untouched(html):
    """不许动 M3-A 生成面板与 M3-C「我的人格」区块。"""
    for el_id in ("persona-create-panel", "btn-generate", "persona-draft", "my-persona-list"):
        assert f'id="{el_id}"' in html
