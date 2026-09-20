# 05：原生只读工具治理实现与交接

实现基线：`9d5a88915efbd402d0f2697318125933fc1500e0`，包含 03 冻结提交；额外差异只有研究文档。
受检实现：`5b26985d12b5d590150db189a3091faaf1ec345f`。最终文档提交不改变该实现的源码与测试树。
分支：`codex/pi-t05-governance`。本票只实现公共只读治理，不实现 Pi bridge、smol 工具迁移或写入/Shell。

## 已交付的执行路径

`NativeReadToolHost` 在显式 Application Run、Hook Run 和 Agent instance 内构造，接收可信装配端选定的 manifest 与固定 cwd。它与 Python Gateway 共用 `_prepare_tool_input`：PreToolUse → 最终严格解码 → CoreToolGuard → 文件保护/最终输入观察。读取算法留在外部 executor。

逻辑工具名与模型可见名称共同匹配 Hook 和路径规则；同一个 Hook 只执行一次，保留 Hook Plan 顺序。原生 cwd 显式传递，不切换进程 cwd。原生参数不进行 Python 工具的隐式类型转换，Hook 修复后仍须满足 schema。

生产入口在 `src/runtime/native_tool_host.py`，持久存储在 `src/runtime/native_journal.py`；公共合同仍为 `native_tools.py` 的 v1，没有新增 Pi 协议字段。

## 06/09 调用顺序

1. 在当前 invocation 内构造 host。传入已选择的 `ToolManifestEntry`；不要接受模型提供的 manifest、cwd 或权限规则。
2. `prepare(NativePrepareRequest)` 保存原始输入，返回授权或被拒绝的 ToolCallRecord。拒绝也落盘，且不产生可执行授权。
3. 独立执行门调用 `start_execution(grant)`。它按持久批准核对 Application/task/Run/instance/call、native session/parent、manifest、提供方、cwd、最终参数及 authorization_id；先持久写入 executing，才返回一次可派发授权。
4. executor 仅使用执行门返回的 manifest/cwd/final_arguments。09 的 `tool_prepare` RPC 可以在回复 bridge 前完成第 2/3 步，无须新增未经协调的协议消息。
5. 实际结果交给 `settle(NativeExecutionOutcome)`。只有 `NativeCommitAck` 表示已持久结算；`NativeJournalEntry` 的 uncertain/cancelled 不得转换成成功的 ToolCallRecord。
6. `cancel(identity)`：派发前为 cancelled，派发后为 uncertain。已提交调用保持原样；重复结果返回同一 ack，冲突结果被拒绝。`close()` 与平台实例/Run 资源关闭接线会结束本 host 的待执行调用。

异步 bridge 回调必须重新绑定该 invocation 的显式上下文。其他 Hook Run 即使处于相同 Application Run，也不能复用本次调用。

## 持久结果与证据

- 每个 Run 的 `native-tools/` 保存按 instance/call 隔离的 JSON 快照；文件名使用摘要，原始身份在内容中。独立 host/process 通过既有 `SecureDirectory` 的文件锁竞争同一调用，不依赖进程内集合实现一次执行。
- 授权、消费、取消和结算通过原子替换及文件/目录 fsync 落盘；初始化还同步目录祖先，避免新建 journal 的目录入口丢失。重复 ack 也重新确认落盘。磁盘失败向调用方传播，不生成成功确认。
- 一个终态快照包含原始 request、最终参数、实际原文、结果摘要、ToolCallRecord、commit_id 与已验证证据，避免跨文件提交一半。`receipt(identity)` 返回独立快照供审计，`inspect(identity)` 返回公共 journal 状态。
- 字符串结果继续使用既有 Context Engine 压缩；journal 保留本次实际读取的原文。05 不扩展 ContextRef 检索权限或 Pi 长输出行为，后续按 09/10 验证。
- 只有宿主注册的 code-owned extractor 能申请可信记忆证据。提取器读取副本，证据校验针对不可被其改写的原始输出；错误 scope、原文中不存在的文本均拒绝。executor 返回的 JSON 标记不能直接成为可信记忆。原有 scope/审核规则不变。
- PostToolUse/失败观察者在持久提交后运行，失败保持 fail-open；重复结算不重放观察者或重复提交记忆证据。观察者不是提交屏障。

## 当前支持范围

- 只支持明确声明 path_parameters 的 `operation: read`；写入/Shell 必须由 06 扩展保护后开放。
- 路径使用相对于绑定 cwd 的普通文件路径或绝对路径；`file://`、前导 `~` 在最终解码时拒绝，避免校验方与不同 executor 解释不一致。
- schema 支持显式 JSON 基础类型、properties、required、additionalProperties、items、enum、description/title；根输入必须是封闭 object。尚未支持的 schema 关键字在注册时拒绝，09 对真实 Pi manifest 做映射时不得静默删除约束。
- 05 交付 journal 的状态和原文，不承诺基座会话自动恢复或重跑 uncertain 调用。Pi 的恢复验收属于 12。
- 06 等 04/05 集成并完成保护调用点移交；09 等 05/07/08 集成。不得仅凭本票完成启动尚缺前置的任务。

## 验收方法

完整回归 **4262 passed、1 skipped**；最终候选真实 Application **40/40 PASS**，累计真实模型 Application **82/82 PASS**。新增模块类型检查通过；修改范围内仍有 9 个与基线相同的既有类型错误，无新增。

机器可读命令、结果、环境与证据清单见 [05-validation.json](05-validation.json)。私有 `config/llm.yaml` 已复制到独立 worktree，权限 0600，受 Git ignore 保护，未纳入任何提交。

本票的 Application 验收从公开 `execute_app` 入口进入，使用真实 Hook、权限、journal、文件读取和资源清理。native runtime/executor 使用明确标记的外部测试适配器，不注册成生产 Pi；CI 只替换模型响应，真实 campaign 则使用复制配置中的远程模型。额外运行原有 smol Agent，验证原生基座兼容性。

真实 campaign 为两个模型 profile × 八种场景 × 两次重复，共 32 个 native Application，加 8 个真实 smol Application。场景包括实际读取、Hook 改写、排除路径、Hook 拒绝、变换后无效参数、文件不存在、执行前取消、执行后取消。成功结果必须与未放入提示词的随机文件内容一致；失败/取消由独立 journal 与 executor 次数核对。

## Standards

初审发现路径解释不同和嵌套封闭对象约束遗漏。已分别补充真实文件/符号链接回归、严格参数回归，并修复生产实现。复核 `5b26985d` 确认两项关闭；新增身份、证据及持久化改动未发现标准违约或功能回归。

## Spec

初审发现新建 journal 父目录未同步，并确认了路径解释问题；实现阶段同时修复旧授权在另一工具选择下被复用、证据提取器改写原文的缺口。复核 `5b26985d` 确认持久确认、授权身份、严格解码、证据、重复结算和取消符合本票，未发现剩余规格缺口或范围扩张。

最终两轴未解决问题数均为 0；完整测试及真实模型结果以验证记录为准。

## 分阶段提交

| 提交 | 内容 |
| --- | --- |
| `d3147917` | 共用 Gateway 治理门，持久准备与一次授权消费 |
| `a7ff86d0` | 持久结果、可信证据、重复/晚到结算与取消 |
| `86d3997b` | Application 验收与真实模型 campaign，逻辑名 Hook 映射 |
| `5b26985d` | 审查修复与对应回归测试 |

最终验收记录作为独立文档提交保留；不 squash 上述实现历史。
