# 文件生成模板

> 本文档包含第三阶段所有文件的完整生成模板。由 `SKILL.md` 引用。
>
> **路径约定**：所有 `<app_name>` 占位符应替换为实际的 Application 名称（小写+下划线）；所有相对路径均从项目根目录解析。

---

## 3.1 Supervisor YAML

**文件路径**：`applications/<app_name>/workflows/<app_name>_agent.yaml`

```yaml
name: "<app_name>_agent"
description: |
  <用户提供的一句话描述，扩展为 2-3 句完整的角色定位>

# model_type 选项：
# - 省略：继承 config/llm.yaml 中 model.default_model_type 的值
# - 指定：使用明确的类型（必须存在于 config/llm.yaml 的 model 节点中）
# model_type: "<selected_model_type>"
tool_call_type: "<tool_call_type>"

workflow: |
  # <Application 名称> 工作流

  ## 背景
  你是一名**<专业角色>**。当前任务是 <任务目标>。

  ## 执行流程
  （注意：在此放置 Mermaid flowchart TD 图，节点包含各 Worker 调用步骤）

  ## 执行原则
  1. 严格按照流程图中的顺序调用各 Worker
  2. 每个 Worker 的输出作为下一个 Worker 的输入上下文
  3. 最后汇总所有 Worker 的结果，输出完整报告

tools:
  # 预定义 Tool
  - name: "read_file"
  - name: "browse_directory"
  # 自定义 Tool（如有）
  # - name: "<tool_name>"
  #   module: "applications.<app_name>.agent_tools.<module>"
  #   function: "<function_name>"

# Worker agents —— 必须放在 workflows/worker_agents/ 目录下
# 简写：直接写文件名（如 "<worker_name_a>.yaml"），适用于同 app 下的 Worker
# 完整路径："applications/<app_name>/workflows/worker_agents/<worker_name_a>.yaml"，适用于跨 app 引用
# 注意：Worker YAML 的白名单覆盖项是独立解析的。
# 如果 Worker 需要额外文件访问权限，必须在对应 Worker YAML 中重复声明 tool_access_control 规则。
worker_agents:
  - path: "<worker_name_a>.yaml"
  - path: "<worker_name_b>.yaml"
  # ...

# 私有 Skill（可选，建议使用列表格式）
# skills:
#   - path: "skills/agent-recall-with-files"
#     platform: "Claude"
#     invocation-control:
#       allow-model: "force-inject"
#       allow-hook: true

execution_env:
  type: "<execution_env_type>"
```

顺序工作流写法：

```yaml
workflow:
  - |
    # 第一段工作流
    <第一次 runtime run 的指令>
  - |
    # 第二段工作流
    <第二次 runtime run 的指令；会保留第一次运行的记忆>
```

---

## 3.2 Worker YAML（每个阶段一个文件）

**文件路径**：`applications/<app_name>/workflows/worker_agents/step<N>_<name>.yaml`

```yaml
name: "step<N>_<name>"
description: "<阶段描述>"
tool_call_type: "<tool_call_type>"
# model_type 可选，策略与 Supervisor 相同
# model_type: "<selected_model_type>"
tools:
  - name: "<tool1>"
  # ...

# 如果此 Worker 需要访问 workspace 之外的目录，请在这里声明。
# Worker YAML 不会继承 Supervisor 的外部路径白名单。
# tool_access_control:
#   path_validation:
#     - tools: ["read_file", "edit_file", "write_markdown_file", "grep_search", "glob_search", "shell_tool"]
#       include_paths:
#         - "/absolute/path/outside/workspace"

planning_interval: 3    # 如启用
max_steps: 40

# 私有 Skill（可选）
# skills:
#   - "skills/agent-recall-with-files"

workflow: |
  # 步骤 <N>：<阶段名称>

  ## 背景
  你是一名**<专业角色>**，负责 <具体职责>。

  ## 核心职责
  1. **<职责 1>**：<描述>
  2. **<职责 2>**：<描述>

  ## 约束
  - ❌ <禁止行为>
  - ✅ <推荐做法>

  ## 输出要求
  - 格式：<格式要求>
  - 必须包含：<必要内容>

agent_function_schema:
  description: |
    <此 Worker 作为 Tool 被调用时的描述>
  inputs:
    query:
      description: "<主要输入参数描述>"
      required: true
  output:
    description: "<输出描述>"
```

---

## 3.3 入口脚本

**文件路径**：`applications/<app_name>/<app_name>_app.py`

