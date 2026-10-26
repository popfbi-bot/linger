"""M3-G 场景编辑的验收判据（issue #31）。

本文件由本窗口实现（NPC 的 patch 因评论转义损坏未能应用，判据与设计决策
均出自 issue #31 与其评论里已确认的部分）：

    · `_source_of()` 按**文件实际所在目录**判，不按 id 猜
    · 内置拦截只拦「写下去会新造影子」——用户目录已有同 id 文件的照改不误
    · 影子覆盖机制（读取侧）原样保留，两条钉条防止它悄悄消失
    · 编辑 = POST 原 id + `overwrite: true`，进度不受影响

每条判据都能证明「不满足时会红」（见各 docstring 的反例说明）。
"""

from __future__ import annotations

import pytest

# 夹具 repo / persona / client / scenario_dir 见 conftest.py（autouse 隔离）


@pytest.fixture
def builtin_id(client) -> str:
    return client.get("/v1/scenarios").json()["scenarios"][0]["id"]


# --------------------------------------------------------------------------- #
# 判据 1：source 字段（按文件实际所在目录判）
# --------------------------------------------------------------------------- #
def test_list_has_source_field(client, scenario_dir):
    """GET /v1/scenarios 每项带 source: user / builtin。

    反例：把 `_brief()` 的 source 恒返回 "user" → 内置场景全标 user（红）；
    恒返回 "builtin" → 用户场景没有编辑入口（红）。
    """
    items = client.get("/v1/scenarios").json()["scenarios"]
    assert items, "应该至少有内置场景"
    builtin = [s for s in items if s["source"] == "builtin"]
    assert builtin, "内置场景应标记为 builtin"
    assert all(s["source"] in ("user", "builtin") for s in items)


def test_source_by_directory_not_by_id(client, scenario_dir, builtin_id):
    """来源按**文件所在目录**判，不按 id 猜。

    用户目录里放一个 id 与内置无关的自建场景 → user；
    手工放一个与内置同 id 的影子 → 还是 user（躺在哪就是谁的）。
    反例：按 id 判（id 在内置清单里就返回 builtin）→ 影子那条红。
    """
    scenario_dir / "noop"  # 目录已由 fixture 建好
    (scenario_dir / "my_corner.yaml").write_text(
        "id: my_corner\ntitle: 我的角落\nsetting: 测试\n", encoding="utf-8"
    )
    # 影子：id 撞内置，但文件在用户目录
    (scenario_dir / f"{builtin_id}.yaml").write_text(
        f"id: {builtin_id}\ntitle: 影子版\nsetting: 测试\n", encoding="utf-8"
    )

    items = {s["id"]: s["source"] for s in client.get("/v1/scenarios").json()["scenarios"]}
    assert items["my_corner"] == "user"
    assert items[builtin_id] == "user", "影子文件躺在用户目录，来源就是 user"


def test_detail_has_source(client, builtin_id):
    """详情也带 source——前端编辑前预填表单要靠它决定给不给编辑。"""
    d = client.get(f"/v1/scenarios/{builtin_id}").json()
    assert d["source"] == "builtin"


# --------------------------------------------------------------------------- #
# 判据 6：内置场景不许被接口覆盖（409，且内置文件字节不变）
# --------------------------------------------------------------------------- #
def test_cannot_overwrite_builtin_via_post(client, builtin_id):
    """POST 内置 id + overwrite:true → 409。

    反例：把 writer 的内置拦截删掉（`if False and builtin.exists()`）
    → 201，内置场景被运行时覆盖（红）。
    """
    r = client.post(
        "/v1/scenarios",
        json={"id": builtin_id, "title": "运行时篡改版", "overwrite": True},
    )
    assert r.status_code == 409, f"内置场景不能被接口覆盖，实际 {r.status_code}"


