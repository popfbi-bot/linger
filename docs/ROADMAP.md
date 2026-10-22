# Linger M0 路线图与验收清单

> 目的：钉住「可运行」的验收判据，避免"写了文档但从不验证"。
> 每个里程碑必须能独立演示，且验收标准**能被证明会失败**（见 story-os 纪律「判据要能证明会失败」）。

## 当前状态（2026-10-22）

- ✅ 骨架已推送 main（commit `1c60583`）
- ✅ Issue #1 已交付并由 PR #2 合并（squash sha `02ff633`）：`orchestrator` + 事件类型 + 演示 + 测试
- ✅ **M0 验收全部通过**（详见下方勾选清单）
- ✅ **人际知识库（Social Knowledge Base）已落地**（commit `874ad94`）：五份语料 → 1113 条结构化行为条目，按阶段/极性索引，注入编译器；全量 24 passed
- ✅ **Issue #3 已交付并由 PR #4 合并（squash sha `9095764`）**：红灯检测接入关系引擎 + AI 行为边界注入；全量 **55 passed**
- ✅ **第六份语料「陪伴缺失与有毒关系」已接入**（commit `415302b`）：180 条负面信号（上篇按人生阶段 100 条，下篇按有毒关系类型 70 条），独立 `companion_negative` 维度；新增 `ToxicSignalDetector` 识别情感勒索/否定打压/冷暴力/忽视等信号，进 `done` 事件，**只提示不惩罚**；同时修复 `.gitignore` 误伤导致语料未入库的隐患（六份语料现全部进版本控制，干净克隆可跑）；全量 **69 passed**
- ✅ **M1 HTTP 服务层完成**：新增 `src/linger/server/app.py`（9 个端点 + SSE 流式 + `main()` 入口）与 `tests/test_api.py`（23 个用例）；`storage/base.py` 新增 `get_memory` 抽象方法；架构文档补全 SSE 顺序契约（`thinking` 出现 1~2 次）；全量 **92 passed**，并经真实 uvicorn + curl 复验
- 🚧 **下一步（M1.5）**：前端最小可跑界面（头像跟随 affect / 气泡跟随 delta / trace 面板）+ 场景运行时（正面清单做剧本节拍）+ 陪伴模式（人生阶段索引 + 有毒信号识别）

## 验收清单（M0「能跑通一轮」）

### 代码存在性（可从文件系统验证）

- [x] `src/linger/orchestrator.py` 存在
- [x] `src/linger/server/__init__.py` 存在，定义 `ServerEvent`（type 含 thinking/affect/delta/done）
- [x] `examples/demo.py` 存在
- [x] `tests/` 目录存在

### 可运行性（可从命令行验证）

- [x] `python examples/demo.py` 退出码为 0，打印完整链路（3 轮对话 + 情绪 + 关系 + 记忆条数 + trace）
- [x] `python -m pytest` 退出码为 0，且测试数 > 0（17 passed）
- [x] 免密钥可跑（默认 `LINGER_LLM=mock`，不需要任何 API key）

### 行为正确性（关键设计约束，必须有测试钉住）

- [x] 记忆写入发生在回复落库**之后**（禁止同轮写记忆）
- [x] 低于相关性闸门的记忆**不会**进入注入
- [x] 记忆检索与状态更新**并发**执行（不是串行）
- [x] `send_message` 返回事件流（thinking/affect/delta/done）
- [x] 编译后的 system prompt 包含人格锚点（`你是林汐`）

### 范围不越界（用 git diff 验证）

- [x] 未修改 `models.py` / `storage/base.py` / `config.py` / `docs/ARCHITECTURE.md`
- [x] 未修改 `pyproject.toml` 的 `[project]` 元数据
- [x] 未引入新的第三方依赖（或只在 optional-dependencies 里加）

## 评审 NPC 交付时的动作

1. `git fetch origin` 拉取 PR 分支
2. 本地 `git checkout` 该分支，独立跑 `python examples/demo.py` 和 `python -m pytest`
3. 用 `git diff --stat origin/main <pr-branch>` 核对改动范围
4. 对照本清单逐项勾选；**不满足任何一项则打回**
5. 只有全部通过，才 `merge-pull` 并关闭 issue

## 下一个里程碑（M0 完成后）

- M1：SSE HTTP 服务（`src/linger/server/app.py`），把 `send_message` 暴露为 `/v1/sessions/{id}/messages`
- M1：记忆管理 API（看/改/删/导出），trace 回溯接口 `/v1/traces/{turn_id}`
- M1：情绪可视化事件（前端头像/气泡跟随情绪）

## M1 验收清单（HTTP 服务层）

> 判据必须**能证明会失败**：每一条都能用一段代码/命令复现「不满足时红」。
> 与 M0 一样，验收不靠自述，靠独立跑。
>
> **状态（2026-10-22 完成）**：28 条全部通过，全量 **92 passed**，
> 并在真实 uvicorn 服务器上用 curl 复验（见下方「真实服务器复验」）。

