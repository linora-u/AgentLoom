# 逐 Skill 更新检查清单

> 按照本检查清单逐项检查每个 Skill 的文件，确保与最新的 `docs/cn/` 和 `src/` 保持一致。
> 仅列出有发现的项（需要更新）；无变更的项可以跳过。
>
> **范围说明（可扩展）**：
> - 默认适用于 `tools/skills-cn/` 下的所有目标 Skills（包括未来新增的 Skills）。
> - `tools/skills-cn/update-skills/` 仅作为规则来源，不是本检查清单的更新目标。
> - 对于新增的 Skills：先运行"通用检查框架"，然后追加该 Skill 特定的检查项。

---

## 通用检查框架（所有目标 Skills）

- [ ] **前提条件**：是否在 AgentLoom 根目录（包含 `config/llm.yaml`）下执行了检查
- [ ] **来源一致性**：阶段 1 的变更来源是否与 `references/doc-skill-mapping.md` 支持的来源一致（防止遗漏检测或更新）
- [ ] **文档引用有效性**：`docs/cn/` 路径、章节编号和术语是否仍然有效
- [ ] **代码依据有效性**：引用的源代码路径、字段和行为描述是否仍可验证
- [ ] **交叉引用有效性**：Skills 之间和 references 之间的链接是否仍然有效
- [ ] **默认测试范围**：是否已执行并通过 `./run_tests.sh tests/skills_test`（仅在用户明确要求时运行全量 `./run_tests.sh`）

---

## create-app

### SKILL.md

- [ ] **必填字段检查清单**：与 `docs/cn/agent_config.md` 第 2 章（YAML 模板）和第 3 章（字段参考手册）对齐
  - 必填字段数量是否正确（name, description, workflow -- 3 个必填）
  - 可选字段列表是否完整（tools, model_type, tool_call_type, worker_agents, execution_env, prompt, skills, planning_interval, max_steps）
- [ ] **信息提取检查清单（#1-#14）**：默认值和描述是否与最新文档一致
  - `model_type` 默认值描述：继承自 `config/llm.yaml` 的 `model.default_model_type`
  - `tool_call_type` 默认值：`code_act`
  - `execution_env` 可用值：`local`、`docker`、`e2b`、`wasm`
- [ ] **model_type 发现和确认工作流**：与 `docs/cn/llm_config.md` 对齐
  - 参数继承链：`models[model_type].param` -> `models.common.param` -> 代码默认值
  - 可用的 model_types 应从 `config/llm.yaml` 动态发现（排除 `default_model_type`），而非硬编码为固定集合
- [ ] **LLM 配置隔离描述**：与 `docs/cn/config-overview.md` 和 `docs/cn/agent_config.md` 对齐
  - Agent YAML 中不得包含 `model`/`llm`/`langfuse`；这些会被自动过滤并输出警告
- [ ] **两种模式描述**：Supervisor + N Workers 与 单 Agent 模式
- [ ] **路径策略**：项目根目录定位规则
- [ ] **智能推荐规则表**：工具推荐是否反映了最新的可用工具

### references/quick-reference.md

- [ ] **预定义工具完整表（第 1 节）**：与 `config/system.yaml` 的 `default_loaded_tools` 以及 `src/tools/` 中实际注册的工具对齐
  - 新增的工具是否已收录
  - 已删除/重命名的工具是否已移除/更新
- [ ] **model_type 选择规则（第 2 节）**：与 `docs/cn/llm_config.md` 对齐
  - 动态发现脚本是否仍然有效
- [ ] **execution_env.type 可用值（第 3 节）**：与 `docs/cn/agent_config.md` 第 3.5 节对齐
- [ ] **tool_call_type 对比（第 4 节）**：Agent 类型名称（CodeAgentV2, ToolCallingAgentV2）是否仍然正确
- [ ] **agent_function_schema.inputs 常用命名（第 5 节）**：与实际使用一致
- [ ] **覆盖允许列表字段（第 6 节）**：与 `src/lib/config/config.py` 中的实际覆盖允许列表对齐
  - 当前 7 个字段：system, smart_summary, tool_access_control, execution_env, code_agent, tools, prompt
