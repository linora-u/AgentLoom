# 默认受管并发执行规格

规格日期：2026-10-14。状态：实施中；按用户最新要求修订为源码参考路线。

本规格汇总有限 Worker 并发、持久执行、NewsAgent 接入及 OpenClaw 机制研究的最新决定。它替代早期方案中的自研持久调度内核、系统服务部署要求和用户选择进程 owner 的配置；既有业务规则、有限 Batch 合同和显式输出预算合同继续有效。

用户已授权实施、完整验收后合入 main 并删除开发 worktree。最新约束优先：只参考 OpenClaw 源码，禁止安装、执行或打包其产品、SDK 及运行依赖闭包；不能通过重命名、内嵌分发包或变换启动路径继续运行。保留原有 AgentLoom 环境，默认采用已有 Python 和依赖；版本调整须有实际兼容性证据。

## Problem Statement

用户希望配置一个并发数，就能可靠地运行多个 Agent，而无需理解线程池、进程监护、执行插件和上游项目的接入方式。NewsAgent 应复用 AgentLoom 的执行能力，保留已有新闻分析流程，不再单独维护通用执行机制。

当前有限 `BoundWorker.map()` 已有单次调用并发合同；持久任务已接入 DBOS，本地默认使用 SQLite。现有未提交原型又接入了 OpenClaw 的公开进程 SDK，并切换了 durable 默认入口。但执行器和 NewsAgent 仍直接依赖具体实现，业务授权、资源恢复及实现内部状态混在一起。把这些代码搬进一个插件目录，不能解决这类耦合。

执行正确性也尚未闭合。静态检查发现启动授权与取消可能交错，导致已取消的尝试随后获得业务写权限；清理未确认时，原型可能一直等待退出文件，控制命令无法清楚报告进展。有限 map 尚未接入拟议的默认执行模块，新机制下的完整 NewsAgent 日期也未验收。

这些问题关系到实际资源：模型回复完成、业务提交完成、DBOS 进入终态和 Pi/MCP/工具进程退出是不同事实。提前释放容量或重试，可能让旧资源与替代尝试重叠。反过来，无限等待而不提供可查询结果，也会让用户无法判断系统是否仍在收尾。

历史 SQLite 提交失败及 522 现场缺少足够证据。更换执行机制不能被当作已经找到根因；后续实跑必须先准备 trace，保留原始错误及恢复过程。

## Solution

AgentLoom 默认装配一个受管执行模块。用户沿用现有并发数量配置和公开 Application 调用，不增加 `process_owner`、`openclaw` 或插件选择项。模块参考已核对的 OpenClaw 源码中资源所有权、父进程死亡通知、启动闸门及退出确认的原则，在已有 Python 资源管理边界内实现必要适配。删除原包接入、安装器和 Python／Node owner 桥，不引入新的调度平台。

有限 map 与持久执行共享执行尝试的资源生命周期合同，保留各自的调度含义：有限 Batch 的容量属于一次 map；持久队列的容量属于稳定执行域。配置并发不会自动开启持久化。

DBOS 继续负责持久入队、阶段容量和 workflow 恢复。默认支持 macOS、Linux 本机运行，使用 SQLite，不要求用户配置 launchd/systemd 或额外数据库、broker 服务。首版覆盖单机多进程，保留将来替换远程存储的接口。

NewsAgent 通过同一框架入口启动、停止及恢复执行。正式 run-day 流程保留初筛完成后自动提交精筛、随后校验和封存的行为；计分继续走既有正式入口。初筛、精筛、校验分别使用独立阶段队列，容量为 20／30／20，同一执行域内跨日期共享。

顶层执行 Python 被强杀后，独立监护进程自动回收全部受管理任务后代，并在收尾后退出。替代尝试必须等旧本机资源退出确认完成后才能启动。清理未确认时，借鉴 OpenClaw 源码中的保留所有权、延后确认和控制请求报告机制，不能用超时制造退出成功。

## User Stories

