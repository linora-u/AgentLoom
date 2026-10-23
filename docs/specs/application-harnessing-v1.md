# Spec: Application 级 Harness 改进与执行

规格日期：2026-10-20。状态：已实现并完成本地验收（2026-10-21）；范围、证据和限制见[验收报告](application-harnessing-v1-acceptance.md)。PR 审查与必需 CI 状态以交付记录为准。

2026-10-22 后续重构已单列为 [Harness² 核心复用与新闻接入 v2](application-harnessing-upstream-reuse-v2.md)。该规格要求替换重复方法实现，并同时迁移开发评分扩展；尚未实施。本文保留首版历史合同与验收记录，后续开发中冲突的方法说明以 v2 为准。

本规范沿用 AgentLoom 领域术语、ADR-0001 的持久执行约束、ADR-0003 的受管资源生命周期，以及 ADR-0004 的完整应用改进决定。

## Problem Statement

Application 可能由多个 Agent、subagent、Skill、工具和程序调度共同完成目标。只优化某个 Worker 的提示词，无法判断其改动是否改善完整任务，也无法可靠处理 Agent 交接、工具分配与编排变化。

用户希望把 Harness² 的机制接入 AgentLoom：在 Application YAML 中启用后，框架围绕已有目标实际执行原配置、候选配置和组合配置，采用一份已经执行的结果，并让后续任务参考有证据支持的经验。用户应能配置反馈方式和探索规模，而不必为每个应用重写改进流程。

当前 Application 生命周期、Supervisor 执行和程序驱动流程的边界不同。简单重复打开应用或切换 YAML，不能保证实际加载完整候选、重复运行完整任务、隔离业务状态，或在崩溃后避免重复提交。缺少这些保证时，结果可能来自混合配置，比较可能受到评分泄漏影响，实际消耗和完成状态也无法解释。

首版需要完成这一执行闭环，同时控制范围：不引入与当前需求无关的版本晋升、总费用配额或多种选择策略。

## Solution

提供 Application 级可配置的 Improvement Harness。未启用的应用继续普通执行；启用的应用在收到一次有限、完整的 Application Task 时，通过统一控制器执行改进流程。真实业务 Agent 和 subagent 会参与基线、对照试跑及组合后的最终执行。

控制器固定原始目标、事实输入、Acceptance Criteria 和改进策略，以完整 Application Harness 为候选单位。候选可以在声明的边界内修改提示词、Skill、工具配置、Agent 组成和委派关系；每份候选都必须通过配置校验并影响真实执行。

首版沿用 Harness² 的通用改进、领域改进、Compose、final 选择和经验积累机制。用户可以选择无标签对照反馈或开发评分反馈，以及有限的并行探索或连续改进。采用仅表示本次交付选择，不自动修改后续任务的基础配置，也不等同于已证明优于 Baseline。

框架提供 Supervisor 驱动及程序驱动的完整任务接入，复用既有执行、持久任务、受管资源和经验处理能力。各 Application Trial 使用不可变资源快照与独立业务状态；恢复沿用原身份和已完成证据。先用小型确定性应用验证通用契约，再以完整新闻任务验证实际接入。

## User Stories

