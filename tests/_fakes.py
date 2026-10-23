"""确定性假件：假嵌入器 + 假模型。不依赖真实模型或网络。"""

from __future__ import annotations

from collections.abc import Iterator


class FakeEmbedder:
    """把文本映射成「词命中」向量，余弦 ≈ 词集合重合度。

    词 = 2 字滑窗。所以「让记忆过闸门」就是把它的关键词原样写进查询里，
    「让它过不了闸门」就是不写——判据清晰，不依赖 HashEmbedder 的字面脾气。
    """

    dim = 1024

    def __init__(self) -> None:
        self._vocab: dict[str, int] = {}

    def _slot(self, token: str) -> int:
        if token not in self._vocab:
            self._vocab[token] = len(self._vocab) % self.dim
        return self._vocab[token]

    def embed(self, text: str) -> list[float]:
        vec = [0.0] * self.dim
        s = "".join(ch for ch in (text or "") if not ch.isspace())
        tokens = [s[i : i + 2] for i in range(len(s) - 1)] or ([s] if s else [])
        for t in tokens:
            vec[self._slot(t)] += 1.0
        norm = sum(v * v for v in vec) ** 0.5
        return [v / norm for v in vec] if norm else vec


class FakeLLM:
    """记下 system prompt，按固定片段流式吐字。"""

    name = "fake"

    def __init__(self, reply: str = "嗯……我在。") -> None:
        self.reply = reply
        self.prompts: list[str] = []
        self.calls = 0

    def generate(self, system, messages, ctx=None) -> Iterator[str]:
        self.calls += 1
        self.prompts.append(system)
        for i in range(0, len(self.reply), 2):
            yield self.reply[i : i + 2]


class RecordingTTS:
    """把收到的参数原样记下来的假 TTS——判据 4/5 唯一可断言的证据。

    为什么不看返回的音频字节：编码器加点噪声字节就变了，两条请求的字节不同
    说明不了「韵律真的变了」。参数才是契约。
    """

    name = "fake-tts"

    def __init__(self) -> None:
        self.calls: list[dict] = []

    def synthesize(self, text, *, voice=None, prosody=None):
        self.calls.append({"text": text, "voice": dict(voice or {}), "prosody": dict(prosody or {})})
        # 结构合法的 WAV 头（44 字节）+ 一点载荷：判据 3 要看的是「能不能播」
        from linger.voice.mock import _wav

        return _wav(b"\x00" * 16)


class RecordingASR:
    """确定性、且**与输入相关**的假 ASR。

    同一段音频 → 同一句（可复现）；不同长度 → 不同长度（证明它真读了字节）。
    """

    name = "fake-asr"

    def __init__(self) -> None:
        self.calls: list[dict] = []

    def transcribe(self, audio, *, language=None):
        self.calls.append({"bytes": len(audio or b""), "language": language})
        return "字" * max(1, len(audio or b"") // 10)


class BrokenASR:
    """provider 挂了 → 必须是 503，不是 500（判据 2 的另一面）。"""

    name = "broken-asr"

    def transcribe(self, audio, *, language=None):
        from linger.voice import VoiceError

        raise VoiceError("ASR 后端不可用")


class BrokenTTS:
    name = "broken-tts"

    def synthesize(self, text, *, voice=None, prosody=None):
        from linger.voice import VoiceError

        raise VoiceError("TTS 后端不可用")
