# Workflow Architecture Review — 四维度检查清单（增强版）

本清单用于逐项审核。可只输出有发现的项，但必须完成完整检查。

---

## 前置门禁（必查）

- [ ] 是否已进入 AgentLoom 根目录（判定条件：存在 `config/llm.yaml`）
- [ ] 对相对路径审核时，根目录前置条件失败是否已立即中止

---

## 维度 1：Workflow 流程设计

### 1.1 阶段划分与复杂度

- [ ] 每个阶段是否单一职责
- [ ] 1 个阶段时是否可直接单 Agent（无需额外编排）
- [ ] 2-7 个阶段时分工是否清晰
- [ ] 8+ 阶段时是否评估过合并或分层编排

### 1.2 Workflow 指令质量（五段式结构）

- [ ] workflow 是否遵循推荐五段式结构？（① 背景与角色 → ② 核心职责与约束 → ③ 执行流程 Mermaid → ④ 各步骤详细说明 → ⑤ 输出要求）
- [ ] 每个步骤是否有明确的**成功标准**和**失败处理**？
- [ ] 关键规则是否用**加粗**强调？有序步骤是否使用编号列表？
- [ ] `workflow` 字段是否使用了 `|` YAML 多行文本块语法，或使用非空列表且每个字符串项都写成多行文本块？
- [ ] workflow 中是否避免了硬编码文件路径？（应通过工具动态获取）

### 1.3 流程图与执行指令一致性

- [ ] 流程图节点是否与已注册能力一致（Worker/Tool）
- [ ] 条件分支是否有明确触发条件和终止条件
- [ ] 文本步骤与流程图是否互相矛盾
- [ ] workflow 是否包含 Mermaid 代码块？（框架存在 Mermaid 块时自动注入 "must be followed strictly" 约束——验证是否利用了此强制执行机制）
- [ ] Mermaid 语法是否正确？（框架通过 `mermaid-syntax-parser` 校验，语法错误会输出 warning）

### 1.4 输出与完成标准

- [ ] 每阶段是否定义了可验证完成标准
- [ ] 最终输出格式是否可稳定复现
- [ ] 是否定义失败时的退出策略

### 1.5 努力预算与规划

- [ ] 每个 Agent 是否合理设置了 `max_steps`？（默认：80；过高浪费资源，过低导致任务提前终止）
- [ ] 长任务链 Agent 是否设置了 `planning_interval`？（每 N 步强制自省一次）
- [ ] 是否给出并发上限与超时策略
- [ ] 架构复杂度是否与任务价值匹配（避免过度设计）

---

## 维度 2：主从 Agent 协调

### 2.1 Supervisor 职责边界

- [ ] Supervisor 是否以调度/决策为主，避免执行细节工作
- [ ] 是否定义 Worker 调用顺序与条件
- [ ] 是否存在多余的中转调用

### 2.2 Worker 契约完整性

- [ ] Worker 是否具备清晰输入输出契约
- [ ] `inputs` 是否覆盖执行所需信息
- [ ] 输出是否可被下游直接消费

### 2.3 依赖显式化

- [ ] Worker 之间是否存在隐式依赖（文件/环境/全局变量）
- [ ] 隐式依赖是否已改为显式参数或明确数据流文档

### 2.4 协调开销与翻译损耗（新增）

- [ ] Supervisor 是否重复改写 Worker 结果导致信息丢失
- [ ] 是否存在“电话游戏”式传递导致语义偏移
- [ ] 是否允许必要场景下直通（减少无效转述）

### 2.5 delegation 去重（新增）

- [ ] 是否有多个 Worker 对同一输入重复分析
- [ ] 是否有职责重叠造成重复工具调用
- [ ] 是否有明确去重策略

---

## 维度 3：Agent/Tool 职责划分（核心）

### 3.1 Agent 级别

- [ ] Worker 主要任务是否确实需要 LLM 理解力
- [ ] 纯确定性任务是否应改为 Tool
- [ ] Tool 中是否误放了需要 LLM 判断的逻辑

### 3.2 Prompt 级别

- [ ] 是否存在可 Tool 化的确定性指令（遍历、排序、过滤、格式化、规则匹配）
- [ ] 是否存在跨 Worker 重复约束与模板
- [ ] 混合任务是否做了“Tool 前后处理 + Agent 推理”拆分

### 3.3 封装模式与交互模式选择

- [ ] 是否选择了正确的 Agent-Tool 路径？
  - **Path A**（`worker_agents` 自动注册）：简单单次 Agent 调用
  - **Path B**（普通 `module + function` 工具）：不涉及 Agent 的确定性逻辑
  - **Path C**（通过 `create_agent_as_tool()` 的 Python 封装）：批量处理、断点续传、错误隔离、进度持久化
