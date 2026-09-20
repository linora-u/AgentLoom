# 08: 让平台工具和 MCP 脱离 smol 构造

**What to build:** 记忆、历史、产物检索、Goal、Worker 调用与 MCP 工具通过中立定义和平台执行入口工作，不再以加载 smol 工具类型为前提。

**Blocked by:** 03：汇合验证结果，冻结三路开发的公共基线

**Status:** draft — 拆分待确认，尚未发布 GitHub；不代表已开工或已完成。

**Required:** Yes — 本票属于最终交付必做项。

**Session:** 平台工具 session；技术上可与 04、05、07 并行

## Scope

平台工具定义/加载和 MCP 转换；不改 memory scope、审核政策或原生规划，不与 05/06 共同改治理管线。

## Acceptance criteria

- [ ] MCP 发现、参数验证、调用和清理通过中立工具合同完成，工具构造不加载 smol adapter。
- [ ] memory/history、ContextRef、Goal 和 Worker 工具可供任意 runtime 使用；只移除 smol 定义依赖，原数据与审核行为不变。
- [ ] 保留新 Worker 实例、运行上下文与 MCP 连接隔离，错误和取消时资源释放。
- [ ] Goal/Todo 的旧 smol decorator 消费迁移完成；Pi 不被额外注入一套重复的 Agent-local Todo。
- [ ] 普通 smol 应用仍能使用这些平台工具；无 smol 环境的工具构造与执行定向验证通过。
- [ ] 新增 catalog/metadata 使用 03 已固定的合同，必要的公共 catalog 合并由协调者处理，不擅自扩张 05 正在使用的接口。

## Handoff

向 09 提供可调用的中立工具 manifest、平台 invoke 和资源清理；该票不宣称 Pi Goal 完成语义已经成立。

实施遵循已生成的 Pi 接入规格、并行开发计划及本轮票据执行索引。只认已集成、已验证的依赖提交；不要自行跳过阻塞任务，也不要从其他 worktree 复制未提交改动。

