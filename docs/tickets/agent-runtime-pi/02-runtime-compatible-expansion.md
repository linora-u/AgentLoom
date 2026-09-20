# 02: 扩展运行契约，同时保持旧 smol 应用可运行

**What to build:** 让现有 YAML 应用继续完成真实 smol 调用，同时允许公共构造使用中立模型选择和后端选项，为原生 provider 基座留出合法入口。

**Blocked by:** None (can start immediately)

**Status:** draft — 拆分待确认，尚未发布 GitHub；不代表已开工或已完成。

**Required:** Yes — 本票属于最终交付必做项。

**Session:** 公共契约 session；可与 01 同时开始

## Scope

这是 expand 阶段的必要前置重构。保留暂时的内部兼容入口；不实现 Pi、不改变长期记忆政策，也不提前删除其他调用者还在使用的形式。

## Acceptance criteria

- [ ] 旧 smol YAML 和原启动入口继续运行，smol 模型协议、Goal、工具结果和已提交 Worker 复用行为保持。
- [ ] 公共构造不再要求所有 runtime 都持有 Python ModelTurnBinding；现有 smol 路径仍获得正确绑定。
- [ ] smol 专属选项保留原意并记录配置来源；显式冲突报错，历史默认不会被错误解释成 Pi 的要求。
- [ ] Worker 是否创建新实例不再依赖有没有 model binding；并发调用具有独立实例和 Hook Run。
- [ ] 公共层不向所有后端强行注入 smol Todo 或 final_answer；smol 在自己的适配入口保留原行为。
- [ ] 定义无工具、无 Goal 的合法最小调用形态，requirements 从实际工具和功能选择推导；缺少能力时明确拒绝，不把所有 runtime 无条件判为需要工具。
- [ ] 更新旧 terminal/prompt 接线及其现有测试，保留绿色兼容提交；这里只用 native fixture 验公共构造，不宣称 Pi 已接入。

## Handoff

向 03 提供可运行的兼容提交、暂存旧形式清单和接口迁移说明；最终删除这些内部旧形式由 14 在所有调用者迁移后完成。

实施遵循已生成的 Pi 接入规格、并行开发计划及本轮票据执行索引。只认已集成、已验证的依赖提交；不要自行跳过阻塞任务，也不要从其他 worktree 复制未提交改动。

