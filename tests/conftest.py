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
