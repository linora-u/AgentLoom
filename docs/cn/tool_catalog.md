# 内置 Tool Catalog

AgentLoom 将内置工具的 metadata 与 Python implementation 加载彻底分开。
因此，toolset 校验、ContextEngine 路由、路径保护和文档生成只读取 catalog
时，不会连带导入 Shell、自学习、Todo 或 ContextEngine implementation。

## Runtime 契约

| Module | 唯一职责 | 禁止承担 |
|---|---|---|
| `src/tools/catalog.py` | `ToolSpec`、toolset 归属、描述、安全与输出 metadata、implementation 引用 | 导入具体工具包或 Runtime 配置 |
| `src/tools/loader.py` | 把一个已注册 implementation 引用解析为 callable | 工具 metadata 或 toolset 归属 |
| `src/tools/tool_meta.py` | 合并全局与 Agent override 后的有效 metadata | 转发 catalog interface 或加载 implementation |
| `src/tools/<group>/` | 具体工具 implementation | 内置 catalog 归属 |

`src.tools` 根包有意不导出任何内容。导入根包既不会注册工具，也不会加载
工具。Runtime 通过 `src.tools.loader` 解析已注册内置工具；只需要 metadata 的
代码直接依赖 `src.tools.catalog`；只测试某个具体 implementation 时，可以直接
导入对应工具组。

implementation group 的 `__init__.py` 兼容导出必须保持 lazy：只有真正请求某个
导出名时才允许加载对应 module，不能提前导入 sibling implementation。catalog
引用指向职责最窄的 implementation module，不指向这些 package facade。

catalog 同时负责面向配置的 `fixed_args` 参数契约。只读校验直接检查 metadata，
不导入 implementation；运行时构造再用真实 callable 复核。边界测试会比较两份
签名，接口漂移会直接导致测试失败。

依赖方向固定为：

```text
metadata consumer ──> catalog <── loader ──> selected implementation
                              ^
effective metadata ───────────┘
```

catalog 是唯一事实源。函数即使存在于 `src/tools/`，没有 catalog 中的
`ToolSpec` 也不是内置工具。YAML 动态工具、生成的 Worker 工具不进入该 catalog；
Goal 工具由 Goal Mode 注入，也不属于默认 catalog。

## Toolsets

`config/system.yaml` 的 `default_toolsets` 提供全局列表。Agent YAML 的
`toolsets:` 会整体替换该列表，不是追加；`toolsets: []` 表示关闭该 Agent 的
内置工具。

| Toolset | 内置工具 |
|---|---|
| `core_shell` | `shell_tool`, `check_background_task`, `kill_background_task`, `list_background_tasks` |
| `core_file` | `read_file`, `edit_file`, `write_file`, `list_directory` |
| `core_search` | `grep_search`, `glob_search` |
| `context` | `loom_retrieve_context` |
| `skills` | `skill` |
| `self_learning` | `session_search`, `session_scroll`, `memory`, `skill_manage` |
| `planning` | `todo_write` |
| `markdown_report` | `write_markdown_file`, `write_markdown_file_raw`, `append_markdown_sections` |
| `code_nav` | `get_file_outline`, `ast_grep_search_file`, `lsp_find_definition`, `lsp_find_references`, `lsp_get_document_symbols`, `lsp_hover`, `lsp_get_workspace_symbols` |

当前默认启用 `core_shell`、`core_file`、`core_search`、`context`、`skills` 和
`self_learning`。`planning` 由 Todo 策略选择；Markdown 报告与代码导航为显式
启用的 toolset。

## 新增内置工具

1. 将 implementation 放进职责最窄的现有工具组；没有合适归属时再创建新组。
2. 只在 `src/tools/catalog.py` 增加一个 `ToolSpec`，显式填写 implementation
   引用以及完整的安全与输出 metadata。
3. 不增加根包转发，不允许 catalog 导入 implementation。
4. 增加解析、metadata 与 fresh-interpreter import 测试；metadata-only 测试必须
   证明没有加载任何 implementation group。
5. 运行覆盖对应 toolset 的真实 Application，并检查 Run manifest、日志、实际
   tool call 与落地产物。

必跑的定向检查：

```bash
uv run pytest \
  tests/tools_test/test_tool_catalog_boundaries.py \
  tests/hooks_test/test_tool_resolve.py \
  tests/hooks_test/test_tool_meta.py -q
```

修改 catalog 或 loader 时至少运行以下真实 workflow：

```bash
AGENTLOOM_RUNTIME_ROOT=/tmp/agentloom-catalog-core \
  uv run loom run applications/tool_registry_core_validation/workflows/core_tools_agent.yaml
AGENTLOOM_RUNTIME_ROOT=/tmp/agentloom-catalog-markdown \
  uv run loom run applications/tool_registry_markdown_validation/workflows/markdown_report_agent.yaml
AGENTLOOM_RUNTIME_ROOT=/tmp/agentloom-catalog-resolve \
  uv run loom run applications/test_demo/workflows/test_tool_resolve_agent.yaml
AGENTLOOM_RUNTIME_ROOT=/tmp/agentloom-catalog-context \
  uv run loom run applications/context_engine_text_retrieve_validation/workflows/context_engine_text_retrieve_validation_agent.yaml
AGENTLOOM_RUNTIME_ROOT=/tmp/agentloom-catalog-self-learning \
  uv run loom run applications/self_learning_smoke/workflows/self_learning_smoke_agent.yaml
```

并发验证时为每个进程使用不同的 `AGENTLOOM_RUNTIME_ROOT`。进程退出码为零仍不足以
证明通过；必须检查每个 `manifest.json`、`logs/runtime.log`，以及工具真正生成的
文件或数据库记录。

### 验证记录：2026-08-05

提交为 `aad0daa` 的运行时代码已通过隔离的真实模型运行。所有 manifest 均以
`status: completed` 结束；审计 runtime log 后，未发现工具未注册、未知 toolset、
implementation 不可调用或循环导入错误。

| Application | Run ID | 已审计结果 |
|---|---|---|
| Core catalog | `run_20260805T121722420197Z_c82db5eab6e6` | `CORE_TOOL_REGISTRY_VALIDATION: PASS` |
| Markdown toolset | `run_20260805T121722420159Z_2299de139dc2` | `MARKDOWN_TOOLSET_VALIDATION: PASS` |
| Search 与 LSP | `run_20260805T121722420285Z_67c9db03778a` | 7 项检查全部通过 |
| Context retrieval | `run_20260805T121722420262Z_67f264434fcd` | 从 context store 取回 `JSON-CTX-4927` |
| Self-learning | `run_20260805T121722420417Z_496aaaa4cd2f` | memory/skill proposal、reference 写入、文件读取均为 `ok: true` |
