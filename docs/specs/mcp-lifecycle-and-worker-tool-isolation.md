# AgentLoom MCP 生命周期与 Worker 工具依赖隔离

规格日期：2026-09-17。研究基线：`ca27966dfab4f4e2889b4cb03472376a06566185`。

状态：规格已编写，业务实现、完整单元测试与真实 Application 验收待执行。现有代码核对和小范围缺陷复现不构成实现验收通过。

本规格承接已经讨论的两个运行时问题，可作为一个 worktree 的开发任务，按 Worker 工具缓存、MCP 生命周期分别提交。自有源码目录与 Application 定义统一由另一条开发线负责；两者的协作关系见 Further Notes。

## Problem Statement

用户需要同一套多 Agent 系统能够稳定地重复运行和并行调用 Worker：每次调用使用本次指定的配置与依赖，任务结束或失败后释放自己取得的 MCP 资源，其他正在运行的 Agent 不受影响。

当前 MCP 并非完全没有清理：Application 收尾已经尝试断开 Supervisor 保存的 manager。但是 Worker 也会创建 manager，其调用结束时缺少对等的清理；工具刷新可能覆盖旧 manager 引用；客户端或 manager 部分构建成功后发生异常时，资源可能尚未交给外层清理逻辑。仅在 Application 最后读取 Supervisor 的一个字段，不能覆盖完整的资源取得与释放过程。

MCP 客户端底层会管理会话、线程、事件循环以及可能由本次连接启动的 stdio 子进程。资源遗留可能造成重复运行时积累连接；错误的统一清理又可能中断其他 Worker 或外部调用方拥有的资源。

当前文件路径形式的 Worker 工厂按 resolved path 缓存 callable。该 callable 已捕获模型、logger、执行环境、Agent 类、Worker 定义及相关元数据。再次使用同一路径时，工厂直接返回旧 callable，跳过新的参数和文件读取。同路径定义更新或不同调用方传入不同依赖时，可能继续使用旧内容。

已有的 fresh Worker 机制解决了每次执行的 Agent 实例隔离，但不能修复从旧闭包中取得错误依赖的问题。普通 Supervisor 加载 Worker 时使用的字典入口不经过这层文件缓存，不能将风险扩大为所有 Worker 均受影响。Repo Map 等使用文件路径工厂的场景需要直接覆盖。

用户要求有效的单元测试以及真实复杂 Application 功能测试。仅验证工具存在、调用 mock 的 disconnect，或每次重启进程后跑一个成功任务，都不足以证明这些问题已经解决。

## Solution

沿用 AgentInvocation 作为一次 Agent 调用的执行与资源所有权 module，在构建任何 MCP 工具之前建立调用资源作用域。资源创建后立即登记清理责任，部分取得失败在取得层回收，完整调用结束在调用层统一收尾。Supervisor 和 Worker 各自释放自己的资源，外部借入资源保留原所有者。

取消 Worker 工厂按文件路径缓存带运行依赖的 callable。每次工厂调用通过现有定义读取 interface 取得当前定义，并绑定本次参数；已创建工具使用其创建时确定的 Worker 定义和依赖，每次执行继续产生 fresh Worker。先测量取消缓存后的构建开销，有明确性能证据后再单独考虑不含运行依赖的解析结果缓存。

主要测试 seam 沿用用户已经确认的 execute_app 及 loom run CLI adapter。工厂依赖选择和 MCP 部分失败通过各自现有 interface 做确定性回归；实际会话和进程释放通过受控真实 MCP transport 验证；最终以真实模型、复杂多 Worker 任务、独立产物验证和 Run 证据验收。

## User Stories

