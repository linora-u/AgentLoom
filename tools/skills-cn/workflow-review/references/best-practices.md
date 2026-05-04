# Workflow Architecture Review — 最佳实践模式库（去仓库耦合版）

本文件提供可迁移的 workflow 评审模式，不绑定当前仓库示例。

---

## 模式 1：复杂度驱动架构选择（Single First）

**原则**：先验证单 Agent 是否足够，再升级为多 Agent。

### 何时优先单 Agent

- 任务链路短，分支较少
- 工具集合清晰且无明显冲突
- 无强并行需求

### 何时升级多 Agent

- Prompt 中条件分支过多，单 Agent 稳定性下降
- 工具重叠严重，路由错误频发
- 任务天然可并行，且子任务边界清晰

### 审核信号

- Worker 数量增加是否带来可验证收益
- 是否出现“为拆分而拆分”
- 是否定义了升级/回退策略

---

## 模式 2：Manager-Worker 契约化协作

**原则**：Supervisor 负责编排与边界控制，Worker 负责专职执行。

### 推荐实践

- Worker 必须有可调用契约（输入、输出、用途）
- 数据流显式化：上一步输出如何进入下一步
- Supervisor 避免二次改写 Worker 结果造成“翻译损耗”

### 典型风险

- Worker 共享隐式文件或隐式全局状态
- Worker 契约过于笼统（如只有 `query`）
- Supervisor 反复转述，导致语义丢失或重复劳动

---

## 模式 3：确定性逻辑下沉到 Tool

**原则**：LLM 负责理解、推理、生成；确定性逻辑交给 Tool。

### 决策树

1. 操作是否需要语义理解？
- 是：保留在 Agent
- 否：优先 Tool 化

2. 是否需要前后置流程（批处理、校验、写回）？
- 是：采用 Agent-as-Tool（外层 Python 控制）
- 否：可直接注入为普通 Tool

### 高频可 Tool 化信号

- 文件遍历、排序、过滤、计数
- 引用定位、规则匹配、格式渲染
- 固定流程循环（validate -> retry）

---

## 模式 4：任务分解边界与去重

**原则**：每个 Worker 的目标、输入、输出必须唯一，避免重复 delegation。

### 推荐实践

- 用“任务声明句”定义 Worker 边界（只做一件事）
- 明确禁止重复分析范围
- 增加去重检查：相同输入是否被多个 Worker 重复消费

### 审核检查

- 是否存在两个 Worker 产出同类结论
- 是否因边界不清导致工具调用重复
- 是否存在不必要的“中转 Worker”

---

## 模式 5：Evaluator-Optimizer 闭环（小样本先行）

**原则**：先建立可复现评估，再做架构改造。

### 推荐流程

1. 先选小样本任务集建立基线（不追求大而全）
2. 定义通过标准（功能正确性、工具调用正确性、输出质量）
3. 加入 edge case（长上下文、歧义输入、多次 handoff）
4. 改造后对比基线，确认收益

### 评审落点

- 是否有明确通过阈值
- 是否覆盖关键边界条件
- 是否避免仅凭主观“看起来更好”

---

## 模式 6：并行化与努力预算

**原则**：并行只用于独立子任务，并受预算约束。

### 并行前提

- 子任务输入独立
- 不共享可变状态
- 失败可隔离且可回收

### 预算控制

- 设定最大并发数
- 设定最大步骤/最大轮次/超时
- 评估 token 与时延收益是否覆盖协调成本

### 常见误区

- 盲目并行导致限流或资源争用
- 并行结果合并逻辑复杂，反而降低质量

---

## 模式 7：观测与恢复（Trace/Checkpoint/Retry）

**原则**：长链路工作流必须可定位、可恢复、可重试。

### 最低要求

- 关键节点有结构化日志（输入摘要、输出摘要、错误）
- 批处理有 checkpoint/progress 状态
- 重试有上限与退避策略
- 失败项可单独重跑，不拖垮全局

### 审核重点

