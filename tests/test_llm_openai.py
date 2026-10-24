"""OpenAI 兼容 provider 的行为判据（今天实测踩出来的三个坑）。

三个坑都来自真实联调，不是假想：

1. **地址重复拼接**：opencsg 网关给的是**完整端点**
   `https://opencsg.com/aigateway/v1/chat/completions`，
   无脑拼 `/chat/completions` 会变成 `.../chat/completions/chat/completions` → 404。
2. **max_tokens 太小**：deepseek-v4 系列是 **thinking 模型**，
   reasoning 也吃 max_tokens 配额。实测 400 时正文被截成空。
3. **429 直接变 500**：网关限流时 `raise_for_status()` 抛出，
   SSE 流中途断掉，前端只看到空白。必须重试 + 给可读错误。

全部用 fake transport 拦截，不联网、确定性可复现。
"""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

from linger.llm.openai_compat import (
    OpenAICompatProvider,
    _chat_completions_url,
    _error_detail,
    _friendly_error,
)

# --------------------------------------------------------------------------- #
# 1. 地址拼接：完整端点不许重复拼
# --------------------------------------------------------------------------- #


def test_url_not_doubled_when_already_full_endpoint():
    """已经是完整端点时原样返回——否则会拼出 /chat/completions/chat/completions。"""
    full = "https://opencsg.com/aigateway/v1/chat/completions"
    assert _chat_completions_url(full) == full


def test_url_with_trailing_slash():
    """结尾斜杠不能导致拼错。"""
    assert _chat_completions_url("https://x.com/v1/") == "https://x.com/v1/chat/completions"


def test_url_base_only():
    assert _chat_completions_url("https://x.com/v1") == "https://x.com/v1/chat/completions"


def test_empty_base_url_raises():
    with pytest.raises(RuntimeError):
        _chat_completions_url("")


# --------------------------------------------------------------------------- #
# 2. 错误信息：可读、不泄露令牌
# --------------------------------------------------------------------------- #


def test_friendly_error_429_says_capacity():
    """429 的提示要说「容量已满」——这是最常见的原因。"""
    resp = httpx.Response(
        429,
        json={"error": {"code": "capacity_exceeded", "message": "concurrency exceeded"}},
        request=httpx.Request("POST", "https://x/v1/chat/completions"),
    )
    msg = _friendly_error(429, resp)
    assert "容量" in msg
    assert "capacity_exceeded" in msg


def test_friendly_error_401_mentions_key():
    resp = httpx.Response(
        401, json={"error": {"message": "bad key"}},
        request=httpx.Request("POST", "https://x/v1/chat/completions"),
    )
    assert "令牌" in _friendly_error(401, resp)


def test_friendly_error_404_mentions_base_url():
    resp = httpx.Response(
        404, json={"error": {"message": "no such model"}},
        request=httpx.Request("POST", "https://x/v1/chat/completions"),
    )
    msg = _friendly_error(404, resp)
    assert "OPENAI_BASE_URL" in msg and "OPENAI_MODEL" in msg


def test_error_detail_does_not_leak_token():
    """错误详情里不能出现请求头里的令牌。"""
    resp = httpx.Response(
        429,
        headers={"x-echo": "Bearer sk-supersecret"},
        json={"error": {"message": "boom"}},
        request=httpx.Request(
            "POST", "https://x/v1/chat/completions",
            headers={"Authorization": "Bearer sk-supersecret"},
        ),
    )
    detail = _error_detail(resp)
    assert "sk-supersecret" not in detail
    assert "boom" in detail


# --------------------------------------------------------------------------- #
# 3. 重试：429 必须退避重试；非 429 不许重试
# --------------------------------------------------------------------------- #