1. As an Agent user, I want to configure only concurrency, so that reliable execution does not require selecting an owner or plugin.
2. As an Application author, I want the execution module assembled by default, so that ordinary calls work without constructing implementation objects.
3. As an Application author, I want existing concurrency precedence preserved, so that project settings, Worker overrides and call overrides remain predictable.
4. As an Application author, I want each map to retain its own concurrency limit, so that separate Batches do not unexpectedly share a Run-wide allowance.
5. As an Application author, I want concurrency independent of persistence, so that a finite map does not silently become a durable queue.
6. As an Application author, I want finite and durable execution to share resource lifecycle guarantees, so that cancellation and cleanup behave consistently.
7. As an Application author, I want existing trusted factories and dependencies preserved, so that adopting managed execution does not change ordinary Worker authoring.
8. As an Application author, I want each attempt to have isolated mutable execution state, so that siblings and retries cannot contaminate one another.
9. As a framework maintainer, I want a small internal process-management interface, so that core execution code does not depend on a concrete library's internals.
10. As a framework maintainer, I want source-informed process ownership within the existing resource boundary, so that AgentLoom does not install OpenClaw or absorb its execution platform.
11. As a framework maintainer, I want DBOS to remain the only durable scheduler, so that two sets of claims, retries and capacity accounting cannot disagree.
12. As a local operator, I want macOS and Linux execution without system-service setup, so that installing the project is sufficient to use its local execution mode.
13. As a local operator, I want SQLite persistence by default, so that durable execution does not require deploying another service.
14. As an integrator, I want a replaceable storage boundary, so that a future remote backend does not require changing news rules.
15. As a local operator, I want independent producer processes to share one local execution domain, so that submitting another date does not duplicate executor capacity.
16. As a NewsAgent operator, I want stage capacities of 20, 30 and 20 shared across dates, so that workload remains bounded as dates accumulate.
17. As a NewsAgent operator, I want each stage's allowance independent, so that one busy stage cannot consume another stage's declared capacity.
18. As a NewsAgent operator, I want run-day to advance through screening and validation automatically, so that I do not manually start each next stage.
19. As a NewsAgent maintainer, I want existing screening and validation rules retained, so that execution refactoring does not change the analysis strategy.
20. As a NewsAgent maintainer, I want sealing and scoring requirements retained, so that an execution success cannot publish incomplete business results.
21. As an operator, I want capacity refilled after an individual attempt finishes cleanup, so that a slow sibling does not stall an entire wave.
22. As an operator, I want stop to prevent new admission, so that cancellation does not start additional business work.
23. As an Application author, I want cancelled attempts unable to regain write authority, so that delayed startup cannot revive stale work.
24. As an Application author, I want attempt and revision checks on business writes, so that late callbacks cannot overwrite newer results.
25. As an operator, I want Pi, MCP, tools and their descendants managed together, so that root-process exit cannot leave task resources behind.
26. As an operator, I want automatic descendant cleanup after the top execution Python is killed, so that cleanup does not depend on restarting the application or installing OpenClaw.
27. As an operator, I want cleanup uncertainty visible through control commands, so that I can distinguish a stop request from confirmed resource exit.
28. As an operator, I want uncertain resources to retain their capacity, so that a replacement cannot overlap an unconfirmed old attempt.
29. As an operator, I want recovery to respect the original resource owner, so that upgrading cannot misidentify or discard live resources.
30. As a NewsAgent operator, I want committed business receipts reconciled after a crash, so that already completed analysis is not unnecessarily repeated.
31. As a NewsAgent operator, I want existing automatic and manual retry rules retained, so that recovery does not reset or exceed the business retry allowance.
32. As a NewsAgent operator, I want new attempts to read current effective configuration, so that an official retry can use a corrected model or prompt setting.
33. As a model user, I want omitted output limits to remain omitted in the final request, so that the framework cannot impose a hidden generic budget.
34. As a model user, I want explicit limits and null overrides preserved, so that execution refactoring does not change budget control.
35. As a NewsAgent operator, I want authenticated AnySearch and the authorized key-failover behavior retained, so that real validation uses the intended search configuration.
36. As an operator, I want traces prepared before execution, so that a failure can be linked to its task, attempt, model request and persistence operation.
37. As an operator, I want original SQLite error codes and causes preserved, so that diagnosis follows evidence rather than a secondary index error.
38. As a reviewer, I want tests through public Application and CLI calls, so that passing private scheduler assertions cannot conceal broken integration.
39. As a reviewer, I want macOS and Linux fault evidence, so that cross-platform support is demonstrated rather than inferred.
40. As a NewsAgent reviewer, I want a complete real date using DeepSeek v4.1 and AnySearch, so that every business phase is exercised without trimming the sample.
41. As a reviewer, I want existing prototype results distinguished from final acceptance, so that earlier green tests cannot be mistaken for validating later changes.
42. As a maintainer, I want pinned source references and actual implementation costs documented, so that borrowed mechanisms remain reviewable without an upstream runtime dependency.
43. As an operator, I want dependencies ready before tasks are accepted, so that runtime downloads or silent fallback cannot change execution guarantees.
44. As an Application author, I want this introduced compatibly, so that I can use the default mechanism without waiting for a major version or rewriting public calls.

