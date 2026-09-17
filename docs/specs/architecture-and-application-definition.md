# AgentLoom 自有源码职责整理与 Application 定义统一

规格日期：2026-09-17。研究基线：`ca27966d`。本规格合并本次讨论的两个目标：自有源码目录与职责整理，以及 Application 定义、配置和路径解释统一。

状态：实施中，完整最终验收待执行。用户于 2026-09-17 明确调整迁移要求：“不要兼容要彻底重构”，并再次要求最终提交前完成大量单元测试、功能测试和真实 Application 执行。本规格据此取消旧 `src.*` 导入及模块命令的兼容层，统一迁移仓库内调用方。历史运行数据与 checkpoint 的保留、恢复要求不变。写出本规格不代表实现完成或测试通过。

## Problem Statement

作为 AgentLoom 的维护者，用户希望打开项目后，能迅速判断多 Agent 编排、上游 smolagents 适配、Application 定义、工具、Studio 与运行状态分别由哪个 module 负责。当前部分目录按依赖库名聚合了大量 AgentLoom 自有行为；理解和修改一个功能需要跨多个职责不明确的位置查找。

项目依赖固定版本的上游 smolagents，同时维护自己的集成 implementation。AgentLoom 自有的定义解释、Hook、Skill、Supervisor / Worker 调用和持久化行为不应都被理解为上游库代码。

此外，Studio 与运行时对同一份 Application 定义存在重复解释。已读代码表明，两条读取路径对重复 YAML key 的处理不同；Studio 手工维护的配置字段集合与运行时发生漂移；Application 和 Worker 的路径推导也有不同规则。因此，用户在 Studio 中看到的有效配置和诊断不一定与真正运行时一致。

目录调整还会影响既有 Python import、动态工具引用、命令入口、TUI 启动、模板寻址和 Application 定位。仅通过导入检查或静态校验，无法证明真实的多 Agent 任务仍能完成。

用户已明确要求：本地开源参考仓库保留原位，不移动、不清理；功能测试必须创建真实 Application，执行复杂任务，并可复用项目已有 Application。单元测试与功能验证均必须通过后，才能交付实现。

## Solution

把 AgentLoom 自有行为按职责集中到可识别的 module，保留已有有效 seam，并将 Python 对外 package 的目标命名空间统一为 `agentloom`。先统一行为归属，再进行目录和命名空间迁移；行为修复与机械搬迁分别审阅和回滚。最终代码只提供新的命名空间，所有仓库内 Python 调用方、Application 工具引用、测试、命令入口、模板和当前文档一起迁移；不保留旧 `src.*` 导入、旧路径转发或兼容 import finder。

用户进一步选择源码直接放在 `src/` 下：`src/application/`、`src/runtime/`、`src/adapters/` 等，不再增加一层 `agentloom` 目录。源码位置和 Python 导入名称分开：通过标准安装配置将源码目录 `src` 映射为 `agentloom` package，调用方使用 `agentloom.application` 等名称。这是构建布局，不是旧命名空间兼容；发布产物只提供 `agentloom`。

让同一份 Application 定义经过同一套读取、校验、配置合成、来源记录和路径解析。Studio adapter 负责展示，运行 adapter 负责执行，两者消费一致的定义语义。只读检查继续保持轻量，不创建 Agent 运行、不调用模型、不连接 MCP、不执行 Hook。

通过一个新建的复杂多 Agent 验收 Application，以及现有生成测试、仓库分析、上下文检索、checkpoint、Goal 和工具 Application，证明改造前后的行为契约。功能验收同时检查实际运行证据和独立验证结果，不能以模型自述成功代替验证。

## User Stories

