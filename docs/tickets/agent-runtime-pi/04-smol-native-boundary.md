# 04: 迁移 smol 专属实现，保持旧应用行为

**What to build:** 现有 smol 应用通过新合同完成任务，其规划、摘要、提示词、Todo 回填和原生结束机制都由 smol adapter 自己持有。

**Blocked by:** 03：汇合验证结果，冻结三路开发的公共基线

**Status:** draft — 拆分待确认，尚未发布 GitHub；不代表已开工或已完成。

**Required:** Yes — 本票属于最终交付必做项。

**Session:** smol session；可与 05、07、08 并行

## Scope

migrate 阶段，限 smol 原生实现及明确移交的旧文件/测试。不得删除 08 仍在使用的兼容工具入口或改写公共记忆语义。

## Acceptance criteria

- [ ] smol 专属规划参数、摘要、模板和错误恢复在适配器内部解释，公共定义没有新增 smol 专属假设。
- [ ] 旧 YAML 的实际效果、原生结构化工具、Stop/Goal 行为及模型协议回放不回归。
- [ ] smol 的消息状态、对话压缩、Todo 回填与同基座恢复保持可用。
- [ ] 公共调用者收到规范结果和 checkpoint envelope，不依赖 smol step/message/result 类型。
- [ ] 定向测试和真实 smol 循环验证通过；尚有兼容入口消费者时保留该入口并记录，不靠提前删测试变绿。

## Handoff

集成后记录剩余内部兼容消费者；该清单供 13 检查无 smol 安装、14 执行最终 contract 删除。

实施遵循已生成的 Pi 接入规格、并行开发计划及本轮票据执行索引。只认已集成、已验证的依赖提交；不要自行跳过阻塞任务，也不要从其他 worktree 复制未提交改动。