- [ ] 是否可直接注入为 Worker（阶段独立、调用简单）
- [ ] 是否避免把流程控制交给 LLM 生成代码临时实现
- [ ] `tool_call_type` 是否合理？（`code_act` 推荐用于 Supervisor 和复杂 Worker；`tool_call` 适用于简单单工具 Worker）

### 3.4 模型与 LLM 配置

- [ ] 指定的 `model_type` 是否存在于 `config/llm.yaml` 中？（不存在的类型会在运行时抛出 `ValueError`——不会静默回退）
- [ ] `model_type` 是否与任务复杂度匹配？（`powerful` 用于复杂推理，`fast` 用于简单分类，`summary` 用于信息提取）
- [ ] Agent YAML 是否包含被禁止的 LLM 字段（`model`、`llm`、`langfuse`）？（被自动过滤并输出 warning——所有 LLM 参数只能在 `config/llm.yaml`）
- [ ] 未指定 `model_type` 时，`llm.yaml` 中是否配置了合适的 `default_model_type`？

### 3.5 工具能力发现

- [ ] 是否基于目标项目动态发现工具能力，而非硬编码工具表
- [ ] 是否按覆盖链计算“有效 `default_loaded_tools`”（而不是并列文件直接下结论）
- [ ] 是否核对了 `tools_mapping` 与 legacy `tools.mapping` 兼容关系
- [ ] `execution_env` 下默认工具可用性是否已核对
- [ ] 新建 Tool 建议是否有明确输入/输出契约

### 3.6 Skills 集成

- [ ] Skills 是否正确引用？（路径解析基于 `AGENT_ROOT`，而非 Agent YAML 文件目录）
- [ ] 是否理解三层加载机制？（全局 `config/system.yaml` → 自动发现 `AGENT_ROOT/skills/` → Agent 私有 `skills` 字段；同名 Skill 后层覆盖前层）
- [ ] `invocation-control.allow-model` 是否设置合理？（`"force-inject"` 用于关键能力如记忆；`true` 用于按需加载；`false` 用于静默 Hook-only）
- [ ] 需要工具名映射时 `platform` 是否正确设置？
- [ ] 是否缺少必要的 Skills？（如 `agent-recall-with-files` 用于跨会话记忆）

---

## 维度 4：错误处理与韧性

### 4.1 门禁与前置校验

- [ ] 是否有必要门禁（上下文、路径、配置）
- [ ] 门禁失败是否立即终止下游调用

### 4.2 错误隔离与重试

- [ ] 单 Worker 失败是否可隔离
- [ ] 是否区分可重试和不可重试错误
- [ ] 重试是否有上限和退避策略

### 4.3 进度恢复

- [ ] 批处理是否有 checkpoint/progress
- [ ] 中断后是否可从上次状态恢复
- [ ] 已完成项是否可跳过
- [ ] `checkpoint.enabled` 是否根据业务场景正确设置？（大多数场景默认 `true` 即可）
- [ ] `checkpoint.cleanup_on_success` 是否符合调试/生产需求？（生产用 `true`，调试用 `false` 保留产物）
- [ ] `checkpoint.max_resume_age` 是否与业务 SLA 匹配？（默认 7 天；短期任务可缩短）
- [ ] `checkpoint.heartbeat_interval` 是否合理？（默认 5s；仅当磁盘 I/O 成为瓶颈时才调大）
- [ ] 崩溃后是否能自动检测中断任务并恢复？

### 4.4 上下文与资源管理

- [ ] 长上下文任务是否启用了 `smart_summary`？（控制 LLM 摘要 vs 简单截断，避免 token 溢出）
- [ ] `prompt` 自定义覆盖（若有）是否与 `workflow` 内容冲突？引用的模板文件是否存在？
- [ ] `tool_access_control` 配置（include_paths、exclude_paths）是否适合 Agent 的文件访问需求？

### 4.5 可观测性

- [ ] 是否记录关键节点输入/输出摘要
- [ ] 错误日志是否可直接用于定位问题
- [ ] 是否能追踪建议变更的影响范围

---

## 横向要求：建议必须可验证（新增）

每条发现都要满足：

- [ ] 有 `[分级]`：`必须修复（阻断上线）` / `建议优化（非阻断）`
- [ ] 有 `[证据]`：字段、原文、代码片段之一
- [ ] 有 `[问题判断]`：说明影响（准确性/可维护性/成本/风险）
- [ ] 有 `[改进建议]`：可以执行、可验收
- [ ] 有 `[置信度]`：高/中/低
- [ ] 有 `[推断]`：是/否
