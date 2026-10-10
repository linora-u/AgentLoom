# 配置路径与迁移

路径跟随声明配置的归属：仓库 `config/system.yaml` 相对 AgentLoom 根；`applications/<app>/config/system.yaml` 和 Agent YAML 相对 Application 根。Application 根是最近的 `workflows/` 的父目录。Worker YAML 放在更深层目录也使用同一个根。

| 配置字段 | Application 中的写法 |
| --- | --- |
| `system_prompt.path` | `config/prompts/role.md` |
| `worker_agents[].path` | `workflows/worker_agents/worker.yaml` |
| `mcp_servers.path` / `paths` | `config/.mcp.anysearch.json` |
| `runtime_options.prompt_template_path` | `config/prompts/template.yaml` |
| `skills.paths` | `skills/custom` |
| `hooks.bundles.<name>.path` | `hooks/audit` |
| `tool_access_control.path_validation` 的 include/exclude | `data/*` |
| `shell_settings.dangerous_paths` | `data/private` |
| `shell_settings.sandbox.allow_write` / `deny_write` | `data` / `data/private` |

继承全局路径时，保留 AgentLoom 根。应用只覆盖 MCP 的 timeout，不会移动继承的 MCP 文件；显式覆盖 path 才使用应用根。通配符和 `*` 哨兵保留。路径解析不扩大各能力已有的访问范围，命令字符串、CLI 入参、模型工具参数和执行工作目录继续遵守原规则。`runtime.root_dir` 是全局配置，始终相对 AgentLoom 根。

这是直接切换，不按文件是否存在回退。旧配置迁移：

- 提示词 `../config/prompts/role.md` 或 `../../config/prompts/role.md` → `config/prompts/role.md`。
- Worker 简写 `worker.yaml` → `workflows/worker_agents/worker.yaml`。
- 应用中 `applications/<app>/config/mcp.json` → `config/mcp.json`。
- 跨应用 Worker → `../other_app/workflows/worker_agents/worker.yaml`。
- 应用中原来指向仓库根的权限路径 `secrets` → `../../secrets`；根据应用嵌套深度调整 `../` 数量。

绝对路径及 `~` 按各能力已有规则处理；相对路径可使用 `../`。迁移后用 `.venv/bin/python agentloom-framework-skill/scripts/validate_application_yaml.py --app-root applications/<app>` 检查定义，CLI 参数仍相对命令所在目录。
