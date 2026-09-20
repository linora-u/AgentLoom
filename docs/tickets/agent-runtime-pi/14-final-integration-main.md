# 14: 清理过渡接口，完成最终验收并交付 main Changes

**What to build:** 将已经交付的功能合成一个经过完整验收的版本，删除无消费者的内部过渡形式；按维护者最新要求，将已验证成果交付到主工作区 main 的未提交 Changes，再清理本次开发 worktree。

**Blocked by:** 11：双向混合基座协作并复用长期记忆；12：Pi 中断恢复与工具双日志对齐；13：交付干净 Pi-only 与 smol 安装路径

**Status:** planned — 等待前置任务集成并验证，尚未实施。

**Required:** Yes — 本票属于最终交付必做项。

**Unlocks:** 无；本票为最终交付。

**Session:** 协调/集成 session 串行执行

## Scope

contract 阶段与最终整合。只清理、修复集成缺陷和验收，不承担前票遗漏的产品能力；保留旧 YAML 的兼容解释。

**Edit boundary:** 协调者在全部前置已集成的候选上串行收口；不得在仍有实现 session 写入时交付或删除其 worktree。保留候选提交与验收证据，再将成果展开到 main 的未提交、未暂存 Changes。

## Acceptance criteria

- [ ] 所有必做任务的实际提交已经进入同一 integration 分支，登记每票验证 revision；阻塞关系通过提交证据解除，不以口头完成代替。
- [ ] 确认所有内部旧形式消费者已迁移，再删除 expand 阶段兼容入口；不删除仍需保留的旧 YAML 支持。
- [ ] 复验 [工具归属](tool-ownership.md)：基础工具随各自基座、平台能力保持中立、专业工具按需加载；公共构造、Hook 与生命周期不反向依赖 smol 基础工具，混合实例清理互不影响。
- [ ] 最终同一候选通过 A01–A14 和既有必需 CI，使用真实 Pi SDK 与真实工具，只在确定性测试中替换 provider 响应。
- [ ] 从最终候选重建发行物并复跑 Pi-only/smol 干净安装、CLI 失败路径，不能拿 13 的早期构建日志盖章。
- [ ] 实际执行的 live-provider smoke 若失败必须修复或撤回对应支持并复验；缺凭证明确 NOT-RUN，不能宣称已通过真实模型验证。
- [ ] 交付前检查 main 是否前进及其未提交用户改动；有变化先集成并验证候选，不覆盖已有 Changes，不重置或强推 main。
- [ ] 将已验证内容放到 main 的未提交、未暂存 Changes，记录 candidate SHA、main 基线及交付内容校验；不自动新增 main 提交、不推送或改无关 parent issue。
- [ ] 确认各本次创建的 worktree 提交已集成、未提交成果已保全、证据已保存后删除这些 worktree；保留实现分支与验收日志，不清理其他任务的工作区或用户运行数据。

## Handoff

交付 main Changes、候选版本与验收报告、已支持能力、未运行的真实 provider 检查和 worktree 清理结果；本票完成才代表整个功能交付完成。

实施遵循 Pi 接入规格及本目录最新的 [工具归属](tool-ownership.md)、[执行索引](README.md) 与 [worktree 计划](worktree-plan.md)。只认已集成、已验证的依赖提交；不要自行跳过阻塞任务，也不要从其他 worktree 复制未提交改动。