1. As an Application operator, I want each Agent invocation to own the MCP resources it creates, so that cleanup has one identifiable responsibility.
2. As an Application operator, I want Supervisor connections released after its invocation, so that repeated runs do not accumulate resources.
3. As an Application operator, I want Worker connections released after that Worker finishes, so that cleanup does not depend on the root Application ending.
4. As an Application operator, I want resources registered before connection setup begins, so that partial construction failures remain recoverable.
5. As an Application operator, I want a client that fails during tool discovery to clean up its partial connection, so that an incomplete constructor does not leak resources.
6. As an Application operator, I want tool adaptation failures to release acquired resources, so that a missing tool list does not hide live connections.
7. As an Application operator, I want subsequent Agent construction failures to release previously acquired MCP resources, so that cleanup covers the entire build process.
8. As an Application operator, I want ordinary connection failures to retain the supported warning-and-skip behavior, so that one unavailable optional server does not prevent useful execution.
9. As an Application operator, I want interruption to propagate after cleanup, so that cancellation is not misreported as an ordinary unavailable server.
10. As an Application operator, I want repeated cleanup to be safe, so that nested error handling does not disconnect a resource twice.
11. As an Application operator, I want cleanup to attempt all owned resources despite one failure, so that later connections are not abandoned.
12. As an Application operator, I want the original execution failure preserved alongside cleanup diagnostics, so that the cause of a failed task remains visible.
13. As an Application operator, I want incomplete cleanup after otherwise successful execution reported honestly, so that the Run is not falsely reported as fully successful.
14. As a Worker author, I want one Worker finishing to leave sibling connections usable, so that parallel work can continue.
15. As a Worker author, I want nested Workers to own their connections independently, so that parent and child cleanup cannot affect each other accidentally.
16. As an integration author, I want borrowed resources left open by the borrower, so that their original owner controls their lifetime.
17. As an Application operator, I want workflow stages and Goal continuations within one invocation to retain usable connections, so that intermediate stages do not close resources prematurely.
18. As an Application operator, I want checkpoint recovery to establish fresh connections, so that it does not depend on serialized live clients.
19. As an Application operator, I want cached Supervisor runtimes to use current invocation tools, so that they cannot call closed connections from an earlier run.
20. As an integration author, I want low-level tool-loading interfaces to express resource ownership, so that obtaining a tool list cannot silently discard its owner.
21. As an Application author, I want metadata inspection to remain free of MCP connections, so that checking a definition does not execute it.
22. As an Application author, I want a new file-based Worker factory call to read the current definition, so that edits become effective predictably.
23. As an Application author, I want invalid or removed definitions rejected on the next factory call, so that stale tools cannot hide configuration errors.
24. As an integration author, I want each factory call to honor its supplied model, so that one caller cannot inherit another caller's model by sharing a path.
25. As an integration author, I want each factory call to honor its logger and execution environment, so that logs and side effects belong to the correct execution.
26. As an integration author, I want supported Agent classes and construction options honored per factory call, so that path reuse does not bypass customization.
27. As a Worker author, I want updated schemas and concurrency settings reflected in newly created tools, so that invocation metadata matches the selected definition.
28. As a Worker author, I want an already created tool to retain its definition snapshot, so that editing a file does not change an in-progress task.
29. As a Worker author, I want caller-owned configuration dictionaries protected from mutation, so that creating or running a tool cannot change later invocations.
30. As a Worker author, I want each execution of one tool to create a fresh Worker, so that memory and execution state do not cross between calls.
31. As a Worker author, I want batch invocations to retain their existing concurrency and result behavior, so that dependency isolation does not break parallel workflows.
32. As an Application author, I want explicitly shared models to remain shareable, so that removing the callable cache does not force unnecessary model construction.
33. As a Repo Map user, I want repeated analysis in one process to use the correct Worker definition and runtime context, so that one analysis cannot reuse stale dependencies from another.
34. As a maintainer, I want cache regression tests to enter through the real file-based factory, so that tests exercise the faulty decision point.
35. As a maintainer, I want repeat-run tests inside one Python process, so that process restart cannot conceal stale global state.
36. As a maintainer, I want controlled failures at each acquisition stage, so that resource recovery is verified beyond the happy path.
37. As a maintainer, I want real local MCP connections exercised, so that successful mock cleanup cannot conceal surviving threads or processes.
38. As a maintainer, I want genuine model and Worker execution in complex Applications, so that unit success is backed by end-to-end behavior.
39. As a maintainer, I want both native tool calling and CodeAct validated, so that cached executor bindings do not make one mode silently regress.
40. As a maintainer, I want artifacts checked against an independent oracle, so that an Agent's success statement cannot substitute for correct results.
41. As a maintainer, I want bounded execution and complete failure evidence, so that functional validation is reproducible and cannot silently retry until green.
42. As a maintainer, I want worktree-local environments and runtime assets, so that concurrent development does not contaminate validation.
43. As a maintainer, I want behavioral fixes separated from source relocation, so that each change can be reviewed and integrated independently.
44. As a maintainer, I want the combined candidate tested after integration, so that independently passing branches do not conceal integration regressions.

