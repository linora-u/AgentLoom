# 11: 双向混合基座协作并复用长期记忆

**What to build:** 同一类 YAML 应用可让 smol Supervisor 调 Pi Worker，也可让 Pi Supervisor 调 smol Worker；前一次任务认可的记忆能在换基座后的任务中使用。

**Blocked by:** 09：Pi 调通原生读取、平台工具与 Goal

**Status:** completed — 在 `codex/pi-t11-mixed` 独立 worktree 实现并验收，源码 `be3c35da`。双向协作、隔离、Goal、ContextRef、认可记忆及公共接线已验证；见 [实施记录](11-implementation.md) 和 [机器可读验收](11-validation.json)。已正式合入 main（`96db1542`），本票 worktree 已删除。

**Required:** Yes — 本票属于最终交付必做项。

**Unlocks:** 14

**Session:** 独立协作/记忆验收 session；可与 10、13 并行

## Scope

主要负责混合应用、公共协作/记忆必要接线和独立验收，不改 Pi bridge、治理管线或记忆产品政策。02/03 已保证 smol 可运行，因此不以 04 的收口排期作为硬阻塞。

**Edit boundary:** 独占本票新增的混合应用与独立验收文件；公共编排、记忆注入与共享测试的必要修改由协调者串行落地。使用现有 Application/Project 记忆规则，不建设第二套记忆系统。

验收使用 09 已开放的只读基础工具与平台 memory/history/Goal 能力：真实读取即可形成可审核证据。不得要求 Pi 执行文件写入、Shell 或恢复，避免暗中依赖与本票并行的 10/12；这些能力在 14 的最终候选上复验组合行为。

## Acceptance criteria

- [x] 两个混合方向都通过 execute_app 调用真实 runtime，不能以 Pi→Pi 或固定 Worker 回答代替。
- [x] 根据 [工具归属](tool-ownership.md)，混合调用的 smol Worker 实际使用自研基础工具，Pi Worker 实际使用官方基础工具；平台记忆/历史/ContextRef 保持共用语义，不能靠两者都调用同一 smol 工具伪装基座替换。
- [x] 并发与重复 Worker 调用保留独立 native session、root/local run、输入输出和工具记录；无实例、Hook 或记忆作用域串用。
- [x] 第一次 smol 运行产生真实工具证据，经现有候选/认可流程形成记忆；第二次 Pi Run 实际读取和使用该记忆，禁止直接向 DB 塞入已认可结果。
- [x] 保持现有 Application/Project scope、root-run snapshot 和审核默认规则，不引入新配置或实时广播。
- [x] 独立 oracle 校验共享产物与 ContextRef 的来源和内容；模型宣称已经记住不算通过。
- [x] 多 Agent 场景复验 Goal root ownership 与实际完成证据，不把 Worker 的 native final 当作根完成。
- [x] 公共 orchestration/self-learning 接线需求由协调者串行落地；本票不修改 10/12 的 Pi 源码。

## Handoff

交付双向混合、隔离、记忆复用及多 Agent Goal 的独立证据；14 在最终候选重跑。

实施遵循 Pi 接入规格及本目录最新的 [工具归属](tool-ownership.md)、[执行索引](README.md) 与 [worktree 计划](worktree-plan.md)。只认已集成、已验证的依赖提交；不要自行跳过阻塞任务，也不要从其他 worktree 复制未提交改动。
