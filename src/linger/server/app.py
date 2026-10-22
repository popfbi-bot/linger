"""HTTP 服务层（docs/ARCHITECTURE.md §5.3 / §6.3）。

把 M0 的引擎链路暴露成 API：前端通过 SSE 消费一轮对话，
通过记忆管理与 trace 接口做「看得见的陪伴」。

三条设计约定：

1. **引擎不变**。本层不重写任何引擎逻辑，只做三件事：
   参数校验 → 调用 Orchestrator / Repository → 序列化。
   流水线顺序（§2.3）是契约，这里不改。

2. **SSE 事件原样转发**。`send_message` 已经产出
   `thinking → affect → delta* → done` 的固定序列（§5.3），
   本层只负责加上 `event:` / `data:` 帧格式，不重排、不合并、不丢弃。
   `data` 的 JSON 同时保留 `type` 与 `data` 两个字段：
   前端既能靠 SSE 的 `event:` 分流，也能靠 JSON 里的 `type` 判断。

3. **错误不外泄成 500**。会话/人格/记忆/trace 不存在一律 404，
   参数不合法一律 422/400。前端能据此区分「你发错了」和「服务端坏了」。

装配方式：`create_app(...)` 工厂。生产用默认参数（读 settings），
测试用注入参数（临时 SQLite + 假 LLM/嵌入器），免密钥、免网络、确定性。
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import Depends, FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from ..config import settings
from ..llm import get_provider
from ..memory.embedding import get_embedder
from ..orchestrator import Orchestrator
from ..storage.base import Repository
from ..storage.sqlite import SQLiteRepository

# --------------------------------------------------------------------------- #
# 请求体
# --------------------------------------------------------------------------- #


class CreatePersonaRequest(BaseModel):
    template_id: str = Field(..., min_length=1, description="人格模板 id，如 linxi / laojiang")


class CreateSessionRequest(BaseModel):
    persona_id: str = Field(..., min_length=1)
    user_id: str = Field(..., min_length=1)
    scenario: str | None = None


class SendMessageRequest(BaseModel):
    content: str = Field(..., min_length=1, description="用户这一轮说的话")


# --------------------------------------------------------------------------- #
# 序列化
# --------------------------------------------------------------------------- #


def _persona_brief(card) -> dict[str, Any]:
    return {
        "id": card.id,
        "name": card.name,
        "identity": card.meta.identity,
        "archetype": card.meta.archetype,
        "version": card.version,
    }


def _memory_brief(node) -> dict[str, Any]:
    return {
        "id": node.id,
        "layer": node.layer,
        "type": node.type,
        "content": node.content,
        "source_type": node.source_type,
        "importance": node.importance,
        "strength": node.strength,
        "mention_count": node.mention_count,
        "recall_count": node.recall_count,
        "status": node.status,
        "pinned": node.pinned,
        "created_at": node.created_at.isoformat(),
    }


# --------------------------------------------------------------------------- #
# SSE
# --------------------------------------------------------------------------- #


def _sse_frame(event_type: str, payload: dict[str, Any]) -> str:
    """一帧 SSE：`event:` 行 + `data:` 行 + 空行。

    data 里同时带 `type` 与 `data`，这样即使客户端忽略 SSE 的
    `event:` 字段（某些实现如此），也能从 JSON 自行分流。
    """
    body = json.dumps({"type": event_type, "data": payload}, ensure_ascii=False)
    return f"event: {event_type}\ndata: {body}\n\n"


def _event_stream(events: Iterator[Any]) -> Iterator[str]:
    """把 Orchestrator 的事件流转成 SSE 帧。"""
    for event in events:
        yield _sse_frame(event.type, event.data)


# --------------------------------------------------------------------------- #
# 应用工厂
# --------------------------------------------------------------------------- #


def create_app(
    repo: Repository | None = None,
    llm: Any | None = None,
    embedder: Any | None = None,
    db_path: Path | None = None,
) -> FastAPI:
    """构造 FastAPI 应用。

    传入 repo/llm/embedder 时直接复用（测试用）；
    不传则在 lifespan 里按 settings 创建（生产/dev 用）。
    """

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        # 外部注入的 repo 由调用方负责关闭，这里不接管
        if repo is not None:
            app.state.repo = repo
            app.state.orch = Orchestrator(repo, llm or get_provider(), embedder or get_embedder(
                settings.embedder, settings.embedding_dim
            ))
            yield
            return

        path = db_path or settings.db_path
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        own_repo = SQLiteRepository(path)
        app.state.repo = own_repo
        app.state.orch = Orchestrator(
            own_repo,
            llm or get_provider(),
            embedder or get_embedder(settings.embedder, settings.embedding_dim),
        )
        try:
            yield
        finally:
            own_repo.close()

    app = FastAPI(
        title="Linger",
        version="0.1.0",
        description="AI 陪伴人格引擎的 HTTP 服务层：SSE 流式对话 + 记忆管理 + trace 回溯",
        lifespan=lifespan,
    )

    # 前端本地调试用。allow_credentials 不能与 "*" 同时开，所以不设。
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["http://localhost:*", "http://127.0.0.1:*"],
        allow_methods=["*"],
        allow_headers=["*"],
    )

    def get_repo() -> Repository:
        return app.state.repo

    def get_orch() -> Orchestrator:
        return app.state.orch

    # ------------------------------------------------------------------ #
    # 健康检查
    # ------------------------------------------------------------------ #
    @app.get("/health")
    def health() -> dict[str, Any]:
        return {
            "status": "ok",
            "llm": settings.llm_provider,
            "embedder": settings.embedder,
        }

    # ------------------------------------------------------------------ #
    # 人格
    # ------------------------------------------------------------------ #
    @app.get("/v1/personas")
    def list_personas(repo: Repository = Depends(get_repo)) -> dict[str, Any]:
        return {"personas": [_persona_brief(c) for c in repo.list_personas()]}

    @app.post("/v1/personas", status_code=201)
    def create_persona(
        body: CreatePersonaRequest,
        repo: Repository = Depends(get_repo),
        orch: Orchestrator = Depends(get_orch),
    ) -> dict[str, Any]:
        try:
            card = orch.persona_factory.create(body.template_id, repo)
        except (FileNotFoundError, KeyError, ValueError) as exc:
            raise HTTPException(status_code=404, detail=f"人格模板不存在: {body.template_id}") from exc
        return _persona_brief(card)

    # ------------------------------------------------------------------ #
    # 会话
    # ------------------------------------------------------------------ #
    @app.post("/v1/sessions", status_code=201)
    def create_session(
        body: CreateSessionRequest,
        repo: Repository = Depends(get_repo),
        orch: Orchestrator = Depends(get_orch),
    ) -> dict[str, Any]:
        if repo.get_persona(body.persona_id) is None:
            raise HTTPException(status_code=404, detail=f"人格不存在: {body.persona_id}")
        session = orch.create_session(body.persona_id, body.user_id, body.scenario)
        return {
            "session_id": session.id,
            "persona_id": session.persona_id,
            "user_id": session.user_id,
            "scenario_id": session.scenario_id,
        }

    @app.get("/v1/sessions/{session_id}")
    def get_session(
        session_id: str,
        repo: Repository = Depends(get_repo),
    ) -> dict[str, Any]:
        session = repo.get_session(session_id)
        if session is None:
            raise HTTPException(status_code=404, detail=f"会话不存在: {session_id}")
        affect = repo.get_affect(session.persona_id, session.user_id)
        relation = repo.get_relation(session.persona_id, session.user_id)
        return {
            "session_id": session.id,
            "persona_id": session.persona_id,
            "user_id": session.user_id,
            "scenario_id": session.scenario_id,
            "started_at": session.started_at.isoformat(),
            "affect": (
                {"label": affect.label, **affect.pad.model_dump()} if affect else None
            ),
            "relation": (
                {
                    "level": relation.level,
                    "intimacy": relation.intimacy,
                    "unresolved_conflicts": relation.unresolved_conflicts,
                }
                if relation
                else None
            ),
        }

    # ------------------------------------------------------------------ #
    # 消息（SSE 流式）
    # ------------------------------------------------------------------ #
    @app.post("/v1/sessions/{session_id}/messages")
    def send_message(
        session_id: str,
        body: SendMessageRequest,
        repo: Repository = Depends(get_repo),
        orch: Orchestrator = Depends(get_orch),
    ) -> StreamingResponse:
        if repo.get_session(session_id) is None:
            raise HTTPException(status_code=404, detail=f"会话不存在: {session_id}")
        return StreamingResponse(
            _event_stream(orch.send_message(session_id, body.content)),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    # ------------------------------------------------------------------ #
    # 记忆管理
    # ------------------------------------------------------------------ #
    @app.get("/v1/sessions/{session_id}/memories")
    def list_memories(
        session_id: str,
        layer: int | None = None,
        repo: Repository = Depends(get_repo),
    ) -> dict[str, Any]:
        session = repo.get_session(session_id)
        if session is None:
            raise HTTPException(status_code=404, detail=f"会话不存在: {session_id}")
        nodes = repo.list_memories(session.persona_id, session.user_id, layer=layer)
        return {
            "session_id": session_id,
            "count": len(nodes),
            "memories": [_memory_brief(n) for n in nodes],
        }

    @app.get("/v1/sessions/{session_id}/memories/export")
    def export_memories(
        session_id: str,
        fmt: str = "json",
        repo: Repository = Depends(get_repo),
    ) -> Any:
        session = repo.get_session(session_id)
        if session is None:
            raise HTTPException(status_code=404, detail=f"会话不存在: {session_id}")
        nodes = repo.list_memories(session.persona_id, session.user_id)
        if fmt == "markdown":
            lines = [
                f"# {session.persona_id} / {session.user_id} 的记忆",
                "",
                "| 层 | 类型 | 内容 | 来源 |",
                "|---|---|---|---|",
            ]
            for n in nodes:
                content = n.content.replace("|", "\\|")
                lines.append(f"| L{n.layer} | {n.type} | {content} | {n.source_type} |")
            return StreamingResponse(
                iter(["\n".join(lines)]),
                media_type="text/markdown; charset=utf-8",
            )
        return {
            "session_id": session_id,
            "persona_id": session.persona_id,
            "user_id": session.user_id,
            "count": len(nodes),
            "memories": [
                {**_memory_brief(n), "entities": n.entities, "created_at": n.created_at.isoformat()}
                for n in nodes
            ],
        }

    @app.delete("/v1/memories/{memory_id}")
    def delete_memory(
        memory_id: str,
        repo: Repository = Depends(get_repo),
    ) -> dict[str, Any]:
        target = repo.get_memory(memory_id)
        if target is None:
            raise HTTPException(status_code=404, detail=f"记忆不存在: {memory_id}")
        repo.delete_memory(memory_id)
        return {"deleted": memory_id, "content": target.content}

    # ------------------------------------------------------------------ #
    # trace 回溯
    # ------------------------------------------------------------------ #
    @app.get("/v1/traces/{turn_id}")
    def get_trace(turn_id: int, repo: Repository = Depends(get_repo)) -> dict[str, Any]:
        trace = repo.get_trace(turn_id)
        if trace is None:
            raise HTTPException(status_code=404, detail=f"trace 不存在: {turn_id}")
        return trace

    return app


app = create_app()


# --------------------------------------------------------------------------- #
# 入口
# --------------------------------------------------------------------------- #


def main() -> None:
    """`linger` 命令的入口（pyproject.toml 已声明 linger.server.app:main）。"""
    import os

    import uvicorn

    uvicorn.run(
        "linger.server.app:app",
        host=os.environ.get("LINGER_HOST", "127.0.0.1"),
        port=int(os.environ.get("LINGER_PORT", "8000")),
        reload=os.environ.get("LINGER_RELOAD", "").lower() in ("1", "true", "yes"),
    )


if __name__ == "__main__":
    main()
