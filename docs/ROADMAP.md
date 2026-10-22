# Linger M0 路线图与验收清单

> 目的：钉住「可运行」的验收判据，避免"写了文档但从不验证"。
> 每个里程碑必须能独立演示，且验收标准**能被证明会失败**（见 story-os 纪律「判据要能证明会失败」）。

## 当前状态（2026-10-22）

- ✅ 骨架已推送 main（commit `1c60583`）
- ✅ Issue #1 已交付并由 PR #2 合并（squash sha `02ff633`）：`orchestrator` + 事件类型 + 演示 + 测试
- ✅ **M0 验收全部通过**（详见下方勾选清单）
- ✅ **人际知识库（Social Knowledge Base）已落地**（commit `874ad94`）：五份语料 → 1113 条结构化行为条目，按阶段/极性索引，注入编译器；全量 24 passed
- ✅ **Issue #3 已交付并由 PR #4 合并（squash sha `9095764`）**：红灯检测接入关系引擎 + AI 行为边界注入；全量 **55 passed**
- 🚧 **下一步（M1）**：SSE HTTP 服务 + 记忆管理 API + 场景运行时（用正面清单生成剧本节拍）

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