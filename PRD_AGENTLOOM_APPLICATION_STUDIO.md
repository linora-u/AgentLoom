# AgentLoom Application Studio PRD

**状态：** Ready for Agent
**日期：** 2026-07-20
**范围：** AgentLoom TUI、内置 Studio Agent、AgentLoom 领域工具、安装与升级体验

## Problem Statement

当前 AgentLoom TUI 没有围绕用户的核心目标组织：用户真正需要的是创建、理解、修改、验证和运行一个 Application，但启动页主要呈现 Recent Runs，已创建的 Applications 缺少清晰入口，导致“能看到一次运行，却找不到被管理对象本身”。

首页的统计口径也不可信。现有 `Agents` 数量把 Supervisor 与 Worker YAML 定义展开后一起计数，容易被误解为已加载或正在运行的 Agent 数；`Skills` 数量只来自部分 Application 目录扫描，却没有说明它不是 Global Skills，也不代表某个 Application 的最终有效 Skills。用户无法从这些数字判断系统实际加载了什么。

详情页把内部 Run ID、原始 Events、JSON 和长文本直接铺开，信息密度高但决策价值低。用户需要快速知道当前状态、失败位置、核心结果、下一步行动、Workers/子 Agent 状态以及日志和产物入口，而不是先理解 AgentLoom 的内部事件格式。

当前 Builder 也不是用户期望的 Agent 开发体验。它只能在内存中暂存 Agent YAML，再通过独立的 `/apply` 写入；不能像 OpenCode 一样持续读取项目、直接修改当前 Application、展示 Diff、校验、运行、分析失败并继续修复。用户必须在聊天、文件编辑和另一个终端之间手工拼接完整工作流。

快捷键和状态反馈缺少可发现性。F6 的作用不清楚且容易与终端冲突；原 `Ctrl+P` 会与 VS Code 的 Quick Open 冲突，且用途没有被准确说明；详情滚动和返回逻辑需要用户猜测。动画与加载状态也没有明确映射到思考、工具执行、校验和运行等真实阶段。

安装文档虽然提供了源码安装命令，但没有建立明确的产品升级路径。用户无法在 TUI 内发现新版本，也不知道重新运行 `./install` 就能更新；可选的 `--no-modify-path` 容易被误认为更新所必需。

## Solution

将 AgentLoom TUI 重构为以对话为中心的 **AgentLoom Application Studio**。它是一个独立的 TypeScript 控制面工具，用于创建和维护 Python AgentLoom Applications，而不是一个由 AgentLoom 自身运行、也不是一个可被自身热替换的普通 Application。

Studio 使用固定版本、随 AgentLoom 一起分发的 OpenCode Runtime，直接复用其 Session、Agent Loop、流式事件、权限、工具调用、子 Agent、历史记录和 Diff 机制。AgentLoom 不重新实现一套相似但逐渐分叉的 Agent Loop。Studio 通过 AgentLoom Plugin/Tool 调用一套位于项目根能力边界中的稳定 CLI/JSON 接口；这些领域工具由 Python 实现，并与 Codex 共享同一个 `agentloom-framework-skill`。

启动时，用户首先看到 `+ New Application` 和完整的 Applications 列表。选择现有 Application 会恢复它自己的持久化 Studio 会话；选择新建则通过同一种对话交互收集目标、输入、输出和验收标准，并生成、校验 Application。Recent Runs、Global Skills 和运行健康信息保留为辅助信息，不再遮蔽 Applications。

用户提出修改后，Studio Agent 在当前权限范围内直接读取和修改文件，随后展示 Diff 并创建可追踪 Revision，不再要求普通流程执行 `/apply`。Studio Agent 以自治 Loop 工作：理解需求、检查现状、修改、校验、试运行、读取结构化状态和日志、继续修复，直到完成标准满足。缺少事实时优先使用工具获取事实；只有缺失业务意图会导致明显不同结果，或操作越权、删除、扩大权限、影响多个 Applications、产生不可逆外部副作用时，才询问用户。

Application 详情展示最终 Effective Config 和来源，而不是只展示原始 YAML。用户可以查看 Supervisor、Workers、Tools、Skills、子 Agent、模型、权限、Hooks、MCP 与文件证据。Run 详情默认只展示可行动摘要；原始 Events、完整 JSON、堆栈和长日志折叠在技术详情中。

## User Stories