1. As an AgentLoom maintainer, I want each module to have an identifiable responsibility, so that I can locate a change without tracing unrelated code.
2. As an AgentLoom maintainer, I want project-owned behavior separated from upstream integration, so that smolagents compatibility work has a clear scope.
3. As an AgentLoom maintainer, I want local reference repositories preserved in place, so that architecture work does not disrupt my research workspace.
4. As an AgentLoom maintainer, I want existing runtime data preserved, so that directory changes do not destroy prior Run evidence or recovery state.
5. As an Application author, I want YAML and Markdown definitions interpreted consistently, so that authoring format does not change validation behavior.
6. As an Application author, I want duplicate configuration keys rejected consistently, so that an accidental duplicate cannot silently change execution.
7. As an Application author, I want existing valid YAML defaults and merge overrides preserved, so that stricter duplicate detection does not reject supported definitions.
8. As an Application author, I want invalid field types reported consistently, so that I can repair a definition before starting a Run.
9. As an Application author, I want global, Application, and Agent configuration precedence applied once, so that overrides behave predictably.
10. As an Application author, I want each effective configuration value to retain its source, so that I can explain why a capability is enabled.
11. As an Application author, I want supported configuration fields recognized by both Studio and execution, so that new fields do not require duplicated lists.
12. As an Application author, I want model selection and fallback to follow the existing model catalog, so that validation and execution select the same profile.
13. As an Application author, I want nested Application paths resolved consistently, so that organizing Applications does not break Worker discovery.
14. As an Application author, I want Worker references resolved independently of package source depth, so that framework directory changes do not change my workflow.
15. As an Application author, I want missing or cyclic Worker references diagnosed before execution, so that invalid topology cannot trigger a partial Run.
16. As an Application author, I want Worker schemas validated consistently, so that my Supervisor receives the intended callable contract.
17. As an Application author, I want repository-owned custom tool references migrated with their implementation, so that Applications execute through the canonical namespace.
18. As an Application author, I want prompt and Skill resources resolved consistently, so that changing launch location does not change Agent behavior.
19. As an Application author, I want Skill loading independent from Hook authorization, so that loading instructions does not silently authorize execution.
20. As an Application operator, I want Hook Plan ordering and source identity preserved, so that runtime policy remains reproducible after refactoring.
21. As an Application operator, I want invalid definitions rejected before Run allocation, so that configuration errors do not create misleading execution evidence.
22. As an Application operator, I want Working Revision and Running Revision kept distinct, so that a running task cannot silently switch definitions.
23. As an Application operator, I want Supervisor and Worker state isolated, so that repeated or concurrent tasks do not contaminate each other.
24. As an Application operator, I want tool failures and policy blocks distinguished, so that the Run report reflects what actually happened.
25. As an Application operator, I want interrupted tasks to resume with the same task identity, so that completed work is retained.
26. As an Application operator, I want resumed tasks to avoid repeating committed side effects, so that recovery does not duplicate file changes or Worker work.
27. As an Application operator, I want Goal budgets to include Worker usage, so that accounting remains correct across delegation and resume.
28. As an Application operator, I want compressed Worker outputs to remain retrievable, so that a Supervisor can verify evidence from long tasks.
29. As an Application operator, I want CLI, Studio, and Python entry points to report the same Run state, so that operational decisions do not depend on presentation.
30. As an AgentLoom integrator, I want one canonical Python namespace and documented entry points, so that the new architecture is explicit rather than hidden behind old forwarding modules.
31. As an AgentLoom integrator, I want canonical imports to use one implementation and shared runtime objects, so that registries and context state are not duplicated.
32. As an AgentLoom integrator, I want installed packages and bundled resources to work outside the source checkout, so that packaging errors are found before release.
33. As a Studio user, I want definition inspection to avoid loading model runtimes and tool implementations, so that browsing Applications remains lightweight.
34. As a Studio user, I want effective configuration and diagnostics to agree with execution, so that the interface accurately explains the next Run.
35. As an AgentLoom contributor, I want deterministic tests through existing interfaces, so that implementation changes do not require rewriting behavior tests.
36. As an AgentLoom contributor, I want a real multi-Worker acceptance Application, so that tests exercise the actual framework instead of only mocks.
37. As an AgentLoom contributor, I want both CodeAct and native tool-calling workflows exercised, so that neither supported execution mode regresses.
38. As an AgentLoom contributor, I want generated tests actually executed, so that creating test files cannot be mistaken for passing tests.
39. As an AgentLoom contributor, I want existing complex Applications included in regression testing, so that established workflows retain their behavior after migration.
40. As an AgentLoom reviewer, I want independently checked artifacts and Run evidence for every acceptance case, so that success claims can be audited.
41. As an AgentLoom reviewer, I want failed attempts and retries retained, so that intermittent failures cannot disappear from the acceptance report.
42. As an AgentLoom reviewer, I want behavior changes and directory migrations delivered in separate batches, so that each batch can be evaluated and reverted safely.
43. As an AgentLoom maintainer, I want open-source references recorded at fixed revisions, so that architectural choices can be traced to inspected implementations.
44. As an AgentLoom maintainer, I want all mandatory checks completed on the delivered revision, so that historical test results cannot substitute for final acceptance.

