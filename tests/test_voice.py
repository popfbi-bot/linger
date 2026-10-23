"""语音链路（issue #35）：听 + 说 + 情绪韵律。

判据 1~9 对应下面的小节标题。几条贯穿全文的原则：

· **断言参数，不断言音频字节。** 两条请求的 wav 字节不同说明不了任何事
  （编码器加点噪声就变了）。韵律与人格语体的证据是 provider 收到的 `prosody`
  与 `voice` 字典——所以假 provider 会把它们记下来（`RecordingTTS`）。
· **mock 不是「恒返回同一个字符串」。** 假的 ASR 有确定的输入相关性：
  同一段音频两次转写一致（可复现），不同长度转写长度不同（真的读了字节）。
  否则全绿只能证明「函数被调用了」，证明不了链路。
· **不引音频硬件依赖。** 服务端只吃字节；前端录音由浏览器负责，这里不碰。
"""

from __future__ import annotations

import base64

import pytest
from _fakes import BrokenASR, BrokenTTS, FakeEmbedder, FakeLLM, RecordingASR, RecordingTTS
from fastapi.testclient import TestClient

from linger.models import PAD, AffectState
from linger.persona.templates import load_template
from linger.server.app import create_app
from linger.voice import get_asr, get_tts, prosody_for
from linger.voice.mock import MockASR, MockTTS

SECRET = "test-secret-voice"

# 一段「像音频」的字节：WA_VE 头 + 载荷。不上真编码——这条链路上没人解码它。
WAV_HEADER = b"RIFF" + b"\x24\x00\x00\x00" + b"WAVE"


def _wav(payload: bytes = b"\x00" * 320) -> bytes:
    return WAV_HEADER + payload


@pytest.fixture
def voice_app(repo):
    """插入了「会记参数的」假 provider 的客户端。"""
    card = load_template("linxi")
    repo.save_persona(card)
    asr, tts = RecordingASR(), RecordingTTS()
    app = create_app(
        repo=repo, llm=FakeLLM(), embedder=FakeEmbedder(), auth_secret=SECRET, asr=asr, tts=tts
    )
    with TestClient(app) as client:
        yield {"client": client, "asr": asr, "tts": tts, "repo": repo, "persona": card}


@pytest.fixture
def mock_app(repo, monkeypatch):
    """走**默认配置路径**的客户端：`get_asr()/get_tts()` 真的被调用、真的选中 mock。

    判据 6 要的正是这条路径——注入了假件就绕过了工厂，等于没测「默认不联网」。
    """
    card = load_template("linxi")
    repo.save_persona(card)
    monkeypatch.setenv("LINGER_ASR", "mock")
    monkeypatch.setenv("LINGER_TTS", "mock")
    app = create_app(repo=repo, llm=FakeLLM(), embedder=FakeEmbedder(), auth_secret=SECRET)
    with TestClient(app) as client:
        yield {"client": client, "repo": repo, "persona": card}


def _set_affect(repo, persona_id: str, user_id: str, *, arousal: float, pleasure: float = 0.0) -> None:
    repo.save_affect(
        persona_id,
        user_id,
        AffectState(
            pad=PAD(pleasure=pleasure, arousal=arousal, dominance=0.5), label="test"
        ),
    )


# =========================================================================== #
# 判据 1：两个抽象 + mock + 按配置选
# =========================================================================== #
def test_asr_tts_protocols_have_the_declared_shape():
    """`transcribe(audio: bytes)` / `synthesize(text, *, voice, prosody)`。"""
    from linger.voice import ASRProvider, TTSProvider

    assert hasattr(ASRProvider, "transcribe")
    assert hasattr(TTSProvider, "synthesize")
    # 协议是 typing.Protocol：运行时可判（不是 ABC，不该要求继承）
    assert getattr(ASRProvider, "_is_protocol", False) is True
    assert getattr(TTSProvider, "_is_protocol", False) is True


def test_default_factories_are_mock(monkeypatch):
    monkeypatch.delenv("LINGER_ASR", raising=False)
    monkeypatch.delenv("LINGER_TTS", raising=False)
    assert get_asr().name == "mock"
    assert get_tts().name == "mock"
    assert isinstance(get_asr("mock"), MockASR)
    assert isinstance(get_tts("mock"), MockTTS)


