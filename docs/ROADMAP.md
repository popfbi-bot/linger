# Linger M0 路线图与验收清单

> 目的：钉住「可运行」的验收判据，避免"写了文档但从不验证"。
> 每个里程碑必须能独立演示，且验收标准**能被证明会失败**（见 story-os 纪律「判据要能证明会失败」）。

## 当前状态（2026-10-24 更新）

> **M4 全部收口，open issue 归零。** 基线 **995 passed / 9 skipped / 0 failed**（真 Windows 11）。
> 九棒：#31 场景编辑 → #32 PostgreSQL Repository → #33 账号体系 → #34 人格市场后端 →
> #39 市场前端 → #35 语音链路 → #36 成本看板 → #37 评测入 CI → #38 事件评估换小模型。
> 基线从 685 涨到 995。**CI 第一次能回答「她还像不像她」（#37 的 12 条评测）。**
> 两个已知尾巴（不占棒次）：`evals/run.py` 不自举 sys.path；附件通道下载 404。

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
- ✅ **M3-C 人格管理完成**（NPC 交付 PR #20，squash sha `19c6f1e`，issue #19 已关闭留痕）：
  `DELETE /v1/personas/{id}` 软删除（200 返回 `{deleted,name}`，不存在/已删均 404，非幂等）；
  **三处读取全部过滤 status**（`list_personas` / `get_persona` / `find_persona_by_template`）；
  「删除模板人格 = 重置这段关系」的语义写进注释；软删除不级联（turn/memory 一行不动）；
  前端「我的人格」列表（`#my-persona-list`）放模板下方不做 tab。全量 **350 passed**（327 + 23）。
  **独立验收**：12 项判据逐条核对 + 三处 status 过滤各变异变红。
- ✅ **M3-D 日记完成**（NPC 交付 PR #22，squash sha `3a74af9`，issue #21 已关闭留痕）：
  架构 §3.3.2 巩固管线第 6 步落地——`DiaryEntry` 模型 + `diary_entry` 表
  （**`UNIQUE(persona_id,user_id,date)` + upsert**，一天一篇，重写覆盖不新增）；
  `src/linger/diary/flow.py` 生成第一人称日记（实测 220 字，目标 200）；
  三个端点：`POST .../diary/generate`、`GET .../diary`、`GET .../diary/{date}`；
  前端「她的日记」卡片（列表 + 写下今天）。全量 **448 passed**（350 + 92 + 6）。
  **验收**：16 项判据逐条核对；三条变异（冲突键改 id / 去掉无对话检查 /
  `_flow_check` 恒空）全部变红。
  **⚠️ 验收修正一处**：原判据把「他说」「我说」单独出现也算流水账，实测属误伤
  （「他说面试砸了的时候，我心里一沉」是正常回忆叙述），改为「用户说」单独报、
  「他说+我说」同现才报。这是本窗口写 issue 时的设计错误，NPC 忠实实现了错误判据。
- ✅ **记忆准入修复**（2026-10-22，`a24af1c`）：用户旅程走查发现**说「今天面试砸了，
  心里堵得慌」记忆面板 0 条**，更严重的是「妈妈病了」「爸爸住院了」也记不住。
  根因：准入条件只有「含『我』字」或「含地名/机构」两条，而中文主语省略是常态——
  **用户越自然，AI 越健忘**。讽刺的是 `_HIGH_IMPORTANCE`（妈/爸/病/手术）与
  `_EVENT`（面试/复查/加班）本来就在用，只是只用于「重要性加权」，没用于「要不要记」，
  两套词表脱节。修法：准入加到四条通道 + 长度改两档（4 字以下丢，4~5 字只放行
  命中准入词表的）+ `_EVENT` 补「工作/上班/职场」；**刻意不纳入纯情绪词**
  （「最近很累」记下来只稀释记忆面板）。全量 **466 passed**（+18）；
  变异（准入回退 / 长度一刀切）两个都红。
- ✅ **M3-E 聊天记录蒸馏完成**（NPC 交付 PR #24，squash sha `b0cbdcf`，issue #23 已关闭留痕）：
  「一键赋角」入口 C——`src/linger/distill/`（parse / fingerprint / compose）
  + `POST /v1/personas/distill`。**风格指纹是确定性算法**（句长中位数、emoji 率、
  跨消息口癖、高频词），LLM 只补 `meta`/`traits`/`values`；**LLM 不许覆盖 voice**
  （实测：模型想写「书面语/句长[30,45]/emoji 0.9」，最终仍是「口语/[4,9]/0.0」）。
  合规：`authorized` 必填且必须 true，落库标 `provenance.type="cloned"`。
  全量 **560 passed**（466 + 85 + 9）。
  **验收**：16 项判据逐条核对；三条变异（去掉授权校验 / 允许模型覆盖 voice /
  口癖按频次）全部变红。**验收修正一处**：口癖不再带边缘标点（原实现只挡
  「全是标点」，挡不住「吧，」这类首尾挂标点的片段）。
- ✅ **M3-F 场景自建完成**（2026-10-22，本窗口实现，`54ceee1`）：原来只能从内置
  YAML 选场景，用户造不了自己的。新增 `src/linger/scenario/writer.py` +
  `POST /v1/scenarios` + `DELETE /v1/scenarios/{id}`；**写用户目录
  `~/.linger/scenarios/`**（`LINGER_SCENARIO_DIR` 可覆盖），不写包目录——
  `pip install` 后那里只读且升级会被覆盖。`loader` 改双目录扫描，同 id 用户版覆盖内置版。
  节拍允许只写一句话（自动编号 b1/b2）；重复 id → 409 不静默覆盖。
  前端场景卡片加「+ 自己写一个场景」表单。全量 **577 passed**（560 + 17）；
  变异（去掉 `newline="\n"` / 允许静默覆盖 / 列表不去重）三条全红——
  其中 `newline` 那条专门钉住「YAML 必须是 LF」这个踩过三次的坑。
