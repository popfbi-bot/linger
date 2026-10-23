"""免密钥的语音 provider。

这里的 mock 不是「恒返回同一个字符串」的假实现——那种全绿只能证明
「函数被调用了」。所以两个 mock 都遵守**确定性 + 输入相关**：

· 同一段音频两次转写结果完全一致（可复现）；
· 不同音频长度转写长度不同（链路真的读了字节）；
· 同一段文本 + 同一组参数的音频字节完全一致，参数一变字节就变。

同时它们**记录自己收到的参数**（`calls`）。判据 4/5 的断言就落在这些记录上：
音频字节不可断言（编码器加点噪声就变了），参数可以。
"""

from __future__ import annotations

import base64
import hashlib
import struct
from typing import Any

# mock ASR 的「音节表」。落在 ASCII 里是因为：这些是**假转写**，不是产品文案，
# 用英文单词能让任何人一眼看出「这是 mock 说的」，不会误当成真实识别结果。
_SYLLABLES = (
    "ling", "xi", "ting", "jian", "shuo", "hao", "ya", "ne", "ba", "wo",
    "zai", "ni", "de", "shen", "bian", "xian", "zai", "hui", "qu", "le",
)

# 每个「音节」吃掉多少字节音频。取 8 是量级选择：一段 1 秒的 16bit/8kHz
# 语音约 16KB，÷8 ≈ 每 8 字节一个字——短句读起来大概对得上。
_BYTES_PER_SYLLABLE = 8

# 太短的字节流不可能是「一句话」：小于这个长度就当作空音频拒掉。
# 4 字节连一个 WAV 头都不够，正常录音不会这么短。
MIN_AUDIO_BYTES = 4


class MockASR:
    """假识别器：字节数 → 音节数，同一段音频永远同一句。"""

    name = "mock"

    def __init__(self) -> None:
        # 每次调用记一条，测试与 /v1/voice/health 都读这份记录
        self.calls: list[dict[str, Any]] = []

    def transcribe(self, audio: bytes, *, language: str | None = None) -> str:
        n = len(audio or b"")
        digest = hashlib.sha256(audio or b"").hexdigest()
        self.calls.append(
            {"op": "asr", "bytes": n, "sha256": digest, "language": language}
        )
        return " ".join(self._words(audio or b""))

    @staticmethod
    def _words(audio: bytes) -> list[str]:
        count = len(audio) // _BYTES_PER_SYLLABLE
        # 至少一个音节：1 字节的输入不该得到空字符串（空转写要留给「什么都没听到」）
        count = max(count, 1)
        digest = hashlib.sha256(audio).digest()
        out: list[str] = []
        for i in range(count):
            # 前 8 个字由音频内容决定，之后由长度决定：既复现又「读到了字节」
            b = (digest[i] if i < len(digest) else len(audio)) % len(_SYLLABLES)
            out.append(_SYLLABLES[b])
        return out


def _wav(payload: bytes) -> bytes:
    """把 payload 包成一段结构合法的 8-bit 单声道 WAV。

    为什么真造 WAV 而不是随便返回几个字节：判据 3 要的是 `audio/wav`，
    浏览器 `Audio` 元素会真的去解这个文件。给一段解不开的数据，
    前端「播放」按钮在真机上就是坏的——而测试里看不出来。
    """
    n = len(payload)
    header = b"RIFF" + struct.pack("<I", 36 + n) + b"WAVE"
    fmt = b"fmt " + struct.pack("<IHHIIHH", 16, 1, 1, 8000, 8000, 1, 8)
    data = b"data" + struct.pack("<I", n)
    return header + fmt + data + payload


class MockTTS:
    """假合成器：参数决定字节，字节是能播的 WAV。"""

    name = "mock"

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def synthesize(
        self,
        text: str,
        *,
        voice: dict[str, Any] | None = None,
        prosody: dict[str, Any] | None = None,
    ) -> bytes:
        voice = voice or {}
        prosody = prosody or {}
        # 记一份**深拷贝的纯字面量**：调用方后续改了字典，记录不该跟着变。
        params = {"text": text, "voice": dict(voice), "prosody": dict(prosody)}
        self.calls.append(params)

        # 参数进哈希：同一文本不同韵律 → 不同音频，这正是「韵律真的生效」的弱证据
        # （强证据是 calls 里的参数本身，见判据 4）。
        stamp = repr((sorted(voice.items()), sorted(prosody.items()))).encode("utf-8")
        seed = hashlib.sha256(text.encode("utf-8") + b"|" + stamp).digest()
        n = max(8, min(400, len(text) * 6))
        payload = bytes(seed[i % len(seed)] for i in range(n))
        return _wav(payload)


def audio_size(audio_b64: str) -> int:
    """解码 base64 后的字节数（给「这段音频有多长」这类观测点用）。"""
    try:
        return len(base64.b64decode(audio_b64, validate=True))
    except Exception:  # noqa: BLE001 - 长度只是观测，坏数据不该在这里炸
        return 0
