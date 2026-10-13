# Spec: 框架持久 Worker 执行、NewsAgent 接入与输出预算修复

Issue：[AgentLoom #114](https://github.com/linora-u/AgentLoom/issues/114)，标签 `ready-for-agent`。

规格日期：2026-10-12。研究基线：`94491c2d4846e1c648bd00dfd1950a0fc4bd6ed2`。

状态：macOS 本机验收通过（2026-10-14）。有限 Worker batch 已交付；本规格扩展持久执行，不重新定义有限 batch。
按用户最新要求，自研持久内核停止扩展，改为接入成熟开源执行引擎；后端选择及业务同事务接线以
[开源持久执行接入方案](durable-worker-open-source-engine-integration-v2.md) 为当前方案。原始验收目标与失败证据保留；当前 2025-09-01 全日封存和计分通过，见 [验收报告](../reviews/durable-worker-dbos-news-acceptance-20261014.md)。
测试入口沿用用户已确认的 Application 公开调用及正式 runner/CLI，并以完整 NewsAgent
真实日期验证业务接入。遵循已接受的 ADR-0001（默认本地持久执行、可替换存储）和
ADR-0002（显式输出上限、独立输入预留）。

## Problem Statement

Application 开发者已经可以用 `BoundWorker.map()` 执行有限 WorkPlan，但仍需自行实现
持续领取、租约、重启恢复、跨进程容量和业务完成确认。NewsAgent 因此维护另一套线程池、
任务状态与资源收尾路径，难以复用框架的 invocation、Context 和执行回执。

直接把新闻线程池换成有限 map 会改变业务：按日期各开一个 map 不能保持跨日期共享
20/30/20 容量；提前领取未启动任务会消耗租约；按波次等待会延迟补位；长期缓存 Worker
定义会使新尝试读不到当前配置；模型返回成功也不能证明业务工具已完成所有提交。

当前真实验证还发现三个必须一起解决的执行问题：

1. 非 Codex 模型没有配置输出上限时，框架仍注入 16,384。真实初筛有四批耗尽预算，
   其中三批预算全部用于 reasoning。只删除配置默认值仍不够，Pi SDK 会从错误的
   model metadata 补出另一个 cap；截断原因还可能被误归类为普通 Provider 错误。
2. 失租旧尝试的失败写入虽然被拒绝，随后不带尝试身份的 retry 仍能把新尝试重新入队，
   在自动重试上限为三次时启动第四次尝试。
3. 工作池填充空位时没有逐项检查 Stop，停止请求后仍会继续领取；租约失效、停止请求
   被接受和本机 Runtime/Pi/MCP 真正退出也没有统一的完成合同。

用户需要默认在本机直接运行的通用持久执行层，保留 NewsAgent 的新闻判断和失败恢复
业务规则，同时能说明每次工作由谁执行、为什么重试、是否完成业务提交，以及资源是否退出。

## Solution

AgentLoom 提供 Application 可调用的持久 Worker 执行服务，复用有限 map 的 invocation
内核，统一持久任务、尝试身份、领取、续约、自动恢复、共享容量、停止、清理与必需回执。
默认使用本地持久存储，SQLite 为首选候选；存储语义公开且可替换，首版覆盖单机多进程。
开发者通过公开入口提交任务并运行消费者，不再编写线程池、Future 或修改框架私有状态。

有限 `map()` 仍是一份可完整枚举 WorkPlan 的一次 Batch，并发只属于本次调用。持久
服务处理持续到来的任务，容量属于稳定的队列域；两个入口共享 invocation 合同，分别
拥有 admission 和恢复策略，不扩大 map 的含义。

NewsAgent 保留 Planner、StageLedger、新闻分组、阶段衔接、候选判断、正式判断、行业
净方向处理、封存与计分。其阶段工作池接入框架服务，同一 Application/root 的同一
阶段跨日期、跨本机消费者共享初筛 20、精筛 30、校验 20 的容量。

输出预算修复贯穿配置、Runtime、SDK 与最终 HTTP 请求：未指定不由框架补 cap，显式
正整数才限制，覆盖中的 null 可解除继承，输入压缩预留与内部摘要预算独立管理。必须
提供 cap 的协议使用已核实能力或要求显式配置；未知时在请求前报错。

术语补充：**持久任务（DurableTask）**有跨消费者重启的稳定身份；**尝试（Attempt）**
是一次实际领取后执行，具有唯一有效尝试凭据；**容量域**是共享在途额度的稳定队列范围；
**执行回执**分别记录业务提交、invocation 结果和资源清理，不能将三者合并为一个成功位。

## User Stories

1. As an Application author, I want a public durable Worker entry point, so that I can submit persistent work without writing a consumer loop or thread pool.
2. As an Application author, I want finite map and durable execution to share invocation behavior, so that Runtime, Context and cleanup rules remain consistent.
3. As an Application author, I want each map to retain its own concurrency limit, so that adding durable execution does not change existing Batch behavior.
4. As a local operator, I want durable execution to work from the local CLI without an external service, so that I can continue using my current deployment.
5. As an integrator, I want a documented replaceable storage contract, so that I can add a remote backend without changing news rules.
6. As a framework maintainer, I want embedded engines evaluated against the same contract as a local core, so that the backend choice follows evidence rather than habit.
7. As an Application author, I want durable task IDs independent of executor Runs, so that queued work survives consumer restarts.
8. As an Application author, I want duplicate submissions handled idempotently, so that a lost enqueue response does not create duplicate work.
9. As an Application author, I want conflicting inputs under the same task identity rejected, so that idempotency cannot silently replace business work.
10. As an operator, I want task inputs and revisions stored before acknowledgement, so that acknowledged work can be recovered after a crash.
11. As an operator, I want multiple local consumers to claim work exclusively, so that one task has one valid Attempt at a time.
12. As an operator, I want consumers to claim only when capacity is available, so that waiting tasks do not lose leases before execution begins.
13. As a NewsAgent operator, I want dates to share stage capacity, so that running two dates does not double the configured load.
14. As an operator, I want fast completions to refill available capacity immediately, so that a slow sibling does not stall a whole wave.
15. As an operator, I want cleanup and uncertain old resources to keep occupying capacity, so that logical slot release cannot conceal excess local execution.
16. As an operator, I want conflicting capacity declarations rejected, so that consumers cannot disagree about one queue's limit.
17. As an operator, I want Stop checked for each admission, so that filling a window does not keep claiming after shutdown has begun.
18. As an operator, I want stop-request acceptance distinguished from drain completion, so that a CLI response states whether resources have actually exited.
19. As an operator, I want lease loss to invalidate writes and stop the old Worker, so that stale work cannot continue consuming local resources unchecked.
20. As an operator, I want recovery to wait for old local resource exit, so that a replacement does not overlap a still-running Attempt.
21. As an operator, I want uncertain shutdown shown as blocked recovery, so that the system does not report a false clean stop.
22. As an Application author, I want fail, retry, complete and recover guarded by Attempt identity, so that late callbacks cannot change newer work.
23. As an Application author, I want automatic retry limits enforced atomically, so that stale errors cannot exceed the configured budget.
24. As a NewsAgent operator, I want explicit manual retry retained and recorded separately, so that I can intentionally retry failed work after correcting configuration.
25. As an Application author, I want retry policy supplied explicitly, so that whole Worker replay does not become an unsafe framework default.
26. As a NewsAgent maintainer, I want task retry to preserve the current recomputation rules, so that partial results are cleared or retained at the same business boundaries.
27. As an Application author, I want business finalization recorded separately from model completion, so that a successful model reply cannot conceal missing submissions.
28. As an Application author, I want committed business results retained after a Runtime tail error, so that a completed task is not automatically executed again.
29. As an operator, I want mandatory completion writes independent of event observers, so that a broken progress callback cannot lose task acknowledgement.
30. As an integrator, I want backends to declare transaction capabilities, so that switching storage does not silently remove business consistency guarantees.
31. As an integrator, I want reconciliation for separate business and execution stores, so that crashes between commits do not lose completed business work.
32. As an Application author, I want host-controlled per-Attempt dependency binding, so that the model cannot select its owner, session or allowed records.
33. As an Application author, I want fresh Agent, Runtime and mutable state per Attempt, so that retries and siblings cannot share accidental state.
34. As a NewsAgent maintainer, I want tool fixed arguments and classification contracts bound by the host, so that migration retains existing submission authorization.
35. As a NewsAgent operator, I want new Attempts to read current instructions and model settings, so that an official retry can use a corrected configuration.
36. As an operator, I want effective non-secret configuration revisions in receipts, so that local output-budget overrides remain traceable without exposing credentials.
37. As a NewsAgent maintainer, I want business analysis fingerprints distinguished from execution revisions, so that later instruction changes do not automatically invalidate sealed results.
38. As a NewsAgent maintainer, I want screening, validation, grouping, sealing and scoring rules preserved, so that execution migration does not change the strategy.
39. As a NewsAgent operator, I want existing queued and completed work migrated consistently, so that adopting the framework does not discard results or restart completed dates.
40. As a model user, I want an omitted output cap to remain omitted through the final request, so that the framework does not impose a hidden limit.
41. As a model user, I want an explicit positive cap enforced according to the selected protocol, so that I can control one invocation predictably.
42. As a model user, I want null overrides to remove inherited caps, so that I can intentionally restore Provider-native behavior.
43. As a model user, I want conflicting cap aliases rejected, so that extra parameters cannot bypass the documented budget.
44. As a model user, I want input reserve and summary budgets managed independently, so that removing a request cap does not remove room for generation or compaction.
45. As a model user, I want required-cap protocols to use verified capability or fail early, so that unknown limits are not invented by the framework.
46. As an operator, I want output truncation classified consistently across Provider reason variants, so that recovery uses the intended failure category.
47. As a reviewer, I want deterministic faults tested through public Application and CLI behavior, so that private scheduler state is not the acceptance interface.
48. As a reviewer, I want real Pi and smolagents Application evidence, so that unit tests cannot hide broken Runtime or SDK wiring.
49. As a NewsAgent reviewer, I want a complete real date using DeepSeek v4.1 and AnySearch, so that every business phase and the actual search path are exercised.
50. As an operator, I want logical leases, local Runtime concurrency and client HTTP concurrency reported separately, so that measurements do not claim unsupported Provider-side guarantees.

## Implementation Decisions

### 职责与公开入口

1. 框架负责通用持久任务、Attempt、原子领取、续约、自动恢复、容量域、停止与必需执行
   回执；应用负责业务输入、Planner、依赖、失败分类策略与完成验证。持久队列和恢复
   不再永久留在 NewsAgent；框架也不吸收新闻数据库全部业务职责。
2. Application 增加 typed 持久执行入口，表达提交、消费、查询、retry/recover、stop/drain。
   精确命名在实现时确定，合同不得通过 monkeypatch callable、修改私有配置或直接调用
   Executor 才能使用。无需新增 Supervisor Agent。
3. 提取或复用共同 invocation 内核。有限 map 与持久服务均经过同一 AgentInvocation、
   Context/Hook/trace/usage/checkpoint 和 instance resource ownership 路径，不复制第二套
   Run 生命周期。每个消费者执行作用域使用真实 Application Run；每个 Attempt 关联
   实际执行它的 Run，持久任务身份不依赖某个活跃 Python Application 对象。
4. 保留有限 map 的输入快照、按输入顺序结果、单次容量、collect/fail-fast、Stop/drain、
   独立 invocation 和清理合同。两个各为 20 的 map 仍可合计 40；map 不增加自动整
   Worker 重试，不与持久队列隐式共享额度。Runtime 内部 Tool 并发及 Provider 额度独立。

### 本地后端与选择门槛

5. 默认本地持久执行，首版单机多进程，不要求另起 Redis、Postgres 或 Temporal。
   SQLite 为首选本地候选；自建最小核与嵌入 DBOS 尚未被指定为最终路线。
6. 后端选择是第一个实施门槛：自建最小 SQLite 核和 DBOS 公共引擎适配以同一故障矩阵
   做可运行对比，覆盖共享容量、旧 Attempt、资源退出、业务 finalize、配置刷新和迁移。
   比较依赖/升级成本、现有业务事务接入和支持 Python 版本，新增 ADR 记录选择与证据。
   原型可限制任务数，但不能用普通 mock 成功代替故障及真实 Runtime 证据。
7. Storage 合同描述持久 enqueue、幂等身份、条件 claim/renew/终态更新、retry/recovery、
   容量记录及可观察状态，不向应用强制暴露 SQLite connection。默认后端实现该合同，
   并用第二个独立合同实现验证可替换性；首版不要求交付生产远程后端。
8. 存储适配与完整执行引擎适配分开。若采用 DBOS，通过其公开 workflow/queue/transaction
   合同承接执行，不能依赖私有 system database API，也不能另建一套争夺同一任务的
   owner、claim 和自动 retry 调度器。Temporal 留作未来外部引擎参考。
9. 持久 schema 必须版本化。启动检测不兼容版本，迁移失败停止执行并返回明确错误；
   不静默清空数据库或回退内存执行。批量入队失败不得留下未确认的部分业务提交。

### 任务、Attempt 与状态转换

10. 持久任务保存稳定 ID、容量域、Worker 身份、不可变输入或输入引用及指纹、业务
    revision、入队时间、重试策略和当前状态。Attempt 追加唯一 ID、有效 owner/fencing
    凭据、次数、执行归属、配置 revision、lease、结果、错误和清理证据。
11. 相同幂等键与相同输入重复提交返回既有任务；相同键但输入或业务 revision 冲突拒绝。
    需要重算的业务 revision 形成显式新版本，不覆盖已完成任务。只有持久提交成功才
    返回入队确认；确认丢失后重发不得重复工作。
12. claim 与容量占用原子完成，只领取可立即启动的任务。领取失败或提交启动失败必须
    形成可恢复记录，不能遗留无归属的 running，也不能消耗所有 pending 的租约。
13. 状态应表达等待、有效执行、业务已提交、执行失败/完成，以及恢复或清理阻塞。
    具体枚举可以由实现确定，但任务业务状态、Attempt 执行结果、资源状态必须分别可查。
    业务完成不因 lease 过期被恢复为 pending。
14. renew、fail、complete、retry 和 recover 的状态转移都在后端条件提交中校验当前
    Attempt/owner、状态和适用的 lease/revision。旧 owner 的拒绝必须有可观察回执；
    不能先查 owner，再无条件写入或重新入队。
15. 自动失败重试是同一受 Attempt 条件约束的操作：仅有效失败、可重试分类且预算允许
    才能入队，保留失败记录并原子推进次数。NewsAgent 延续现有分类和默认三次自动
    尝试预算，旧尝试不得产生第四次自动尝试。
16. 显式管理员 retry 与自动 retry 区分，校验最新任务状态/revision并记录操作。
    保留当前正式 CLI 对失败任务进行人工重试的能力；不能通过 stale callback 冒充
    管理操作，也不能无声重置历史 Attempt 次数。恢复计数与策略需公开、可验收。
17. 通用服务不默认重放有外部副作用的 Worker；应用显式提供失败/恢复策略。首版不实现
    通用模型轮次 replay。News 新 Attempt 沿用当前整个业务任务重算：按原业务规则
    清理部分条目和下游候选，使用当前定义重算；同一 Attempt 内的工具纠错行为保留。
18. claim 前后、持久完成前后和消费者重启均有恢复路径；已完成任务不重跑，大量 pending
    恢复仍经过正常容量控制。不能把恢复定义为立即批量启动全部失联任务。

### 共享容量、失租与资源退出

19. 容量域有稳定身份，以 Application、执行 root/namespace 和绑定 Worker/阶段区分。
    News 同 root 同阶段跨日期、跨本机进程共享 20/30/20；不同 root 默认隔离。
    每个消费者的本地线程数不能作为共享容量的权威来源。
20. 容量策略复用现有 Worker 执行策略解析并绑定明确 Worker 身份，记录有效值和来源。
    持久服务固定域策略；相同域不同消费者声明冲突时拒绝启动，不能各自用代码覆盖
    创建额外额度。首版不新增 Supervisor policy schema 或在线动态改容量合同。
21. 每项从最终 admission/资源启动到 owned cleanup 完成占用额度。任一快速完成项可
    及时补位，不等待整波最慢项。消费者内工作集有界；任务总量保存在持久存储，
    不为全队列预建 Future、Agent 或 Runtime。
22. Stop 判定参与每一次 admission/claim，并有明确线性化点。停止生效后不再启动新项；
    已领取但尚未启动的项依合同安全退回或记录取消，不伪造 invocation。已启动项保留
    实际结果，停止请求不代表已撤销模型、搜索或业务动作。
23. 续约失败或确认失租后立即使旧尝试失去写入权限，并触发其本机执行/资源停止。
    恢复协调器先确认旧 Runtime、回调、MCP 及归属子进程退出，再释放容量和启动替代。
    lease 到期本身不是安全补位的充分条件。
24. Attempt 资源必须可定位并由正确 owner 停止，归属包含进程创建身份，不能只依赖
    可复用 PID。启动与资源登记、父进程被杀而子进程残留、消费者重启均属于证明范围。
    如同进程线程无法安全中止，保持容量占用及可观察阻塞，不虚报清理成功。
25. 退出超时或资源状态不明时，任务/域显示 recovery/cleanup blocked，保留占用、原因、
    owner 和处理入口。不能靠释放数据库 slot 维持表面吞吐；人工处置也需确认退出。
26. stop/drain 区分“请求已接受”和“已停止”。已有异步 stop 命令可保持接受语义，但必须
    提供公开等待/查询完成入口；只有确认 owned 资源退出才报告 drained/stopped。
    Application 根资源只在所属 invocation 收尾后关闭，不误关其他消费者或 map。
27. 本机进程退出和客户端请求结束不能证明供应商服务端已取消请求。回执分开报告
    持租数、local Runtime scope、客户端 HTTP 在途和 owned 进程，不承诺供应商并行峰值。

### 可信绑定与配置 revision

28. 提供公开、宿主控制的逐 Attempt 准备接口，在该 Attempt admission 后、Agent 启动
    前绑定独立 session、有效尝试凭据、允许提交序号、fixed_args、初筛分类合同、动态
    Stop 和资源归属。宿主依赖不从模型输入反序列化，模型不得覆盖工具固定参数。
29. 准备、执行与清理异常均经过共同 invocation 合同，每次执行 fresh Agent/Runtime；
    session、memory、可变输入、工具绑定、Hook 和 checkpoint 归属不跨 Attempt 污染。
    没有业务待处理条目的任务仍通过业务完成验证，不为其虚构模型调用。
30. 有限 BoundWorker 继续保持绑定快照；持久 News 的新 Attempt 明确刷新当前
    Prompt/Skill/模型/定义，并冻结为该 Attempt 快照。不得长期持有一个旧 BoundWorker
    偷换现有“新尝试读取当前配置”行为，也不允许运行中的 Attempt 随文件编辑漂移。
31. revision 来自脱敏后的有效配置及定义/Prompt/Skill 指纹，包含非秘密本地覆盖，
    特别是输出预算。记录 effective model、adapter、Runtime、cap 与来源，不保存
    Key、Authorization、秘密环境值；秘密改变不通过原始值或普通 hash 泄漏。
32. Attempt 执行 revision 与业务分析新鲜度指纹分开。保留封存后的既有业务规则，不因
    后续 Skill 更新自动废弃封存产物；本地非秘密模型覆盖不得继续从有效配置签名中遗漏。
    迁移后的新鲜度验证必须使用明确版本规则，不能用 stale bypass 掩盖差异。

### 业务完成、必需确认与跨存储一致性

33. Progress/event_sink 是可失败观察者，不能承担必须成功的 ack、业务 finalize、租约
    更新或容量释放。必需持久化失败是执行基础设施错误，停止不安全补位并保留可恢复
    回执；观察者普通异常仍只产生诊断，不能破坏已提交业务结果。
34. 完成由业务工具及完成验证判断。News Worker 仅输出 completed 不足以成功；缺条目、
    无效结果、未 finalize 或未完成补充校验不得推进阶段或封存。
35. 已持久业务 finalize 后发生模型尾部错误、Runtime/trace/cleanup 失败，保留业务
    完成并单独记录执行/清理失败；不自动重算已提交任务。必要时仅协调缺失执行回执，
    资源退出仍阻塞容量释放，不能用业务完成掩盖泄漏。
36. 后端分别声明自身条件状态转移、与业务存储共同事务、引擎恢复能力。共同事务仅在
    实际同事务边界成立时使用；同一数据库文件不等于不同 connection/引擎自动共同提交。
37. 业务与执行分库/远程时明确采用幂等提交、提交回执、transactional outbox 或 reconcile
    协议，覆盖业务成功但执行确认缺失的崩溃窗口。业务提交必须携带并原子验证有效
    尝试/revision，不能先远端读 owner 再本地无条件写入。未提供该能力的后端组合明确
    拒绝对应业务保证，不宣传无条件 exactly-once。

### NewsAgent 接入与迁移

38. 将 News 三阶段正式业务链路的通用消费、租约、Attempt、恢复和容量交给框架，
    替换自有执行池。保留业务 Planner/StageLedger、阶段请求、候选生命周期、来源
    时间检查、industry_net 补充校验、封存、结果投影及 evaluate 计分。
39. 保留现有新闻分组算法、token 批预算和整条新闻分组合同。消费容量与业务分组参数
    在责任上分开，但本规格不重写现有分组行为、不因线程池替换改变输入计划。
    20/30/20 明确绑定三个实际 Worker，不能只靠私有阶段字符串猜配置对象。
40. 保留正式准备、行情复制、run-day、查询、retry、stop、evaluate 使用方式；追加的
    持久执行状态通过已有应用服务投影公开。框架 schedules 仍是到期启动 Application，
    不把它误称为新闻阶段消费者，也不增加第二套业务队列。
41. 迁移覆盖既有 pending/running/failed/completed、Attempt 历史、部分条目、候选与
    已封存结果。迁移期间阻止旧消费者并行领取；先确认旧资源退出，再切换单一执行
    权威。重复迁移幂等，失败停止消费，已完成项不重跑；无需用户清库或创建备份分支。

### 输出预算与错误分类

42. 所有支持 adapter 的配置、直接构造、profile patch、overlay、序列化和缓存保持
    三态：缺失继承；显式 null 解除继承；严格正整数形成 cap。bool、零、负数、非法
    字符串拒绝；None/内部 sentinel 不得被转换成真正零输出或再次补成 16,384。
43. 普通调用最终 HTTP payload 的 cap 合同在 SDK 建参及 extra 参数合并后仍成立。
    支持省略的 OpenAI Chat/Responses/Codex 路径未配置时不发送 cap；显式值使用协议
    正确字段并保留实际数值，不能由 SDK 静默升高、clamp 或由 extra_body 覆盖。
    如端点确有已核实范围限制，提前校验；不支持显式 cap 的端点报明确配置错误。
44. context window、已核实模型最大输出能力、调用方 cap 分开。不得用 context window
    填供应商输出 metadata；SDK 必需 metadata 的来源/未知策略公开。必须 cap 的协议
    用已核实能力并记录来源，或要求用户显式配置；能力未知时在任何网络请求前失败。
45. 统一校验 max_tokens、max_completion_tokens、max_output_tokens 及嵌套额外参数。
    同义同值按所选协议归一为一个字段，不同值或 null 解除与额外 cap 冲突报错。
    保留已支持旧 max_tokens 的迁移合同并记录来源，不能顺手把其旧上下文/输入含义
    重新解释，也不能把旧字段兼容变成隐式默认值。
46. 输入 token limit/压缩预留使用独立有来源的策略，取消请求 cap 不把输入预留清零，
    也不向 HTTP 注入预留值。内部摘要显式预算保留独立来源和优先级，不被普通 profile
    cap覆盖或因 profile 为空一并删除。输入与显式预算组合不合法时明确校验。
47. Pi 与 smol/LiteLLM 支持路径均贯通 nullable cap，消除 int(None)、overlay 丢 null、
    零 sentinel 与 SDK/全局配置补值的违约行为。不能只改 Python 配置默认或仅断言
    Runtime options；最终请求才是上限验收接口。
48. Responses incomplete reason 为 length 或 max_output_tokens、Chat finish_reason=length
    时统一为现有预算耗尽/output_validation 语义，保留 raw reason、usage、effective/wire
    cap 和来源。News 按原失败策略决定重试；普通 Provider 错误不一律改成可重试，
    自动重试不偷偷升预算。未设置 cap 仍可能被供应商截断，不能伪称原生能力已核实。

## Testing Decisions

### 入口与既有测试依据

1. 延续已确认的最高入口：从 Application Run 的公开 Worker 调用执行有限和持久场景，
   再通过正式 Application runner/CLI 验证跨层接线。新增持久能力在同一 Application
   facade 暴露，不以线程池、引擎私有数据库或内部 Future 作为验收入口。
2. 确定性测试使用真实后端、真实本机多进程和可控模型/工具 transport，通过公开任务
   状态、回执、业务产物、最终 HTTP payload 及资源退出断言行为。故障控制可以置于
   底层 transport，但业务入口不绕过 Application，测试不绑定调度器内部类布局。
3. 复用当前 Application batch、真实 Pi/smol Runtime、CLI terminal receipt 和资源退出
   测试模式，以及 News 的阶段恢复、CLI retry、尾部错误不重算、新尝试配置刷新、
   历史来源校验和完整 run-day 测试。原有有限 map 验收保持通过。
4. 使用锁定真实 SDK 与 HTTP transport 验证 wire；mock SDK options 不能代替发送
   payload。支持矩阵分别覆盖 Pi/smol 已支持协议，不要求 Runtime 支持尚未提供的 adapter。

### 必须通过的确定性验收矩阵

| 场景 | 外部行为与证据 |
| --- | --- |
| 入队提交前/后 kill；确认丢失后重复提交 | 未确认事务不产生半批任务；已提交任务可查、可恢复；重复输入只执行一份，冲突输入被拒绝 |
| 两个进程竞争同任务 | 只有一个有效 Attempt，独立 session；旧凭据无法提交、失败或重新入队 |
| 同 root 多日期、多消费者，两个不同 root | 同阶段合计不超 20/30/20；不同 root 隔离；冲突容量声明拒绝；有足够任务时能达到配置容量 |
| 慢首项、快后项、大量 pending | 快项清理后立即补位；不等整波；内存执行工作集有界；恢复不突破容量 |
| Stop 恰在首次 claim 后和循环补位间发生 | Stop 生效后不领取/启动后两项；未启动项没有虚构 invocation；已启动项按实际结果 drain |
| claim 后、资源登记/启动期间 kill | 任务和子资源归属可恢复；无不可追踪孤儿；确认退出前不派替代 |
| 续约失败但 Runtime/搜索仍阻塞 | 旧工具写入被拒绝；收到停止；旧资源退出前保留占用；退出不明公开 blocked |
| 父进程退出但 Pi/MCP 子进程残留；PID 复用 | 只停止原 Attempt 的资源；不误杀他人；残留退出证据完成前不补位 |
| 旧 Attempt 晚到 retryable error，新 Attempt 已到三次上限 | fail/retry 被同一条件拒绝，不能回 pending或自动 claim 出第四次；当前失败和次数保持 |
| 旧 Attempt 晚到 complete、renew、recover、工具提交 | 均无法污染新 Attempt、候选或新业务 revision；拒绝回执可查 |
| 手工 retry 与自动 retry 竞争 | 当前状态条件正确、人工操作可追溯；无双执行；历史次数不被清空，完成任务不被重排 |
| 模型输出 completed 但工具缺失/未 finalize | 业务不完成、阶段不推进、日期不封存；保留失败及缺失条目证据 |
| business finalize 后模型/Runtime/trace 失败 | 已提交业务保持完成、不自动重跑；执行错误仍可查；清理失败不提前释放容量 |
| 业务提交后、框架 ack 前 kill；不同存储组合 | 重启从业务 receipt/outbox 协调，任务不丢失；提交幂等；不能由 stale owner覆写 |
| 外部 HTTP 成功但持久 receipt 前 kill | 明确展示可重复外部调用的窗口；不宣称 exactly-once，不伪造已取消供应商动作 |
| event_sink 抛普通异常；必需持久写入失败 | 前者仅观察诊断，后者显式基础设施错误及安全停止；业务确认不依赖 observer |
| 新 Attempt 前修改 Prompt/Skill/model.local；运行中再修改 | 新尝试读取当前有效配置，运行中快照不漂移；非秘密覆盖进入 revision；凭据不进入公开证据 |
| News 失败/失租任务重算、已完成/封存任务恢复 | 按原规则清理部分结果和候选；已完成不重跑；Skill更新不自动废弃封存产物 |
| schema 升级、重复迁移、迁移失败、旧消费者仍活跃 | 状态/历史/候选保留，重复迁移幂等；不兼容失败停止；旧资源退出前不启新消费者 |
| map 与持久服务同时运行、Application 关闭 | map容量合同不变；归属资源互不误关；根关闭等待所属工作，关闭后不能启动新任务 |

### 输出预算验收

5. 配置矩阵覆盖每个支持 adapter 的省略、null、显式正整数、非法值、继承、解除、
   overlay、序列化/缓存和旧字段兼容。对应构造路径结果一致；alias 和 extra_body 同值
   归一、异值拒绝。未配置与 null 解除经过全链路不变成 16,384 或零。
6. 通过真实 Pi SDK 和 LiteLLM HTTP transport 验证：Chat/Responses 省略时没有输出
   cap；显式 32,768 数值和字段正确；context_window=500,000 不能产生 SDK 补出的
   495,897 cap；Codex省略/显式分别正确。不支持 cap 的订阅端点明确拒绝显式配置，
   必需 cap 协议能力未知时零请求并提前报错。
7. 分别验证普通调用、输入压缩触发、内部摘要请求；取消 cap 后输入预留仍有效，摘要
   显式预算不会被删除/覆盖。protocol 对极小显式 cap 的实际行为必须校验，不能静默
   由 SDK 改值。trace 报告调用方 cap、wire cap、reserve与来源，并通过凭据脱敏检查。
8. 受控响应分别返回 length、max_output_tokens、普通网络错误和业务工具错误，验证
   分类与原 News retry策略。reasoning 耗尽预算但无正文也属于截断；不能把所有 error
   变为可重试或无条件提升 cap。

### 真实 Runtime 与完整 NewsAgent 验收

9. Pi 和 smolagents 各通过真实 Application runner/CLI 完成一个最小持久场景，包含
   真正模型请求、业务工具提交、有限 map 对照、stop/drain 和资源退出。使用真实可用
   配置，保存命令、exit code、产物、脱敏 HTTP字段、usage及 Runtime 证据。支持 Python
   最低版本与项目当前支持高版本均通过相关检查；引擎声明支持不能替代组合运行。
10. 迁移后的 NewsAgent 必须完整运行真实日期 `2025-08-24`：正式 prepare、copy-prices、
    run-day、封存检查与 evaluate，使用 DeepSeek v4.1、Pi/openai_responses 和官方
    AnySearch MCP，保持默认 20/30/20，不裁剪新闻、行业或候选，不降低并发掩盖失败。
11. 固定全量输入和行情副本 SHA，以现有 1,802 原始/1,426 去重新闻为基线验证初筛
    100%覆盖。后续筛选数量和模型判断不要求逐字/逐数相同；所有入选新闻、方向候选
    和必要 industry_net 校验都必须完成，并保留原来源历史可得时间检查。
12. 必须实际产生校验候选，有真实非空 AnySearch 搜索/提取返回及查证来源；空校验、
    只连接 MCP 或打印工具名不算通过。保留工具 error/blocked，不把它们抹成成功。
    输出预算可按已确认合同显式调整，记录有效配置、每次请求与失败恢复，不能伪称
    调本地值等于修复默认语义。
13. 正式 run-day/evaluate exit0、业务封存、结果文件与行情 SHA、计分完整/缺价情况
    均读回验证。分别测量持租、Runtime scope、客户端 HTTP在途峰值和进程退出；所有
    owned资源最终退出。供应商内部并行未知须明确记录。
14. 实施前已完成的全日运行仅作对照证据，不能当作迁移验收。实施后另有隔离的完整
    证据集；保留旧产物，不覆盖原失败、恢复和计分报告。不以模型预测得分是否提高
    判断执行层验收通过，也不要求重新运行本规格编写阶段的模型测试。

## Out of Scope

- 首版多机器分布式执行、自动把远程存储升级成远程执行保证，或引入默认外部服务。
- 通用 Workflow/DAG 产品、模型轮次/Tool turn 的确定性 replay、跨系统无条件 exactly-once。
- 新闻 Prompt、Skill、筛选策略、分组算法、行业净方向规则、计分规则及预测效果优化。
- 将有限 map 改成全 Run 配额、统一所有 Agent-as-Tool/Provider 限流、自动调并发或在线改容量。
- 新的 Supervisor Worker policy schema、强制 Supervisor 编排、重做现有 schedules 服务。
- 通过统一常量猜模型最大输出能力、自动升预算，或顺手删除内部摘要预算与输入压缩策略。
- 与本规格无关的配置校验 Issue、仓库历史改写、分支清理或发布/合并产品代码。

## Further Notes

本规格作为一个整体 Issue 交付，实施顺序为：完整修复预算语义及截断分类；通过后端
选择门槛并记录 ADR；共用 invocation 与可信绑定、持久状态及本机恢复；迁移 NewsAgent；
通过故障矩阵和真实全日验收。单项修复通过不等于整体完成。实现期间可切分内部工作，
不能把“仅替换线程池”或“只删 16K 默认值”作为本规格交付。

有限 batch 的前置合同与已交付工作见 [Issue #104](https://github.com/linora-u/AgentLoom/issues/104)。
本规格延续该合同；Issue #17/#19 的独立配置验证讨论不由本次自动关闭。

仓库研究依据：`GLOSSARY.md`、`CONCURRENCY_ARCHITECTURE.md`、
`docs/research/news-agent-concurrency-integration-review.md`、
`docs/research/durable-agent-execution-open-source.md`、
`docs/research/model-output-budget-default-review.md`，以及已接受的 ADR-0001/0002。
既有测试参考：Application batch/真实 Runtime 验收与 NewsAgent 的
`test_stage_pipeline`、`test_cli_workflow`、`test_run_day_cli`、`test_worker_pools`。

开源依据用于划清机制边界，不表示已选定实现：

- [DBOS 3.2.0](https://github.com/dbos-inc/dbos-transact-py/releases/tag/3.2.0)、[Queue](https://docs.dbos.dev/python/tutorials/queue-tutorial)、[SQLite 与数据库部署](https://docs.dbos.dev/python/tutorials/database-connection)、[并发执行冲突](https://docs.dbos.dev/explanations/concurrent-executions)、[事务](https://docs.dbos.dev/python/tutorials/transaction-tutorial)。
- [Temporal Activity 幂等边界](https://docs.temporal.io/activity-definition#idempotency)、[Activity timeout](https://docs.temporal.io/develop/python/activities/timeouts)。

实施前完整实跑基线：2025-08-24 的 1,426 条新闻全部处理，初筛20、精筛30、校验23个
任务完成，189个候选完成校验并封存；真实 AnySearch有非空返回，正式 run-day/evaluate
均 exit0，本机资源最终全部退出。首轮四批受隐式16K cap影响，用户授权仅把本地cap调至
32K后通过官方retry完成，原16个完成批保持。计分32个信号、23.4375%，只证明执行和
计分流程完成。该结果来自旧 News执行池，不能声称持久框架、默认预算或恢复竞争已修复。

公开 Issue 只包含脱敏规格及摘要。完整输入、原始trace、凭据、私有端点标识和本机
绝对证据路径保留在本地，不复制到公共 tracker。