1. As an Application user, I want to enable harness improvement in Application YAML, so that eligible tasks use the mechanism without a separate invocation mode.
2. As an Application user, I want applications without enabled harnessing to retain ordinary execution, so that adopting the feature is explicit.
3. As an Application user, I want the execution Agents to run during improvement, so that I receive an executed deliverable rather than only proposed configuration.
4. As an Application author, I want improvement to cover the entire Application Harness, so that collaboration among Agents and subagents is evaluated together.
5. As an Application author, I want candidates to change prompts and Skills, so that task instructions and reusable procedures can improve.
6. As an Application author, I want candidates to change declared tool bindings, Toolsets and MCP configuration, so that tool use can improve within allowed capabilities.
7. As an Application author, I want candidates to add or remove Agents and change delegation relationships, so that the mechanism can improve the actual Agent Topology.
8. As an Application author, I want validated orchestration configuration to drive program-controlled execution, so that configuration changes have observable effects.
9. As an Application owner, I want the original Goal, inputs and Acceptance Criteria fixed across candidates, so that improvement cannot redefine success.
10. As an Application author, I want Supervisor and program-driven tasks to share one improvement entry, so that business code does not implement another improvement loop.
11. As an Application operator, I want unsupported applications and invalid policies rejected before model execution, so that an enabled flag cannot conceal missing execution capabilities.
12. As an Application author, I want nested Worker calls and retries to stay within the current trial, so that they do not recursively start experiments.
13. As an Application operator, I want task preparation to happen once before comparison, so that repeated trials use the same original facts.
14. As an Application operator, I want immutable snapshots of all candidate resources, so that live workspace edits cannot change an active or resumed trial.
15. As an Application operator, I want candidate identities independent of temporary locations, so that relocating identical resources does not invent a different configuration.
16. As an Application operator, I want separate trial processes, business state and tool sessions, so that competing candidates cannot consume or overwrite each other's work.
17. As an Application operator, I want credentials bound outside candidate archives, so that experiment provenance does not expose authentication material.
18. As an Application owner, I want real access restrictions for hidden evaluation data, so that file access and tools cannot bypass the feedback policy.
19. As an Application owner, I want contrastive feedback without hidden task scores, so that improvement can follow the upstream mechanism.
20. As an Application owner, I want explicitly configured development-score feedback, so that designated development evidence can guide improvement.
21. As an Application owner, I want business Agents to obey the original evidence cutoff in either feedback mode, so that optimization does not introduce future information.
22. As an Application owner, I want evaluation data used for selection to be identified as such, so that it is not later reported as unseen validation.
23. As an Application user, I want parallel or sequential exploration with a finite positive k, so that I can choose breadth or depth through configuration.
24. As an Application operator, I want the planned full-task execution count explained, so that k is not mistaken for a single model call or a currency budget.
25. As an Application author, I want optional custom improvement roles to use ordinary Agent definitions, so that existing model, prompt, Skill and tool configuration remains sufficient.
26. As an Application user, I want final selection to follow the upstream composed-final pool, so that the integration has one understandable adoption mechanism.
27. As an Application owner, I want every adopted final to satisfy the business delivery contract, so that successful process execution alone cannot qualify a result.
28. As an Application user, I want a valid single final adopted without an unnecessary ranking call, so that k equal to one retains the upstream behavior.
29. As an Application user, I want missing valid finals and selection failures reported explicitly, so that the system cannot silently substitute the baseline or claim an arbitrary winner.
30. As an Application owner, I want the delivered result linked to its exact executed configuration, so that a later rerun cannot inherit an earlier result's acceptance evidence.
31. As an Application operator, I want adoption into the business destination to be idempotent, so that experiment isolation does not cause duplicate publication.
32. As an Application user, I want completion, business acceptance and relative improvement reported separately, so that adoption is not presented as proof of superiority.
33. As an Application owner, I want evidence-supported lessons and adopted configurations retained in a Harness Library, so that later tasks can benefit from previous improvement attempts.
34. As an Application owner, I want reflection to allow no library entry, so that unsupported lessons are not stored merely because an experiment ran.
35. As an Application author, I want each later task to start from its declared base harness, so that library reuse does not silently replace the application's default configuration.
36. As an Application operator, I want known usage and missing measurements recorded distinctly, so that failed calls and unreported costs are not shown as free execution.
37. As an Application operator, I want cumulative usage receipts reconciled by invocation identity, so that repeated events do not inflate reported consumption.
38. As an Application operator, I want to stop an experiment and observe resource cleanup, so that cancellation acknowledgements cannot hide running work.
39. As an Application operator, I want recovery to reuse persisted candidates and completed stages, so that restarting does not regenerate proposals or rerun successful business actions.
40. As an Application operator, I want uncertain external actions reconciled before retry, so that a crash cannot cause an unverified duplicate action.
41. As an Application user, I want completed delivery preserved when later reflection fails, so that an auxiliary failure does not erase the actual business outcome.
42. As a news Application owner, I want complete coverage, independent checking, cutoff evidence and retained abstentions, so that configuration optimization preserves the existing business obligations.
43. As an AgentLoom contributor, I want deterministic acceptance tests through the complete-task entry, so that the framework contract can be verified without paid model calls or private application dependencies.
44. As an Application owner, I want a complete real application acceptance run after deterministic validation, so that fixture success is not confused with proven business effectiveness.

