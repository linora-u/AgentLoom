# 文档/代码 → Skill 影响映射表

> 本文件定义了 `docs/cn/` 文档和 `src/` 代码的变更如何影响 `tools/skills-cn/` 下每个 Skill 的具体文件。
> 当检测到变更时，使用本映射表定位需要更新的 Skill 文件。
>
> **范围说明（可扩展）**：
> - 默认覆盖 `tools/skills-cn/` 下的所有 Skills（包括未来新增的 Skills）。
> - `tools/skills-cn/update-skills/` 仅作为规则来源，不是更新目标。
> - 新增 Skill 后，请将该 Skill 的文档/代码映射条目追加到本表中。

---

## 1. 文档 → Skill 映射

### docs/cn/agent_config.md（Agent YAML 配置完整参考）

| 受影响的 Skill | 受影响的文件 | 需同步的内容 |
|---------------|-------------|-------------|
| `create-app` | `SKILL.md` | 信息提取检查清单（必填/可选字段）、YAML 模板示例、字段默认值、Supervisor/Worker 角色定义 |
| `create-app` | `references/templates.md` | Supervisor/Worker YAML 模板中的字段名、默认值和注释说明 |
| `create-app` | `references/quick-reference.md` | checkpoint 配置字段（第 6 节）、覆盖允许列表字段（第 7 节）、关键约束检查清单（第 8 节）、worker_agents 解析规则（第 9 节）、agent_function_schema.inputs 命名（第 5 节） |
| `create-app` | `references/troubleshooting.md` | Worker 加载失败排障、agent_function_schema 相关错误 |
| `create-app` | `references/full-example.md` | 端到端示例中的字段、结构和术语必须与最新 Agent 配置保持一致 |
| `create-app` | `references/agent-yaml-schema.json` | Agent YAML 字段定义、必填字段和类型约束同步 |
| `create-app` | `scripts/validate_application_yaml.py` | YAML 验证逻辑必须与最新的字段规则保持一致 |
| `workflow-review` | `SKILL.md` | 审查维度中的 agent_function_schema 契约检查 |
| `workflow-review` | `references/review-checklist.md` | Worker 契约完整性检查项（维度 2.2） |

### docs/cn/skills_config.md（Skills 配置完整参考）

| 受影响的 Skill | 受影响的文件 | 需同步的内容 |
|---------------|-------------|-------------|
| `create-skill` | `SKILL.md` | 信息提取检查清单、Skill 类型判定指南、Hook 需求判定指南、解决方案模板 |
| `create-skill` | `references/skill-template.md` | SKILL.md frontmatter 字段速查表、Hook 事件速查（9 个事件）、invocation-control 配置语法、抽象工具名映射 |
| `create-skill` | `references/hook-scripts-guide.md` | 环境变量列表（5 个变量）、输出 JSON 格式（7 个字段）、决策值、退出码规则、common.py 模板 |
| `create-app` | `SKILL.md` | 私有 skills 配置字段（阶段 1 检查清单 #14） |

### docs/cn/system_config.md（系统配置完整参考）

| 受影响的 Skill | 受影响的文件 | 需同步的内容 |
|---------------|-------------|-------------|
| `create-app` | `references/quick-reference.md` | 预定义工具列表（tools.default）、execution_env 选项、code_agent 配置；**第 6 节 — checkpoint 配置字段**（enabled、cleanup_on_success、max_resume_age、heartbeat_interval） |
| `create-app` | `SKILL.md` | 应用级 config/system.yaml 覆盖说明（信息提取检查清单 #12） |
| `create-app` | `references/full-example.md` | 示例中的系统配置覆盖字段和注释说明 |
| `create-app` | `references/agent-yaml-schema.json` | Schema 中与 system_config 相关的字段约束 |
| `workflow-review` | `references/system-tools.md` | 配置来源发现工作流（default_loaded_tools, tools_mapping） |
| `workflow-review` | `SKILL.md` | 上下文扫描阶段的配置读取路径 |
| `workflow-review` | `references/best-practices.md` | **模式 7 — AgentLoom checkpoint 配置字段和崩溃检测逻辑** |
| `workflow-review` | `references/review-checklist.md` | **维度 4.3 — checkpoint 配置审查项** |

### docs/cn/llm_config.md（LLM 配置完整参考）

| 受影响的 Skill | 受影响的文件 | 需同步的内容 |
|---------------|-------------|-------------|
| `create-app` | `SKILL.md` | model_type 发现和确认工作流、参数继承链、重试机制描述 |
| `create-app` | `references/quick-reference.md` | model_type 选择规则（第 2 节）、动态发现脚本 |
| `create-app` | `references/troubleshooting.md` | model_type 不存在时的排障步骤 |
| `create-app` | `references/full-example.md` | 示例中的 model_type 选择策略和说明 |

### docs/cn/config-overview.md（配置系统概述）

| 受影响的 Skill | 受影响的文件 | 需同步的内容 |
|---------------|-------------|-------------|
| `create-app` | `SKILL.md` | 4 层配置加载顺序、LLM 配置隔离原则 |
| `create-app` | `references/quick-reference.md` | 覆盖允许列表字段、配置覆盖规则 |
| `create-skill` | `SKILL.md` | Skills 3 层加载顺序（全局 → 自动发现 → Agent 私有） |

### docs/cn/README.md（项目概述）