## Implementation Decisions

### 默认行为与公开合同

1. **并发配置保持现有形式。** 保留 `worker_execution` 的 defaults、Worker 覆盖和 `map(concurrency=N)`。优先级继续为框架默认、项目配置、Application 配置、Worker 配置、调用覆盖。值为严格正整数，拒绝布尔值、字符串及隐式 auto；不增加未经需求支持的数量上限。框架默认值仍为 1。
2. **执行机制由框架选择。** 默认机制不能因并发数量来源不同而改变。普通 Agent／Worker 不配置进程 owner、上游产品名称或插件类型；数量为 1 也不降级资源生命周期合同。
3. **保留两种调度含义。** `BoundWorker.map()` 执行一个有限 WorkPlan，产生一次 Batch，其上限只属于该次调用；`Application.durable()` 提供跨 Run 的持久执行连接。map 的顺序结果、输入快照、collect／fail-fast、停止及收尾合同继续有效。两者共享资源生命周期，不共用一个隐式全局容量池。
4. **不增加普通调用者的接入负担。** Application、BoundWorker、DurableApplication 仍是业务入口。有限 map 的 live agent_factory、可信依赖、Context、Run 和 trace 绑定必须保留；不能要求用户为了配置并发而改写成持久宿主描述，也不能跨进程传递数据库连接或 Runtime 活句柄。有限 map 的具体接线属于实施项，尚未由原型证明。

### 模块职责与上游复用

5. **内置模块先行。** 进程管理按插件边界隔离，但首版由框架内置并默认装配。只有一个默认实现时，不新建通用插件发现平台；禁止安装 OpenClaw 产品、SDK 或运行资产，不启动 Gateway 或选择其 Worker 平台。
6. **框架与实现之间只保留一个主要边界。** 该边界提供执行域打开与恢复、执行尝试、停止与退出确认。返回的业务传递结果和资源退出事实相互独立。具体方法名在实施时按现有注入点收敛；核心执行器不读取插件的 PID 表、目录布局、Node IPC 或具体 active 集合。
7. **统一装配。** Application composition 默认创建执行模块，启动、停止、查询及恢复复用同一入口。NewsAgent 不直接构造具体进程实现。既有 `jobs=` 仅作高级可信注入兼容入口，普通调用者无需使用。AgentRuntime／RuntimeRegistry 继续负责 Pi、smolagents 等 Agent Runtime，资源所有权不混入模型 Runtime 注册职责。
8. **授权与业务协调留在正确层次。** 框架负责执行尝试身份、可信启动许可、取消和资源作用域。NewsAgent 负责业务 grant 的事务实现、业务完成回执和重放决策。进程管理模块不访问新闻业务表、不决定业务 retry，也不吸收完整阶段流程。现有混合的 OwnedJobs 职责必须据此拆分，而不是原样迁移到插件。
9. **参考来源固定为 OpenClaw 源码。** 借鉴已核对的父死亡关联、受管资源清理、启动闸门及退出观察原则，记录其源码位置及与实现的对应关系。最新用户要求撤销旧的“直接 SDK／内部运行模块复用”路线；不得下载运行资产、vendor 原包或通过改名继续执行。也不引入完整 NodeWorkerSupervisor 的 journal、claim、capacity 和 workspace 平台。
10. **按实际总代码收敛实现。** 优先复用项目已有的 Python、psutil、资源注册和进程启动边界，只补齐独立监护及退出确认所需职责。原 Node owner 安装器、运行桥及专用 Node wheel 依赖须退出发行与 CI。记录源代码参考 commit、许可证、实际新增／删除代码和依赖；源码研究基线仍为 OpenClaw 2026.10.3，不能把原包局部探针算作新实现验收。
11. **宿主死亡通知保持单一可信来源。** 独立监护观察可信宿主控制连接的关闭，负责资源范围收尾；任务后代不得持有阻止通知的控制端。使用现有 Python 环境，不为 owner 新增 Node 运行时；Pi 自身原有 Node 需求保持独立。监护不决定业务重试、队列容量或新闻状态。

