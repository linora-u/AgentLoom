# Spec: 复用 Harness² 核心重构框架 Harness，并接入新闻应用

规格日期：2026-10-22。状态：设计已确认，待实施与验收。

Issue：[AgentLoom #142](https://github.com/linora-u/AgentLoom/issues/142)，标签 `ready-for-agent`。

本规格承接 Application 级 Harness 首版，针对方法层重复实现与上游语义偏差进行重构。开发顺序已确认：先完成框架 Harness 核心及开发评分扩展，再接入新闻应用，最后完成完整任务及跨日期验证。主要测试边界沿用用户此前确认的完整 Application Task 公开执行入口。本次编写与发布规格不启动代码迁移或模型实验。

## Problem Statement

Application 使用者希望通过 YAML 启用 Harness² 机制，让完整的多 Agent 应用围绕同一任务提出改进、实际试跑、比较组合，并将有依据的经验用于后续任务。使用者需要的是可复用、可解释的框架能力，以及在新闻应用上能够真实运行的接入。

目前框架的方法层自行实现了递归、角色提示、证据构造、选择、反思与经验库，没有直接调用上游核心。维护两套相似方法增加了复杂度，并产生实际退化：提案理由虽存档却没有传给后续组合与反思；整个资源快照中的历史文件混入当前候选证据；巨大 JSON 分片增加读取负担；经验又受到额外审核状态和现实生成时间过滤，不能按历史任务顺序正常复用。

使用者难以确认哪些能力遵循 Harness²、哪些是 AgentLoom 适配、哪些是自定义扩展，也难以判断采用了候选是否代表效果改善。继续在当前控制器上增加证据导航、反思补修和记忆晋升流程，会进一步扩大维护成本。

同时，AgentLoom 确有上游不能直接替代的职责：Pi 调用、Application YAML 与完整资源加载、不可变快照、独立业务状态、业务验收、停止与资源清理、恢复和产物发布。重构需要准确划定这些边界，并同时支持用户已要求的开发评分反馈，而不是删除必要保障或将新闻业务写进框架。

## Solution

框架直接复用固定版本的 Harness² 方法核心，替换重复的递归、方法提示词、证据拼装、选择、反思和 Library 逻辑。上游源码随项目保存并实际参与执行，保留来源、许可证和有限适配补丁。框架只负责连接 Pi、Application 资源规则和完整任务执行。

使用者继续通过已有 Harnessing Policy 选择启用、反馈模式、递归方式、有限 k、可修改组件及 Pi 角色执行配置。原生无评分路径和开发评分扩展共用同一个方法核心；开发评分由宿主在完整 Trial 后计算，再以明确标识的开发证据提供给后续改进阶段。

框架完成后，新闻应用接入通用执行与评分接口，保留初筛、精筛、独立校验、冲突处理四类业务职责，允许优化其中的 Agent、子 Agent、Skill、工具及委派关系。按日期完成任务后，下一任务参考同一实验此前有效的 Library 条目。

最终交付需分别说明框架接入是否正确、新闻任务是否完整、经验是否真实复用，以及未参与调优的数据上是否存在效果改善。候选采用不等于效果提升，也不自动修改正式源配置或 Git 分支。

## User Stories

1. As an Application owner, I want Harness enabled through the existing Application YAML, so that I can choose which applications use improvement without changing business code.
2. As an Application owner, I want disabled applications to retain their ordinary execution behavior, so that adopting the refactor is explicit.
3. As an Application owner, I want the complete Application Harness to be the improvement unit, so that changes account for interactions between Agents and subagents.
4. As an Application owner, I want the original objective and Acceptance Criteria frozen, so that optimization improves the method used to satisfy the same request.
5. As a framework maintainer, I want the framework to execute the upstream method core directly, so that I maintain one implementation of the improvement method.
6. As a framework maintainer, I want the upstream revision and local patches recorded, so that I can explain behavior and review future updates.
7. As a framework distributor, I want the copied core and required resources available after installation, so that installed applications work independently of a developer checkout.
8. As a framework maintainer, I want replaced local method branches removed, so that a copied upstream tree does not coexist with another active implementation.
9. As a framework maintainer, I want framework capability delivered before news-specific integration, so that another application can use the same Harness.
10. As an integrator, I want the news application's existing contracts checked during framework development, so that the generic interfaces support a real application.
11. As an Application owner, I want parallel and sequential recursion available through existing configuration, so that I can choose exploration width or successive improvement.
12. As an operator, I want k validated as a finite positive integer, so that the planned experiment has a clear size.
13. As an operator, I want logical stage counts separated from actual model requests and usage, so that I can understand the cost of an experiment.
14. As an Application owner, I want general and domain proposals based on actual baseline evidence, so that proposed changes address observed behavior.
15. As an Application owner, I want candidate configurations to execute the complete task, so that improvement evidence includes real outcomes rather than static opinions.
16. As an Application owner, I want sequential improvement to use the preceding composed configuration and accumulated evidence, so that successive steps are connected.
17. As an Application owner, I want all sequential composed checkpoints eligible for final selection, so that an earlier useful result is retained.
18. As an operator, I want every improvement role executed through Pi, so that the method uses the runtime selected for this integration.
19. As an Application owner, I want existing role YAML references to configure Pi execution, so that model and execution choices remain configurable.
20. As an Application owner, I want full upstream method prompts used by the roles, so that shortened local instructions do not omit important reasoning constraints.
21. As an improvement Agent, I want task materials, editable harness files, outputs, differences and decisions organized separately, so that I can inspect relevant evidence efficiently.
22. As an improvement Agent, I want prior proposal and composition reasoning included with Trial evidence, so that I can assess why an edit was attempted.
23. As an improvement Agent, I want historical experiences distinguished from current-task evidence, so that an old review is not mistaken for the current execution.
24. As an improvement Agent, I want to edit files directly in my candidate workspace through Pi tools, so that I do not return entire file trees in JSON.
25. As an Application owner, I want the edited tree validated and frozen before execution, so that the Trial uses the candidate that was actually reviewed.
26. As an Application owner, I want YAML, prompts, Skills, supporting Skill resources, tools and Agent delegation recognized, so that supported application resources are not silently dropped.
27. As an Application owner, I want invalid resource paths, fixed-field changes and invalid Agent relationships rejected, so that candidate edits stay within the declared boundary.
28. As an operator, I want a legitimate KEEP_BASE distinguished from an error fallback, so that the record accurately describes what happened.
29. As an Application owner, I want composition to keep, revise or drop edits using observed evidence, so that the final configuration need not accumulate every proposal.
30. As an Application owner, I want only delivered, business-accepted and cleaned-up finals eligible, so that a model completion cannot hide an incomplete task.
31. As an Application owner, I want the original baseline excluded from the final selection pool, so that selection follows the agreed upstream method.
32. As an operator, I want k=1 to deliver the single eligible final without a ranking Agent call, so that the method retains upstream behavior.
33. As an Application owner, I want multiple finals compared under anonymous IDs, so that branch names do not privilege a candidate.
34. As an operator, I want no eligible final or a failed selector reported explicitly, so that a fallback record cannot be mistaken for successful adoption.
35. As an Application owner, I want the selected already-executed artifact published idempotently, so that adoption does not rerun the business task.
36. As an Application owner, I want adoption distinguished from permanent configuration deployment and measured improvement, so that delivery has a precise meaning.
37. As an Application owner, I want contrastive mode to keep hidden scores and outcomes out of the improvement context, so that it follows the upstream evidence boundary.
38. As an Application owner, I want development_score migrated in the same delivery, so that my configured development feedback remains usable.
39. As an Application owner, I want both feedback modes to share recursion, composition, selection and reflection, so that scoring does not create another controller.
40. As an integrator, I want a generic trusted evaluator interface, so that the framework does not depend on a news-specific scoring formula.
41. As an operator, I want development scores computed after each complete Trial, so that feedback describes a finished execution.
42. As an improvement Agent, I want allowed development feedback identified with its data partition and Trial, so that its source is explicit.
43. As an Application owner, I want development-mode instructions to acknowledge visible feedback, so that the prompt does not falsely claim there are no scores.
44. As an Application owner, I want hidden validation and test evidence blocked in both modes, so that allowing development scores does not disable the remaining access boundaries.
45. As an operator, I want feedback mode, partition and evaluator identity included in experiment and cache identity, so that results from different feedback conditions cannot mix.
46. As an Application owner, I want scoring feedback to preserve selector anonymity, so that anonymous outputs cannot be identified through metric metadata.
47. As an operator, I want missing score samples distinguished from evaluator failure, so that absent measurements are not reported as zero or silently ignored.
48. As an operator, I want completed business work retained when scoring needs recovery, so that retrying evaluation does not rerun the application.
49. As an Application owner, I want reflection to connect each edit to observed benefit, harm, no effect or confounding, so that stored lessons have traceable support.
50. As an Application owner, I want reflection to produce no Library entry when evidence is insufficient, so that experience accumulation does not require invented lessons.
51. As an Application owner, I want valid experience and its adopted harness available to the next task in the same stream, so that learning is not blocked by an unrelated memory approval process.
52. As an operator, I want Library state isolated by experiment, recursion and feedback conditions, so that concurrent experiments cannot remove or contaminate one another's entries.
53. As an Application owner, I want historical tasks to see earlier entries in stream order, so that real-world creation time does not block historical replay learning.
54. As an Application owner, I want each new task to start from its declared base and consult the Library, so that historical adoption does not silently replace my default configuration.
55. As an operator, I want ordinary reflection failure to preserve selected business delivery, so that an optional missing experience does not invalidate an otherwise completed task.
56. As an operator, I want input resources and candidate identities frozen, so that workspace edits during a long experiment cannot change its conditions.
57. As an operator, I want separate business stores, Runtime state, sessions and outputs for competing Trials, so that one candidate cannot overwrite another's evidence.
58. As an operator, I want credentials bound only to authorized execution capabilities, so that snapshots and evidence remain free of secrets.
59. As an operator, I want Stop to halt new stages and propagate to owned execution resources, so that cancellation is not treated as a retryable candidate edit failure.
60. As an operator, I want physical cleanup distinguished from a stop acknowledgement, so that execution is not reported as fully stopped while resources remain active.
61. As an operator, I want resume to reuse completed role, Trial, selection and publication records, so that interruption does not duplicate paid work or business actions.
62. As an operator, I want incompatible method revisions rejected on resume, so that old archives are not misrepresented as valid new-core cache entries.
63. As an operator, I want failed calls and unknown usage retained accurately, so that incomplete reporting does not appear as free execution.
64. As a news Application owner, I want initial screening, refinement, independent validation and conflict handling retained, so that optimization preserves the four agreed business responsibilities.
65. As a news Application owner, I want each responsibility's Agents, subagents, Skills, tools and delegation optimized, so that the whole application participates within those business boundaries.
66. As a news Application owner, I want complete coverage and retained neutral, abstained and failed cases, so that optimization cannot improve apparent results by dropping difficult inputs.
67. As a news Application owner, I want business inputs constrained by the original evidence cutoff in either mode, so that raw future outcomes do not become business-Agent inputs.
68. As a news Application owner, I want the existing evaluator and frozen pricing inputs reused, so that the migration does not redefine returns or direction accuracy.
69. As a news Application owner, I want dates advanced in chronological order with prior experience visible, so that the daily workflow exercises the continual-learning mechanism.
70. As a news Application owner, I want completed dates and selected publications restored without model replay, so that the daily workflow can resume efficiently.
71. As a framework maintainer, I want public-entry tests reusable without private applications or real credentials, so that generic correctness can be checked in a clean installation.
72. As an Application owner, I want development feedback and independent performance evidence reported separately, so that tuning on a date is not presented as unseen validation.
73. As an Application owner, I want actual method activation and candidate file changes verifiable, so that an installed directory or completed baseline cannot be mistaken for working improvement.
74. As an operator, I want stage failures, NO_ENTRY and unchanged candidates retained in reports, so that the experiment can be assessed without hiding unsuccessful work.

## Implementation Decisions

1. **实施顺序。** 第一阶段重构框架 Harness，包括上游核心调用、Pi、Application 资源与执行适配，以及本轮同时交付的开发评分扩展；第二阶段接入新闻业务；第三阶段完成完整任务、跨日期及恢复验收。第一阶段使用现有新闻接口检查契约适配，同时以公开小型应用证明框架不依赖新闻业务。

2. **唯一的方法实现。** 固定 Google Research Harness² 提交 `1cfe254e8d25b15f37048c3730134686abe60335`，将必要源码、依赖和资源随框架保存及安装。直接调用其调度、递归、证据、方法提示词、候选文件采集、选择、反思、Library 和阶段账本。保留 LICENSE、版权信息、来源、固定版本及有限补丁说明。安装后的运行不依赖开发机的临时上游检出。

3. **删除重复逻辑。** 替换本地独立递归与选择分支、五份简化方法提示词、整文件 edits JSON 往返、巨大 JSON 分片证据路径、自制实验内 Library 审核链与额外反思补修协议。共用一个方法核心与阶段状态来源，宿主日志仅承担执行身份、结果和必要恢复职责。ContextRef 等框架通用能力不因本次重构而被全局删除。

4. **保留必要宿主职责。** 框架 Application 执行适配负责预检、输入准备、不可变资源版本、身份绑定、独立业务状态、一次完整业务执行、原业务验收、清理确认及选中产物发布。沿用现有 Runtime、Durable 和受管资源生命周期，不另造执行引擎。

5. **配置兼容。** 沿用 Harnessing Policy 的 enabled、feedback、recursion、editable 和 roles。启用后按完整 Application Task 进入实验；未启用走普通路径；内部 Worker 和恢复不递归启动新实验。feedback.mode 保留 contrastive 与 development_score；后者要求明确 evaluator 和 partition。recursion.mode 保留 parallel 与 sequential，k 为有限正整数。应用是固定优化单位，不增加单值 scope、额外触发模式或核心实现选择开关。

6. **Pi 接入。** 通用改进、领域改进、组合、选择、反思全部通过 Pi 执行。角色 YAML 保留模型、调用参数、工具能力与执行定义的可配置性；上游生成方法主体及短任务指引。通过有限调用通道适配保留上游的缓存、失败、隔离工作区与文件采集契约，避免将 Pi 冒充其他上游通道，避免默认选择或反思路径回到其他执行器。存量业务 Runtime 的选择仍属于既有 Application 合同。

7. **候选编辑。** Pi 在专属候选工作区内用读取、编辑和写入工具直接修改资源。上游采集修改后的执行配置树，宿主校验后封存为 Harness Snapshot，再执行完整 Trial。候选来源、内容身份、文件差异和实际执行资源能够对应。合法无修改、无效修改重试及异常继承 fallback 树分别记录，停止请求不得被当作可恢复提案错误吞掉。

8. **Application 可编辑资源。** 为上游增加窄的 Application 资源识别与提示片段，覆盖允许修改的 YAML、中文提示文件、Skill 及其依赖资源、工具配置和 Agent 委派关系。完整保留所需资源字节，不因上游路径过滤静默丢失 references 或 assets；被保留的资源不自动获得执行或修改权限。结合字段校验与完整依赖检查，禁止越过固定目标、验收、业务代码、凭据和已声明可编辑组件边界。

9. **证据分工。** 使用上游分文件上下文，将任务源材料、当前可编辑执行配置、Baseline、候选、产物、差异、提案与组合决定、Library 分开提供。来源、Trial、当前性和时间范围可识别；源材料由执行适配明确声明，历史复盘不再因位于快照中自动进入当前配置证据。后续组合与反思必须实际收到前序 notes、失败说明和差异。原始运行证据由宿主留存，上游的单次 observation 截断不得被表述为模型已完整读取全部内容。

10. **递归合同。** 复用上游单模式 `4k` 次完整任务执行和 `3k` 个改进阶段的正常计划；重试、选择、反思以及底层模型调用另记。parallel 从共同基线探索通用与领域候选后组合；sequential 每步使用前一组合配置并保留历史，最终池包括所有组合 checkpoint。停止或失败造成的未执行阶段如实记录，不补造执行回执。阶段规模与应用内部 Worker 并发各自使用原有语义。

11. **业务验收与采用。** 将宿主业务验收、真实产物与资源清理完成映射到上游可交付结果。选择池只包含合格的组合 final；原始 Baseline 不加入交付池。k=1 直接采用唯一合格 final，k>1 复用匿名比较。Composer 可合法保留 base 配置。没有合格 final 明确失败；选择器异常记录的 fallback 仍保持失败或 partial 状态，不能据此报告正常成功。

12. **采用与发布。** 交付被选中的已经执行产物，并记录对应配置、Trial 与决定身份。发布可幂等恢复，不在选择后重新执行业务，不自动修改项目源文件、Git 分支或后续任务默认 base。完整性验收与相对效果评价分开；高开发分不能使未满足业务验收的 final 合格。

13. **评分扩展归属。** development_score 与原生路径本轮一起交付，共用递归、组合、选择、反思和 Library，通过有限的反馈证据与提示扩展接入。框架提供可信评价器绑定与反馈管理，应用提供评价实现。评分保留在宿主反馈记录，不改变上游 RunView 无评分的合同，也不创建另一套控制器。

14. **评分时点与证据边界。** 每个完整 Trial 结束后，由可信宿主计算该 Trial 的允许开发反馈，供之后的提案、组合、选择和反思读取；最终 Trial 也保留评价。contrastive 保留原版无评分提示及上下文；development_score 明确说明允许的开发评分，替换与此冲突的无评分声明。开发反馈单独组织，限定评价器、分区、Trial 和来源，不全局关闭隐藏答案与 grader 访问检查。

15. **评分选择语义。** 开发模式的选择反馈使用与产物相同的匿名编号，并清理会揭示分支身份的元数据。k=1 直接交付语义保持；不加入按开发分自动择优的第二套算法、Baseline 回退或评分提升门禁。采用与开发分提高、稳定收益提高之间不存在自动等价关系。

16. **评分完整性。** 正常无可计分样本保留不可计分状态与样本数量，不伪造成零分或效果提升。评价器执行失败、分区不符或原始评分输入改变明确报错，并保留已完成业务工作；恢复可继续评价，不因此重新运行完整业务或静默切换 feedback.mode。

17. **Library 语义。** 使用上游对逐项编辑效果的反思及有效经验解析，最多形成一条可迁移经验，也允许 NO_ENTRY。有效经验连同采用的完整配置进入同一实验任务流的 Library，不经过 Curated Memory 人工晋升。普通反思调用失败或缺少有效经验时跳过入库并保留已选业务产物；证据构造、泄漏校验或持久化异常保留其真实方法状态与错误。

18. **经验与命名空间。** Library、角色缓存和恢复身份区分 Application、实验流、递归模式与 k、反馈模式、开发分区、评价器身份、核心版本及冻结输入。同一任务流只可见当前任务之前的有效条目；不同实验不共享会被 retain 删除条目的可变库根。历史日期顺序与经验生成的现实时间分离，业务事实截止仍由应用输入合同保障。下一任务从声明的 base 开始，参考 Library 中的先前经验。

19. **隔离与长任务。** Trial 之间隔离业务数据库、账本、Runtime、队列域、会话、输出和写入目标，绑定授权的私有能力，沿用完整资源冻结与防凭据泄漏检查。候选运行期间不修改其冻结配置。一次完整新闻日期是可结束的 Application Task；按日期推进复用前序经验，不增加常驻业务执行中的热修改机制。底层进程隔离不被宣称能回滚远程副作用或重建历史网页版本。

20. **停止、恢复与用量。** 复用上游角色/任务缓存并与宿主原执行身份、清理和发布幂等衔接；停止后停止新阶段，控制拥有的资源并确认退出，保留其他任务。恢复优先核对已持久化成果，复用已完成业务和发布结果。新旧核心记录按版本区分，不把旧记录当作新核心有效缓存；不兼容恢复明确报告，不自动重跑。上游 logical ledger 与实际模型用量分别保留，失败调用计入、重复回执去重、缺报保持 unknown。

21. **新闻适配。** 使用框架公开完整任务与可信评价器接口，复用原新闻准备、全量覆盖、独立校验、证据截止、放弃/中性/失败留痕、封存、评分和发布。保留初筛、精筛、独立校验、冲突处理四类职责，允许配置内调整 Agent 及其委派组织。新闻专属工具约束、数据格式和评价公式留在应用，框架不引用新闻实现。

22. **新闻评分与日期推进。** 复用现有新闻开发评分器及封存结果评价，沿用开发月份检查和评分行情输入哈希，不重新定义收益或方向准确率。日期按既定先后推进，共用该实验此前 Library；保留日期准备、失败留痕与发布幂等的薄适配。业务 Agent 原始输入在两种模式下均遵守截止；开发分指导过的配置和日期属于开发调优证据，不能作为独立未见验证。

23. **文档与交付证据。** 文档明确列出上游原生方法、Pi/Application 必要适配和开发评分扩展，并同步配置说明、安装资源及恢复行为变化。交付提供固定版本、有限补丁、实际核心调用证据、候选编辑与差异、完整任务验收和已知限制。新规格替换首版中冲突的方法与反馈实现说明，继续保留其完整任务、资源隔离和业务验收目标。

## Testing Decisions

1. **已确认的主要边界。** 沿用“公开完整 Application Task 入口 → 最终结果、持久回执、产物与资源状态”作为唯一主要新增测试边界。Supervisor 和程序驱动任务通过这一完整任务合同验证；正式 runner/CLI 只验证真实接线。两种反馈模式、两种递归模式及新闻接入不各建一套测试 API。

2. **测试外部行为。** 使用现有受控模型服务、临时 Application、可注入依赖、真实 Pi 工具会话、受管进程与 Durable 底座。控制模型和外部服务的响应，实际经过 YAML 加载、上游方法、文件读取编辑、候选封存、完整执行、验收及发布。断言任务结果、证据可见性、阶段依赖和资源状态，不锁定内部类、私有字典、提示词全文或包装函数调用次数。

3. **既有先例与核心复用。** 延续完整 Harness Application 测试、Supervisor 公开 runner/CLI 恢复测试、真实 OS 隔离测试、Durable 私有能力传播测试、用量回执测试及新闻完整业务/日序列验收。复用固定上游已有的核心检查；有必要补充时采用相同输入和受控执行器比较原生模式对外结果，不重写一套用于自证的参考算法。方法核心直接复用与旧分支删除另通过依赖和代码审阅确认。

4. **验收矩阵。**

   | 场景 | 必须可观察的结果 |
   | --- | --- |
   | 启用、未启用、配置无效、隔离能力不足 | 普通行为保持；启用进入一次实验；配置和能力错误在模型调用前明确拒绝 |
   | 干净安装及公开小应用 | 无私有应用、私有数据或开发机临时源码依赖；加载实际打包的上游核心与 Pi 资源 |
   | parallel/sequential，k=1 与 k>1 | 正常计划符合 4k/3k；Trial 依赖真实；sequential 读取前一组合版本，选择可覆盖历史组合 checkpoint |
   | 多 Agent、子 Agent、Skill、工具与委派改动 | 候选文件实际改变；完整执行中的加载、委派和工具使用随之改变；固定业务合同保留 |
   | Skill 支持资源与非法路径/字段 | 所需资源字节完整；越界修改明确拒绝；不能通过路径过滤静默产生缺资源候选 |
   | 证据差异、前序决策、历史复盘诱饵 | 后续角色能读到真实 diff 与 notes；历史复盘不被混为当前候选或当前 Trial 事实 |
   | 合法 KEEP_BASE、无效编辑与传输错误 | 合法不变和异常 fallback 分别记录，重试按上游语义执行，结果不被包装为已改善 |
   | 无合格 final、单 final、多 final、选择失败 | 只选择合格组合结果；k=1 无排名调用；无结果失败；失败 fallback 不变成正常成功 |
   | contrastive 加入隐藏评分诱饵 | 改进链路不能读取评分；原生方法行为不因开发评分扩展而改变 |
   | development_score、错误分区与隐藏验证诱饵 | 指定开发分在完整 Trial 后进入后续阶段；其他分区与隐藏验证/测试证据被拒绝 |
   | 匿名候选与开发反馈 | 产物和评分编号一致，反馈元数据不泄露分支身份，final 选择池语义保持 |
   | 无可计分样本、评价器失败与评分恢复 | 不可计分和错误分开；保留既有业务产物；恢复评分不重跑完整业务 |
   | 有效经验、NO_ENTRY、普通反思失败 | 有效经验连同采用配置入库；其余不凑经验；已选业务交付保持可核对 |
   | 两个历史任务、模式/分区并行 | 后任务读到同流前序经验；不被现实生成时间阻断；各流不会互删或读取对方库与缓存 |
   | 下一任务与源项目配置 | 从声明的 base 开始并参考 Library；源配置与 Git 状态没有自动晋升写入 |
   | 冻结后工作区修改、同业务 ID 的竞争 Trial | 各 Trial 使用原冻结输入，业务状态与产物隔离，归档身份与实际执行一致 |
   | 停止遇到提案、工具或业务执行在途 | 不继续新阶段；停止不被吞为候选失败重试；资源退出与 ACK 分别记录 |
   | 候选生成、业务已提交、选择或发布完成后崩溃 | 使用持久结果继续，已完成业务和发布不重复；恢复条件变化明确拒绝 |
   | 重复回执、失败模型调用、未知费用 | 不重复累计，失败消耗保留，未知不能变成已知零费用 |
   | 新闻四类职责与完整输入 | 保留完整覆盖、独立校验及中性/放弃/失败；候选不能缩减任务以提高表面成绩 |
   | 新闻真实评分与跨日期恢复 | 复用原评价和冻结输入；开发模式反馈可见；已完成日期恢复不新增模型调用或重复发布 |

5. **验证顺序。** 先验证框架在公开小型应用上的完整任务、两种反馈及故障恢复，再验证新闻适配，最后执行固定输入的完整新闻日期与至少两个连续日期的经验复用验收。真实业务中没有形成有效经验时如实保留 NO_ENTRY；用受控任务证明 Library 传递合同，不能强制新闻反思制造经验。两种反馈可以分别使用明确配置的验收实验，不要求在一次任务中自动同时运行。

6. **效果证据。** 工程验收报告包括实际版本、配置、执行/恢复记录、候选差异、用量和限制。真实模型实验与受控测试分别报告；已参与开发反馈的日期不作为未见验证。收益或方向准确率提升需要独立数据和适当对照，未观察到改善时明确写出，不影响对工程接入是否通过的单独判断。

## Out of Scope

- 重新设计 Harness² 算法、自动在两种递归模式之间择优或令改进器重写方法核心。
- 另一套递归控制器、评分版控制器、逐阶段测试 API、通用摘要 Agent 或精选记忆审批作为实验内 Library 前置条件。
- 单 Worker 优化粒度、只有一个值的 scope、额外触发模式与可选核心实现开关。
- 自动更新源 YAML、Git 提交/合并、下次默认配置切换、正式版本晋升和回滚产品流程。
- Baseline 备用交付、按开发分最高自动采用、评分提升门禁和预算耗尽另选结果机制。
- 无限 k、无限改进至收敛、实验总金额/token硬配额、预算预留与通用账单系统。
- 向候选开放任意框架、业务或工具源码修改、模型权重训练和通用代码自修改平台。
- 改变新闻四类职责之间的业务阶段图、新建通用工作流引擎或重定义新闻评价公式。
- 常驻应用中途热修改候选、通用网络历史回放、远程副作用回滚、新的延迟评分调度服务或跨机器执行引擎。
- 自动转换全部旧实验归档、未经兼容性检查复用旧阶段缓存或在恢复不兼容时自动重跑。
- 将 ContextRef 等通用框架能力全局移除，或重构与本次 Harness 接入无关的其他 Runtime。
- 用规格发布代替实现、以局部 Worker 成功代替完整任务验收，或把采用与工程测试通过等同于投资效果改善。

## Further Notes

- 本规格是已关闭的 [Application 级 Harness 首版规格 #136](https://github.com/linora-u/AgentLoom/issues/136) 的后续重构要求，遵循领域术语及 ADR-0004、ADR-0005；历史实施与验收记录保留。本规格不表示上游核心替换已经完成。
- **上游直接复用**：递归、完整 Probe、Compose、final 选择、方法提示词、证据分文件与差异、选择后反思、Library 及阶段账本。固定提交下的行为为依据，后续上游更新另行评估。
- **AgentLoom 必要适配**：Pi、Application 资源规则、完整业务执行、快照与隔离、可信业务验收、停止恢复、真实用量和发布幂等。
- **用户明确要求的扩展**：development_score 反馈及普通 YAML 角色执行配置。这些是 AgentLoom 的接口与能力，不称为 Harness² 原生 YAML 参数。
- 上游依据：[Harness² 项目](https://github.com/google-research/harness2)、[固定版本的方法调度](https://github.com/google-research/harness2/blob/1cfe254e8d25b15f37048c3730134686abe60335/harness2/core/schedule.py)、[证据构造](https://github.com/google-research/harness2/blob/1cfe254e8d25b15f37048c3730134686abe60335/harness2/core/context.py)、[选择与反思](https://github.com/google-research/harness2/blob/1cfe254e8d25b15f37048c3730134686abe60335/harness2/core/selection.py)、[Library](https://github.com/google-research/harness2/blob/1cfe254e8d25b15f37048c3730134686abe60335/harness2/core/library.py)。
- 本规格只公布框架和应用接入合同，不包含私有应用源码、数据、凭据或历史运行原文。公开通用验证与本地新闻业务验收分开执行和报告。
