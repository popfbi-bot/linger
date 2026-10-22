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
- ✅ **M1 HTTP 服务层完成**：新增 `src/linger/server/app.py`（10 个端点 + SSE 流式 + `main()` 入口）与 `tests/test_api.py`（32 个用例）；`storage/base.py` 新增 `get_memory` 抽象方法；架构文档补全 SSE 顺序契约（`thinking` 出现 1~2 次）；**经 NPC 3 个 PR 交叉评审，吸收 1 个端点缺口 + 4 条判据缺口，并修复「纯空白输入写入脏 turn」的真实 bug**；全量 **101 passed**，并经真实 uvicorn + curl 复验
- 🚧 **下一步（M1.5）**：前端最小可跑界面（A 组，自己做）+ 场景运行时（B 组，派工）；陪伴模式（人生阶段索引）顺延 M2

## M1.5 验收清单（前端 + 场景运行时）

> 两组独立验收：A 组是「人眼可见的陪伴感」，B 组是「角色扮演的第二层」。
> 判据必须能证明会失败。

### A 组：前端最小可跑界面

- [ ] `GET /` 返回 200，`content-type` 含 `text/html`
- [ ] 页面**无外部 CDN 依赖**（离线可跑，不引 React/Vue/Tailwind 的 CDN）
- [ ] 页面含五个可见区块：人格选择 / 对话区 / 情绪指示器 / 记忆面板 / trace 面板
- [ ] JS 调用的端点与后端一致：`GET /v1/personas/templates`、`POST /v1/sessions`、`POST /v1/sessions/{id}/messages`
- [ ] SSE 用 `fetch` + `ReadableStream` 或 `EventSource` 消费，且按 `event:` 分流
- [ ] `affect` 事件到达时，情绪指示器的**文字与颜色都变化**（不是只在 console 打印）
- [ ] `delta` 事件到达时，文本**逐段追加**到气泡（不是等 done 一次性显示）
- [ ] `done` 事件到达后，记忆面板与 trace 面板**自动刷新**
- [ ] 断线/404/422 时页面显示可读错误，不白屏
- [ ] 静态资源由后端挂载（`/static/*` 可访问），刷新页面不掉状态

### B 组：场景运行时（角色扮演第二层）

- [ ] 新增 `src/linger/scenario/` 模块，含结构化 `Scenario` 模型
- [ ] `Scenario` 字段齐备：`id`/`title`/`world`/`setting`/`opening_narration`/`user_role`/`beats`/`constraints`/`exit_conditions`
- [ ] 至少 **2 个内置场景** YAML（`src/linger/templates/scenarios/*.yaml`）
- [ ] `GET /v1/scenarios` 返回场景列表（含 id/title/setting 摘要）
- [ ] `POST /v1/sessions` 支持 `scenario_id`；不存在的 id 返回 **404**
- [ ] 编译器的 `scenario` 块包含三样东西：场景设定 + **未完成节拍** + 约束
      （不再只是把字符串原样拼进 prompt）
- [ ] 节拍按**关键词 trigger** 推进（确定性，可复现，不用模型判定）
- [ ] 已完成的节拍**不再出现在**后续 prompt 里（否则 AI 会反复推进同一节拍）
- [ ] 场景进度**持久化**：重建 Orchestrator 后节拍进度不丢
- [ ] 一轮结束后 `done` 事件或 trace 里能看到当前节拍状态
- [ ] `python -m pytest` 全绿，且测试数 **> 101**
- [ ] 干净克隆后 pytest 通过

### 范围边界

**允许修改**（场景运行时必须动这些，M1 的「不动契约」约束在本轮放开）：

- `src/linger/orchestrator.py`（`create_session` 接收 scenario_id 并加载场景）
- `src/linger/compose/compiler.py`（`_scenario_block` 升级为结构化渲染）
- `src/linger/storage/base.py` / `sqlite.py`（场景进度持久化）
- `src/linger/server/app.py`（仅新增 `/v1/scenarios` 端点；**不要动静态文件路由**，那部分由 A 组负责）
- `src/linger/models.py`（仅在确有必要时加字段，加完要说明为什么不能放 scenario 模块里）

**不得修改**：`src/linger/social/` `src/linger/memory/` `src/linger/affect/` `src/linger/relation/` `src/linger/persona/` 的既有语义；**不得引入新第三方依赖**。

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
> **状态（2026-10-22 完成）**：28 条全部通过，全量 **101 passed**，
> 并在真实 uvicorn 服务器上用 curl 复验（见下方「真实服务器复验」）。
>
> **NPC 交叉评审产出**：NPC 为 issue #5 提交了 3 个 PR（#6/#7/#8），
> 三选一对比后发现两处本轮遗漏，已吸收：
> 1. **端点缺口**：缺 `GET /v1/personas/templates`（前端要建人格，
>    得先知道有哪些模板可点，否则只能靠猜 template_id）→ 已补
> 2. **判据缺口**：缺 4 条行为判据 →
>    - 被拒的请求**不能留下半轮 turn**（否则记忆长在空输入上）
>    - trace 的 turn_id 必须与 done 事件一致（否则回溯功能断掉）
>    - 第二轮也要能查到 trace（turn_id 单调递增，不是只有第一轮有）
>    - 聊完之后 affect/relation 必须落库（否则情绪白算了）
>
> **评审中发现并修复的真实 bug**：`content="   "`（纯空白）能通过
> `min_length=1` 校验，被当成有效消息写入 turn 表并在空输入上提炼记忆。
> 已加 `field_validator` 拒绝纯空白（content / template_id / persona_id / user_id），
> 复验：空白 content 与空白 template_id 均返回 422，turn 表不再有脏数据。

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
| 模板清单（后补） | `GET /v1/personas/templates` → `['laojiang', 'linxi']`，含 name / archetype / sample |
| 纯空白输入（修复后） | `content="   "` → 422，turn 表不新增记录；`template_id="   "` → 422 |
| OpenAPI | `/openapi.json` 正常生成，10 个 path |