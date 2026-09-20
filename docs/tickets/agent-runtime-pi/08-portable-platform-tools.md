# 08: 解耦平台工具、可选专业工具与 MCP

**What to build:** 记忆、历史、产物检索、Goal、Worker 调用、Skill 服务及选中的专业/MCP 工具通过中立定义和平台入口执行，不加载自研 smol 基础工具也能使用这些能力。

**Blocked by:** 03：冻结基座与平台边界，交付可并行的公共基线

**Status:** in-progress — 03 已验收；当前在 `codex/pi-t08-platform` 的已有 worktree 开发，见 [当前登记](worktree-plan.md)。本票尚未完成验收，不重复创建实现 session，不能据此放行 09。

**Required:** Yes — 本票属于最终交付必做项。

**Unlocks:** 09

**Session:** 平台工具 session；技术上可与 04、05、07 并行

## Scope

按 [工具归属](tool-ownership.md) 处理平台工具、可选专业工具和 MCP。不是把全部现有工具平台化：基础文件、Shell、grep/glob、Todo 由 04 收进 smol；本票不共同搬迁它们，不改 memory scope、审核政策或 05/06 的治理管线。

**Edit boundary:** 独占平台/专业工具登记、工具入口、MCP 和已划定的 SkillCatalog 文件，见 [03 文件交接](03-file-ownership.md)。Skill parser、公共 prompt、Application 的 LSP 预热、全局 loader/catalog 和 smol 兼容接线交协调者串行修改；不把旧 `file_ops/`、`search/` 整包搬走。

## Acceptance criteria

- [ ] MCP 发现、参数验证、调用和清理通过中立工具合同完成，工具构造不加载 smol adapter。
- [ ] memory/history、ContextRef、Goal 和 Worker 工具可供任意 runtime 使用；只移除 smol 定义依赖，原数据与审核行为不变。
- [ ] 保持已有 YAML 和记忆默认规则，不增加必填记忆配置；基座对话历史、压缩摘要和 Todo 不被当作平台共享记忆或 Goal 状态。
- [ ] 保留新 Worker 实例、运行上下文与 MCP 连接隔离，错误和取消时资源释放。
- [ ] 平台 Goal 的旧 smol decorator 消费迁移完成；Todo 的迁移由 04 负责，Pi 不被额外注入自研 smol Todo。
- [ ] AST/LSP、代码大纲、Markdown 报告及其共享资源作为独立可选工具集合构造和执行，未选择时不加载；构造入口不依赖 smol 类型或基础工具实现。
- [ ] 从实际 Application 入口验证专业工具的按需生命周期：未选择 LSP 时不预热服务，选择后可调用；关闭一个 Worker 不影响其他实例。仅 catalog 的惰性导入测试不算通过。
- [ ] SkillCatalog、scope、提案/审核和中立激活入口归平台；原生加载/提示词呈现留给基座接线。Pi 09 接入时不得同时启用两套自动发现或激活路径。
- [ ] 普通 smol 应用仍能使用这些平台工具；无 smol 环境的工具构造与执行定向验证通过。
- [ ] 只维护 03 划定的平台/专业工具分区；04 维护 smol 分区，公共 catalog 聚合/loader 接线由协调者串行处理。不把 core_file/core_shell/core_search 加入 Pi 的平台默认集合。

## Handoff

向 09 提供可调用的平台/可选工具 manifest、invoke、Skill 接入选择和资源清理，以及逐工具的归属、默认加载、公共保护和行为证据清单。本票用 smol 与 native fixture 验证实际工具行为，真实 Pi 接线与 Goal 完成语义由 09 验收；不以“Pi 尚未完成”为由阻塞自身的中立工具验收。

实施遵循 Pi 接入规格及本目录最新的 [工具归属](tool-ownership.md)、[执行索引](README.md) 与 [worktree 计划](worktree-plan.md)。只认已集成、已验证的依赖提交；不要自行跳过阻塞任务，也不要从其他 worktree 复制未提交改动。