## Implementation Decisions

### 范围与 module 归属

1. 本规格包含自有源码职责整理与 Application 定义统一。开源参考仓库保持原位，忽略配置与研究资产不因本次任务被迁移或删除。Application 定义格式与运行语义、用户配置、运行数据和历史 checkpoint 的契约保持；旧 Python 导入与模块命令按第 20、21 条迁移。
2. 目录迁移以职责是否集中为依据，不以文件长度、目录数量或机械分层为依据。module 的 depth 体现为调用者需要掌握的规则减少；locality 体现为一种行为的修改集中；leverage 体现为同一实现同时服务展示、验证与运行。
3. 目标 Python 命名空间为 `agentloom`，维持单一 Python 分发包。已有 TypeScript TUI 继续独立维护。命名空间迁移在定义与路径行为统一并验证之后执行。
4. 按以下语义组织 module，具体文件拆分在实施时结合实际依赖确定，不提前创建空目录或只转发的新抽象。

| module | 负责的行为 | 归属原则 |
| --- | --- | --- |
| Application | 定义解释、拓扑、有效配置来源、运行入口、Application Run 终态、revision | 定义知识与 Application 身份集中；展示不能另写规则 |
| Agent runtime | Supervisor / Worker 调用、上下文绑定、Hook Run、Goal、Todo、checkpoint、上下文检索 | 保留已形成的运行所有权与隔离能力 |
| smolagents adapter | 上游 Agent 扩展、上游对象转换、确有版本耦合的兼容行为 | 只收纳真实依赖上游对象模型的 implementation |
| MCP / LSP adapters | 外部连接及相应工具适配 | 沿用已有外部行为契约，不借搬迁重写生命周期 |
| Configuration | 项目配置、模型目录、配置来源读取和原有规范化规则 | 由 Application 定义 module 复用，避免展示与执行各自拼装 |
| Tools | 轻量 catalog、选择性 loader、实际工具行为 | metadata 与 implementation 的加载 seam 保持有效 |
| Self-learning | 现有持久化、review、记忆作用域 | 保留已有 locality，不因为实现较大而重新拆散 |
| CLI / Studio bridge / Schedules | 命令适配、展示和调度入口 | 消费 Application 与 Run 的既有 interface，不持有第二份运行真相 |

5. 保留已有 `AgentInvocation`、`ApplicationRunLifecycle`、`ToolCallRecord`、fresh Worker、运行所属工具实例与 Supervisor 串行保护等设计。对拟删除的转发或重复逻辑应用 deletion test：删除后复杂度应消失或集中，而不是分散到更多调用者。
6. 本次只存在一个实际 Agent 引擎 adapter。CodeAct 与原生工具调用是现有执行模式；不为假想的第二框架增加通用 Agent 引擎 interface。

### Application 定义与配置解释

7. 集中 YAML / Markdown 定义读取、规范化、结构验证、有效配置合成、来源信息和 Application / Worker 路径解析。优先复用现有定义读取、规范化对象与有效配置快照，不再维护 Studio 专属字段白名单或另一套 YAML 读取语义。
8. 重复 YAML key 在 Studio 与运行前校验中均拒绝；现有合法 YAML merge 默认值和显式 override 保持支持。Markdown 内嵌定义与正文工作流的现有语义保持一致。解析失败必须保留可定位诊断。
9. 有效配置按现有全局 → Application → Agent 优先级形成。字段的默认值、类型规范化、列表替换与配置特有规则以现有行为契约为准，不统一改成递归合并。Todo、工具集、Skill、MCP、权限、模型选型和上下文相关字段不能被展示路径悄悄丢弃。
10. 保存合成值与原始来源两类信息。只读展示使用可公开的投影，不展示认证秘密；运行时保留合法执行所需的信息。不能为了展示方便过滤后再把残缺值作为运行输入。
11. Application 身份和 Worker 引用基于显式项目上下文及定义来源解析，摆脱 package 文件深度假设。覆盖嵌套 Application、相对/绝对引用、Markdown Worker、缺失目标和递归引用。沿用已支持的路径形式与访问策略。
12. Studio 检查和真正运行共享静态定义语义，但保留阶段差异：静态有效不等于模型或外部工具已连接可用。可静态判断的错误应在 Run 分配前拒绝；外部连接错误必须如实报告为对应运行阶段失败。
13. 保留既有 Working Revision / Running Revision 语义。新调用应重新取得当前定义或经内容身份正确失效的快照；运行中的 Application 不受编辑热切换。不得顺带承诺新的历史版本回放能力。
14. 模型引用、默认 profile、summary profile 和条件就绪要求沿用现有模型目录契约。关联的配置校验 issue 不作为覆盖已有规则的依据；实施时以当前代码、文档和已接受决策核对差异。

