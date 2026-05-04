---
name: workflow-review
description: "Use when reviewing a user-provided AgentLoom-style application path for workflow architecture quality, especially agent/tool boundaries, orchestration contracts, and resilience risks that require evidence-based recommendations."
---

# Workflow Architecture Review

用于审核 AgentLoom 风格 Application 的 workflow 设计质量。
本 Skill 保留 AgentLoom 框架语义，但不依赖当前仓库里的具体样例、固定目录或固定工具清单。

> 配套文档（相对于 Skill 根目录）：
> - [references/review-checklist.md](./references/review-checklist.md)
> - [references/best-practices.md](./references/best-practices.md)
> - [references/system-tools.md](./references/system-tools.md)
> - [scripts/scan_tools.py](./scripts/scan_tools.py)

## 适用场景

- 用户提供 `application path`，需要判断 workflow 架构是否合理
- 需要评审 Supervisor/Worker 协调、Agent 与 Tool 边界、韧性设计
- 需要给出可执行的改进建议，而不是泛化建议

## 不适用场景

- 创建新 Application（请用 `create-app`）
- 非 Agent 架构评审（纯算法/纯样式优化）
- 不含可验证证据的“主观打分”

---

## 第一阶段：输入确认

必须先确认：

1. **Application 路径（必需）**
2. **根目录前置条件（必需）**：先进入 AgentLoom 根目录再执行检测/更新
3. 审核范围（默认四维度全量）
4. 业务背景（可选，影响优先级）

根目录识别标准：存在 `config/llm.yaml` 文件。
  - ⚠️ 不要使用 `config/system.yaml` 进行识别，因为应用级目录也可能包含此文件（例如 `applications/ai_quality_analysis/config/system.yaml`），无法唯一标识项目根目录。
  - `config/llm.yaml` 是全局唯一的，仅存在于 AgentLoom 根目录。

若根目录前置条件不满足，直接返回缺失项，不进入后续阶段。

如果是自主执行模式：从上下文提取路径；提取失败则直接返回缺失项，不进入后续阶段。

---

## 第二阶段：上下文扫描（能力发现优先）

先做“结构扫描 + 能力发现”，再做分析判断。

### 2.1 结构扫描

优先调用 `scripts/scan_tools.py`：

```bash
# 先进入 AgentLoom 根目录（必须满足 config/llm.yaml 存在）
cd /path/to/AgentLoom
pwd
ls config/llm.yaml

.venv/bin/python -c "
import sys
sys.path.insert(0, '/abs/path/to/workflow-review')
from scripts.scan_tools import scan_app_structure
print(scan_app_structure('applications/<app_name>'))
"
```

然后按需提取单个 YAML 的 `workflow` 原文：

```bash
.venv/bin/python -c "
import sys
sys.path.insert(0, '/abs/path/to/workflow-review')
from scripts.scan_tools import extract_workflow_text
print(extract_workflow_text('.../worker_agents/<step>.yaml'))
"
```

### 2.2 能力发现（动态，不写死）

不要假设固定系统工具列表，按目标项目实际配置发现：

1. 读取项目级/应用级 `config/system.yaml`，按**覆盖链**计算有效配置（避免并列文件直接下结论）
2. 提取有效 `default_loaded_tools`（含来源层级），并考虑列表替换语义
3. 提取 `tools_mapping`，若缺失则检查 legacy `tools.mapping` 兼容映射
4. 读取 Agent 配置中的 `tools`、`worker_agents`、`execution_env`、`model_type`
5. 结合 `execution_env` 判断默认工具可用性（`docker`/`e2b` 下默认工具整体跳过）
6. 读取 `agent_tools/*.py`，提取公开函数与 docstring 能力摘要
7. 对照 `workflow` 指令中的操作动词，判断能力缺口

---

## 第三阶段：四维度审核

每条发现必须可落地，格式固定为：

- `[分级]`：`必须修复（阻断上线）` / `建议优化（非阻断）`
- `[证据]`：引用具体字段、prompt 原文、或代码片段
- `[问题判断]`：说明风险与影响
- `[改进建议]`：配置改法 / 架构改法 / 新能力建议
- `[置信度]`：`高` / `中` / `低`
- `[推断]`：`是` / `否`

### 维度 1：Workflow 流程设计