def test_unimplemented_provider_degrades_to_mock_and_is_recorded():
    """配了没接入的 provider：降级到 mock，且**记下来**（判据 6 的可观测面）。

    显式传名字而不是只 setenv：`settings` 是 import 时求值一次的快照
    （#34 踩过这个坑），改环境变量不会让 `settings.asr_provider` 跟着变——
    那是刻意的权衡，配置是部署事实。工厂本身接受显式名字，生产路径读 settings。
    """
    from linger.voice import fallback_notes, reset_fallbacks

    reset_fallbacks()
    assert get_asr("whisper").name == "mock"
    notes = fallback_notes()
    assert "asr" in notes and "whisper" in notes["asr"]

    # 换成 mock 之后记录要清掉，否则「降级过一次」会永远挂在健康检查里
    get_asr("mock")
    assert "asr" not in fallback_notes()


# =========================================================================== #
# 判据 2：POST /v1/asr
# =========================================================================== #
def test_asr_multipart_returns_text(voice_app):
    c = voice_app["client"]
    resp = c.post("/v1/asr", files={"audio": ("voice.wav", _wav(), "audio/wav")})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["text"] and isinstance(body["text"], str)
    assert body["bytes"] == len(_wav())
    # provider 真的收到了字节，且长度对得上（不是空跑一遍）
    assert voice_app["asr"].calls[-1]["bytes"] == len(_wav())


def test_asr_base64_json_returns_text(voice_app):
    c = voice_app["client"]
    payload = base64.b64encode(_wav()).decode()
    resp = c.post("/v1/asr", json={"audio_base64": payload, "language": "zh"})
    assert resp.status_code == 200, resp.text
    assert resp.json()["text"]
    assert voice_app["asr"].calls[-1]["language"] == "zh"


def test_asr_accepts_data_url_prefix(voice_app):
    """前端 `FileReader.readAsDataURL` 给的就是带前缀的串。"""
    payload = "data:audio/webm;base64," + base64.b64encode(_wav()).decode()
    resp = voice_app["client"].post("/v1/asr", json={"audio_base64": payload})
    assert resp.status_code == 200, resp.text


@pytest.mark.parametrize(
    "kwargs",
    [
        {"files": {"audio": ("empty.wav", b"", "audio/wav")}},
        {"json": {"audio_base64": ""}},
        {"json": {}},
        {"json": {"audio_base64": base64.b64encode(b"not audio at all").decode()}},
        {"json": {"audio_base64": "!!!!not-base64!!!!"}},
        {"json": {"audio_base64": base64.b64encode(b"AB").decode()}},
    ],
    ids=["empty-file", "empty-b64", "no-body", "not-audio", "bad-base64", "too-short"],
)
def test_asr_rejects_bad_input_with_422_not_500(voice_app, kwargs):
    """判据 2 的红线：空文件 / 非音频字节 → 422。**绝不是 500**。"""
    resp = voice_app["client"].post("/v1/asr", **kwargs)
    assert resp.status_code == 422, resp.text


def test_asr_oversized_audio_is_413(voice_app, monkeypatch):
    from linger.config import settings

    monkeypatch.setattr(settings, "voice_max_audio_bytes", 32)
    resp = voice_app["client"].post("/v1/asr", files={"audio": ("big.wav", _wav(b"\x00" * 400), "audio/wav")})
    assert resp.status_code == 413


def test_asr_provider_failure_is_503(repo):
    """provider 挂了是 503（服务不可用），不是 500——两者的区别是「重试有没有意义」。"""
    card = load_template("linxi")
    repo.save_persona(card)
    app = create_app(
        repo=repo, llm=FakeLLM(), embedder=FakeEmbedder(), auth_secret=SECRET,
        asr=BrokenASR(), tts=RecordingTTS(),
    )
    with TestClient(app) as c:
        resp = c.post("/v1/asr", files={"audio": ("v.wav", _wav(), "audio/wav")})
    assert resp.status_code == 503


def test_mock_asr_is_deterministic_and_length_related():
    """判据 6 的「mock 不能是恒返回同一个字符串」：复现 + 输入相关。"""
    asr = MockASR()
    a = _wav(b"\x01" * 160)
    b = _wav(b"\x02" * 800)
    assert asr.transcribe(a) == asr.transcribe(a)          # 同一段音频两次一致
    assert asr.transcribe(a) != asr.transcribe(b)          # 不同音频不同结果
    assert len(asr.transcribe(b)) > len(asr.transcribe(a))  # 更长的音频更长的转写


