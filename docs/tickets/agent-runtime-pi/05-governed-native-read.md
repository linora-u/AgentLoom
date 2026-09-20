# 05: 通过统一治理执行一次原生读取并持久记录

**What to build:** 一次委托给 native executor 的只读调用，完整经过 AgentLoom 参数变换、授权、实际读取、结果持久化和记录，调用者获得与其他工具一致的可追踪结果。

**Blocked by:** 03：冻结基座与平台边界，交付可并行的公共基线

**Status:** planned — 等待前置任务集成并验证，尚未实施。

**Required:** Yes — 本票属于最终交付必做项。

**Session:** 工具治理 session；可与 04、07、08 并行

## Scope

实现生产 Tool Gateway 的 native 准备/结算最小路径；独立验收只替换外部 executor，权限和结算必须使用生产实现。真 Pi 官方工具复验归 09。

按 [工具归属](tool-ownership.md)，本票只拥有公共治理路径，不搬迁或通用化 smol 的 read_file/grep/glob 实现，也不改 04 正在移动的基础工具文件。

## Acceptance criteria

- [ ] 实际工具请求经过现有 Hook、变换后的严格校验、权限检查和稳定 call ID 记录；执行器获得最终参数。
- [ ] 拒绝时 executor 没有执行，blocked 与工具异常区分；错误的 Run、实例、授权或参数摘要不能复用一次批准。
- [ ] 同一个调用在返回成功前获得持久结算确认；执行 journal 与 ToolCallRecord 关联，而不是把 observer 回调当落盘屏障。
- [ ] 真实文件读取结果及其原始/最终参数、来源和 call identity 可被独立验证；伪造、错配的可信记忆证据被拒绝。
- [ ] 重复/晚到的结算或事件不能重复提交结果或修改已终结调用。
- [ ] 旧 Python 工具调用路径及 Hook ADR 行为保持通过；fixture 不得自己实现被测试的权限或持久化逻辑。
- [ ] executor 提供方与平台授权/结算分离；只读 native 测试不通过调用 smol read_file 伪装为基座工具。公共 metadata 的接线由协调者串行集成。

## Handoff

向 06、09 交付稳定的 native 治理接口和可复用行为测试；06 还须等 04 完成工具搬迁，09 还须等 07/08。05 不宣称 Node/Pi 集成已完成。

实施遵循 Pi 接入规格及本目录最新的 [工具归属](tool-ownership.md)、[执行索引](README.md) 与 [worktree 计划](worktree-plan.md)。只认已集成、已验证的依赖提交；不要自行跳过阻塞任务，也不要从其他 worktree 复制未提交改动。
