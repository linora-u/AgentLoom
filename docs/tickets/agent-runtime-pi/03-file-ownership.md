# 03：并行文件所有权与兼容移交

本清单基于 03 实际调用者登记。它细化 [worktree 计划](worktree-plan.md)，避免按旧 tools 目录整包搬迁。路径为当前源文件；04 完成后提供精确目标路径给 06。任何一份文件同时只有一个修改者。

**状态：03 已验收并冻结分工。** 下游基线必须包含 `refs/agentloom/ticket03-frozen` 对应提交，并核对 [验收记录](03-validation.json)；允许经检查的后继提交，实际工作区见 [当前登记](worktree-plan.md)。本文定义修改权，不表示后续迁移已经验收；协调者统一分发本轮文档修订。

## 冻结后可独立编辑的范围

| 票据 | 独占源文件/目录 | 必须保留的边界 |
| --- | --- | --- |
| 04 | `src/adapters/smolagents/`；`src/tools/file_ops/read_file/`、`edit_file/`、`write_file/`、`directory_browser.py`、`path_existence.py`；`src/tools/search/grep_tool/`、`glob_tool/`；`src/tools/todo/`、`src/runtime/todo/`；下表列明的 Shell/文件混合模块 | smol 的基础执行、状态、Todo 和 terminal；不搬平台 Goal/记忆/专业工具 |
| 05 → 06 | `src/runtime/tool_gateway.py` 的执行实现、native journal 新实现及对应治理测试 | 消费已冻结的 `native_tools.py`；其他分支不同时改 Gateway。合同变化先交协调者，再由本文件所有者接入 |
| 07 → 09 → 10 → 12 | `src/adapters/pi/` 的协议消费/生产 bridge、独立 Node manifest/lock、Pi 专属测试 | 顺序开发，不与其他分支共同改 Node lock；不把测试 SDK fixture 当生产 runtime |
| 08 | `src/tools/platform_catalog.py`、`optional_catalog.py`；`src/tools/context/`、`self_learning/`、`goal/`、`skills/`；`src/tools/file_ops/file_outliner.py`、`markdown_writer.py`；`src/tools/search/ast_grep_tool/`、`lsp_tool/` 与查询资源；`src/adapters/mcp/`；`src/runtime/skills/catalog.py` | 平台入口/可选工具去 smol 构造依赖；保留 scope、审核与证据规则，不改基础工具 |
| 11 | 混合应用 YAML 与独立协作/记忆验收 | 只消费 09 已有只读和平台能力，避免暗中依赖 10/12 |
| 13 | Python packaging/lock、installer、CI、安装失败路径及发行测试 | 消费 Pi 构建产物，不改 Node lock；最终发行物由 14 重建复验 |

## 混合模块按职责交接

| 当前文件/函数 | 04 阶段 | 后续所有权 |
| --- | --- | --- |
| `file_ops/_read_file_state.py`：`ReadFileState.check_dedup`、范围/内容缓存 | 整文件由 04 收入 smol 并保持已有行为；04 提供迁移后路径 | 缓存、去重、内容摘要仍属 smol；不能变成 Pi 必需状态 |
| 同文件：`check_staleness`、`update_after_write` 的保护语义 | 04 暂保留调用与测试，不能删除保护 | 06 接收整文件的修改权，提取公共写前规则并给 smol 接回调用；不复制去重算法到 Pi |
| `file_ops/_safety.py`：`normalize_path`、`validate_file_access` 与设备/二进制检查 | 04 保留实际读写行为并登记每个调用点 | 06 按既有承诺区分公共访问保护与基座读取策略；只提取需共享的规则 |
| `search/search_utils.py`：`_load_exclude_paths` | 04 暂保留规则读取与调用点 | 06 提取权限规则访问；逻辑工具名映射不能绕过排除范围 |
| 同文件：`SKIP_DIRS`、ripgrep glob 格式和 Python fallback 格式 | 04 维护具体搜索算法 | 执行格式留 smol；Pi 使用官方能力，经验证后接入同一查询约束 |
| `shell/process.py`、`background_task.py`、`shell_session.py`、`shell_snapshot.py`、输出 reader/interceptor、watchdog、tree_kill | 04 持有具体会话、句柄、后台状态、输出解析和关闭回调 | 公共资源 API 不反向导入这些文件；06 仅接收明确列出的保护调用点 |
| `shell/validator.py` 的 `validate_command`；`security.py` 的 `check_command_security`/`validate_command_security`；`path_validation.py` 的 `check_path_constraints`；`readonly_validation.py` | 04 迁移时保持规则效果及现有测试 | 06 接收策略提取和调用点，供 smol/native 共用；04 移交后不再改这些文件 |
| `shell/shell_command_ast.py` 与命令解析 helper | 04 保留执行依赖 | 06 如需共享解析，先明确哪些函数是政策解释、哪些是基座输出处理；不能复制第二套政策引擎 |
| `shell/command_semantics.py` 的退出码解释、展示文本 | 04 | 留在 smol 执行实现；native executor 负责自己的真实执行结果，公共层规范终态 |
| `shell/subprocess_env.py` 兼容导出 | 04 可迁移调用者 | 实现已归 `runtime/subprocess_env.py`；Hook 已直接使用公共入口，14 按消费者情况清理旧导出 |
| `runtime/skills/parser.py`：`parse_skill_file`/`SkillMetadata` 与 `build_skills_prompt` | 不允许 04/08 同时编辑此混合文件 | 文件由协调者串行处理：目录解析归平台，基座提示呈现按 adapter 接线。04 的原生模板仍在 smol 目录独立开发 |
| `runtime/prompts/prompt_builder.py`、`runtime/agent.py::_build_runtime_instructions` | 保持既有平台上下文和 smol 兼容 | 协调者串行处理：平台记忆/环境/Skill 目录与基座模板、压缩、Todo 提示分开；04/08 交小补丁请求 |