### 持久队列与容量

12. **DBOS 是唯一持久调度引擎。** 沿用已接入的 DBOS 3.2.0 公开 Client／queue／workflow／step API，不维护框架自有领取、续约、持久 slot 或 sweep 内核，不直接操作 DBOS 私有系统表。稳定工作项身份、revision、重复提交幂等和输入冲突拒绝继续有效。
13. **默认本地，远程可替换。** DBOS system SQLite 与 NewsAgent 业务 SQLite 分离，不要求两库共同参与同一原生事务。通过业务完成回执及公开恢复协调跨库提交窗口。远程替换优先使用引擎公开连接或执行接口，不能把私有表包装成存储接口后恢复自研领取循环。远程接口保留不代表多机执行已交付。
14. **首版单机多进程。** 同一执行域允许多个 CLI producer，但持久消费采用一个本地 executor，任务在受管本机资源内执行。框架在接纳和恢复前检查已有消费者与旧资源归属，不能仅凭 DBOS 的进程内 singleton 声称跨 CLI 唯一。独立执行域互不误停。
15. **阶段额度独立且跨日期共享。** 三个 Worker 分别注册初筛 20、精筛 30、校验 20 的队列额度；多个日期不会各自再获得一份。不同声明冲突在消费者启动前拒绝。阶段独立容量是公开保证，不要求三个物理线程池或三个 executor；DBOS 可以共用底层执行线程池。快任务收尾后及时补位，不等待整波。
16. **收尾也占容量。** 在旧尝试仍持有资源、退出未确认，或必需终态未成功持久化时，相应容量不能供替代尝试使用。取消状态、Future 完成和根进程返回不等于容量可释放。不得为了满足指标增加第二套持久容量账本。

### 启动、取消与退出

17. **先登记可取消尝试，再授予启动资格。** 沿用 OpenClaw 的 admission／start gate 顺序，在授权事务内、放行前及业务 dispatch 前复核当前尝试和取消事实。取消先关闭启动资格，再撤销业务写权并停止已创建资源。已登记且已取消的尝试不能由后到的 grant 或 running 更新重新激活。这个合同不扩展成对尚未登记的任意未来任务永久拒绝。
18. **写入受尝试和 revision 约束。** 工具提交、业务完成、失败及 retry 必须核对当前执行授权。不能出现父进程撤销未存在的 grant 后记录撤销完成，子进程随后创建有效 grant 的窗口。对该静态风险先构造可控复现，再通过同一公开入口验证修复；不以增加一个文件标记代替事务合同。
19. **管理完整本机资源范围。** Pi／Node、stdio MCP、工具子进程及孙进程都必须进入框架受管资源范围。作用域由可信宿主绑定，框架验证并传播；模型输入和任意环境变量不能接管别人的资源范围。只支持能满足该合同的 Runtime／工具配置；主动逃离管理范围的运行方式在业务开始前拒绝。
20. **宿主强杀必须自动清理。** 验收强杀对象是顶层执行 Python。框架可以自行启动独立 Python 监护，监护存活期间完成全部受管后代清理，随后退出。过程不依赖宿主 Python 的 finally、重启、recover 或测试侧清理。正常退出、根崩溃和关闭异常同样不能遗留任务后代。
21. **按上游机制处理清理未确认。** 保留资源所有权及原容量，保存原始清理失败，允许 status／stop／recover 报告未确认或返回当前观察结果，后台继续保留收尾责任。控制请求的有界观察与完整 drain／close 分开；不把观察超时写成成功，不承诺所有 close 都固定几秒完成，也不以无解释的无限文件轮询作为控制接口。
22. **恢复后才允许替代。** 新消费前核对旧资源退出、旧授权和业务回执。确认业务已提交时协调执行状态，避免重复模型调用；未完成业务按已有恢复规则处理。只有旧管理范围确已退出且必需记录成功落盘，才能补位或 resume。资源退出回执必须能区分业务完成回执。
23. **保存原有业务失败策略。** 自动重试和人工 retry 的边界、计数及部分结果清理规则继续由既有业务策略决定。过期尝试不能重新入队或重置新尝试次数。进程管理模块不增加自动重启、不静默调整输出预算，也不把所有 Provider 错误改成可重试。

