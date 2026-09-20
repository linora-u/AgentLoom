# 09: Pi 调通原生读取、平台工具与 Goal

**What to build:** 一个真实 Pi Application 在运行中调用官方只读工具和 AgentLoom 平台工具，双向通信、权限、结果证据及 Goal/Stop 完成规则全部生效。

**Blocked by:** 05：通过统一治理执行一次原生读取并持久记录；07：从现有应用入口运行一个无工具 Pi Agent；08：让平台工具和 MCP 脱离 smol 构造

**Status:** draft — 拆分待确认，尚未发布 GitHub；不代表已开工或已完成。

**Required:** Yes — 本票属于最终交付必做项。

**Session:** 沿用 Pi session；可与 06 并行

## Scope

本票打通生产完整调用链，只开启已验证的只读和平台能力；文件修改/Shell 在 10 开放。消费 05 的接口，不修改 06 正在开发的治理实现。

## Acceptance criteria

- [ ] 在 execute_app 内实际调用 Pi 官方只读工具及 AgentLoom 工具，工具选择明确、无重复基础工具或隐式同名覆盖。
- [ ] 原始非法参数经过已验证的异步 Hook 入口修正，失败不执行；实际参数和规范 ToolCallRecord 对应。
- [ ] Python 等待 run 期间仍能处理 Pi 的平台工具回调，工具返回后 Pi 继续；并行批次正确绑定 call ID、Hook Run 和实例，不能靠单独 invoke 探针证明无死锁。
- [ ] native prepare/settle 在真实 Pi SDK 上复验 05 的治理样例，成功返回前结果已经提交。
- [ ] Pi 原生 final 不绕过 Goal：未完成时依照平台规则继续，只有根 Supervisor 能完成目标，Stop 阻断和完成证据生效且只有一个根终态。
- [ ] 平台回调期间取消或进程死亡能结束请求并清理；未支持的 write/Shell/resume 配置在执行前明确拒绝。
- [ ] 本票所需公共 registry/readiness/Goal 接线由协调者同次合入，不能留给最终验收补功能。

## Handoff

交付真 Pi 双向工具和 Goal Application 证据；解锁 10、11，并在 04 完成后解锁 13。

实施遵循已生成的 Pi 接入规格、并行开发计划及本轮票据执行索引。只认已集成、已验证的依赖提交；不要自行跳过阻塞任务，也不要从其他 worktree 复制未提交改动。