- [ ] **关键约束检查清单（第 7 节）**：与最新文档和代码行为一致
  - 约束的数量或内容是否发生了变更
- [ ] **worker_agents.path 解析规则（第 8 节）**：三种写法的解析逻辑是否仍然正确

### references/templates.md

- [ ] **Supervisor YAML 模板（3.1）**：字段名、注释和默认值与 `docs/cn/agent_config.md` 第 2.1 节一致
  - Skills 配置语法（invocation-control 格式）
- [ ] **Worker YAML 模板（3.2）**：字段名和注释与 `docs/cn/agent_config.md` 第 2.2 节一致
- [ ] **入口脚本模板（3.3）**：导入路径（`src.runner`）是否仍然正确
- [ ] **自定义工具模板（3.4）**：无装饰器、docstring 提取的描述是否仍然正确
- [ ] **应用级 system.yaml 模板（3.5）**：可覆盖字段是否与覆盖允许列表一致

### references/troubleshooting.md

- [ ] **错误消息文本**：与代码中实际抛出的错误消息一致
- [ ] **排障步骤**：与最新的代码行为匹配
- [ ] **验证脚本**：Python 代码片段能在当前版本上正确运行

### references/full-example.md

- [ ] **端到端示例流程**：目录结构和字段与最新文档和代码行为一致
- [ ] **示例中的 model_type 策略**：反映了"动态发现 + 默认继承"机制
- [ ] **示例中的 skills/worker 配置**：与当前 `docs/cn/agent_config.md` 规则一致

### references/agent-yaml-schema.json

- [ ] **字段定义**：必填字段、可选字段和类型约束与当前 Agent YAML 规则一致
- [ ] **结构完整性**：Supervisor/Worker 相关结构未遗漏关键字段
- [ ] **约束一致性**：枚举值和默认值描述与文档和实现一致

### scripts/validate_application_yaml.py

- [ ] **验证规则**：与当前文档定义和实现逻辑一致
- [ ] **错误消息**：验证失败消息与实际规则一致，避免产生误导信息
- [ ] **脚本可运行性**：在 `.venv` 环境中可执行

---

## create-skill

### SKILL.md

- [ ] **信息提取检查清单（#1-#11）**：与 `docs/cn/skills_config.md` 对齐
  - Skill 类型：强制注入 / 按需 / 隐藏
  - `invocation-control.allow-model` 三个值：`"force-inject"` / `true` / `false`
- [ ] **Skill 类型判定指南表**：`allow-model` 值与 `docs/cn/skills_config.md` 一致
- [ ] **Hook 需求判定指南表**：Hook 事件数量和名称与最新规范一致
  - 当前 9 个事件：TaskStart, TaskComplete, TaskFail, SubtaskStart, SubtaskFinish, PreToolUse, PostToolUse, PostToolError, Stop
- [ ] **解决方案模板**：展示的 frontmatter 字段和配置示例
- [ ] **文档引用**：`docs/cn/skills_config.md` 的章节编号是否正确
- [ ] **3 层加载顺序描述**：全局 → skills/ 自动发现 → Agent 私有

### references/skill-template.md

- [ ] **SKILL.md 完整模板**：frontmatter 字段与 `src/lib/smolagents/skills/parser.py` 中实际支持的字段一致
  - 支持的 frontmatter 字段列表
  - Hook 事件名称拼写
- [ ] **YAML frontmatter 字段速查表**：字段名、类型、默认值
  - 注意：`platform` 和 `invocation-control` 不在 SKILL.md 中定义（在引用侧配置）
- [ ] **抽象工具名映射表**：Read/Write/Edit/Bash/Glob/Grep → 实际工具名
- [ ] **9 个 Hook 事件速查表**：事件名、matcher 要求、tool_name 值、典型用例
- [ ] **引用侧配置速查（invocation-control）**：语法格式
  - `allow-model`：`true` / `false` / `"force-inject"`
  - `allow-hook`：`true` / `false`