### NewsAgent 与模型请求

24. **保留正式流程。** run-day 等待初筛业务及执行要求完成后，自动提交精筛，再推进校验和封存。单独提交初筛仍按已有阶段提交合同处理，不因此变成完整 run-day。候选判断、正式判断、行业净方向合并、补充校验和新闻分组继续按现有业务规则。封存是计分前提，run-day 返回 sealed 不代替正式计分验收。
25. **业务完成独立核验。** Worker 返回或模型最后一句成功不能代替必需工具提交、覆盖检查和业务完成回执。业务已经提交但 Runtime 尾部失败时，保留完成业务并记录执行失败，避免无条件重算。执行库状态与业务回执通过框架公开恢复协调，不要求进程插件理解新闻状态。
26. **配置在尝试间刷新，在尝试内固定。** 新尝试读取当前有效 Prompt、Skill 和模型设置，并记录非秘密配置 revision；正在执行的尝试使用自己的快照。分析输入指纹与执行配置 revision 分开，配置更新不自动废弃已封存结果。
27. **输出预算遵循 ADR-0002。** 未配置时不向 Provider 注入通用上限；显式正整数才限制；覆盖中的 null 可解除继承。输入压缩预留、内部摘要预算和输出上限分别管理，context window 不能当作最大输出能力。协议必须带上限时，使用核实能力或要求显式配置，未知则请求前报错。验证以最终 HTTP payload 为准，既有截断分类、raw reason 与 usage 保留。本规格将已实现修复作为回归合同，不重新扩大预算功能范围。
28. **AnySearch 遵循最新授权。** 使用当前已授权的认证配置与 main 中的 Key 自动切换逻辑，不恢复过时的“只用首个 Key”限制。真实验收检查请求携带配置所要求的认证，保留脱敏的切换、额度错误和成功搜索证据。不能仅凭本机配置断言供应商计费已经落到某账户。

### 可观测性、安装与迁移

29. **运行前准备 trace。** 关联 Application、Run、Worker、工作项、revision、执行尝试及资源范围；记录接纳、授权、放行、取消、业务提交、清理和退出确认。保存源码／构建身份、依赖版本、非秘密有效配置、新闻输入与行情指纹、模型请求及搜索调用关联。公开日志不包含 Key 或完整认证头。
30. **保留持久化原始故障。** SQLite 错误记录原始 exception chain、sqlite_errorcode／sqlite_errorname（驱动可提供时）、操作与事务阶段、数据库身份、进程身份、SQLite 版本及 journal 配置。未提供的字段明确记为未知，不能编造。提交失败不能被后续容量索引异常覆盖，必需回执写入失败必须通过公开入口显示为基础设施错误。历史 522 不因新路径通过而自动结案。
31. **环境改动保持最小。** 默认模块使用项目现有 Python 和依赖，标准安装和业务运行均不得安装 OpenClaw，或要求专用全局 Node／owner 安装命令。缺依赖、平台不支持时，在接纳前明确报错，不能悄悄退回较弱清理模式。默认本地运行不要求设置系统服务。只移除旧 owner 专用依赖，保留 DBOS、Pi 和其他既有组件；调整 Python 或依赖版本前须提供实际失败及兼容性证据。
32. **兼容旧资源，不热替换活动 owner。** 资源描述保存实际实现身份、描述格式和必要上游版本。旧 launchd 资源由能识别它的兼容 Adapter 排空；缺少所需 Adapter 时保留诊断并拒绝启动替代，不能删除描述绕过检查。历史业务结果保持可读，已提交队列、工作项及回执身份不因模块拆分而改名。新执行默认走框架装配入口，旧 Adapter 只服务兼容。
33. **当前开发周期交付并清理。** 不需要等待 AgentLoom 下一大版本或 OpenClaw 新增公开接口。保持公开调用兼容，撤掉 execution-owner 安装能力并说明旧接法退出；业务数据合同继续有效。完成全部验收与 review 后合入 main、同步公开版本，并删除本任务开发 worktree。交付评审比较实际代码、删除的重复职责和发行依赖，不能只以模块数量判断简化程度。