1. 作为 AgentLoom 用户，我希望启动 TUI 后首先看到 Applications，从而直接进入我要创建或维护的对象。
2. 作为首次使用者，我希望 `+ New Application` 是启动列表的第一项，从而无需先理解 Run、Agent YAML 或目录结构。
3. 作为已有项目的维护者，我希望看到所有已创建但从未运行过的 Applications，从而不会因为没有 Run 而丢失入口。
4. 作为已有项目的维护者，我希望看到每个 Application 的名称、健康状态、运行状态和最近修改时间，从而快速选择工作目标。
5. 作为用户，我希望 Recent Runs 是辅助信息而不是启动页主体，从而不会把一次执行误认为系统的核心管理对象。
6. 作为用户，我希望首页只显示 Applications 的准确数量，从而不会把 Supervisor 和 Worker 定义数误认为运行中的 Agents。
7. 作为用户，我希望首页显示真正可用于 AgentLoom 运行时的 Global Skills 数量，从而理解全局能力范围。
8. 作为用户，我希望点击 Global Skills 数量后查看具体 Skill，而不是只看到一个无法解释的数字。
9. 作为 Application 维护者，我希望进入 Application 后看到它最终生效的 Skills，从而知道运行时实际加载了什么。
10. 作为 Application 维护者，我希望每个 Skill 标注 Global、Application 或 Agent 私有来源，从而理解覆盖和加载关系。
11. 作为用户，我希望 Studio 自己使用的 `agentloom-framework-skill` 不计入 Application Skills，从而避免控制面能力与运行时能力混淆。
12. 作为用户，我希望 TUI 不扫描并统计机器上所有 Codex、Claude 或其他工具的 Skills，从而保证统计口径稳定且与 AgentLoom 相关。
13. 作为用户，我希望选择一个 Application 后恢复它独立的 Studio 会话，从而继续此前的设计和修改上下文。
14. 作为维护多个 Applications 的用户，我希望不同 Application 的对话历史彼此隔离，从而避免错误地把一个项目的需求应用到另一个项目。
15. 作为用户，我希望切换 Application 时同时切换会话、工作范围和状态视图，从而清楚当前操作目标。
16. 作为用户，我希望当前 Application、Working Revision、Running Revision、权限模式和 Studio 模型始终可见，从而理解每次操作的上下文。
17. 作为新建 Application 的用户，我希望通过自然语言描述目标、角色、输入、输出和验收标准，从而不必学习一套独立表单向导。
18. 作为新建 Application 的用户，我希望 Studio 只在关键业务意图缺失时逐个追问，从而避免冗长且机械的初始化流程。
19. 作为新建 Application 的用户，我希望在对话过程中同步预览 Agent 拓扑和配置，从而及时纠正设计方向。
20. 作为用户，我希望创建和后续维护使用相同的对话交互，从而只学习一套工作方式。
21. 作为 Application 维护者，我希望直接用自然语言要求增加、删除或修改 Agent、Worker、Tool、Skill、Prompt 和配置，从而不必手工定位所有 YAML。
22. 作为 Application 维护者，我希望 Studio 在当前 Application 范围内直接完成修改，从而不再需要独立 `/apply` 草稿流程。
23. 作为 Application 维护者，我希望每次修改后看到清晰 Diff，从而知道 Agent 实际改变了什么。
24. 作为 Application 维护者，我希望每次修改形成可追踪 Revision，从而能够区分工作配置和正在运行的配置。
25. 作为运行中 Application 的维护者，我希望当前 Run 固定使用启动时的 Revision，从而避免一次执行中途发生不可解释的热切换。
26. 作为运行中 Application 的维护者，我希望修改后由显式重启或新 Run 使用新 Revision，从而控制配置切换时机。
27. 作为用户，我希望第一版不要求学习 `/undo` 和 `/redo` 命令，从而保持交互简单。
28. 作为用户，我希望对修改不满意时可以直接告诉 Studio 继续调整，从而通过对话完成修正。
29. 作为用户，我希望 Studio 在数据不确定时先读取配置、代码、日志和状态，从而减少不必要的澄清问题。
30. 作为用户，我希望 Studio 自主执行“理解、检查、修改、校验、试运行、分析、修复”的 Loop，从而获得完整结果而不是一份操作建议。
31. 作为用户，我希望只有业务意图确实影响结果时才被询问，从而让 Agent 保持自治但不擅自决定产品目标。
32. 作为用户，我希望危险、越权、删除或不可逆操作在执行前获得授权，从而避免自治 Loop 扩大风险。
33. 作为普通用户，我希望默认使用 Application Only 权限，从而允许 Studio 读取项目但只直接修改当前 Application。
34. 作为高级用户，我希望可以选择 Full Access，从而在明确需要时允许 Studio 处理整个项目范围的任务。
35. 作为高级用户，我希望 Full Access 只在当前 TUI 会话有效，从而不会在重启后意外继承高权限。
36. 作为用户，我希望查看、分析和静态校验自动执行，从而不会被低风险授权频繁打断。
37. 作为用户，我希望首次启动、停止、恢复或重启 Application 时选择“仅本次”或“本次会话始终允许”，从而控制成本和外部副作用。
38. 作为 Full Access 用户，我希望运行控制可以自动执行但仍显示发生了什么，从而兼顾效率和透明度。
39. 作为 Application 维护者，我希望 Studio 自动读取 Global Skills，但修改它们前展示受影响的 Applications，从而理解全局变更范围。
40. 作为 Application 维护者，我希望 Global Skill 或全局配置修改请求一次明确授权，从而避免普通 Application 修改影响其他项目。
41. 作为用户，我希望点击 Application 后看到 Effective Config，而不是自行推导全局、Application 和 Agent 三层配置，从而准确理解运行行为。
42. 作为用户，我希望每个有效配置字段显示来源，从而能够定位应该修改哪一层。
43. 作为用户，我希望查看 Supervisor 与 Workers 的拓扑关系，从而理解任务如何分解和委派。
44. 作为用户，我希望点击任一 Agent 查看角色、模型、Tools、Skills、子 Agent、权限、Hooks 和 MCP，从而审计它的能力边界。
45. 作为用户，我希望原始 YAML 作为折叠的技术证据保留，从而在需要时检查实现而不让默认页面过载。
46. 作为用户，我希望明确区分 Studio 模型和 Application 运行模型，从而不会误以为切换聊天模型会修改业务 Agent。
47. 作为用户，我希望 Studio 模型沿用 OpenCode 的 Provider、认证和模型切换体验，从而复用熟悉的配置方式。
48. 作为用户，我希望 Application 运行模型继续使用 AgentLoom 的模型配置和 Agent `model_type`，从而保持 Python Runtime 的配置真相。
49. 作为用户，我希望模型密钥、Provider 头和敏感认证信息不进入详情或桥接 DTO，从而避免凭证泄露。
50. 作为用户，我希望 Run 详情首先显示状态、耗时和进度，从而快速判断是否需要干预。
51. 作为用户，我希望运行中能看到当前执行的 Agent 或步骤，从而知道系统不是卡死。
52. 作为失败 Run 的用户，我希望首先看到失败 Agent、失败步骤、核心原因和建议操作，从而直接开始修复。
53. 作为用户，我希望看到核心输入和最终输出摘要，从而判断 Run 是否满足任务目标。
54. 作为用户，我希望看到 Workers 或子 Agent 的状态，从而定位多 Agent 流程中的瓶颈。
55. 作为用户，我希望结果、产物和日志以清晰入口呈现，从而按需深入而不是被长文本淹没。
56. 作为高级用户，我希望原始 Events、JSON、堆栈和完整日志默认折叠，从而既保留诊断证据又维持信息层级。
57. 作为用户，我希望 Run 详情区分 never-run、running、completed、failed、crashed、interrupted 和 unknown，从而不会把缺失证据误报为失败或成功。
58. 作为用户，我希望 Run 和 Task 身份保持清晰，从而理解 resume 会产生新的 Run 但仍属于同一逻辑 Task。
59. 作为用户，我希望 Studio 根据结构化 Run receipt、manifest、checkpoint 和有界日志判断状态，从而避免从自由文本猜测运行真相。
60. 作为用户，我希望失败诊断只使用经过脱敏且有界的上下文，从而兼顾诊断效果、性能和安全。
61. 作为用户，我希望 `Ctrl+X` 打开统一命令面板，从而搜索 Applications、会话、主 Agents、Runs、Skills 和命令，同时避免与 VS Code 冲突。
62. 作为用户，我希望命令面板中的每个入口有准确描述，从而知道执行后会发生什么。
63. 作为 OpenCode 用户，我希望 `/help` 打开帮助，从而延续已有使用习惯。
64. 作为用户，我希望全局不新增 `?` 帮助快捷键，从而避免与 OpenCode 的 Diff Viewer 语义冲突。
65. 作为用户，我希望移除 F6 焦点切换，从而避免终端冲突和不可发现的隐藏状态。
66. 作为键盘用户，我希望 Enter 打开选中项、Esc 返回，从而使用一致的导航规则。
67. 作为鼠标用户，我希望可以点击 Applications、Agents、Runs、Skills、Tools 和日志入口，从而不依赖记忆快捷键。
68. 作为用户，我希望聊天在打开详情时仍然保留，从而边观察状态边继续要求 Studio 修改。
69. 作为用户，我希望思考、工具执行、校验、运行、等待授权和失败分别有明确状态反馈，从而理解 Agent Loop 正在做什么。
70. 作为用户，我希望流式输出和工具块采用 OpenCode 风格的平滑更新，从而获得一致且可读的对话体验。
71. 作为低刷新或偏好减少动态效果的终端用户，我希望动画自动降级为静态状态符号，从而保持可用性。
72. 作为用户，我希望动画只对应真实状态而不是装饰性循环，从而不会把视觉活动误认为实际进展。
73. 作为用户，我希望 TUI 启动时后台检查更新，从而及时发现修复和功能更新。
74. 作为用户，我希望更新可一键执行但不会在运行中静默替换，从而保持当前会话稳定。
75. 作为用户，我希望 TUI 与内置 OpenCode Runtime 整体升级，从而避免协议版本不兼容。
76. 作为命令行用户，我希望使用 `agentloom update` 主动升级，从而不必查找安装脚本用法。
77. 作为源码用户，我希望文档明确说明重新运行 `./install` 即可更新，从而使用最短路径安装最新构建。
78. 作为源码用户，我希望文档说明 `--no-modify-path` 只是可选项，从而不会误以为它是安装或升级必需参数。
79. 作为用户，我希望更新完成后由产品安全重启 TUI，从而确保新版本完整生效。
80. 作为 Codex 用户，我希望能够加载与 Studio 相同的 AgentLoom Framework Skill 并调用同一套领域工具，从而复用可靠的配置和诊断能力。
81. 作为 AgentLoom 维护者，我希望专业领域逻辑只在 Python 权威实现中存在一份，从而避免 TUI、Codex 和 Runtime 产生不同配置真相。
82. 作为 AgentLoom 维护者，我希望 OpenCode 负责通用文件读写、搜索、Diff、权限和 Agent Loop，从而不在 Python 中复制通用编码 Agent 能力。
83. 作为 AgentLoom 维护者，我希望 Studio Agent 只有在所有适用验证完成后才声明任务完成，从而避免“已修改”被误报为“已交付”。
84. 作为用户，我希望未授权真实运行时结果明确标记为“配置已验证，尚未运行”，从而准确理解剩余风险。
85. 作为用户，我希望真实冒烟运行失败后 Studio 自动分析并继续修复，从而最终获得可工作的 Application。

