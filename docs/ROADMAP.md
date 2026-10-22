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
- ✅ **M1.5 完成**：前端最小可跑界面（`server/static/` 三文件 + 场景选择 + 节拍进度面板）+
  场景运行时（`src/linger/scenario/`，两个内置剧本、关键词节拍推进、进度持久化）；
  **经独立验收**——不只跑测试，而是自己写脚本逐轮核对「节拍是否真的从 prompt 消失」；
 全量 **142 passed**，干净克隆同样通过，并经真实 uvicorn + 模拟前端全流程复验
- ✅ **关系角色图谱已落地**（第七份语料《亲密陪伴关系中的人格身份全录》）：
  `src/linger/social/roles.py` 把 **210 个关系身份位** 按 14 个维度索引；
  `RelationRoleKnowledge` 提供 `by_dimension` / `search` / `complementary_roles`（AI 该补哪个位）/
  `dynamics`（戏剧三角、追逃锁死、内在小孩等关系剧本）/ `detect`（文本识别）；
  Orchestrator 把识别结果放进 `done` 事件的 `state.roles`，编译器新增 `roles` 块注入互补角色；
  **只识别，不惩罚**——绝不进入 `unresolved_conflicts`。全量 **163 passed**（+21）
- ✅ **M2-A 完成**（NPC 交付 PR #12，squash sha `4328625`，issue #11 已关闭留痕）：
  陪伴档案 `CompanionProfile`（模型 + `companion_profile` 表 + 抽象/实现成对）+
  `src/linger/companion/profile.py`（8 阶段确定性识别、需求取自语料、风险取自 toxic 信号）+
  编译器 `companion` 块（空档案不注入、排在缓存断点之后）+
  `GET/PATCH /v1/companion/{persona_id}/{user_id}`；
  **经独立验收**——自行复现三条「改坏→红」，并独立复验端点行为；
  全量 **222 passed**，干净克隆同样通过
- ✅ **M2-B 完成**（commit `1b5a8cb`）：前端陪伴档案面板——阶段可手动校正、
  角色/需求/风险分色标签、空态引导文案、`done` 后自动刷新；新增 15 个前端测试
- ✅ **README 补齐**（commit `6b38672`）：修复 `pyproject.toml` 声明 `readme = "README.md"`
  但文件缺失导致 **`pip install .` 失败**的构建缺陷
- ✅ **M2-C 记忆管理前端完成**（commit `c4c3dcd`）：记忆面板支持**编辑**
  与**编辑历史回溯**——前端每条记忆加「编辑」（行内 textarea + 保存/取消）与「历史」
  （展开 before/after）；后端新增 `memory_edit_log` 表 + `MemoryEditRecord` 模型 +
  `GET /v1/memories/{id}/edits` 端点，PATCH 时记录 old/new。
  **经独立验收**（变异 `add_memory_edit` 为 no-op → 2 红，还原 9 绿）；全量 **285 passed**
- ✅ **M2 整体验收通过**（2026-10-22，`b58e595`）：三条线全部闭环——
  A 陪伴档案（模型/存储/识别/注入/API）、B 前端档案面板、C 记忆管理（PATCH + 编辑
  + before/after 审计）。**全量 287 passed；干净克隆同样 287 passed**（仓库自洽，
  不依赖本机残留）。M2 主题「让 AI 记得你现在处在什么阶段、你在关系里是谁」达成：
  识别结果可持久、可查看、可手动校正，且编辑留痕可回溯；三者一律**只提示不惩罚**。
- ✅ **M3-A 一句话生成人格完成**（2026-10-22，issue #18 派工后本窗口自行实现）：
  `POST /v1/personas/generate`——一句话描述 → LLM 生成 → 解析 → Schema 校验 →
  **R1~R5 确定性冲突消解** → 返回 `persona`/`warnings`/`preview`（6 条台词），**不落库**；
  确认后由 `POST /v1/personas` 接受完整人格卡落库（服务端重发 id、标
  `provenance.type="generated"`）。前端人格区新增「+ 用一句话造一个」入口：
  描述输入 → 生成预览（名字/身份/台词/自动修正说明）→「用这个开始」→ 直接开会话。
  全量 **319 passed**（287 + 32），端到端复验五步全通。
  **验收**：变异「删 R1」「生成即落库」「不捕获生成异常」三处均变红。