## Implementation Decisions

1. **优化单位与触发。** 完整 Application Harness 是唯一优化单位，不增加 scope 参数。应用级 harnessing.enabled 决定是否启用；启用后每个完整 Application Task 进入改进流程，不增加 trigger 参数。配置检查、生命周期打开、Worker 绑定、子 Agent 调用和任务重试均不单独触发实验。缺省未启用时保留普通执行行为。

2. **配置职责。** Harnessing Policy 由现有 Application 系统配置承载，包含反馈方式、递归方式及 k、允许修改的组件，以及可选的改进角色引用。feedback.mode 只有 contrastive 和 development_score；recursion.mode 只有 parallel 和 sequential；k 必须是有限正整数，拒绝布尔值、零、负数、空值和字符串。示例探索规模不构成用户长期运行预算的承诺。合并后的有效配置必须在运行前可检查并被冻结，未知字段和不支持的能力明确报错，不为单一行为增加参数。

3. **完整任务入口。** 新增或收敛一处完整任务执行边界，负责准备任务、预检、分派普通执行或改进执行、取消与恢复、汇总结果。Supervisor 与程序驱动流程通过同一控制器接入。Application 生命周期接口继续负责生命周期，不能重放调用方任意 Python 上下文主体；控制器内部使用一次执行能力，避免公开入口之间递归触发。

4. **执行驱动。** 驱动由框架或应用开发者接入一次，承担任务准备、候选绑定、完整执行和恢复核对。准备结果固定目标、请求范围、原始事实、截止与交付契约；绑定结果明确候选身份、实际资源根、已校验编排和独立状态；执行回执汇总所有关联 Run、业务验收、产物、用量缺报和清理结果；恢复核对报告已完成动作、未完成部分及无法确认的外部动作。一次 Trial 可以包含多个 Run，但比较单位始终是原完整请求。接入必须保留既有锁、授权、租约、上下文与完成回执，不得通过私有快捷调用绕过。

5. **候选权限。** 可修改组件覆盖声明允许的提示词、Skills、tools、toolsets、mcp_servers 与 worker_agents，包括 Agent 增删、工具分配及委派关系。候选不能修改根目标、验收规则、反馈策略、原始输入和隐藏评估。整图预检覆盖引用、接口、资源依赖和既有拓扑约束，不能仅证明 YAML 可解析。改进角色可使用普通 Agent 定义配置模型、提示词、Skill 和工具，未覆盖时使用内置角色；不另建一套模型配置语言。

6. **程序编排接入。** 需要优化的调度行为必须由经过校验的候选配置驱动，并被前台执行、后台推进、恢复和完成判定共同使用。框架和应用为此需要的代码改造属于实施工作。候选自身不因此获得任意修改业务 Python 或工具源码的权限；现有实现中固定的代码与工具版本作为执行依赖记录。

7. **上游递归语义。** 沿用基线执行、通用改进及 Probe、领域改进及 Probe、Compose 及 final 执行的依赖关系。parallel 按 k 扩展候选探索；sequential 基于前一组合配置与累积证据推进 k 步，所有组合 checkpoint 均进入 final 候选范围。标准单模式计划为 4k 次完整任务执行和 3k 次改进会话，选择、反思、评价及重试另计。首版每个实验选择一种递归模式，不增加同时比较两种模式的产品选项。

8. **身份与快照。** 区分任务快照、候选、实验、Trial 与实际调用尝试。实验冻结基础版本、任务、策略、评价口径及可见经验视图；恢复保持原 Trial，独立重复执行使用新 Trial，真实重试保留新尝试身份。候选内容身份基于规范化逻辑相对路径及实际内容，覆盖全部参与 Agent 定义、有效提示词、Skill 完整依赖、有效系统与模型配置、工具绑定和编排配置。归档记录固定应用代码、工具实现及环境版本；凭据正文不进入归档。会话与运行目录等动态绑定信息另记回执，不改变候选内容身份。

