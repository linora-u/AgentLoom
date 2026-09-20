# 11: 双向混合基座协作并复用长期记忆

**What to build:** 同一类 YAML 应用可让 smol Supervisor 调 Pi Worker，也可让 Pi Supervisor 调 smol Worker；前一次任务认可的记忆能在换基座后的任务中使用。

**Blocked by:** 09：Pi 调通原生读取、平台工具与 Goal

**Status:** draft — 拆分待确认，尚未发布 GitHub；不代表已开工或已完成。

**Required:** Yes — 本票属于最终交付必做项。

**Session:** 独立协作/记忆验收 session；可与 10、13 并行

## Scope

主要负责混合应用、公共协作/记忆必要接线和独立验收，不改 Pi bridge、治理管线或记忆产品政策。02/03 已保证 smol 可运行，因此不以 04 的收口排期作为硬阻塞。

## Acceptance criteria

- [ ] 两个混合方向都通过 execute_app 调用真实 runtime，不能以 Pi→Pi 或固定 Worker 回答代替。
- [ ] 并发与重复 Worker 调用保留独立 native session、root/local run、输入输出和工具记录；无实例、Hook 或记忆作用域串用。
- [ ] 第一次 smol 运行产生真实工具证据，经现有候选/认可流程形成记忆；第二次 Pi Run 实际读取和使用该记忆，禁止直接向 DB 塞入已认可结果。
- [ ] 保持现有 Application/Project scope、root-run snapshot 和审核默认规则，不引入新配置或实时广播。
- [ ] 独立 oracle 校验共享产物与 ContextRef 的来源和内容；模型宣称已经记住不算通过。
- [ ] 多 Agent 场景复验 Goal root ownership 与实际完成证据，不把 Worker 的 native final 当作根完成。
- [ ] 公共 orchestration/self-learning 接线需求由协调者串行落地；本票不修改 10/12 的 Pi 源码。

## Handoff

交付双向混合、隔离、记忆复用及多 Agent Goal 的独立证据；14 在最终候选重跑。

实施遵循已生成的 Pi 接入规格、并行开发计划及本轮票据执行索引。只认已集成、已验证的依赖提交；不要自行跳过阻塞任务，也不要从其他 worktree 复制未提交改动。