## Implementation Decisions

1. **产品边界**：TUI 被定义为独立的 Application Studio 控制面。它管理 AgentLoom Applications，但它本身不是 AgentLoom Application，也不使用 Python Agent Runtime 执行 Studio Agent Loop。
2. **OpenCode Runtime**：随产品固定并分发一个兼容版本的 TypeScript/Bun OpenCode Runtime，通过其 HTTP/SDK 边界使用 Session、Prompt Loop、流式事件、权限、Tool、Subagent、Compaction 和持久化能力。不得复制或重新实现这些状态机。
3. **进程职责**：TypeScript 层拥有 Studio Session、LLM 流、Agent Loop、权限、通用文件工具、Diff 和 UI 状态；Python 层只拥有 AgentLoom 配置语义、Effective Config、Application 校验、运行生命周期和可观测性真相。
4. **共享领域能力**：AgentLoom 专业能力以稳定、版本化的 CLI/JSON 接口提供。至少覆盖 Application catalog、Effective Config、Agent topology、Tools/Skills/Permissions 来源、静态校验、影响分析、Run 启停恢复、Run 详情、日志与产物定位。
5. **复用边界**：Studio 的 OpenCode Plugin/Tool 和 Codex 的 Framework Skill 调用同一套领域接口。确定性逻辑不得只写在 Skill 提示词或 TypeScript 适配器中。
6. **Framework Skill**：项目根能力边界中只维护一个共享 `agentloom-framework-skill`，负责把任务路由到正确参考资料和专业工具。它不注入被管理 Application 的运行时，也不计入 Application Skills。
7. **首版协议**：首版使用本地 CLI/JSON 或等价的本地结构化 RPC，不引入 MCP Server。未来如需跨进程或跨产品远程复用，可在稳定领域接口外再增加 MCP 适配层。
8. **OpenCode 版本管理**：TUI 与 OpenCode Runtime 形成一个兼容版本单元，由 AgentLoom 安装和更新流程统一管理。升级前必须验证 SDK/API 与 Plugin/Tool 兼容性。
9. **启动模型**：启动选择器第一项固定为 `+ New Application`，其后列出全部 Applications。Recent Runs 和 Global Skills 是辅助入口，Agents 不在首页单独计数。
10. **Application 身份**：Application 是 Studio 的首要工作空间和会话隔离键。任何修改与 Run 控制必须绑定当前选中的 Application；未选择 Application 时只能创建、浏览或讨论，不能隐式修改任意现有 Application。
11. **Session 持久化**：每个 Application 拥有独立、持久化的 Studio Session 历史。切换 Application 同时切换 Session、工作范围、Revision 和状态面板。
12. **对话式创建**：新建 Application 不使用独立表单向导。Studio 通过对话提取目标、角色、输入、输出、约束和验收标准，仅在结果会实质分叉时逐个询问缺失业务意图。
13. **直接修改**：当前 Application 范围内的普通创建和编辑由 Studio 直接执行，随后展示 Diff 并记录 Revision。废除普通工作流中的内存草稿和 `/apply` 门槛。
14. **Revision 模型**：至少区分 Working Revision 与 Running Revision。Run 启动后固定使用当时的 Revision；文件变化不会热切换运行中的 Agent。新配置只在显式新 Run、重启或恢复策略允许的边界生效。
15. **简化恢复体验**：首版保留底层 Revision 证据，但不要求提供 `/undo`、`/redo` 用户命令。用户通过自然语言要求继续修改或恢复；后续可基于真实使用需求开放历史操作。
16. **自治 Loop**：默认工作流是“理解需求 → 检查事实 → 修改 → 静态校验 → 请求必要运行授权 → 冒烟运行 → 读取结构化证据 → 自动修复 → 完成”。不得因为可通过工具获取的数据不确定而提前询问用户。
17. **询问边界**：只有业务意图缺失会导致明显不同结果，或操作越出授权范围、删除文件、扩大权限、修改多个 Applications、产生不可逆外部影响时，Studio 才暂停并请求用户决定。
18. **默认权限**：Application Only 是默认模式。它允许读取整个 AgentLoom 项目以理解上下文，允许直接写当前 Application；对全局配置、Global Skills、其他 Applications 和框架源码的写入需要授权。
19. **Full Access**：用户可显式选择 Full Access。它只在当前 TUI 会话有效，重启后恢复 Application Only。即使自动执行，也必须在 UI 中保留 Tool、Diff、Run 和影响范围记录。
20. **运行授权**：查看、分析和静态校验自动执行。Application 启动、停止、重启和恢复首次询问，支持“仅本次”和“本次会话始终允许”。Full Access 下可自动执行。
21. **全局变更授权**：Global Skills 和全局配置可自动读取。写入前必须计算并展示受影响 Applications，再请求一次或当前会话授权；Full Access 下免确认但仍展示 Diff 和影响范围。
22. **模型隔离**：Studio Agent 使用 OpenCode Provider、认证和模型选择；Application Agents 继续使用 AgentLoom 的独立 LLM 配置和 `model_type`。Studio 模型切换不得隐式修改 Application 模型。
23. **敏感信息边界**：Application 模型凭证、Base URL、Headers 和 Provider 私密配置不得进入 Studio DTO、日志、详情或 LLM 上下文。只暴露经过脱敏的模型类型、状态和必要诊断信息。
24. **Application 首页**：主列表展示 Application 名称、健康状态、是否运行和最近修改时间。布局必须让键盘和鼠标都能直接进入 Application，不要求先打开 Recent Run。
25. **Chat-first 布局**：顶部展示当前 Application、Revision、权限和 Studio 模型；中间展示会话、Tool calls、Diff、授权与 Run 进度；辅助面板展示 Application 健康、拓扑、Recent Runs 以及 Effective Tools/Skills/Permissions。窄终端下辅助面板可折叠，但信息仍可通过命令面板访问。
26. **Application 详情**：默认展示最终 Effective Config，而非仅展示原始 YAML。每个重要字段标注 Global、Application 或 Agent 来源；原始 YAML 作为折叠技术证据。
27. **Agent 详情**：Supervisor 和 Worker 使用统一详情模型，至少展示角色、描述、模型类型、Workflow 摘要、Tools、Skills、Workers/子 Agent、权限、Hooks、MCP、文件来源和校验状态。
28. **统计语义**：首页不展示展开后的 Agent 定义总数。Application 数量按 Application 目录和有效 catalog 去重计算；Agent 定义只在对应 Application 拓扑中展示。
29. **Skills 语义**：Global Skills 只统计 AgentLoom Runtime 真正可用的全局 Skills；Application 详情显示最终注册的 Skills、load mode 和来源。不得用 Application 目录扫描结果冒充全局或有效加载结果。
30. **Skill/Hook 独立性**：遵守现有 ADR，Skill 与 Hook 是独立模块。Studio 的 Effective Config 视图可以同时展示两者，但不得暗示 Skill 自动携带 Hook，也不得通过 Skill 扫描发现 Hook。
31. **Run 真相来源**：Run 状态使用结构化 lifecycle、receipt、manifest、checkpoint、heartbeat、task tree、bounded log 和 artifacts。不得通过自由文本日志推测 canonical 状态，也不得伪造缺失结果。
32. **Run 状态模型**：保留 never-run、running、completed、failed、crashed、interrupted 和 unknown 的明确语义。`run_id` 表示一次 attempt，`task_id` 表示可跨 resume 延续的逻辑任务。
33. **Run 默认详情**：默认只展示状态、耗时、进度、当前或失败 Agent/步骤、核心输入输出摘要、错误原因、建议操作、Workers/子 Agent 状态、产物和日志入口。
34. **Run 技术详情**：原始 Events、完整 JSON、堆栈和长日志默认折叠，并继续使用有界读取、大小限制、路径约束和清晰的“截断/缺失/无效”状态。
35. **失败诊断**：Studio 可自动分析失败，但输入必须脱敏、有界并优先来自结构化证据。完整日志留在磁盘，通过路径入口访问。
36. **导航规则**：`Ctrl+X` 是统一命令面板入口，可检索 Applications、Sessions、主 Agents、Runs、Skills 和 Commands；Worker 子 Agent 只在 Application 或主 Agent 详情中展示；Enter 或点击打开，Esc 返回或中断当前 OpenCode Session。
37. **帮助规则**：帮助通过 `/help` 或命令面板打开。全局不绑定 `?`；`?` 仅在 OpenCode 已定义的局部视图中保留局部含义。移除 F6 焦点切换。
38. **OpenCode 交互一致性**：模型选择、权限提示、Tool block、Diff、Subagent Session、流式消息和中断语义优先沿用内置 OpenCode Runtime 的既有行为；AgentLoom 只增加领域命令与 Application 视图。
39. **动画规则**：动画必须由真实状态驱动，分别表达思考、工具执行、校验、运行、等待授权、完成和失败。支持 reduced-motion 或终端能力检测，降级时使用稳定的静态符号。
40. **完成标准**：Studio 只有在 YAML/Schema、引用、Effective Config、Tools、Skills、权限、Agent 拓扑和相关测试通过，并在行为变更时完成一次获准的真实 Application 冒烟运行后，才可声明完成。
41. **未运行状态**：如果用户没有授权真实运行，Studio 必须明确报告“配置已验证，尚未运行”，并列出未验证风险，不得宣称任务完全完成。
42. **自动修复**：冒烟运行失败后，Studio 自动读取结构化 Run 证据并继续 Loop；只有达到询问边界或权限边界才暂停。
43. **更新发现**：TUI 启动后后台检查兼容版本，不阻塞首屏。有更新时显示版本与关键变化，但不得在活动 Session 中静默替换二进制。
44. **一键更新**：用户确认后，产品自动下载或构建、安装 TUI 与配套 OpenCode Runtime，并安全重启。另提供 `agentloom update` 主动入口。
45. **源码更新**：文档将 `./install` 作为源码安装和更新的标准命令；`--no-modify-path` 只说明为“不改 Shell PATH 配置”的可选参数。
46. **文档同步**：根文档和 TUI 文档必须删除旧的 `/apply`、F6、Run-first 和 Python Chat Agent 描述，增加 Application Studio、权限、Skills 统计、OpenCode 模型、更新和完成标准说明。
47. **兼容现有 Runtime**：Python AgentLoom 的 Supervisor、Worker、Agent YAML、Runner、checkpoint、Hook Runtime 和结构化 Run 合同继续作为领域真相；本需求不改变它们的既定含义。
48. **安装产物**：安装产物必须包含 TUI、固定版本 OpenCode Runtime、Python Runtime 和兼容的启动包装器，用户不需要单独安装 OpenCode。

