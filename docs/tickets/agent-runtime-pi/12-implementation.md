# 12 实施记录

状态：恢复功能已实现并通过针对性验证，等待 14 在同一最终候选完成全量及发行验收。基线为 main `36a7e6a14c75e72f4420851e6120dac38a90269d`，实现分支 `codex/pi-t12-recovery`。

## 恢复边界

Pi SDK 继续拥有会话、消息、模型循环和压缩。适配层使用官方 `SessionManager.getHeader/getEntries/open/appendMessage` 保存和恢复原生状态；公共 checkpoint 仅保存带版本的 envelope 与内容摘要。

SDK 的 extension `message_end` 发生在原生消息入库之前。因此该阶段只做参数转换和调用登记，平台工具的实际调用已移到 SDK tool execute。工具派发前、模型请求前、成功结束时保存会话；host 将原生 artifact 和公共 checkpoint 持久化后才确认。公共 sink 的写入失败必须传播，不能仅报告 checkpoint 降级后继续副作用。

恢复先校验 Application/task、SDK/状态版本、原生 session/parent/call 身份、工具映射与参数，以及原始 Run 的 host journal。结果已提交但原生 toolResult 缺失时，通过 SDK appendMessage 补齐，不调用执行器。executing/uncertain 一律阻止自动恢复；只有 journal 明确证明尚未派发的 prepared/authorized/cancelled 调用可以补入 not_executed 错误。所有调用校验完成后才生成 SDK 恢复计划。

旧调用保留旧 Run 和 instance 身份，新调用使用新 Run 身份。读取产生的写入前置证据只有在 host 独立复核文件版本未变时才恢复。平台工具另存调用收据，已提交 Worker 输出直接复用。

## 契约与压缩

Pi JSONL v2 增补 `session_checkpoint` 回调和可选 checkpoint_enabled 字段；common native-tool 契约仍为 v1。Pi 状态 schema 为 1，绑定官方 SDK 0.79.4 / 原生会话格式 3，不保证跨 SDK 状态迁移。

默认仍关闭自动压缩，可用 Pi 专属 runtime_options.compaction 启用官方算法。压缩前后落 checkpoint，取消同时调用 SDK abortCompaction 与 abort。

## 当前验证范围

Python 类型检查（10 文件，0 errors / 0 warnings）、官方 SDK 安装和 TypeScript 构建通过。按维护者要求先开发后测试，已完成 23 项真实 SDK 功能测试，仅 HTTP 模型响应使用 fixture：

- 20 项 Application 恢复：新 Run 恢复已提交 read；真实进程在派发前、执行后提交前、host 提交后被杀死；独立文件计数证明未重跑副作用；不兼容版本、跨 runtime/task、摘要/父节点/参数/结果损坏与 symlink 拒绝；旧 read 证据只允许写入未变化的文件；Pi/smol Worker 结果复用；checkpoint 存储失败阻止副作用。
- 3 项原生压缩与恢复中取消：真实 SDK compaction HTTP 等待期间 SIGINT；从 assistant 尾部恢复并再次自动压缩；恢复模型等待期间 SIGINT/SIGKILL；检查唯一终态及真实 SDK PID 退出。

首次两轴审查发现恢复直接调用底层 Agent 绕过完整会话生命周期、跨 Run 重用 provider call ID 被误拒，以及 journal 恢复解释重复和异常终态缺口。已改为 SDK `sendCustomMessage(..., {triggerTurn: true})`，使用不可见的恢复控制消息进入原生完整循环；持久调用按 native parent + provider ID 定位，公共 journal 独立校验原始 Run 凭据。损坏存储统一映射为恢复失败并保留异常链。

测试开发中的失败保留在外部日志：最初 pytest 入口缺 tests namespace，改用 python -m；测试 checkpoint 开关最初误放 Agent YAML，改为公共 system 配置；随后发现 transport 回调白名单漏 session_checkpoint，已修复。上述 23 项是修复后结果，不代表最终 A01–A14 / 发行 / 全量测试已通过。

后续在最终候选执行恢复/故障窗口测试、既有 CI、干净安装与真实 provider 验证；记录结果后合入 main 并清理本次 worktree。