| 受影响的 Skill | 受影响的文件 | 需同步的内容 |
|---------------|-------------|-------------|
| 所有 Skills | 各自的 SKILL.md | 框架描述、核心概念术语（如"每个 Agent 即是一个工具"）、功能列表 |

---

## 2. 代码 → Skill 映射

### src/lib/checkpoint/ + src/lib/heartbeat/（断点续跑与心跳系统）

| 受影响的 Skill | 受影响的文件 | 需同步的内容 |
|---------------|-------------|-------------|
| `workflow-review` | `references/best-practices.md` | 模式 7 — checkpoint 配置字段、两级心跳、崩溃检测阈值、恢复流程 |
| `workflow-review` | `references/review-checklist.md` | 维度 4.3 — checkpoint 配置审查项 |
| `create-app` | `references/quick-reference.md` | 第 6 节 — 系统级 checkpoint 配置字段表 |

### src/lib/config/（配置加载逻辑）

| 受影响的 Skill | 受影响的文件 | 需同步的内容 |
|---------------|-------------|-------------|
| `create-app` | `references/quick-reference.md` | 覆盖允许列表字段（代码中定义的实际允许列表可能变更） |
| `create-app` | `references/troubleshooting.md` | 配置加载失败的错误消息文本 |
| `create-app` | `SKILL.md` | LLM 隔离行为（从 Agent YAML 中过滤 model/llm/langfuse 的逻辑） |
| `create-app` | `scripts/validate_application_yaml.py` | 配置验证规则和错误消息与最新实现对齐 |

### src/lib/smolagents/skills/（Skills 解析和加载）

| 受影响的 Skill | 受影响的文件 | 需同步的内容 |
|---------------|-------------|-------------|
| `create-skill` | `references/skill-template.md` | SKILL.md frontmatter 支持的字段（与 parser.py 对齐） |
| `create-skill` | `references/hook-scripts-guide.md` | Hook 执行工作流、环境变量传递逻辑 |
| `create-skill` | `SKILL.md` | Skill 注册和加载机制描述 |

### src/lib/smolagents/agent/（Agent 工厂和基类）

| 受影响的 Skill | 受影响的文件 | 需同步的内容 |
|---------------|-------------|-------------|
| `create-app` | `SKILL.md` | Agent 创建工作流、YAML 字段解析逻辑 |
| `create-app` | `references/templates.md` | YAML 模板中字段的有效性 |
| `create-app` | `references/full-example.md` | 示例目录结构与 Agent 解析行为对齐 |
| `create-app` | `references/agent-yaml-schema.json` | Schema 与 Agent 解析行为的一致性 |

### src/tools/（工具系统）

| 受影响的 Skill | 受影响的文件 | 需同步的内容 |
|---------------|-------------|-------------|
| `create-app` | `references/quick-reference.md` | 预定义工具的实际函数名和功能描述 |
| `workflow-review` | `references/system-tools.md` | 工具发现策略、默认工具加载机制 |
| `workflow-review` | `SKILL.md` | scan_tools.py 的调用方式 |

### src/lib/smolagents/hooks/（Hook 类型定义和管理器）

| 受影响的 Skill | 受影响的文件 | 需同步的内容 |
|---------------|-------------|-------------|
| `create-skill` | `references/hook-scripts-guide.md` | HookResult 字段（decision/modified_input/modified_response/agent_context/user_message/reason/telemetry）、HOOK_CONTEXT_JSON 中传递的 HookContext 字段、decision 允许值 |
| `create-skill` | `references/skill-template.md` | HookEvent 枚举中的 Hook 事件名、matcher 行为 |
| `create-skill` | `SKILL.md` | Hook 执行工作流、HookResult 处理逻辑 |

### src/lib/smolagents/models/（模型类型系统）

| 受影响的 Skill | 受影响的文件 | 需同步的内容 |
|---------------|-------------|-------------|
| `create-app` | `references/quick-reference.md` | model_type 动态发现脚本 |
| `create-app` | `SKILL.md` | model_type 发现和确认工作流 |
| `create-app` | `references/full-example.md` | 示例中的 model_type 演示与实际可用类型机制对齐 |

---

## 3. 快速查找指南

当你知道哪个文件发生了变更时，使用此速查表定位受影响的 Skills：

| 变更文件 | 受影响的 Skills（按优先级排序） |
|---------|-------------------------------|
| `docs/cn/agent_config.md` | create-app（高）> workflow-review（中） |
| `docs/cn/skills_config.md` | create-skill（高）> create-app（低） |
| `docs/cn/system_config.md` | create-app（高）> workflow-review（中） |
| `docs/cn/llm_config.md` | create-app（高） |
| `docs/cn/config-overview.md` | create-app（中）> create-skill（低） |
| `docs/cn/README.md` | 所有（低） |
| `src/lib/config/` | create-app（高） |
| `src/lib/smolagents/skills/` | create-skill（高） |
| `src/lib/smolagents/hooks/` | create-skill（高） |
| `src/lib/smolagents/agent/` | create-app（中） |
| `src/tools/` | create-app（中）> workflow-review（中） |
| `src/lib/smolagents/models/` | create-app（中） |
| `config/system.yaml` | create-app（中）> workflow-review（低） |
| `config/llm.yaml` | create-app（中） |
| `docs/cn/system_config.md`（checkpoint 章节） | create-app（中）> workflow-review（中） |
| `src/lib/checkpoint/` | workflow-review（高） |
| `src/lib/heartbeat/` | workflow-review（高） |
