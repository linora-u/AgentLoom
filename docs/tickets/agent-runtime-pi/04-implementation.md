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

## 验收计划

运行原有工具、保护、Todo、提示词、协议回放、checkpoint 与应用兼容测试，不删除已有行为断言。最终代码候选固定后，运行：

- `existing_application_validation.py` 的 9 组真实 Application（文件/Shell、Markdown、仓库探索、单元测试、三组 ContextRef、两组 Goal/并行 Worker）。
- `real_checkpoint_validation.py` 的 main / worker / completed 三组真实中断恢复。
- `model_protocol_matrix.py` 中私有配置可用的协议配置；缺失记 NOT-RUN，不冒充通过。

最终结果、审查结论及 04→06 的保护调用点在验收后补齐。