- ✅ **M4-A LLM 调用层韧性完成**（NPC 交付 PR #27，squash sha `c5c8cd0`，issue #25 已关闭）：
  超时 / 重试 / 降级 / 熔断；产品四处消费 LLM（对话 / 人格生成 / 日记 / 蒸馏）全部受保护。
  核心语义：**流式输出首 chunk 之后不切换 provider**（否则前后半句语气断裂，比报错更糟）。
- ✅ **M3-D 五步向导完成**（NPC 交付 PR #28，squash sha `f1058ce`，issue #26 已关闭）：
  四条创建入口的最后一条（D）——表单即 Schema 可视化。
- ✅ **M4-B CI 护栏完成**（NPC 交付 PR #30，squash sha `9a8f287`，issue #29 已关闭）：
  `.cnb.yml` 落地，push 到 main 与面向 main 的 PR 都跑 `python -m pytest -q -ra`；
  install / test 拆两个 stage；`LINGER_LLM` 钉死 `mock`，CI 免密钥免网络。
  **验收方独立复现「会红」**：临时 `assert False` 用例 → 退出码 1 且打印 FAILED 名。
- ✅ **向导来源标记修正**（`9e502db`）：走查发现入口 D 的 `provenance.type` 与入口 B
  同为 `generated`。向导是用户一项项填的，应为 `authored`。新增
  `tests/test_persona_provenance.py` 钉住四条入口各不相同（变异验证：改回
  generated → 4 红）。
- 🚧 **#31~#37 已派工，排队串行执行**（2026-10-22 一次性派完，一棒一棒验收）：
  #31 场景编辑 → #32 PostgreSQL Repository → #33 账号体系 → #34 人格市场
  （依赖 #33）→ #35 语音链路 → #36 成本看板 → #37 评测入 CI（依赖 #30）。
  每个 issue 正文都写明「收到开工指令才动手」，避免多棒并行互相踩分支。
- 📌 **队列现状（2026-10-24 00:30 盘点，逐项核实非凭印象）**：
  **已完成并合并**：#31 场景编辑（`234b428`）、#32 PostgreSQL（`566a6f8`）、
  #33 账号体系（`5902f98`）、#34 市场后端（`3de810b`）、#39 市场前端（`2da3723`）、
  #35 语音链路（`4945b86`）。基线 **884（875 passed / 9 skipped / 0 failed）**。
  **未完成只剩三条**：#36 成本看板（2026-10-23 16:04Z 发开工指令，未交付）、
  #37 评测入 CI（排队）、#38 事件评估换小模型（排队）。
  串行顺序定为 **#36 → #37 → #38**；#38 排在最后，因为它会改 `evaluator.py`
  的事件判定，等评测进 CI 之后再动，回归才有护栏兜底。
- ✅ **M3-G 场景编辑完成**（2026-10-23，issue #31 已关闭，commit `234b428`）：
  **由本窗口实现**——NPC 两轮写完但 patch 被评论转义毁掉（代码里 `join("\n")` 的
  `\n` 字面量被反转义成真换行，hunk 行数对不上），自动修复三轮后撞上不可判定的
  三引号字符串模式，主动停手，按判据自行实现（M3-A 先例）。
  交付：`loader._source_of()` 按文件实际目录判来源、`GET` 详情带 `source`、
  编辑入口只给用户场景（前端双方向变异都验过）、内置拦截只拦新造影子、
  编辑沿用原 id 保进度、影子机制双钉条、`scenario_dir` fixture 上移 conftest
  autouse（修「全量红单独绿」的隔离问题）。
  全量 **685 passed**（666 + 19），变异 4 方向全红。
  NPC 的设计决策（评论里确认过的）全部采纳，commit 描述里致谢。
  **⇒ 后续 NPC 交付一律 base64**；自动修 patch 的尝试与止损理由记在
  工作区 `fix_patch2.py` 与 issue #31 关闭说明里。
  #31 的 NPC 已写完代码、全绿（它自报 693~695 passed）、变异 5 条全红，
  但 `git push` 403、`cnb git create-branch` 403（errcode 10023），
  **连建空分支都不行**。`CNB_NPC_ENABLE_WORKMODE=false` 是本机账号侧开关，NPC 开不了。
  #32~#37 因排队设计尚未开工（无分支无 PR，符合预期）。
  **关键事实**：每个 NPC 会话拿一份**新克隆**，上一轮的工作树不会带过来——
  所以「写完但没推上去」的成果**在平台上不存在**，只能重新做。
  **已验证**：本窗口自己的 token 有 `repo-code:rw`（建分支+删分支探针通过）。
  **兜底链路已定**：NPC 把 patch 全文贴进评论 → 本窗口 apply → 以本窗口身份推分支、
  开 PR、合并、关 issue，PR 描述写明代码是 NPC 写的。
  ⚠️ **附件下载走不通**：NPC 给的 `/-/files/issues/...` 链接，
  直连 404、Bearer 400(`Invalid argument`)、`cnb issues get-issue-files` 404。
  **只能贴评论，不能用附件。**
- ✅ **#31 闭环（见上 M3-G 完成条目）后，#32（PostgreSQL Repository）已发开工指令**
  （2026-10-23），交付纪律（base64 / 别试推送 / 别用附件）写进了开工评论。
- 🔴 **分片贴 patch 会被转义破坏（2026-10-23 上午实测）**：
  NPC 把 1071 行 patch 分 4 片贴进评论，拼接成功但 `git am` 报
  `corrupt patch at line 224`。根因：代码里的 `join("\n")`，
  那个 `\n` 是**两字符字面量**，经 markdown 代码块 + JSON 双层转义后
  被反转义成了真换行。同类还有 docstring 的 `"""` 后接换行等。
  **无法自动修复**——`"` 后接换行在 Python 里合法，盲目替换会把对的改坏。
  ⇒ **交付 patch 一律用 base64**（`base64 -w0`，无字符需要转义）；
  分片必须**行数对齐**并告知每片行数。兜底方案是逐文件贴完整内容（不走 patch）。
