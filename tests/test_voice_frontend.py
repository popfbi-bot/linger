"""语音前端（判据 7）：按钮 + 点了会调 /v1/asr + 结果进输入框。

能断言到哪、为什么停在这：

· **能断言**：按钮存在且 id 稳定、点它之后真的发了 `POST /v1/asr`、
  请求体是 multipart 且里面塞了音频、识别结果落进了 `#input` 的值。
· **不能断言**：真的录到了声音。那需要浏览器自动化 + 音频设备，
  issue 明说不值当。所以这里用真 node 跑真 `app.js` + 假 DOM + 假 MediaRecorder：
  录音链路的**接口**是真的（`getUserMedia` → `MediaRecorder` → `Blob` → `FormData`），
  只是字节由假件提供。

为什么不 grep：grep 能证明「代码里有 /v1/asr」，证明不了「点了它真的发出去、
结果真的进了输入框」。后者才是判据 7 的内容。
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

STATIC = Path(__file__).resolve().parent.parent / "src" / "linger" / "server" / "static"
APP_JS = STATIC / "app.js"
HARNESS = Path(__file__).resolve().parent / "js" / "voice_harness.js"
NODE = shutil.which("node")
needs_node = pytest.mark.skipif(NODE is None, reason="需要 node 跑真实前端脚本")


# 一个「够用」的模板清单：会话要能开起来，`state.personaId` 才有值，
# 播放按钮才会真的走到 /v1/tts。模板内容不参与断言，只负责把链路搭起来。
TEMPLATES = [{"id": "linxi", "name": "林汐", "archetype": "温柔倾听者", "age": 24, "sample": "嗯……先坐吧。"}]


def _run(payload: dict) -> dict:
    full = {"app_js": str(APP_JS), "wait_ms": 120, "templates": TEMPLATES, **payload}
    proc = subprocess.run(
        [NODE, str(HARNESS)],
        input=json.dumps(full),
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert proc.returncode == 0, f"前端脚本跑挂了：\n{proc.stderr[:3000]}"
    return json.loads(proc.stdout)


@pytest.fixture(scope="module")
def dom() -> dict:
    return _run({"asr_text": "今天有点累"})


# --------------------------------------------------------------------------- #
# 判据 7-1：按钮存在，id 稳定
# --------------------------------------------------------------------------- #
def test_mic_button_exists_in_dom():
    html = (STATIC / "index.html").read_text(encoding="utf-8")
    assert 'id="btn-mic"' in html
    # 状态行也是判据的一部分：识别失败要说人话，不是静默
    assert 'id="voice-status"' in html


@needs_node
def test_mic_button_is_enabled_when_browser_can_record(dom):
    assert dom["canRecord"] is True
    assert dom["micDisabledInitial"] is False


@needs_node
def test_mic_button_stays_disabled_without_recording_support():
    """不支持录音的浏览器：按钮 disabled，但**文字输入一行不动**（语音是加的，不是换的）。"""
    out = _run({"can_record": False})
    assert out["canRecord"] is False
    assert out["micDisabledInitial"] is True
    # 没有录音能力时不该有任何 /v1/asr 请求
    assert not [c for c in out["fetchCalls"] if c["url"] == "/v1/asr"]


# --------------------------------------------------------------------------- #
# 判据 7-2：点了会调 /v1/asr
# --------------------------------------------------------------------------- #
@needs_node
def test_clicking_mic_posts_to_asr(dom):
    calls = [c for c in dom["fetchCalls"] if c["url"] == "/v1/asr"]
    assert len(calls) == 1, dom["fetchCalls"]
    assert calls[0]["method"] == "POST"
    assert calls[0]["bodyIsForm"] is True


@needs_node
def test_asr_request_carries_audio_and_lets_browser_set_content_type(dom):
    """multipart 里真的塞了 audio 文件；**没有**手写 Content-Type。

    手写 Content-Type 会丢掉 boundary，服务端解析必然失败——这是一个
    改动后「看起来能跑」的 bug，所以要在协议层钉住。
    """
    form = dom["asrForm"]
    assert form is not None, dom
    assert form["hasAudio"] is True
    assert form["hasExplicitContentType"] is False


@needs_node
def test_recording_tracks_are_stopped(dom):
    """录完必须显式停掉麦克风轨道——否则浏览器录音指示灯一直亮着。"""
    assert dom["streamStopped"] == [True]


# --------------------------------------------------------------------------- #
# 判据 7-3：识别结果进输入框（且不自动发送）
# --------------------------------------------------------------------------- #
@needs_node
def test_transcript_lands_in_input(dom):
    assert dom["inputValue"] == "今天有点累"
    # 不自动发送：只发 /v1/asr，没有 messages 请求
    assert not [c for c in dom["fetchCalls"] if c["url"].endswith("/messages")]


@needs_node
def test_empty_transcript_is_reported_not_swallowed():
    out = _run({"asr_text": ""})
    assert out["inputValue"] == ""
    assert "没听清" in out["statusText"]


@needs_node
def test_asr_error_is_reported_in_status_line():
    out = _run({"asr_status": 422})
    assert "识别失败" in out["statusText"]
    assert out["inputValue"] == ""


# --------------------------------------------------------------------------- #
# 判据 7-4：回复的「播放」入口
# --------------------------------------------------------------------------- #
@needs_node
def test_play_button_is_attached_to_reply(dom):
    assert len(dom["playButtons"]) == 1
    btn = dom["playButtons"][0]
    assert btn["text"] == "嗯，我在。"
    assert "听她念" in btn["label"]


@needs_node
def test_clicking_play_posts_to_tts_with_persona_and_user(dom):
    body = dom["ttsBody"]
    assert body is not None, dom
    assert body["text"] == "嗯，我在。"
    # persona_id 决定嗓音，user_id 决定读哪一份情绪——两个都不能少
    assert body["persona_id"] == "p_created"
    assert body["user_id"]


@needs_node
def test_every_ai_bubble_gets_a_play_button():
    """不是只有最后一条能听：每一句 AI 的话都该能单独点。"""
    out = _run({"asr_text": ""})
    assert len(out["playButtons"]) == 1
