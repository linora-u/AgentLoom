# 12 实施记录

状态：已完成。基线为 main `36a7e6a1`，最终代码验收候选为
`f5b8a0f`，实现分支为 `codex/pi-t12-recovery`。机器可读结果见
[12-validation.json](12-validation.json)。

## 恢复边界

Pi SDK 继续拥有会话、消息、模型循环和压缩。适配层使用官方 `SessionManager.getHeader/getEntries/open/appendMessage` 保存和恢复原生状态；公共 checkpoint 仅保存带版本的 envelope 与内容摘要。

SDK 的 extension `message_end` 发生在原生消息入库之前。因此该阶段只做参数转换和调用登记，平台工具的实际调用已移到 SDK tool execute。工具派发前、模型请求前、成功结束时保存会话；host 将原生 artifact 和公共 checkpoint 持久化后才确认。公共 sink 的写入失败必须传播，不能仅报告 checkpoint 降级后继续副作用。

恢复先校验 Application/task、协议、bridge、SDK、状态 schema、原生
session/parent/call 身份、工具映射与参数，以及原始 Run 的 host journal。
结果已提交但原生 toolResult 缺失时，通过 SDK appendMessage 补齐，不调用
执行器。executing/uncertain 一律阻止自动恢复；只有 journal 明确证明尚未派发
的 prepared/authorized/cancelled 调用可以补入 not_executed 错误。所有调用
校验完成后才生成 SDK 恢复计划。

旧调用保留旧 Run 和 instance 身份，新调用使用新 Run 身份。provider call ID
只在完整 NativeCallIdentity 中解释，同一 ID 可在后续 native parent 合法复用。
读取产生的写入前置证据只有在 host 独立复核文件版本未变时才恢复。平台工具
另存调用收据；Worker 已完成、但 adapter receipt 尚未提交的崩溃窗口，通过
精确匹配原 Run、Worker、格式化输入和输入摘要来补齐 receipt，不重新执行
Worker。普通平台工具没有第二份独立完成证据时仍保持 uncertain。

## 契约与压缩

Pi JSONL v2 增补 `session_checkpoint` 回调和可选 checkpoint_enabled 字段；common native-tool 契约仍为 v1。Pi bridge 版本为 1，状态 schema 为 2，绑定官方 SDK 0.79.4 / 原生会话格式 3，不保证跨 bridge、状态 schema 或 SDK 的状态迁移。

默认仍关闭自动压缩，可用 Pi 专属 runtime_options.compaction 启用官方算法。压缩前后落 checkpoint，取消同时调用 SDK abortCompaction 与 abort。

## 最终验证

最终代码候选通过 10 文件 Pyright（0 errors / 0 warnings）、官方 SDK 安装和
TypeScript 构建。确定性测试使用真实 SDK、工具和 Application，仅 HTTP 模型
响应使用 fixture：

- Application 恢复覆盖新 Run 恢复已提交 read；真实进程在派发前、执行后
  提交前、host 提交后被杀死；独立文件计数证明未重跑副作用；不兼容版本、
  跨 runtime/task、摘要/父节点/参数/结果损坏与 symlink 拒绝；旧 read 证据
  只允许写入未变化的文件；Pi/smol Worker 结果复用；checkpoint 存储失败阻止
  副作用。
- 3 项原生压缩与恢复中取消：真实 SDK compaction HTTP 等待期间 SIGINT；从 assistant 尾部恢复并再次自动压缩；恢复模型等待期间 SIGINT/SIGKILL；检查唯一终态及真实 SDK PID 退出。
- 四类缺字段/错误类型调用验证首次 SDK toolResult 与持久拒绝回执一致；空
  Worker 结果与正常 Gateway 使用同一 canonical 输出。
- 恢复、治理、进程、写入与 Shell 组合专项 118 项通过；最终边界专项 30 项
  通过。最终同一候选的完整测试为 4557 passed、1 skipped。
- 非 editable wheel 的真实 provider 恢复验证覆盖 Pi Chat 与 Pi Responses：
  read 已由 host 提交但 SDK 未收到结果时杀死 bridge，新 Run 恢复后继续
  write/bash，原 read 未重做。

首次两轴审查发现恢复直接调用底层 Agent 绕过完整会话生命周期、跨 Run 重用 provider call ID 被误拒，以及 journal 恢复解释重复和异常终态缺口。已改为 SDK `sendCustomMessage(..., {triggerTurn: true})`，使用不可见的恢复控制消息进入原生完整循环；持久调用按 native parent + provider ID 定位，公共 journal 独立校验原始 Run 凭据。损坏存储统一映射为恢复失败并保留异常链。

多轮独立 Standards/Spec 复审发现并修复：非法参数拒绝正文漂移、不安全工具
只串行执行器而未串行完整 Hook 生命周期、裸 call ID、缺少 bridge 版本、
Worker 完成与 adapter receipt 之间的崩溃窗口，以及空 Worker 结果的投影差异。
最终复审均为 0 个实现阻塞；保留的大型 bridge/runtime coordinator 拆分建议是
后续维护性工作，不是本票缺陷。

完整命令、版本、失败历史和外部证据路径见
[12-validation.json](12-validation.json) 与
[14-validation.json](14-validation.json)。