```python
#!/usr/bin/env python3
"""
<app_name> – Application 入口脚本。

运行方式（从项目根目录执行）：
    <python> applications/<app_name>/<app_name>_app.py

或使用 runner：
    <python> src/runner.py applications/<app_name>/workflows/<app_name>_agent.yaml

或使用 AgentLoom CLI（如已安装）：
    loom run applications/<app_name>/workflows/<app_name>_agent.yaml
"""

import os
import sys

# 确保项目根目录在 sys.path 中
project_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if project_root not in sys.path:
    sys.path.insert(0, project_root)

from src.runner import run_app


def main():
    run_app("applications/<app_name>/workflows/<app_name>_agent.yaml")


if __name__ == "__main__":
    main()
```

---

## 3.4 自定义 Tool（如有）

**文件路径**：`applications/<app_name>/agent_tools/<module_name>.py`

```python
"""
<Tool 模块描述>

注意：动态加载的 Tool 是纯 Python 函数——无需装饰器。
框架通过 YAML 中的 module + function 配置动态导入。
Tool 描述自动从函数的 docstring 中提取；YAML 中的 description 字段会被忽略。
"""


def <function_name>(<params>) -> str:
    """
    <Tool 功能的详细描述——此 docstring 被框架提取为 Tool 描述。>

    Args:
        <param>: <参数描述>

    Returns:
        <返回值描述>
    """
    # TODO: 实现 Tool 逻辑
    pass
```

---

## 3.5 App 级 config/system.yaml（如有）

**文件路径**：`applications/<app_name>/config/system.yaml`

```yaml
# App 级系统配置覆盖
# 此文件深度合并覆盖全局 config/system.yaml

tool_access_control:
  # include_paths: []
  # exclude_paths: []
  # tool_access_control:
  #   - tools: ["read_file", "edit_file"]
  #     exclude_paths: ["build"]

# tools:
#   default:
#     - "read_file"
#     - "shell_tool"
#     - ...
```

---

## 3.6 自定义 sysprompt（如有）

**文件路径**：`applications/<app_name>/sysprompt/code_agent.yaml`

> 仅在用户明确要求时生成。大多数情况下，框架默认 Prompt 即可满足需求。

```yaml
# 自定义系统提示词模板
# 框架会加载此文件内容作为 Agent 的系统提示词，
# 替代默认的内置提示词。
system_prompt: |
  你是一个专业的 <角色描述>。

  ## 核心职责
  <描述 Agent 的主要职责>

  ## 行为约束
  - <约束 1>
  - <约束 2>

  ## 输出格式
  <描述预期输出格式>
```

> **注意**：Agent YAML 中的 `prompt` 字段必须指向此文件才会生效：
> ```yaml
> prompt:
>   path: "applications/<app_name>/sysprompt/code_agent.yaml"
> ```

---

## 3.7 单 Agent 模式 YAML（当用户选择单 Agent 时替代 3.1 和 3.2）

**文件路径**：`applications/<app_name>/workflows/<app_name>_agent.yaml`

```yaml
name: "<app_name>"
description: "<一句话描述>"
# model_type 选项：
# - 省略：继承 config/llm.yaml 中 model.default_model_type 的值
# - 指定：使用明确的类型（必须存在于 config/llm.yaml 的 model 节点中）
# model_type: "<selected_model_type>"
tool_call_type: "<tool_call_type>"
max_steps: <N>

tools:
  - name: "<tool1>"
  - name: "<tool2>"
  # ...

# 私有 Skill（可选，支持以下三种格式）
# skills: "skills/agent-recall-with-files"
# skills:
#   path: "skills/agent-recall-with-files"
#   platform: "Claude"
# skills:
#   - "skills/agent-recall-with-files"
#   - path: "applications/<app_name>/skills/domain-skill"
#     invocation-control:
#       allow-model: true
#       allow-hook: true

workflow: |
  # <Application 名称>

  ## 背景
  你是一名**<专业角色>**，负责 <具体职责>。

  ## 任务
  1. <步骤 1>
  2. <步骤 2>
  3. <步骤 3>

  ## 约束
  - ❌ <禁止行为>
  - ✅ <推荐做法>

  ## 输出要求
  - 格式：<格式要求>
  - 必须包含：<必要内容>

# 单 Agent 模式说明：
#   - 不需要 worker_agents 字段（无子 Agent）
#   - 不需要 agent_function_schema 字段（不会被其他 Agent 调用）
#   - 直接通过 src/runner.py 运行
```

> **完整单 Agent 示例**：参见 `references/full-example.md` 末尾的 `simple_scanner` 示例。

---

## 3.8 skills 字段参考（适用于所有 Agent YAML）

```yaml
# 格式 1：字符串（最简单）
skills: "skills/agent-recall-with-files"

# 格式 2：字典（单个 Skill）
skills:
  path: "skills/agent-recall-with-files"
  platform: "Claude"
  invocation-control:
    allow-model: "force-inject"
    allow-hook: true

# 格式 3：列表（推荐）
skills:
  - "skills/agent-recall-with-files"
  - path: "skills/agent-visualization"
    invocation-control:
      allow-model: false
      allow-hook: true
```