### Hook、工具及只读 seam

15. 遵守已接受的 Hook Runtime ADR：Skill 与 Hook 独立；Hook Spec 仅来自显式配置或显式 Hook Bundle；原始来源保留到 Hook Plan；Hook Plan 不可变；每次调用拥有独立 Hook Run。
16. Hook 配置按稳定 ID 完整替换或禁用，不进行部分字段合并；Handler 顺序确定。变换后的工具输入必须重新严格解码，之后经过 `CoreToolGuard`、最终输入记录、实际副作用与结果观察。blocked 不能伪装成 completed 或一般工具失败。
17. 只读定义检查不加载模型运行、具体工具 implementation，不连接外部进程，不执行 Shell Hook，也不分配 Run 存储。Tools catalog / loader 以及懒加载导出的现有 seam 必须保留。
18. 不将所有上游类型简单再包一遍。确实需要移动的 smolagents 适配保持原有安装时机与语义，版本继续固定。兼容 patch 的主动替换或上游升级是另一个独立决策。

### 命名空间迁移与分发

19. 在移动前枚举 Python 导出、命令入口、TUI 隔离模式启动、动态工具引用、模板和资源寻址、Application 定位等对外 interface。仓库内调用方全部迁移到新归属，不只检查静态 import。
20. 根据用户最新决定，本次是命名空间的破坏性迁移：移除旧 `src.*` Python package 身份、旧 `python -m src` / `src.tui_bridge` 入口及所有专为旧路径存在的转发、alias finder 和 synthetic namespace。真实 implementation 位于 `src/` 下的职责目录，由标准构建配置作为 `agentloom` package 安装；源码目录名称不构成旧导入支持。全局配置、registry、类身份和 ContextVars 只创建一份。正常的内聚公共导出可以保留，但不能借此恢复旧命名空间。
21. Application 定义格式、`loom` CLI 参数和 Run 返回语义保持可用；仓库内既有 Application 的框架工具引用、内部字符串和生成模板同批迁移。外部集成使用的旧 `src.*` 引用需要升级到文档列出的 canonical 路径，不提供旧导入兼容。历史 checkpoint 的稳定存储协议与基于历史源码 revision 的独立 capsule 不依赖候选代码提供旧 package。
22. 包资源随分发产物正确打包；从源码开发安装、wheel 安装、仓库外工作目录与 TUI 隔离解释器启动均必须可用。启动所需项目上下文必须显式或按已有发现规则解析，不能偶然依赖当前源码目录。
23. 运行存储格式、Run / task 身份、恢复协议和用户数据位置不因目录迁移变化。迁移后的实现必须能读取本任务基线版本产生的恢复材料并完成对应验收。

### 分批交付

| 批次 | 交付 | 进入下一批的条件 | 回滚粒度 |
| --- | --- | --- | --- |
| A：基线与验收载体 | 建立 module 归属和兼容清单，新增真实架构验收 Application，收集现有测试和指定 Application 基线 | 每个必测场景有明确预期、验证器和证据位置；基线缺陷被记录 | 测试与文档独立提交，参考仓库不变 |
| B：统一定义 | 先统一 YAML / Markdown 读取，再统一配置来源、有效值与路径解析，接入 Studio 和运行入口 | 定义一致性矩阵、只读副作用检查与相关真实任务通过 | 行为 PR 分开提交，外部入口保持 |
| C：职责归组 | 按已验证语义移动 AgentLoom 编排与真实上游适配；消除重复解释和浅转发 | 核心 Python / TUI 回归、两种 Agent 模式与复杂 Application 通过 | 可运行的小范围迁移 PR |
| D：命名空间与分发 | 统一目标 package，移除旧命名空间，迁移全部调用方、入口、动态引用和资源寻址 | canonical 安装矩阵、迁移后的既有 Application 与所有最终验收通过 | 机械迁移单独提交，保留上一安装产物 |
| E：最终验收 | 在最终候选 revision 执行全套必要检查并汇总证据 | 下述完成门槛全部满足；无未解释失败 | 失败则修复原因，重新验证受影响范围 |

