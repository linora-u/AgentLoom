# Workflow Review — 工具能力动态发现指南

本文件用于在审核时动态识别目标 Application 的工具能力，避免依赖固定工具表。

## 核心原则

- 不假设目标项目的系统工具集合与当前仓库一致
- 不依赖硬编码工具数量或固定名称
- 先发现能力，再下结论
- 所有结论基于“有效配置”，不是“多个文件并列罗列”

---

## Step 0：根目录前置条件

执行扫描前必须满足：

- 当前目录为 AgentLoom 根目录
- 判定条件：存在 `config/llm.yaml`

不满足时直接停止，先修正执行目录。

---

## Step 1：发现配置来源并计算有效值

按覆盖链收集配置文件（低优先级 -> 高优先级）：

1. 项目级：`<project_root>/config/system.yaml`
2. 应用级：`<app_path>/config/system.yaml`

计算规则：

- `dict` 深度合并
- `list` 整体替换（不是追加）

必须输出：

- 有效 `default_loaded_tools`（最终值）
- `default_loaded_tools` 的来源层级（最后一次定义位置）
- 若最终值为空列表，要明确说明是“上层显式覆盖为空”还是“未配置”

---

## Step 2：发现映射与执行环境语义

### 2.1 映射检查

1. 先检查 `tools_mapping.Claude`
2. 若为空，再检查 legacy `tools.mapping`
3. 若两者同时存在，以 `tools_mapping.Claude` 为准，legacy 仅记录为“被忽略”

### 2.2 执行环境检查

对 Supervisor/Worker 分别收集：

- `execution_env.type`

结合运行时规则判定默认工具可用性：

- `execution_env.type` 为 `docker` / `e2b`：默认工具列表整体跳过
- 其他类型：默认工具按有效 `default_loaded_tools` 加载

---

## Step 3：发现 Agent 实际可用能力

对 Supervisor/Worker 分别收集：

1. YAML/Markdown 中显式 `tools` 声明
2. `worker_agents` 路径形式与后缀（`.md` / `.yaml` / `.yml`）
3. `agent_tools/*.py` 公开函数（函数名 + docstring 摘要）

输出建议格式：

```markdown
## 工具能力矩阵
| Agent | 显式工具 | 默认工具(有效值) | 映射来源 | execution_env | 自定义工具能力 | 备注 |
|------|---------|------------------|---------|---------------|---------------|------|
```

---

## Step 4：能力对齐（需求 vs 能力）

从 prompt 中提取操作意图，再映射到能力类型：

- 文件读取/搜索
- 文件写入/编辑
- 结构化解析（AST/符号）
- Shell 执行/验证
- 格式化输出
- 外部系统调用

判断逻辑：

1. 目标能力已覆盖：标记“可直接使用”
2. 能力可由现有工具组合实现：标记“可编排实现”
3. 无覆盖：建议新增自定义 Tool，并说明输入/输出契约

---

## Step 5：何时建议新建 Tool

满足任一条件即可建议新增：

- prompt 中有高频确定性步骤，当前无稳定实现
- 多个 Worker 重复实现同类确定性逻辑
- 现有工具可用但入参/出参不适配当前场景，导致 Agent 侧拼装复杂

建议输出模板：

```markdown
[改进建议]
- 新增 Tool: <tool_name>
- 目标能力: <能力边界>
- 输入: <关键参数>
- 输出: <结构化返回>
- 影响范围: <哪些 Worker/Supervisor 使用>
```

---

## 审核时避免的误判

- 仅凭“当前仓库默认工具”判断别的项目“缺工具”
- 未确认 `execution_env` 就断言默认工具必定可用
- 将“可由组合实现”误判为“必须新建工具”
- 在缺少配置证据时给高置信度结论
- 忽略列表替换语义，误把底层 `default_loaded_tools` 当最终值
