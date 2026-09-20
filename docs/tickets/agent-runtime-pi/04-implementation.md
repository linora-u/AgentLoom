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

## 验收计划

运行原有工具、保护、Todo、提示词、协议回放、checkpoint 与应用兼容测试，不删除已有行为断言。最终代码候选固定后，运行：

- `existing_application_validation.py` 的 9 组真实 Application（文件/Shell、Markdown、仓库探索、单元测试、三组 ContextRef、两组 Goal/并行 Worker）。
- `real_checkpoint_validation.py` 的 main / worker / completed 三组真实中断恢复。
- `model_protocol_matrix.py` 中私有配置可用的协议配置；缺失记 NOT-RUN，不冒充通过。

最终结果、审查结论及 04→06 的保护调用点在验收后补齐。