- ✅ **派工命名整改（2026-10-23，主公指出「为什么那么多重名的」）**：
  8 个 open issue 标题全带「（第 N 棒）」——把**流程信息塞进了标题**，
  列表里一眼扫过去分不清谁是谁。已全部改名，只留「做什么」。
  **规矩**：标题只表达**做什么**；排队关系、依赖关系写在评论与正文里，
  不塞标题。以后新派工的 issue 标题形如 `M4-B：事件评估换成小模型`，
  不带棒次、不带 issue 编号。
- 🔴 **提交推到了错误分支（2026-10-23 上午，M3-G 差点丢）**：
  实现 M3-G 时 HEAD 停在临时分支 `_fixcheck2` 上，`git commit` 与 `git push`
  都作用在**当前分支**，导致 `234b428` / `60a8478` 只进了 `_fixcheck2`，
  **origin/main 一直没有 M3-G**。当时汇报「已推 main」是错的——
  **push 返回成功 ≠ 推到了 main**。
  **铁律**：① 提交前先 `git checkout main` 并确认 `git branch --show-current`；
  ② 推送后必须 `git fetch` 再 `git merge-base --is-ancestor <sha> origin/main`
  回查，不采信 push 的返回信息；③ 任何「已完成」都要在**接收端**确认。
  **本次补救**：`git merge --no-ff _fixcheck2` 合回 main 并重推（`80e4f71`），
  已回查 `234b428` 在 origin/main。
- 🔴 **NPC 交付的 base64 patch 仍可能缺文件（2026-10-23，#32 第一棒）**：
  NPC 用 base64 分 4 段贴 patch，但第 1 段**内容损坏**（解码出二进制乱码，
  长度不是 4 的倍数），且 **`postgres.py` 整个文件不在 patch 里**——
  commit message 有 `create mode postgres.py`，正文却没有该文件 diff。
  已要求重交，改用**逐文件贴完整内容**（Plan C），不走 patch。
  **教训**：base64 解决转义问题，但解决不了「文件漏贴」；
  收到 base64 patch 后先核对 commit message 的文件清单与实际 diff 是否一致，
  再进入验收。
- 🔴 **#32 第一棒交付失败，已重做（2026-10-23，#32 第二棒）**：
  NPC 的 4 段 base64 里第 1 段被评论通道**压缩变形**（`\x08` 游程 + 字节替换），
  恰好吃掉 `postgres.py` 的 diff 头 + 类定义 + 前几个方法（约 7000~18300 字节）。
  我这边解码出 `postgres_schema.sql`（140 行）与 `tests/test_postgres_repo.py`
  （476 行）碎片，但后者**有 f-string 语法错误**（`{{b[0] for b in bad]}`），
  `py_compile` 报 SyntaxError——说明 NPC 自报的「685 passed」与文件对不上。
  按验收纪律：**不替 NPC 修代码、不把来源不可靠的碎片提交进 main**。
  已与 NPC 对齐：选**方案 A 重新开工**，重新发开工指令（`#32` 评论里），
  约定 base64 单段 / 4 的倍数折行 / 贴前 `git apply --check` 自检 /
  贴真实 pytest 输出。NPC 承诺基于当前 main 重写 `postgres.py` + 修正测试。
  工作区留档：`recovered_postgres_schema.sql`、`recovered_test_postgres_repo.py`
  （仅诊断证据，非交付物）。
- ✅ **#32 第二棒验收通过并合并（2026-10-23，merge `566a6f8`）**：
  NPC 重做交付：3 段 base64（87188 字符，全部 mod4=0），7 个文件，
  `git am` 一次干净应用。**九条判据全过**：30 个抽象方法全实现、方法集合一致
  （`open()` 经 `_NOT_PUBLIC_API` 显式排除，已记录）、33 条 SQL 零 `?`、
  11 张表与 `_SCHEMA` 一致、按配置返回 PG 且构造不连库、psycopg 在 extras、
  DSN 门控 + importorskip、业务层零 SQLiteRepository 引用。
  全量 **708 passed / 8 skipped / 0 failed**（基线 685）。
  变异 3 方向：删 `get_diary` → 4 红；SQL `%s`→`?` → 1 红。
  **已知局限**：pass 桩能骗过 `__abstractmethods__` 检查（issue 判据 1 的设计边界），
  NPC 的 c7 真库往返恰好覆盖此盲区。
  NPC 这次做到贴前自检（`base64 -d | cmp`、折行 cmp、干净 main `git apply --check`）、
  回读验证（从评论读回字符比对）、真实测试输出——**交付链路走通了**。
  #33（账号体系）已于 12:16 发开工指令。
- 🔴 **列表分页骗人：以为指令没发，差点重复开工（2026-10-23）**：
  `list-issue-comments` **默认只返回前 10 条**（`get-issue` 的 `comment_count`
  才是全量）。我据此误判「重新开工指令没送达」，重发了一次——而 NPC 其实
  早在 03:15 收到指令、03:29 就交付完了。**差点造成重复开工。**
  **铁律**：① 查评论必须 `--page-size 30`（或按 `comment_count` 翻页）；
  ② 回查「我发的东西在不在」要搜内容（`if '重新开工' in body`），
  不能只看最后一条的时间戳；③ 重发任何「开工/派工」指令前，先确认
  对方**没有**基于旧指令在干活。
