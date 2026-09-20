# 09 实施与验收记录

状态：开发中。基线 `7965cc00`，分支 `codex/pi-t09-tools`。

按票据约定的公开边界做纵向验证：`execute_app` + 已安装的官方 Pi SDK + HTTP provider；再使用私有配置中的真实模型。HTTP fixture 用于强制产生非法参数、并行批次、重试及中断，不能替代真实模型 Application 验证。

实施顺序：

1. 显式选择官方只读工具，沿已有 prepare/settle 契约完成一次 Application 调用，验证持久回执。
2. 接入平台/可选专业工具的双向回调；复用 Gateway，使用 01 已验证的异步 `message_end` 变换入口，核对最终参数与调用身份。
3. 接通并行 Worker、Goal、Stop、Skill 与生命周期，验证拒绝未开放的能力。
4. 多组真实模型 Application、故障回归、类型检查、完整测试、代码审查；记录每条验收证据后，保留阶段提交合入 main，删除本次 worktree。

SDK 是安装依赖。使用 `uv run --locked loom install-runtime pi`，锁定 0.79.4；不提交 SDK 源码、node_modules 或构建输出。

06 拥有的 Native Host / Gateway 治理实现不在本分支修改范围内。公共选择、registry/readiness 与 Goal/Worker 接线在本票完成。
