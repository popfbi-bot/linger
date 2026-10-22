"""存储接口（Repository 模式）。

业务层只依赖这些抽象方法，不感知底层是 SQLite 还是 PostgreSQL。
生产切换：实现一个 PostgresRepository（schema 见 docs/ARCHITECTURE.md §4.2），
在 `storage/get_repository()` 里按配置选择即可，业务代码零改动。
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any

from ..models import AffectState, MemoryNode, PersonaCard, RelationState, Session, TraceRecord, Turn


class Repository(ABC):
    # ---- 人格 ---------------------------------------------------------- #
    @abstractmethod
    def save_persona(self, card: PersonaCard) -> None: ...

    @abstractmethod
    def get_persona(self, persona_id: str) -> PersonaCard | None: ...

    @abstractmethod
    def list_personas(self, limit: int = 50) -> list[PersonaCard]: ...

    # ---- 会话 ---------------------------------------------------------- #
    @abstractmethod
    def save_session(self, session: Session) -> None: ...

    @abstractmethod
    def get_session(self, session_id: str) -> Session | None: ...

    # ---- 对话轮（L0） -------------------------------------------------- #
    @abstractmethod
    def append_turn(
        self, session_id: str, role: str, content: str, affect: dict[str, float] | None = None
    ) -> int: ...

    @abstractmethod
    def list_turns(self, session_id: str, limit: int = 40) -> list[Turn]: ...

    # ---- 情绪 ---------------------------------------------------------- #
    @abstractmethod
    def get_affect(self, persona_id: str, user_id: str) -> AffectState | None: ...

    @abstractmethod
    def save_affect(self, persona_id: str, user_id: str, state: AffectState) -> None: ...

    # ---- 关系 ---------------------------------------------------------- #
    @abstractmethod
    def get_relation(self, persona_id: str, user_id: str) -> RelationState | None: ...

    @abstractmethod
    def save_relation(self, state: RelationState) -> None: ...

    # ---- 记忆（L2~L5） ------------------------------------------------- #
    @abstractmethod
    def add_memories(self, nodes: list[MemoryNode]) -> None: ...

    @abstractmethod
    def list_memories(
        self, persona_id: str, user_id: str, layer: int | None = None, limit: int = 500
    ) -> list[MemoryNode]: ...

    @abstractmethod
    def get_memory(self, memory_id: str) -> MemoryNode | None:
        """按 id 查单条记忆。

        M1 记忆管理 API 需要它：HTTP 层拿到 memory_id 后要能定位到
        它属于哪个 persona/user，否则既无法判断"不存在"（该 404），
        也无法在不遍历全部会话的前提下删除。
        """

    @abstractmethod
    def update_memory(self, node: MemoryNode) -> None: ...

    @abstractmethod
    def delete_memory(self, memory_id: str) -> None: ...

    # ---- 回溯 ---------------------------------------------------------- #
    @abstractmethod
    def save_trace(self, trace: TraceRecord) -> None: ...

    @abstractmethod
    def get_trace(self, turn_id: int) -> dict[str, Any] | None: ...

    def close(self) -> None:  # pragma: no cover
        return None