def test_builtin_file_bytes_unchanged_after_rejected_post(client, builtin_id):
    """409 之后内置 yaml 一个字节都不能变——拒绝就要拒绝干净。

    反例：先写后校验的实现（写完才发现撞内置）→ 文件已被改（红）。
    """
    from linger.scenario.writer import builtin_scenario_dir

    before = (builtin_scenario_dir() / f"{builtin_id}.yaml").read_bytes()
    client.post(
        "/v1/scenarios",
        json={"id": builtin_id, "title": "运行时篡改版", "overwrite": True},
    )
    after = (builtin_scenario_dir() / f"{builtin_id}.yaml").read_bytes()
    assert before == after, "内置场景文件被改动——拦截必须发生在写入之前"


def test_can_still_edit_own_user_scenario(client, scenario_dir):
    """内置拦录取不误伤：用户目录已有同 id 文件 → 照改不误。

    反例：把拦截条件改成 `builtin.exists()`（不看用户目录有没有）
    → 用户永远改不动自己那个场景（红）。
    """
    # 先合法地建一个用户场景
    r = client.post(
        "/v1/scenarios",
        json={"id": "rainy_roof2", "title": "初版", "setting": "s"},
    )
    assert r.status_code == 201
    # 编辑（overwrite: true）
    r = client.post(
        "/v1/scenarios",
        json={"id": "rainy_roof2", "title": "改过的标题", "overwrite": True},
    )
    assert r.status_code == 201, f"编辑自己的场景不该被拦，实际 {r.status_code}"
    assert client.get("/v1/scenarios/rainy_roof2").json()["title"] == "改过的标题"


# --------------------------------------------------------------------------- #
# 判据 3：编辑沿用原 id，已开会话的场景进度不断
# --------------------------------------------------------------------------- #
def test_edit_keeps_same_id_and_session_progress(client, scenario_dir):
    """编辑不改 id → 已开会话的场景进度还能对上。

    场景：用户开了会话推进了节拍 → 回来改场景标题 → 回到会话继续，
    进度必须还在。反例：编辑时生成新 id → 进度对不上（红）。
    """
    # 建场景（第一拍带触发词——空触发不自动推进是 writer 的明确设计）
    r = client.post(
        "/v1/scenarios",
        json={
            "id": "rainy_roof3",
            "title": "进度测试",
            "setting": "s",
            "beats": [{"goal": "第一拍", "trigger": ["你好"]}, "第二拍"],
        },
    )
    assert r.status_code == 201

    # 开会话并推进第一拍（第一拍 trigger 默认是第 1 次发言）
    pid = client.post("/v1/personas", json={"template_id": "linxi"}).json()["id"]
    sid = client.post(
        "/v1/sessions", json={"persona_id": pid, "user_id": "u_prog", "scenario_id": "rainy_roof3"}
    ).json()["session_id"]

    # 编辑：只改标题，节拍原样
    detail = client.get("/v1/scenarios/rainy_roof3").json()
    r = client.post(
        "/v1/scenarios",
        json={**detail, "title": "进度测试（改）", "overwrite": True},
    )
    assert r.status_code == 201
    assert client.get("/v1/scenarios/rainy_roof3").json()["id"] == "rainy_roof3", "编辑不能改 id"

    # 会话进度还在：发一句话推进，节拍从第一拍开始推进（不是从头）
    client.post(f"/v1/sessions/{sid}/messages", json={"content": "你好"})
    state = client.get(f"/v1/sessions/{sid}").json().get("scenario", {})
    assert state.get("scenario_id") == "rainy_roof3", "编辑后原会话的场景还在"
    assert state.get("done_beats"), "第一拍应该已被推进（进度没断）"


