# 09 实施与验收记录

09 在 `codex/pi-t09-tools` 独立 worktree 开发，初始基线 `7965cc00`。使用官方 Pi SDK 0.79.4，通过生产 `execute_app` 验证；没有复制 SDK、创建 experiments 或实现另一套 Agent loop。

## 已接通的边界

- **Pi**：官方 `read` 执行器、模型 turn、工具调用循环与原生内存会话。`read`/`pi_read` 显式选择，未选择时不出现；不回退自研 smol 工具。09 只开放已验证的 `read`，grep/find/ls 的目录范围映射没有随本票开放。
- **AgentLoom**：YAML、Supervisor/Worker、Goal/Stop、SkillCatalog、长期记忆/历史、ContextRef、公共 Hook/Gateway/Native Host 与 Run/实例资源。平台/可选专业/MCP 工具沿 08 的中立入口执行。
- **Pi adapter**：协议和 schema 转换、模型参数投影、工具包装、事件证据与进程生命周期。平台规则没有复制进 SDK，也没有导入 smol 基础工具。

平台工具沿 `platform_invoke` 进入已有 Gateway；真实 SDK 的异步 `message_end` 入口先交原始参数给 Hook，拿到规范 ToolCallRecord 后更新 SDK 中的最终输入。SDK execute 只消费已完成的回执，不重复执行 Python 工具。原生 read 沿 prepare → 官方 execute → settle，只有持久提交确认后才把结果交给模型。

工具并发沿用 catalog 和 Agent 有效 `tool_metadata`：不安全工具在实际平台回调发生前串行调度，安全批次继续并行。Hook 的 `agent_context` 在下一次模型请求恰好投递一次，不等整个 SDK prompt 结束。

双向回调在等待 Run 的同时执行，保留 Application/Task/Run/instance/call ID 与 Hook ContextVar。并行 Worker 的证据汇总到根 Run，两个 Worker 可以使用相同 provider call ID。取消先停止排队回调，再关闭对应 Run/实例资源并排空已运行回调。关闭标记随执行上下文共享，迟到资源立即关闭；Hook 子进程在 Popen 前登记，取消能阻止启动或终止实际进程。12 调用/8 回调线程的 SDK 测试确认只启动 8 个 Hook 子进程，全部收回，排队 4 个不启动。MCP server、Node 和 Hook 子进程均验证清理。

普通宿主 Python 扩展必须以有限操作或 `register_resource` 关闭句柄协作退出；框架不会强杀调用 execute_app 的宿主线程，也不会在回调仍可写入时先宣布 Run 结束。强制终止保证针对受管子进程。这沿用 03 的资源合同，不新增必填 YAML。

Goal 在每个模型 turn 前通过 `model_prepare` 检查。完成后仅根 Supervisor 可消费一次最终交付许可，模型请求不再提供工具，执行入口也拒绝新工具工作；Stop 拒绝不会被 Goal 完成状态吞掉。模型 timeout/rate/retry 按每次模型请求执行，不包含平台工具耗时，不回滚历史或重放已完成工具。桥接目前输出模型阶段事件，没有新增 token 流接口。

ContextRef 的公共任务存储现在独立于 checkpoint 开启：Pi 关闭 checkpoint 时也能压缩并检索大工具输出，Worker 继承同一任务存储。这不表示 Pi 已支持恢复。

## 使用方式与安装

```yaml
name: pi_reader
agent_runtime: pi
checkpoint: {enabled: false}
toolsets: []
tools:
  - name: read
  - name: get_file_outline
description: 检查仓库里的实现。
workflow: 读取指定源码，并使用 get_file_outline 核对结构后给出结论。
```

`uv run --locked loom install-runtime pi` 安装并构建桥接；直接依赖 `@earendil-works/pi-coding-agent`、公共错误流所需的 `@earendil-works/pi-ai` 都固定为 0.79.4，完整 npm 依赖树由 package-lock 固定。SDK 是 Node 包，uv 启动 Python 安装入口，由 npm ci 下载。生成的 node_modules/dist 不提交 Git；源码/schema/lock 指纹变化会要求重新安装构建。