关注：阶段职责、依赖显式化、分支清晰度、输出约束、可观测性入口。

额外关注：

- **Workflow 五段式结构**：workflow 是否遵循推荐结构（① 背景与角色 → ② 核心职责与约束 → ③ 执行流程 Mermaid → ④ 各步骤详细说明 → ⑤ 输出要求）。缺失段落会降低 LLM 执行质量。
- **`description` 角色边界**：`description` 应简短明了，说明 Agent 的核心定位，不应与 `workflow` 冗长重复。
- **单 Agent 过度设计**：如果只有一个 Worker 执行具体任务，不要生硬引入 Supervisor，造成单 Agent 过度设计。
- **Mermaid 流程图特殊语义**：框架会自动检测 workflow 中的 ` ```mermaid ` 代码块，提取并用 `<workflow>` XML 标签封装。当存在 Mermaid 块时，框架会**自动注入 "must be followed strictly"** 约束。需验证 Mermaid 流程图与文本指令是否一致，并利用了此强制执行机制。
- **`planning_interval` 配置**：对于长任务链 Agent（步骤多），检查是否设置了 `planning_interval` 以启用定期自省。未设置时，Agent 可能在复杂工作流中迷失进度。
- **`max_steps` 预算**：检查每个 Agent 是否合理设置了 `max_steps`（默认：80）。过高浪费资源，过低导致任务提前终止。

### 维度 2：主从 Agent 协调

关注：

- `agent_function_schema` 契约清晰度
- Worker 依赖是否显式传递
- 协调开销与“翻译损耗”（Supervisor 反复转述造成信息丢失）
- delegation 去重（避免多个 Worker 做重复分析）

### 维度 3：Agent/Tool 职责划分（核心）

关注：

- 纯确定性步骤是否被 LLM 承担（应 Tool 化）
- Prompt 中重复模板、重复约束是否可抽取
- Tool 封装模式是否合理——需对照**三种 Agent-Tool 路径**验证：
  - **Path A**：`worker_agents` 自动注册（简单单次调用场景）
  - **Path B**：通过 `module + function` 的普通动态工具（不含 Agent）
  - **Path C**：通过 `YamlAgentFactory.create_agent_as_tool()` 的 Python 封装 Agent 工具（适用于批量处理、断点续传、错误隔离、进度持久化）
- `model_type` 与任务复杂度是否匹配，**且指定的 `model_type` 是否实际存在于 `llm.yaml` 中**（不存在的类型会在运行时抛出 `ValueError`——不会静默回退）
- `tool_call_type` 选择是否合理：`code_act` 推荐用于 Supervisor 和复杂 Worker（支持循环、条件、多步编排）；`tool_call` 适用于简单单工具 Worker
- Agent YAML 中是否包含**被禁止的 LLM 字段**（`model`、`llm`、`langfuse`）——这些字段会被自动过滤并输出警告，LLM 参数只能在 `config/llm.yaml` 中配置
- **`code_agent` 安全性（建议优化）**：建议不要在 `additional_authorized_imports` 或 `additional_functions` 中使用 `"*"`，建议使用具体的白名单以防范代码执行风险。
- **`execution_env` 环境匹配（建议优化）**：存在代码生成的复杂任务，建议配置 `execution_env` 为 `docker` 或 `e2b` 以进行安全沙箱隔离，不建议使用默认的 `local` 环境。

**Skills 集成**（当应用使用 Skills 时检查）：

- Skills 是否正确引用（路径解析基于 `AGENT_ROOT`，而非 Agent YAML 文件目录）
- `invocation-control` 是否合理：`force-inject` 用于关键能力（如记忆系统），`true` 用于按需加载，`false` 用于静默 Hook-only Skills
- 是否理解三层加载机制：全局 Skills（`config/system.yaml`）→ 自动发现（`AGENT_ROOT/skills/`）→ Agent 私有（Agent YAML `skills` 字段）。后层同名 Skill 覆盖前层并输出警告。
- 需要工具名映射时 `platform` 是否正确设置
- **Hook 物理验证**：检查声明的 Hook 是否有对应的物理脚本存在（例如声明了 `TaskCreated`，必须存在 `scripts/on_task_start.py` 物理文件）。

### 维度 4：错误处理与韧性

关注：

- 门禁、错误隔离、重试与上限
- 断点续传（checkpoint/progress）
- 并行度/步骤预算是否可控（token 与时延）——具体检查每个 Agent 的 `max_steps`（默认 80）和 `planning_interval` 设置
- 失败路径是否可恢复、可定位
- 长上下文任务是否启用了 `smart_summary`（上下文压缩）——控制框架使用 LLM 摘要还是简单截断，避免 token 溢出
- `prompt` 自定义覆盖（若有）是否与 `workflow` 内容冲突，或引用了不存在的模板文件

---

## 第四阶段：输出报告

仅输出有发现的维度；无发现维度一行带过。

```markdown
# Workflow 架构审核报告：<app_name>