## Testing Decisions

### 入口与既有依据

1. 沿用用户已经确认的最高测试入口：Application Run 中的 `BoundWorker.map()`，以及同一 Application 的公开 durable 提交、执行、停止和恢复调用；正式 runner／CLI 验证最终接线。NewsAgent 使用正式 prepare、run-day、retry／recover、status 和计分入口。不再就测试入口重复采访。
2. 验收只断言外部行为：结果顺序和业务产物、公开状态与回执、实际请求 payload、真实资源身份和退出。线程池私有状态、DBOS 私有表、插件目录布局及内部 active 字典不是验收接口。
3. 复用现有有限 Batch／terminal-control、durable Application／业务回执恢复、真实 Pi／MCP、managed smolagents、News controller／阶段衔接／CLI retry 测试模式。故障控制可以注入底层 transport 或持久化边界，但实际执行从公开 Application 入口进入。
4. macOS、Linux 运行相同公开故障矩阵。进程判断使用 PID 与创建身份及实际管理范围，覆盖根先退出、后台后代、忽略 TERM 的后代和监听端口。强杀后先观察自动回收，再允许 recover、replacement 或测试保护清理，避免把测试清理算成产品能力。

### 必需验收矩阵

| 场景 | 必须观察到的外部行为 |
| --- | --- |
| 默认并发、Worker 覆盖、调用覆盖及并发 1 | 全部自动获得受管机制，无 owner／plugin 参数；并发来源和数量可查，非法值在启动前拒绝 |
| 两次 map 并行及 map 与 durable 同时运行 | 每个 Batch 保留自身上限，结果按输入顺序返回；有限调用不隐式入持久队列，资源互不误关 |
| 有限 map 使用现有 factory、可信依赖和不同 Runtime | Context／Run／trace 正确归属，尝试状态隔离，取消及根关闭收尾符合原公开合同 |
| 多 CLI、多日期和不同执行域 | 同域只运行一个本地消费者；三个阶段共享各自 20／30／20，不同域隔离，容量声明冲突拒绝 |
| 充足任务、慢首项和快后项 | 各阶段能利用声明额度，任一快项完成清理后补位，不等待整波；未确认收尾不会被补位 |
| stop 在接纳、授权事务、放行或 dispatch 边界发生 | 生效后不启动后续业务；取消先到的已登记尝试不能随后获得有效 grant，也不产生业务孙进程 |
| 旧尝试晚到工具提交、完成、失败或 retry | 新 revision／尝试状态不受污染，自动重试计数不被重置或突破，拒绝原因可查 |
| 正常完成、关闭回调失败／阻塞、Application 根崩溃 | 业务与执行错误分别保存；Pi／MCP／工具及受管后代确已退出后才释放容量 |
| 顶层执行 Python SIGKILL | 不重启、不 recover、不借助测试清理，任务后代和库监护自动退出；随后恢复不与旧资源重叠 |
| 清理结果 uncertain／失败，之后恢复确认能力 | status／stop 可报告未确认，所有权和容量保留，替代尝试不启动；后续确认成功才完成收尾 |
| 业务提交后、执行 ack 前崩溃；业务完成但 Runtime 尾部失败 | 通过完成回执恢复已有业务，不重复模型调用；仍保留尾部错误，资源收尾不被跳过 |
| 退出确认后必需终态落盘失败 | 不虚报可补位或 clean stop，原始写入错误可查；恢复协调记录后才重新供容量使用 |
| SQLite 提交故障及观察回调故障 | 提交故障保留原始错误和事务信息，不被次生容量错误覆盖；观察回调不能代替或破坏必需业务确认 |
| 旧资源仍活动、描述版本不兼容、PID 被复用 | 不误杀其他资源，不丢弃旧描述，不启动替代；兼容 Adapter 排空后历史结果仍可读 |
| 依赖缺失、不兼容平台或 Runtime 主动逃离管理范围 | 业务开始前明确失败，不退回弱清理，不在任务内下载依赖 |
| run-day、阶段单独提交、补充校验及多个日期 | run-day 自动推进既有阶段；单阶段提交不改变含义；三阶段额度独立，全部必需校验完成才封存 |
| 配置刷新、输出预算、真实 SDK transport | 新尝试读取新配置，运行快照固定；未配置及 null 解除不注入输出 cap，显式值原样约束请求，截断原因保留 |
| AnySearch 认证、Key 额度失败和后续成功 | 使用已授权认证及切换逻辑，脱敏 trace 可重建调用经过；无用固定成功响应代替真实搜索 |