- 🔴 **自检验证的不是接收端：#34 交付的 patch 缺 84%（2026-10-23）**：
  NPC 贴了 37 段 base64（PART 标记 + 逐行），**自己的收工报告写「从发出去的两段文本
  反向拼回 → 123897 bytes → sha256 一致」**。但本窗口从**平台上已发出的评论**
  反向抓取：37 段齐、base64 总长只有 **25804 字符**（声明 165196，缺 84%），
  解码直接失败，sha256 对不上；且**段的内容本身错位**——PART 1 开头不是 patch 头
  而是 `config.py` 的 diff 中段，PART 2 的前 60 字符在 PART 1 的第 188 字符处就出现过。
  ⇒ **它的自检验证的是脚本内存里的中间变量，不是平台真正收到的字节。**
  **铁律**：① 「交付前自检」必须**从接收端反向读回**再拼、算 sha256，
  不能只验自己发出去的字符串；② sha256 锚点之所以有效，是因为**接收端能算**——
  锚点只能证明「我这边拼的和我本地一致」，不能证明「平台收到的是这个」；
  ③ 段落偏移必须**逐段确认首尾相接**（`part_NN` 首 40 字符接 `part_(NN-1)` 末 40 字符）。
  **这是「写了不等于送达」的最新形态**：自检通过 ≠ 送达的内容正确。
- ✅ **#34 人格市场验收通过并合并（2026-10-23，merge `3de810b`）**：
  **⇒ 交付协议再次升级：`git bundle` 主推 + `git format-patch` 兜底 + sha256 锚点。**
  bundle 是 git 自己的传输格式，`git bundle verify` 校验对象完整性，**少一字节就红**；
  patch 是文本，少一行只是「好像能 apply」——那正是上次失败看不见的地方。
  实测：bundle `ecc6197f…75c73bdc`、`git bundle verify` is okay、
  patch `c915ecb9…a3662f36`，两者均与声明逐字节一致。
  验收：全量 **804 passed / 9 skipped / 0 failed**（基线 754，+50）；
  本窗口另写独立自验脚本（真调 API）十条判据全过；
  变异 4 方向实测红条数 **5 / 4 / 3 / 3**（NPC 自报 5/4/3/2）。
  **NPC 主动查自己账**：上一轮的「818 passed」它回仓库核对发现分支与提交
  **根本不存在**，主动认账重做——这是本批最可贵的交付动作。
  设计论证全部采纳：上架存**人格卡快照**（审核过的字节 = 分发的字节）、
  归属写 `provenance.owner`（无主默认不可上架）、审核权走配置名单（不配 = 没人能审）、
  作者不能审自己、判据 6 选幂等复用、非 approved 对外 404 而非 403。
  **NPC 自查出的真 bug**：`settings = Settings()` 是 import 时求值一次的快照，
  改环境变量不跟着变 → 审核员名单读到空 → 「没人能审」且**无任何报错**（静默故障），
  已改为每次现读。
  ⚠️ **#34 遗留缺口（已开 #39 补）**：服务端创建人格时**不写 `provenance.owner`**
  （只补 `type`），而归属检查要求 `owner == 当前用户` ⇒ 用户创建的人格无法上架。
  客户端可声明 owner（且只能声称自己的），但**前端没做** ⇒ 市场功能用户碰不到。
- ✅ **#39 市场前端验收通过并合并（2026-10-23，merge `2da3723`）**：
  #34 的收尾——补上「用户点得到、上架得了」。交付走 **bundle 主推**：
  NPC 贴 6 段 base64 bundle + 17 段 patch，`git bundle verify` **is okay**
  （对象级校验，少一字节就红），不需要外部 sha256 声明也可靠。
  验收：全量 **836 passed / 9 skipped / 0 failed**（基线 804，+32；
  9 个 skip 全是 PostgreSQL 缺 psycopg，node v22 在环境里所以 DOM 测试真跑）；
  本窗口另写两套独立复现——**真实 HTTP 14/14**（匿名读 200 / 匿名写 401 /
  登录创建带 owner / 声明别人 403 / 匿名创建无 owner）+ **真 DOM 7/7**
  （四张卡只有自己的有上架入口，别人的/无主的/自己的 cloned 都没有）。
  变异双向实测：前端去掉归属判断 → 4 红；服务端 `_stamp_owner` 变 no-op
  → 本窗口 2 红 + NPC 测试 6 failures。还原后双双复绿。
  **设计采纳**：归属只认当前账号（声称别人 → 403，落库前拦）；
  `_persona_brief` 露 owner + provenance（前端不必猜「列表里的都是我的」）；
  模板实例化只认领一次（第一行归第一个登录用户，后来者抢不走）；
  上架按钮**不进 DOM** 而不是置灰（看不见才点不到）。
  **交叉复核（2026-10-24，另一轮会话独立做的）**：结论与该记录一致——
  登录 → `POST /v1/personas` 带顶层 `owner` → `provenance.owner` 被盖章
  → 上架 **201 pending_review**（#34 缺口真补上了，此前必然 403）；
  声称别人的 owner → **403**（落库前拦）；匿名创建 → provenance 无 owner。
  ⇒ 两个独立会话、两套不同方法（HTTP 直调 vs 脚本工厂）得到同一结论。
  ⚠️ **验收方修正一处**：NPC 越界改了 `docs/ROADMAP.md`（issue 明令别动 docs），
  合并时摘出，本章由本窗口重写——**文档归一窗口管**，否则两处各写一份必然漂移。
  #35 语音链路、#36 成本看板、#37 评测入 CI、#38 事件评估换小模型继续排队。