## Implementation Decisions

### 范围与既有契约

1. 本规格只处理 MCP 资源所有权和 Worker callable 的依赖隔离。沿用当前 smolagents 版本与 Agent-as-Tool 执行方式，不重新定义 Application 配置优先级。
2. 沿用 Application、Supervisor、Worker、AgentInvocation、Run、Hook Plan、Hook Run、Skill 和 Goal 领域语言。既有 Hook Runtime ADR 继续生效：每次调用拥有独立 Hook Run；变换、严格解码、CoreToolGuard、最终输入记录、工具副作用的顺序不变。
3. 资源取得属于执行阶段。Application 定义检查和 Studio 只读查询不能连接 MCP 或执行 Hook。
4. 在 AgentInvocation 这个既有 module 内集中构建期间的资源登记、清理和错误处理，提高其 interface 的 depth 与维护 locality。MCP client/manager 继续作为具体 adapter，不新增通用资源框架或进程全局 MCP owner。

### MCP 所有权与资源取得

5. AgentInvocation 在构建运行 Agent 和工具之前建立资源作用域，覆盖本次调用的全部工作流阶段、Goal 段以及相关收尾。资源清理失败时，也必须恢复调用的上下文绑定。
6. 优先使用标准库 ExitStack 或已有上下文管理能力，通过既有构建链传递所有权。缓存 Agent 上的单个可覆盖字段不能充当完整资源记录；任何内部绑定都必须按调用隔离，不能退回进程全局 owner。
7. manager 创建后先登记清理，再开始连接。运行 Agent 尚未返回就构建失败时，已取得资源也要释放。同一作用域内重复构建工具，不能通过覆盖字段丢失之前的登记。
8. client adapter 负责自身部分启动失败的恢复。MCPAdapt 进入时可能先启动线程/会话，再在工具发现或适配时失败；构造函数抛异常时，外层拿不到返回值，不能把清理责任留给外层。恢复过程要区分资源是否实际启动，尝试清理后继续抛出原始错误或中断。
9. McpManager 负责已交给它的客户端。客户端创建成功、但发现工具或登记失败时，要关闭本地已取得的客户端。同名客户端不能被静默覆盖；重复 connect 不能无意间建立重复连接。
10. 工具装配发布要保持一致：某组工具构建失败、对应资源已经释放后，该组工具不能仍以指向关闭连接的状态暴露。普通单服务器连接失败保留现有“告警并跳过”语义，其他成功连接继续按既有契约使用。
11. MCP 取得之后，prompt、Hook 包装、Worker 构建等后续步骤失败，必须触发同一条资源清理路径，不能依赖已经完整返回的 Supervisor 或 Worker 对象。
12. Supervisor 和各个 Worker 分别拥有自己创建的连接。一个调用结束不能无差别关闭仍在工作的兄弟/子调用资源。父调用拥有的并行批次正常返回前，其子调用收尾也应完成。处理中断时，不能把 Future.cancel() 返回当作运行线程已经停止的证据。
13. 外部传入的执行环境、模型和工具默认为借用，只有明确转移所有权后才由本次调用关闭。本规格不引入隐式 MCP 池化、引用计数或跨 Application 共享连接。
14. Supervisor 可按现有锁契约缓存 runtime，但工具必须绑定本次调用的有效资源。同时验证 runtime 工具查找和 CodeAct executor 的绑定，不能假定替换一个 tools 字典就更新了所有执行路径。
15. checkpoint 不保存活跃 MCP client、线程、事件循环或连接。恢复时按所选定义重新取得连接，保持已有 task 身份、新 Run 身份及已提交进度契约。

