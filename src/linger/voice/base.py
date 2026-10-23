"""语音层的两个协议 + 一个异常。

形状照抄 `llm/base.py`：业务层只认协议，不关心背后是 mock 还是云端 Whisper。
两件事**故意不做**：

· 不抄韧性层。重试 / 熔断是 M4-A 已验收的行为，绑在 LLM 的整轮超时语义上；
  语音只是一次同步调用，套上去只会让「一次 TTS 失败」变成另一个需要解释的降级故事。
· 不做流式音频。这一棒先把「说得出来」跑通，流式是另一个问题（分片、停顿、打断）。

`VoiceError` 只有一个基类，理由和 `LLMRuntimeError` 一样：HTTP 层捕获这一个
就能把「语音服务挂了」翻译成 503，不必为每种坏法各写一遍。
"""

from __future__ import annotations

from typing import Any, Protocol


class VoiceError(RuntimeError):
    """语音 provider 不可用的基类。HTTP 层据此返回 503（不是 500）。"""


class AudioRejected(ValueError):
    """输入不是可识别的音频 / 音频为空。

    刻意继承 ValueError 而不是 VoiceError：这是**调用方发错了**（422），
    不是服务端坏了（503）。两者的区别是「重试有没有意义」。
    """


class ASRProvider(Protocol):
    name: str

    def transcribe(self, audio: bytes, *, language: str | None = None) -> str:
        """把一段音频变成文字。输入必须是**字节**——服务端不碰音频设备。"""
        ...


class TTSProvider(Protocol):
    name: str

    def synthesize(
        self,
        text: str,
        *,
        voice: dict[str, Any] | None = None,
        prosody: dict[str, Any] | None = None,
    ) -> bytes:
        """把文字变成音频字节。

        `voice` 是人格的语体（语域、句长、口头禅），`prosody` 是这一句的
        情绪韵律（rate / pitch / energy）。两者分开是为了让「她是谁」和
        「她此刻什么心情」各自可断言——前者跟人格卡走，后者跟 PAD 走。
        """
        ...