9. **实际加载与业务隔离。** 每个 Trial 使用只读候选资源、独立进程、业务账本、持久任务命名空间、产物与工具会话，原始大输入可只读共享。候选资源上下文贯通控制器、受管主机说明、子进程、Application、模型、Skill 与 MCP 加载；固定代码根和候选资源根分别明确，不允许静默回落到工作区实时资源。修改当前目录或沿用旧 Worker 不算完成隔离。业务 revision 保留原业务含义，Trial 隔离通过执行命名空间实现；竞争候选读取同一经验视图。

10. **反馈及访问控制。** contrastive 使用任务要求、轨迹、允许产物和业务验收，不向提出、组合、选择和反思链路提供当前任务隐藏评分或事后结果。development_score 另绑定明确的现有评价器与开发数据分区，允许改进链路使用其评分；业务 Agent 在两种模式下都遵守事实截止。验证数据一旦参与候选排序或继续调优，须标记为参与开发或选择，不能继续声称是独立未见验收。访问限制必须覆盖文件、执行工具、MCP、原生工具、继承凭据和经验引用，不能只依赖提示词或独立进程。应用未具备所需隔离能力时预检拒绝，不静默降级。

11. **业务验收与外部证据。** 固定输入覆盖、必要的独立查证、证据截止及交付语义，不把原 Agent 数量、派生批次或产物文件名误当成不可修改的业务目标。保留中性、放弃、失败及原因，不通过强凑信号数量改善指标。网络资料保留查询、来源、抓取时点、版本线索和错误；今日抓取不能仅凭日期字段冒充历史可见版本。相同原始输入不保证实时网络相同，外部变化须作为比较局限报告。

12. **final 选择与采用。** 正常选择池只含实际执行得到的组合 final，排除原始 Baseline 产物。Compose 可以保留基础配置，但仍须重新完整执行成为 final。k 为一时，唯一 final 通过应用交付检查即可直接采用，不增加排序调用；k 大于一时，在合格 final 范围内用允许证据选择。没有合格 final 或 selector 失败，明确返回失败或不完整，不静默回退 Baseline，也不把第一个结果标为已验证赢家。执行终态、业务验收、相对效果各自记录；采用不代表有相对改善证据。

13. **交付与经验库。** 交付选择结果及其对应已执行候选。需要发布到正式业务空间时，由应用适配层核对任务和产物身份并幂等发布，不以覆盖正式业务数据库导入 Trial，不把重新运行的新产物冒充原已验收结果。采用后执行反思，允许没有可入库经验；有适用经验时记录采用配置、经验及证据。完整配置留在候选归档，现有 Curated Memory 处理机制承载经验与引用。经验保留来源、数据分区和适用时间，按应用及反馈可见性限制复用，防止评分经经验旁路泄漏。后续任务仍从声明的基础配置开始，Harness Library 只为改进器提供参考。

14. **探索预算与用量。** 首版不建设实验总金额或 token 配额，也不增加只能表示“不限制”的预算字段。有限 k 完成后实验结束；现有单次输出、上下文、超时、重试与用户停止约束继续有效。汇总已知 token、请求、耗时和失败尝试，按实际调用身份对账累计事件与终态回执，避免重复计量。未报告保持未知，占位零不代表免费；金额仅在通道可靠报告时展示，不承诺跨提供方精确账单。

15. **停止与恢复。** 复用既有 Durable、检查点和受管资源机制。停止后不启动新阶段，取消在途执行，并分别确认取消请求、持久终态和物理资源退出。恢复先核对原任务、候选、经验视图及持久化受管主机说明，确认旧资源与授权已处理，再恢复原 Trial。先查业务与执行回执，复用已完成候选、阶段、选择和发布；不能只恢复业务队列却重新调用改进器。业务已提交而用量缺报时保留缺报，不能为补账重跑业务。未知外部动作先核对；变化的任务或候选使用新身份。恢复粒度沿用现有任务与检查点，不承诺任意模型指令级续跑。