## Testing Decisions

### 已确认的测试 seam

1. 用户已确认：以现有 Application 执行 interface（`execute_app` 及其 `loom run` CLI adapter）作为主要 seam，验证定义 → Supervisor → Worker → 实际工具 → Run 终态与产物的完整行为。
2. 配置展示的一致性通过现有 Studio bridge 的 `application.detail` / `application.validate` 只读 interface 对照验证。它是必要的第二个观察入口，不复制一套业务测试实现。
3. 解析、类型规范化、配置合成、路径和错误分支的单元测试使用所属 module 的既有 interface。能在上述较高 seam 表达的行为，优先从较高 seam 验证，不新增仅为测试存在的公共入口。
4. 好测试验证可观察的输入、输出、错误、调用副作用和持久化结果；不要求某个私有方法被调用，也不把文件布局、内部 helper 数量、模型措辞或具体思考步骤固定成断言。
5. 预期值独立于被测 implementation：配置 fixtures 有手工审阅的期望值和来源；任务 fixtures 有独立 oracle；产物验证器不能调用同一个待验证配置合成函数来生成“正确答案”。两入口相等但同时错误仍应失败。

### 单元与确定性回归矩阵

清单中的每一行都必须有明确断言和正常/异常对照。使用参数化覆盖实际组合，避免为堆数量而复制测试。新增和受影响分支必须有覆盖证据，但单一覆盖率不能代替行为验收。

| 领域 | 必测行为 |
| --- | --- |
| 定义格式 | YAML、Markdown、空内容、非法顶层、缺少必填字段、工作流字符串和列表 |
| YAML 语义 | 重复顶层/嵌套 key、重复 Hook event、合法 merge 默认值与显式 override；Studio 与运行检查结论一致 |
| 字段规范化 | 可接受的布尔/数值输入、非法类型和枚举、缺省值、合法空值、不能被悄悄忽略的错误值 |
| 配置优先级 | 全局、Application、Agent 的逐级覆盖；缺省、空值、列表替换、Todo、工具集、权限和上下文字段 |
| 来源与隔离 | 有效值对应正确来源；快照不能修改原始配置；两个 Application 的读取与运行配置互不污染 |
| 模型引用 | 显式 profile、默认 fallback、未知 profile、summary 要求和现有条件就绪语义；诊断不含 secret |
| Worker 拓扑 | 有效 schema、缺少 schema、必需输入、无效输入定义、YAML / Markdown Worker、缺失目标、循环引用 |
| 路径语义 | 项目相对、定义来源相对、合法绝对路径、嵌套 Application、缺失资源、路径规范化与现有访问限制 |
| 只读行为 | 全新解释器读取定义和 Studio 信息，不导入模型运行或具体工具；不连接 MCP，不执行 Hook，不创建 Run |
| Hook Runtime | ID 替换、禁用、来源、稳定顺序、变换输入重解码、阻断短路、root/local Hook Run 隔离、fail-open / fail-closed 契约 |
| Tools | catalog 查询与懒加载、动态工具引用、固定参数、参数错误、真实工具结果的 completed / error / blocked 投影 |
| Run 生命周期 | 静态拒绝前无 Run，执行成功/失败/中断/预算限制，终结错误不能继续报告成功；事件与结果一致 |
| revision | Working / Running Revision 独立；相同定义新调用看到当前内容，正在运行的调用不被编辑改变 |
| 多 Agent 隔离 | fresh Worker、并发 Worker、重复 Supervisor 调用、上下文与工具状态隔离、累计 token 归属 |
| canonical 导出 | 新入口的对象身份、类型判断、共享配置与 ContextVars 一致；保持懒加载；分发包不存在旧 src package 或旧命令入口 |
| 安装与入口 | 开发安装、wheel 安装、外部 cwd、CLI、TUI 隔离模式、包资源、脚手架生成的 Application 与动态工具 |
| 历史恢复 | 基线版本产生的 checkpoint 能被候选版本读取并按既有协议恢复，不改变 task 身份和已提交工作 |

