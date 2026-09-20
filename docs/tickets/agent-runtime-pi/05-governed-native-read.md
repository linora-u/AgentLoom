# 05: 通过统一治理执行一次原生读取并持久记录

**What to build:** 一次委托给 native executor 的只读调用，完整经过 AgentLoom 参数变换、授权、实际读取、结果持久化和记录，调用者获得与其他工具一致的可追踪结果。

**Blocked by:** 03：汇合验证结果，冻结三路开发的公共基线

**Status:** draft — 拆分待确认，尚未发布 GitHub；不代表已开工或已完成。

**Required:** Yes — 本票属于最终交付必做项。

**Session:** 工具治理 session；可与 04、07、08 并行

## Scope

实现生产 Tool Gateway 的 native 准备/结算最小路径；独立验收只替换外部 executor，权限和结算必须使用生产实现。真 Pi 官方工具复验归 09。

## Acceptance criteria

- [ ] 实际工具请求经过现有 Hook、变换后的严格校验、权限检查和稳定 call ID 记录；执行器获得最终参数。
- [ ] 拒绝时 executor 没有执行，blocked 与工具异常区分；错误的 Run、实例、授权或参数摘要不能复用一次批准。
- [ ] 同一个调用在返回成功前获得持久结算确认；执行 journal 与 ToolCallRecord 关联，而不是把 observer 回调当落盘屏障。
- [ ] 真实文件读取结果及其原始/最终参数、来源和 call identity 可被独立验证；伪造、错配的可信记忆证据被拒绝。
- [ ] 重复/晚到的结算或事件不能重复提交结果或修改已终结调用。
- [ ] 旧 Python 工具调用路径及 Hook ADR 行为保持通过；fixture 不得自己实现被测试的权限或持久化逻辑。

## Handoff

向 06、09 交付稳定的 native 治理接口和可复用行为测试；05 不宣称 Node/Pi 集成已完成。

实施遵循已生成的 Pi 接入规格、并行开发计划及本轮票据执行索引。只认已集成、已验证的依赖提交；不要自行跳过阻塞任务，也不要从其他 worktree 复制未提交改动。