- ✅ **#35 语音链路验收通过并合并（2026-10-23，merge `4945b86`）**：
  听（`POST /v1/asr`）+ 说（`POST /v1/tts`）+ 情绪韵律（PAD → rate/pitch/energy）。
  交付照 #34 的 bundle 协议，**这次补齐了 sha256 双锚点**（base64 文本 + 解码字节）
  与回读核对：bundle `d07225d5…`（39970B）、patch `fdb107b0…`（97928B），
  我这边拼接后逐字节一致，`git bundle verify` is okay。
  验收：全量 **884（875 passed / 9 skipped / 0 failed）**，基线 836，+48；
  我的独立复现 **HTTP 24/24**（两种信封等价、空/坏/非音频全 422、
  高低 arousal 的 rate 1.23 vs 0.78 方向对、register 透传、cadence 随句长变、
  cloned 卡 403、SSE 序列不变且 ASR/TTS 零调用）+ **DOM 18/18**
  （点麦克风真发 multipart 且不手写 Content-Type、结果进输入框不自动发、
  轨道真停、播放按钮带 persona_id+user_id、不支持录音时按钮 disabled 而文字输入照旧）。
  变异双向：`prosody_for` 写死 → 我 2 红 + NPC 2 failures，还原复绿。
  **设计亮点**：mock 有确定性的输入相关性（不是恒返回一句）；mock WAV 是结构合法的
  真 WAV（前端 `Audio` 能解）；`X-Prosody-*` 响应头把韵律变成可观测事实；
  两种请求信封（multipart / base64）在入口合并成同一条字节流。
  ⚠️ **验收方修正一处（重要）**：`test_default_path_never_touches_network`
  把 `socket.socket` **类本身**换成 boom，在 Windows 上必挂——
  ProactorEventLoop 在 `asyncio/windows_events.py` 里做
  `isinstance(conn, socket.socket)`，类变函数后抛 TypeError，而这异常发生在
  recv 回调里 → **事件循环静默失效、请求永不返回**。表现不是「测试红」，
  是「全量卡在 97% 不动」（实测挂死 13 分钟）。CI 是 Linux 所以一直绿。
  修法：只掐 `create_connection` / `getaddrinfo` / `socket.connect` / `connect_ex`。
  并验证没削软：往 `MockASR.transcribe` 注入真 `create_connection` → 秒红。
- ✅ **#36 成本看板验收通过并合并（2026-10-24，merge `1660629`）**：
  计量层（`cost/{meter,pricing,report}.py`）+ `GET /v1/costs` + 前端面板。
  交付照 bundle 协议并**升级到三层 sha256**（base64 文本 / gz 字节 / 解压后）：
  bundle 41 片 `5c5131…`、patch 38 片 `7160fd…`，**六个锚点全对上**。
  **本窗口补了 NPC 做不到的一项：真 Windows 11 全量 913 passed / 9 skipped /
  0 failed（278s）**——NPC 的 Windows 证据只到 Wine 模拟，真 Proactor 它覆盖不到
  （而 #35 那个挂死恰好出在真 Proactor 上）。真机验证通过，该风险关闭。
  七条判据独立自验全过（真调 API / 真查库 / 真 node）；变异 3 方向全红
  （未知单价当 0 → 3 红、失败不记账 → 2 红、写路径加聚合 → 2 红）。
  **设计亮点**：「未知单价返回 `None` 绝不返回 0」——0 会被读成「这次免费」，
  而真相是「不知道花了多少」，把不知道渲染成免费是比缺失更糟的谎言；
  `unknown_models` 把「不知道的是谁」列出来，前端 `null → 「未知」`。
  写路径严守 O(1)（单条 INSERT，聚合只留在 `/v1/costs` 查询时）。
  ⚠️ **验收方发现：范围外改动未声明**。NPC 顺手改了
  `social/{knowledge,roles}.py`、`test_social_redflag.py`、`conftest.py`
  （加 autouse fixture）、新增 `tests/_cjk_corpus/`，加
  `LINGER_SOCIAL_DATA_DIR` + `LINGER_SOCIAL_NAME_MAP` 两个开关。
  动因是「Wine 读不出中文文件名」——而**真 Windows 11 实测 7 个中文语料全部可读、
  临时目录写中文名正常**，该故障在目标平台不存在，是 Wine 模拟实现缺陷。
  ⇒ **为迁就验证环境而改产品代码，方向反了**；且**交付说明一字未提**（我靠
  `git diff --name-only` 才发现）。改动本身无害（不设环境变量即原行为），
  故接受合并，但**新立纪律：范围外改动必须在交付说明里单列一节，否则驳回重交**。
- ✅ **#37 评测入 CI 验收通过并合并（2026-10-24，merge `a33088b`）**：
  `evals/`（10 文件）+ 12 条用例分 persona/memory/boundary 三类，带权重子判据 +
  类别下限（0.85/0.80/0.85）+ 总分下限 0.85；CI 新增第三个 stage `evals`。
  交付 bundle 8 片 + patch 6 片，**三层 sha256 六个锚点全对上**（b64 `63fe92cf…`/
  `5c570a89…`、gz `6ae2ec6c…`/`25e284d5…`、解压 `8ead43b4…`/`344f8038…`）。
  **判据 4 双向验证**：NPC 用「mock 的问答分支改成客服腔」的**实现破坏**（不是改期望值，
  这个选择对——改期望值只证明测试会红，改实现才证明「她变了她能被发现」）；
  本窗口用同一破坏点、不同话术独立复现：**破坏 → 退出码 1 / 总分 0.68
  （persona 0.47、boundary 0.72、6 条用例红，明确标出「挂了句长/口癖/客服腔」），
  还原 → 退出码 0 / 0.96**。**最后那两行「挂了什么」是整个评测最有价值的部分**——
  它不说「分数掉了」，它说「林汐开始做总结了」。
  七条判据本窗口另写脚本独立验全过（诚实性：NPC 主动写「Windows 那遍我没跑，
  我不编数字」，本窗口补真 Windows 11 **951 passed / 9 skipped / 0 failed / 276s**，
  与其 Linux 951 一致）。范围只有 `.cnb.yml` + `evals/` + `pyproject.toml`，
  **无范围外改动**（#36 立的新纪律生效）。
  ⚠️ **验收方自踩的坑（值得记）**：我做判据 4 变异时用 `Path.write_text()` 把
  `mock.py` 写回，**Windows 上把整个文件的 LF 翻成 CRLF**，`git status` 显示
  226 行全变，差点把脏改动带进合并。改法：变异与还原都用 `read_bytes()`/`write_bytes()`。
  **教训：验收脚本自己必须遵守它检查的纪律**——否则验证器会污染被验证的对象。
  （这正是 issue 里反复警告的「改文件不要用 `open(path,"w")`」，而我是在一个
  检查别人代码的脚本里违反的。）
  两个非阻塞瑕疵：`python -m evals.run` 不自举 `sys.path`（本地没装包会
  `ModuleNotFoundError: linger`，需 `PYTHONPATH=src`）；`python evals/run.py`
  这种跑法会因相对导入报错（必须走 `-m`）。