class _FakeStream:
    """假流式响应：模拟 httpx 的语义。

    · `raise_for_status()` 非 200 就抛 `HTTPStatusError`
    · `iter_lines()` 只在 200 时被调（真 httpx 也是这个顺序）

    第几次调用由 `attempt`（client 传进来的序号）决定——
    不能自己数：真 httpx 是 client 每次 stream() 都新建一个响应对象，
    fake 若自带计数会永远读到 script[0]。
    """

    def __init__(self, script: list[int], attempt: int) -> None:
        self.script = script
        self.attempt = attempt
        self.status = script[min(attempt, len(script) - 1)]

    @property
    def status_code(self) -> int:
        """真 httpx 的 `Response` 有 `status_code`（不是 `status`）。

        假件原来只有 `status`，于是「非 2xx 先 read() 再抛」这段修复
        在假件上直接 AttributeError——**假件比真实宽松，也是一种失真**：
        它让被测代码用了真实环境里不存在的属性名而测不出来。
        """
        return self.status

    def read(self) -> bytes:
        """真 httpx 的 `Response.read()`。出错路径会先调它把 body 落定。

        假件不实现的话，`if r.status_code >= 400: r.read()` 直接 AttributeError。
        """
        return b""

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def raise_for_status(self):
        if self.status != 200:
            raise httpx.HTTPStatusError(
                f"status {self.status}",
                request=httpx.Request("POST", "https://x/v1/chat/completions"),
                response=httpx.Response(
                    self.status,
                    json={"error": {"code": "capacity_exceeded", "message": "full"}},
                    request=httpx.Request("POST", "https://x/v1/chat/completions"),
                ),
            )

    def iter_lines(self):
        for line in (
            'data: {"choices":[{"delta":{"content":"嗯。"}}]}',
            'data: {"choices":[{"delta":{"content":"我在。"}}]}',
            "data: [DONE]",
        ):
            yield line


class _FakeClient:
    """替换 httpx.Client：第一次 429，第二次 200。"""

    def __init__(self, script: list[int]) -> None:
        self.script = script
        self.stream_attempts = 0
        self.last_headers: dict = {}

    def __call__(self, *a, **kw):
        return self

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def stream(self, method, url, json=None, headers=None):
        attempt = self.stream_attempts
        self.stream_attempts += 1
        self.last_headers = headers or {}
        return _FakeStream(self.script, attempt)


def _provider(monkeypatch, script: list[int]) -> tuple[OpenAICompatProvider, _FakeClient]:
    fake = _FakeClient(script)
    monkeypatch.setattr(httpx, "Client", fake)
    # 退避 sleep 直接跳过，测试不真等
    import time as _time

    monkeypatch.setattr(_time, "sleep", lambda s: None)
    # 绕过 __init__ 的 api key 检查
    provider = OpenAICompatProvider.__new__(OpenAICompatProvider)
    return provider, fake


def test_429_then_success_retries(monkeypatch):
    """判据：429 之后必须重试并最终拿到正文。

    反例：把重试摘掉（attempts=1），这条会红——拿到的是 RuntimeError。
    """
    provider, fake = _provider(monkeypatch, [429, 200])
    text = "".join(provider.generate("sys", [{"role": "user", "content": "在吗"}]))
    assert text == "嗯。我在。", f"重试后应拿到正文，实际 {text!r}"
    assert fake.stream_attempts == 2, f"应请求 2 次（1 次 429 + 1 次成功），实际 {fake.stream_attempts}"


def test_401_does_not_retry(monkeypatch):
    """判据：401/403 是配置错，重试没意义——必须立刻失败。"""
    provider, fake = _provider(monkeypatch, [401])
    with pytest.raises(RuntimeError, match="令牌"):
        list(provider.generate("sys", [{"role": "user", "content": "hi"}]))
    assert fake.stream_attempts == 1, "401 不该重试"


def test_429_exhausted_raises_readable(monkeypatch):
    """判据：重试用尽后，报错要能说清「容量已满」，而不是裸 httpx 异常。"""
    provider, fake = _provider(monkeypatch, [429, 429, 429, 429])
    with pytest.raises(RuntimeError, match="容量"):
        list(provider.generate("sys", [{"role": "user", "content": "hi"}]))
    assert fake.stream_attempts == 4, f"应尝试 4 次，实际 {fake.stream_attempts}"


def test_authorization_header_sent(monkeypatch):
    """令牌必须以 Bearer 形式进请求头。"""
    provider, fake = _provider(monkeypatch, [200])
    list(provider.generate("sys", [{"role": "user", "content": "hi"}]))
    assert fake.last_headers.get("Authorization", "").startswith("Bearer ")


# --------------------------------------------------------------------------- #
# 4. max_tokens：thinking 模型的 reasoning 也吃配额
# --------------------------------------------------------------------------- #


