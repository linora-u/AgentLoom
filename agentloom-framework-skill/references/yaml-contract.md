# Agent YAML 契约

## 通用必填字段

每个 Agent YAML 必须有：

```yaml
name: "<agent_name>"
description: "<一两句话角色定位>"
workflow: |
  <完整执行协议>
```

`description` 只写角色定位；详细流程写进 `workflow`。

## Supervisor

```yaml
name: "<app_name>"
description: "<Supervisor 角色>"
model_type: "powerful"
tool_call_type: "code_act"
max_steps: 80
worker_agents:
  - path: "applications/<app_name>/workflows/worker_agents/<worker>.yaml"
workflow: |
  # <Workflow Name>
  ...
```

规则：

- `worker_agents` 只支持 `path`，不要写 `name`。
- `path` 从 AgentLoom 根目录解析，推荐写完整相对路径。
- `tool_call_type`：复杂编排用 `code_act`，简单固定调用可用 `tool_call`。

## Worker

```yaml
name: "<worker_name>"
description: "<Worker 职责>"
model_type: "powerful"
tool_call_type: "tool_call"
max_steps: 40
planning_interval: 3
agent_function_schema:
  description: "<作为工具被 Supervisor 调用时的说明>"
  inputs:
    user_request:
      description: "用户需求"
      required: true
  output:
    description: "Markdown 文本"
workflow: |
  # <Worker Workflow>
  ...
```

规则：

- Worker 被 Supervisor 调用时必须有 `agent_function_schema`。
- `inputs` 的 key 必须是合法 Python 标识符。
- 输出应是可被下游 Worker 或 Supervisor 直接使用的文本。

## 模型与配置

- Agent YAML 不写 `model`、`llm`、`langfuse`。
- `model_type` 必须存在于 `config/llm.yaml`。
- 不写 `model_type` 时依赖 `model.default_model_type`，但生成新 Application 时推荐显式写出实际可用类型。
- `execution_env.type` 为 `docker` / `e2b` 时默认工具不会自动加载。

## 三种 Agent-Tool 路径

| 路径 | 何时使用 | 配置方式 |
|---|---|---|
| Path A: `worker_agents` 自动注册 | Supervisor 调一次 Worker | `worker_agents: [{path: ...}]` |
| Path B: 普通动态工具 | 确定性 Python 能力 | `tools: [{name, module, function}]` |
| Path C: Python 包装 Agent | 批量、断点、错误隔离、前后置处理 | Tool 内调用 `YamlAgentFactory.create_agent_as_tool()` |

优先 Path A；需要 Python 控制流时再用 Path C。
