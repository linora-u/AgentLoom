# 09: Pi 调通原生读取、平台工具与 Goal

**What to build:** 一个真实 Pi Application 在运行中调用官方只读工具和 AgentLoom 平台工具，双向通信、权限、结果证据及 Goal/Stop 完成规则全部生效。

**Blocked by:** 05：通过统一治理执行一次原生读取并持久记录；07：从现有应用入口运行一个无工具 Pi Agent；08：解耦平台工具、可选专业工具与 MCP

**Status:** planned — 等待前置任务集成并验证，尚未实施。

**Required:** Yes — 本票属于最终交付必做项。

**Unlocks:** 10、11、13

**Session:** 沿用 Pi session；可与 06 并行

## Scope

本票打通生产完整调用链，只开启已验证的只读和平台能力；文件修改/Shell 在 10 开放。消费 05 的接口，不修改 06 正在开发的治理实现。

**Edit boundary:** 接收 07 的 Pi adapter/bridge 修改权，消费 05 的治理和 08 的中立工具入口。需要更改这些依赖时交对应所有者及协调者处理；公共 Goal/Worker/registry 接线在本票验收前完成。

按 [工具归属](tool-ownership.md) 装配 Pi 官方基础工具、已选平台工具和本阶段允许的只读专业工具。Markdown 等文件修改专业工具需等 10 的保护接线；平台 memory/Goal 更新继续按各自已有规则执行。

## Acceptance criteria

- [ ] 在 execute_app 内实际调用 Pi 官方只读工具及 AgentLoom 工具，工具选择明确、无重复基础工具或隐式同名覆盖。
- [ ] 根据 04/08 的职责边界及 08 的工具交接清单，分别验证官方基础工具、平台工具和可选专业工具的实际提供方；基础工具缺失时明确拒绝，不自动回退 smol。04 的迁移结果不作为本票新增前置。
- [ ] 默认不注入自研 smol 的 read_file/grep/glob/Shell/Todo；至少实际调用一个显式选择的只读专业工具，并验证未选择时不出现。显式旧工具选择无有效映射时 preflight 拒绝。
- [ ] 平台 Skill 目录与 Pi 激活方式只有一条已选接入路径，不重复扫描或注入；不开放未授权的本机资源发现。
- [ ] 原始非法参数经过已验证的异步 Hook 入口修正，失败不执行；实际参数和规范 ToolCallRecord 对应。
- [ ] Python 等待 run 期间仍能处理 Pi 的平台工具回调，工具返回后 Pi 继续；并行批次正确绑定 call ID、Hook Run 和实例，不能靠单独 invoke 探针证明无死锁。
- [ ] native prepare/settle 在真实 Pi SDK 上复验 05 的治理样例，成功返回前结果已经提交。
- [ ] Pi 原生 final 不绕过 Goal：未完成时依照平台规则继续，只有根 Supervisor 能完成目标，Stop 阻断和完成证据生效且只有一个根终态。
- [ ] 平台回调期间取消或进程死亡能结束请求并清理；未支持的 write/Shell/resume 配置在执行前明确拒绝。
- [ ] 本票所需公共 registry/readiness/Goal 接线由协调者同次合入，不能留给最终验收补功能。

## Handoff

交付真 Pi 双向工具和 Goal Application 证据；解锁 10、11，并在 04 完成后解锁 13。

实施遵循 Pi 接入规格及本目录最新的 [工具归属](tool-ownership.md)、[执行索引](README.md) 与 [worktree 计划](worktree-plan.md)。只认已集成、已验证的依赖提交；不要自行跳过阻塞任务，也不要从其他 worktree 复制未提交改动。