## Testing Decisions

1. **测试原则**：优先测试用户可观察行为和跨边界合同，不断言私有函数、组件内部状态或 OpenCode 内部实现细节。只在高层测试无法准确定位协议破坏时增加窄合同测试。
2. **主测试接缝**：建立一条 Application Studio 端到端验收接缝。它从临时 AgentLoom 项目启动真实的 TypeScript Studio 客户端、固定版本 OpenCode Runtime 和 Python 领域接口，使用确定性模型 Provider 驱动一次完整对话，验证“选择或创建 Application → 直接修改 → 展示 Diff → 生成 Revision → 静态校验 → 授权运行 → 获取结构化 Run → 展示有效摘要”的外部行为。这是首要回归门禁。
3. **真实 Application 验收**：除确定性端到端接缝外，使用至少一个真实 Supervisor + Worker Application 完成冒烟验收，覆盖 Application overlay、Agent YAML、Tool、Skill、权限、Run receipt、Worker 状态和最终结果。需要真实模型的用例作为受控验收矩阵执行，不用脆弱文本匹配替代 Runtime 证据。
4. **OpenCode 集成合同**：验证 Session 创建/恢复、流式事件、Tool call、Permission once/always/reject、Subagent、Diff、Interrupt 和持久化历史的用户可见行为。测试通过 SDK/API 边界进行，不复制 OpenCode 自身的单元测试。
5. **Python 领域合同**：复用现有 NDJSON RPC、catalog、definition validation、runtime truth 和 durable Run 测试模式，扩展为版本化 CLI/JSON 合同测试。必须验证错误分类、并发请求关联、有界读取、路径安全与敏感字段不跨边界。
6. **Application catalog 测试**：验证从未运行、运行中、已完成和配置无效的 Applications 都可发现；首页数量等于去重 Application 数，而不是 Supervisor/Worker YAML 数。
7. **Skills 测试**：验证 Global、Application、Agent 私有三层来源、load mode、重复名称、无效 Skill 和最终有效注册结果。验证 Framework Skill 不进入 Application Skills 统计，Skill 发现不会产生 Hook 副作用。
8. **Effective Config 测试**：复用现有 Layered Config 与 Agent YAML 校验测试，验证 Global、Application overlay 和 Agent 白名单覆盖的最终值与来源。敏感 LLM 配置必须保持物理隔离。
9. **权限测试**：覆盖 Application Only 的读/写范围、Global 变更影响分析、跨 Application 写入、删除、高风险运行、once/always/reject 以及 Full Access 会话重置。断言实际允许或拒绝的外部操作，不断言权限引擎内部规则表。
10. **Revision 测试**：验证直接编辑产生 Diff 与新 Working Revision；活动 Run 保持原 Running Revision；显式新 Run 或重启才使用新 Revision。
11. **自治 Loop 测试**：给出包含可发现事实缺口的需求，验证 Studio 主动调用检查工具并完成修改，而不是向用户询问可由项目获得的信息；给出真正模糊的业务目标，验证 Studio 暂停并请求决定。
12. **完成声明测试**：验证静态校验失败、真实冒烟失败和未获运行授权三种情况下均不能输出“完全完成”；只有完整验证闭环成功后才允许完成状态。
13. **Run 详情测试**：复用现有 canonical runtime truth 测试，覆盖 never-run、running、completed、failed、crashed、interrupted、unknown，以及同名 Run 在不同 Applications 下的寻址。验证 Worker 中间结果不会被误报为最终结果。
14. **有界可观测性测试**：验证超大或无效 manifest、Events、日志和 artifacts 不会导致无界读取或错误成功状态；UI 必须标识缺失、无效和截断。
15. **UI 行为测试**：在现有 Controller、Presentation、View、Layout 和 Theme 高层接缝上验证 Applications-first 首屏、顶部上下文、Chat 保留、详情折叠、窄终端布局和状态驱动动画降级。
16. **导航测试**：验证 `Ctrl+X` 打开统一命令面板、`/help` 打开帮助、Enter/点击进入、Esc 返回或中断，并确认 F6、`Ctrl+P` 与全局 `?` 不再作为产品入口。
17. **安装测试**：复用现有隔离安装夹具，验证标准 `./install`、可选 `--no-modify-path`、TUI/OpenCode/Python 整体安装、PATH 幂等修改以及无手工虚拟环境激活。
18. **更新测试**：使用本地伪更新源验证后台检查不阻塞启动、用户确认后整体更新、活动 Session 不被静默替换、协议不兼容时拒绝更新、失败后保留可运行旧版本。
19. **文档验收**：通过命令与文案契约检查，确保安装、更新、Applications、Skills 统计、权限、模型边界、快捷键和完成标准与实现一致，不再出现 `/apply`、F6 或 Run-first 旧流程。
20. **回归门禁**：TypeScript 类型检查与测试、Python TUI/领域接口测试、Runner/Config/Skills/Hook 相关测试、构建、隔离安装和真实 Application 冒烟验收全部通过后才能交付。

