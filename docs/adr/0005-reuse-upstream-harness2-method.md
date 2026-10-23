---
status: accepted
---

# 复用 Harness² 方法核心，保留 AgentLoom 执行适配

2026-10-22，源码核查发现本地方法层重新实现了 Harness² 的递归、角色合同、证据、选择、反思和 Library，并丢失提案理由传递、证据来源区分及任务流经验可见性。按用户要求，固定上游版本纳入项目并直接调用其方法核心，替换本地重复逻辑；AgentLoom 保留 Pi 通道、Application 资源识别与校验、完整业务执行和必要的隔离、停止、恢复、用量及发布保障。维护有限的接入补丁，避免同时维护两套方法状态机。

本决定细化 [ADR-0004](0004-application-scoped-harness-improvement.md)。用户确认本轮同时迁移 `development_score`，作为共用上游方法核心的显式反馈扩展；原生无评分路径保持原有证据边界，两种反馈模式/分区的经验与缓存隔离。新闻保留初筛、精筛、独立校验、冲突处理四类业务职责，允许优化其中的 Agent、子 Agent、Skill、工具及委派关系，不在本轮建设通用业务流程引擎。

方法核心迁移、Pi/Application 适配和新闻接入已经实现；固定上游版本与有限补丁见 [来源记录](../../third_party/harness2/PROVENANCE.md)。[验收报告](../specs/application-harnessing-upstream-reuse-v2-acceptance.md) 分别记录公开受控验证、真实完整新闻日期、跨日期经验输入和已完成日期恢复：第一日期已交付，第二日期的组合 final 未通过业务验收，保留失败且没有发布。迁移实现不等于两个日期均成功或金融效果改善；最终 Standards/Spec 审查、远端交付检查及工作区清理仍须按 [任务图](../specs/application-harnessing-upstream-reuse-v2-tasks.md) 完成。设计依据、扩展合同和验证要求仍见 [复用核查](../research/harness2-reuse-audit-20261022.md)。
