# 04 实施与交接

## 基线与实施顺序

- 起点：`9d5a88915efbd402d0f2697318125933fc1500e0`。01–03 已保留原有提交历史合入 main；7 份待合入研究文档另作提交。未跟踪的参考仓库不纳入本票。
- 工作区：`AgentLoom-worktrees/t04`，分支 `codex/pi-t04-smol`；私有 `config/llm.yaml` 已逐字复制、设为 0600 并确认被 Git 忽略，凭据不进入提交和验收报告。
- 阶段 1：基础文件、grep/glob、Shell/后台任务迁入 smol，旧导入路径保持同一实现和状态。
- 阶段 2：Todo、原生模板、选项解释和私有状态迁入 smol；公共层只保留通用上下文和兼容转发。
- 阶段 3：按 Application、ToolGateway/catalog、runtime/checkpoint 和资源关闭等已约定边界回归；独立双轴审查。
- 阶段 4：固定代码提交后运行真实模型验收，记录成功和失败；最终回交主工作区并删除本票 worktree，保留阶段提交及外部验收证据。

每个可验证阶段单独提交，不 squash 成一个提交。公共接线由本票协调者串行处理，不修改 05 的 Gateway 或 08 的专业工具实现。

## 基础工具迁移

精确旧→新文件清单见 [04-path-migration.json](04-path-migration.json)。实现位于 `src/adapters/smolagents/tools/`；原模块只保留兼容入口。叶子模块别名指向同一个模块对象，避免旧消费者和新执行者形成两套 Shell 注册表、读取缓存或不同的函数全局状态。

文件/搜索混合包只迁移登记的基础工具；AST/LSP、大纲和 Markdown 仍由 08 处理。子进程环境直接复用 `runtime/subprocess_env.py`，旧 `tools.shell.subprocess_env` 继续兼容。

## 私有状态与公共接线

- smol 的 `options.py` 解释规划、摘要、Todo、模板与恢复参数；Application 只做层级投影和旧字段兼容分派。RuntimeFactory 消费解析后的私有选项，不再把这些选项写回公共 RuntimeDefinition。
- 原生模板和模型反馈恢复移到 smol；公共环境、Skill 目录、任务协议及 ContextRef 服务继续留在框架层。旧提示目录中用户未跟踪的本地覆盖仍可被读取。
- Todo schema/provider/store 位于 smol。生产 provider 只借用公共 `task_storage` 安全存储句柄；保持 `todos.json` 格式、跨进程文件锁、损坏隔离和恢复。CheckpointManager/Coordinator 的旧 Todo 方法仅作懒加载兼容转发。
- Shell 审计按需登记日志资源；logger 不再导入 smol。共享日志 scope 保证 Worker 第一次使用时也复用同一日志资源，日志关闭和 Run 资源关闭均可回收它；迟到的线程不能重新打开已关闭日志。
- 现有 native checkpoint envelope、模型协议和 smol 压缩逻辑保持原有格式。旧 smol Python 字段是 02 的过渡合同，14 根据消费者再清理；本票不破坏这些入口。

目前定向回归通过：工具/catalog/读取/资源 61 项；私有执行、Todo、提示词、Application 163 项；Shell 审计和导入隔离 63 项。阶段 2 的 14 个改动模块通过 mypy。大组工具回归 969 通过、1 跳过，后置 Application 的 3 个失败与遗留任务上下文有关，独立 Application 回归 3 项通过，继续查根因。

## 实例隔离修正

两个可复现的失败已通过独立回归定位：读取缓存原先是进程级单例，Worker B 会误以为自己读过 Worker A 读取的文件；旧 `task_context` 在恢复父任务时读取了另一个线程的全局 fallback，污染后续 Application 的显式任务 ID。

读取缓存现按完整 RuntimeKey 和 Agent instance 保存，登记 03 的捕获句柄关闭回调；关闭 A 不清空 B 的读取状态。直接调用工具、没有 Run 的旧调用者保留 standalone cache。任务上下文只恢复本 ContextVar 的显式父 ID。两个新增测试先复现失败，修改后连同 Shell 并发与后置 Application 共 31 项通过。

## 04 → 06 保护调用点交接

