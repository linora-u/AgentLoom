# Spec：统一 Step 观测与大工具结果的持久引用

状态：目标规格，非实现进度报告。对应 [#79](https://github.com/linora-u/AgentLoom/issues/79)。项目整体职责与开发顺序见[能力架构](agentloom-capability-architecture.md)。

## Problem Statement（问题）

运行同一个 Application 时，smolagents 能按 New run、Step、Calling tool、Observations、Final answer 展示过程，pi 目前主要输出启动信息和最终答案。用户无法稳定地看到每轮模型调用、工具结果和失败，也无法用同一种方式理解 Supervisor 与 Worker 的执行。

现有 runtime.log、RuntimeEvent、Hook 诊断和 checkpoint 各有用途，却没有一份证据能同时回答：模型当轮实际收到了什么、Hook 最终允许了什么、工具执行了什么、模型随后看到了哪段结果。大工具输出会让原结果、模型可见的缩减结果和日志脱节。ContextStore 会按容量淘汰条目，也可设置 TTL，因此其引用不能作为可续跑、可事后查看的全文凭据。

用户需要像 smolagents 一样直接阅读运行过程，同时能完整查看保留的执行内容，并从最近安全 checkpoint 继续同一 Task。运行展示、持久记录和恢复状态需要各有明确职责。

## Solution（方案）

pi 和 smolagents 在各自真实的模型、工具执行边界报告同一套执行事实。一个与具体 runtime 解耦的展示器按当前 smolagents 的格式渲染 New run、Step、工具调用、Observations、错误、耗时、token 用量和最终答案。普通工具结果完整显示；大结果先持久保存，再把**同一份**“预览 + 可取回引用”交给模型并显示在 Observations。终端 text 模式与 runtime.log 保留相同的事件顺序、字段和正文；文件日志可以使用无 ANSI 的文本样式。模型回复正文沿用 smolagents 的 DEBUG 可见性。机器输出模式保持现有协议。

独立的必需本地记录保存每轮模型请求与回复、有效 Hook 决策、工具最终参数、未按长度截断的结果及模型可见投影，并用 Task、Run、Agent、Step 和 call ID 关联。大正文放在 Task 范围的普通文件中，索引保存身份、元数据和内容引用，不引入新数据库。Agent 通过统一的受限读取能力分页或搜索；人通过本地内容位置和 Python 检查接口展开 Step。记录写入失败使 Run 失败；未来外部 trace exporter 失败不阻断 Run。

既有 Hook 继续负责工具门禁、输入转换和 Stop 决策；runtime loop 继续负责模型调用、工具调度、重试和 checkpoint。Step 观测不是新的 Hook，也不是新的 AgentRuntime 按 Step 执行接口。

## User Stories（用户故事）

1. 作为终端用户，我希望 pi 与 smolagents 都显示同样的 New run 和 Step 序列，以便逐轮理解执行。
2. 作为终端用户，我希望看到工具的最终授权参数与 Observations，以便知道实际调用和返回了什么。
3. 作为终端用户，我希望普通工具结果直接完整打印，以便不必为常见结果另开文件。
4. 作为终端用户，我希望大结果显示模型实际收到的预览和可用引用，以便日志可读且不会误以为模型读过全文。
5. 作为终端用户，我希望失败、每步耗时及可得的 token 用量出现在对应 Step，以便定位问题和成本。
6. 作为终端用户，我希望只看到一次通过 Stop 与输出校验的最终答案，以便续跑提案不会被误认为完成。
7. 作为终端用户，我希望关闭终端后仍能从 runtime.log 和 Run 的本地记录位置找到相同过程与完整内容。
8. 作为机器输出的消费者，我希望 JSON/JSONL stdout 保持现有协议，不混入人读版 Step 文本。
9. 作为 Application 作者，我希望一次模型轮次和它发起的工具批次构成一个 Step，并行工具按 call ID 区分。
10. 作为 Application 作者，我希望 Worker 有独立 Step 编号并与父 Agent 关联，重试 attempt 与新模型轮次也能区分。
11. 作为 Application 作者，我希望 pi 和 smolagents 的 PreToolUse、PostToolUse、失败 Post 和 Stop 语义一致。
12. 作为 Agent，我希望小结果直接进入上下文，大结果只占用受控的预览空间，并能按需读取全文。
13. 作为 Agent，我希望每个收到的引用都已持久保存且当前 Task 有权读取。
14. 作为 Agent，我希望能按字节分页和有界搜索，即使结果是一行超长 JSON 也能完整取回。
15. 作为检查运行的人，我希望按 Run 和 Step 查看实际模型请求、回复、工具原结果、模型可见结果及其关联。
16. 作为检查运行的人，我希望记录明确说明模型请求在哪个边界采集，避免把上层草稿误称为 provider 实收请求。
17. 作为 Task 所有者，我希望续跑后的新 Run 仍能读取先前已提交的大结果引用。
18. 作为 Task 所有者，我希望只从最近安全 checkpoint 继续；引用损坏或工具副作用状态不明时明确失败。
19. 作为 Hook 作者，我希望现有 Shell PostToolUse 的 tool_response 输入契约保持不变。
20. 作为维护者，我希望模型失败、bridge 故障、被 Hook 阻止的调用与真正的工具失败分开记录。
21. 作为维护者，我希望 pi bridge 不因平台工具全文二次传输而超过帧上限。
22. 作为维护者，我希望必需本地记录故障使 Run 明确失败，外部观测服务故障则只留下诊断。
23. 作为集成作者，我希望未来 Langfuse 等组件能消费稳定、与 runtime 无关的事实和内容引用，并自行决定导出字段。
24. 作为审查者，我希望通过 Application 公开运行入口验证上述行为，而非仅检查私有回调或渲染对象。

## Implementation Decisions（实现决策）

### 1. 执行对象、Step 与模型边界

- Application 定义负责选择模型与 runtime；构造时把模型绑定交给具体 Agent runtime。运行时由 pi 或 smolagents 的 Agent loop 决定何时向模型发请求、何时调用工具。观测只读取已发生的执行事实，不把模型接入误画成一个在 Agent loop 之前执行的步骤。
- Task 是可跨 Run 的逻辑任务；Run 是一次执行尝试；Agent invocation 构成 Supervisor/Worker 父子树。Step 在**单个 Agent invocation 内编号**：一次模型轮次及其工具批次为一个 Step，并行调用是该 Step 下以 call ID 区分的子项。模型传输重试记为同一轮次的 attempt；输出纠错与 Stop 拒绝后的继续产生后续轮次。另记全局提交序号，不用日志到达顺序推断因果。
- 规范化事实至少携带版本、事件 ID、Task/Run/Agent/父 Agent 身份、Step/attempt、call ID、时间、状态、错误和内容引用。运行时只负责把各自事件映射进这套事实；展示器、检查接口和将来的 exporter 从事实读取，不另建执行真相。
- “实际模型请求”指经 AgentLoom 和 provider 适配层最终转换、送入可观察 provider 传输边界的消息和参数；响应也在对应边界配对记录。记录必须标明 capture boundary，不得把 LiteLLM 输入、pi bridge 投影或其它上游对象无条件声称为网络实际发送内容。若某 provider 暂不能观测最终边界，要明确标成未满足完整采集，不得静默用近似值替代。
- 必需记录与用户 RuntimeEvent sink、Hook 诊断及 Post 观察者分开。模型请求/响应在模型出口记；工具终态在已提交的工具执行出口记；有效 PreToolUse/Stop 决策在门禁出口记。记录失败位于 Post 观察者的吞错范围之外，不能被当成普通日志失败忽略。

### 2. 现有 Hook 接在哪一层

| 现有 Hook | 所在层与作用 | 本规格的最小适配 |
| --- | --- | --- |
| PreToolUse | Tool Gateway 和 pi 原生工具宿主；执行前转换或阻止输入 | 记录最终有效决策和参数；仍须经过严格解码及 CoreToolGuard |
| PostToolUse / PostToolUseFailure | 同一工具执行出口；分别观察已完成和已执行失败的调用 | 关联终态 ToolCallRecord；每个适用调用只派发一次；不让观察者改写结果 |
| Stop / StopFailure | Agent 终止门禁及根运行失败观察 | 记录有效 Stop 决策；只有接受并完成输出校验才展示最终答案 |
| SessionStart / SessionEnd | 根运行生命周期 | 保留现有派发时机，与 Run 身份关联 |
| SubagentStart / SubagentStop | 父 Agent 拥有的 Worker 生命周期 | 保留父子身份，不复制到每个 Worker Step |
| TaskCreated / TaskCompleted | 根 Task 生命周期 | 保留现有派发时机，与 Task 身份关联 |

- 以上是当前全部 11 个 Hook 事件。真正能阻止执行的只有 PreToolUse 和 Stop；其余为观察或生命周期事件。被阻止的工具调用是 blocked，没有工具副作用，也不触发 PostToolUseFailure。模型错误、未知工具和 bridge 协议错误不伪装成“工具执行失败”。
- smolagents 工具和 pi 平台工具仍经 Tool Gateway；pi 原生文件/Shell 工具仍经原生工具宿主。pi bridge 只在 SDK 执行前取得已授权参数；成功或失败后的 Post 由 Python 执行边界派发，避免双重派发。两个 runtime 沿用各自 Agent invocation 的 HookRun；pi 要把真实 Step 编号同步到 Hook 上下文，并验证并行调用及续跑。
- 不新增 Step/Model Hook、通用 SDK Hook 转发层、自动重试 Hook 或新 Hook 配置。Hook 的有界诊断快照也不承担完整审计存储。

### 3. 统一展示与记录的对应关系

- 以当前 AgentLoom smolagents 的 INFO 输出为视觉和内容基准：New run 面板、Step 分隔、Calling tool 参数、Observations、红色错误、Step 耗时和累计/增量 token、最终答案。pi 和 smolagents 使用同一展示实现；pi 单独安装不依赖 smolagents 包。用对照样例验收版式，不复制上游 logger 的整套内部实现。
- Observations 的工具结果正文必须等于**交给模型的工具结果正文**；展示层不能自行再截断或摘要。普通结果全文显示。大结果的模型投影是尺寸、来源、预览、引用和读取说明，日志完整打印这份投影；保留的完整正文在持久内容中查看。时间戳、Step 标题和本地检查位置属于展示元数据，不属于模型结果正文。
- 模型回复正文按现有 smolagents 行为在 DEBUG 展示；实际请求与回复无论日志级别均进入必需本地记录。终端 text 与 runtime.log 保持语义及正文一致，Rich 颜色不要求写入文件。若活动日志会轮转，历史 Step 仍须能从持久事实重建或从保留的日志分段读取。
- Stop 拒绝和输出校验失败显示为继续/失败过程，不打印为成功最终答案；接受后的答案只由一个出口打印一次。

### 4. 大结果、引用与读取

- 使用 Task 范围的不可变普通文件保存未经**长度截断**且按统一策略脱敏的工具结果；Run 记录索引、工具调用与模型投影。它不是数据库，也不借用有容量淘汰和可选 TTL 的 ContextStore。文件与引用不因成功 checkpoint 清理、Run 日志轮转或现有 Run 自动清理而失效；本轮不新增自动 trace 清理。
- 先将正文流式写入临时文件，计算字节数和完整性哈希，原子发布并写索引，成功后才把不透明引用交给模型。引用关联 Task、产出 Run、Agent、Step、call ID、类型、大小与内容哈希；模型只收到引用 ID，不收到磁盘路径。Run receipt 或人读日志给出本地检查位置；Python 接口按 Run/Step 和引用校验后读取。
- 是否投影为大结果由模型上下文预算与 pi bridge 字节预算决定，与终端宽度无关。只做一次模型投影；模型输入、Observations 和记录中的 model_visible_result 复用该值。未按长度截断的工具正文另存为 retained_result。统一脱敏发生在持久化与形成可读取/可展示内容之前；“完整”指脱敏后保留全部非秘密内容，而非承诺恢复已移除的敏感字段。
- 只要能生成引用，就自动向该 Agent 提供同一个只读取回能力；不要求每份 Agent YAML 手动选择第二个工具。复用现有 loom_retrieve_context 入口时，对新旧引用采用明确的版本/格式分流，保持旧引用读取语义。新持久引用限定当前 Task 与授权 Agent 范围；搜索和字节分页均有固定响应上限、续读位置与完整性检查。limit=0 不能变成无限读取，超长单行和 UTF-8 多字节内容可以分段复原。
- pi 平台工具的全文留在 AgentLoom 持久存储；bridge 回传工具完成凭据和模型投影，不再带着同一大正文跨帧二次传输。Shell Post Hook 仍收到其现有的完整 tool_response；若未来要给 Shell Hook 引用输入，需另立兼容性设计。
- 工具有副作用且正文持久化失败时，Run 明确失败并保留可用的已提交证据；不得发布悬空引用。续跑时核对工具提交状态，能证实已提交则复用结果，状态不明则拒绝自动重放。记录并不等于对外部副作用作“恰好一次”的保证。

### 5. 检查、恢复与外部扩展

- Python 检查接口按 Run/Step 返回有序元数据及模型可见内容，并以有界读取展开完整模型请求/回复和工具结果。人读日志标出本地位置；不为此增加 Studio 页面或新 CLI 命令。
- checkpoint 保存继续执行所需的 Agent 状态，trace 保存发生过什么；不能从日志重建 checkpoint。仅从最近安全且已提交的 checkpoint 续跑，先校验引用和已提交工具状态。历史 Step 可完整查看，不承诺从任意 Step time travel，也不承诺外部模型或工具确定性重演。
- 对未来 exporter 预留可注入的事件消费契约，输入为版本化、与 runtime 无关的事实和受控内容引用，默认不连接外部 sink。Langfuse 等组件可自行映射 span 和选择字段；本轮不接任何远端服务。未来导出采用异步、失败隔离和诊断记录；远端究竟发送哪些正文应在具体集成时明确定义，不能从“本地详录”推导为默认上传全文。

## Testing Decisions（测试决策）

- 主验收入口是 Application 公开运行接口及其 text CLI，以同一组外部可见场景覆盖 pi 与 smolagents；检查终端、runtime.log、Run receipt、Python 检查接口、取回能力和续跑结果。沿用现有 Application、pi 协议、checkpoint、Tool Gateway 与输出协议测试的 fixture 风格。测试行为和不变量，不断言私有回调顺序或具体磁盘文件名。
- 对照现有 smolagents INFO/DEBUG 样例验证两 runtime 的 New run、Step、最终参数、Observations、错误、耗时/token 和一次最终答案；验证 Worker 父子关联、并行调用、重试 attempt、输出纠错及 Stop 续跑。JSON/JSONL stdout 不得混入人读版式；pi 单独安装可运行。
- 用普通、小型和超过 bridge 帧上限的大型工具结果（包括多行文本及超长单行 JSON）验证：模型输入与 Observations 的结果正文一致；分页可复原脱敏后的完整结果；缓存淘汰、成功 checkpoint 清理及跨 Run 续跑均不使新引用失效；pi bridge 帧有界；工具只调用一次。
- 验证实际模型请求包含当轮历史、工具定义和最终 provider 参数，并与回复/错误配对；同时断言记录的 capture boundary。使用可观察传输的确定性 provider fixture 做边界验证，再以真实复杂 Application 的模型调用检查端到端 Step、工具和 trace 的可读性，避免仅靠模拟路径宣称完成。
- 验证 PreToolUse 修改和阻止、完成/失败后对应 Post 只派发一次、blocked 无失败 Post、Stop 门禁、pi Hook 的真实 Step 编号。验证模型及 bridge 错误不派发工具失败 Hook，观察 Hook 故障不覆盖工具终态。
- 故障注入覆盖本地记录写失败、内容损坏、引用越权、外部导出适配器失败和已提交工具后中断：本地故障使 Run 失败且不产生不可用引用；外部故障不影响 Run；提交状态不明时不自动重放。验证 Shell Post Hook 的完整 tool_response 契约。

## Out of Scope（本轮不做）

- 新 Step/Model Hook、通用 pi SDK Hook 转发、新工具重试策略、公开的按 Step 执行 runtime API，或重写整套 Hook 机制。
- Studio trace 页面、新 CLI trace 命令、另起一套面向用户的 trace JSONL 协议、Langfuse/OTLP 实际接入。
- 任意历史 Step 的 time travel、外部模型/工具的确定性重放、跨 runtime checkpoint 转换。
- 二进制媒体按文本内联打印；新增自动 trace 清理策略；改变 Shell Post Hook 的输入协议。
- 借 #79 对整个 app 或 execution 目录做一次性重排。职责整理按[能力架构](agentloom-capability-architecture.md)分步处理。

## Further Notes（说明）

- runtime.log 是人读展示，trace 是可检查的执行证据，checkpoint 是可恢复状态，ContextStore 是可淘汰缓存；四者不能互相代替。最终答案与 Step 正文可以由 trace 重建，但恢复不能靠重新打印日志完成。
- “日志与模型看到的结果一致”特指工具结果正文。日志额外的标题、耗时、本地路径，以及 DEBUG 级模型回复，均不属于模型收到的工具结果。
- 当前代码中的局部 recorder 或模型输入记录可以作为实施起点；只有在模型采集边界、Hook 决策、持久引用、两 runtime 展示和安全续跑均通过上述验收后，才可称本规格完成。
