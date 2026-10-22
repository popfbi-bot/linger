"""OpenAI 兼容接口提供者（DeepSeek / Moonshot / 本地 vLLM 均可用）。

只需配置 .env：
    LINGER_LLM=openai
    OPENAI_BASE_URL=https://api.deepseek.com/v1
    OPENAI_API_KEY=sk-xxx
    OPENAI_MODEL=deepseek-chat
"""

from __future__ import annotations

import json
import time
from collections.abc import Iterator
from typing import Any

import httpx

from ..config import settings


def _chat_completions_url(base_url: str) -> str:
    """把配置的地址拼成 chat/completions 端点。

    为什么不能无脑拼 `base_url + "/chat/completions"`：
        有些网关给的是**完整端点**（如 `https://opencsg.com/aigateway/v1/chat/completions`），
        无脑拼会变成 `.../chat/completions/chat/completions` → 404。
        所以先看结尾在哪儿，已经是端点就直接用。
    """
    url = (base_url or "").strip().rstrip("/")
    if not url:
        raise RuntimeError("OPENAI_BASE_URL 为空。")
    if url.endswith("/chat/completions"):
        return url
    return url + "/chat/completions"


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
            # 注意：thinking 型模型（deepseek-v4 系列）会把 reasoning 也算进
            # max_tokens。给小了会出现「正文为空、只有思考过程被截断」——
            # 实测 max_tokens=400 时 flash 模型返回空 content。留足余量。
            "max_tokens": 2000,
        }
        headers = {
            "Authorization": f"Bearer {settings.openai_api_key}",
            "Content-Type": "application/json",
        }
        url = _chat_completions_url(settings.openai_base_url)

        # 限流（429）重试：网关的并发配额是动态的，退避一下往往就能过。
        # 不重试的话 429 会直接变成 500 —— SSE 流中途断掉，前端只看到空白。
        attempts = 4
        for attempt in range(attempts):
            try:
                yield from self._stream(url, payload, headers)
                return
            except httpx.HTTPStatusError as exc:
                code = exc.response.status_code
                if code == 429 and attempt < attempts - 1:
                    backoff = 2.0 * (attempt + 1)
                    detail = _error_detail(exc.response)
                    print(
                        f"[linger] 模型限流（429），{backoff:.0f}s 后重试"
                        f"（{attempt + 2}/{attempts}）{detail}"
                    )
                    time.sleep(backoff)
                    continue
                raise RuntimeError(_friendly_error(code, exc.response)) from exc

    def _stream(
        self, url: str, payload: dict, headers: dict
    ) -> Iterator[str]:
        """真正的流式请求。异常由调用方处理。"""
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


def _error_detail(response: httpx.Response) -> str:
    """从错误响应里抠出可读的原因（不泄露令牌）。"""
    try:
        body = response.json()
    except Exception:
        return response.text[:120]
    if isinstance(body, dict):
        err = body.get("error", body)
        if isinstance(err, dict):
            code = err.get("code") or ""
            msg = err.get("message") or ""
            return f"[{code}] {msg}"[:160]
    return str(body)[:120]


def _friendly_error(code: int, response: httpx.Response) -> str:
    """把 HTTP 错误码翻译成人能看懂的话。"""
    detail = _error_detail(response)
    if code == 401 or code == 403:
        return f"模型接口拒绝了这个令牌（{code}）。检查 OPENAI_API_KEY 是否正确、是否有权限。{detail}"
    if code == 404:
        return f"模型接口找不到这个端点或模型（{code}）。检查 OPENAI_BASE_URL 与 OPENAI_MODEL。{detail}"
    if code == 429:
        return f"模型接口限流（{code}），重试多次仍不通。这个模型可能当前容量已满，换一个试试。{detail}"
    if code >= 500:
        return f"模型服务端错误（{code}），稍后重试。{detail}"
    return f"模型接口返回 {code}。{detail}"
