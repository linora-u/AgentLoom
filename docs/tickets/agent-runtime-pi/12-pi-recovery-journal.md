# 12: Pi 中断恢复与工具双日志对齐

**What to build:** 一个已产生真实工具效果的 Pi 任务中断后可按支持边界恢复，已提交工作不重做，无法确定的副作用不会被自动重复执行。

**Blocked by:** 10：Pi 官方写入和 Shell 产生可保护、可检索的结果

**Status:** done — 从包含 10/11/13 的 main `36a7e6a1` 开始，最终代码
候选 `f5b8a0f` 已完成真实 SDK 恢复、故障窗口、版本/身份和 Application 验收；
见 [实施记录](12-implementation.md) 与 [验证记录](12-validation.json)。

**Required:** Yes — 本票属于最终交付必做项。

**Unlocks:** 14

**Session:** 沿用 Pi session；可与尚未完成的 11、13 并行

## Scope

集中实现同基座恢复、host/native journal 对齐和两个精确 crash window；不重复开发前面已经覆盖的全部取消机制。

**Edit boundary:** 延续 Pi adapter/bridge 的修改权；公共 checkpoint 与 journal 的必要变更交协调者串行处理，并补对共享合同的回归验证。11 不编辑这些恢复文件，13 只消费构建产物。

遵循 [工具归属](tool-ownership.md)：应用 task/Run、已提交 Worker 结果与证据由平台持有，Pi session/payload 由 Pi adapter 解释；不导入 smol 的消息、Todo、Shell 私有状态来实现 Pi 恢复。

## Acceptance criteria

- [x] 正常 Pi session 重启后用同一 task、新 Run 继续，已完成 Worker 结果和已提交工具结果可复用。
- [x] 验证副作用已发生但 host 未提交的窗口：标记 uncertain，保留证据，不伪造未执行或成功，也不自动重试。
- [x] 验证 host 已提交、native toolResult 未持久化的窗口：通过支持的 SDK 补齐已提交结果而不执行工具；用独立文件/计数器核对执行次数。
- [x] 不兼容版本、跨 runtime 或无法安全对齐的状态明确拒绝；不能通过一律拒绝恢复来让正常恢复用例通过。
- [x] 恢复/compaction/等待结果期间可取消，协议损坏与子进程死亡能释放 pending 请求及受管子进程。
- [x] checkpoint、journal 和 native session 的关联、持久化顺序、版本以及终态唯一性可被独立验证。
- [x] 公共 checkpoint 协调的修改由本票独占经协调者接线，05/06 已固定的 journal 合同如需变更须先升级共享合同。

## Handoff

14 已在同一代码候选完成 A01–A14、发行和 live provider 验收；正式 main
交付与 worktree 清理见 [14 实施记录](14-implementation.md)。

实施遵循 Pi 接入规格及本目录最新的 [工具归属](tool-ownership.md)、[执行索引](README.md) 与 [worktree 计划](worktree-plan.md)。只认已集成、已验证的依赖提交；不要自行跳过阻塞任务，也不要从其他 worktree 复制未提交改动。
