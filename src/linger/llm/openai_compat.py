"""OpenAI 兼容接口提供者（DeepSeek / Moonshot / 本地 vLLM 均可用）。

只需配置 .env：
    LINGER_LLM=openai
    OPENAI_BASE_URL=https://api.deepseek.com/v1
    OPENAI_API_KEY=sk-xxx
    OPENAI_MODEL=deepseek-chat
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from typing import Any

import httpx

from ..config import settings


class OpenAICompatProvider:
    name = "openai"

    def __init__(self) -> None:
        if not settings.openai_api_key:
            raise RuntimeError(
                "LINGER_LLM=openai 但未配置 OPENAI_API_KEY。改回 LINGER_LLM=mock 可免密钥运行。"
            )

    def generate(
        self,
        system: str,
        messages: list[dict[str, str]],
        ctx: dict[str, Any] | None = None,
    ) -> Iterator[str]:
        payload = {
            "model": settings.openai_model,
            "messages": [{"role": "system", "content": system}, *messages],
            "stream": True,
            "temperature": 0.9,
            "max_tokens": 400,
        }
        headers = {
            "Authorization": f"Bearer {settings.openai_api_key}",
            "Content-Type": "application/json",
        }
        url = settings.openai_base_url.rstrip("/") + "/chat/completions"

        with httpx.Client(timeout=60.0) as client, client.stream("POST", url, json=payload, headers=headers) as r:
            r.raise_for_status()
            for line in r.iter_lines():
                if not line or not line.startswith("data:"):
                    continue
                data = line[5:].strip()
                if data == "[DONE]":
                    break
                try:
                    delta = json.loads(data)["choices"][0]["delta"].get("content") or ""
                except (KeyError, IndexError, json.JSONDecodeError):
                    continue
                if delta:
                    yield delta