必须执行当前 CI 所要求的完整 Python 测试集合，包括 CI 显式纳入的额外验收契约测试；同时执行 TUI tests、typecheck 与 build，以及受影响的 Application 自有确定性测试。迁移后测试收集不能悄悄减少，新增 skip / xfail 不能用来使失败变绿。

### 必须新建的真实复杂 Application

建立名为 `architecture_contract_validation` 的真实 Application，纳入项目可运行的验收资产。至少有一名 Supervisor 和四名职责不同的 Worker，覆盖仓库调查、变更规划、实现/测试生成和独立验证。至少一个 Worker 以 Markdown 定义，另有 YAML 定义；真实执行原有 typed Worker 调用契约。

任务使用可重置的多目录 Python 样例仓库：包含跨 module 调用、多个输入类别、已知预期行为、明确缺陷和有效/无效配置素材。要求 Agent 分析现状、定位缺陷、在专用工作区修改代码、生成有意义的回归测试、真实运行测试，并形成引用实际证据的结构化报告。Supervisor 必须使用各 Worker 的真实结果，不能直接填写预设答案或省略 Worker。

验收要求：

- 原生工具调用和 CodeAct 各运行一个对应 Supervisor 定义，使用真实配置模型；模型和 Worker 不能以固定回答替身代替。
- 实际 trace 显示至少四个不同 Worker 被调用，并能关联 root/local run、工具行为与结果。任务包含跨 Worker 数据传递、真实读写和真实测试执行。
- 独立验证器检查最终代码是否满足预置行为 oracle，所需测试是否真实收集且通过，生成报告是否准确引用产物；禁止删除既有测试或放宽断言来获得通过。
- 将同一有效 Application 放到嵌套位置执行，验证定位和相对资源语义；重复调用验证上下文不串扰。两种模式的必测复杂任务在最终候选上各有连续两次成功记录，失败尝试不得丢弃。
- 为该 Application 生成对应错误变体，验证重复 key、非法 Worker 和错误引用在 Studio 与运行前检查中一致拒绝，且没有模型请求、工具副作用或 Run 分配。
- fixture 数据与工具可确定性生成，但 Agent 推理、Worker 调用、工具执行及 Run 持久化必须走真实框架。

### 现有 Application 的真实功能回归

下面均为必测功能族；每族使用已有 Application 定义或仅为隔离、路径迁移所需的适配，不能用简化到失去原目的的任务替代。

| 编号 | Application / 场景 | 复杂任务与独立验收 |
| --- | --- | --- |
| F1 | 新建 architecture_contract_validation：原生工具调用 | 四 Worker 完成分析、修复、生成测试与验证；oracle、实际测试报告、产物和 Run 终态一致 |
| F2 | 新建 architecture_contract_validation：CodeAct | 完成同等任务；验证 Python 执行、typed Worker、工具参数与结果处理；与 F1 按行为比较 |
| F3 | Unit Test Studio | 跑完五个 Worker 的完整链路，为至少两个有分支和异常输入的函数生成测试；宿主再次执行生成的 pytest，断言 collected > 0、failures = 0、errors = 0，且必需行为场景存在。仅检查文件含 pytest 或 parametrize 不算通过 |
| F4 | Repo Map | 对包含至少三层目录、多个 module 和跨目录引用的受控仓库完成扫描、排名、Worker 架构分析和 Skill 产物；独立核对符号与引用可解析，关键已知关系被覆盖，报告与产物非空。不得只执行不调用 Agent 的前两步 |
| F5 | ContextEngine 的 text / json / multi-worker 验收 Application | 全部三类真实执行；确认预期隐藏记录可通过实际 ContextRef 检索，引用独立且来源正确，检索事件和结果对应，而不只匹配最终 PASS 文本 |
| F6 | 复杂 checkpoint Supervisor / Worker 场景 | 分别在 Supervisor 和 Worker 已提交进度处中断，再经公开运行入口恢复；同一 task_id、新 run_id，历史上下文与产物可用，已有写入和已完成 Worker 不重复执行，最终 manifest 与副作用 ledger 正确 |
| F7 | Goal bounded 与 parallel budget 场景 | 有限预算的多阶段任务正常完成；并行 Worker 共享预算触发 budget_limited 后保留恢复材料，再调整本次验收配置并恢复完成；累计 usage 保留，已完成批次不重跑 |
| F8 | Tool Catalog Core 与 Markdown 验收 Application | 两个 Application 都真实执行，覆盖默认工具集和显式替换、Shell / 文件 / 搜索 / Markdown 的实际工具行为，独立读取产物并核对内容及调用证据 |
| F9 | 新建 Application 的拒绝与策略阻断变体 | 静态无效定义在模型调用前拒绝；配置合法而工具被策略阻断时，真实运行产生 blocked 结果且不发生被阻断副作用，状态与诊断一致 |