- ✅ **M3-B 模板幂等 + 用户隔离完成**（2026-10-22，修致命缺陷）：实测发现
  **点两次同一模板会生成两个 persona id，于是同一用户换个会话记忆从 1 条掉到 0 条**——
  用户看到的是「刷新页面再点林汐，她就不记得我了」。根因是 `PersonaFactory.create`
  每次都发新 id，而所有状态按 `(persona_id, user_id)` 存，记忆被切碎到多个 id 下。
  修法：按 `provenance.template` 查已实例化人格并复用（`Repository.find_persona_by_template`）；
  前端 `user_id` 从写死 `u_web` 改为 localStorage 持久（否则同一服务上所有浏览器共用一份记忆）。
  全量 **327 passed**（319 + 8）；变异去掉幂等查询 → 3 红（含核心判据「记忆跨会话留存」）。
- 🚧 **M3-C 已派工**（issue #19，NPC）：人格管理——`DELETE /v1/personas/{id}` 软删除 +
  三处读取过滤 status + 前端「我的人格」列表与删除。解决「造了看不见、删不掉」。
- 🚧 **后续**：场景 YAML 可视化编辑、聊天记录蒸馏（入口 C）、五步向导（入口 D）

## M2 验收清单（陪伴档案）

> M2 的主题：**让 AI 记得「你现在处在什么阶段、你在关系里是谁」**。
> M1.5 解决了「单轮对话不越界」，M2 解决「跨轮对话不健忘」。
>
> 判据必须**能证明会失败**。M2-A 已派工给云端 NPC（issue 正文即判据来源）。

### M2-A 陪伴档案（核心：模型 + 存储 + 识别 + 注入 + API）

**为什么需要它**：现在 AI 每轮都能靠 `roles` 块临时推断用户的处境，但这层推断**不持久**——
换个会话就没了。陪伴档案把「用户是谁」变成可存储、可查看、可由用户校正的状态。

- [x] 新增 `CompanionProfile` 模型（`src/linger/models.py`）：字段含
      `persona_id` / `user_id` / `life_stage` / `roles` / `needs` / `risks` / `updated_at`
- [x] 新增 `companion_profile` 存储表 + Repository 抽象方法
      `save_companion_profile` / `get_companion_profile`
      （**必须同步改 `storage/base.py` 与 `storage/sqlite.py`**，抽象与实现成对）
- [x] 人生阶段识别：从用户话语中识别 8 个阶段（胎儿期/婴儿期/幼儿期/童年期/青春期/青年期/中期年/老年期）
- [x] 陪伴需求：按 `life_stage` 从 `SocialKnowledge.companion_pitfalls()` 取该阶段的陪伴需求
- [x] 有毒风险：按 `SocialKnowledge.toxic_relation_items()` 汇总当前关系中的风险类型
- [x] 每轮对话结束后**更新档案**（与写记忆同一时机：回复落库之后）
- [x] 编译器新增 `companion` 块，把档案注入 prompt（档案为空时**不注入**，不破坏前缀缓存）
- [x] `GET /v1/companion/{persona_id}/{user_id}` 返回档案；不存在返回**空档案**而不是 404
      （档案是可选状态，不存在是常态）
- [x] `PATCH /v1/companion/{persona_id}/{user_id}` 允许**用户手动校正** `life_stage`
- [x] 档案内容**绝不进入 `unresolved_conflicts`**（识别不是惩罚）
- [x] 新增 `tests/test_companion.py`，测试数 > 163
- [x] `python -m pytest` 全绿
- [x] **贴红→绿对比**：判据能被证明「不满足会红」