- ✅ **#38 事件评估换小模型验收通过并合并（2026-10-24，merge `1501652`）**：
  `affect/event_model.py`（EventJudge + Verdict + 熔断/缓存/超时）+ orchestrator 接线。
  **形态：模型优先、规则兜底、失败静默降级**（provider=None 即纯规则＝旧行为）。
  交付：**一条评论、一个 b64（25612 字符、一行、mod4=0）、三档 sha256**
  （b64 `fefc1714…`/gz `ac1904e2…`/patch `b6280e7c…`），
  **树哈希 `76ffa4bb…` 与本窗口逐字节一致**——这是内容收敛的最强证明。
  验收：十二条判据独立自验全过（含判据9 **真端到端**：`verdict.is_deep` → `relation.tick`
  → `deep_turns` 0→1，反向不涨）；**真 Windows 11 全量 995 passed / 9 skipped / 0 failed（314s）**；
  **评测 0.96 逐项持平**（persona .94 / memory .98 / boundary .97）——
  **#37 的护栏第一次实战，证明这棒没碰「她像不像她」**；
  变异 3 方向全红（摘降级链 → 我判据13红/NPC 80 failed+28 errors；超时 3→60s → 判据6红；
  `eval_source` 写死 model → 判据4红）。
  **核实 issue 红线**：`evaluator.py` 只改 docstring，函数体一字未变
  （仍是 `len>120 or 词表`），签名与规则行为完整保留。
  **设计采纳 3 条**：
  ① **模型只判 bool，刻度仍由规则算**——NPC 主动指出的偏离，理由比 issue 正文更好：
     「模型给的浮点数没法测、会抖动、会越界；而『情绪移动多少』是产品刻度，不是语言理解问题。
     让它判浮点，等于把一个可测的量变成不可测的」⇒ **不补夹逼层，现形态更好**。
  ② **bool 字段与规则「取或」，模型不是否决权**——「模型加的是敏感度，
     否则一次抽风就能让关系倒退」。失败组件只该被允许往好的方向偏。
  ③ docstring 里封死了「顺手加词表」这条路：「词表永远差一个词，而这条规则的价值
     恰恰是它窄——宽了就会把寒暄也算成深夜长谈，关系等级会自己往上漂」。
- 🔴 **交付形态的终局结论：分片本身在产生故障，不要优化分片（2026-10-24）**：
  #38 的交付错了两轮——第一轮 28 片，前 14 片是**上一轮的另一份 bundle**（另一个 HEAD），
  拼不起来（NPC 自己二分定位到「gz 只能解到 476773 字节，崩溃点在片 15 内部 4273 字节」）；
  第二轮改成**一条评论、一个 b64、没有接缝**，零故障，树哈希逐字节一致。
  **同一个 NPC、同一份代码，差别只在交付形态。**
  NPC 的原话值得记住：「分片粘贴这个交付形态本身在产生故障，它每次都在
  『我以为拼对了』和『实际拼错了』之间赌一次。」
  ⇒ **以后交付优先「单条评论 + 单 b64 + 三档 sha256」；需要分片时，每 5 片回读算一次
  前缀哈希，并在头片写死「共几片、总长多少」。**
  另一个教训（NPC 自述）：贴到 15/28 停摆 45 分钟，根因是**上一轮会话结束、没有进程继续**
  ——「分片粘贴式的交付，退出条件必须写在会话里，而不是靠会话还活着」。
- ✅ **#33 账号体系验收通过并合并（2026-10-23，merge `5902f98`）**：
  交付用 **sha256 锚点**——NPC 贴了 14 段（中途被平台截断、丢 4 字符、自己手滑
  把一个 `E` 打成 `Q`，全部只有回读比对能抓到），但给出
  `sha256(m33.patch)`，本窗口按其拼接规则还原后 **sha256 逐字节一致**
  （`183820424430099947347ae1b83decc8b3935636f2ae4ae4e3cd42e67d4ef55f`）。
  **⇒ 交付协议升级：base64 + sha256 锚点**，这是目前最稳的交付方式，
  后续每棒照此。验收：全量 **754 passed / 8 skipped / 0 failed**（基线 708，+46）；
  本窗口另写独立自验脚本（真调 API，不复用 NPC 测试）八条判据全过；
  变异 4 方向实测红条数 **9 / 6 / 3 / 1**（NPC 自报 8 / 2 / 3 / 1，报得偏保守）。
  一处既有测试改动已核对认可：`test_postgres_repo.py` 表数 11→12（新增 account 表，
  断言方向未变，NPC 主动声明）。
- 🟡 **已知缺口：日记的「搬移」是复制，源 id 下留孤儿行（#33 遗留，不阻塞）**：
  认领时记忆是真搬移（`update_memory` 改 user_id 保 id），**日记不是**——
  `storage/base.py` 有 `update_memory` 却没有 `delete_diary`，日记删不掉，
  只能另存一份到账号名下，源行留在原地。
  用户可见行为全对（账号下恰好一份、幂等、孤儿不可见），但
  `accounts/service.py` 的 docstring 写「源 id 名下的记忆与日记都空了」——
  **这句话对日记不成立**，已要求 NPC 改文档。
  要真搬移需给 `Repository` 加 `delete_diary`（契约变更，要动 30 方法 × 2 实现，
  会破坏 #32 刚验收的 PG 前提）⇒ **记为后续项**，等 Repository 契约本来就要动时一起加。