16. **结果与新闻试点。** 汇总本次任务、实验和各 Trial 的身份、阶段、实际配置、交付位置、业务验收、采用理由、已知用量及缺报、错误和恢复证据。成功发布不因后续反思失败而消失；局部产物也不能使整个实验被标为成功。新闻试点在比较前只做一次原始资料准备，固定原始记录与时间边界，使用同一候选计划推进完整任务，复用现有业务验收与事后评价。报告完整覆盖、方向性结果、中性或放弃、失败、可计分样本和消耗；缺价格、零样本、分母变化或外部条件变化时不单凭高分宣称改善。

## Testing Decisions

1. **主要测试边界。** 用户已确认以“公开完整 Application Task 入口 → 可观察任务结果、持久回执、产物和资源状态”为唯一主要新增测试边界。两类驱动、两种反馈和两种递归模式通过同一边界验证。既有 Application 生命周期、Runtime、Durable 与受管资源测试继续作为底座；不为每个内部阶段、辅助类或调度循环另建测试接口。

2. **测试方法。** 复用现有受控模型服务、可注入 Runtime、临时 Application 项目和真实本地持久执行夹具。确定性响应只控制模型或外部服务行为，实际经过配置加载、Agent 委派、候选绑定、完整执行及回执。用故障注入与受控同步点验证崩溃窗口和清理，不断言私有容器、调用次数实现细节或具体提示词全文。测试阶段数量时核对有业务含义的完整执行记录。

3. **既有测试先例。** 延续当前 YAML 任务序列和 Goal 恢复测试的公开 runner 断言方式、结构化 subagent 的跨 Runtime 契约测试、Durable 提交与命名空间隔离测试，以及受管进程取消和物理退出测试。程序驱动接入复用完整业务运行、封存后恢复和业务提交先于执行确认的现有验收方式，不新建替代生命周期的模拟框架。

4. **必须通过的外部行为矩阵。**

   | 场景 | 可观察验收结果 |
   | --- | --- |
   | 未启用、已启用、未知配置、未接入驱动或隔离不足 | 普通路径保持原行为；启用时进入一次完整实验；无效或缺能力时在模型调用前明确拒绝 |
   | Supervisor 与程序驱动入口，以及内部 Worker 调用和重试 | 两类入口均产生完整任务回执，子调用不触发嵌套实验，不绕过既有锁与授权 |
   | parallel 与 sequential，k 为一及大于一 | 完整执行记录符合阶段数量和依赖；连续改进实际使用前一组合配置；非法 k 在执行前拒绝 |
   | 多层 Agent 委派、Agent 增删、Skill 与工具分配改变 | 实际请求、委派、工具访问和交付发生预期变化，完整业务职责仍满足；无效拓扑被拒绝 |
   | 程序前台推进、后台推进及恢复 | 三者使用同一已验证候选计划，不能恢复到工作区原编排 |
   | 冻结后修改工作区资源，或移动相同候选归档 | 运行及恢复仍读取原资源内容；相同候选内容身份不随临时位置改变 |
   | 同一业务 ID 的竞争 Trial、同一 Trial 恢复 | 竞争状态、账本、队列、会话与产物隔离；恢复复用原身份与已完成记录 |
   | 文件、执行工具、MCP 或经验引用尝试读取隐藏评分 | 实际拒绝访问且保留失败证据；开发模式只向允许链路提供指定开发分区评分 |
   | 完整原始输入、缺失覆盖、无可计分样本或时点不明来源 | 约束保持固定；不合格 final 不可采用；放弃与失败保留；不伪造历史证据或改善结论 |
   | final 池、k 为一、多个 final、无合格 final、selector 失败 | 原始 Baseline 不进入选择；单 final 可直接采用；失败明确报告，不静默回退 |
   | 选中结果发布、发布重试与选后重新运行 | 正式交付幂等且关联确切候选；新输出需要新验收，不覆盖正式业务数据库 |
   | 有效反思、无经验条目、不同评分来源、下一个任务 | 仅合适经验入库且来源可追溯；当前实验内视图固定；后续任务仍从基础配置开始 |
   | 累计 usage、重复终态回执、失败调用、缺报 | 调用消耗不重复累计，失败消耗保留，缺报明确未知 |
   | 停止与模型、工具或子进程在途执行竞争 | 停止新阶段；区分 ACK、持久终态与物理退出，不误停其他 Trial |
   | 候选已生成、业务已提交、执行未确认、选择或发布已记录时崩溃 | 恢复复用已持久化成果；不重新提案或重复成功业务动作；未知外部动作先核对 |
   | 正式交付后反思失败，或只产生局部产物 | 保留真实交付及辅助阶段失败；局部完成不得冒充完整成功 |