> **M2-A 状态（2026-10-22 完成，PR #12，squash sha `4328625`）**：11 条全部通过，
> 全量 **222 passed**（163 + 44 新增 + 15 前端），**干净克隆同样 222 passed**。
>
> **独立验收（不采信自述，本窗口自行复现三条「改坏→红」）**：
> 1. 把 `update_companion_profile` 提前到 LLM 流式之前
>    → `test_13_profile_updated_after_reply_committed` 红（序列 `turn:user, profile, turn:assistant`）
> 2. 把「刚结婚」从青年期挪到中年期
>    → `test_05_detect_life_stage[我刚结婚-青年期]` 红
> 3. 让 toxic 信号进 `conflict`
>    → `test_26_toxic_conversation_does_not_penalize_relation` 红
>
> **端点独立复验**：`GET` 不存在档案 → 200 + `is_empty:true`（非 404）；
> `PATCH {"life_stage":"青少年期"}` → 422；`PATCH {"life_stage":"中年期"}` → 200 且
> `needs` 换成中年期语料原文（证明来自语料而非硬编码）。
>
> **实现上的两个好设计**（值得留着）：
> - 阶段识别带**虚构语境降噪**：「电影里主角退休了」不算用户自己的处境；
> - 阶段为空时**不给通用需求**（`generic=False`）——通用需求是胎儿期那批条目，
>   对空档案毫无信息量，却会让档案由空变非空、白白顶掉前缀缓存。
>
> **遗留（当时）**：NPC 发现既有缺陷 `SocialKnowledge.companion_profiles()` 恒返回空列表
> （section 过滤条件与语料实际标题对不上）。它**没有偷偷改既有语义**，
> 而是新增只读查询 `positive_demands()` 作回退并建议单独开 issue →
> 已登记为 **issue #13**，后由 NPC 修复（见下）。

> **issue #13 已修复（PR #14，squash sha `1a68312`）**：`companion_profiles()` 改成按语料
> **真实的标题结构**取值，不再硬编码一个只存在于 `##` 大标题里、从未出现在条目上的 section 名。
>
> **根因比 issue 正文写的更深一层**：`人生阶段陪伴需求与角色.md` 的**上篇和下篇章节都写成
> 「一、xxx」**，下篇还有「一、**亲情陪伴**：血浓于水的牵绊」——序号写法完全相同，
> 单看章节名分不开。所以判定分三层：
> 1. 认大标题（`SocialItem` 新增 `heading` 字段）：`上篇：按人生阶段…` 是需求篇，
>    `下篇：按陪伴角色类型…` 是角色篇；
> 2. 认章节名：`序号 + 阶段名`；
> 3. **只取正面语料**——`陪伴缺失与有毒关系_负面清单.md` 的上篇同样按「一、胎儿期…」排版，
>    不排除它就会把「陪伴缺失」当成「陪伴需求」混进来。
>
> **结果**：**140 条**（覆盖语料 10 个阶段：胎儿期→老年期，比引擎的 8 阶段多出
> 学龄期 / 更年期·准老年期）。`companion_pitfalls()` 行为不变（全量 180 条、老年期 15 条）。
> 独立核对：`total` / `toxic` 条目数 / `red_flag` 条目数 / `toxic_relation_types()` /
> `companion_recovery()` 全部与基线一致，**无回归**。
>
> **踩坑留痕（差点默默改坏另一份语料）**：给标题正则加捕获组后，下篇 toxic 判定原用的是
> `m.group(1)`，一下从「标题文字」变成了「`#` 的个数」，`toxic_relation_types()` 直接变空。
> 全量跑测试时 `test_toxic_signals` 立刻报红才发现——**这就是「一起跑全量」的价值**。
>
> **判据红→绿（本窗口自行复现，非采信自述）**：
> 1. 还原成老实现（硬编码 section 名）→ 4 红，含 `test_companion_profiles_not_empty`
> 2. 去掉「只取正面语料」（`polarity` 那道闸）→ 2 红，负面条目混进需求
> 3. `_is_profiles_heading` 改成恒真 → 3 红（上篇/下篇分不开）
> 4. `toxic` 判定改回 `m.group(1)` → `test_toxic_signals` 2 红
>
> ⚠️ **NPC 自述里有一条不实，已修正**：它写「去掉『章节名不含陪伴』这层 → 7 红」。
> 本窗口实测：单独去掉 `if "陪伴" in name` **7 条全绿**——因为下篇标题
> （「一、亲情陪伴：血浓于水的牵绊」）里**根本不含任何阶段名**，
> `any(stage in name)` 早就挡掉了，那层对当前语料是**死代码**。
> 它防的是「一、青春期陪伴」这种阶段名与陪伴撞车的**未来**章节。
>
> ⚠️ **由此暴露的真问题：判定层原先没有测试覆盖。** 上面 7 条端到端用例
> 只验「结果对不对」，没验「判定函数本身」。把两个判定函数改坏，
> 端到端全绿——**没被测试覆盖的判据等于不存在**。
> 已补 19 条判定层测试（`test_is_profiles_heading` / `test_is_life_stage_section`
> / 撞车标题 / 负面语料不得混入），并逐条验证过改坏会红。
> 全量 **261 passed**（242 + 19）。
>
> **范围越界记录**：NPC 的 PR 同时改了 `docs/ROADMAP.md`，
> 而 issue 边界明确写了「不要改 ROADMAP，文档由本窗口统一对齐」。
> 内容方向是对的（本段即在其基础上收编重写），但**纪律上要记一笔**：
> 下次派工要把「连文档也别动」写进显眼位置。

