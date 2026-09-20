# 01: 验证发布版 Pi 的 Hook 与会话恢复接入

**What to build:** 用锁定的实际发布 Pi SDK 跑通一个小型真实 AgentSession，证明 AgentLoom 需要的异步参数变换、执行前阻断和已提交结果续接存在可用入口，为后续契约提供实证。

**Blocked by:** None (can start immediately)

**Status:** completed — 已集成并经协调者独立复验；见 [集成验收](01-02-integration.md)。未注册生产 Pi。

**Required:** Yes — 本票属于最终交付必做项。

**Session:** 独立验证 session；可与 02 同时开始

## Scope

只交付可重复运行的隔离验证程序、版本锁定材料和结论；不改公共运行代码，不注册生产 Pi，不修改本地上游参考仓库。

## Acceptance criteria

- [x] 记录实际 npm 分发物、完整依赖锁、integrity、Node 与 Pi 版本；验证程序不依赖开发机的上游源码 checkout。
- [x] 保持给模型的严格工具 schema，验证原始非法参数经异步 Python Hook 修正后，实际执行的是合法的最终参数；不能只改原本合法的参数。
- [x] 验证 Hook 失败或拒绝后没有工具副作用、Hook 不重复运行，以及多工具批次中的顺序和身份关联。
- [x] 通过公开 SDK 使用原生 provider 测试入口、官方工具和自动会话压缩；只替换模型响应，不以假的 Agent 循环代替。
- [x] 实际制造 host 结果已提交、Pi toolResult 尚未持久化的窗口，证明能用已提交结果续接且不重跑工具；不支持的状态明确列出，不能以一律拒绝代替正常恢复。
- [x] 输出可采用的接入入口与限制；任一必要合同无法实现时记录阻塞，不得把源码推测或跳过测试报告为通过。

## Handoff

把验证程序、锁定版本、结果和失败边界交给 03；必要合同未通过时，03 不得冻结后续实现接口。

实施遵循已生成的 Pi 接入规格、并行开发计划及本轮票据执行索引。只认已集成、已验证的依赖提交；不要自行跳过阻塞任务，也不要从其他 worktree 复制未提交改动。

## Implementation evidence

- 隔离验证程序与接入边界：[README](../../../experiments/pi-sdk-compatibility/README.md)。
- 精确版本、完整依赖锁与 tarball integrity 均随实验提交；Node/平台/Python 版本和源码 SHA-256 见 [验证报告](../../../experiments/pi-sdk-compatibility/RESULT.json)。
- `npm ci --ignore-scripts --no-audit --no-fund` 可复现安装；`npm run verify` 执行类型检查及 13 个真实 SDK 用例。
- 已证明异步非法参数修正、独立 fail-closed 执行门、原生 provider/官方工具/自动压缩、真实 SIGKILL 提交窗口和不重复执行的单调用恢复。恢复范围与其余未覆盖状态在 README 明确列出，后续 12 仍负责生产恢复矩阵。
- 真实线上 provider：NOT-RUN；生产工具治理与 Application 接入不属于本票完成声明。