- 错误信息是否可操作
- 中断后是否能从上次进度恢复
- 是否区分可重试错误与不可重试错误
### AgentLoom 内置 Checkpoint 配置

AgentLoom 提供**内置断点续跑/心跳监控系统**，无需应用层代码。配置全部居于 `config/system.yaml` 的 `checkpoint.*` 下：

| 字段 | 默认值 | 用途 |
|-------|---------|------|
| `checkpoint.enabled` | `true` | 全局开关。仅对暂时性脚本设 `false` |
| `checkpoint.cleanup_on_success` | `true` | `true` = 生产（成功后清理）；`false` = 调试（保留产物） |
| `checkpoint.max_resume_age` | `604800`（7 天） | 崩溃/中断任务的可恢复时间窗口 |
| `checkpoint.heartbeat_interval` | `5`（秒） | 心跳刷新频率，决定崩溃检测延迟 |

**崩溃检测阈值**：心跳文件超过 30 秒未刷新、PID 已死亡或 status 为 `stopped`/`exited` 时，任务被判定为 `crashed`。

**两级心跳**：
- **Supervisor 心跳**（`{task_id}/heartbeat.json`）：跟踪整体步骤数和进程存活性。
- **Worker 心跳**（`{task_id}/workers/{name}/heartbeat.json`）：聚合所有并发 worker 调用的 status/step。

**恢复流程**：重新运行时，框架恢复 Supervisor memory steps，已完成的 Worker 调用（通过 `input_hash` 匹配）直接返回缓存结果，从中断点继续执行。

### 配置最佳实践

| 场景 | 推荐设置 |
|------|------------|
| **生产** | `cleanup_on_success: true`，`max_resume_age: 604800`（默认值，可完全省略 checkpoint 段） |
| **调试/检查** | `cleanup_on_success: false`，`max_resume_age: 86400` |
| **暂时性脚本** | `enabled: false`（禁用所有开销） |
| **超大批量任务** | `heartbeat_interval: 5`（默认）；仅当磁盘 I/O 成为瓶颈时可适当增大 |
---

## 模式 8：Prompt 设计与输出契约

**原则**：Prompt 负责“决策”，模板化内容与固定格式由 Tool 接管。

### 推荐实践

- 避免在多个 Worker 中重复同一约束段
- 输出采用结构化契约（JSON/表格 schema）
- 在评审报告中明确区分 `[证据]` 与 `[推断]`

### 审核重点

- 是否存在大段重复模板占用上下文
- 是否把可程序化格式交给 LLM 记忆
- 是否把推断当事实陈述

---

## 模式 9：Prompt 质量基线（行业通用）

**原则**：先保证指令清晰与可检验，再追求复杂能力。

### 推荐实践

- 角色与目标明确：一句话定义角色、任务目标、成功条件
- 约束可执行：明确输入边界、禁止项、失败处理、升级条件
- 输出可验证：固定结构 + 必填字段 + 证据来源
- 用小样本回归：固定测试集对比改造前后结果

### 审核重点

- 是否存在“目标清晰但验收不清晰”的 Prompt
- 是否把关键条件写成“建议”而非“必须”
- 是否缺少对信息不足场景的处理策略

---

## 反模式速查

- 让 LLM 负责循环控制和状态持久化
- Worker 同时负责 IO、编排、分析三类职责
- 无预算地扩大 Worker 数量和并行度
- 缺少可复现评估就直接重构
- 报告建议不可验证、不可执行

---

## 模式 10：Skills 配置与集成

**原则**：Skills 在不修改框架代码的前提下扩展 Agent 能力；选择正确的调用模式，避免加载冲突。

### 三种调用模式

| 模式 | `allow-model` | 行为 | 适用场景 |
|------|--------------|------|---------|
| **强制注入型** | `"force-inject"` | 完整指令在 Agent 初始化时嵌入 system prompt；LLM 始终遵循 | 关键能力（记忆系统、安全规范）必须始终激活 |
| **按需加载型** | `true`（默认） | 出现在技能目录中；由 LLM 决定何时调用 `load_skill()` | 领域指南、工作流参考、可选知识 |
| **隐藏型** | `false` | LLM 完全不知道该 Skill 存在；仅通过 Hook 静默运行 | 事件采集、可视化、透明监控 |

