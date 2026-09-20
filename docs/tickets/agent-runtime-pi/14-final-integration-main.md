# 14: 清理过渡接口，完成最终验收并合并 main

**What to build:** 将已经交付的功能合成一个经过完整验收的版本，删除不再有消费者的内部过渡形式，并将已验证结果合入 main。

**Blocked by:** 11：双向混合基座协作并复用长期记忆；12：Pi 中断恢复与工具双日志对齐；13：交付干净 Pi-only 与 smol 安装路径

**Status:** draft — 拆分待确认，尚未发布 GitHub；不代表已开工或已完成。

**Required:** Yes — 本票属于最终交付必做项。

**Session:** 协调/集成 session 串行执行

## Scope

contract 阶段与最终整合。只清理、修复集成缺陷和验收，不承担前票遗漏的产品能力；保留旧 YAML 的兼容解释。

## Acceptance criteria

- [ ] 所有必做任务的实际提交已经进入同一 integration 分支，登记每票验证 revision；阻塞关系通过提交证据解除，不以口头完成代替。
- [ ] 确认所有内部旧形式消费者已迁移，再删除 expand 阶段兼容入口；不删除仍需保留的旧 YAML 支持。
- [ ] 最终同一候选通过 A01–A14 和既有必需 CI，使用真实 Pi SDK 与真实工具，只在确定性测试中替换 provider 响应。
- [ ] 从最终候选重建发行物并复跑 Pi-only/smol 干净安装、CLI 失败路径，不能拿 13 的早期构建日志盖章。
- [ ] 实际执行的 live-provider smoke 若失败必须修复或撤回对应支持并复验；缺凭证明确 NOT-RUN，不能宣称已通过真实模型验证。
- [ ] 合并前检查 main 是否前进；有变化先合入 integration、解决冲突并验证最终树，不覆盖他人工作或强推。
- [ ] 将已验证 integration 合入 main，记录 candidate/merge SHA与证据关系；不改或关闭无关 parent issue，保留工作树和证据直到确认无未提交成果。

## Handoff

交付 main 上的版本、验收报告、已支持能力和未运行的真实provider检查；本票完成才代表整个功能交付完成。

实施遵循已生成的 Pi 接入规格、并行开发计划及本轮票据执行索引。只认已集成、已验证的依赖提交；不要自行跳过阻塞任务，也不要从其他 worktree 复制未提交改动。

