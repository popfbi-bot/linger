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
from fastapi.responses import HTMLResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, field_validator, model_validator

from ..companion import LIFE_STAGES, correct_life_stage, life_stage_valid
from ..config import settings
from ..llm import get_provider
from ..memory.embedding import get_embedder
from ..models import CompanionProfile, MemoryEditRecord, PersonaCard, new_id, now_utc
from ..orchestrator import Orchestrator
from ..persona.generator import PersonaGenerateError, generate_persona
from ..storage.base import Repository
from ..storage.sqlite import SQLiteRepository

# 记忆正文长度上限：提炼后的短句，500 字足够容纳「我妈妈下周要去医院复查」这类口径。
MEMORY_CONTENT_MAX = 500

# 前端静态资源目录（index.html / app.js / style.css）
_STATIC_DIR = Path(__file__).resolve().parent / "static"

# --------------------------------------------------------------------------- #
# 请求体
# --------------------------------------------------------------------------- #


class CreatePersonaRequest(BaseModel):
    """创建人格：两条入口共用同一个端点（架构 §3.1.3「四条入口统一」）。

    - 入口 A（模板市场）：只给 `template_id`，从预置模板实例化。
    - 入口 B（一句话生成）：把 `/v1/personas/generate` 返回的 `persona` **原样**传回，
      确认落库。服务端会重发 id 并标记 `provenance.type = "generated"`。
    """

    template_id: str | None = Field(default=None, description="人格模板 id，如 linxi / laojiang")
    persona: dict[str, Any] | None = Field(
        default=None, description="完整人格卡（生成预览确认后落库）"
    )

    @field_validator("template_id")
    @classmethod
    def _not_blank(cls, v: str | None) -> str | None:
        """拒绝纯空白。

        `min_length=1` 只挡得住空字符串，挡不住 "   "——
        而空白 template_id 会一路走到存储层才炸，错误码还不受控。
        """
        if v is None:
            return None
        v = v.strip()
        if not v:
            raise ValueError("template_id 不能为空白")
        return v

    @model_validator(mode="after")
    def _at_least_one(self) -> "CreatePersonaRequest":
        """两种模式必须给一个。空 body 不许静默成功。"""
        if not self.template_id and not self.persona:
            raise ValueError("template_id 与 persona 至少给一个")
        return self


class GeneratePersonaRequest(BaseModel):
    """一句话生成人格（入口 B）。"""

    description: str = Field(..., description="一句话描述，如「一个嘴硬心软的退役剑客」")

    @field_validator("description")
    @classmethod
    def _check(cls, v: str) -> str:
        """长度与空白都在入口挡住。

        太短（1 个字）模型只能瞎编；太长（>200）不是「一句话」，
        用户真想要细节应该走五步向导（入口 D）。
        """
        v = v.strip()
        if len(v) < 2:
            raise ValueError("描述至少 2 个字")
        if len(v) > 200:
            raise ValueError("描述不能超过 200 字")
        return v