- ⚠️ **本地绿 ≠ CI 绿：CI 跑 Python 3.11，本地是 3.13（每棒交付后必查）**：
  `.cnb.yml` 用 `python:3.11-slim`，本地是 3.13.12——**本地跑绿不代表 CI 跑绿**。
  已用 `ast.parse(feature_version=(3, 11))` 全仓扫过（src + tests）：**3.10/3.11 均语法兼容**，
  且未使用 `datetime.UTC` / `itertools.batched` / `typing.override` 等 3.12+ 专有 API。
  **检查脚本留在工作区**（一行 `ast.parse(..., feature_version=(3,11))` 扫全仓即可）。
  ⚠️ **语法兼容 ≠ 运行兼容**：3.11 与 3.13 的标准库行为差异仍可能咬人，
  每棒交付后要把这个检查跑一遍。
- ⚠️ **部署注意：密码哈希依赖 OpenSSL 的 scrypt**：
  `accounts/security.py` 用 `hashlib.scrypt`（标准库，issue 不许引 bcrypt/argon2）。
  但 **OpenSSL 不支持 scrypt 的环境会抛 `ValueError`**（某些精简 Linux 镜像），
  当前实现**没有 fallback**。
  CI 环境（Debian + OpenSSL 3.x）不受影响 ⇒ 不是 CI 风险，是**部署风险**。
  记为后续项：部署文档里写明「需 OpenSSL ≥ 1.1.1」，或加 pbkdf2 fallback。
- ✅ **端到端多轮验收完成**（2026-10-23 晨，真模型 + 真实服务器，非 TestClient）：
  起 `serve_ds.py` 连跑四轮深度倾诉，**回复质量与状态联动全部正常**——
  陪伴档案跨轮累积（中年期保持、角色 1→2 个）、
  第 4 轮「算了，说了你也不懂」被接住而没有硬追问。
  **但发现一个真问题**：四轮 `level` 全为 0、`deep_turns` 没涨。
  根因：`affect/evaluator.py` 的 `is_deep_turn` 靠 `len(text) > 120` 或
  十几词的 `_NEG_EVENT` 词表——「房贷」「加班」「不顾家」「撂挑子」全不在表里，
  **用户把心掏出来，关系引擎以为他在寒暄**。
  → 已派工 **#38 M4-B：事件评估换成小模型**（模型优先 + 规则兜底 + 失败静默降级），
  派工稿里附了完整实测对话作为需求依据。

## 未完成工作清单（2026-10-22 晚盘点）

> 每次盘点都逐项 grep 核实过，不凭印象。
> 2026-10-22 晚：剩余 7 项**全部派工完毕**（issue #31~#37），
> 按棒次串行执行，一棒合并后再开下一棒。

### M3 剩余（产品体验）

| 项 | 状态 | 说明 |
|---|---|---|
| 入口 A 模板市场 | ✅ | |
| 入口 B 一句话生成 | ✅ | 含 R1~R5 冲突消解 + 样例预览 |
| 人格管理（列表/删除） | ✅ | |
| 日记生成与阅读页 | ✅ | 架构 §3.3.2 第 6 步，一天一篇（唯一键 upsert） |
| 入口 C 聊天记录蒸馏 | ✅ | 确定性风格指纹 + LLM 只补推断字段；`authorized` 强制 + `cloned` 标记 |
| 入口 D 五步向导 | ✅ | PR #28；来源标记已修正为 `authored` |
| 场景运行时 + 节拍 | ✅ | M1.5 |
| 场景自建（表单） | ✅ | 写 YAML 到用户目录，双目录扫描 |
| **场景编辑已有** | ✅ #31 已完成（merge `234b428`，685 passed） | 由本窗口实现（NPC patch 被转义毁掉）；`loader._source_of()` 按实际目录判来源 |

### M4 平台化（工程化，离用户体验较远）

| 项 | 状态 |
|---|---|
| 多模型路由与降级 | ✅ | PR #27 已合并（超时/重试/降级/熔断） |
| CI 护栏（push / PR 自动跑测试） | ✅ | PR #30 已合并，`.cnb.yml` 在 main 上；**CI 跑 Python 3.11，本地是 3.13** |
| **语音链路（ASR/TTS + 情绪韵律）** | ✅ #35 已完成（merge `4945b86`） | 协议+mock+按配置选；韵律断言参数不断言字节；cloned 卡 403；884 tests |
| **人格市场（上传/审核/分发）** | ✅ #34 后端（merge `3de810b`）+ #39 前端（merge `2da3723`） | 红线 `cloned` 不许上架钉死（变异 5 红）；前端补「点得到、上架得了」，836 passed |
| **评测自动化接入 CI** | ✅ #37 已完成（merge `a33088b`，951 passed 真 Windows） | 12 用例三类；判据4 实现破坏→退出码1 |
| **事件评估换小模型** | ✅ #38 已完成（merge `1501652`，995 passed 真 Windows） | 模型判 bool + 规则算刻度；失败静默降级到原口径 |
| **成本看板与灰度** | ✅ #36 已完成（merge `1660629`，913 passed 真 Windows） | 未知单价返回 null 不返回 0；写路径保持 O(1) |

### 基础设施

| 项 | 状态 | 说明 |
|---|---|---|
| SQLite Repository | ✅ | dev / 单机够用 |
| **PostgreSQL Repository** | ✅ #32 已完成（merge `566a6f8`，708 passed） | 判据「不连库也能验」全部落实，CI 无 PG 时 8 个真库用例 skip |
| 前端匿名身份（localStorage） | ✅ | M3-B，同一浏览器稳定、不同浏览器隔离 |
| **真正的账号体系（登录/跨设备）** | ✅ #33 已完成（merge `5902f98`，754 passed） | 账号 user_id 即稳定 id + 认领迁移；防用户枚举做到响应体+耗时一致 |

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

## M4-C 人格市场前端（#39）