写入/Shell/Markdown 写工具、未映射的旧 smol 工具名、Pi checkpoint/resume 仍在执行前明确拒绝；这些不能通过追加 runtime_options 绕过。混合 smol/Pi 与干净安装的完整验收分别留给 11/13，写入/Shell 与恢复分别由 10/12 完成。

## 测试证据

公开边界测试使用真实已安装的 Pi SDK + HTTP 模型 fixture，强制覆盖非法参数、异步 Hook 修正及拒绝、原生/平台归属、同 Run 并行、Worker 隔离、Goal/Stop、取消、进程死亡、MCP 释放和 ContextRef。另在原生 settle 前、持久 commit 后分别杀死真实 SDK：前者保留 uncertain，后者保留 committed，两者 Application 均失败，不能制造成功。

真实 provider 使用仓库私有模型配置，覆盖 Chat 与 Responses；不会把配置或凭证写入报告。每次尝试保存源码 SHA、工作区是否有改动、模型 profile、Run、工具回执、MCP 进程事件和验证结果。原始证据放项目外 `AgentLoom-validation/t09/`，项目内保留摘要和重跑入口 `tests/acceptance/pi_tool_validation.py`。

历史失败不删除：

- smoke-01：5/6。并行 Worker 实际执行完成，但根审计遗漏 Worker 工具记录；已修复公共事件传递。
- campaign-01：38/40。两个 ContextRef 场景因 checkpoint 关闭时公共存储未激活而失败；已从 checkpoint 接线中独立，context-02 的两种模型复验 2/2。
- campaign-final-01：40/40；06 集成及审查修复后的 campaign-integrated-02：40/40。最后 40 次全部来自干净源码 `2bfcde13`。

完整回归 **4408 passed、1 skipped、3 warnings，395.89 秒**；TypeScript 构建通过，改动范围的 Python 类型检查 12 个文件、0 error/0 warning。两条弃用警告和一条模型 fixture 序列化警告没有测试失败。全部 5 轮真实模型尝试共 128 次，125 次通过、3 次历史失败；修复后最终两轮均为 40/40。详细命令、候选 SHA、验收映射和外部证据路径见 [09-validation.json](09-validation.json)。

## Standards

独立审查指出 Hook context 投递、工具并发 metadata、Hook 子进程登记与取消竞态，已补公开入口回归并修复。复核无剩余可操作问题；未复制基础工具实现或公共治理规则。

## Spec

独立审查确认下一轮 Hook context、取消期间的受管 Hook 进程、排队回调与迟到登记都已闭合。复核无剩余确定性规格缺口；普通 Python 扩展的协作取消边界如上，不宣称可强制终止任意宿主函数。

审查余项：Standards 0，Spec 0。

## 集成与交付

06 和后续目录清理已通过 `e26bfa7e` 合入 09 worktree，阶段提交保留。审查修复后的代码候选为 `2bfcde13`；完整回归及最终真实应用结果见 [09-validation.json](09-validation.json)。首次完整检查的 7 个失败也保留：早期目录归属校验拦截已有 resolver 的固定参数校验/只读详情、catalog 测试假定全部 native 为 smol、直接运行缺少 ContextRef 根目录；已分别修复或更新对应能力断言，没有新增 Studio 功能。

2026-09-20 已将 main 从 `8d7ba7bf` 正式快进到 `cc0edeb18aeedb29e1c40e0c44ead449fc1223dc`，保留全部阶段提交。冻结入口 `refs/agentloom/ticket09-frozen` 指向该交付提交；其中源码/测试与受检 `2bfcde13` 完全一致，后续只有交付文档补记。

main 已执行 `uv run --locked loom install-runtime pi` 并确认 0.79.4 ready，Python 导入路径也指向 main。本票 worktree `/Users/bytedance/.codex/worktrees/pi-t09-tools/AgentLoom` 已删除，实现分支和外部验收证据保留。现有 `codex/`、`temp/`、参考 `pi/` 及两个 stash 未动；没有推送远端。

06 与 09 已完成，因此 **10、11、13 可分别新建 session/worktree 并行**；10 完成后再做 12；11、12、13 完成后串行做 14。Pi adapter/bridge/Node lock 的修改权移交 10，13 不共改该锁文件。