实施时为每个场景提供可复跑的显式运行入口和验证器。现有脚本中依赖固定解释器、共享临时目录、历史工作区或只检查输出文本的地方，必须先适配到本次受控验收目录，再执行；不能直接清空用户已有输出或运行数据。

真实功能套件使用项目既有模型配置，记录实际模型与执行模式。设置每场景的最长时间和显式运行限制，有限预算场景遵循其预算契约；失败或达到限制必须如实记录。缺少模型配置、网络不可达或依赖不可用不能计作通过，也不能静默退回 mock。

### 什么才算通过

1. 成功类场景同时满足：实际 Run 终态符合预期、Worker / 工具执行证据存在、独立产物检查通过、所有必需测试通过。模型说“完成”或 CLI 返回 0 都不能单独作为证据。
2. 拒绝、阻断、中断和预算限制类场景按约定结果验收。预期的非零退出可以构成场景通过；必须同时核对正确状态、无不应出现的副作用，以及需要时的恢复结果。不得把“全部通过”解释为强制所有场景 exit 0。
3. 为完整 Python/TUI 回归记录通过、失败、错误、跳过数及测试收集变化。必需功能族全部执行；不存在无解释 failure / error，不用删除测试、弱化断言或新增跳过掩盖问题。
4. 所有验收记录绑定候选代码 revision、Application 内容身份、模型/模式、配置摘要、开始结束时间、退出状态、Run / task ID、独立断言结果以及可定位的产物和日志。涉及认证的原始配置不进入报告。
5. 每次失败保留原始证据并定位原因；修复后重跑对应完整场景和受影响回归。外部瞬态故障可在有上限且留痕的策略下重试，不能挑一次偶然成功覆盖失败历史。
6. 基线先记录，不要求把已确认的当前不一致错误作为正确行为固定下来。新增的目标契约回归可以先失败，待实现后转绿；历史独立缺陷必须明确记录和处理，不能与本次成功结果混淆。
7. 最终候选上，F1/F2 各连续两次成功；F3–F9 的所有子场景均通过；全套必需单元/回归、安装和构建检查通过。最后一次有相关代码变更后，必须更新对应验证证据。
8. 任何阻断上述必测契约的问题未解决时，本规格实现不能标记完成。若发现超出原计划但阻塞验收的缺陷，独立提交最小必要修复并增加回归，不把它变成默许跳过。

### 现有测试先例

- 只读导入检查已使用全新解释器验证不加载模型运行；这是继续保持轻量 seam 的先例。
- Tools catalog 与 loader 检查已验证只读 metadata 和选择性 implementation 加载，迁移时保留该方向。
- Application Studio contract、definition validation、Application root discovery 与有效配置来源测试可复用同一组行为 fixtures。
- YAML 重复 key 与 Hook compiler 测试已经覆盖严格解析和合法 merge 的区别；Hook runtime 测试验证输入变换、身份传播及工具副作用顺序。
- ApplicationRunLifecycle 和运行可观察性测试已覆盖终态、checkpoint、事件与错误投影，作为迁移后的稳定行为契约。
- ContextEngine 已有真实 Application 验证器，会检查存储记录和检索事件；应扩展这种独立证据验证方式。
- 复杂 checkpoint 验证已使用副作用 ledger 和历史恢复材料；保留行为目标，改掉与开发机器耦合的执行假设。
- Goal 验收已有完整多 Worker 预算和恢复场景。其历史运行记录是测试先例，不是本次候选的通过证据。
- Unit Test Studio 现有 smoke 对生成文本的检查不足以满足此次要求，必须新增真正执行生成测试和检查预期行为的验收。

## Out of Scope

