"""存储层入口：按 `settings.storage` 选后端。

未选中的后端**不 import**（`__getattr__` 惰性加载）：
装了 sqlite 的人不该为了一个没用的 PG 模块把它拖进内存，
反过来也一样——PostgresRepository 模块顶层不碰 psycopg，加载它不需要驱动。
"""

from ..config import settings
from .base import Repository
from .sqlite import SQLiteRepository

__all__ = [
    "PostgresRepository",
    "Repository",
    "SQLiteRepository",
    "get_repository",
    "reset_repository_cache",
]

_CACHE: dict[str, Repository] = {}


def __getattr__(name: str):
    # 惰性暴露 PostgresRepository：`from linger.storage import PostgresRepository` 仍然可用，
    # 但只有在真的要它时才加载那个模块（以及它背后的 psycopg）。
    if name == "PostgresRepository":
        from .postgres import PostgresRepository as _PG

        return _PG
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def get_repository() -> Repository:
    """按配置返回单例。构造不连库——真实连接等到首次查询（判据 5）。"""
    key = f"{settings.storage}:{settings.db_path}"
    if key not in _CACHE:
        if settings.storage == "sqlite":
            _CACHE[key] = SQLiteRepository(settings.db_path)
        elif settings.storage == "postgres":
            from .postgres import PostgresRepository

            _CACHE[key] = PostgresRepository(settings.pg_dsn)
        else:
            raise ValueError(
                f"未知存储后端: {settings.storage}（可选 sqlite / postgres）"
            )
    return _CACHE[key]


def reset_repository_cache() -> None:
    """丢掉缓存并关掉已建的连接。

    进程退出时该关池；测试里切换后端时也必须重置——
    否则同一个进程里 `LINGER_STORAGE=postgres` 会拿到上一个用例复用的
    SQLite 实例，那种「测试绿但后端没换」的假绿比红更难查。
    """
    for repo in _CACHE.values():
        repo.close()
    _CACHE.clear()