### 清理、终态与低层入口

16. 没有资源、部分启动、完整启动和重复清理都必须安全。清理失败不能把仍存活资源标记为已成功关闭；即使一个资源清理失败，也继续尝试其他资源。
17. 已经执行失败、中断或预算受限时，保留原始错误和对应主终态，并附加清理诊断。原本执行成功但清理失败时，最终 Run 必须体现失败与可定位诊断，不能静默 completed。沿用 ApplicationRunLifecycle 的终态和持久化契约，不另建竞争的终态实现。
18. 可处理的取消和 KeyboardInterrupt 在清理后继续传播，不能被普通连接失败处理吞成“可选服务器不可用”。实际清理操作与测试必须有明确时间上限；仅安排后台关闭不算资源已经退出。
19. 正常调用全部接入资源作用域之后，移除 Application 收尾中通过 Supervisor 私有字段发现 MCP owner 的特殊逻辑。ApplicationRunLifecycle 继续协调整体收尾，两层不能各自维护一套 MCP 所有权记录。
20. 保留低层“返回 tools 与 manager 句柄”的 interface；未将所有权移交给调用作用域时，由调用方负责该句柄。临时创建一个 Agent 后只返回工具列表，不能导致 owner 丢失。
21. 对只返回列表的工具加载及目录加载入口，将资源登记到当前调用，或在既有 interface 上添加可选的显式 owner 参数。请求 MCP 却既无 owner、也不返回清理句柄时，在连接之前给出明确诊断与迁移说明。原有无 MCP 列表加载继续兼容。盘点并适配仓库调用点，不以垃圾回收、析构或进程退出替代正常清理。
22. 显式 owner 要求修正了此前没有资源归属的低层用法。记录受影响调用方，并与目录/定义开发线协调这一兼容性调整；Application 公开执行入口不能被静默破坏。

### Worker callable 构建

23. 删除 resolved Worker path 到带运行依赖 callable 的全局缓存，包括查询、写入及不再需要的锁。不要改成包含对象身份、mtime 或更多运行参数的大型缓存 key。
24. 每次工厂调用都使用本次传入的模型、logger、执行环境、Agent 类和受支持构建参数，并通过既有定义 loader 读取当前文件。保留文件/字典输入的校验与来源语义。
25. callable 保留创建时选定的 Worker 定义、schema 和依赖。磁盘修改/删除影响下一次工厂构建，不改变已创建工具的定义。下一次构建遇到无效、缺失或不再导出的定义，按既有失败/不导出契约处理，不能返回旧工具。
26. 对可变配置使用独立快照，Worker 执行不能修改调用者字典或 callable 持有的定义。模型、logger、执行环境和活跃资源不能通过 deepcopy 来代替正确所有权。
27. 保留 fresh Worker：工具的每次执行和每个 batch item 都拥有独立 Agent memory/state、Hook Run、local Run 身份及本次取得的 MCP 资源。显式传入模型仍可按其支持的契约共享；删除 callable 缓存不等于关闭 model manager 的独立行为。
28. 保留 typed 参数、schema metadata、必需/可选输入、结构化结果转换、ContextEngine 结果处理以及 batch 并发契约。无 schema 时不导出工具的行为必须有回归。
29. Repo Map 可以在一次分析操作中创建一次工具，并在该操作的多个 batch 中复用；不同分析操作之间不能依赖进程全局工厂缓存。同步更新相关文档和假设。
30. 现有清缓存入口如需兼容，可保留为无操作入口，但正确性不能再依赖它。回归测试不能在正要验证隔离的两次调用之间清理全局状态。
31. 用代表性 Worker 定义独立测量工厂构建开销，将其与模型请求耗时分开。第一版不增加替代缓存；只有存在明确性能问题，才另行提出有界、按正确内容及来源身份缓存的解析结果方案。复用统一 Application 定义 module，不新增 parser，不缓存混有运行对象的可变有效配置。

