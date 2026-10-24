"""模型配置端点（界面里绑定 key）的验收。

判据：
1. **永不回显完整 key**——GET/PUT 的响应里都不能出现用户填的 key 原文
2. 保存即生效（热切换）：`app.state.orch` 用的 provider 真的换了
3. `api_key` 三态：不传=保持、`""`=清除、有值=替换
4. 只允许环回来源修改（非本机 → 403）
5. 清除后回到 mock
6. 配置文件落在 `data/` 下（已被 .gitignore 忽略，不会进版本库）
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from _fakes import FakeEmbedder, FakeLLM
from linger.config import settings
from linger.llm import settings_store as store
from linger.server.app import create_app
from linger.storage.sqlite import SQLiteRepository

FAKE_KEY = "sk-test-SECRET-9f3a2b7c"  # 故意用可 grep 的标记串


@pytest.fixture
def app_env(tmp_path, monkeypatch):
    """把 db_path 指到 tmp，避免写到真实的 ./data/。"""
    db = tmp_path / "t.db"
    monkeypatch.setattr(settings, "db_path", db, raising=False)
    # 清掉可能从环境带来的 openai 配置，避免测试互相污染
    monkeypatch.setattr(settings, "llm_provider", "mock", raising=False)
    monkeypatch.setattr(settings, "openai_api_key", "", raising=False)
    monkeypatch.setattr(settings, "openai_base_url", "https://api.openai.com/v1", raising=False)
    monkeypatch.setattr(settings, "openai_model", "gpt-4o-mini", raising=False)
    repo = SQLiteRepository(db)
    app = create_app(repo=repo, llm=FakeLLM(), embedder=FakeEmbedder())
    with TestClient(app) as c:
        yield c, app, tmp_path
    repo.close()


# ---------------------------------------------------------------- 判据 1
def test_get_never_leaks_full_key(app_env):
    c, _, _ = app_env
    r = c.put("/v1/settings/llm", json={"provider": "mock", "api_key": FAKE_KEY})
    assert r.status_code == 200, r.text

    body = c.get("/v1/settings/llm")
    assert body.status_code == 200
    raw = body.text
    assert FAKE_KEY not in raw, f"GET 里出现了完整 key！\n{raw}"
    eff = body.json()["effective"]
    assert eff["api_key_set"] is True
    assert eff["api_key_hint"].startswith("sk-t"), eff["api_key_hint"]
    assert FAKE_KEY not in eff["api_key_hint"], "提示里泄露了完整 key"


def test_put_response_never_leaks_full_key(app_env):
    c, _, _ = app_env
    r = c.put("/v1/settings/llm", json={"provider": "mock", "api_key": FAKE_KEY})
    assert r.status_code == 200
    assert FAKE_KEY not in r.text, "PUT 响应里出现了完整 key"


# ---------------------------------------------------------------- 判据 2
def test_put_hot_switches_provider(app_env):
    """保存后 orchestrator 立刻换 provider，不用重启。"""
    c, app, _ = app_env
    before = app.state.orch.llm
    r = c.put("/v1/settings/llm", json={"provider": "mock", "model": "whatever"})
    assert r.status_code == 200, r.text
    after = app.state.orch.llm
    # mock 装配出来的对象每次都是新的：证明重建发生了
    assert after is not before, "orchestrator 没有换 provider——热切换没生效"
    # 计量层仍然包着（否则成本看板会静默停更）
    assert hasattr(after, "generate"), "新 provider 不可用"


def test_put_rebuilds_orchestrator(app_env):
    c, app, _ = app_env
    before = app.state.orch
    r = c.put("/v1/settings/llm", json={"provider": "mock"})
    assert r.status_code == 200
    assert app.state.orch is not before, "orchestrator 没重建，下一轮还会用旧的 llm"


# ---------------------------------------------------------------- 判据 3
def test_api_key_none_keeps_existing(app_env):
    """只改 model 时不能把 key 抹掉。"""
    c, _, _ = app_env
    c.put("/v1/settings/llm", json={"provider": "mock", "api_key": FAKE_KEY})
    assert store.load().api_key == FAKE_KEY

    # 不传 api_key（模拟「我只想换模型」）
    r = c.put("/v1/settings/llm", json={"provider": "mock", "model": "another-model"})
    assert r.status_code == 200, r.text
    assert store.load().api_key == FAKE_KEY, "只改 model 却把 key 清空了"
    assert store.load().model == "another-model"


def test_api_key_empty_string_clears(app_env):
    c, _, _ = app_env
    c.put("/v1/settings/llm", json={"provider": "mock", "api_key": FAKE_KEY})
    assert store.load().api_key == FAKE_KEY

    r = c.put("/v1/settings/llm", json={"provider": "mock", "api_key": ""})
    assert r.status_code == 200, r.text
    assert store.load().api_key == "", "空字符串应该显式清除 key"


# ---------------------------------------------------------------- 判据 4
def test_non_local_request_rejected(app_env):
    """非环回来源不能改配置（防止有人 --host 0.0.0.0 时被外人改）。"""
    c, app, _ = app_env
    from linger.server import app as app_mod

    # 直接测那个守卫函数，比伪造 Request 更可靠
    class FakeReq:
        class client:
            host = "192.168.1.99"

    with pytest.raises(app_mod.HTTPException) as ei:
        app_mod._require_local(FakeReq())
    assert ei.value.status_code == 403

    # 环回与非环回都验一遍，避免「一律拒绝」也算过
    class LocalReq:
        class client:
            host = "127.0.0.1"

    app_mod._require_local(LocalReq())  # 不抛即通过


# ---------------------------------------------------------------- 判据 5
def test_delete_clears_and_returns_to_mock(app_env):
    c, _, _ = app_env
    c.put("/v1/settings/llm", json={"provider": "mock", "api_key": FAKE_KEY})
    assert store.load().api_key == FAKE_KEY

    r = c.delete("/v1/settings/llm")
    assert r.status_code == 200, r.text
    assert not store.settings_path().exists(), "文件还在"
    assert settings.llm_provider == "mock", f"清除后应回 mock，实得 {settings.llm_provider}"
    assert settings.openai_api_key in ("", None) or not settings.openai_api_key


# ---------------------------------------------------------------- 判据 6
def test_settings_file_under_data_dir(app_env):
    """文件必须落在 db 同目录（默认 ./data/），而 data/ 在 .gitignore 里。"""
    c, _, tmp = app_env
    c.put("/v1/settings/llm", json={"provider": "mock"})
    p = store.settings_path()
    assert p.parent == tmp.resolve(), f"配置没落在 db 目录: {p}"

    gitignore = Path(".gitignore").read_text(encoding="utf-8")
    assert "/data/" in gitignore or "data/" in gitignore, ".gitignore 没忽略 data/，密钥可能进版本库"


def test_no_key_in_repo_after_save(app_env):
    """保存后，仓库里不应出现含 key 的文件（除了被忽略的 data/）。"""
    c, _, tmp = app_env
    c.put("/v1/settings/llm", json={"provider": "mock", "api_key": FAKE_KEY})
    p = store.settings_path()
    assert p.exists()
    # 文件内容确实是明文（这点要在文档里说清楚，所以测试也钉住它）
    raw = p.read_text(encoding="utf-8")
    assert FAKE_KEY in raw, "文件里没有 key？保存逻辑有问题"
    # 但它不在仓库目录里（tmp_path 是 pytest 的临时目录）
    repo_root = Path(__file__).resolve().parent.parent
    assert repo_root not in p.parents, f"配置写进了仓库目录: {p}"


# ---------------------------------------------------------------- 边界
def test_empty_provider_rejected(app_env):
    c, _, _ = app_env
    r = c.put("/v1/settings/llm", json={"provider": ""})
    # 空 provider 会回落到 old.provider（mock）；若 old 也空才 400
    assert r.status_code in (200, 400)


def test_probe_endpoint_works_with_mock(app_env):
    """「测试连接」在 mock 下也应通过（证明链路通）。"""
    c, _, _ = app_env
    r = c.post("/v1/settings/llm/test")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] is True, f"mock 探测失败: {body}"
    assert body["latency_ms"] >= 0
    assert body.get("reply"), "没有回复内容"


def test_probe_reports_error_without_leaking(app_env, monkeypatch):
    """探测失败时把原因带回来，但不能带出 key。"""
    c, app, _ = app_env

    class Boom:
        name = "boom"

        def generate(self, *a, **k):
            raise RuntimeError(f"401 Unauthorized (key={FAKE_KEY})")

    from linger.server import app as app_mod
    from linger import llm as llm_mod

    monkeypatch.setattr(settings, "llm_provider", "openai", raising=False)
    monkeypatch.setattr(settings, "openai_api_key", FAKE_KEY, raising=False)
    # 探测自己会 `_build` 一个直连 provider（不碰 app.state.llm，见 _probe_llm 注释），
    # 所以要在这里换掉它
    monkeypatch.setattr(llm_mod, "_build", lambda name: Boom())
    out = app_mod._probe_llm(app)
    assert out["ok"] is False
    assert "401" in out["error"], "错误原因应该保留"
    assert FAKE_KEY not in out["error"], "错误信息里泄露了 key！"


def test_probe_is_not_masked_by_fallback(app_env, monkeypatch):
    """**兜底不能替坏 key 撒谎**。

    默认 `LINGER_LLM_FALLBACK=mock`。如果探测用套了韧性层的 `app.state.llm`，
    那么「key 是错的」会被 mock 接住，界面显示「已生效 · 回了一句『嗯……』」——
    用户以为配好了，实际每句话都在兜底。这正是 #38 里那种「看起来正常，
    其实永远在走降级」的故障形态，必须钉住。
    """
    c, app, _ = app_env

    class Boom:
        name = "boom"

        def generate(self, *a, **k):
            raise RuntimeError("401 Unauthorized")

    from linger.server import app as app_mod
    from linger import llm as llm_mod

    monkeypatch.setattr(settings, "llm_provider", "openai", raising=False)
    monkeypatch.setattr(settings, "llm_fallback", "mock", raising=False)
    monkeypatch.setattr(llm_mod, "_build", lambda name: Boom())
    out = app_mod._probe_llm(app)
    assert out["ok"] is False, "主 provider 坏了，兜底接住不算「配置可用」"
    assert "401" in out["error"]


def test_probe_has_a_time_cap(app_env, monkeypatch):
    """卡住的地址不能让人盯 60 秒。"""
    import time as _time

    c, app, _ = app_env

    class Hangs:
        name = "hangs"

        def generate(self, *a, **k):
            _time.sleep(30)  # 模拟 DNS 挂住 / TCP 黑洞
            yield "太晚了"

    from linger.server import app as app_mod
    from linger import llm as llm_mod

    monkeypatch.setattr(settings, "llm_provider", "openai", raising=False)
    monkeypatch.setattr(llm_mod, "_build", lambda name: Hangs())
    monkeypatch.setattr(app_mod, "PROBE_DEADLINE", 0.3, raising=False)

    started = _time.monotonic()
    out = app_mod._probe_llm(app)
    elapsed = _time.monotonic() - started

    assert out["ok"] is False
    assert "秒没有回应" in out["error"]
    assert elapsed < 5, f"探测没有被时间上界挡住，用了 {elapsed:.1f}s"


# --------------------------------------------------------------------------- #
# 前端：面板真的会发请求、真的不抹掉 key（跑真 app.js，不是复刻）
# --------------------------------------------------------------------------- #
STATIC = Path(__file__).resolve().parent.parent / "src" / "linger" / "server" / "static"
HARNESS = Path(__file__).resolve().parent / "js" / "llm_settings_harness.js"
NODE = shutil.which("node")
needs_node = pytest.mark.skipif(NODE is None, reason="需要 node 跑真实前端脚本")

MOCK_EFF = {
    "effective": {
        "provider": "mock",
        "base_url": "",
        "model": "",
        "api_key_set": False,
        "api_key_hint": "",
    }
}


def _drive(scenario: str, settings_payload: dict | None = None) -> dict:
    proc = subprocess.run(
        [NODE, str(HARNESS)],
        input=json.dumps(
            {
                "app_js": str(STATIC / "app.js"),
                "scenario": scenario,
                "settings": settings_payload or MOCK_EFF,
            }
        ),
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=120,
        check=False,
    )
    assert proc.returncode == 0, f"前端脚本跑挂了：\n{proc.stderr[:3000]}"
    out = json.loads(proc.stdout)
    assert not out.get("error"), f"harness 内部报错：{out['error']}"
    return out


def _writes(out: dict) -> list[dict]:
    return [c for c in out["calls"] if c["method"] in ("PUT", "DELETE", "POST")]


@needs_node
def test_panel_opens_and_shows_effective_config():
    """点「模型」能开，并且显示的是服务端说的**当前生效**配置。"""
    out = _drive("open", {
        "effective": {
            "provider": "openai",
            "base_url": "https://api.deepseek.com/v1",
            "model": "deepseek-chat",
            "api_key_set": True,
            "api_key_hint": "sk-1…wxyz",
        },
        "file": "data/llm_settings.json",
    })
    assert out["panelHidden"] is False, "面板没打开"
    assert out["providerValue"] == "openai"
    assert "deepseek-chat" in out["hint"]
    assert "sk-1…wxyz" in out["hint"], "应该显示掩码后的 key 提示"
    assert out["filePath"] == "data/llm_settings.json"
    assert out["keyInputValue"] == "", "输入框不能回填 key"


@needs_node
def test_mock_mode_disables_and_clears_remote_fields():
    """mock 下三个字段置灰**并且清空**。

    不置灰：看着像能填，填了没用。置灰但留着上次的 openai 地址：
    用户会问「我没填怎么就有地址了」。所以两个都要。
    """
    out = _drive("open", {
        "effective": {
            "provider": "mock",
            "base_url": "https://api.openai.com/v1",
            "model": "mock-v2",
            "api_key_set": False,
            "api_key_hint": "",
        }
    })
    assert out["providerValue"] == "mock"
    assert out["fieldsDisabled"] == {"baseUrl": True, "model": True, "key": True}
    assert out["baseUrlValue"] == "", f"mock 下不该显示地址，实得 {out['baseUrlValue']!r}"
    assert out["modelValue"] == "", f"mock 下不该显示模型名，实得 {out['modelValue']!r}"


@needs_node
def test_saving_with_key_sends_it_once_and_never_echoes():
    """填了 key → PUT 带上它；但界面上不留、响应里的 key 也不回显。"""
    out = _drive("save_with_key")
    writes = _writes(out)
    assert len(writes) == 1 and writes[0]["method"] == "PUT", writes
    body = writes[0]["body"]
    assert body["provider"] == "openai"
    assert body["base_url"] == "https://api.deepseek.com/v1"
    assert body["model"] == "deepseek-chat"
    assert body["api_key"] == "sk-1234567890abcdefwxyz"
    assert out["keyInputValue"] == "", "提交后输入框应清空"
    assert out["okHidden"] is False and "已生效" in out["okText"]


@needs_node
def test_saving_without_key_does_not_wipe_it():
    """**这条是重点**：只想换模型名时留空 key，请求里不能带 api_key。

    如果前端无脑传 `api_key: ""`，后端会按三态语义把它当成「显式清除」——
    用户会发现「我只是换了个模型名，怎么就变回 mock 了」。
    """
    out = _drive("save_without_key")
    body = _writes(out)[0]["body"]
    assert "api_key" not in body, f"留空时不该带 api_key，实得 {body}"
    assert body["model"] == "deepseek-reasoner"


@needs_node
def test_clear_returns_to_mock_and_disables_fields():
    out = _drive("clear")
    assert _writes(out)[0]["method"] == "DELETE"
    assert out["providerValue"] == "mock"
    assert out["fieldsDisabled"] == {"baseUrl": True, "model": True, "key": True}, \
        "mock 下三个字段应置灰（置灰而非隐藏，才看得出「能填但现在不用」）"
    assert "mock" in out["hint"]


@needs_node
def test_rejection_shows_reason_and_keeps_typed_key_for_retry():
    """后端试连失败会回滚并 400。界面要把原因说出来，且**保留用户填的 key**——
    否则他得重新粘一遍才能改个地址重试。"""
    out = _drive("save_rejected")
    assert out["errHidden"] is False
    assert "连不上" in out["errText"]
    assert out["keyInputValue"] == "sk-bad", "失败后不该清掉用户填的 key"
    assert "mock" in out["hint"], "失败应体现回滚（仍显示原来的 mock）"


@needs_node
def test_test_button_reports_failure():
    out = _drive("test_fail")
    assert _writes(out)[0]["url"] == "/v1/settings/llm/test"
    assert out["errHidden"] is False
    assert "timed out" in out["errText"]


# --------------------------------------------------------------------------- #
# 错误文本：我们自己抛的 RuntimeError 已经是人话，不要再套类名
# --------------------------------------------------------------------------- #


def test_err_text_drops_runtime_prefix():
    """判据：provider 的友好错译直接展示，不加 `RuntimeError: ` 前缀。

    用户看到「RuntimeError: 模型接口拒绝了这个令牌（401）」会以为是程序崩了，
    而不是「我 key 填错了」。前缀是噪音，会把责任推给程序而不是配置。
    """
    from linger.server.app import _err_text

    friendly = "模型接口拒绝了这个令牌（401）。检查 key 是否正确。"
    out = _err_text(RuntimeError(friendly))
    assert out == friendly, f"不该带前缀，实际：{out!r}"


def test_err_text_keeps_class_for_unexpected():
    """判据：非 RuntimeError 的意外异常要保留类名——那是真正的程序问题。"""
    from linger.server.app import _err_text

    out = _err_text(ValueError("boom"))
    assert out.startswith("ValueError:"), f"意外异常应保留类名，实际：{out!r}"


def test_err_text_falls_back_to_class_when_empty():
    """判据：空消息不能变成空字符串（前端会显示一个空错误框）。"""
    from linger.server.app import _err_text

    assert _err_text(RuntimeError("")) == "RuntimeError"