## 审核概要
- Application: <app_name>
- 模式: <单 Agent / Supervisor + N Worker>
- Worker 数量: <N>
- 自定义 Tool 数量: <M>
- 必须修复（阻断上线）: <count>
- 建议优化（非阻断）: <count>

## 发现 <编号>: <标题>
[分级]
- 必须修复（阻断上线）/建议优化（非阻断）

[证据]
- ...

[问题判断]
- ...

[改进建议]
- ...

[置信度]
- 高/中/低

[推断]
- 是/否
```

### 改进建议分层规则

- 配置类：直接给字段级修改建议
- 架构类：给步骤化改造方案（不要求完整代码）
- 新能力类：说明需要的能力边界与输入输出，不预设仓库实现

---

## AgentLoom 约束备忘（通用）

**必填字段与结构：**
- Agent YAML 必填：`name`、`description`、`workflow`
- Worker 建议完整定义 `agent_function_schema`
- `worker_agents` 使用 `path`（不是 `name`），支持三种形式：绝对路径 / 相对路径 / 简写名（无分隔符）
- `worker_agents` 支持后缀：`.md` / `.yaml` / `.yml`
- `workflow` 是非空字符串（使用 `|` YAML 多行文本块语法）或按顺序执行的非空 `list[str]`；支持 Markdown 和 Mermaid

**LLM 配置隔离：**
- Agent YAML 或 `system.yaml` 中的 `model`、`llm`、`langfuse` 字段会被**自动过滤**并输出 warning 日志——所有 LLM 参数只能在 `config/llm.yaml` 中配置
- Agent 通过 `model_type` 选择模型；若指定的类型在 `llm.yaml` 中不存在，运行时直接抛出 `ValueError`（不会静默回退）
- 未指定 `model_type` 时的回退链：使用 `llm.yaml` 的 `default_model_type`（默认为 `"common"`）

**配置覆盖：**
- Agent YAML overlay 白名单（仅 7 个字段）：`system`、`smart_summary`、`tool_access_control`、`execution_env`、`code_agent`、`tools`、`prompt`
- `default_loaded_tools` 在覆盖链中受列表替换语义影响，必须基于有效配置判定
- `default_loaded_tools` 在不同执行环境的加载行为不同（`docker`/`e2b`/`wasm` 整体跳过默认工具）

**Skills 系统：**
- 三层加载：全局 Skills（`config/system.yaml`）→ 自动发现（`AGENT_ROOT/skills/`）→ Agent 私有（Agent YAML `skills`）
- 三种调用模式：`allow-model: "force-inject"`（注入 system prompt）/ `true`（按需 `load_skill()`）/ `false`（隐藏，仅 Hook）
- 9 个 Hook 事件：TaskCreated、TaskCompleted、StopFailure、SubagentStart、SubagentStop、PreToolUse、PostToolUse、PostToolUseFailure、Stop

**关键可选字段：**
- `tool_call_type`：`"code_act"`（默认，推荐 Supervisor 使用）或 `"tool_call"`（简单 Worker 使用）
- `planning_interval`：正整数，每 N 步强制规划一次（适用于长任务链）
- `max_steps`：整数，默认 80，超过后 Agent 强制终止
- `smart_summary`：布尔值，控制上下文压缩策略（LLM 摘要 vs 截断）

## Prompt 质量基线（对齐行业最佳实践）

- 指令要具体：明确角色、目标、约束、输入边界与输出格式
- 结构要清晰：用分段/标签区分“规则、上下文、样例、任务”
- 输出要可检验：固定字段，明确证据与推断分界
- 评估要闭环：改造前后使用同一小样本场景对比