- [ ] **三种 Skill 类型配置示例**：YAML 示例代码

### references/hook-scripts-guide.md

- [ ] **5 个环境变量**：变量名和默认值与 `src/lib/smolagents/skills/skills.py` 一致
  - AGENT_NAME, TASK_ID, TOOL_NAME, HOOK_EVENT, HOOK_CONTEXT_JSON
- [ ] **HOOK_CONTEXT_JSON 结构**：字段列表与代码中实际传递的一致
- [ ] **输出 JSON 格式 -- 7 个字段**：decision, modified_input, modified_response, agent_context, user_message, reason, telemetry
- [ ] **decision 三个值**：allow / block / modify
- [ ] **退出码规则表**：6 种组合的行为
- [ ] **common.py 模板**：函数签名和逻辑与框架兼容

---

## workflow-review

### SKILL.md

- [ ] **上下文扫描阶段**：scan_tools.py 的调用方式、路径参数
- [ ] **能力发现工作流**：配置字段路径（default_loaded_tools, tools_mapping）与 `docs/cn/system_config.md` 一致
- [ ] **四维度审查内容**：审查重点是否反映了最新的框架能力

### references/review-checklist.md

- [ ] **维度 1 -- Workflow 设计**：检查清单项内容
- [ ] **维度 2 -- Supervisor-Worker 协作**：
  - Worker 契约完整性（agent_function_schema 字段）
  - 协作开销和转译损耗检查
  - 委派去重检查
- [ ] **维度 3 -- Agent/Tool 职责分离**：
  - 工具能力发现要求（动态 vs 硬编码）
  - 封装模式选择
- [ ] **维度 4 -- 错误处理与韧性**：
  - 可观测性要求
  - 进度恢复机制

### references/best-practices.md

- [ ] **8 个模式内容**：新的框架功能是否需要新增模式
- [ ] **反模式速查**：是否需要新增反模式
- [ ] **模式 7 — Checkpoint 配置字段**：`checkpoint.*` 字段（enabled、cleanup_on_success、max_resume_age、heartbeat_interval）及其默认值是否当前有效？
- [ ] **模式 7 — 崩溃检测阈值**：`HEARTBEAT_STALE_THRESHOLD`（30s）是否与代码一致？
- [ ] **模式 7 — 两级心跳**：Supervisor/Worker 心跳说明是否与当前实现一致？

### references/review-checklist.md

- [ ] **维度 4.3 — Checkpoint 审查项**：8 个 checkpoint 配置检查项（enabled、cleanup_on_success、max_resume_age、heartbeat_interval、崩溃恢复）是否当前有效？

### references/system-tools.md

- [ ] **步骤 1 -- 配置来源发现**：配置路径和字段名（`default_loaded_tools`, `tools_mapping`）与 `docs/cn/system_config.md` 一致
- [ ] **步骤 2 -- Agent 实际可用工具**：发现工作流描述
- [ ] **步骤 3 -- 能力对齐**：能力类型分类
- [ ] **步骤 4 -- 何时建议创建新工具**：决策标准
- [ ] **误报警告**：是否需要新增场景

---

## 跨领域检查项（所有 Skills 通用）

- [ ] Skills 中引用的所有 `docs/cn/` 文档路径是否仍然存在
- [ ] Skills 中引用的所有 `docs/cn/` 章节编号是否仍然正确
- [ ] Skills 中使用的所有框架术语是否与最新文档一致（如 Agent 角色名称、配置字段名）
- [ ] 阶段 1 变更检测来源是否与映射表支持的来源一致
- [ ] 更新结果是否排除了 `tools/skills-cn/update-skills/`
- [ ] references 中的所有 YAML 示例代码是否能通过 YAML 语法验证
- [ ] references 中的所有 Python 代码片段是否能在当前 `.venv` 环境中运行
- [ ] Skills 之间的交叉引用是否有效（如 create-app 引用 create-skill，workflow-review 引用 create-app）