### M2-B 前端陪伴档案面板

- [x] 页面新增可见区块「陪伴档案」，显示阶段 / 角色 / 需求 / 风险
- [x] 档案为空时有引导文案（不是空白面板）
- [x] 用户可手动修改人生阶段并保存（调 `PATCH`）
- [x] 面板在 `done` 事件后自动刷新
- [x] 无 CDN 依赖（延续 M1.5 纪律）

> **M2-B 状态（2026-10-22 完成，commit `1b5a8cb`）**：5 条全部通过，
> 新增 `tests/test_companion_frontend.py`（15 例），全量 **178 passed**（合并 M2-A 后 222）。
> 阶段下拉的 8 个选项与后端 `LIFE_STAGES` **逐项比对**（列表漂移会红）——
> 后端 API 响应里带 `life_stages` 字段，可直接作为契约来源。
>
> **写测试时踩的判据错位**：`test_render_companion_escapes_html` 一开始查
> `renderCompanion` 函数体里的 `escapeHtml`，红了。查下去发现转义其实发生在
> `renderTags`（真正拼 `innerHTML` 的地方），`renderCompanion` 只负责分发。
> ⇒ **判据要钉在真正拼 DOM 的那一层**，不是「看起来相关」的函数。
> 这是判据自身的问题，改判据而不是改实现。

### M2-C 记忆管理前端

- [x] 记忆面板支持**编辑**记忆内容（`PATCH /v1/memories/{id}`）
- [x] 编辑后能回溯到修改前后的差异（`GET /v1/memories/{id}/edits` 审计日志）

> **M2-C 状态（2026-10-22 完成）**：前端记忆面板每条目加「编辑」按钮
> （行内 textarea + 保存/取消）与「历史」按钮（展开 before/after）；
> 后端新增 `memory_edit_log` 表 + `MemoryEditRecord` 模型 +
> `GET /v1/memories/{id}/edits` 端点；PATCH 时记录 old/new。
> **经独立验收**——变异 `add_memory_edit` 为 no-op 后
> `test_patch_writes_edit_log` / `test_second_edit_appends_history` 红，还原后绿。
> 全量 **285 passed**（276 + 9），端到端复验（造记忆 → PATCH → edits → 列表）。

### 范围边界（M2-A）

**允许修改**：`src/linger/models.py`（仅新增 `CompanionProfile`）、
`src/linger/storage/base.py` + `sqlite.py`（新增表与方法）、`src/linger/orchestrator.py`、
`src/linger/compose/compiler.py`、`src/linger/server/app.py`（仅新增 companion 端点）、
新增 `src/linger/companion/` 模块与 `tests/test_companion.py`。

**不得修改**：`src/linger/social/` 的既有语义（角色图谱与人际知识库是只读消费方）、
`src/linger/memory/` `src/linger/affect/` `src/linger/relation/` `src/linger/persona/`、
既有测试文件的断言值、**不得引入新第三方依赖**。

## M1.5 验收清单（前端 + 场景运行时）

> 两组独立验收：A 组是「人眼可见的陪伴感」，B 组是「角色扮演的第二层」。
> 判据必须能证明会失败。

### A 组：前端最小可跑界面