### 开发批次

| 批次 | 工作 | 完成门槛 |
| --- | --- | --- |
| A：基线与回归 | 固化同进程缓存和 MCP 部分取得失败的复现；准备隔离的功能验收素材 | 目标回归能暴露基线缺陷，已有正确契约有记录，各功能场景有独立预期 |
| B：Worker 工厂隔离 | 取消 callable 全局缓存，保留快照、fresh Worker 与 batch，适配 Repo Map 假设 | 重复/并发工厂测试和受影响 Worker/Repo Map 确定性测试通过，构建耗时有记录 |
| C：MCP 所有权 | client 部分启动恢复、manager 回滚、invocation 归属、缓存 runtime 重新绑定、低层 owner 契约 | 正常/失败/中断/重复/并发生命周期测试通过，真实本地 transport 证明资源释放 |
| D：功能与集成验收 | 最终候选运行真实模型场景和完整必要回归，合入目录/定义变更后再验收 | 必测场景全部通过，有 Run、产物和资源证据，未解决失败不能标完成 |

测试基础、Worker 行为修复、MCP 行为修复分别形成可审阅提交/PR。源码搬迁由目录开发线负责，不混入这些行为修改。

## Testing Decisions

### 测试 seam 与有效断言

1. 沿用用户已确认的 execute_app / loom run 主 seam，检查真实模型、多 Worker、工具副作用、Run 终态和产物。现有确认足以支撑本规格，无需再次访谈。
2. 确定性依赖测试通过真实文件入口 create_agent_as_tool 构建并执行工具。可使用记录依赖使用情况的 Agent/model adapter，但不能 mock 工厂本身，也不能只测绕过缓存的 agent_as_tool。
3. 生命周期优先通过 Agent run / Application 执行入口验证；Agent 尚未存在的失败，通过已有 McpManager/client interface 验证。在外部 transport/adapter seam 注入故障，以所有权、结果和清理副作用为断言，不固定私有 helper 调用顺序。
4. 单独设置真实本地 MCP transport 集成层，使用真实 client/manager，可用脚本模型控制时序。下述真实模型功能层不得使用脚本 Agent、固定回答或 mock MCP 工具执行替代。
5. 使用独立 oracle、资源 ledger 和产物校验；不能让被测 implementation 生成自己的预期值。disconnect 被调用、内部字典为空或模型输出 PASS，都不足以证明真实资源退出或任务正确。
6. 如果改动涉及定义检查，保留 Studio application.detail/application.validate 的只读回归：无模型请求、无 MCP 连接、无 Hook 执行、无 Run 创建；不新增配置语义。

### 单元与确定性回归矩阵

每一行均为必测，覆盖适用的正常/异常对照。参数化有意义的组合，不为测试数量复制用例。

