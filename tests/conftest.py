"""测试夹具：临时 SQLite + 内置人格模板。

假嵌入器 / 假模型见 tests/_fakes.py（确定性、可重复、无网络）。
"""

from __future__ import annotations

import json
import shutil
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

# --------------------------------------------------------------------------- #
# Node 可用性（前端判据靠 node 跑真 app.js，见 tests/js/）
# --------------------------------------------------------------------------- #
# 为什么放 conftest：之前 test_market_frontend.py 自己定义一份、test_scenario_edit_frontend.py
# 一份都没有——于是 CI 镜像（`python:3.11-slim`，没 node）里**全量红**，而本地绿。
# 同一类依赖只留一个判据，才不会一边跳过一边炸。
#
# ⚠️ 跳过**不是**解决办法：`.cnb.yml` 的 install stage 现在会装 nodejs，
# 所以 CI 上这些判据是真跑的。**跳过数从 9 涨上去就是「装漏了」的信号**
# ——9 是本地无 psycopg 时的既定跳过数。
HAS_NODE = shutil.which("node") is not None
needs_node = pytest.mark.skipif(not HAS_NODE, reason="需要 node 跑真实前端脚本")


# --------------------------------------------------------------------------- #
# 读不出中文文件名的环境（验收用）
# --------------------------------------------------------------------------- #
# 现象：`src/linger/social/data/*.md` 的文件名是中文。Linux 与真 Windows 上
# `Path.exists()` / `read_text()` 都正常，但在**某些 Windows 兼容层**
# （本次验收用 Wine 跑 Windows CPython 3.11 时踩到）里，对这些文件名
# `open()` 直接 ENOENT、连 `write_bytes()` 都建不出来。于是两份语料一条都
# 加载不出来，test_roles / test_social / test_toxic_signals / test_companion
# 全线红——这跟被测代码一个字都没关系。
#
# 这里用产品侧的 `LINGER_SOCIAL_DATA_DIR` + `LINGER_SOCIAL_NAME_MAP` 两个开关：
# 语料照原内容放进 `tests/_cjk_corpus/`，磁盘名是 ASCII、逻辑名仍是中文。
# 那两个开关不是为测试造的——中文文件名在解压工具、同步盘、兼容层里被弄坏
# 是常见事故，产品侧留「换目录 / 换磁盘名」的出口，比逼人改数据文件名诚实。
_CJK_CORPUS = Path(__file__).resolve().parent / "_cjk_corpus"
_CJK_MAP_FILE = Path(__file__).resolve().parent / "_cjk_corpus_map.json"


def _cjk_names_unreadable() -> bool:
    import platform

    if platform.system() != "Windows":
        return False
    import linger.social.knowledge as knowledge

    names = [f[0] for f in knowledge._CORPORA]
    # 不能靠 `glob`/`iterdir` 判断：返回的已经是乱码，只能按常量名逐个探。
    return not any((knowledge._DATA_DIR / n).exists() for n in names)


@pytest.fixture(autouse=True)
def _ascii_corpus_when_names_unreadable(monkeypatch):
    """中文文件名读不出来时，把语料目录与磁盘名映射切到 ASCII 副本。"""
    if not _cjk_names_unreadable():
        return
    mapping = json.loads(_CJK_MAP_FILE.read_text("utf-8"))
    monkeypatch.setenv("LINGER_SOCIAL_DATA_DIR", str(_CJK_CORPUS))
    monkeypatch.setenv("LINGER_SOCIAL_NAME_MAP", json.dumps(mapping, ensure_ascii=False))

    import linger.social.knowledge as knowledge
    import linger.social.roles as roles

    # 模块级常量与默认参数在 import 时就绑好了，这里一并换掉。
    monkeypatch.setattr(knowledge, "_DATA_DIR", _CJK_CORPUS)
    monkeypatch.setattr(knowledge, "_NAME_MAP", dict(mapping))
    monkeypatch.setattr(roles, "_DATA_DIR", _CJK_CORPUS)
    monkeypatch.setattr(roles, "_NAME_MAP", dict(mapping))
    for cls in (knowledge.SocialKnowledge, roles.RelationRoleKnowledge):
        defaults = list(cls.__init__.__defaults__ or ())
        if defaults:
            defaults[0] = _CJK_CORPUS
            monkeypatch.setattr(cls.__init__, "__defaults__", tuple(defaults))


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
    from _fakes import FakeLLM
    from fastapi.testclient import TestClient

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
