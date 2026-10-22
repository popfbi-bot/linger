"""一键打开灵犀的界面。

    python examples/serve.py

它会做三件事：
    1. 用 dev 默认配置起一个本地服务（SQLite + 哈希嵌入 + Mock 模型，免密钥）；
    2. 把地址打印出来；
    3. 尝试自动打开浏览器。

想看真模型的效果，先设两个环境变量再跑：

    LINGER_LLM=openai OPENAI_API_KEY=sk-... OPENAI_BASE_URL=https://api.deepseek.com/v1 \
    OPENAI_MODEL=deepseek-chat python examples/serve.py
"""

from __future__ import annotations

import os
import sys
import threading
import webbrowser
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

HOST = os.environ.get("LINGER_HOST", "127.0.0.1")
PORT = int(os.environ.get("LINGER_PORT", "8000"))


def _open_browser_later(url: str, delay: float = 1.6) -> None:
    """等 uvicorn 起来再开浏览器。用 Timer 而不是 sleep，免得阻塞启动。"""
    threading.Timer(delay, lambda: webbrowser.open(url)).start()


def main() -> int:
    import uvicorn

    url = f"http://{HOST}:{PORT}"
    print("=" * 60)
    print("  灵犀 Linger")
    print("=" * 60)
    print(f"  界面：{url}")
    print(f"  API 文档：{url}/docs")
    print(f"  模型：{os.environ.get('LINGER_LLM', 'mock')}")
    print()
    print("  点左边选人格，选场景（或「不设定场景」），然后说第一句话。")
    print("  Ctrl+C 退出。")
    print("=" * 60)

    if os.environ.get("LINGER_NO_BROWSER", "").lower() not in ("1", "true", "yes"):
        _open_browser_later(url)

    uvicorn.run(
        "linger.server.app:app",
        host=HOST,
        port=PORT,
        log_level="warning",
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