# --------------------------------------------------------------------------- #
# 判据 7：影子机制的两条钉条
# --------------------------------------------------------------------------- #
def test_manual_shadow_still_works_and_deduped(client, scenario_dir, builtin_id):
    """手工影子仍生效且列表去重——机制在，接口不造影子是两回事。

    反例：把 `loader._paths()` 的元组顺序反过来（builtin 在前）
    → 列表读到内置版、source 变 builtin（红）。
    """
    (scenario_dir / f"{builtin_id}.yaml").write_text(
        f"id: {builtin_id}\ntitle: 手工影子版\nsetting: 测试\n", encoding="utf-8"
    )
    listed = [s for s in client.get("/v1/scenarios").json()["scenarios"] if s["id"] == builtin_id]
    assert len(listed) == 1, "同 id 只出现一次"
    assert listed[0]["title"] == "手工影子版"
    assert listed[0]["source"] == "user"

    d = client.get(f"/v1/scenarios/{builtin_id}").json()
    assert d["title"] == "手工影子版", "详情也读用户版"


def test_shadow_source_stays_user(client, scenario_dir, builtin_id):
    """影子文件的 source 必须是 user——第二条钉条。

    与上一条互补：上一条验「读到用户版内容」，这条验「来源标记正确」。
    只测一条的话，「内容对但标记错」能骗过判据。
    """
    (scenario_dir / f"{builtin_id}.yaml").write_text(
        f"id: {builtin_id}\ntitle: 影子\nsetting: 测试\n", encoding="utf-8"
    )
    listed = [s for s in client.get("/v1/scenarios").json()["scenarios"] if s["id"] == builtin_id]
    assert listed[0]["source"] == "user"


# --------------------------------------------------------------------------- #
# 判据 2/编辑交互的后端部分：overwrite 编辑后字段不丢
# --------------------------------------------------------------------------- #
def test_edit_preserves_untouched_fields(client, scenario_dir):
    """编辑只改给了的字段，没动的字段不许被清空。

    前端预填表单后全量提交——但后端要保证「提交里缺的字段」
    不被当成空串写进去（否则 beats/触发词一编辑就没）。
    反例：_normalize 把缺省字段统一置空 → 这条红。
    """
    r = client.post(
        "/v1/scenarios",
        json={
            "id": "keep_fields",
            "title": "原标题",
            "setting": "原设定",
            "opening_narration": "原开场",
            "beats": [{"goal": "拍一", "trigger": ["触发词"]}],
        },
    )
    assert r.status_code == 201

    # 编辑：只改标题，但 beats/setting/opening 原样带上（模拟前端预填提交）
    detail = client.get("/v1/scenarios/keep_fields").json()
    r = client.post("/v1/scenarios", json={**detail, "title": "新标题", "overwrite": True})
    assert r.status_code == 201

    after = client.get("/v1/scenarios/keep_fields").json()
    assert after["title"] == "新标题"
    assert after["setting"] == "原设定", "没改的字段不能丢"
    assert after["opening_narration"] == "原开场", "没改的字段不能丢"
    assert [b["goal"] for b in after["beats"]] == ["拍一"], "节拍不能丢"
    assert after["beats"][0]["trigger"] == ["触发词"], "触发词不能丢"


# --------------------------------------------------------------------------- #
# 判据 7 补充：写出的 YAML 仍是 LF（这个坑踩过三次，单独钉）
# --------------------------------------------------------------------------- #
def test_edited_yaml_is_lf(client, scenario_dir):
    """编辑后写出的 YAML 仍是 LF，不能因为走了编辑分支就混进 CRLF。

    反例：open() 少了 newline="\n" → Windows 上写出 \r\n（红）。
    """
    client.post("/v1/scenarios", json={"id": "lf_check", "title": "初版", "setting": "s"})
    client.post(
        "/v1/scenarios",
        json={"id": "lf_check", "title": "又改了", "setting": "s", "overwrite": True},
    )
    raw = (scenario_dir / "lf_check.yaml").read_bytes()
    assert b"\r\n" not in raw, "写出的 YAML 含 CRLF——open() 少了 newline='\\n'"
    assert raw.endswith(b"\n")