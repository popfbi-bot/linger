"""测试夹具：临时 SQLite + 内置人格模板。

假嵌入器 / 假模型见 tests/_fakes.py（确定性、可重复、无网络）。
"""

from __future__ import annotations

import sys
import tempfile
from collections.abc import Iterator
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from _fakes import FakeEmbedder  # noqa: E402,F401

from linger.memory.embedding import HashEmbedder  # noqa: E402
from linger.models import PersonaCard  # noqa: E402
from linger.storage.sqlite import SQLiteRepository  # noqa: E402


@pytest.fixture
def repo() -> Iterator[SQLiteRepository]:
    with tempfile.TemporaryDirectory() as tmp:
        r = SQLiteRepository(Path(tmp) / "test.db")
        yield r
        r.close()


@pytest.fixture
def persona(repo: SQLiteRepository) -> PersonaCard:
    from linger.persona.templates import load_template

    card = load_template("linxi")
    repo.save_persona(card)
    return card


@pytest.fixture
def fake_embedder() -> FakeEmbedder:
    return FakeEmbedder()


@pytest.fixture
def real_embedder() -> HashEmbedder:
    return HashEmbedder(256)


# --------------------------------------------------------------------------- #
# HTTP 客户端（test_api.py 与 test_frontend.py 共用）
# --------------------------------------------------------------------------- #
@pytest.fixture(autouse=True)
def scenario_dir(tmp_path, monkeypatch):
    """把用户场景目录钉到临时目录——**对所有测试生效**（M3-G 测试隔离修复）。

    为什么放 conftest 且 autouse：原先这个 fixture 只在 test_scenario_create.py
    里，其它走到 `POST /v1/scenarios` 的用例（如 test_scenario.py 的节拍推进）
    会写进**真实的** `~/.linger/`——全量跑红、单独跑绿，因为顺序不同目录脏净不同。
    这个坑的特征就是「隔离只做了一半」：新建测试的自己 setenv 了，
    没意识到别的用例也在用同一个全局目录。

    目录**建好再返回**：save_scenario() 会自己 mkdir，但「手工放影子文件」
    这类绕过 API 直接写文件的测试也需要它，统一在这里建省得每个测试自己来。
    """
    target = tmp_path / "scenarios"
    target.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("LINGER_SCENARIO_DIR", str(target))
    return target


@pytest.fixture
def client(repo: SQLiteRepository, persona: PersonaCard):
    """带临时 SQLite 的测试客户端。

    persona fixture 已经把 linxi 人格存进 repo。
    LLM 与嵌入器用假件：免密钥、免网络、确定性。
    """
    from fastapi.testclient import TestClient
    from _fakes import FakeLLM

    from linger.server.app import create_app

    app = create_app(repo=repo, llm=FakeLLM(), embedder=FakeEmbedder())
    with TestClient(app) as c:
        yield c


@pytest.fixture
def session_id(client, persona: PersonaCard) -> str:
    resp = client.post(
        "/v1/sessions",
        json={"persona_id": persona.id, "user_id": "u_api"},
    )
    assert resp.status_code == 201
    return resp.json()["session_id"]