class CreateSessionRequest(BaseModel):
    persona_id: str = Field(..., min_length=1)
    user_id: str = Field(..., min_length=1)
    # 语义是场景 id（如 rainy_bookstore）。老字段名 `scenario` 保留兼容：
    # M1 的调用方传的是自由设定文本，两者都收，`scenario_id` 优先。
    scenario_id: str | None = None
    scenario: str | None = Field(default=None, description="兼容字段，等同 scenario_id")

    @field_validator("persona_id", "user_id")
    @classmethod
    def _not_blank(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("不能为空白")
        return v.strip()


class SendMessageRequest(BaseModel):
    content: str = Field(..., min_length=1, description="用户这一轮说的话")

    @field_validator("content")
    @classmethod
    def _not_blank(cls, v: str) -> str:
        """拒绝纯空白内容。

        这不只是洁癖：空白消息会走进 send_message，写下一条空 turn，
        并让记忆引擎在空输入上提炼——静默失败，且很难查。
        """
        v = v.strip()
        if not v:
            raise ValueError("content 不能为空白")
        return v


class CorrectLifeStageRequest(BaseModel):
    """用户手动校正人生阶段。

    只开放 `life_stage` 一个字段：roles/needs/risks 是识别结果，
    让用户改它们就等于让用户给自己贴标签——那不是校正，是伪装。
    """

    life_stage: str = Field(..., min_length=1, description="胎儿期/婴儿期/…/老年期")

    @field_validator("life_stage")
    @classmethod
    def _valid_stage(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("life_stage 不能为空白")
        if not life_stage_valid(v):
            raise ValueError(f"未知的人生阶段: {v}（可选：{'、'.join(LIFE_STAGES)}）")
        return v


class UpdateMemoryRequest(BaseModel):
    """用户编辑自己的一条记忆（部分更新）。

    只开放 `content`：用户改的是**她记错了什么**，不是她是谁的来源。
    source_type / layer / created_at 由服务端保留——编辑内容不等于改写历史，
    ai_inferred 的条目被你纠正后，仍然是「当初她推出来的那一条」。
    statistics（mention_count / recall_count / strength）同理不动：
    改口径不该让「被提起过几次」归零。
    """

    content: str = Field(..., description="记忆正文（提炼后的短句，≤500 字）")

    @field_validator("content")
    @classmethod
    def _not_blank(cls, v: str) -> str:
        """拒绝纯空白。

        空白 content 会落进检索，向量近似为零，之后永远召不回也不需要召不回，
        而用户以为「我改好了」——静默失败里最难查的一类。
        """
        v = v.strip()
        if not v:
            raise ValueError("content 不能为空白")
        return v

    @field_validator("content")
    @classmethod
    def _not_too_long(cls, v: str) -> str:
        """记忆是提炼后的短句，不是长文。超长说明这不是一次「纠正」，是另起一条。"""
        if len(v) > MEMORY_CONTENT_MAX:
            raise ValueError(f"content 超过 {MEMORY_CONTENT_MAX} 字上限（实际 {len(v)} 字）")
        return v


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
        # 从未编辑过时回落 created_at：前端只需要一个「这条记忆多新」，
        # 不必为「改过没有」再多一个分支。
        "updated_at": (node.updated_at or node.created_at).isoformat(),
        # 是否被用户编辑过：只有编辑才推进 updated_at，所以这个标记等价于
        # 「有没有编辑历史」。前端据此显示「编辑/历史」按钮。
        "edited": node.updated_at is not None,
    }


def _companion_detail(profile) -> dict[str, Any]:
    """档案序列化。空档案也走这里——不存在与空是同一件事（可选状态）。"""
    return {
        "persona_id": profile.persona_id,
        "user_id": profile.user_id,
        "life_stage": profile.life_stage,
        "roles": list(profile.roles),
        "needs": list(profile.needs),
        "risks": list(profile.risks),
        "is_empty": profile.is_empty,
        "updated_at": profile.updated_at.isoformat(),
    }


def _scenario_detail(scenario) -> dict[str, Any]:
    return {
        "id": scenario.id,
        "title": scenario.title,
        "world": scenario.world,
        "setting": scenario.setting,
        "opening_narration": scenario.opening_narration,
        "user_role": scenario.user_role,
        "beats": [{"id": b.id, "goal": b.goal} for b in scenario.beats],
        "constraints": scenario.constraints,
        "exit_conditions": scenario.exit_conditions,
    }


def _session_scenario_state(repo: Repository, session) -> dict[str, Any]:
    """会话的场景进度摘要（GET /v1/sessions/{id} 与创建响应共用）。

    没有场景 → `{"scenario_id": None}`；老式自由文本 → 只有 scenario_id；
    真场景 → id / title / done_beats / pending_beats。
    """
    from ..scenario import Scenario, load_scenario

    if not session.scenario_id:
        return {"scenario_id": None}
    try:
        scenario = load_scenario(session.scenario_id)
    except FileNotFoundError:
        return {"scenario_id": session.scenario_id}
    if not isinstance(scenario, Scenario):  # pragma: no cover - 防御
        return {"scenario_id": session.scenario_id}
    scenario.restore(repo.get_scenario_progress(session.id))
    return scenario.state()


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
            app.state.embedder = embedder or get_embedder(
                settings.embedder, settings.embedding_dim
            )
            app.state.orch = Orchestrator(repo, llm or get_provider(), app.state.embedder)
            yield
            return

        path = db_path or settings.db_path
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        own_repo = SQLiteRepository(path)
        app.state.repo = own_repo
        app.state.embedder = embedder or get_embedder(
            settings.embedder, settings.embedding_dim
        )
        app.state.orch = Orchestrator(own_repo, llm or get_provider(), app.state.embedder)
        try:
            yield
        finally:
            own_repo.close()

    app = FastAPI(
        title="Linger",
        version="0.1.0",
        description="AI 陪伴人格引擎的 HTTP 服务层：SSE 流式对话 + 记忆管理 + 陪伴档案 + trace 回溯",
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

    def get_embedder_dep() -> Any:
        """编辑记忆要重算向量，所以嵌入器要能被依赖注入拿到。

        必须是**应用实际在用的那一个**：测试注入 FakeEmbedder 时，
        这里也得是它，否则断言「向量等于新文本的向量」就没意义。
        """
        return app.state.embedder

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
    # 场景（剧本）
    # ------------------------------------------------------------------ #
    @app.get("/v1/scenarios")
    def list_scenarios_endpoint() -> dict[str, Any]:
        """场景清单。前端先看有什么可点，再用 scenario_id 开会话。"""
        from ..scenario import list_scenarios

        return {"scenarios": list_scenarios()}

    @app.get("/v1/scenarios/{scenario_id}")
    def get_scenario_endpoint(scenario_id: str) -> dict[str, Any]:
        from ..scenario import load_scenario

        try:
            return _scenario_detail(load_scenario(scenario_id))
        except FileNotFoundError as exc:
            raise HTTPException(status_code=404, detail=f"场景不存在: {scenario_id}") from exc

    # ------------------------------------------------------------------ #
    # 人格
    # ------------------------------------------------------------------ #
    @app.get("/v1/personas")
    def list_personas(repo: Repository = Depends(get_repo)) -> dict[str, Any]:
        return {"personas": [_persona_brief(c) for c in repo.list_personas()]}

    @app.get("/v1/personas/templates")
    def list_persona_templates() -> list[dict[str, Any]]:
        """可实例化的模板清单。

        前端先看有什么可点，再去 POST /v1/personas——
        否则用户只能靠猜 template_id。
        """
        from ..persona.templates import list_templates

        return list_templates()

    @app.post("/v1/personas/generate")
    def generate_persona_endpoint(
        body: GeneratePersonaRequest,
        orch: Orchestrator = Depends(get_orch),
    ) -> dict[str, Any]:
        """一句话描述 → 人格卡提案（**不落库**）。

        返回 `persona` / `warnings` / `preview`。前端展示预览，
        用户确认后把 `persona` 原样 POST 到 `/v1/personas` 落库。

        为什么不生成即入库：用户会反复试描述（「嘴硬心软」改成「嘴硬心更软」），
        每次都入库会把人格库变成草稿箱。
        """
        try:
            result = generate_persona(body.description, orch.llm)
        except PersonaGenerateError as exc:
            # 模型吐了不可用的东西 → 422（是「这次生成不能要」，不是服务端坏了）
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        return result.as_dict()

    @app.post("/v1/personas", status_code=201)
    def create_persona(
        body: CreatePersonaRequest,
        repo: Repository = Depends(get_repo),
        orch: Orchestrator = Depends(get_orch),
    ) -> dict[str, Any]:
        # 入口 B：生成预览确认后落库
        if body.persona is not None:
            try:
                card = PersonaCard(**body.persona)
            except Exception as exc:
                raise HTTPException(status_code=422, detail=f"人格卡不合法：{exc}") from exc
            # id 必须服务端重发：客户端带回来的 id 不可信（可能撞库或被伪造）
            card.id = new_id("p")
            card.version = 1
            card.provenance = {**(card.provenance or {}), "type": "generated"}
            repo.save_persona(card)
            return _persona_brief(card)

        # 入口 A：模板实例化
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

        # scenario_id 优先；老字段 scenario 兼容（可能是 id，也可能是自由文本，
        # 由 Orchestrator 决定怎么解释）。
        requested = body.scenario_id or body.scenario
        try:
            session = orch.create_session(body.persona_id, body.user_id, requested)
        except ValueError as exc:
            # 场景 id 不存在 → 404（不是 500，也不是静默忽略）
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        return {
            "session_id": session.id,
            "persona_id": session.persona_id,
            "user_id": session.user_id,
            "scenario_id": session.scenario_id,
            "scenario": _session_scenario_state(repo, session),
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
            "scenario": _session_scenario_state(repo, session),
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

    @app.patch("/v1/memories/{memory_id}")
    def update_memory(
        memory_id: str,
        body: UpdateMemoryRequest,
        repo: Repository = Depends(get_repo),
        embedder: Any = Depends(get_embedder_dep),
    ) -> dict[str, Any]:
        """编辑一条记忆的正文。

        AI 猜错的时候（`source_type="ai_inferred"`）用户必须能改——
        否则她记错一件事，就会一直错下去。

        三件事必须同时发生，少一件都会让检索与展示分叉：
          1. 重算 embedding：改了口径而向量还是旧的，会把这条记忆召回到
             完全无关的对话里；
          2. 刷新 updated_at：面板要能显示「你什么时候改的」；
          3. 保留来源与分层与统计：编辑内容不是改写历史。
        第四件事是**审计**：编辑前的内容必须留痕（`memory_edit_log`），
        让「她之前记的是什么、你改成了什么」永远可回溯（M2-C 判据 2）。
        """
        target = repo.get_memory(memory_id)
        if target is None:
            raise HTTPException(status_code=404, detail=f"记忆不存在: {memory_id}")

        old_content = target.content
        target.content = body.content
        target.embedding = list(embedder.embed(body.content))
        target.updated_at = now_utc()

        repo.update_memory(target)
        repo.add_memory_edit(
            MemoryEditRecord(
                memory_id=memory_id,
                old_content=old_content,
                new_content=body.content,
            )
        )
        return _memory_brief(target)

    @app.get("/v1/memories/{memory_id}/edits")
    def list_memory_edits(
        memory_id: str,
        repo: Repository = Depends(get_repo),
    ) -> dict[str, Any]:
        """返回一条记忆的编辑历史（新的在前）。

        用于 trace 面板回溯「修改前后的差异」。记忆不存在返回 404，
        与 PATCH/DELETE 保持一致；没有编辑历史返回空列表（不是错误）。
        """
        target = repo.get_memory(memory_id)
        if target is None:
            raise HTTPException(status_code=404, detail=f"记忆不存在: {memory_id}")

        records = repo.list_memory_edits(memory_id)
        return {
            "memory_id": memory_id,
            "edits": [
                {
                    "old_content": r.old_content,
                    "new_content": r.new_content,
                    "edited_at": r.edited_at.isoformat(),
                }
                for r in records
            ],
        }

    # ------------------------------------------------------------------ #
    # 陪伴档案（M2-A）
    # ------------------------------------------------------------------ #
    @app.get("/v1/companion/{persona_id}/{user_id}")
    def get_companion(
        persona_id: str,
        user_id: str,
        repo: Repository = Depends(get_repo),
    ) -> dict[str, Any]:
        """返回档案。不存在时返回**空档案**（200），不是 404。

        档案是可选状态：「还没有」是常态，不是错误。
        客户端拿到 `is_empty: true` 就该显示引导文案，而不是报错页。
        """
        profile = repo.get_companion_profile(persona_id, user_id) or CompanionProfile(
            persona_id=persona_id, user_id=user_id
        )
        return {**_companion_detail(profile), "life_stages": list(LIFE_STAGES)}

    @app.patch("/v1/companion/{persona_id}/{user_id}")
    def patch_companion(
        persona_id: str,
        user_id: str,
        body: CorrectLifeStageRequest,
        repo: Repository = Depends(get_repo),
    ) -> dict[str, Any]:
        """用户手动校正人生阶段。非法阶段值由请求体校验挡成 422。"""
        profile = correct_life_stage(repo, persona_id, user_id, body.life_stage)
        return {**_companion_detail(profile), "life_stages": list(LIFE_STAGES)}

    # ------------------------------------------------------------------ #
    # trace 回溯
    # ------------------------------------------------------------------ #
    @app.get("/v1/traces/{turn_id}")
    def get_trace(turn_id: int, repo: Repository = Depends(get_repo)) -> dict[str, Any]:
        trace = repo.get_trace(turn_id)
        if trace is None:
            raise HTTPException(status_code=404, detail=f"trace 不存在: {turn_id}")
        return trace

    # ------------------------------------------------------------------ #
    # 前端（最小可跑界面）
    # ------------------------------------------------------------------ #
    # 刻意不做构建链：三个静态文件，无 CDN、无框架、无打包。
    # 这一层的目的是「打开就能有感觉」，不是工程完备。
    if _STATIC_DIR.is_dir():
        app.mount("/static", StaticFiles(directory=str(_STATIC_DIR)), name="static")

        @app.get("/", response_class=HTMLResponse, include_in_schema=False)
        def index() -> str:
            return (_STATIC_DIR / "index.html").read_text(encoding="utf-8")

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
