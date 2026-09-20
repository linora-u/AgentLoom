# README 模板

Application README 面向两个对象：用户和后续 Agent。它必须能回答“怎么运行、产出在哪、验证过什么、还有什么问题”。

## 推荐结构

````markdown
# <Application Name>

## 目标

一句话说明这个 Application 解决什么问题。

## 输入与输出

| 项 | 说明 |
|---|---|
| 输入 | ... |
| 输出 | ... |

## 组织结构

```text
applications/<app_name>/
...
```

## Agent 与 Tool 分工

| 组件 | 职责 |
|---|---|
| Supervisor | ... |
| Worker A | ... |
| Worker B | ... |
| Tool | ... |

## 运行

默认直接运行 Agent YAML：

```bash
.venv/bin/loom run applications/<app_name>/workflows/<agent>.yaml
```

如果应用提供了自定义 Python wrapper，再补充 wrapper 用法：

```bash
.venv/bin/python applications/<app_name>/<app_name>_app.py "<用户需求>"
```

## 验证记录

| 命令 | 结果 |
|---|---|
| `validate_application_yaml.py ...` | 通过/失败 |
| `scan_app_structure(...)` | 通过/失败 |
| `py_compile ...` | 通过/失败 |
| 运行验证 | 通过/失败/未执行，原因 |
| Run 证据 | `manifest.json`、`logs/runtime.log`、`audit/shell.jsonl` 的真实路径与关键结论 |

## 已知问题

- ...
````

## 写法要求

- 不写营销口号。
- 不把“未执行”写成“通过”。
- 如果运行失败，记录根因和下一步。
- 如果是多 Agent，必须写清每个 Worker 的输入输出。
- 运行验证必须记录 manifest 中的 `application_id`、`task_id`、`run_id`，并读同一 run 的 runtime log/audit；不要只记录退出码或 final answer。
- Application 用户交付物写自身 `output_dir`；`.agentloom/runs/.../artifacts` 只放运行证据；Agent 可见工作区位于 `.agentloom/workspaces/agents/<application_id>/<agent_path>/`，任务状态继续隔离在 `tasks/<task_id>/`。
