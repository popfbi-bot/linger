"""存储接口（Repository 模式）。

业务层只依赖这些抽象方法，不感知底层是 SQLite 还是 PostgreSQL。
生产切换：实现一个 PostgresRepository（schema 见 docs/ARCHITECTURE.md §4.2），
在 `storage/get_repository()` 里按配置选择即可，业务代码零改动。
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import date
from typing import Any

from ..models import (
    AffectState,
    CompanionProfile,
    DiaryEntry,
    MemoryEditRecord,
    MemoryNode,
    PersonaCard,
    RelationState,
    Session,
    TraceRecord,
    Turn,
)


class Repository(ABC):
    # ---- 人格 ---------------------------------------------------------- #
    @abstractmethod
    def save_persona(self, card: PersonaCard) -> None: ...

    @abstractmethod
    def get_persona(self, persona_id: str) -> PersonaCard | None:
        """按 id 取人格；不存在**或已删除**返回 None。

        M3-C：删除是软删（status='deleted'），所以读取路径必须过滤状态。
        返回 None 让调用方（HTTP 层）自然给 404，不必各自再判一次状态。
        """

    @abstractmethod
    def list_personas(self, limit: int = 50) -> list[PersonaCard]:
        """列出可用人格（只含 status='active'）。已删除的不出现。"""

    @abstractmethod
    def find_persona_by_template(self, template_id: str) -> PersonaCard | None:
        """按模板来源查找**仍然 active** 的已实例化人格；没有返回 None。

        **为什么需要它**：模板实例化必须幂等。记忆按 `(persona_id, user_id)` 存，
        每次点模板都新建一个人格 id，会把同一个用户的记忆切碎到多个 id 下——
        表现为「刷新页面再点林汐，她就不记得你了」。对陪伴产品这是致命缺陷，
        不是功能缺失。

        判定依据是 `provenance.template`（`load_template` 本来就写了这个键）。
        用户自建（一句话生成）的人格没有这个键，不会被误撞。

        **为什么必须过滤 status**：删除模板人格 = 重置这段关系。
        用户删掉林汐再点林汐，应该是一个全新开始（记忆从零），
        而不是把旧记忆接回来。已删的那份查不到，实例化才会重建。
        """

    @abstractmethod
    def delete_persona(self, persona_id: str) -> PersonaCard | None:
        """软删除人格，返回被删的卡；不存在或已删除返回 None。

        实现约定（M3-C）：

        - 只把 `persona.status` 改成 'deleted'，**不物理删行**，
          也不动 `memory_node` / `turn` / `session`。
          删除人格 ≠ 删除历史——用户只是「这个人格不要了」，
          会话记录与记忆是另一个维度的数据（也便于误删后恢复）。
        - 已删的人格再删返回 None（HTTP 层映射 404）。
          不做幂等 200：那样用户看不出自己删了两遍，前端也拿不到真实状态。
        """

    # ---- 会话 ---------------------------------------------------------- #
    @abstractmethod
    def save_session(self, session: Session) -> None: ...

    @abstractmethod
    def get_session(self, session_id: str) -> Session | None: ...

    @abstractmethod
    def list_sessions(self, persona_id: str, user_id: str, limit: int = 100) -> list[Session]:
        """列出某个人格与某个用户之间的会话（最近活跃的在前）。

        **为什么需要它**：日记是「当天」的产物，而 turn 只挂在 session 上。
        要收集「今天这个人格与这个用户聊了什么」，必须先能按人找到会话——
        否则只能遍历全表 turn，那在真实数据量下是灾难。
        """

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

    # ---- 记忆编辑审计（M2-C） ------------------------------------------ #
    @abstractmethod
    def add_memory_edit(self, record: MemoryEditRecord) -> None:
        """记录一次记忆编辑（before/after）。

        编辑是「纠正口径」，必须留痕——前端 trace 面板据此展示修改前后的差异。
        写入发生在 PATCH 端点成功更新记忆之后，与记忆落库同一事务。
        """

    @abstractmethod
    def list_memory_edits(self, memory_id: str, limit: int = 20) -> list[MemoryEditRecord]:
        """按记忆返回编辑历史（新的在前）。没有记录返回空列表。"""

    # ---- 场景进度（M1.5） ---------------------------------------------- #
    @abstractmethod
    def save_scenario_progress(self, session_id: str, data: dict) -> None:
        """写入某个会话的场景进度。

        存最简结构 `{"scenario_id": ..., "done_beats": [...]}`——
        场景定义属于代码（YAML），进度属于运行时数据，两者不能混在一处，
        否则改一个 yaml 就会重写用户的历史。
        """

    @abstractmethod
    def get_scenario_progress(self, session_id: str) -> dict | None:
        """读回场景进度；没有记录返回 None（= 所有节拍都还没走）。"""

    # ---- 陪伴档案（M2-A） ---------------------------------------------- #
    @abstractmethod
    def save_companion_profile(self, profile: CompanionProfile) -> None:
        """写入陪伴档案（主键：persona_id + user_id，整体覆盖）。

        档案是「当前认知」而不是流水账：识别结果变了就整体替换，
        不需要保留中间态——历史在 turn 与 trace 里，这里只存结论。
        """

    @abstractmethod
    def get_companion_profile(self, persona_id: str, user_id: str) -> CompanionProfile | None:
        """读回陪伴档案；没有记录返回 None。

        None 是常态（新用户、纯闲聊），调用方要能空手走开：
        HTTP 层返回空档案而不是 404，编译器直接跳过注入。
        """

    # ---- 日记（M3-D） -------------------------------------------------- #
    @abstractmethod
    def save_diary(self, entry: DiaryEntry) -> None:
        """写入一篇日记（**upsert**）。

        唯一键是 `(persona_id, user_id, date)`，不是 `id`：
        一天只有一篇，重复生成是覆盖当天那篇，不是再插一行。
        所以实现必须按三元组做 ON CONFLICT，`id` 只在首次生成时定下来。
        """

    @abstractmethod
    def get_diary(self, persona_id: str, user_id: str, day: date) -> DiaryEntry | None:
        """取某一天的日记；没有返回 None（HTTP 层映射 404）。

        没有任何日记**不等于**「今天什么都没发生」——
        那两件事在语义上完全不同，所以这里不造空壳日记。
        """

    @abstractmethod
    def list_diary(self, persona_id: str, user_id: str, limit: int = 30) -> list[DiaryEntry]:
        """按日期倒序列出日记（新的在前）。

        用户隔离是硬要求：日记按 `(persona_id, user_id)` 过滤，
        `u_a` 的心事不能被 `u_b` 读到。
        """

    # ---- 回溯 ---------------------------------------------------------- #
    @abstractmethod
    def save_trace(self, trace: TraceRecord) -> None: ...

    @abstractmethod
    def get_trace(self, turn_id: int) -> dict[str, Any] | None: ...

    def close(self) -> None:  # pragma: no cover
        return None