## Out of Scope

- 将 Python AgentLoom Runtime 重写为 TypeScript。
- 将 Studio Agent 实现成一个普通 AgentLoom Application，或让它在运行中修改并热替换自身。
- 在 AgentLoom 内复制 OpenCode 的 Session、Permission、Agent Loop、Subagent 或 Diff 状态机。
- 首版提供 MCP Server；首版以本地版本化 CLI/JSON 作为共享专业工具边界。
- 对运行中的 Application 进行配置热更新。
- 首版向用户暴露 `/undo` 和 `/redo` 命令；Revision 证据仍会保留。
- 默认扫描并统计机器上所有 Codex、Claude 或第三方 Skills。
- 将原始 Events、完整 JSON 或完整日志恢复为默认 Run 详情。
- 静默自动安装更新，或在活动 Session 中替换正在运行的组件。
- 改变现有 Hook Runtime 合同，或把 Hook 重新嵌入 Skill。
- 以自由文本日志解析替代 AgentLoom 的结构化 Run 生命周期和 receipt。
- 为本次需求重做 Web UI、调度器或 Python Agent 执行语义；相关能力只需在 Studio 中继续可发现和兼容。

## Further Notes

- 当前本机安装包已在本次讨论中重新构建和安装。正常源码更新只需要运行 `./install`；`--no-modify-path` 仅用于避免修改 Shell 配置。
- 现状诊断中，项目 catalog 包含 25 个 Applications；原首页的 107 Agents 来自 Supervisor 与 Worker YAML 的展开定义，不代表已加载或正在运行数量。PRD 明确移除该首页统计口径。
- 现状中的 4 Skills 只来自部分 Application 本地 Skill 扫描，不代表 Global Skills 或任一 Application 的 Effective Skills。新实现必须从 Runtime 配置和 Agent 注册结果产生准确视图。
- 当前根文档和 TUI 文档仍描述 Python Builder、内存 Draft、`/apply`、F6 和“不能运行 Application”的旧产品边界；实现本 PRD 时必须同步替换，避免再次出现源码已改但安装包或文档仍旧的情况。
- OpenCode 集成基线来自已核对的本地 OpenCode 1.18.3 源码。其核心包为私有 workspace 包且依赖多个内部 workspace package，因此应以固定 Runtime 进程和 SDK/API 为边界，不直接 import 不稳定内部 Session 源码。
- OpenCode 的默认全局入口是 `Ctrl+P` 命令面板；AgentLoom 为避免与 VS Code Quick Open 冲突，产品入口改用单键 `Ctrl+X`。帮助可通过 `/help` 打开；`help_show` 默认没有全局按键，`?` 只在 Diff Viewer 等局部场景有意义。
- AgentLoom 配置仍遵循现有分层规则：框架默认值、Global 系统配置、Application overlay、Agent 白名单字段共同形成 Effective Config；LLM 配置继续物理隔离。
- Run 可观测性继续遵循现有结构化合同：一次 resume 产生新的 `run_id`，但保持逻辑 `task_id`；成功、失败、拒绝和中断必须由类型化事件和 receipt 表达。
- 本 PRD 按用户要求仅生成在项目根目录，不发布到外部 Issue Tracker。