### 推荐做法

- `force-inject` 应谨慎使用——每个注入的 Skill 都消耗 system prompt token 预算
- 验证路径解析：Skill 路径相对于 `AGENT_ROOT`（包含 `config/system.yaml` 的目录），而非 Agent YAML 文件位置
- 理解三层加载机制：全局（`config/system.yaml`）→ 自动发现（`AGENT_ROOT/skills/`）→ Agent 私有（`skills` 字段）。同名 Skill 后层覆盖前层并输出 warning
- 需要工具名映射时设置 `platform`（如 `"Claude"` 使用 `tools_mapping.Claude`）

### 典型风险

- 关键 Skills 缺少 `force-inject`（如未注入 `agent-recall-with-files` 导致跨会话记忆丢失）
- 混淆 `AGENT_ROOT` 和 Agent YAML 目录导致 Skill 路径解析错误
- 多层重复加载同一 Skill 但不了解覆盖行为
- 忘记 `allow-model: false` 的 Skill 即使通过 `load_skill()` 也无法被 LLM 加载

---

## 模式 11：Agent 可选参数最佳实践

**原则**：正确配置可选参数，避免静默失败和资源浪费。

### 关键参数

| 参数 | 默认值 | 常见陷阱 | 推荐做法 |
|------|--------|---------|---------|
| `model_type` | `llm.yaml` 的 `default_model_type`（通常为 `"common"`） | 指定不存在的类型导致运行时 `ValueError`（不会静默回退） | 验证类型在 `llm.yaml` 中存在；复杂推理用 `powerful`，简单任务用 `fast` |
| `tool_call_type` | `"code_act"` | 对 Supervisor 使用 `tool_call` 会限制编排能力 | Supervisor 和复杂 Worker 用 `code_act`；简单单工具 Worker 用 `tool_call` |
| `max_steps` | `80` | 简单 Worker 默认值过高（浪费资源）；复杂 Supervisor 可能过低 | 按任务复杂度设置；简单 Worker: 20-40；复杂 Supervisor: 60-120 |
| `planning_interval` | 未设置 | 长任务链无定期规划会迷失进度 | 预期 20+ 步的 Agent 设置为 3-5 |
| `smart_summary` | `false` | 长上下文任务无压缩导致 token 溢出 | 处理大文件或多步骤任务时启用 |
| `prompt` | 框架默认 | 自定义模板路径错误导致静默回退到默认值 | 验证模板路径存在；确保内容与 `workflow` 互补（不重复） |

### LLM 配置隔离规则

- **禁止**在 Agent YAML 或 `system.yaml` 中写 `model`、`llm`、`langfuse` 字段——它们会被自动过滤并输出 warning 日志
- Agent 仅通过 `model_type` 选择模型；所有 LLM 参数只能在 `config/llm.yaml` 中配置
- 未指定 `model_type` 时的回退链：`default_model_type` → `"common"` 类型 → 代码内置默认值

### 配置 overlay 白名单

Agent YAML 仅可覆盖以下 7 个系统级字段：`system`、`smart_summary`、`tool_access_control`、`execution_env`、`code_agent`、`tools`、`prompt`。其他字段在 overlay 时会被静默忽略。

### Workflow 书写质量

- 遵循**五段式结构**：① 背景与角色 → ② 核心职责与约束 → ③ 执行流程（Mermaid）→ ④ 各步骤详细说明 → ⑤ 输出要求
- Mermaid 块自动获得**框架强制执行**（"must be followed strictly"）——将核心逻辑放在 Mermaid 中可利用此机制
- 单个 `workflow` 使用 `|` YAML 多行文本块语法；顺序 `workflow` 使用非空 `list[str]`，每项建议用多行文本块
- 避免在 `workflow` 中硬编码路径——使用工具动态发现
