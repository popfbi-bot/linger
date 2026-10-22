from ..config import settings
from .base import Repository
from .sqlite import SQLiteRepository

__all__ = ["Repository", "SQLiteRepository", "get_repository"]

_CACHE: dict[str, Repository] = {}


def get_repository() -> Repository:
    key = f"{settings.storage}:{settings.db_path}"
    if key not in _CACHE:
        if settings.storage == "sqlite":
            _CACHE[key] = SQLiteRepository(settings.db_path)
        else:
            raise ValueError(f"未知存储后端: {settings.storage}（骨架阶段仅支持 sqlite）")
    return _CACHE[key]