def test_max_tokens_leaves_room_for_reasoning(monkeypatch):
    """判据：max_tokens 必须给 reasoning 留够余量。

    实测：deepseek-v4 系列把 reasoning 算进 max_tokens，
    400 会让正文被截成空（reasoning_tokens 就占了 55+）。
    这条反例是「把 max_tokens 改回 400」。
    """
    provider, fake = _provider(monkeypatch, [200])

    captured: dict = {}
    orig_stream = fake.stream

    def spy(method, url, json=None, headers=None):
        captured.update(json or {})
        return orig_stream(method, url, json=json, headers=headers)

    monkeypatch.setattr(fake, "stream", spy)
    list(provider.generate("sys", [{"role": "user", "content": "hi"}]))
    assert captured.get("max_tokens", 0) >= 1500, (
        f"max_tokens 太小（{captured.get('max_tokens')}）——"
        "thinking 模型的 reasoning 会吃掉配额，正文会被截成空"
    )


# --------------------------------------------------------------------------- #
# 5. 错误响应体必须被读出来——否则「友好错译」永远走不到
# --------------------------------------------------------------------------- #


class _UnreadBodyStream:
    """真实的流式错误响应：body 惰性，未 `read()` 前访问 `.json()` 会抛。

    这是 httpx 的真实语义（`ResponseNotRead`）。原来的假件用
    `httpx.Response(401, json={...})` 构造，body 是现成的、不需要读，
    所以测试一直绿——而线上用户填错 key 时看到的是一句 httpx 内部报错。

    这里刻意**持有同一个 `httpx.Response` 对象**：`read()` 落定它的 body，
    `raise_for_status()` 抛出的异常也指向它——和真 httpx 的对象关系一致。
    否则「读完再抛」这个修复在假件上验不出来。
    """

    def __init__(self, status: int, payload: dict) -> None:
        self.status = status
        self._resp = httpx.Response(
            status,
            stream=httpx.ByteStream(json.dumps(payload).encode("utf-8")),
            request=httpx.Request("POST", "https://x/v1/chat/completions"),
        )

    @property
    def status_code(self) -> int:
        return self.status

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def read(self) -> bytes:
        """把惰性 body 读进内存——真 httpx 里这一步之后 `.json()` 才能用。"""
        return self._resp.read()

    def raise_for_status(self):
        self._resp.raise_for_status()

    def iter_lines(self):
        yield "data: [DONE]"


class _UnreadBodyClient:
    def __init__(self, status: int, payload: dict) -> None:
        self.status = status
        self.payload = payload

    def __call__(self, *a, **kw):
        return self

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def stream(self, method, url, json=None, headers=None):
        return _UnreadBodyStream(self.status, self.payload)


def test_error_body_is_read_before_raising(monkeypatch):
    """判据：401 时报错必须是「令牌」人话，不能是 `ResponseNotRead`。

    这条反例很干脆：把 `_stream` 里那句 `if r.status_code >= 400: r.read()`
    删掉，本测试立刻红，报错变成
    「ResponseNotRead: Attempted to access streaming response content...」
    ——用户照着这句去查，永远查不到「你的 key 错了」。
    """
    fake = _UnreadBodyClient(401, {"error": {"message": "invalid api key"}})
    monkeypatch.setattr(httpx, "Client", fake)
    provider = OpenAICompatProvider.__new__(OpenAICompatProvider)

    with pytest.raises(RuntimeError) as ei:
        list(provider.generate("sys", [{"role": "user", "content": "hi"}]))
    msg = str(ei.value)
    assert "ResponseNotRead" not in msg, f"响应体没被读出来，报错是 httpx 内部细节：{msg}"
    assert "令牌" in msg, f"应给出「令牌」人话提示，实际：{msg}"


def test_error_body_read_keeps_server_detail(monkeypatch):
    """判据：读出来的响应体要真的用上——服务端给的原因不能丢。"""
    fake = _UnreadBodyClient(404, {"error": {"message": "no such model: gpt-9"}})
    monkeypatch.setattr(httpx, "Client", fake)
    provider = OpenAICompatProvider.__new__(OpenAICompatProvider)

    with pytest.raises(RuntimeError) as ei:
        list(provider.generate("sys", [{"role": "user", "content": "hi"}]))
    msg = str(ei.value)
    assert "no such model: gpt-9" in msg, f"服务端原因丢了：{msg}"