5. **通用框架与应用验收。** 通用测试必须在不含私有应用、私有数据和真实凭据的干净环境中完成。Pi 与 smolagents 通过相同的公开任务契约验证实际加载、子 Agent、用量和清理；进程、文件与持久状态隔离不能只用内存模拟通过。程序驱动的新闻验收单独使用现有业务夹具并覆盖完整任务，不把某一 Worker 成功当作新闻应用完成。

6. **完成标准。** 实施交付需报告所执行命令、对应版本、测试矩阵结果及关键回执。先完成确定性小应用和故障注入，再使用一份固定输入的完整新闻请求验收接入。真实模型验证与相对效果评估保留实际消耗、放弃和失败；无法执行的必需验收明确列为未完成，不能以夹具结果代替。本文生成不启动这些模型实验。

## Out of Scope

- 单 Worker 优化粒度，以及只有一个取值的 scope、额外触发模式和候选选择策略参数。
- 自动更新基础 YAML、跨任务活动版本切换、版本晋升、版本回滚或为其预留空接口。
- 将原始 Baseline 作为备用交付池，以及预算耗尽时另行挑选结果的多种策略。
- 实验总费用或总 token 硬配额、预算预留结算、通用精确账单，以及没有总上限时的占位配置。
- 无限 k、持续运行直到目标达成、自动比较两种递归模式，以及改进器重写递归算法。
- 向候选开放任意业务源码或工具源码编辑、模型权重训练和通用代码自修改平台。
- 通用网络回放、远程副作用回滚、重新设计业务评分或新增延迟评价调度系统。
- 新建持久执行引擎、跨机器调度、另一套租约或资源回收机制，以及任意指令级恢复承诺。
- 仅生成 spec 即启动真实模型实验，或将采用结果、成功夹具测试等同于业务效果提升证明。

## Further Notes

**参考 Harness² 的部分：** 通用与领域两类改进、实际 Probe、Compose、parallel 或 sequential 与有限 k、只从组合 final 中选择、单 final 直接采用、选择后反思及 Harness Library、后续任务继续从 base harness 开始、阶段数量与实际用量记录。首版不额外增加实验总费用控制器，也与所核对上游版本的边界一致。

**AgentLoom 必要适配：** Application YAML 接入、完整多 Agent 配置与实际编排、Supervisor 和程序任务驱动、完整依赖快照、业务状态隔离、业务验收、防评分泄漏及停止恢复。这些要求用于使原机制在现有 Application 上可执行、可核对，不宣称是上游开箱即用的能力。

**用户要求的扩展：** development_score 开发评分反馈，以及用普通 Agent 定义配置改进角色。contrastive 是 AgentLoom 对上游反馈行为的配置命名；所有 harnessing 字段属于 AgentLoom 拟议接口，不能当作 Harness² 自带的 YAML 参数。

上游来源为 [Google Research Harness²](https://github.com/google-research/harness2) 与 [论文](https://arxiv.org/abs/2610.14224)。研究固定版本为 1cfe254e8d25b15f37048c3730134686abe60335，避免用后续上游变化隐式修改本规范。

本规范与领域术语、ADR-0004 和研究记录共同表达已确认方向；不要求用户再次决定内部类型名或预先固定每次运行的 k。首次真实新闻运行参数在执行前通过配置确定。本次文档工作不表示功能已实现或通过模型效果验收。