以下路径均以 `src/adapters/smolagents/tools/` 为根。06 在 04 与 05 均集成后接收这些文件的修改权，沿既有调用提取公共政策；不要再从旧路径复制实现。

| 迁移后文件 | 保留的保护与调用者 | 对应回归 |
| --- | --- | --- |
| `file_ops/_read_file_state.py` | `check_staleness`、`update_after_write` 被 `edit_file/edit_file.py`、`write_file/write_file.py` 使用；06 分离写前保护，读取范围缓存/去重继续归 smol，并保留实例隔离 | `tests/tools_test/file_ops/test_edit_file.py`、`test_write_file.py`；`tests/tools_test/test_smol_native_tools.py` |
| `file_ops/_safety.py` | read/edit/write 调用 `normalize_path`、`validate_file_access`；设备、二进制、读取大小策略仍属具体读取实现 | `test_safety.py`、`test_file_path_validation.py`、`test_read_file.py` |
| `search/search_utils.py` | `_load_exclude_paths` 经 `rg_exclude_globs` / `merged_skip_dirs` 提供给 grep/glob；06 提取权限配置访问，保留各自搜索格式 | `tests/tools_test/search/test_search_exclude_e2e.py`、`test_grep_exclude.py`、`test_glob_exclude.py` |
| `shell/validator.py`、`security.py`、`path_validation.py`、`readonly_validation.py` | `shell_tool.py` 在执行前调用 `validate_command`，再进入安全/路径/只读规则；06 统一政策调用，保留实际 session cwd 和最终输入约束 | `tests/tools_test/shell/test_validator.py`、`test_security.py`、`test_path_validation.py`、`test_path_security.py`、`test_readonly_validation.py` |
| `shell/shell_command_ast.py`、`pipe_redirect.py` | 命令解析及重定向分析；按政策/执行职责拆分，避免复制规则引擎 | `test_path_security.py`、`test_pipe_redirect.py`、`test_security.py` |

Shell process/session/background/watchdog/output、退出码解释和审计资源实现继续归 smol。`runtime/resources.py`、Gateway、Goal、长期记忆、ContextRef 的公共合同未改。

## 兼容入口清单（供 13 / 14）

- [路径清单](04-path-migration.json) 中旧工具叶子模块：仅别名；旧包 `__init__.py` 保留逐项导出。旧 YAML builtin 名、core toolset 和固定参数持续支持。
- `runtime.todo`、`tools.todo`、`runtime.error_recovery`、`runtime.prompts.prompt_builder` 及 prompt 包的旧导出：测试和外部 Python 调用者仍使用；生产 smol 调用已改为新路径。
- `CheckpointManager.load_todos/replace_todos` 与 Coordinator 同名入口：测试/旧调用者兼容；生产 smol provider 不再通过它们执行。`_todos_path` 也是旧辅助入口。
- `application.validation` 的旧执行/Todo validator、`application.runtime_options.SMOL_DEFAULTS/LEGACY_OPTIONS`：兼容转发；实现位于 smol options，且不加载 SDK。
- RuntimeDefinition 的旧 smol 字段、BaseAgent 的旧 `max_steps` 子类覆盖、公共 Gateway 的旧 `final_answer_binding`：保留 02 expand 合同。final_answer 实现和 manifest 已由 smol 持有。
- `tools.shell.subprocess_env`：唯一实现在公共 runtime；smol、Hook 等生产调用已直接指向公共实现。

14 只在确认调用者迁走后清理内部别名。旧 YAML 和用户显式模板路径不是可删除的内部别名。

## 验收计划

运行原有工具、保护、Todo、提示词、协议回放、checkpoint 与应用兼容测试，不删除已有行为断言。最终代码候选固定后，运行：

- `existing_application_validation.py` 的 9 组真实 Application（文件/Shell、Markdown、仓库探索、单元测试、三组 ContextRef、两组 Goal/并行 Worker）。
- `real_checkpoint_validation.py` 的 main / worker / completed 三组真实中断恢复。
- `model_protocol_matrix.py` 中私有配置可用的协议配置；缺失记 NOT-RUN，不冒充通过。

最终结果、审查结论及 04→06 的保护调用点在验收后补齐。