### 完整真实日期验收

5. 使用新默认机制从 2025-08-18 开始验证，跳过已有 output 的日期；原主目录的 output 不删除、不覆盖。完整运行有全部必要行情覆盖的真实日期，不裁剪新闻、候选或校验样本。若行情覆盖不满足计分要求，先按既有计分规则核对缺口，不能拿准备成功当作全日通过。
6. 模型使用 DeepSeek v4.1，联网校验使用真实 AnySearch，阶段容量保持 20／30／20。先保存 trace、输入／行情／配置指纹、源码及构建身份，再执行正式 prepare、run-day、必要的官方 retry／recover、封存和计分入口。用户已授权实际模型和搜索验收。
7. 可以按用户已授权范围调整本地输出预算，但必须核验服务端支持并记录数值、来源和生效请求；不改模型、Prompt、业务规则或全量输入来掩盖失败。恢复既有成功业务，失败与各轮配置记录保留，人工 retry 单独记录。
8. 通过条件包括：所有原始准备输入有去向；初筛覆盖完整；精筛产出与候选查证匹配；必需独立校验及补充校验全部结束；当天完成封存；正式计分完成且行情来源可核验；当前业务 revision 的执行状态与业务回执一致；收尾确认没有遗留本机任务资源。失败日期只记录为失败，不以部分成功宣布验收通过。
9. 本机并发、客户端在途请求和远端服务行为分别记录。新默认机制的全日证据必须绑定实际运行的构建；旧 launchd 全日结果、本地 HTTP fixture 和公开 SDK 局部探针都不能替代它。

### 已有证据与尚欠验收

| 已有证据 | 能支持的结论 | 不能支持的结论 |
| --- | --- | --- |
| macOS／Linux 公开 SDK 原型：各 7 项 durable Application、2 项真实 Pi／MCP、3 种进程故障探针通过 | 固定版公开 SDK 原型能管理所测资源并应对所测故障 | 默认模块拆分、有限 map、清理 uncertain 和授权竞态已完成 |
| 后续 macOS 默认入口与 controller／CLI 30 项、真实 Pi／MCP 2 项通过 | 未提交 durable 默认接线在该构建和范围内可运行 | 后续源码与此前 Linux wheel 完全一致，或最终跨平台接线已验收 |
| 2025-09-01 完整 NewsAgent 日期通过 | 旧 DBOS＋launchd 路径有真实业务基线 | 新默认机制通过完整业务验收，或历史 SQLite 522 根因已修复 |
| 2025-09-02 已完成官方准备 | 后续真实日期验收有准备基础 | 模型、搜索、封存或计分已执行并通过 |

实施后的新证据必须覆盖所有受改动影响的上述合同。只重跑适用回归，不用无关测试数量替代缺失场景。