- [x] `GET /` 返回 200，`content-type` 含 `text/html`
- [x] 页面**无外部 CDN 依赖**（离线可跑，不引 React/Vue/Tailwind 的 CDN）
- [x] 页面含五个可见区块：人格选择 / 对话区 / 情绪指示器 / 记忆面板 / trace 面板
- [x] JS 调用的端点与后端一致：`GET /v1/personas/templates`、`POST /v1/sessions`、`POST /v1/sessions/{id}/messages`
- [x] SSE 用 `fetch` + `ReadableStream` 或 `EventSource` 消费，且按 `event:` 分流
- [x] `affect` 事件到达时，情绪指示器的**文字与颜色都变化**（不是只在 console 打印）
- [x] `delta` 事件到达时，文本**逐段追加**到气泡（不是等 done 一次性显示）
- [x] `done` 事件到达后，记忆面板与 trace 面板**自动刷新**
- [x] 断线/404/422 时页面显示可读错误，不白屏
- [x] 静态资源由后端挂载（`/static/*` 可访问），刷新页面不掉状态

> **A 组状态（2026-10-22 完成，commit `23b5504`）**：10 条全部通过，17 个测试钉住
> （静态资源 HTTP、五区块 DOM、零 CDN 的 7 种模式检查、JS 端点与后端一致性、
> SSE 解析四个边界、六种情绪标签的 JS/CSS 映射与引擎一致性）。
> 另经真实 uvicorn + 模拟前端完整流程复验：页面 3.8KB、事件顺序
> `thinking→affect→thinking→delta×13→done`、13 段 delta 拼成完整回复、记忆面板显示 2 条。
>
> **实现中撞到的真问题**：`TextDecoder` 必须带 `{stream: true}`。
> 实测按 64 字节切片时，中文多字节序列会被切断（0xe5 首字节落在一块边界上），
> 不做增量解码会直接抛 `UnicodeDecodeError`——这个问题只有用小块读才暴露，
> 一次读完整个响应是发现不了的。

> **B 组状态（2026-10-22 完成）**：12 条全部通过，**经独立验收**——不只是跑它写的测试，
> 而是自己写脚本逐条核对（含「已完成节拍是否真的从 prompt 消失」的逐轮验证）。
> 合并 A 组前端后全量 **139 passed**（118 + 21）。
> 场景进度落在新表 `scenario_progress`（session_id 主键），Repository 抽象新增
> `save_scenario_progress` / `get_scenario_progress`；节拍推进在回复落库之后，与写记忆同一时机。
> 向后兼容：`create_session(..., scenario="一段文本")` 与 `compose(scenario="一段文本")` 仍按原样拼接。
>
> **独立验收补记（判据的精确化）**：原判据「已完成的节拍不再出现在后续 prompt 里」
> 表述不够精确，会误判。准确说法是「**本轮开始时已完成的**节拍不出现在**本轮** prompt 里」——
> 本轮正在推进的那个节拍，在本轮 prompt 里是**必须**存在的（否则 AI 不知道要推进它）。
> 逐轮实测：第 1 轮 3 个节拍全在 → 第 2 轮 b1 消失 → 第 3 轮 b1/b2 消失 → 第 4 轮全消失并出现收束提示。

### B 组：场景运行时（角色扮演第二层）

- [x] 新增 `src/linger/scenario/` 模块，含结构化 `Scenario` 模型
- [x] `Scenario` 字段齐备：`id`/`title`/`world`/`setting`/`opening_narration`/`user_role`/`beats`/`constraints`/`exit_conditions`
- [x] 至少 **2 个内置场景** YAML（`src/linger/templates/scenarios/*.yaml`）
- [x] `GET /v1/scenarios` 返回场景列表（含 id/title/setting 摘要）
- [x] `POST /v1/sessions` 支持 `scenario_id`；不存在的 id 返回 **404**
- [x] 编译器的 `scenario` 块包含三样东西：场景设定 + **未完成节拍** + 约束
      （不再只是把字符串原样拼进 prompt）
- [x] 节拍按**关键词 trigger** 推进（确定性，可复现，不用模型判定）
- [x] 已完成的节拍**不再出现在**后续 prompt 里（否则 AI 会反复推进同一节拍）
- [x] 场景进度**持久化**：重建 Orchestrator 后节拍进度不丢
- [x] 一轮结束后 `done` 事件或 trace 里能看到当前节拍状态
- [x] `python -m pytest` 全绿，且测试数 **> 101**
- [x] 干净克隆后 pytest 通过

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