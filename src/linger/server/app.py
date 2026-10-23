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

import base64
import binascii
import json
import secrets
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager
from datetime import date as date_cls
from datetime import datetime
from pathlib import Path
from typing import Any

from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, ValidationError, field_validator, model_validator

from ..accounts import Account
from ..accounts.service import (
    AUTH_FAILED_MESSAGE,
    AccountError,
    authenticate,
    bearer_token,
    claim_anonymous_data,
    register,
    resolve_token,
    token_for,
)
from ..companion import LIFE_STAGES, correct_life_stage, life_stage_valid
from ..config import settings
from ..diary import DiaryGenerateError, generate_diary
from ..distill import PersonaDistillError, TranscriptError, distill_persona, parse_transcript
from ..llm import ResilientProvider, get_provider
from ..llm.base import LLMRuntimeError
from ..market import service as market_service
from ..market.service import MarketError
from ..memory.embedding import get_embedder
from ..models import (
    PAD,
    CompanionProfile,
    DiaryEntry,
    MemoryEditRecord,
    PersonaCard,
    new_id,
    now_utc,
)
from ..orchestrator import Orchestrator
from ..persona.generator import PersonaGenerateError, generate_persona
from ..persona.wizard import PersonaWizardError, preview_draft
from ..storage import get_repository
from ..storage.base import Repository
from ..voice import AudioRejected, VoiceError, get_asr, get_tts, prosody_for, voice_params
from ..voice import fallback_notes as voice_fallback_notes

# 记忆正文长度上限：提炼后的短句，500 字足够容纳「我妈妈下周要去医院复查」这类口径。
MEMORY_CONTENT_MAX = 500

# 聊天记录蒸馏的最少行数（判据 4）：两行够不上「一段对话」，也数不出风格。
MIN_TRANSCRIPT_ROWS = 3

# 前端静态资源目录（index.html / app.js / style.css）
_STATIC_DIR = Path(__file__).resolve().parent / "static"

# 音频判定的最小长度（字节）。4 字节连一个 WAV 头都不够，正常录音不会这么短——
# 所以「空上传」和「把一段 JSON 当音频发」在入口就挡掉，不让它们走到 provider 里。
MIN_AUDIO_BYTES = 4

# 合规红线（§3.1.3）：cloned 卡是「复刻一个真人」，它的声音不许被合成出来。
# 与「cloned 不许上架」同源——那一棒管的是分发，这一棒管的是第二张嘴。
VOICE_BLOCKED_PROVENANCE = "cloned"

# --------------------------------------------------------------------------- #
# 请求体
# --------------------------------------------------------------------------- #


class CreatePersonaRequest(BaseModel):
    """创建人格：两条入口共用同一个端点（架构 §3.1.3「四条入口统一」）。

    - 入口 A（模板市场）：只给 `template_id`，从预置模板实例化。
    - 入口 B（一句话生成）：把 `/v1/personas/generate` 返回的 `persona` **原样**传回，
      确认落库。服务端会重发 id 并标记 `provenance.type = "generated"`。

    `owner`（#39 补）：**声明这个人格归谁**。人格卡本身不带身份，归属只存在
    `provenance.owner` 里。`/v1/personas` 这条创建路径上没有别的字段携带它，
    于是「用户自己造的人格」在市场里没有主人——上架必然 403。
    单个顶层字段而不是让前端自己往 `persona.provenance` 里塞：
    服务端要按当前账号核对它（声称别人 → 403），核对点只有一处。
    """

    template_id: str | None = Field(default=None, description="人格模板 id，如 linxi / laojiang")
    persona: dict[str, Any] | None = Field(
        default=None, description="完整人格卡（生成预览确认后落库）"
    )
    owner: str | None = Field(
        default=None,
        description="归属账号的 user_id。登录时带上（服务端核对必须是本人），匿名时不带",
    )

    @field_validator("owner")
    @classmethod
    def _owner_blank_to_none(cls, v: str | None) -> str | None:
        """空字符串等价于「不给」，免得库里多个 `owner: ""`。

        没 owner 与空 owner 在归属检查里等价（都是「不归谁」），但数据上别留两种写法的空值。
        """
        if v is None:
            return None
        v = v.strip()
        return v or None

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


class DistillPersonaRequest(BaseModel):
    """聊天记录蒸馏（入口 C）。字段校验是**合规红线**，不是参数洁癖。

    `authorized` 必须是 true：复刻一个真人的说话方式，前提是用户拥有这段对话
    的授权或已征得对方同意。这一条挡在 Schema 层——任何走到蒸馏逻辑之前的请求
    都必须先过它，避免以后有人加一个「内部调用」旁路把它绕过去。
    """

    transcript: str = Field(..., description="纯文本聊天记录，至少 3 行非空")
    target_name: str | None = Field(default=None, description="要复刻谁；不给则取发言最多的一方")
    authorized: bool = Field(..., description="必须为 true：拥有对话授权或已获对方同意")

    @field_validator("transcript")
    @classmethod
    def _check_transcript(cls, v: str) -> str:
        if not v or not v.strip():
            raise ValueError("聊天记录不能为空")
        rows = [line for line in v.splitlines() if line.strip()]
        if len(rows) < MIN_TRANSCRIPT_ROWS:
            raise ValueError(f"聊天记录至少 {MIN_TRANSCRIPT_ROWS} 行")
        return v

    @field_validator("target_name")
    @classmethod
    def _blank_to_none(cls, v: str | None) -> str | None:
        """空字符串等价于「不给」。前端输了又删干净时不该当成一个叫 "" 的人。"""
        if v is None:
            return None
        v = v.strip()
        return v or None

    @field_validator("authorized")
    @classmethod
    def _must_authorize(cls, v: bool) -> bool:
        if not v:
            raise ValueError("必须确认拥有这段对话的授权，或已获得对方同意")
        return v


class PreviewPersonaDraftRequest(BaseModel):
    """五步向导的预览请求（入口 D）。**刻意不校验内容**。

    `draft` 收一个任意对象，允许为空（`{}`），也允许只填了几个字段——
    向导第 1 步之前用户什么都还没填，那时前端送来的就是 `{}`；
    如果这里要求必填，用户打开面板第一屏就看到 422。

    字段级的正确性（clamp、区间、缺省）在 `persona.wizard` 里做，
    那里复用了 M3-A 的同一套 Schema 校验——校验只能有一处定义。
    """

    draft: dict[str, Any] | None = Field(
        default=None, description="部分填写的人格卡草稿，如 {'meta': {'name': '沈砚'}}"
    )


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