以上 04→06 是实际修改权交接，不只是逻辑依赖。06 开工前，04 必须交出旧→新路径、仍使用的保护函数和对应测试；06 不从旧目录复制一份继续开发。

## 始终由协调者串行接线

`src/runtime/agent_runtime.py`、`native_tools.py`、`resources.py`、`agent.py`、`factory.py`、`invocation.py`；Application 定义/validation/readiness/lifecycle；配置和模型 headers 投影；`src/tools/catalog.py`、`catalog_types.py`、`selection.py`、`loader.py`；混合包的 `__init__.py`；公共 prompt/Skill parser；`src/application/runner.py::_execute_app` 中的 LSP 预热入口。

04/08 各自修改登记分区，不编辑全量 catalog。基础工具迁移可保留旧路径兼容导出，使未完成的另一分支仍可运行。公共入口的必要补丁计入提出需求的那张票：协调者串行合入，实现分支同步后完成实际 Application 验收，不能拖到 14。

08 调整未选专业工具的 LSP 构造/预热时，提交 Application runner 的最小接线补丁，由协调者与 07 的公共入口修改串行集成；不能只让 catalog 的 lazy import 测试通过就宣称整个应用已无需这些资源。

## 测试交接

| 所有者 | 03 已有的基线与后续要求 |
| --- | --- |
| 协调者 | `tests/application_test/test_native_runtime_expansion.py`、`test_smol_compatibility_application.py`；catalog boundary、canonical `runtime_options`、旧 smol 字段静默忽略与工具固定参数测试。公共入口改动需重跑对应用例 |
| 04 | `tests/tools_test/shell/`、基础 file_ops/grep/glob/Todo 测试与 smol 测试；保留真实 Shell 调用、后台进程终止及实例隔离用例 |
| 05/06 | `tests/lib_test/runtime/test_native_tool_contract.py` 和 `native_contract_fixture.py` 的场景，迁接生产 Gateway；既有 Gateway/Hook/权限、文件保护和证据测试 |
| 07/09/10/12 | `test_pi_bridge_protocol.py` 与协议 schema；`tests/pi_sdk_compatibility/` 的真实 SDK 接入结论；逐票增加实际公开入口测试 |
| 08 | 选定平台工具、MCP、AST/LSP/大纲/Markdown 的构造/执行；目录包边界不依赖 smol SDK；与 04 不共改同一测试文件 |

原有混合测试文件由协调者管理，先拆出按归属的测试再移交；各分支不能为了迁移自行删掉另一组仍在使用的测试。

## 兼容入口与清理时机

main 已按当前消费者清理一批迁移残留，见 [迁移清理记录](migration-cleanup.md)。本节的 03 冻结分工及历史移交表保留；后续开发以清理记录中的现存路径为准。

- 工具默认集合、builtin 名称/固定参数及启动方式保持既有契约；这不包含旧顶层 smol 执行参数。后端参数只解释 `runtime_options`，旧字段静默忽略。
- `agentloom.tools.catalog.ToolSpec` / `ToolImplementation` 保留原导入位置，新实现类型在 catalog_types；metadata 不引入 SDK。
- `agentloom.tools.shell.subprocess_env` 与 `agentloom.adapters.litellm.request_headers` 两个临时兼容导出已清理；调用者分别直接使用 `agentloom.runtime.subprocess_env`、`agentloom.configuration.model_request_headers`。
- 14 按维护者最新要求删除 smol 顶层旧字段解释与公共 RuntimeDefinition 的过渡参数；仓库应用、生成指南和测试使用 `runtime_options`。旧 terminal 入口仍按实际消费者独立核对，不借 YAML 变更扩大删除范围。
- Goal 的旧 smol decorator 仍由 08 移除；03 只标明平台 manifest。Todo decorator 与状态由 04 处理。
- SDK 原始 RESULT.json 和历史提交证据不改写；移动记录见 `03-sdk-relocation.json`。03 的新重放不冒充 01 的原始运行。

03 的冻结结果继续作为公共合同基线；四路使用包含该结果的已验证提交，并同步本清单的最新修订。存在 worktree 不改变文件所有权，也不意味着可以启动尚有未完成前置的下一票。