- 移动、清理或重新组织本地开源参考仓库，包括已忽略的研究 checkout。
- 大规模移动用户 Application、重写 Application 定义格式、迁移运行数据库或删除历史产物。
- 更换 smolagents、同时升级依赖、引入多引擎通用框架，或重写既有 Agent 协作模式。
- 重做 TUI 产品交互、记忆算法、压缩算法、Goal 规则或 Hook 授权模型。
- 新增完整配置检查命令或扩大模型就绪检查到所有未使用 profile；相关已有 issue 单独跟踪。
- 将 MCP 生命周期和 Worker 缓存作为额外的独立重构项目。若其现有缺陷阻断本规格的必测行为，则按验收要求提交必要修复，而非豁免测试。
- 为旧 `src.*` 路径或旧模块命令提供兼容层；用户已要求本次彻底迁移，不另设兼容期。
- 无限时长或无界 token 消耗的压力测试；本规格用有限、可复跑且覆盖实际行为的复杂任务验收。

## Further Notes

- 本规格的两个目标来自已讨论的“自有源码目录/职责整理”和“Application 配置解释统一”；参考仓库搬迁方案已被用户明确否决并移出范围。
- 用户最新要求取消命名空间兼容层。较早的研究清单、Issue 初始描述和阶段提交中若仍提及旧入口兼容，以本规格更新后的要求为准；最终交付必须移除这些中间兼容代码。后续 GitHub 最终推送在完整必要单元、构建、安装、真实 Application 验收与代码审查完成后进行。
- 随后用户明确选择“src/ 下直接放各模块：通过安装配置映射为 agentloom 包”。这取代实施中一度采用的仓库根目录 `agentloom/` 布局；取消旧导入兼容、职责拆分与完整验收要求继续有效。
- 用户已明确确认主要测试 seam：真实 Application 的公开执行入口，加上 Studio 只读入口的配置一致性对照，以及所属 module 的单元测试。
- 本规格交付时只完成研究与规格编写。此前两项只读导入检查通过，仅证明研究基线的轻量导入行为，不构成本规格的完整验收。
- Issue tracker 使用项目已配置的 GitHub Issues；发布标签为 `ready-for-agent`。
- 与现有 [配置分阶段校验讨论 #17](https://github.com/linora-u/AgentLoom/issues/17) 和 [LLM 配置校验目标 #19](https://github.com/linora-u/AgentLoom/issues/19) 关联。本规格聚焦共享定义语义和职责迁移，不自动扩展或关闭这两个 issue。
- 领域语言沿用现有 Application、Supervisor、Worker、Run、Hook Spec、Hook Plan、Hook Run、Skill 与 Goal；实施中如新增稳定领域概念，应同步维护项目领域词汇和必要的 ADR。

已核验的开源依据：

1. [smolagents v1.26.0 的多 Agent 示例](https://github.com/huggingface/smolagents/blob/12c1bc820eca50ace6f80a21d90426d41d74f845/docs/source/en/examples/multiagents.md) 与 [MultiStepAgent](https://github.com/huggingface/smolagents/blob/12c1bc820eca50ace6f80a21d90426d41d74f845/src/smolagents/agents.py#L268)：沿用真实 Agent-as-Tool / managed agent 扩展能力；保留 AgentLoom typed Worker、Hook 与持久化契约。
2. [OpenAI Agents SDK 的 Agent 定义](https://github.com/openai/openai-agents-python/blob/58a6d2c932810fcf9cb7a1a5a68e666ff04553ed/src/agents/agent.py#L295)、[Runner](https://github.com/openai/openai-agents-python/blob/58a6d2c932810fcf9cb7a1a5a68e666ff04553ed/src/agents/run.py#L259) 与 [RunContext](https://github.com/openai/openai-agents-python/blob/58a6d2c932810fcf9cb7a1a5a68e666ff04553ed/src/agents/run_context.py#L72)：借鉴定义、执行和按次状态的归属；不假定其浅拷贝提供不可变定义或线程隔离。
3. [OpenCode v1.18.3 的独立 TUI package](https://github.com/anomalyco/opencode/blob/127bdb30784d508cc556c71a0f32b508a3061517/packages/tui/package.json)：保持 TUI、运行行为与 SDK/bridge 分工。AgentLoom 已有上游来源记录，继续记录固定 commit、原始来源、适配理由和复用代码的许可。

交付证据应包括：module 归属与迁移说明、可复跑的新验收 Application、确定性测试报告、真实 Application 逐场景结果、安装/构建结果、失败修复记录及最终汇总。缺少任何必测部分，状态保持“未完成验收”。