| 项 | 值 |
|---|---|
| 服务端（additive） | `POST /v1/personas` 收可选 `owner`，声称别人 → 403（落库前拦）；`_persona_brief` 露 `owner` + `provenance`；模板实例化只认领一次 |
| 前端（原生 JS） | 市场区块**常驻收起**——未登录也看得见、不弹登录墙；上架按钮只进「归属是自己」的卡，**不进 DOM** 而非置灰 |
| 测试 | `tests/test_market_frontend.py` 23 条（HTTP + 真 node 跑真 app.js 的 DOM harness）；全量 836 / 9 skipped / 0 failed |
| 交付形态 | git bundle 主推（`git bundle verify` 对象级校验）+ patch 兜底 |
## ⚠️ 观测空白：CI 运行状态查不到（2026-10-24）

本窗口推了多个合并提交（#34/#35/#36/#39 及 docs），但**无法用 CLI 确认远端 CI 是否绿**：
- `cnb` CLI 没有 pipeline/runs 子命令（`repositories` 模块 14 个工具里没有）；
- `cnb event get-events --date 26-10-24` 返回 404（errcode 5 Resource not found）。

**间接证据**：本地真 Windows 全量 913 passed / 9 skipped / 0 failed；
CI 环境差异已逐项分析（Linux vs Windows、Python 3.11 vs 3.13 语法兼容、
无 psycopg → 9 skip 一致），理论上应当绿。
**但这只是推断，不是观测**——「本地绿 ≠ CI 绿」这条纪律的反面同样成立。

⇒ **待确认**：之后若拿到 CNB 的 pipeline 查询方式（或 Web 控制台看一眼），
把 #34~#39 各提交的 CI 结果补记在此。在此之前，**不要声称「CI 已验证」**。

### 补充（2026-10-24 21:40）：空白被填了一半，而且「理论上应当绿」是错的

上面那段推断里，我逐项比对了 Linux vs Windows、Python 3.11 vs 3.13、无 psycopg
——**漏了最要命的一项：node**。

NPC 的 PR #40（`feat/market-frontend`）上带着两个 CI 修复提交，其中一条查到：

> `test_beat_roundtrip` **从 #31 起在 CI 里一直是红的**。`.cnb.yml` 的镜像是
> `python:3.11-slim`，没有 node，而它裸起 `subprocess` → `FileNotFoundError`。
> 本地有 node 所以全绿，面向 main 的 PR 一直没跑到它，于是没人发现。

**所以「本地绿 ⇒ CI 大概也绿」这条推断，在只知道三种差异的情况下就是猜。**
漏掉的那个差异不在语言/平台上，在**测试依赖的可执行文件**上——`tests/js/`
的那一堆判据是「用 node 跑真 app.js」，没有 node 就整片炸。

修法不是跳过，是**把执行环境补上**（`.cnb.yml` install stage 装 `nodejs`），
`needs_node` 只做本地兜底。配套立了一条判据：

> **跳过数基线是 9。它涨上去就是「node 装漏了」的信号**，不是正常波动。

本窗口实测：屏蔽 node 后跑那两个前端测试文件 → `25 passed, 6 skipped, 0 error`
（6 + 9 = 15，与 NPC 说的数字一致）；不屏蔽 → `1022 passed, 9 skipped`。

**仍未知的**：pipeline 跑没跑、绿没绿。但「至少有一个已知红点」这件事已经从
「不知道」变成「找到并修了」——这比一句「理论上应该绿」值钱。

## ✅ M4 收口后的端到端复验（2026-10-24 夜，真 uvicorn + 真 HTTP）

不是 TestClient，是 `uvicorn` 起真服务器、用 urllib 发真请求，14 项全过：

| # | 项 | 结果 |
|---|---|---|
| 1 | 模板列表 | 2 个模板 |
| 2 | 建人格（模板） | `p_b04cc31cbf` |
| 3 | 开会话 | `s_2e14c69cc1` |
| 4 | **聊天 SSE 流式** | 7 事件 / 7 字，类型 `thinking/affect/delta/done` |
| 5 | 日记 | 0 篇（需单独触发生成，正常） |
| 6 | **成本看板** | `calls=6 tokens=6995 cost=0.0`（mock 无单价 → 0，符合「未知不给假数」） |
| 7 | 注册 / 登录 | `u_b36c09a5e9` |
| 8 | **带 owner 建人格** | owner 已盖章 |
| 9 | 上架 | `l_d8ee40db06` pending_review |
| 10 | 公共列表不泄露 pending | 0 条，未泄露 |
| 11 | **审核 + 重复审核** | approve ok，重复 → **409** |
| 12 | **安装幂等复用** | 副本 `p_067655d477`，两次安装同一份 |
| 13 | ASR 空音频 | **422**（不是 500） |
| 14 | TTS | **真 WAV 98 字节，RIFF 头正确** |

**复验中发现的三处「假失败」，都是我的脚本问题、产品行为正确**：
① `POST /v1/sessions` 一直是 `{"type":"delta","data":{"text":...}}` 嵌套结构，我按平铺取了 → 修脚本；
② 服务器没设 `LINGER_MARKET_REVIEWER_EMAILS` → 审核 403「审核员名单里没有你」
   **这正是缺省安全方向（不配 = 没人能审）**，不是 bug；
③ 两次复验共用同一个库 → 注册 409「邮箱已注册」，改成「注册或登录」后正常。

### 「全跑通」的证据边界（诚实划线）

- ✅ **已验证**：995 passed / 9 skipped（真 Windows 11）；评测 0.96；
  真实服务器端到端 14/14（mock LLM）。
- ❓ **未验证**：① **CI 实际运行状态**——CLI 查不到 pipeline，只能靠环境差异推断
  （见上文「观测空白」）；② **真实模型**——本机无 `OPENAI_API_KEY`，全程 `LINGER_LLM=mock`；
  ③ **生产部署**——`LINGER_AUTH_SECRET` 未配（#33 遗留提醒）。
- ⇒ 说「代码与集成链路跑通」有据；说「CI 绿」「真模型效果」**没有据，不能说**。
