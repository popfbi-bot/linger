# 灵犀 Linger

**一个给 AI 的状态引擎——让人格、情绪、记忆、关系四件事真正活起来，而不是每次对话都从零开始。**

一键赋予 AI 一个角色（人格卡 + 场景剧本 + 关系档案），它就有了**记性**、**脾气**、**分寸**，能陪你聊下去。

> 私有仓库，第一版能跑之前不公开。

---

## 它解决什么问题

大多数「AI 角色扮演」是**每轮失忆**的：换个会话人格就丢了，说话没分寸，情绪永远不变，关系永远停在原地。

Linger 用五个状态引擎解决这件事：

| 引擎 | 回答的问题 | 存在哪 |
|---|---|---|
| **人格** PersonaCard | 「她是谁」 | 库中的一行（可版本化） |
| **情绪** PAD 状态机 | 「她此刻什么心情」 | 连续状态，每轮演化 |
| **关系** RelationState | 「我们是什么关系、到哪一步了」 | 等级 + 亲密度 + 未消解冲突 |
| **记忆** 六层模型 | 「她记得什么」 | L2~L5 分层，带来源标注 |
| **陪伴档案** CompanionProfile | 「你知道我是谁吗」 | 人生阶段 + 关系角色 + 需求 + 风险 |

最后这层是 M2 加的。前四层是「她这会儿的状态」，陪伴档案是「**她对你的了解**」——
换个会话也不丢，用户还能手动校正（「其实我是青年期」）。

再加上**人际知识库**（七份语料，1500+ 条结构化知识）和**场景运行时**（结构化剧本 + 节拍推进）。

---

## 快速开始

### 免密钥跑起来（推荐先跑这个）

默认用 Mock 模型 + 哈希嵌入，**不需要任何 API key**：

```bash
pip install -e ".[dev]"
python examples/serve.py
```

打开 <http://127.0.0.1:8000>，选人格、选场景，开始聊。

> Mock 模型只会机械回应（它是个占位件，不是真 AI）。
> 想看真实效果，配置真模型：

```bash
export LINGER_LLM=openai
export OPENAI_BASE_URL=https://api.deepseek.com/v1
export OPENAI_API_KEY=sk-...
export OPENAI_MODEL=deepseek-chat
python examples/serve.py
```

真模型有超时 / 重试 / 降级 / 熔断，默认值就能用，也可以调：

```bash
export LINGER_LLM_FALLBACK=mock       # 主模型挂了降级到谁；空 = 不降级
export LINGER_LLM_TIMEOUT=20          # 首 chunk 超时（秒）
export LINGER_LLM_TOTAL_TIMEOUT=120   # 整轮回复的总时限（秒）
export LINGER_LLM_RETRIES=1           # 每个 provider 的重试次数
export LINGER_LLM_BREAKER_THRESHOLD=5 # 连续失败几次开熔断
export LINGER_LLM_BREAKER_COOLDOWN=60 # 熔断冷却秒数
```

`GET /v1/llm/health` 看实时状态：谁在熔断、上一次调用降级了没有、首字用了多久。

### 跑测试

```bash
python -m pytest        # 598 passed
```

### 命令行演示

```bash
python examples/demo.py         # 三轮完整链路：情绪演化 / 记忆提炼 / 关系升级 / trace
python examples/demo_toxic.py   # 有毒关系信号 + 关系角色识别
```

---

## 核心概念

### 一轮对话的流水线（顺序是契约，不能重排）

```
记忆检索 ┐
         ├─→ Prompt 编译 → LLM 流式 → 回复落库 → 写记忆 → trace
状态更新 ┘
```

三条硬约束（每条都对应一个已知的坑）：

1. **只注入通过相关性闸门的记忆** —— 检索反噬的解药。
2. **记忆检索与状态更新并行**，并行任务之间不共享可变状态。
3. **回复落库后才写新记忆**，绝不在生成回复的同一轮里写。

### 人际知识库：七份语料，三种维度

| 语料 | 维度 | 回答的问题 |
|---|---|---|
| 初遇美好 / 初遇糟糕 / 暗生情愫 / 相处雷区 | `stage` | **行为边界**：这个阶段该做什么、不该做什么 |
| 人生阶段陪伴需求与角色 | `companion` | **陪伴需求**：每个年龄阶段需要什么 |
| 陪伴缺失与有毒关系 | `companion_negative` | **正在经历什么**：被消耗/被贬低/被忽视的信号 |
| 亲密陪伴关系中的人格身份全录 | `relation_role` | **用户是谁**：210 个关系身份位，按 14 个维度索引 |

三个检测器，各管一件事，**边界互不越界**：

| 通道 | 回答的问题 | 是否惩罚关系 |
|---|---|---|
| `RedFlagDetector` | 用户做错了什么 | ✅ 是（`unresolved_conflicts`） |
| `ToxicSignalDetector` | 用户正在经历什么伤害 | ❌ 只提示 |
| `RelationRoleDetector` | 用户在关系里是谁 | ❌ 只提示 |