# =========================================================================== #
# 判据 3：POST /v1/tts
# =========================================================================== #
def test_tts_returns_wav_bytes(voice_app):
    resp = voice_app["client"].post(
        "/v1/tts", json={"text": "嗯，我在。", "persona_id": voice_app["persona"].id}
    )
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("audio/wav")
    assert resp.content[:4] == b"RIFF"
    assert len(resp.content) > 44  # 比一个 WAV 头长
    # 同一段文本在不同情绪下音频不该被缓存住（否则播错情绪）
    assert resp.headers["cache-control"] == "no-store"


@pytest.mark.parametrize(
    "body",
    [
        {"text": "", "persona_id": "p_x"},
        {"text": "   ", "persona_id": "p_x"},
        {"persona_id": "p_x"},
        {"text": "嗯" * 2001, "persona_id": "p_x"},
    ],
    ids=["empty", "blank", "missing-text", "too-long"],
)
def test_tts_rejects_bad_text_with_422(voice_app, body):
    resp = voice_app["client"].post("/v1/tts", json=body)
    assert resp.status_code == 422, resp.text


def test_tts_blank_persona_id_is_not_500(voice_app):
    """空 persona_id：422（字段校验）或 404（查不到这个人格）都算对，
    唯一不能出现的是 500——「你发的东西不对」不该表现成「服务端坏了」。"""
    resp = voice_app["client"].post("/v1/tts", json={"text": "嗯", "persona_id": "   "})
    assert resp.status_code in (404, 422), resp.text


def test_tts_unknown_persona_is_404(voice_app):
    resp = voice_app["client"].post("/v1/tts", json={"text": "嗯", "persona_id": "p_nope"})
    assert resp.status_code == 404


def test_tts_cloned_card_is_forbidden(repo):
    """cloned（复刻真人）不许出声——与「不许上架」同一条红线的第二面。"""
    card = load_template("linxi")
    card.provenance = {"type": "cloned"}
    repo.save_persona(card)
    app = create_app(
        repo=repo, llm=FakeLLM(), embedder=FakeEmbedder(), auth_secret=SECRET,
        asr=RecordingASR(), tts=RecordingTTS(),
    )
    with TestClient(app) as c:
        resp = c.post("/v1/tts", json={"text": "嗯", "persona_id": card.id})
    assert resp.status_code == 403


def test_tts_provider_failure_is_503(repo):
    card = load_template("linxi")
    repo.save_persona(card)
    app = create_app(
        repo=repo, llm=FakeLLM(), embedder=FakeEmbedder(), auth_secret=SECRET,
        asr=RecordingASR(), tts=BrokenTTS(),
    )
    with TestClient(app) as c:
        resp = c.post("/v1/tts", json={"text": "嗯", "persona_id": card.id})
    assert resp.status_code == 503


# =========================================================================== #
# 判据 4：情绪韵律是真的（断言参数，不断言字节）
# =========================================================================== #
def test_prosody_differs_between_high_and_low_arousal(voice_app):
    """判据 4。同一段文本，高 arousal vs 低 arousal → provider 收到的 prosody 不同。"""
    c, tts, repo = voice_app["client"], voice_app["tts"], voice_app["repo"]
    pid = voice_app["persona"].id

    _set_affect(repo, pid, "u_hot", arousal=0.95)
    _set_affect(repo, pid, "u_cold", arousal=0.05)

    assert c.post("/v1/tts", json={"text": "我在听。", "persona_id": pid, "user_id": "u_hot"}).status_code == 200
    assert c.post("/v1/tts", json={"text": "我在听。", "persona_id": pid, "user_id": "u_cold"}).status_code == 200

    hot, cold = tts.calls[-2], tts.calls[-1]
    assert hot["text"] == cold["text"] == "我在听。"
    # 至少一项不同——这是判据 4 的原话，也刻意不要求「全部不同」
    assert hot["prosody"]["rate"] != cold["prosody"]["rate"] or hot["prosody"]["pitch"] != cold["prosody"]["pitch"]
    # 具体方向也要对：激动说得快，低落说得慢
    assert hot["prosody"]["rate"] > cold["prosody"]["rate"]


