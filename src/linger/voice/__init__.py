"""语音层：ASR（听）+ TTS（说，带情绪韵律）。

形状照抄 `llm/`：协议（`base.py`）+ 免密钥实现（`mock.py`）+ 按配置选（本文件）。
**没有**韧性层——重试 / 熔断是 M4-A 已验收的 LLM 行为，这一棒不碰。

`LINGER_ASR` / `LINGER_TTS` 默认都是 `mock`：不填任何配置就能跑完整条链路，
CI 也不联网。真实 provider（Whisper / 各家 TTS）只留了名字与配置位——
认得出名字、说得清「为什么没启用」，但不会凭空造一个半成品对接出来。
"""

from __future__ import annotations

from ..config import settings
from .base import ASRProvider, AudioRejected, TTSProvider, VoiceError
from .mock import MockASR, MockTTS
from .prosody import avg_sentence_chars, prosody_for, voice_params

__all__ = [
    "ASRProvider",
    "AudioRejected",
    "MockASR",
    "MockTTS",
    "TTSProvider",
    "VoiceError",
    "avg_sentence_chars",
    "get_asr",
    "get_tts",
    "prosody_for",
    "voice_params",
]

# 认识但没实现的名字。列出来不是摆设：`LINGER_TTS=openai` 时我们要能说清
# 「你配的这个还没接」并降级到 mock，而不是抛一个 KeyError 让人猜。
_KNOWN_UNIMPLEMENTED = {
    "asr": ("whisper", "openai", "sensevoice", "funasr"),
    "tts": ("openai", "azure", "edge", "elevenlabs", "minimax", "cosyvoice"),
}

# 最近一次「配置要求真实 provider、实际退回 mock」的记录。
# `/v1/voice/health` 读它——降级必须是**可观测**的，静默降级等于骗人。
_fallbacks: dict[str, str] = {}


def _fallback_reason(kind: str, name: str) -> str:
    if name in _KNOWN_UNIMPLEMENTED[kind]:
        return f"{kind.upper()} provider「{name}」尚未接入（本棒只留接口与配置位），已降级到 mock"
    return f"未知的 {kind.upper()} provider「{name}」，已降级到 mock"


def _record(kind: str, reason: str | None) -> None:
    if reason is None:
        _fallbacks.pop(kind, None)
    else:
        _fallbacks[kind] = reason


def get_asr(name: str | None = None) -> ASRProvider:
    """按名字造一个 ASR provider。默认读 `LINGER_ASR`，默认值 `mock`。

    **未实现的名字降级到 mock 并记录**，不抛异常（判据 6）：配错一个环境变量
    不该让整个服务起不来，但也不能假装配好了——所以记在 `_fallbacks` 里。
    """
    key = (name if name is not None else settings.asr_provider) or "mock"
    key = key.strip().lower()
    if key == "mock":
        _record("asr", None)
        return MockASR()
    _record("asr", _fallback_reason("asr", key))
    return MockASR()


def get_tts(name: str | None = None) -> TTSProvider:
    """按名字造一个 TTS provider。默认读 `LINGER_TTS`，默认值 `mock`。"""
    key = (name if name is not None else settings.tts_provider) or "mock"
    key = key.strip().lower()
    if key == "mock":
        _record("tts", None)
        return MockTTS()
    _record("tts", _fallback_reason("tts", key))
    return MockTTS()


def fallback_notes() -> dict[str, str]:
    """当前进程里发生过的降级（空 dict = 没有降级）。"""
    return dict(_fallbacks)


def reset_fallbacks() -> None:
    """清空降级记录（测试用：它是进程级状态）。"""
    _fallbacks.clear()
