"""用户自建场景（M3-F）。

产品原来只能从内置 YAML 选场景——用户没法自己写。这个模块补上：
`POST /v1/scenarios` 把用户填的场景落成 YAML，`loader` 同时扫
「用户目录 → 内置目录」，同 id 时用户版本覆盖内置版本。

几个刻意的设计，测试逐一钉住：
    1. 写用户目录而不是包目录（pip install 后包目录只读）
    2. 节拍允许只写一句话（自动编号 b1/b2）
    3. 重复 id 返回 409 而不是静默覆盖
    4. 内置场景删不掉（返回 404）
    5. **写出的文件必须是 LF**——`open(..., "w")` 在 Windows 会把整个文件
       翻成 CRLF，让 YAML 在 git 里显示为整体变更（本窗口已踩三次）
"""

from __future__ import annotations

import pytest

# 夹具 repo / persona / client 见 conftest.py
# scenario_dir 也在 conftest.py（autouse，M3-G 测试隔离修复）——这里不再重复定义，
# 否则本文件的局部定义会覆盖全局的，其它文件依旧不隔离。

BODY = {
    "id": "rainy_roof",
    "title": "雨夜天台",
    "world": "现代都市",
    "setting": "深夜，写字楼天台，下着雨。她躲在檐下抽一根没点的烟。",
    "opening_narration": "雨声盖住了城市的噪音。她抬头看了你一眼，没说话。",
    "user_role": "同楼加班的同事",
    "beats": [
        "你们第一次真正说上话",
        {"goal": "她提起辞职的念头", "trigger": ["辞职", "不想干了"]},
    ],
}


# --------------------------------------------------------------------------- #
# 1. 创建
# --------------------------------------------------------------------------- #
def test_create_returns_201(client):
    resp = client.post("/v1/scenarios", json=BODY)
    assert resp.status_code == 201
    data = resp.json()
    assert data["id"] == "rainy_roof"
    assert data["title"] == "雨夜天台"
    assert data["opening_narration"].startswith("雨声")


def test_beats_get_auto_ids(client):
    """只写一句话的节拍也要能存，自动编号 b1/b2。"""
    data = client.post("/v1/scenarios", json=BODY).json()
    assert [b["id"] for b in data["beats"]] == ["b1", "b2"]
    assert data["beats"][0]["goal"] == "你们第一次真正说上话"
    assert data["beats"][1]["trigger"] == ["辞职", "不想干了"]


def test_created_scenario_appears_in_list(client):
    before = len(client.get("/v1/scenarios").json()["scenarios"])
    client.post("/v1/scenarios", json=BODY)
    after = client.get("/v1/scenarios").json()["scenarios"]
    assert len(after) == before + 1
    assert any(s["id"] == "rainy_roof" for s in after)


def test_created_scenario_usable_in_session(client, persona):
    """造完就能拿它开会话，不用重启服务。"""
    client.post("/v1/scenarios", json=BODY)
    resp = client.post(
        "/v1/sessions",
        json={"persona_id": persona.id, "user_id": "u_a", "scenario_id": "rainy_roof"},
    )
    assert resp.status_code == 201
    assert resp.json()["scenario"]["scenario_id"] == "rainy_roof"


def test_written_file_is_lf_not_crlf(client, scenario_dir):
    """YAML 必须是 LF。CRLF 会让整个文件在 git 里显示为变更。"""
    client.post("/v1/scenarios", json=BODY)
    path = scenario_dir / "rainy_roof.yaml"
    assert path.exists()
    raw = path.read_bytes()
    assert b"\r\n" not in raw, "写出的 YAML 含 CRLF——open() 少了 newline='\\n'"
    assert raw.endswith(b"\n"), "文件应以换行结尾"


# --------------------------------------------------------------------------- #
# 2. 冲突与覆盖
# --------------------------------------------------------------------------- #
def test_duplicate_id_returns_409(client):
    assert client.post("/v1/scenarios", json=BODY).status_code == 201
    resp = client.post("/v1/scenarios", json=BODY)
    assert resp.status_code == 409


def test_overwrite_flag_allows_update(client):
    client.post("/v1/scenarios", json=BODY)
    changed = {**BODY, "title": "雨夜天台（改）", "overwrite": True}
    resp = client.post("/v1/scenarios", json=changed)
    assert resp.status_code == 201
    assert resp.json()["title"] == "雨夜天台（改）"


# --------------------------------------------------------------------------- #
# 3. 校验（都该 422）
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "patch",
    [
        {"id": "Bad-ID"},          # 大写与连字符
        {"id": "1abc"},            # 数字开头
        {"id": "x"},               # 太短
        {"title": "   "},          # 空白标题
        {"beats": [{"goal": ""}]},  # 空节拍描述
        {"beats": [{"goal": "a", "id": "b1"}, {"goal": "b", "id": "b1"}]},  # 节拍 id 重复
        {"beats": "不是数组"},
    ],
)
def test_invalid_payload_returns_422(client, patch):
    body = {**BODY, "id": "valid_id", **patch}
    assert client.post("/v1/scenarios", json=body).status_code == 422


# --------------------------------------------------------------------------- #
# 4. 删除
# --------------------------------------------------------------------------- #
def test_delete_user_scenario(client):
    client.post("/v1/scenarios", json=BODY)
    assert client.delete("/v1/scenarios/rainy_roof").status_code == 200
    assert client.delete("/v1/scenarios/rainy_roof").status_code == 404


def test_builtin_scenario_cannot_be_deleted(client):
    """内置场景是产品资产，不该被运行时删掉。"""
    builtin = [s for s in client.get("/v1/scenarios").json()["scenarios"]]
    assert builtin, "应该有内置场景"
    # 造一个用户场景后，内置的仍在；删内置的应 404
    assert client.delete(f"/v1/scenarios/{builtin[0]['id']}").status_code == 404


# --------------------------------------------------------------------------- #
# 5. 影子覆盖机制（读取侧）：用户目录的同 id 文件优先，列表只出现一次
# --------------------------------------------------------------------------- #
def test_user_scenario_overrides_builtin_same_id(client, scenario_dir):
    """同 id 时用户版本优先——用户改过的场景应该生效，且列表里只出现一次。

    M3-G 语义变更说明（本窗口授权改这条断言）：
    旧版用 `POST + overwrite: true` 造影子——那正是 M3-G 禁止的
    「接口主动覆盖内置场景」。**读取侧的影子机制原样保留**：
    手工把同 id 文件放进用户目录（历史遗留 / 手改 / git 同步），
    loader 读用户版。接口不造影子，机制不消失——两条都要有人钉。
    """
    builtin = client.get("/v1/scenarios").json()["scenarios"]
    assert builtin
    target_id = builtin[0]["id"]

    # 绕过 POST，直接写文件（这就是「手工影子」的形态）
    scenario_dir.mkdir(parents=True, exist_ok=True)
    (scenario_dir / f"{target_id}.yaml").write_text(
        f"id: {target_id}\ntitle: 手工影子版\nsetting: 测试\n", encoding="utf-8"
    )

    listed = [s for s in client.get("/v1/scenarios").json()["scenarios"] if s["id"] == target_id]
    assert len(listed) == 1, "同 id 在列表里只能出现一次"
    assert listed[0]["title"] == "手工影子版"
    assert listed[0]["source"] == "user", "躺在用户目录里，来源就是 user"

    detail = client.get(f"/v1/scenarios/{target_id}").json()
    assert detail["title"] == "手工影子版", "详情也必须读到用户版"