class CreateScenarioRequest(BaseModel):
    """用户自建场景。

    `beats` 收 `list[Any]` 而不是 `list[Beat]`：允许「只写一句描述」的简写
    （`["第一次见面"]`）。化简逻辑放在 `scenario/writer.py` 的 `_normalize`，
    HTTP 层不承担「两种写法都认」的复杂度。
    """

    id: str = Field(
        ...,
        min_length=2,
        max_length=32,
        description="小写字母/数字/下划线，字母开头，如 rainy_roof",
    )
    title: str = Field(..., min_length=1, max_length=60)
    world: str = Field(default="", max_length=200)
    setting: str = Field(default="", max_length=2000)
    opening_narration: str = Field(default="", max_length=2000)
    user_role: str = Field(default="", max_length=200)
    beats: list[Any] = Field(
        default_factory=list,
        description='[{"goal": "...", "trigger": ["..."]}] 或 ["..."] 两种写法都收',
    )
    constraints: list[str] = Field(default_factory=list)
    exit_conditions: list[str] = Field(default_factory=list)
    overwrite: bool = Field(default=False, description="同 id 已存在时是否覆盖")

    @field_validator("id")
    @classmethod
    def _id_format(cls, v: str) -> str:
        """id 要能当文件名用。格式不对在这里挡掉，别等写文件时才炸。"""
        from ..scenario.writer import SCENARIO_ID_RE

        v = v.strip()
        if not SCENARIO_ID_RE.match(v):
            raise ValueError("id 只能用小写字母、数字、下划线，2~32 位，且以字母开头")
        return v

    @field_validator("title")
    @classmethod
    def _title_not_blank(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("标题不能为空白")
        return v


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


class GenerateDiaryRequest(BaseModel):
    """写下今天。`date` 可选，默认「今天」。

    日期在这里只做**格式**校验（`2026-13-45` → 422）。
    「今天没有对话」是生成层的判断，不是请求体校验——
    那需要查库，属于业务，不属于 schema。
    """

    user_id: str = Field(..., min_length=1)
    date: date_cls | None = Field(default=None, description="YYYY-MM-DD，默认今天")

    @field_validator("user_id")
    @classmethod
    def _not_blank(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("user_id 不能为空白")
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


class ListPersonaRequest(BaseModel):
    """上架请求（判据 1）。`title` / `summary` 空则由服务端回落。"""

    persona_id: str = Field(..., min_length=1, description="要上架的人格 id")
    title: str = Field(default="", max_length=80)
    summary: str = Field(default="", max_length=400)


class ReviewListingRequest(BaseModel):
    """审核请求（判据 5）。动作只允许 approve / reject，别的值在服务层被拒。"""

    action: str = Field(..., description="approve 或 reject")
    note: str = Field(default="", max_length=400)


class RegisterRequest(BaseModel):
    """注册：邮箱 + 密码（判据 1）。

    密码**不在 schema 层做复杂度规则**，只在 service 层校验长度：
    复杂度规则（大小写/符号）对陪伴产品是负价值，用户会写在便签上。
    长度下限 8 挡住的是「随手一试」，那才是真实威胁。
    """

    email: str = Field(..., min_length=3, max_length=254, description="登录邮箱")
    password: str = Field(..., min_length=1, max_length=128, description="至少 8 位")
    display_name: str = Field(default="", max_length=40, description="显示名，可空")


class LoginRequest(BaseModel):
    """登录。字段名与注册一致，前端两个表单可以共用一套取值逻辑。"""

    email: str = Field(..., min_length=1, max_length=254)
    password: str = Field(..., min_length=1, max_length=128)


class ClaimRequest(BaseModel):
    """认领匿名数据（判据 6）。

    `persona_ids` 必填，且**必须显式**：记忆按 `(persona_id, user_id)` 存，
    服务端凭什么知道该搬哪些人格下的数据？扫全库会把别人的搬进来。
    只搬用户当前界面上看得见的那几个人格，是唯一安全的默认。

    `source_user_ids` 可选：不给就由服务端在这些 persona 的会话里找。
    给了也只在这个范围内生效（服务端仍会核对会话归属与身份）。
    """

    persona_ids: list[str] = Field(..., min_length=1, max_length=50)
    source_user_ids: list[str] = Field(default_factory=list, max_length=50)

    @field_validator("persona_ids")
    @classmethod
    def _not_blank(cls, v: list[str]) -> list[str]:
        out = [x.strip() for x in v if x and x.strip()]
        if not out:
            raise ValueError("persona_ids 不能为空")
        return out


def _account_http_error(exc: AccountError) -> HTTPException:
    """AccountError.kind → 状态码。**只有这一处做这个翻译**。

    `auth_failed` 固定 401 + 固定文案：文案若由调用方各自拼，
    迟早会有人拼出「该邮箱未注册」——那判据 3 就破了。
    """
    status = {
        "email_taken": 409,
        "invalid_email": 422,
        "weak_password": 422,
        "invalid_input": 422,
        "auth_failed": 401,
    }.get(exc.kind, 400)
    headers = {"WWW-Authenticate": "Bearer"} if status == 401 else None
    return HTTPException(
        status_code=status,
        detail=AUTH_FAILED_MESSAGE if exc.kind == "auth_failed" else exc.message,
        headers=headers,
    )


def _resolve_auth_secret() -> str:
    """令牌密钥：显式配置优先，否则每进程随机一个。

    随机默认值的代价是「重启后所有令牌作废」（用户被登出），
    但比起「仓库里写死一个密钥所有人共用」，这个代价小得多——
    写死的密钥等于没有签名，任何人都能签出合法令牌。

    所以生产必须配 `LINGER_AUTH_SECRET`；开发与 CI 用随机值即可。
    """
    if settings.auth_secret:
        return settings.auth_secret
    return secrets.token_urlsafe(32)


# --------------------------------------------------------------------------- #
# 序列化
# --------------------------------------------------------------------------- #


def _owner_of_card(card) -> str:
    """人格的归属（`provenance.owner`）。**没有就是空字符串**，不是 None。

    判据 4 的 DOM 断言只看一件事：`owner` 非空且等于 `state.userId`。
    所以这里给的是可以直接比较的字符串，前端不必再判一层 None。
    """
    return str((card.provenance or {}).get("owner") or "").strip()


def _declared_owner(account: Account | None, body: CreatePersonaRequest) -> str | None:
    """这张卡该归属谁（#39 判据 3）。**只认当前账号，不认客户端的主张。**

    三种情况：
      · 没登录 → `None`。匿名卡没有归属，于是一律不可上架——这是后端既定语义
        （归属检查要 `owner == 当前用户`），前端因此**不写** owner。
      · 登录且声明了自己的 id → 用它。
      · 登录却声明了别人的 id → 403。这不是「参数错了」：声称别人的人格，
        正是市场归属检查要拦的那件事。**在落库之前拦**，不留半张卡。
    """
    if account is None:
        return None
    if body.owner is not None and body.owner != account.user_id:
        raise HTTPException(status_code=403, detail="不能把人格归属声明给别人")
    return account.user_id


def _stamp_owner(card: PersonaCard, owner: str | None) -> PersonaCard:
    """把归属写进 `provenance.owner`，其余 provenance 字段原样保留。

    为什么不覆盖整份 provenance：`type` / `template` / `origin` 都是可审计的事实
    （「这张卡从哪来」），落库这一步只该**补**一件它知道的事——主人是谁。

    `owner is None`（匿名）时一个字段都不动：匿名卡保持「没有传承」的原样，
    而不是被写成一个空字符串。
    """
    if owner:
        card.provenance = {**(card.provenance or {}), "owner": owner}
    return card


def _persona_brief(card) -> dict[str, Any]:
    """人格摘要。**带 owner 与 provenance**（#39 补）。

    为什么要露 owner：判据 4 要求「上架入口只出现在自己的卡上」，
    而前端唯一能据以判断的就是这个字段。让前端去猜（比如「我列表里的都是我的」）
    在模板实例化共享人格的情况下是错的——那一行可能是别人先认领的。
    provenance 一起回：上架按钮还要靠 `type == cloned` 提前置灰，
    不必等点了才吃一个 403。
    """
    return {
        "id": card.id,
        "name": card.name,
        "identity": card.meta.identity,
        "archetype": card.meta.archetype,
        "version": card.version,
        "owner": _owner_of_card(card),
        "provenance": dict(card.provenance or {}),
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


def _diary_detail(entry: DiaryEntry) -> dict[str, Any]:
    return {
        "id": entry.id,
        "persona_id": entry.persona_id,
        "user_id": entry.user_id,
        "date": entry.date.isoformat(),
        "content": entry.content,
        "mood_label": entry.mood_label,
        "highlight_memories": list(entry.highlight_memories),
        "created_at": entry.created_at.isoformat(),
    }


def _today() -> date_cls:
    """「今天」按**本地时区**算，不是 UTC。

    日记是给人看的：北京时间凌晨 1 点写的，用户认的是那天，
    而不是 UTC 还停留在昨天的那个日期。差一天会让「今天」的日记落到昨天，
    用户点「写下今天」看到的是昨天那篇。
    """
    return datetime.now().astimezone().date()


def _turns_of_day(repo: Repository, persona_id: str, user_id: str, day: date_cls) -> list:
    """当天该 (persona_id, user_id) 的全部 turn。

    turn 表只按 session 建索引、不带 persona/user，所以先按人筛会话再取轮次。
    会话数量是「一个人和她的所有会话」，量级小；真到需要时再加冗余列与迁移。
    """
    out = []
    for session in repo.list_sessions(persona_id, user_id):
        for turn in repo.list_turns(session.id, limit=1000):
            if turn.created_at.astimezone().date() == day:
                out.append(turn)
    out.sort(key=lambda t: (t.created_at, t.id))
    return out


def _scenario_detail(scenario) -> dict[str, Any]:
    return {
        "id": scenario.id,
        "title": scenario.title,
        "world": scenario.world,
        "setting": scenario.setting,
        "opening_narration": scenario.opening_narration,
        "user_role": scenario.user_role,
        # trigger 也返回：自建场景后用户要能确认「我写的触发词存进去了」
        # （前端进度面板只用 id/goal，多这两个字段不增加它的负担）
        "beats": [
            {"id": b.id, "goal": b.goal, "trigger": list(b.trigger), "done": b.done}
            for b in scenario.beats
        ],
        "constraints": scenario.constraints,
        "exit_conditions": scenario.exit_conditions,
    }


def _scenario_source(scenario_id: str) -> str:
    """场景来源（user / builtin）。详情里带出来，前端据此决定给不给编辑入口。

    找不到文件（理论不该发生，load 已成功）时保守返回 builtin——
    宁可少给编辑入口，不能错给。
    """
    from ..scenario.loader import _source_of
    from ..scenario.writer import builtin_scenario_dir, user_scenario_dir

    for base in (user_scenario_dir(), builtin_scenario_dir()):
        p = base / f"{scenario_id}.yaml"
        if p.exists():
            return _source_of(p)
    return "builtin"


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


def _sse_error_frame(exc: Exception) -> str:
    """流已经建立之后模型才坏：只能发一个 error 事件再结束。

    状态码改不了了（响应头早就发出去了），所以这里**不能**假装成功，
    也不能无声地断流——断流会让前端一直转圈。给一个明确的 error 事件，
    前端才能说人话：「这次没接上，再试一下」。
    """
    return _sse_frame(
        "error",
        {"stage": "llm", "kind": type(exc).__name__, "message": str(exc) or type(exc).__name__},
    )


def _first_event(events: Iterator[Any]) -> tuple[Any | None, Iterator[Any]]:
    """从事件流里取第一个**能证明模型活着**的事件，剩下的原样留下。

    为什么要在建立 SSE 之前先取：模型全挂时，我们要能返回 503 而不是
    「200 + 流里报错」。用户看到 200 更困惑——200 意味着「我等，就会有字」。

    Orchestrator 是生成器，并且在第一次触到 `self.llm.generate(...)` 之前
    只会产出 `thinking` / `affect` 这类「准备工作完成」的事件（§5.3 的固定序列
    `thinking → affect → delta*`）。所以在看到第一个 `delta` 之前，
    模型还没被调用；一旦看到 `delta`，这一轮就已经「开始吐字」，不能再改状态码。

    前半段先崩（如会话状态坏了）就照旧抛——那不是模型的问题。
    """
    buffered: list[Any] = []
    for event in events:
        buffered.append(event)
        if getattr(event, "type", None) == "delta":
            return buffered, events
    # 流里没有 delta：要么空回复，要么已经失败（异常已在迭代中抛出）
    if not buffered:
        return None, iter(())
    return buffered, events


class TTSRequest(BaseModel):
    """合成请求。

    `user_id` 决定读哪一份情绪状态（PAD 是按 `(persona_id, user_id)` 存的）；
    不给就当匿名用户——于是「没聊过天」的请求自然落在中性的基线上，
    不需要在这里编一个默认 PAD。
    """

    text: str = Field(..., description="要说的话")
    persona_id: str = Field(..., description="用谁的嗓音/语体说")
    user_id: str = Field(default="", description="读谁的情绪状态。不给按匿名用户处理")

    @field_validator("text")
    @classmethod
    def _not_blank(cls, v: str) -> str:
        # 空白文本合成出来是一段静音，用户点「播放」听不到任何东西——
        # 那是比报错更糟的反馈（他会以为坏了，然后一直点）。
        if not v or not v.strip():
            raise ValueError("text 不能为空")
        if len(v) > TTS_TEXT_MAX:
            raise ValueError(f"text 不能超过 {TTS_TEXT_MAX} 字")
        return v

    @field_validator("persona_id")
    @classmethod
    def _persona_not_blank(cls, v: str) -> str:
        v = (v or "").strip()
        if not v:
            raise ValueError("persona_id 不能为空")
        return v


class ASRJsonRequest(BaseModel):
    """base64 形态的识别请求（与 multipart 并存，见 `post_asr` 的说明）。"""

    audio_base64: str = Field(default="", description="音频字节的 base64 编码")
    language: str | None = Field(default=None, description="语言提示，如 zh / en；不给由 provider 判断")


TTS_TEXT_MAX = 2000


def _decode_b64(raw: str) -> bytes:
    """宽松的 base64 解码：容忍 data URL 前缀与缺失的 padding。

    为什么要宽松：前端用 `FileReader.readAsDataURL` 拿到的是
    `data:audio/webm;base64,xxx`，而各家小程序 / 脚本拼出来的字符串
    padding 时有时无。这不是「参数校验可以不严」——真正的校验在其后
    （长度、头部字节），base64 本身的宽容只是不让人卡在格式细节上。
    """
    s = (raw or "").strip()
    if s.startswith("data:"):
        comma = s.find(",")
        s = s[comma + 1 :] if comma != -1 else ""
    s = "".join(s.split())
    if not s:
        return b""
    pad = (-len(s)) % 4
    try:
        return base64.b64decode(s + "=" * pad, validate=False)
    except (binascii.Error, ValueError) as exc:
        raise AudioRejected("audio_base64 不是合法的 base64") from exc


def _looks_like_audio(data: bytes) -> bool:
    """头部字节判定：只认「明显是音频容器」的几种。

    不解析到底——那是 provider 的事。这里只挡「把一段 JSON / 一张图片当音频发」，
    因为那种输入走到 provider 里只会换来一个更难解释的错误。
    判定宽松是刻意的：宁可让 provider 对一个可疑容器报错，也不要因为
    某个新格式（比如 opus in ogg）不在这里的名单上就把用户挡在门外。
    """
    if len(data) < MIN_AUDIO_BYTES:
        return False
    head = data[:12]
    return (
        head[:4] in (b"RIFF", b"OggS", b"fLaC", b"FORM")  # wav / ogg / flac / aiff
        or head[:4] == b"\x1aE\xdf\xa3"  # webm / mkv（MediaRecorder 的默认格式）
        or head[4:8] == b"ftyp"  # mp4 / m4a
        or head[:3] == b"ID3"  # mp3（带 ID3 头）
        or head[:2] in (b"\xff\xfb", b"\xff\xf3", b"\xff\xf2")  # mp3 裸帧
    )


async def _json_body(request: Any) -> dict[str, Any] | None:
    """读 JSON body，读不出来就返回 None（由调用方决定是 422 还是别的）。

    `request.json()` 在空 body 上会抛；空 body 与「不是 JSON」在这里等价——
    对调用方都是「你没给音频」。
    """
    raw = await request.body()
    if not raw.strip():
        return None
    try:
        payload = json.loads(raw)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


def _validation_detail(exc: Any) -> str:
    """pydantic 的报错压成一句话：前端只需要知道「哪个字段不对」。"""
    errors = getattr(exc, "errors", lambda: [])()
    parts = []
    for e in errors:
        loc = ".".join(str(x) for x in e.get("loc", ())) or "body"
        parts.append(f"{loc}: {e.get('msg', '不合法')}")
    return "；".join(parts) or "请求体不合法"


def _synthesize_audio(
    tts: Any,
    text: str,
    *,
    voice: dict[str, Any],
    pad: Any,
) -> bytes:
    """把「说这句话」集中在这里：HTTP 层与测试都走同一条路。

    prosody 由 PAD 现算，不缓存：同一句「我在」，她心情不同就该说得不同。
    """
    return tts.synthesize(text, voice=voice, prosody=prosody_for(pad))


# --------------------------------------------------------------------------- #
# 应用工厂
# --------------------------------------------------------------------------- #


def create_app(
    repo: Repository | None = None,
    llm: Any | None = None,
    embedder: Any | None = None,
    db_path: Path | None = None,
    auth_secret: str | None = None,
    asr: Any | None = None,
    tts: Any | None = None,
) -> FastAPI:
    """构造 FastAPI 应用。

    传入 repo/llm/embedder 时直接复用（测试用）；
    不传则在 lifespan 里按 settings 创建（生产/dev 用）。

    `auth_secret`：令牌自签密钥。不传就用 `settings.auth_secret`，
    它也为空时**每个进程随机生成一个**——见 `_resolve_auth_secret`。

    `asr` / `tts`：语音 provider。不传则按 `LINGER_ASR` / `LINGER_TTS` 装配
    （默认 mock）。测试插入自己的实时 mock 就是为了断言「provider 收到了什么参数」——
    那些参数是韵律判据唯一可断言的证据。
    """
    secret = auth_secret or _resolve_auth_secret()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        # provider 在装配时就定下来，并挂在 app.state 上：
        # `/v1/llm/health` 要读它的熔断状态与最近一次调用的观测数据，
        # 而 Orchestrator 只认 LLMProvider 协议（韧性与观测都在协议之外）。
        app.state.llm = llm or get_provider()
        # 语音 provider 同样在装配时定下来：`/v1/voice/health` 要读它，
        # 而端点只认 `ASRProvider` / `TTSProvider` 协议（不认具体实现）。
        app.state.asr = asr if asr is not None else get_asr()
        app.state.tts = tts if tts is not None else get_tts()

        # 外部注入的 repo 由调用方负责关闭，这里不接管
        if repo is not None:
            app.state.repo = repo
            app.state.embedder = embedder or get_embedder(
                settings.embedder, settings.embedding_dim
            )
            app.state.orch = Orchestrator(repo, app.state.llm, app.state.embedder)
            yield
            return

        # 后端由配置决定（sqlite / postgres），这里不认识具体实现——
        # 业务层一旦 import 了某个具体 Repository，换存储就得改业务代码（判据 8）。
        # db_path 只在 sqlite 后端有意义，故只在显式传入时覆盖配置。
        if db_path is not None:
            settings.db_path = Path(db_path)
        own_repo = get_repository()
        app.state.repo = own_repo
        app.state.embedder = embedder or get_embedder(
            settings.embedder, settings.embedding_dim
        )
        app.state.orch = Orchestrator(own_repo, app.state.llm, app.state.embedder)
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

    # ------------------------------------------------------------------ #
    # 身份：可选 Bearer 令牌（判据 4 / 5 的边界就在这里）
    # ------------------------------------------------------------------ #
    def _resolve_actor(authorization: str | None) -> Account | None:
        token = bearer_token(authorization)
        if token is None:
            return None
        return resolve_token(app.state.repo, token, secret)

    def get_actor(
        authorization: str | None = Header(default=None, alias="Authorization"),
    ) -> Account | None:
        """带令牌就认出是谁，不带就返回 None。

        **这是判据 5 的实现方式，也是整个改动的红线**：
        它不抛 401。没有令牌 → None → 走原来的匿名逻辑，行为与今天逐字节相同。
        令牌无效/过期 → 也返回 None，同样退回匿名玩法。

        为什么不在这里 401：`Authorization` 是**可选**头，一旦它坏掉就让请求失败，
        那么「换了台设备、令牌过期了，连匿名聊天都用不了」——用户看到的是一堵墙。
        需要强制登录的端点（只有 `/v1/auth/me`）自己调 `require_actor`。
        """
        return _resolve_actor(authorization)

    def require_actor(
        authorization: str | None = Header(default=None, alias="Authorization"),
    ) -> Account:
        """必须是一个有效账号。伪造 / 过期 / 缺失令牌一律 401，**绝不是 500**（判据 4）。

        只给「本来就没有匿名玩法」的端点用：`/v1/auth/me`、`/v1/auth/claim`。
        """
        account = _resolve_actor(authorization)
        if account is None:
            raise HTTPException(
                status_code=401,
                detail="未登录或登录已过期",
                headers={"WWW-Authenticate": "Bearer"},
            )
        return account

    async def optional_actor(
        authorization: str | None = Header(default=None, alias="Authorization"),
    ) -> Account | None:
        """供**信任调用方 user_id** 的匿名端点使用：有令牌就核对，没有就不管。

        两件事：
        1. 带了令牌且有效 → 要求请求体里的 `user_id` 必须是这个账号的。
           否则「登录了却还在往别人的 user_id 里写数据」——那是跨账号写。
        2. 带了令牌但无效 → 401。这看上去与"判据 5"冲突，其实不是：
           判据 5 说的是**不带令牌时行为不变**（匿名照旧），
           不是「带一个假令牌也当匿名」。带假令牌是明确的攻击信号。

        写成 async：FastAPI 对 async 依赖与 sync 依赖一视同仁，
        但这里将来若要读库/打日志，async 不会把线程池吃掉。
        """
        account = _resolve_actor(authorization)
        if account is None and bearer_token(authorization) is not None:
            raise HTTPException(
                status_code=401,
                detail="令牌无效或已过期",
                headers={"WWW-Authenticate": "Bearer"},
            )
        return account

    def actor_user_id(account: Account | None) -> str | None:
        """账号的 user_id（没登录就是 None）。只做取值，不判空。"""
        return account.user_id if account is not None else None

    def ensure_user(
        account: Account | None, requested: str, *, allow_anonymous: bool = True
    ) -> str:
        """把「请求里的 user_id」与「令牌里的 user_id」对齐。

        三种情况：
          · 没登录（account is None）→ 原样返回 requested。**匿名玩法不变**（判据 5）。
            调用方若不允许匿名，自己在此之前就 401 了。
          · 登录了且 requested 就是自己的 → 返回它。
          · 登录了但 requested 是别人的 → 409。

        为什么是 409 而不是悄悄改成自己的 id：静默改写会让「你以为在跟林汐聊、
        其实写进了另一个账号的记忆」这种 bug 永远查不出来。宁可报错。

        为什么允许 requested 为空/不等于账号：前端登录后会把 localStorage 里的
        user_id 换成账号的，但**其它客户端**（脚本、旧标签页）未必。
        所以这里给的是「不匹配就报错」而不是「不匹配就猜」。
        """
        if account is None:
            return requested
        if requested and requested != account.user_id:
            raise HTTPException(
                status_code=409,
                detail=(
                    "登录身份与请求里的 user_id 不一致："
                    f"当前账号是 {account.user_id}，请求写的是 {requested}"
                ),
            )
        return account.user_id

    def get_orch() -> Orchestrator:
        return app.state.orch

    def get_llm() -> Any:
        """当前在用的 provider。`/v1/llm/health` 需要看它的内部状态。"""
        return app.state.llm

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
    # 模型健康（M4-A）
    # ------------------------------------------------------------------ #
    @app.get("/v1/llm/health")
    def llm_health(llm: Any = Depends(get_llm)) -> dict[str, Any]:
        """模型层的观测端点：谁在用、谁在熔断、上一次调用怎么样。

        刻意不做成 404：`LINGER_LLM=mock` 时也返回 200（`circuits: []`）——
        前端可以无条件轮询这个端点，不用先判断自己有没有配模型。
        「没有韧性层」本身就是一种需要被看到的事实。

        这里读的是进程内的即时状态，不查库、不打外部接口：
        健康检查自己不能成为新的故障点。
        """
        if not isinstance(llm, ResilientProvider):
            return {
                "provider": getattr(llm, "name", "unknown"),
                "circuits": [],
                "last_call": None,
            }

        providers = llm.providers
        report = llm.last_report()
        return {
            "provider": llm.name,
            "primary": getattr(providers[0], "name", "unknown"),
            "fallbacks": [getattr(p, "name", "unknown") for p in providers[1:]],
            "circuits": llm.circuits(),
            "last_call": {
                "ok": report["ok"],
                "degraded": report["degraded"],
                "provider": report["provider"],
                "elapsed_ms": report["elapsed_ms"],
                "first_chunk_ms": report["first_chunk_ms"],
            },
            # 下面几项是给排障用的，前端可以忽略
            "last_error": report["error"],
            "attempts": report["attempts"],
            "skipped": report["skipped"],
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
            detail = _scenario_detail(load_scenario(scenario_id))
        except FileNotFoundError as exc:
            raise HTTPException(status_code=404, detail=f"场景不存在: {scenario_id}") from exc
        # 编辑前预填表单要用：前端只对 source=user 的场景给「编辑」入口
        detail["source"] = _scenario_source(scenario_id)
        return detail

    @app.post("/v1/scenarios", status_code=201)
    def create_scenario_endpoint(body: CreateScenarioRequest) -> dict[str, Any]:
        """用户自建场景 → 写成 YAML（落在用户目录，不污染包内资源）。

        错误码分工：
          · 422 —— 用户填错了（id 格式、标题为空、节拍描述空…）
          · 409 —— 同 id 已存在且没说要覆盖（让用户自己想清楚是不是改名）
        """
        from ..scenario import load_scenario
        from ..scenario.writer import ScenarioExistsError, ScenarioWriteError, save_scenario

        payload = body.model_dump(exclude={"overwrite"})
        try:
            save_scenario(payload, overwrite=body.overwrite)
        except ScenarioExistsError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except ScenarioWriteError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

        return _scenario_detail(load_scenario(body.id))

    @app.delete("/v1/scenarios/{scenario_id}")
    def delete_scenario_endpoint(scenario_id: str) -> dict[str, Any]:
        """删除**用户自建**场景。内置场景删不掉（返回 404，不是 403——
        对调用方而言「你删不了它」和「它不在你能删的范围里」是同一件事）。"""
        from ..scenario.writer import delete_scenario

        if not delete_scenario(scenario_id):
            raise HTTPException(
                status_code=404,
                detail=f"用户场景不存在或内置场景不可删: {scenario_id}",
            )
        return {"deleted": scenario_id}

    # ------------------------------------------------------------------ #
    # 账号（M3-H）
    # ------------------------------------------------------------------ #
    def _token_response(account: Account) -> dict[str, Any]:
        """注册与登录共用的响应体。两处必须长得一模一样——
        前端只写一份「存 token、换 user_id」的代码。"""
        return {
            "token": token_for(account, secret),
            "token_type": "Bearer",
            "expires_in": int(settings.auth_token_ttl),
            "user_id": account.user_id,
            **account.public_dict(),
        }

    @app.post("/v1/auth/register", status_code=201)
    def register_endpoint(
        body: RegisterRequest,
        repo: Repository = Depends(get_repo),
    ) -> dict[str, Any]:
        """注册 → 201 + 稳定 user_id + 令牌（判据 1）。

        同一邮箱再来一次 → 409。判定由存储层的 UNIQUE 完成（见 Repository.create_account），
        这里只翻错误类型——不做「先查再插」，那在并发下会漏。

        为什么注册直接给令牌：注册完还要再登录一次是纯噪音，
        而「邮箱是我的」这件事在本棒没有验证流程（不做邮箱验证是 issue 的边界）。
        """
        try:
            account = register(repo, body.email, body.password, body.display_name)
        except AccountError as exc:
            raise _account_http_error(exc) from exc
        return _token_response(account)

    @app.post("/v1/auth/login")
    def login_endpoint(
        body: LoginRequest,
        repo: Repository = Depends(get_repo),
    ) -> dict[str, Any]:
        """登录 → 200 + 令牌；失败 → 401，**邮箱不存在与密码错完全一致**（判据 3）。

        「一致」包括三处，任何一处的差异都能被用来枚举用户：
          · 状态码（都是 401）
          · 响应体（都是 `{"detail": "邮箱或密码不正确"}`）
          · 耗时（不存在的邮箱也跑一次假 scrypt，见 accounts.security.dummy_verify）
        """
        try:
            account = authenticate(repo, body.email, body.password)
        except AccountError as exc:
            raise _account_http_error(exc) from exc
        return _token_response(account)

    @app.get("/v1/auth/me")
    def me_endpoint(account: Account = Depends(require_actor)) -> dict[str, Any]:
        """我是谁。前端刷新页面后靠它判断「token 还有效吗」。

        这是全站**唯一**强制登录的读端点：它存在的意义就是「确认身份」，
        没有匿名语义可言。
        """
        return account.public_dict()

    @app.post("/v1/auth/claim")
    def claim_endpoint(
        body: ClaimRequest,
        account: Account = Depends(require_actor),
        repo: Repository = Depends(get_repo),
    ) -> dict[str, Any]:
        """把匿名期间的记忆 / 日记 / 会话迁到账号名下（判据 6）。

        **必须登录**：认领的目标是「我的账号」，没有账号就无从谈起。
        匿名调用返回 401，而不是「悄悄什么都不做」。

        `persona_ids` 来自请求：服务端只在这个范围内搬，
        不会去扫「这个 user_id 在别人的人格下也有数据」——
        那样等于给了一个读别人记忆的接口。
        """
        result = claim_anonymous_data(
            repo,
            account.user_id,
            body.persona_ids,
            body.source_user_ids or None,
        )
        return result.as_dict()

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
        except LLMRuntimeError as exc:
            # 模型层挂了（全不可用 / 超时）→ 503：用户改描述也没用，等一会儿再来
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        return result.as_dict()

    @app.post("/v1/personas/distill")
    def distill_persona_endpoint(
        body: DistillPersonaRequest,
        orch: Orchestrator = Depends(get_orch),
    ) -> dict[str, Any]:
        """聊天记录 → 人格卡提案（**不落库**，入口 C）。

        与 `/v1/personas/generate` 同构：先给提案，用户确认后再 POST `/v1/personas`。
        区别在结果的来源——这里 `voice` 是**从原文数出来的**（确定性指纹），
        模型只补 `meta` / `traits` / `values`。`fingerprint` 原样返回给前端，
        让用户能核对「我们到底统计出了什么」，而不是只能相信黑箱。

        合规红线（§3.1.3）：`authorized` 由请求体校验强制为 true；
        产物 `provenance.type = "cloned"`，不允许上架模板市场。
        """
        try:
            lines = parse_transcript(body.transcript)
            result = distill_persona(lines, body.target_name, orch.llm)
        except (TranscriptError, PersonaDistillError) as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        except LLMRuntimeError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        return result.as_dict()

    @app.post("/v1/personas/draft/preview")
    def preview_persona_draft_endpoint(
        body: PreviewPersonaDraftRequest | None = None,
        orch: Orchestrator = Depends(get_orch),
    ) -> dict[str, Any]:
        """向导草稿 → 完整人格卡预览（**不落库**，入口 D）。

        为什么要有这个端点：向导的价值是「我看得见每个字段」，那就必须能
        「填到一半就看效果」。没有它，用户只能走到第 5 步落库后才知道
        自己填出来的语气对不对——那已经不是向导，是盲填。

        与 `/v1/personas/generate` 的三处不同：
          · 输入是**结构化草稿**而不是一句话描述（用户已经选好了每一项）
          · 预览只有 3 条台词（向导会反复预览，6 条太慢太贵）
          · 草稿可以残缺，空 `{}` 也返回 200（第 1 步之前就是这个状态）

        相同的是纪律：`resolve_conflicts` 是同一份实现，`draft` 不落库。
        """
        draft = (body.draft if body is not None else None) or {}
        try:
            result = preview_draft(draft, orch.llm)
        except PersonaWizardError as exc:
            # 草稿结构坏到补不出卡（比如 meta 不是对象）→ 422，不是 500
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        return result.as_dict()

    @app.post("/v1/personas", status_code=201)
    def create_persona(
        body: CreatePersonaRequest,
        repo: Repository = Depends(get_repo),
        orch: Orchestrator = Depends(get_orch),
        account: Account | None = Depends(optional_actor),
    ) -> dict[str, Any]:
        """创建人格。**可选登录**：匿名玩法不变，登录只多一件事——记下主人是谁。

        归属是市场的前置：`market/service.py::create_listing` 要求
        `provenance.owner == 当前 user_id`，而落库这一步原先只补 `type`。
        于是「用户自己造的人格」在市场里全是无主的，上架必然 403（#39 的判据 3）。
        """
        owner = _declared_owner(account, body)

        # 入口 B：生成预览确认后落库
        if body.persona is not None:
            try:
                card = PersonaCard(**body.persona)
            except Exception as exc:
                raise HTTPException(status_code=422, detail=f"人格卡不合法：{exc}") from exc
            # id 必须服务端重发：客户端带回来的 id 不可信（可能撞库或被伪造）
            card.id = new_id("p")
            card.version = 1
            # provenance.type 由**调用方**决定：入口 C（聊天记录蒸馏）传 cloned，
            # 入口 B（一句话生成）不传则默认 generated。
            # 服务端只补缺省，不覆盖——「这张卡从哪来」是可审计的事实，
            # 不能因为落库这一步又把它抹平成 generated。
            incoming_type = str((card.provenance or {}).get("type") or "").strip()
            card.provenance = {
                **(card.provenance or {}),
                "type": incoming_type or "generated",
            }
            repo.save_persona(_stamp_owner(card, owner))
            return _persona_brief(card)

        # 入口 A：模板实例化
        try:
            card = orch.persona_factory.create(body.template_id, repo)
        except (FileNotFoundError, KeyError, ValueError) as exc:
            raise HTTPException(status_code=404, detail=f"人格模板不存在: {body.template_id}") from exc
        # 模板实例化是**幂等**的：第一个人点「林汐」时建了那一行，后面所有人点都拿到它。
        # 已经有人认领的卡不会被后来者改写主人（否则谁点一下就把别人的卡抢走了）；
        # 无主的那一张由**第一个登录用户**认领——这正是「点了一个模板，然后上架」的路。
        if owner and not _owner_of_card(card):
            repo.save_persona(_stamp_owner(card, owner))
        return _persona_brief(card)

    @app.delete("/v1/personas/{persona_id}")
    def delete_persona(persona_id: str, repo: Repository = Depends(get_repo)) -> dict[str, Any]:
        """删除自己造的人格（M3-C）。**软删除**。

        三件事说清楚：

        1. 删的是「这个人格」，不是「这段历史」。只把 persona.status 标为 deleted，
           memory_node / turn / session 一行不动——用户说的只是「她我不要了」，
           不是「把我们聊过的都抹掉」。误删也还有翻库恢复的余地。
        2. 已删的再删给 404，不做幂等 200。幂等看起来「更友好」，
           但它让用户看不出自己删了两遍，前端也无法用状态码判断列表该不该刷新。
        3. 删掉模板人格后，再点同一个模板会**重新实例化一个新人格**——
           这是刻意的「重置这段关系」：新 id、记忆从零，不是把旧记忆接回来。
           保证这一点的是 find_persona_by_template 过滤 status，见 storage 层注释。
        """
        card = repo.delete_persona(persona_id)
        if card is None:
            # 不存在与「已删除」同码：两种情况对用户都是「没有一个可删的人格」
            raise HTTPException(status_code=404, detail=f"人格不存在: {persona_id}")
        return {"deleted": card.id, "name": card.name}

    # ------------------------------------------------------------------ #
    # 会话
    # ------------------------------------------------------------------ #
    @app.post("/v1/sessions", status_code=201)
    def create_session(
        body: CreateSessionRequest,
        repo: Repository = Depends(get_repo),
        orch: Orchestrator = Depends(get_orch),
        account: Account | None = Depends(optional_actor),
    ) -> dict[str, Any]:
        if repo.get_persona(body.persona_id) is None:
            raise HTTPException(status_code=404, detail=f"人格不存在: {body.persona_id}")

        # 登录后 user_id 必须与账号一致；未登录时原样使用（判据 5）。
        user_id = ensure_user(account, body.user_id)

        # scenario_id 优先；老字段 scenario 兼容（可能是 id，也可能是自由文本，
        # 由 Orchestrator 决定怎么解释）。
        requested = body.scenario_id or body.scenario
        try:
            session = orch.create_session(body.persona_id, user_id, requested)
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
    ) -> Any:
        """一轮对话（SSE 流式）。

        错误码分两种情况，区别只在「流有没有建立」：

          · 首 chunk 前就全挂了 → **503 + detail**。
            用户还没看到任何字，这时候说「服务暂时不可用」是诚实的；
            给 200 再在流里报错，用户看到转圈更困惑。
          · 已经吐过字之后断了 → 只能发一个 `error` 事件再结束。
            SSE 已经 200，改不了状态码，所以至少要把话说清楚。
        """
        if repo.get_session(session_id) is None:
            raise HTTPException(status_code=404, detail=f"会话不存在: {session_id}")

        events = orch.send_message(session_id, body.content)
        try:
            buffered, rest = _first_event(events)
        except LLMRuntimeError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc

        def stream() -> Iterator[str]:
            try:
                yield from (_sse_frame(e.type, e.data) for e in buffered)
                yield from _event_stream(rest)
            except LLMRuntimeError as exc:
                yield _sse_error_frame(exc)

        return StreamingResponse(
            stream(),
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
        account: Account | None = Depends(optional_actor),
    ) -> dict[str, Any]:
        """返回档案。不存在时返回**空档案**（200），不是 404。

        档案是可选状态：「还没有」是常态，不是错误。
        客户端拿到 `is_empty: true` 就该显示引导文案，而不是报错页。
        """
        user_id = ensure_user(account, user_id)
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
        account: Account | None = Depends(optional_actor),
    ) -> dict[str, Any]:
        """用户手动校正人生阶段。非法阶段值由请求体校验挡成 422。"""
        user_id = ensure_user(account, user_id)
        profile = correct_life_stage(repo, persona_id, user_id, body.life_stage)
        return {**_companion_detail(profile), "life_stages": list(LIFE_STAGES)}

    # ------------------------------------------------------------------ #
    # 日记（M3-D）
    # ------------------------------------------------------------------ #
    @app.post("/v1/personas/{persona_id}/diary/generate")
    def generate_diary_endpoint(
        persona_id: str,
        body: GenerateDiaryRequest,
        repo: Repository = Depends(get_repo),
        orch: Orchestrator = Depends(get_orch),
        account: Account | None = Depends(optional_actor),
    ) -> dict[str, Any]:
        """让「她」写下今天。**同一天重复调用是覆盖**，不是新增。

        错误码的分工是刻意的：
          · 人格不存在 → 404（调用方找错了人，不是内容有问题）
          · 日期非法 → 422（由请求体校验挡下，如 2026-13-45）
          · 今天没有对话 → 422（DiaryGenerateError）——不是 500。
            这是「这次生成不能要」，不是服务端坏了；编一篇「今天什么都没发生」
            更糟，用户会以为她真的记得一个空白的日子。

        日记不在对话链路里（不是 orchestrator 的一环）：这里只是读当天已有的
        turn / 记忆 / 情绪，走一次独立的生成。所以它不会拖慢任何一轮对话。
        """
        card = repo.get_persona(persona_id)
        if card is None:
            raise HTTPException(status_code=404, detail=f"人格不存在: {persona_id}")

        user_id = ensure_user(account, body.user_id)
        day = body.date or _today()
        turns = _turns_of_day(repo, persona_id, user_id, day)
        affect = repo.get_affect(persona_id, user_id)
        affect_label = (affect.label if affect else "") or "中性"
        memories = repo.list_memories(persona_id, user_id, limit=50)

        try:
            entry, warnings = generate_diary(
                card, user_id, day, turns, memories, affect_label, orch.llm
            )
        except DiaryGenerateError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        except LLMRuntimeError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc

        # 重写当天那篇时沿用原 id：日期没变，这篇日记还是同一篇。
        # 换 id 会让前端「高亮新日记」的 diff 看起来像换了一个对象。
        existing = repo.get_diary(persona_id, user_id, day)
        if existing is not None:
            entry.id = existing.id
        # upsert：唯一键 (persona_id, user_id, date)，所以这里天然是幂等的
        repo.save_diary(entry)
        return {"entry": _diary_detail(entry), "warnings": warnings}

    @app.get("/v1/personas/{persona_id}/diary")
    def list_diary_endpoint(
        persona_id: str,
        user_id: str,
        limit: int = 30,
        repo: Repository = Depends(get_repo),
        account: Account | None = Depends(optional_actor),
    ) -> dict[str, Any]:
        """按日期倒序列出日记。

        空列表是 200 而不是 404：日记是可选状态，「她还没写过」不是错误，
        前端据此显示引导文案。
        """
        if repo.get_persona(persona_id) is None:
            raise HTTPException(status_code=404, detail=f"人格不存在: {persona_id}")
        user_id = ensure_user(account, user_id)
        entries = repo.list_diary(persona_id, user_id, limit=limit)
        return {"persona_id": persona_id, "user_id": user_id,
                "count": len(entries), "diary": [_diary_detail(e) for e in entries]}

    @app.get("/v1/personas/{persona_id}/diary/{day}")
    def get_diary_endpoint(
        persona_id: str,
        day: str,
        user_id: str,
        repo: Repository = Depends(get_repo),
        account: Account | None = Depends(optional_actor),
    ) -> dict[str, Any]:
        """取某一天的日记。没有 → 404（「这天她没写」是明确事实，不是空壳）。

        `day` 用字符串接收再自己 parse：路径参数声明成 `date` 时，
        非法值由框架校验，报错形态不受这里控制；自己 parse 能保证
        422 且 detail 是中文人话。
        """
        if repo.get_persona(persona_id) is None:
            raise HTTPException(status_code=404, detail=f"人格不存在: {persona_id}")
        try:
            target = date_cls.fromisoformat(day)
        except ValueError as exc:
            raise HTTPException(
                status_code=422, detail=f"日期格式非法: {day}（应为 YYYY-MM-DD）"
            ) from exc

        entry = repo.get_diary(persona_id, ensure_user(account, user_id), target)
        if entry is None:
            raise HTTPException(
                status_code=404, detail=f"这一天还没有日记: {persona_id}/{user_id}/{day}"
            )
        return {"entry": _diary_detail(entry)}

    # ------------------------------------------------------------------ #
    # 人格市场（M4-B）：上架 / 审核 / 分发
    # ------------------------------------------------------------------ #
    # 本组端点**全部要求登录**：市场是账号之间的动作，匿名 id 随时可以换一个，
    # 用匿名 id 上架等于「谁都能声称自己是作者」。
    # 注意这是对既有「未登录不弹登录墙」边界的唯一触碰点——只碰市场这一组。
    def _market_http_error(exc: MarketError) -> HTTPException:
        """MarketError.kind → 状态码。**只有这一处做这个翻译**。

        `cloned_forbidden` 用 403（判据 2 建议值）：它不是「参数错了」，
        是「这件事不允许」。文案直接取服务层的固定常量，不在这一层另写一份——
        两份文案迟早会漂移，而测试逐字断言的是那一份。
        """
        status = {
            "not_found": 404,
            "not_owner": 403,
            "cloned_forbidden": 403,
            "forbidden": 403,
            "self_review": 403,
            "not_approved": 409,
            "bad_state": 409,
            "bad_action": 422,
            "bad_snapshot": 422,
        }.get(exc.kind, 400)
        return HTTPException(status_code=status, detail=exc.message)

    def _mask_user(user_id: str) -> str:
        """公开列表里的作者 id 只留前 6 位+尾 2 位。

        公开的是人格，不是账号——完整 user_id 可被拿去当「这个人在市场里"
        的关联键，把一个人上架过的所有东西串起来。
        """
        if not user_id:
            return ""
        return user_id[:6] + "***" + user_id[-2:]

    # 市场侧复用模块级的 `_persona_brief`（它带 owner 与 provenance）：
    # 外壳形状与 `_listing_detail` 一致，但**同一份人格摘要只有一个定义**——
    # 两处各写一份的后果是「列表里说有 owner、安装返回说没有」，前端得猜信谁。

    def _listing_detail(listing: Any) -> dict[str, Any]:
        return {
            **market_service.listing_brief(listing),
            "persona_id": listing.persona_id,
            "author_id": _mask_user(listing.author_id),
            "status": listing.status,
            "reviewed_at": listing.reviewed_at.isoformat() if listing.reviewed_at else None,
            "reviewer_id": _mask_user(listing.reviewer_id),
            "review_note": listing.review_note,
        }

    @app.post("/v1/market/listings")
    def create_market_listing_endpoint(
        body: ListPersonaRequest,
        account: Account = Depends(require_actor),
        repo: Repository = Depends(get_repo),
    ) -> Any:
        """上架本人名下一个的人格（判据 1）→ 201 `pending_review` + `listed_at`。

        错误码：
          · 403 `not_owner` —— 不是自己的人格（判据 3）
          · 403 `cloned_forbidden` —— 蒸馏产物（判据 2，**红线**）
          · 404 `not_found` —— 人格不存在或已删
          · 409 `bad_state` —— 该人格已有待审/在架条目（重复上架走「复用」，见下）

        「已有待审/在架条目」返回 **200 + 同一条 id**（`created: false`）：
        重复点「上架」不该报错，但也不能留下两条负重。
        """
        try:
            listing, created = market_service.create_listing(
                repo,
                user_id=account.user_id,
                persona_id=body.persona_id,
                title=body.title,
                summary=body.summary,
            )
        except MarketError as exc:
            raise _market_http_error(exc) from exc
        # 首次 201，复用 200。状态码必须在**运行时**按 `created` 决定，
        # 所以这里不写 `status_code=201`（那是写死的）。
        return JSONResponse(
            status_code=201 if created else 200,
            content={**_listing_detail(listing), "created": created},
        )

    @app.get("/v1/market/listings")
    def list_market_listings_endpoint(
        limit: int = 50,
        repo: Repository = Depends(get_repo),
    ) -> dict[str, Any]:
        """公共列表：**只返回 approved**（判据 4）。

        `pending_review` / `rejected` 在这里一次都不出现——不是在调用方传参，
        是 `public_listings()` 里写死了 `status=approved`。
        作者看自己的全部条目走 `/v1/market/mine`。

        这个端点**不要求登录**：逛市场不该先注册。但每个 `author_id` 一律脱敏。
        """
        listings = market_service.public_listings(repo, limit=limit)
        return {
            "count": len(listings),
            "listings": [
                {**market_service.listing_brief(item), "author_id": _mask_user(item.author_id)}
                for item in listings
            ],
        }

    @app.get("/v1/market/mine")
    def my_market_listings_endpoint(
        account: Account = Depends(require_actor),
        repo: Repository = Depends(get_repo),
    ) -> dict[str, Any]:
        """作者自己的全部条目（任何状态）。判据 4 说的「那是另一个端点」就是这个。

        为什么必须鉴权：它是按作者过滤的视图。让它接受 `?author_id=`
        等于公开「某人上架了什么东西、被拒了几次」——审核侧诉说的是敏感的。
        """
        listings = market_service.own_listings(repo, account.user_id)
        return {"count": len(listings), "listings": [_listing_detail(i) for i in listings]}

    @app.get("/v1/market/listings/{listing_id}")
    def get_market_listing_endpoint(
        listing_id: str,
        repo: Repository = Depends(get_repo),
        account: Account | None = Depends(optional_actor),
    ) -> dict[str, Any]:
        """单条详情（含人格卡快照）。

        可见性规则只有两条：
          · `approved` —— 谁都能看（它就是叙述上的东西）
          · 其它状态——**只有作者自己**能看，别人一律 404

        为什么非 approved 对别人是 404 而不是 403：403 等于确认「这个 id 存在」，
        那就把「谁送审了、被拒了」变成可探测的事实。
        """
        try:
            listing = market_service.get_visible_listing(
                repo, listing_id, viewer_id=account.user_id if account else None
            )
        except MarketError as exc:
            raise _market_http_error(exc) from exc
        return {
            **_listing_detail(listing),
            "persona": listing.snapshot,
            "author_id": _mask_user(listing.author_id),
        }

    @app.post("/v1/market/listings/{listing_id}/review")
    def review_market_listing_endpoint(
        listing_id: str,
        body: ReviewListingRequest,
        account: Account = Depends(require_actor),
        repo: Repository = Depends(get_repo),
    ) -> dict[str, Any]:
        """审核 `approve` / `reject`（判据 5）。

        谁有审核权**由配置决定**（`LINGER_MARKET_REVIEWER_EMAILS`，逗号分隔），
        且**作者不能审自己的条目**。两条 guard 都在 `market.service.review()` 里，
        顺序是：动作合法 → 审核权 → 非本人 → 状态机。

        重复审核 → 409（状态机不允许），不是幂等 200：
        第二次 approve 也返回 200，前端就无法区分「我批了」和「早就批了」，
        「谁在什么时刻批的」——多一条 200 就多一次假记录。
        """
        try:
            listing = market_service.review(
                repo,
                reviewer_id=account.user_id,
                reviewer_email=account.email,
                reviewer_emails=market_service.reviewer_emails(),
                listing_id=listing_id,
                action=body.action,
                note=body.note,
            )
        except MarketError as exc:
            raise _market_http_error(exc) from exc
        return {**_listing_detail(listing), "action": body.action}

    @app.post("/v1/market/listings/{listing_id}/install")
    def install_market_listing_endpoint(
        listing_id: str,
        account: Account = Depends(require_actor),
        repo: Repository = Depends(get_repo),
    ) -> Any:
        """安装：在当前账号下拿到一份人格副本（判据 6）。

        **幂等复用**：同一账号装同一条目只有一份副本，第二次 200 + 同 id +
        `reused: true`。理由见 `market/service.py::install` 的 docstring。
        状态码：首次 201，复用 200。两者都含 `persona`（副本的 brief）与
        `provenance.origin`（拆清来源 listing），前缀步骤都来自市场。
        """
        try:
            outcome = market_service.install(repo, user_id=account.user_id, listing_id=listing_id)
        except MarketError as exc:
            raise _market_http_error(exc) from exc
        payload = {
            "persona": _persona_brief(outcome.persona),
            "provenance": outcome.persona.provenance,
            "listing_id": outcome.listing_id,
            "author_id": _mask_user(outcome.author_id),
            "copied": outcome.copied,
            "reused": not outcome.copied,
        }
        return JSONResponse(
            status_code=201 if outcome.copied else 200, content=payload
        )

    @app.delete("/v1/market/listings/{listing_id}")
    def unlist_market_listing_endpoint(
        listing_id: str,
        account: Account = Depends(require_actor),
        repo: Repository = Depends(get_repo),
    ) -> dict[str, Any]:
        """下架自己的在架条目（判据 7）→ 状态 `unlisted`，公共列表里消失。

        **已安装的人手里的副本不受影响**：下架只改 `market_listing.status`，
        一行 persona / memory / session 都不碰。这条由「不写」保证。
        """
        try:
            listing = market_service.unlist(repo, user_id=account.user_id, listing_id=listing_id)
        except MarketError as exc:
            raise _market_http_error(exc) from exc
        return {"id": listing.id, "status": listing.status, "unlisted": True}

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
    # 语音（M4-C）：听 / 说 / 观测
    # ------------------------------------------------------------------ #
    # 这一层是**加出来的**：不碰 send_message / orchestrator / compose 的任何一行。
    # 文本链路照旧，语音只是「另一条进入对话的入口」和「另一条离开对话的出口」。
    def get_asr_dep() -> Any:
        return app.state.asr

    def get_tts_dep() -> Any:
        return app.state.tts

    @app.get("/v1/voice/health")
    def voice_health(
        asr: Any = Depends(get_asr_dep),
        tts: Any = Depends(get_tts_dep),
    ) -> dict[str, Any]:
        """谁在听、谁在说、有没有发生降级。

        与 `/v1/llm/health` 同构：配置了真实 provider 却没接上时，降级必须是
        **看得见**的。静默降级等于骗人——用户会以为自己在用 Whisper。
        """
        notes = voice_fallback_notes()
        return {
            "asr": {
                "configured": settings.asr_provider,
                "provider": getattr(asr, "name", "unknown"),
                "fallback": notes.get("asr"),
            },
            "tts": {
                "configured": settings.tts_provider,
                "provider": getattr(tts, "name", "unknown"),
                "fallback": notes.get("tts"),
            },
        }

    @app.post("/v1/asr")
    async def post_asr(
        request: Request,
        asr: Any = Depends(get_asr_dep),
    ) -> dict[str, Any]:
        """音频 → 文字。**两种提交方式并存**，不选一种。

        原计划是「选一种并在 PR 里说明」。实际做下来发现两个调用方的形状不同：

          · 浏览器 `MediaRecorder` 手里是一个 `Blob`，`FormData` 直接塞进去最自然，
            不用先 base64（那要多一次 33% 体积膨胀和一次内存拷贝）；
          · 脚本 / 测试 / 未来的小程序手里是 base64 字符串，multipart 反而别扭。

        两者都在入口合并到**同一个字节流**上，之后走完全相同的校验与 provider。
        所以「两种入口」不是两套实现，只是两种信封。

        请求体**自己拆**，不用 `File(...)` / `Body(...)` 声明：FastAPI 不允许同一个
        端点既声明 `UploadFile` 又声明 JSON body（它只认一种 content-type，
        另一种会被判 422 且信息误导）。手拆的代价是十来行，换来的是
        「同一张表里两种信封都成立」，以及一条清晰的分支——值。

        错误码（判据 2 的红线）：空音频 / 非音频字节 → **422**，绝不是 500。
        500 意味着「服务端坏了」，而这两种情况都是「你发的东西不对」。
        """
        content_type = (request.headers.get("content-type") or "").lower()
        if "multipart/form-data" in content_type:
            form = await request.form()
            upload = form.get("audio")
            if isinstance(upload, str) or upload is None:
                raise HTTPException(status_code=422, detail="multipart 缺少 audio 文件字段")
            data = await upload.read()
            language = None
        else:
            payload = await _json_body(request)
            if payload is None:
                raise HTTPException(
                    status_code=422,
                    detail="缺少音频：用 multipart 的 audio 字段，或 JSON 的 audio_base64",
                )
            try:
                parsed = ASRJsonRequest(**payload)
            except ValidationError as exc:
                raise HTTPException(status_code=422, detail=_validation_detail(exc)) from exc
            try:
                data = _decode_b64(parsed.audio_base64)
            except AudioRejected as exc:
                # 422 而不是 500：坏 base64 是「你发的东西不对」，不是服务端坏了
                raise HTTPException(status_code=422, detail=str(exc)) from exc
            language = (parsed.language or "").strip() or None

        if not data:
            raise HTTPException(status_code=422, detail="音频为空")
        if len(data) > settings.voice_max_audio_bytes:
            # 413 而不是 422：格式没问题是太大了，告警语义不同
            raise HTTPException(
                status_code=413,
                detail=f"音频过大：{len(data)} 字节，上限 {settings.voice_max_audio_bytes}",
            )
        if not _looks_like_audio(data):
            raise HTTPException(
                status_code=422,
                detail="不是可识别的音频（支持 wav / webm / ogg / mp3 / m4a）",
            )

        try:
            text = asr.transcribe(data, language=language)
        except AudioRejected as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        except VoiceError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        return {"text": text, "bytes": len(data)}

    def _prosody_source(repo: Repository, persona_id: str, user_id: str) -> PAD:
        """这一句该用什么样的情绪基线。

        三档，从具体到中性：
          1. 这个用户跟这个人格聊过 → 用他此刻的 PAD（真实情绪）；
          2. 没聊过 → 用这个人格的基线（她平时就是这个调子）；
          3. 连人格都没有 → 中性的 PAD()（不该发生，人格不存在在前面已经 404）。
        """
        state = repo.get_affect(persona_id, user_id) if user_id else None
        if state is not None:
            return state.pad
        card = repo.get_persona(persona_id)
        if card is not None:
            return card.affect.baseline
        return PAD()

    @app.post("/v1/tts")
    def post_tts(
        body: TTSRequest,
        repo: Repository = Depends(get_repo),
        tts: Any = Depends(get_tts_dep),
    ) -> Response:
        """文字 → 音频（`audio/wav`）。

        三件事按顺序，每一步都有个非它不可的理由：

        1. **人格必须存在**（404）。没有它就没有嗓音，也就没有「她是谁」。
        2. **cloned 卡不许出声**（403）。复刻一个真人的说话方式，已经是这条产品
           最敏感的边界（§3.1.3）；再给他配一张合成的嘴，等于把「像他」升级成
           「就是他」。这与「cloned 不许上架」同源，是同一道红线的第二面。
        3. **情绪决定韵律**，不是装饰。同一句「我在」，她在焦躁和低落时
           说得不一样——见 `voice/prosody.py`。
        """
        card = repo.get_persona(body.persona_id)
        if card is None:
            raise HTTPException(status_code=404, detail=f"人格不存在: {body.persona_id}")
        if str((card.provenance or {}).get("type") or "") == VOICE_BLOCKED_PROVENANCE:
            raise HTTPException(
                status_code=403,
                detail="这张人格卡是复刻真人的（cloned），不允许合成声音",
            )

        pad = _prosody_source(repo, card.id, body.user_id.strip())
        voice = voice_params(card.voice.model_dump(), body.text)
        try:
            audio = _synthesize_audio(tts, body.text, voice=voice, pad=pad)
        except VoiceError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc

        # Content-Length 由 Response 自己算；这里只钉媒体类型与「别缓存」：
        # 同一段文本在不同情绪下的音频不同（URL 却一样），缓存住就会播错情绪。
        return Response(
            content=audio,
            media_type="audio/wav",
            headers={
                "Cache-Control": "no-store",
                "X-Prosody-Rate": f"{prosody_for(pad)['rate']:.2f}",
                "X-Prosody-Pitch": f"{prosody_for(pad)['pitch']:.2f}",
            },
        )

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