def test_prosody_is_pure_function_of_pad():
    """韵律不掺随机：同一份 PAD 永远同一组值，否则判据 4 的断言会抖。"""
    pad = PAD(pleasure=0.3, arousal=0.8, dominance=0.5)
    assert prosody_for(pad) == prosody_for(pad)
    assert set(prosody_for(pad)) == {"rate", "pitch", "energy"}


def test_every_prosody_dimension_actually_varies():
    """三项都必须真的随情绪变：任何一项恒定，等于那一维没做。

    逐维验证而不是「随手挑两个 PAD 比一比」——后者会被「另两维的变化」
    蒙过去，测不出某一维被写死。
    """
    low = prosody_for(PAD(pleasure=-0.5, arousal=0.1, dominance=0.5))
    high = prosody_for(PAD(pleasure=0.5, arousal=0.9, dominance=0.5))
    assert low["rate"] != high["rate"], "rate 不随 arousal 变"
    assert low["pitch"] != high["pitch"], "pitch 不随 pleasure 变"
    assert low["energy"] != high["energy"], "energy 不随 arousal 变"
    # dominance 这一棒不参与韵律：它影响的是「敢不敢说」，是文本层的事
    assert prosody_for(PAD(pleasure=0.0, arousal=0.5, dominance=0.1)) == prosody_for(
        PAD(pleasure=0.0, arousal=0.5, dominance=0.9)
    )


def test_tts_without_user_id_uses_persona_baseline(voice_app):
    """没聊过天的人点「播放」：用她的基线情绪，不是随便一个 PAD()。"""
    c, tts = voice_app["client"], voice_app["tts"]
    card = voice_app["persona"]
    c.post("/v1/tts", json={"text": "嗯。", "persona_id": card.id})
    assert tts.calls[-1]["prosody"] == prosody_for(card.affect.baseline)


# =========================================================================== #
# 判据 5：人格 voice 参与（同样断言参数）
# =========================================================================== #
def test_persona_voice_reaches_provider(voice_app):
    """语域原样透传；句长（cadence）由「她平时的句长 vs 这一句」算出来。"""
    c, tts = voice_app["client"], voice_app["tts"]
    card = voice_app["persona"]

    c.post("/v1/tts", json={"text": "嗯。", "persona_id": card.id})
    short = tts.calls[-1]["voice"]
    assert short["register"] == card.voice.register
    # 林汐 avg_sentence_len 是 [8, 20]（均值 14），"嗯。"只有 1 个字 → 说得比她平时还短
    assert short["cadence"] < 1.0

    long_text = "今天下雨了，我在书店里坐着，把昨天没读完的那本书读完了一半，忽然想起你说过的话。"
    c.post("/v1/tts", json={"text": long_text, "persona_id": card.id})
    long = tts.calls[-1]["voice"]
    assert long["cadence"] > short["cadence"]
    assert long["sentence_chars"] > short["sentence_chars"]


def test_two_personas_get_different_voice_params(repo, voice_app):
    """换一个人格，voice 参数跟着变——否则「人格 voice 参与」只是句空话。"""
    c, tts = voice_app["client"], voice_app["tts"]
    other = load_template("laojiang")
    repo.save_persona(other)

    c.post("/v1/tts", json={"text": "坐下。先喝口酒。", "persona_id": voice_app["persona"].id})
    c.post("/v1/tts", json={"text": "坐下。先喝口酒。", "persona_id": other.id})
    assert tts.calls[-2]["voice"] != tts.calls[-1]["voice"]


# =========================================================================== #
# 判据 6：免密钥免网络 + 降级可观测
# =========================================================================== #
def test_default_path_never_touches_network(mock_app, monkeypatch):
    """默认配置下整条链路不联网：DNS 与连接入口全被掐，照样 200。

    **不能把 `socket.socket` 这个类本身替换掉**（验收方修正，2026-10-23）：
    Windows 的 `ProactorEventLoop` 在 `asyncio/windows_events.py` 里会对收到的
    连接做 `isinstance(conn, socket.socket)`。类被换成函数后那里抛
    `TypeError: isinstance() arg 2 must be a type`，而这个异常发生在事件循环的
    recv 回调里——事件循环就此静默失效，请求**永远不返回**。
    表现不是「测试红」，而是「全量测试卡在 97% 不动」（本机实测挂死 13 分钟）。
    Linux 的 selector 事件循环不走那条 isinstance，所以 CI 绿、Windows 挂。

    只掐「发起连接」的入口（`create_connection` / `getaddrinfo` /
    `socket.connect` / `connect_ex`），类型本身保持原样：
    任何真实外连都会 boom，而事件循环照常工作。
    """
    import socket

    def boom(*_a, **_kw):
        raise AssertionError("语音链路试图联网")

    monkeypatch.setattr(socket, "create_connection", boom)
    monkeypatch.setattr(socket, "getaddrinfo", boom)
    monkeypatch.setattr(socket.socket, "connect", boom)
    monkeypatch.setattr(socket.socket, "connect_ex", boom)

    c = mock_app["client"]
    pid = mock_app["persona"].id
    assert c.post("/v1/asr", files={"audio": ("v.wav", _wav(), "audio/wav")}).status_code == 200
    assert c.post("/v1/tts", json={"text": "嗯。", "persona_id": pid}).status_code == 200