| 领域 | 必须断言的行为 |
| --- | --- |
| 同路径、不同依赖 | 同一进程先后用不同模型、logger、执行环境、Agent 类构建工具，执行时确实使用各自传入的依赖 |
| 文件更新 | 改写同一路径 YAML/Markdown 后再次构建，新的工作流、schema、并发 metadata 生效；路径别名不能掩盖变更 |
| 文件失效 | 先成功构建，再改成无效内容、删除文件或移除导出 schema，后一次构建遵守失败/不导出契约 |
| 定义快照 | 工具创建后修改调用者字典或文件，既有工具仍保留选定定义；fresh Worker 不修改快照或其他调用的配置 |
| 同进程默认选择 | 同一路径在连续运行上下文中按受支持默认模型选择取得正确当前依赖，不清缓存、不重启解释器 |
| fresh Worker / batch | 顺序及并发调用保留 typed 输入、独立 memory/state、Hook Run/root-local 身份，单项失败不污染其他结果 |
| Worker 输出 | schema 错误、可选输入、结构化结果、ContextEngine 压缩和并发参数选择遵守既有契约 |
| client 部分取得 | transport 启动后及发现/适配阶段注入失败、中断，即使构造函数没有返回 client，也回收已启动资源 |
| manager 部分取得 | client 创建后发现/登记失败要关闭；失败服务器不能使成功资源失去 owner；重复 connect 不静默覆盖活跃 client |
| 工具/runtime 装配 | MCP 取得后，在包装、发布或后续 Agent 构建阶段失败，资源回收且不暴露指向关闭连接的工具 |
| 调用完成 | 根 Supervisor、Worker、直接运行的 Agent，在成功和执行失败时均释放自己的连接 |
| 重复调用 | 两种模式复用缓存 Supervisor，旧资源退出、新工具可用，每次调用后受测活跃资源回到基线 |
| 并发/嵌套归属 | 一个 Worker 结束/失败后兄弟 Worker 仍可调用 MCP；嵌套调用和显式借入资源保持正确 owner |
| 中断/预算限制 | 处理完中断、Goal 预算耗尽后的资源回收再传播正确终态，线程/future 处理不能提前关闭兄弟资源 |
| 清理错误 | 一个关闭失败不跳过其余资源，重复清理安全，主终态与附加清理诊断遵守规格 |
| 低层调用方 | 显式 manager/owner 路径正确释放，无 owner 的列表式 MCP 加载在连接前拒绝，无 MCP 旧用法继续工作 |
| 恢复 | 同 task、新 Run、新连接；恢复状态可用，已提交副作用不重复 |
| 只读检查 | 如受影响，Studio 检查不取得运行资源、不请求模型、不执行 Hook、不分配 Run |

### 真实 MCP transport 验证

使用受控本地服务器，覆盖当前支持的 stdio 和 HTTP/SSE transport。区分测试 harness 与 Agent 的资源所有权：client 启动的 stdio 子进程随 owner 关闭而退出；独立部署的 HTTP/SSE 服务器保持可用，Agent 只释放自己的会话/连接。

记录唯一 test/run 标记、实际支持的 client/session/connection 身份、启动进程 PID、进行中的工具操作和关闭证据。无状态 transport 验证客户端 transport 实际关闭，不能虚构持久 session ID。按需核对本测试拥有的线程、事件循环和子进程，不能把宿主无关后台活动算入泄漏。

覆盖成功、部分启动失败、Worker 生命周期交叠、重复运行与可处理中断。在声明的超时内等待清理结束；超时或资源遗留均失败，依赖解释器退出不算通过。独立测试之间恢复 fixture 基线，但缓存回归所需的跨调用状态不得被提前清掉。

### 真实模型复杂 Application 验收

优先复用目录开发线的 architecture_contract_validation Application 和独立校验器，通过明确的资产归属为其增加运行隔离与 MCP 场景。若该资产尚未落地，可由本开发线提供给两份规格共用；不能并行创建两个互相竞争的版本，也不能将复杂任务替换为 echo smoke。

Application 包含一名 Supervisor 和至少四名不同 Worker，分别完成调查、修复、测试生成和独立核验。任务使用可重置的多目录 Python fixture，包含跨 module 依赖、已知缺陷和独立行为预期。Agent 必须真实分析和修改代码、执行生成测试，并根据产物形成报告。真实 MCP 工具应承担任务必需工作，例如读取代码证据、写入分析产物、记录验证证据，不能只是配置了却不调用。

