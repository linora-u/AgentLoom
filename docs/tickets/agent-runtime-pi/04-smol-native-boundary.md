# 04: 收拢自研 smol Agent 及其基础工具，保持旧应用行为

**What to build:** 现有 YAML 应用使用迁移后的完整自研 smol Agent 完成文件读写、搜索、Shell 与任务执行；基础工具、规划、摘要、Todo、会话状态和资源释放归属该基座，公共层不再负责其私有执行细节。

**Blocked by:** 03：冻结基座与平台边界，交付可并行的公共基线

**Status:** planned — 等待 03 的实际冻结提交，尚未实施。

**Required:** Yes — 本票属于最终交付必做项。

**Session:** smol session；可与 05、07、08 并行

## Scope

migrate 阶段，按 [工具归属](tool-ownership.md) 收拢自研 Agent 的完整实现，不仅包一层 SDK。迁移基础文件/目录、grep/glob、Shell/后台任务和 Todo 工具及其私有状态；AST/LSP/大纲/Markdown 属于 08 的可选专业工具，不整目录误搬。

03 先处理公共消费者需要的辅助入口。04 保留既有保护行为，登记仍嵌在基础工具中的保护调用点，供随后 06 提取和接线；不改 05 的公共 Gateway，不把公共权限、Goal、长期记忆或 ContextRef 迁成 smol 私有能力。

## Acceptance criteria

- [ ] smol 专属规划参数、摘要、模板和错误恢复在适配器内部解释，公共定义没有新增 smol 专属假设。
- [ ] read/write/edit/list_directory、grep/glob、Shell 与后台任务工具迁到 smol 所属实现，使用 03 的基座 manifest 登记；只选平台工具或 native fixture 时不会导入这些基础工具。
- [ ] smol 的 todo_write、final_answer、Todo 状态与回填由本票统一迁移；08 只负责平台 Goal，不同时改 Todo。尚有消费者的旧内部入口保留明确过渡清单。
- [ ] 私有 Shell 会话、后台任务与运行资源通过 03 的关闭合同回收；公共 Application 不直接访问其注册表，并发 Worker 关闭不影响其他实例。
- [ ] 读取去重、输出解释等基座实现与应由公共层承担的写前保护分别登记；保护效果不丢失，不复制现有公共治理引擎。
- [ ] 旧 YAML 的实际效果、原生结构化工具、Stop/Goal 行为及模型协议回放不回归。
- [ ] smol 的消息状态、对话压缩、Todo 回填与同基座恢复保持可用。
- [ ] 公共调用者收到规范结果和 checkpoint envelope，不依赖 smol step/message/result 类型。
- [ ] 旧 YAML 的 core_file/core_shell/core_search、显式工具名和固定参数保持效果；专业工具/MCP 经现有兼容入口仍可使用，不等待 08 才恢复旧应用。
- [ ] 定向测试和真实 smol 循环验证通过；尚有兼容入口消费者时保留该入口并记录，不靠提前删测试变绿。

## Handoff

交付迁移后的 smol 基础工具行为证据及准确文件移交清单。04 与 05 均完成并集成后才启动 06，06 随后拥有列出的公共保护提取/调用点；04 不再继续改这些文件。剩余内部兼容消费者供 13 检查无 smol 安装、14 执行最终清理。

实施遵循 Pi 接入规格及本目录最新的 [工具归属](tool-ownership.md)、[执行索引](README.md) 与 [worktree 计划](worktree-plan.md)。只认已集成、已验证的依赖提交；不要自行跳过阻塞任务，也不要从其他 worktree 复制未提交改动。
