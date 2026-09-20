# 10: Pi 官方写入和 Shell 产生可保护、可检索的结果

**What to build:** Pi 使用官方编辑、写入和 Shell 实现完成受控任务，AgentLoom 保留授权、文件历史、完整产物和可验证的记忆证据。

**Blocked by:** 06：补齐文件修改和 Shell 的治理与保护；09：Pi 调通原生读取、平台工具与 Goal

**Status:** done — 独立 worktree 的候选 `ef15789b`（执行源码同 `9efc5303`）完成 4464 passed、1 skipped 的全量检查；1 项已有记忆进程就绪超时，所在模块原样复验 7/7 通过；最终真实模型 30/30；11/13 已纳入。正式 main 交付与清理见 [实施记录](10-implementation.md)，逐次证据见 [验证记录](10-validation.json)。

**Required:** Yes — 本票属于最终交付必做项。

**Unlocks:** 12

**Session:** 沿用 Pi session；可与 11、13 并行

## Scope

把 06 的治理接入实际官方工具，不重写编辑器/压缩算法，不改新的长期记忆策略。

**Edit boundary:** 延续 Pi adapter/bridge 与 Node lock 的唯一修改权；公共治理调用 06 已交付实现，平台工具调用 08 的入口。与 11/13 并行时不编辑其应用验收或 Python packaging 文件；构建产物合同变化通知 13。

遵循 [工具归属](tool-ownership.md)：Pi 的基础写入/Shell 使用官方实现，自研 smol 基础工具留在 smol。可选专业写入工具只在显式选择且同样满足平台保护时开放。

## Acceptance criteria

- [x] 通过 execute_app 实际执行官方 read/edit/write/bash 中启用的能力；同名 wrapper 明确委托官方实现。
- [x] 工具 manifest/调用证据能区分 Pi 基础工具与平台/专业工具；不把 smol write_file/shell_tool 作为 Pi 原生工具的执行后门，未选择的 Markdown 等专业写入工具不自动注入。
- [x] 实际 Pi 调用复验拒绝写入、Shell 命令约束、变换后的最终参数和写前备份，独立检查禁止副作用未发生。
- [x] 模型可见截断不丢失本次受限查询的原始产物；ContextRef 检索、来源和 coverage 与第一次执行一致。
- [x] 成功证据绑定真正的工具输入、输出和 Run/call identity；失败/blocked/截断文本不能伪造完整成功证据。
- [x] 原生工具运行中的取消和管理范围内的 Shell 子进程清理经过测试；模糊结果不得被报告成功。
- [x] 只声明逐工具验证过的 native 映射；缺失捕获或政策执行能力的映射明确拒绝，不能默认全部支持。

## Handoff

向 12 提供 `tests/pi_test/test_write_shell_faults.py` 的实际副作用、捕获损坏与 journal 故障入口；执行中/uncertain 不自动重跑，已持久 commit 的效果不因丢确认消失。12 从包含 `refs/agentloom/ticket10-frozen` 的已集成提交启动；14 在 12 完成后重建最终发行物。Pi snapshot/resume 本票仍未开放。

实施遵循 Pi 接入规格及本目录最新的 [工具归属](tool-ownership.md)、[执行索引](README.md) 与 [worktree 计划](worktree-plan.md)。只认已集成、已验证的依赖提交；不要自行跳过阻塞任务，也不要从其他 worktree 复制未提交改动。
