# 03: 冻结基座与平台边界，交付可并行的公共基线

**What to build:** 让旧 smol 应用通过按归属装配工具的兼容入口运行，同时验证无工具 native 调用不被 smol 默认工具污染；以此冻结公共合同，让四路 worktree 能独立开发。

**Blocked by:** 01：验证发布版 Pi 的 Hook 与会话恢复接入；02：扩展运行契约，同时保持旧 smol 应用可运行

**Status:** in-progress-unverified — 前置 01/02 已完成；当前 Changes 已有本票实现草案，尚无完整验收及冻结交接记录，不能放行下游。

**Required:** Yes — 本票属于最终交付必做项。

**Unlocks:** 04、05、07、08

**Session:** 协调 session 执行；不可与未完成的 01/02 抢先推进

## Scope

串行前置门禁。按 [工具归属](tool-ownership.md) 分清自研 smol 基础工具、平台工具与可选专业工具；交付公共合同、兼容装配、共享 fixture 和最小依赖拆分。完整工具搬迁属于 04，平台实现解耦属于 08，生产 native 治理属于 05/06。

接手时先核对 [合同草案](03-contracts.md)、[文件交接草案](03-file-ownership.md) 和当前源码/测试，不覆盖或重复实现已有成果。以下条件以实际验证记录勾选；本轮票据修订不替本票作通过声明。

## Acceptance criteria

- [ ] 集成 01/02 的实际提交，原 smol Application 和公共合约测试通过，记录准确的冻结 commit。
- [ ] 工具 manifest 表达归属、提供方和能力；为 smol 基础工具、平台工具、可选专业工具提供独立登记入口，公共 catalog 仅聚合元数据，不急切导入所有实现。
- [ ] 旧 smol 工具名、toolset、固定参数和默认行为保持；历史全局 core_file/core_shell/core_search 不向 native fixture 注入 Python 基础工具。显式无工具配置仍为空，同名冲突和无兼容映射的显式旧工具选择明确拒绝。
- [ ] 提取公共 Hook 已使用的进程环境辅助入口，建立实例/Run 对齐的资源关闭接线，使 04 搬迁后公共 Application 无需直接访问 smol Shell 注册表；通过现有 smol 调用与关闭测试验证，不仅写接口声明。
- [ ] 登记混合模块的逐文件/函数归属，特别是文件保护与读取缓存、Shell 策略与执行器、SkillCatalog 与激活呈现、公共上下文与 smol 提示词。04 与 08 的工具分区可独立修改；06 的迁移后保护调用点需等 04 移交。
- [ ] 固定 runtime options、模型投影、工具 manifest、逻辑工具与 native 名称映射、结果状态和能力声明；模型投影同时覆盖所选 profile 与实例实际生效的请求 headers 策略，保留来源优先级且不把凭证写入公开元数据；后续分支不得各自添加不兼容私有字段。
- [ ] 分别冻结通用完整调用合同与 Pi 双向 JSONL 桥接协议：请求、响应、事件、handshake、run、snapshot、cancel、close 的身份与错误语义明确；未来 CLI/SDK 基座不必照搬 Pi 协议或持有 Python provider binding。
- [ ] 冻结 native prepare/settle：最终参数、执行提供方、授权关联、持久提交确认和不确定结果；ToolCallRecord 保持既有终态，执行 journal 独立表达未结算状态。
- [ ] 通过共享 fixture 表达参数修正、拒绝、写前保护顺序、双日志窗口及取消；不将 fixture 通过算作真实 native 生产管线完成。
- [ ] 为后续无工具 Pi 入口确定注册/readiness 接线方式；未实现能力仍拒绝，smol 保持可用。
- [ ] 登记各任务的模块所有者、smol 旧实现和相关测试移交清单、协议版本及合约变更流程。
- [ ] 核对 01 的 SDK 验证程序在测试目录中的迁移、引用和验证入口，保留原始验收记录；在本票验收 revision 上重放 SDK 验证，不把它放入正式 adapter。已有位置记录见 `03-sdk-relocation.json`。

## Handoff

04、05、07、08 都必须从这个已集成、已验证的 commit 开始；交接实际冻结 SHA、注册分区、资源关闭合同、文件所有权和兼容别名清单。只冻结协议或只有 mock 绿色不算完成；共享入口变更由协调 session 串行处理。

逐项完成 worktree 计划的放行清单后才开启四路。对仍共用的构造入口或测试文件指定协调者，不能只按目录命名就认定 04 与 08 没有冲突。

实施遵循 Pi 接入规格及本目录最新的 [工具归属](tool-ownership.md)、[执行索引](README.md) 与 [worktree 计划](worktree-plan.md)。只认已集成、已验证的依赖提交；不要自行跳过阻塞任务，也不要从其他 worktree 复制未提交改动。
