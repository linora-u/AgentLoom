# 08 平台、可选专业工具与 MCP 交接清单

本票沿用 03 的 `platform_catalog`、`optional_catalog`，没有新增一份全量注册表。所有下列入口由中立 `ToolBinding` / `ToolGateway` 构造与执行；smol 只负责把定义呈现给自己的执行循环。文件、Shell、grep/glob、Todo 的基础实现见 [04 清单](04-tool-inventory.md)，不注入 Pi。

| 原名称 | 归属 / 提供方 | 当前入口（`agentloom.` 前缀省略） | 选择与默认 | 公共约束与行为证据 |
| --- | --- | --- | --- | --- |
| get_goal / update_goal | platform / agentloom | tools.goal.goal_tools | 沿用已有 Goal 开关 | 根目标归属、完成证据；Goal 单测及中立/真实 Application |
| YAML 定义的 Worker 函数 | platform / agentloom | runtime.factory、runtime.agent | 仅装配选定 Worker | 每次新实例、独立 Run/连接；并发 Worker Application |
| memory | platform / agentloom | tools.self_learning.memory_tool | 沿用 self_learning 与显式工具选择 | app/project 范围、项目晋升审核；中立与真实 Application |
| session_search | platform / agentloom | tools.self_learning.session_tools | 同上 | 受范围约束的历史检索；中立与真实 Application |
| session_scroll | platform / agentloom | tools.self_learning.session_tools | 同上 | 索引事件周围的脱敏记录；既有 self_learning 回归 |
| loom_retrieve_context | platform / agentloom | tools.context.retrieve_context | 沿用 context 集合或显式选择 | 当前运行的 ContextRef 原文；中立 Application 验证隐藏标记 |
| skill | platform / agentloom | tools.skills.skill_tool | 沿用 skills 集合或显式选择 | 当前 SkillCatalog、app 优先级、惰性激活；中立与真实 Application |
| skill_manage | platform / agentloom | tools.self_learning.skill_manage_tool | 沿用 self_learning 或显式选择 | 提案不自动激活/晋升；中立与真实 Application，保留实际失败记录 |
| write_markdown_file | optional / agentloom | tools.file_ops.markdown_writer | 显式工具或 markdown_report 集合 | 文件路径权限、写入历史；真实 Markdown 产物 |
| write_markdown_file_raw | optional / agentloom | tools.file_ops.markdown_writer | 同上 | 同上；中立 Application 实际写入，真实模型漏参限制见报告 |
| append_markdown_sections | optional / agentloom | tools.file_ops.markdown_writer | 同上 | 同上，保留已有内容；真实 Markdown 产物 |
| get_file_outline | optional / agentloom | tools.file_ops.file_outliner | 显式工具或 code_nav 集合 | 路径读取权限；Python/JSON 大纲真实 Application |
| ast_grep_search_file | optional / agentloom | tools.search.ast_grep_tool | 同上 | 路径读取权限；AST 实际匹配 |
| lsp_find_definition | optional / agentloom | tools.search.lsp_tool | 同上 | 路径权限、实例资源归属；真实 Application / 既有 fallback 回归 |
| lsp_find_references | optional / agentloom | tools.search.lsp_tool | 同上 | 同上 |
| lsp_get_document_symbols | optional / agentloom | tools.search.lsp_tool | 同上 | 同上；中立 Application |
| lsp_hover | optional / agentloom | tools.search.lsp_tool | 同上 | 同上；真实 Python Language Server，断言返回来源为 LSP |
| lsp_get_workspace_symbols | optional / agentloom | tools.search.lsp_tool | 同上 | 目录授权、实例资源归属；真实 Application / fallback 回归 |
| mcp__server__tool | external / mcp:server | adapters.mcp | 仅配置选定服务时发现 | 原始 JSON Schema、公共 Hook/Guard/记录、连接隔离、错误/取消/超时清理 |

“沿用”不增加必填 YAML。专业工具不会成为新的平台默认依赖；未选择 LSP 时 Application 不导入或预热服务。选中后首次调用才启动所属实例的服务，关闭一个 Worker 不影响另一个 Worker；初始化异常和重复语言配置也验证资源释放。

公共执行顺序不变：Hook 修正参数 → 最终严格解码/校验 → Guard → 文件历史 → 记录最终输入 → 执行。MCP 使用新增的可选 `ToolBinding.input_validator` 在最终解码阶段验证完整 schema；其本地 `$ref`、组合类型、额外属性保持原义，不自动联网读取 schema 引用。MCP 的 `operation=control` 不代表自动获得文件访问能力，远端服务仍需自身权限约束。

09 消费当前中立 manifest 与 invoke，不再把工具包转回 smol `Tool`。SkillCatalog、范围和提案规则继续由平台持有；Pi 注册中立 `skill` 或选择经过验证的原生呈现路径，必须只启用一条发现/激活路径。本票没有开启 Pi 本地资源发现。

主要证据入口：`tests/application_test/test_platform_tool_application.py`、`tests/mcp_test/`、`tests/goal_test/`、`tests/acceptance/platform_tool_validation.py`。SDK 不存在的真实 Python 环境验证了平台工具和中立 Application；Pi 生产回调接线仍由 09 验收。