| 编号 | 场景 | 必需证据 |
| --- | --- | --- |
| F1 | 原生工具调用复杂任务 | 真实模型请求、至少四个不同 Worker、实际 MCP 副作用、有意义的生成测试真实执行、独立代码/产物检查和正确 Run 终态 |
| F2 | CodeAct 同等任务 | 完成相同的行为目标与独立检查，验证 Python 执行和多阶段 MCP/Worker 工具绑定 |
| F3 | 同进程重复构建与运行 | 同一 Python 进程使用同一 Worker 路径执行不同受控任务，合法修改定义/配置后重新构建；工具/模型/上下文证据能识别新依赖，各任务产物隔离 |
| F4 | 既有 Repo Map | 在至少三层目录和跨 module 引用的 fixture 上完成扫描、排名、真实 Worker 分析、Skill/产物生成；同进程重复分析操作覆盖文件工厂，独立核验已知关系和产物来源 |
| F5 | 并行 Worker 受控失败 | 至少两个 Worker 生命周期交叠，fixture 诱发一个失败后，兄弟 Worker 仍完成实际 MCP 调用；各项及汇总结果遵守预先定义的 Application 契约，资源按 owner 释放 |
| F6 | 可处理中断与恢复 | 在 Supervisor/Worker 已提交进度后中断，检查资源回收和 interrupted 证据；经公开入口恢复，同 task、新 Run、新连接，不重复已提交副作用 |
| F7 | Goal 续跑与预算恢复 | 同次调用多个 Goal 段保持 MCP 可用；既有并行预算场景达到预期限制后释放资源，再显式调整验收预算恢复，保留累计进度 |

F3 的依赖身份必须能从请求路由/配置、运行日志或工具副作用观察，不能从模型措辞推断。确定性测试覆盖全部显式参数；真实模型场景至少覆盖受支持的模型配置/选择变化，以及不同执行/输出上下文，不虚构环境中不存在的 model profile。同进程执行顺序 Application、并发 Worker 即可满足要求，不要求新增多个无关 Application 同进程并发的能力。

最终候选上，原生工具调用与 CodeAct 成功场景各连续两次通过，F3–F7 所有子场景符合预期。失败、中断、预算限制场景可以非零退出，但只有终态、副作用、资源证据及后续恢复全部正确才算场景通过。保留每次失败，外部瞬态故障只能按有上限、有记录的策略重试。

### 完成门槛与证据

1. 执行当前 CI 要求的 Python 测试集合，以及受影响 Application 的确定性测试，覆盖 MCP、runtime 构建、生命周期、Worker schema/batch、Hook、ContextEngine、checkpoint 与 Goal。记录收集、通过、失败、错误、跳过数量及已有跳过原因；禁止删测试、弱化断言或增加 skip/xfail 使必测场景变绿。
2. 本运行时开发线在受影响或 CI 要求时执行 TUI、构建、安装检查。与目录改造合并的最终候选仍须满足前一份规格完整的 Python/TUI、安装和功能矩阵，本规格不缩减其要求。
3. 所有必测真实模型和真实 transport 场景都须执行并通过。缺少凭据、依赖不可用、网络错误、超时不算通过，不能静默退回 mock。
4. 证据绑定候选 revision 及未提交补丁身份、定义/fixture 身份、模型与模式、起止时间、运行入口、Run/task ID、终态、独立断言、产物位置和资源 ledger；报告不含 secret。
5. 执行前声明每场景的时间、并发和预算上限，保存原始失败及修复记录；有相关变更后重跑受影响完整场景。规格文档和历史基线结果不能作为最终通过证据。
6. worktree 合入目录/定义变更后重新执行必要的组合验收；分支各自通过不代表组合通过。
7. 完成要求：必测失败全部解决、没有未解释的所属资源遗留、证据完整且可复跑。阻断这些契约的必要修复独立提交并追加对应回归。

### 既有测试先例

- Worker 工厂模式测试已验证 fresh Agent 和 batch，但主要直接进入 agent_as_tool；保留有效契约，补真实文件工厂入口。
- MCP manager 测试已覆盖连接成功、可选服务器失败、聚合和基本断开；扩展到部分启动和真实资源副作用。
- MCP adapter 测试已覆盖结构化结果、工具错误、Hook/终态投影，生命周期修改须保留这些行为。
- runtime-builder 和 ApplicationRunLifecycle 测试覆盖缓存 runtime、收尾、终态；追加按次 MCP 所有权和清理失败。
- Repo Map 是真实文件路径工厂调用方，必须执行完整分析阶段，仅扫描而不调用 Worker 不足以验收。
- checkpoint/Goal Application 已有进度 ledger、中断恢复与并行预算先例，应复用独立验证方式，不只检查最终文本。

## Out of Scope