## Out of Scope

- 首版多机器执行、网络分区恢复、远程旧资源清理和分布式 exactly-once；本规格仅预留远程存储接口。
- 顶层 Python 和独立监护同时被强杀、机器掉电、主动逃离管理范围的任意第三方进程。约定的顶层 Python 强杀场景仍必须自动回收全部受管后代。
- 证明 Provider 已停止远端推理或计费。本机退出回执只证明本机受管资源退出。
- Windows 支持、完整 OpenClaw Gateway／Worker 平台、完整 NodeWorkerSupervisor 的调度和业务协议。
- 第二套自研持久队列、claim／lease／slot 内核、完整通用进程平台、通用插件 marketplace 或用户选择 owner 的配置。借鉴源码补齐既有 Python 资源管理职责属于本次范围。
- 改变新闻筛选、校验、行业合并、分组、计分、封存及既有业务重试规则；强制三个独立物理线程池。
- 将输出预算修复重新设计为模型选型或配置校验平台；无证据关闭历史 SQLite 522／锁诊断问题。
- 安装、执行、打包或重命名分发 OpenClaw 原包／SDK；为绕过安全拦截改换路径或启动方式；无证据升级 Python／依赖；Git 历史改写。

## Further Notes

- 术语使用项目 GLOSSARY：Application、Run、BoundWorker、WorkItem、WorkPlan、Batch、DurableApplication、执行尝试、执行授权、业务完成回执、资源退出回执和新闻阶段队列。三个新闻阶段有独立队列容量，不称为三个独立线程池。
- ADR-0001 的默认本地持久化及可替换存储继续有效；ADR-0002 的显式输出上限及独立输入预留继续有效；ADR-0003 的第三方库管理、macOS／Linux 和宿主强杀回收合同替代旧系统服务路线。
- 本规格继承 [有限 Worker Batch 规格 #104](https://github.com/linora-u/AgentLoom/issues/104) 和 [持久 Worker／NewsAgent 规格 #114](https://github.com/linora-u/AgentLoom/issues/114) 的外部合同，并更新其执行机制与部署边界。[输出预算任务 #115](https://github.com/linora-u/AgentLoom/issues/115) 保持独立归属。本轮不自动关闭既有 issue 或改写其实施状态。
- OpenClaw 一手依据锁定 commit `59cb44796b0f1f0daa0e37af8b1495abb45d9bc1`：[公开 process SDK](https://github.com/openclaw/openclaw/blob/59cb44796b0f1f0daa0e37af8b1495abb45d9bc1/src/plugin-sdk/process-runtime.ts)、[通用 owned-stdio 原实现](https://github.com/openclaw/openclaw/blob/59cb44796b0f1f0daa0e37af8b1495abb45d9bc1/src/process/owned-stdio.ts)、[启动取消测试](https://github.com/openclaw/openclaw/blob/59cb44796b0f1f0daa0e37af8b1495abb45d9bc1/src/node-host/node-worker-supervisor.startup.test.ts)、[清理未确认测试](https://github.com/openclaw/openclaw/blob/59cb44796b0f1f0daa0e37af8b1495abb45d9bc1/src/node-host/node-worker-supervisor.cleanup-result.test.ts)、[恢复和强杀测试](https://github.com/openclaw/openclaw/blob/59cb44796b0f1f0daa0e37af8b1495abb45d9bc1/src/node-host/node-worker-supervisor.recovery.test.ts)。完整 Worker 测试用于核对机制，不能直接充当 AgentLoom 接入结果。
- 推荐实施顺序为：提炼默认装配与资源生命周期边界；移除执行器／NewsAgent 对具体实现的依赖并接入有限 map；对齐启动取消及未确认清理机制；完成 macOS／Linux 公开故障矩阵；最后运行新机制下的完整真实日期。每一步都保留既有业务和有限 Batch 合同。
- 核心原型基线为 `a5364b8e4b3285a5711099f362a1dacc0e8c90e2`，其上仍有未提交改动。未来发布与验收必须记录最终 commit／构建，不能把当前 HEAD 当作所有原型代码的完整身份。