### A. 入口与契约（当前必然失败项）

- [x] `src/linger/server/app.py` 存在，且提供 `main()` 函数
      （`pyproject.toml` 已声明 `linger = "linger.server.app:main"`，入口缺失即为失败）
- [x] `python -c "from linger.server.app import app"` 退出码 0
- [x] `GET /health` 返回 200，且 body 含 `status` 字段
- [x] 未修改 `pyproject.toml`（依赖已在其中：`fastapi` / `uvicorn` / `httpx`）
- [x] 未修改 `models.py` / `config.py` / `docs/ARCHITECTURE.md` 的既有契约
      （例外说明见「E. 范围」）

### B. 会话与消息（核心链路）

- [x] `POST /v1/personas` 能从模板实例化人格，返回 201 或 200，body 含 `id` 与 `name`
- [x] `POST /v1/sessions` 能创建会话，返回 body 含 `session_id`
- [x] `POST /v1/sessions/{id}/messages` 以 **SSE** 流式返回
      （响应头 `content-type` 含 `text/event-stream`）
- [x] SSE 事件顺序为 `thinking → affect → delta* → done`
      （注：`thinking` 出现 1~2 次——`stage=recall` 与 `stage=compose` 各一次）
- [x] 事件行格式为 `event: <type>` + `data: <json>`，且 `data` 内含 `type` 字段
- [x] `done` 事件的 data 含 `turn_id` / `used_memories` / `state` / `usage` 四要素
- [x] `state` 内含 `affect` / `relation` / `red_flags` / `toxic_signals`
- [x] 会话不存在时返回 404（不是 500）
- [x] 空消息内容返回 422 或 400（不是静默接受）

### C. 记忆管理与回溯

- [x] `GET /v1/sessions/{id}/memories` 返回该会话人格的记忆列表
- [x] `DELETE /v1/memories/{memory_id}` 可删除记忆，删除后再查为 404
- [x] `GET /v1/sessions/{id}/memories/export` 可导出（JSON 或 Markdown）
- [x] `GET /v1/traces/{turn_id}` 返回该轮 trace，含 `query` / `candidates` / `passed_gate` / `selected` / `tokens_by_block`
- [x] 记忆不存在时返回 404

### D. 可运行性

- [x] `python -m pytest` 退出码 0，且测试数 > 69（实测 **92 passed**）
- [x] 新增 `tests/test_api.py`，覆盖 A/B/C 全部条目（23 个用例）
- [x] 免密钥可跑（默认 `LINGER_LLM=mock`）
- [x] `uvicorn linger.server.app:app` 可启动（实测已起真实服务器复验）
- [x] 干净克隆（`git clone` + `pip install -e .`）后 pytest 通过

### E. 范围不越界

- [x] 新增文件限于：`src/linger/server/app.py`、`tests/test_api.py`
- [x] 未修改 `src/linger/social/` `src/linger/memory/` `src/linger/affect/` `src/linger/relation/` `src/linger/compose/` `src/linger/persona/`
- [x] 未修改 `src/linger/orchestrator.py`（M0 已定稿的流水线顺序是契约）
- [x] 未引入 `pyproject.toml` 之外的新第三方依赖
- [x] **唯一例外（已记录）**：`src/linger/storage/base.py` 新增抽象方法
      `get_memory(memory_id) -> MemoryNode | None`，`sqlite.py` 同步实现。
      理由：HTTP 层拿到 memory_id 后需要判断「存不存在」（否则无法返回 404），
      既有 Repository 抽象没有按 id 取单条记忆的能力。
      这是**新增**而非**修改**既有方法语义，不影响 SQLite 之外的实现方
      （实现方需补一个方法即可）。

### 真实服务器复验（uvicorn + curl，非 TestClient）

| 验收项 | 实测 |
|---|---|
| `GET /health` | 200 `{"status":"ok","llm":"mock","embedder":"hash"}` |
| `POST /v1/personas` | 201 → `{"id":"p_8561ce72f4","name":"林汐",...}` |
| `POST /v1/sessions` | 201 → `{"session_id":"s_e0abc3b93c",...}` |
| SSE 响应头 | `content-type: text/event-stream; charset=utf-8` |
| SSE 事件序列 | `thinking(recall)` → `affect` → `thinking(compose)` → `delta×12` → `done` |
| `done` 四要素 | turn_id=2 / used_memories=[] / state / usage{in:1304,out:32} |
| `state` 四段 | affect / relation / red_flags / toxic_signals 齐全 |
| 记忆列表 | count=1 → `m_4424175c80` L3「我妈妈下周要去医院复查」 |
| 删除记忆 | 200 → 再删 404 → 列表归零 |
| trace 回溯 | query/candidates/passed_gate/selected/tokens_by_block 齐全，prompt 1588 字符 |
| 错误码 | 不存在会话 404 / 空 content 422 / 不存在模板 404 / 不存在 trace 404 / 不存在记忆 404（**无一为 500**） |