- 自有源码搬迁、namespace 迁移、Application schema 重设计或第二套配置 parser，由目录/定义开发线负责。
- 移动或清理本地开源参考仓库，删除用户既有 Application、checkpoint 和产物。
- 无关的 smolagents/mcpadapt 版本升级或替换、第二 Agent 引擎，以及 Hook、Goal、ContextEngine、调度模型重写。
- 没有明确需求的进程全局 MCP 连接池、跨 Application 自动共享与引用计数。
- 已创建 callable 内部热更新 Worker 定义，或改变正在执行任务的语义。
- 重做 model manager 缓存、deepcopy 活跃依赖、没有性能证据就实现通用解析缓存。
- 仅为了方便测试新增公共资源管理框架；应复用既有执行和工厂 interface，在必要处明确 owner。
- 对不可捕获进程终止、操作系统故障、断电承诺 Python 清理保证；可处理的中断、部分启动和正常进程运行仍是必测。
- 无界压力/模型运行，以及干扰其他 worktree 或用户资源的验证。

## Further Notes

### 与目录改造的协作

关联规格：[自有源码职责整理与 Application 定义统一](architecture-and-application-definition.md)，已发布为 [Issue #67](https://github.com/linora-u/AgentLoom/issues/67)。前一份规格没有将 MCP 生命周期、Worker callable 缓存作为独立重构范围；本规格补充这条运行时开发线，不替代前一份规格，也不豁免其验收。

目录开发线拥有源码位置、namespace/兼容导出、共享定义解释；本开发线拥有运行行为修复。双方会修改 YAML Agent 工厂和 runtime 构建，应先约定 module interface 与提交归属，再并行编辑，最后将行为提交适配到一致布局。真实 Application fixture/harness 明确唯一负责人，供两份规格共享。

每个 worktree 使用能正确解析到本 checkout 的独立 Python 环境/安装、运行/产物/checkpoint 位置、测试数据库、端口及可追踪 MCP 进程。Git worktree 不隔离固定临时路径和外部资源；并行验证前参数化这些位置，并协调真实模型并发与限额。

### 已核对的研究事实及其限制

- 基线已有 Application 结束时的 Supervisor MCP 清理；问题在调用所有权不完整及部分失败路径，不是完全没有清理。
- 一个通过真实文件工厂、使用记录型 Agent adapter 的小复现，改写临时定义并传入新模型/环境对象后，仍取得相同 callable、旧工作流和旧依赖。该复现未调用模型。
- 一个故障注入复现，在 manager 取得 client 后令工具发现失败，随后的 manager 清理没有关闭该 client。它验证取得路径的所有权缺口，不等于完成真实 transport 验收。
- 写出本规格时尚未完成业务实现或完整功能测试。上述复现须固化为回归，并补齐真实 transport、真实模型证据。

### 开源依据

1. [smolagents v1.26.0 MCPClient](https://github.com/huggingface/smolagents/blob/12c1bc820eca50ace6f80a21d90426d41d74f845/src/smolagents/mcp_client.py)：显式连接、断开和上下文管理。AgentLoom 保留自身协议适配，将生命周期放到实际 Agent 调用 seam。
2. [OpenAI Agents SDK model-provider 生命周期](https://github.com/openai/openai-agents-python/blob/58a6d2c932810fcf9cb7a1a5a68e666ff04553ed/src/agents/run_internal/model_provider_lifecycle.py)：区分 Runner 自建与调用者传入资源，并在取消时完成清理。它处理的是 model provider，本项目借鉴所有权原则，不直接照搬为同步 MCP 实现。
3. Python 标准库 ExitStack 提供清理登记和逆序释放。对可能部分成功的取得过程，应提前登记；context manager 进入失败不意味着退出方法自动执行过。
4. 当前锁定的 mcpadapt 0.1.20 同步实现，在进入时启动线程/事件循环、退出时关闭。部分启动及中断行为必须针对实际锁定依赖验证，不能假定构造函数抛异常就没有遗留资源。

本次按用户要求交付本地规格，不发布新 issue、不开始实现、不创建 worktree，也不宣称测试通过。
