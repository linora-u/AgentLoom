# 04 工具归属与注册清单

下列工具的实现归自研 smol Agent；builtin manifest 的 `owner=runtime`、`provider=smolagents`，公开名称与旧 YAML 一致。Pi 使用自己的官方基础实现，经 05/06 的公共治理和 Pi adapter 接线，不加载本表实现。

入口以 `agentloom.adapters.smolagents.` 为前缀。默认列描述 smol 旧配置的装配效果；`toolsets: []` 可关闭默认基础工具集合，显式工具仍按配置装配。

| 原名称 / manifest 名 | 新入口 | 装配条件 | 公共保护及行为证据 |
| --- | --- | --- | --- |
| `read_file` | `tools.file_ops.read_file:read_file` | 默认 `core_file` 或显式选择 | Gateway/Hook/路径权限；保留分页、二进制与设备检查；read_file、安全与实例隔离测试 |
| `write_file` | `tools.file_ops.write_file:write_file` | 默认 `core_file` 或显式选择 | Gateway/Hook/写前保护/历史；未读与外部改写检测；write_file 与文件历史测试、真实 core Application |
| `edit_file` | `tools.file_ops.edit_file:edit_file` | 默认 `core_file` 或显式选择 | 同上，且保留唯一匹配/差异行为；edit_file 测试、真实 core Application |
| `list_directory` | `tools.file_ops.directory_browser:list_directory` | 默认 `core_file` 或显式选择 | 路径与排除规则；目录行为测试、真实 core Application |
| `grep_search` | `tools.search.grep_tool:grep_search` | 默认 `core_search` 或显式选择 | 路径/查询排除；grep 和 exclude E2E、真实 core Application |
| `glob_search` | `tools.search.glob_tool:glob_search` | 默认 `core_search` 或显式选择 | 路径/查询排除；glob 和 exclude E2E、真实 core Application |
| `shell_tool` | `tools.shell.shell_tool:shell_tool` | 默认 `core_shell` 或显式选择 | Gateway/Hook、命令/路径/只读规则、文件历史与实际 cwd；真实 Shell、并发、后台终止和 core Application |
| `check_background_task` | `tools.shell.background_task_tools:check_background_task` | 默认 `core_shell` 或显式选择 | 本实例/Run 的任务归属；后台任务与跨 Run 隔离测试 |
| `kill_background_task` | `tools.shell.background_task_tools:kill_background_task` | 默认 `core_shell` 或显式选择 | 同上；终止真实进程、幂等关闭与其他实例存活测试 |
| `list_background_tasks` | `tools.shell.background_task_tools:list_background_tasks` | 默认 `core_shell` 或显式选择 | 本实例/Run 范围；后台任务列表与资源测试 |
| `todo_write` | `tools.todo.todo_write:todo_write` | smol `todo.mode=auto/on` 自动补齐；`off` 移除 | Gateway/Hook、实例作用域、schema 与原子替换；Todo/恢复测试、真实 on/auto/off Application |
| `final_answer` | `terminal:final_answer_binding` | smol 必备终结能力，由 adapter 补齐 | 公共 Stop/Goal 判定保持生效；runtime/Application/Goal 验收。manifest operation 为 `control`，capability 为 `completion.final` |

`check_path_exists`、`quick_list_directory` 等既有 Python 辅助导出随 smol 迁移，但并非 catalog 的默认 builtin，不新增隐式注册。完整文件位置与兼容路径见 [04-path-migration.json](04-path-migration.json)。

平台接线仍是公共 ToolGateway、Hook、权限、资源关闭、规范结果/checkpoint envelope，以及平台提供的 Goal、ContextRef、长期记忆、Skill/Worker 入口。原生工具通过这些入口接受平台约束，平台不执行第二份 smol 循环，也不把 Todo 内容当作 Goal 或长期记忆。

04 保留了基础工具中既有的保护调用，但没有将它们作为公共工具实现。06 的精确保护提取清单、函数、调用者、测试与剩余兼容消费者见 [实施交接](04-implementation.md)。Markdown、大纲、AST/LSP、MCP 与平台工具的中立化由 08 负责；本票保留它们的旧 smol 兼容路径。