角色图谱还能算出**互补角色**——用户在演「受害者」时，AI 该补「倾听者 / 情绪稳定器 / 避风港」；
用户在演「焦虑型依恋者」时，该补「安全基地 / 情绪稳定器 / 倾听者」。
这让 AI 的回应从「共情」升级为「补位」。

### 场景运行时

角色扮演需要第二层——「我们在哪、发生什么」。

场景是结构化 YAML（9 个字段），带**节拍**（beat）。节拍按关键词 trigger 推进，
进度**持久化**（重建 Orchestrator 不丢）。已完成节拍不再出现在后续 prompt 里
——否则 AI 会反复推进同一节拍，这是场景运行时最典型的翻车方式。

内置两个剧本：`rainy_bookstore`（雨天的书店）、`late_night_store`（深夜便利店）。

---

## HTTP API

| 方法 | 路径 | 作用 |
|---|---|---|
| GET | `/health` | 健康检查 |
| GET | `/v1/personas/templates` | 人格模板清单 |
| POST | `/v1/personas` | 从模板实例化人格 |
| GET | `/v1/personas` | 人格列表 |
| GET | `/v1/scenarios` | 场景列表 |
| GET | `/v1/scenarios/{id}` | 场景详情 |
| POST | `/v1/sessions` | 创建会话（可带 `scenario_id`） |
| GET | `/v1/sessions/{id}` | 会话详情 |
| POST | `/v1/sessions/{id}/messages` | **SSE 流式对话** |
| GET | `/v1/sessions/{id}/memories` | 记忆列表 |
| GET | `/v1/sessions/{id}/memories/export` | 记忆导出 |
| DELETE | `/v1/memories/{id}` | 删除记忆 |
| GET | `/v1/traces/{turn_id}` | 单轮回溯 |
| GET | `/v1/companion/{persona_id}/{user_id}` | 陪伴档案（不存在返回空档案，非 404） |
| PATCH | `/v1/companion/{persona_id}/{user_id}` | 手动校正人生阶段 |
| GET | `/` | 静态前端首页（不在 OpenAPI schema 中） |

### SSE 事件序列

```
thinking(recall) → affect → thinking(compose) → delta* → done
```

- `thinking`：可解释性信号（召回了几条、prompt 多少 token）
- `affect`：情绪标签 + PAD 三维 + 关系等级（前端据此改表情、颜色、速度）
- `delta`：逐段文本
- `done`：turn_id / used_memories / state（情绪·关系·红灯·有毒信号·角色）/ usage

---

## 项目结构

```
src/linger/
├── models.py         # 数据模型（PersonaCard / AffectState / RelationState / MemoryNode …）
├── orchestrator.py   # 一轮对话的流水线编排（顺序是契约）
├── persona/          # 人格卡与模板
├── affect/           # PAD 情绪引擎：冲击评估 + 状态演化 + 表达提示
├── relation/         # 关系引擎：等级门槛、tick、称呼与边界
├── memory/           # 六层记忆：提取 / 去重 / 检索 / 嵌入
├── social/           # 人际知识库（七份语料）+ 三个检测器 + 角色图谱
├── companion/        # 陪伴档案：阶段识别 + 需求/风险汇总 + 档案更新
├── scenario/         # 场景运行时：Beat / Scenario / 进度持久化
├── compose/          # Prompt 编译器（分层块 + 预算 + 缓存边界）
├── server/           # FastAPI 服务 + 静态前端
├── storage/          # Repository 抽象 + SQLite 实现
└── templates/        # 人格 YAML + 场景 YAML
```

### Prompt 编译的块顺序

拼接顺序必须「**稳定 → 动态**」，同一人格的所有用户共享前缀缓存，成本可降 50~70%：

```
persona → constraints → ai_boundaries ⟂ → relation → social → scenario → affect → roles → companion → memory
```

`⟂` 是缓存断点。断点之前是**稳定前缀**（同一人格的所有用户共享），之后是动态块。
顺序变更需评审。

---

## 路线图

| 里程碑 | 状态 | 内容 |
|---|---|---|
| M0 | ✅ | 骨架 + orchestrator + 事件契约 |
| M1 | ✅ | HTTP 服务层 + SSE 流式 + 记忆管理与 trace 回溯 |
| M1.5 | ✅ | 前端界面 + 场景运行时 |
| M2-A | ✅ | **陪伴档案**：人生阶段 + 关系角色 + 陪伴需求 + 有毒风险，持久化并注入 prompt |
| M2-B | ✅ | 前端陪伴档案面板（让用户看得见「她记得我是谁」，并可手动校正） |
| M2-C | ⏳ | 记忆管理前端（可编辑记忆） |
| 后续 | ⏳ | 多人格 / 多用户登录态、场景 YAML 可视化编辑 |

详见 [`docs/ROADMAP.md`](docs/ROADMAP.md)（含每一步的验收判据）与
[`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md)。

---

## 开发纪律

**验收判据必须能证明「会失败」。** 测试全绿不等于判据对——
「永远绿」的判据和「正确」的判据长得一模一样。每个里程碑的判据都写在 ROADMAP 里，
交付时要贴出「判据被弄坏时会红」的证据。

**不信自述，独立跑。** 数字要自己跑，不要抄别人的报告。

---

## 许可

MIT