def test_voice_health_reports_provider_and_fallback(mock_app):
    c = mock_app["client"]
    body = c.get("/v1/voice/health").json()
    assert body["asr"]["provider"] == "mock"
    assert body["tts"]["provider"] == "mock"
    assert body["asr"]["fallback"] is None


def test_voice_health_shows_degradation(repo, monkeypatch):
    """配了 whisper 却没接入 → 健康检查必须说出来，静默降级等于骗人。

    `settings` 是 import 时快照，所以这里直接改字段（生产里就是进程启动时的环境变量）。
    """
    from linger.config import settings
    from linger.voice import reset_fallbacks

    reset_fallbacks()
    monkeypatch.setattr(settings, "asr_provider", "whisper")
    card = load_template("linxi")
    repo.save_persona(card)
    app = create_app(repo=repo, llm=FakeLLM(), embedder=FakeEmbedder(), auth_secret=SECRET)
    with TestClient(app) as c:
        body = c.get("/v1/voice/health").json()
    assert body["asr"]["provider"] == "mock"
    assert body["asr"]["configured"] == "whisper"
    assert body["asr"]["fallback"] and "whisper" in body["asr"]["fallback"]


# =========================================================================== #
# 判据 7：前端有可断言的锚点（DOM 级断言在 test_voice_frontend.py）
# =========================================================================== #
def test_frontend_has_stable_voice_anchors():
    from pathlib import Path

    static = Path(__file__).resolve().parent.parent / "src" / "linger" / "server" / "static"
    html = (static / "index.html").read_text(encoding="utf-8")
    assert 'id="btn-mic"' in html
    assert 'id="voice-status"' in html
    js = (static / "app.js").read_text(encoding="utf-8")
    assert '"/v1/asr"' in js and '"/v1/tts"' in js
    assert "MediaRecorder" in js


# =========================================================================== #
# 判据 8：不破坏现有文本链路
# =========================================================================== #
def test_text_chain_unchanged_without_voice_params(voice_app):
    """不带任何语音参数的请求，行为与今天一致：SSE 事件序列原样。"""
    c = voice_app["client"]
    pid = voice_app["persona"].id
    sid = c.post("/v1/sessions", json={"persona_id": pid, "user_id": "u_text"}).json()["session_id"]

    with c.stream("POST", f"/v1/sessions/{sid}/messages", json={"content": "今天有点累"}) as resp:
        assert resp.status_code == 200
        assert resp.headers["content-type"].startswith("text/event-stream")
        raw = "".join(resp.iter_text())

    types = [line[len("event: "):] for line in raw.splitlines() if line.startswith("event: ")]
    assert types[0] == "thinking"
    assert "affect" in types
    assert "delta" in types
    assert types[-1] == "done"
    # 语音 provider 一次都不该被碰到
    assert voice_app["asr"].calls == []
    assert voice_app["tts"].calls == []


def test_voice_endpoints_are_additive_in_openapi(voice_app):
    """新端点是**加**出来的：老端点一个不少，老端点的方法签名没被改。"""
    paths = voice_app["client"].get("/openapi.json").json()["paths"]
    for expected in ("/v1/asr", "/v1/tts", "/v1/voice/health"):
        assert expected in paths
    for keep in (
        "/v1/sessions/{session_id}/messages",
        "/v1/personas",
        "/v1/sessions/{session_id}/memories",
        "/v1/traces/{turn_id}",
    ):
        assert keep in paths
