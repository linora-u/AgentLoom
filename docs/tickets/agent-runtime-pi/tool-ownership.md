# 工具与基座归属：本轮执行约束

更新：2026-09-20。根据维护者最新确认：AgentLoom 管理 YAML、多 Agent、长期记忆与公共治理；smolagents 是项目自研 Agent 的执行基础。仓库中自己编写的工具，不因此自动成为平台工具。

本文细化原核心规格中的“smol 专属实现”和“AgentLoom 工具”。后续票据按本文划分所有权，不再把全部现有基础工具改成跨基座默认工具；其他既有验收要求继续有效。路径是迁移定位信息，不是新的公开 API。

判定归属看职责：更换基座后仍需保留的应用规则和资产归 AgentLoom；完成单个 Agent 推理与执行所需的基础实现归基座；对既有服务的协议、类型和事件转换归 adapter。不能根据“代码现在位于 src/tools”或“是我们自己写的”决定归属。

正式基座实现落在 `src/adapters/smolagents/`、`src/adapters/pi/`；平台运行规则留在现有 application/runtime/self_learning 等公共模块。01 的验证程序已在当前工作树的 `tests/pi_sdk_compatibility/`，迁移验收由 03 收口；不把 experiments 当生产分层，也不把公共治理整体塞进 adapters。

## 0. 先分清平台、Agent 实现和适配工作

| 层次 | 长期维护的内容 | 对应票据 |
| --- | --- | --- |
| AgentLoom 平台 | YAML 解析、Application/Supervisor/Worker 关系、任务派发、Run 身份、Goal、长期记忆与证据、工具权限及产物 | 03 定边界；05/06 治理；08 平台入口；11 混合应用验证 |
| 基座自己的 Agent | 单 Agent 循环、模型调用、局部计划、对话历史与压缩、基础工具算法、原生会话状态 | 04 收拢自研 smol；Pi 复用发布 SDK 的对应实现 |
| adapter 的接线 | 配置/模型参数投影、工具定义转换、原生执行包装、事件/结果映射、启动取消关闭、checkpoint 关联 | 04 的 smol 接线；07/09/10/12 的 Pi 接线 |

`src/adapters/smolagents/` 可以同时包含“自研 Agent 的完整实现”和“连接平台的适配代码”，在目录内部区分即可。不要为看起来统一而再造一个所有基座都必须继承的 planner、memory 或基础工具实现。

目标布局只调整职责需要移动的内容；平台模块沿用现有位置：

```text
src/
  application/                 # YAML、多 Agent 定义与应用生命周期
  runtime/                     # 公共调用合同、工具治理、资源和恢复协调
  self_learning/               # 平台长期记忆、历史、证据和审核
  tools/                       # 平台工具入口、按需专业工具及中立登记
  adapters/
    smolagents/                # 自研 Agent + 平台接线
      tools/                   # 自研文件、搜索、Shell、Todo 等基础实现
      ...                      # 私有循环、模板、摘要、状态与恢复
    pi/                        # SDK/bridge 接线及官方工具包装
    mcp/                       # 外部工具协议接入
```

这里是迁移目标，不表示当前 `runtime/` 和 `tools/` 已全部完成拆分。04 迁移 smol 私有实现，08 去除平台/专业工具的 smol 构造依赖；共享 helper 由 03 定修改权，06 提取既有公共保护。

参考文档中的宿主负责请求与启动、包装进程负责参数/会话/输出转换、CLI 执行 Agent loop，支持这种职责划分。AgentLoom 借鉴这一边界；多 Agent 编排和长期记忆归属仍按本项目需求决定。来源：[CLI 本地适配参考索引](https://bytedance.larkoffice.com/docx/JBfhdGHIIoTud2xk4n1cnJfLnSf)（本次读取 revision 3 的职责与工具策略节选）。

## 1. 三类工具

| 归属 | 当前实现或能力 | 目标与负责人 |
| --- | --- | --- |
| 自研 smol Agent 的基础工具 | read_file、write_file、edit_file、list_directory | 04 收入 smol adapter 的 tools；Pi 使用自己的对应实现 |
| 同上 | shell_tool、check/list/kill_background_task、Shell session、输出处理与进程状态 | 04 收入 smol 工具实现；资源随对应实例/Run 释放 |
| 同上 | grep_search、glob_search | 04 收入 smol 基础搜索工具 |
| 同上 | todo_write、final_answer、Todo 回填、规划与会话摘要 | 04 收入 smol；不把其内部状态作为其他基座的必需合同 |
| 平台工具 | Worker 调用、应用 Goal | 08 通过中立定义暴露，公共编排与根目标归属不变 |
| 平台工具 | memory、session_search、session_scroll | 08 解耦基座构造；保留长期记忆、历史、证据与审核规则 |
| 平台工具 | loom_retrieve_context | 08 提供中立调用；ContextRef 的产物存储与检索仍由公共服务持有 |
| 平台工具 | skill_manage | 08 保留 Skill 提案/审核语义；不是 Agent-local 规划工具 |
| 可选专业工具 | AST/LSP、get_file_outline、Markdown 报告工具及相关资源 | 08 保留按需选择和跨基座调用；不是平台核心或各基座默认工具 |
| 外部协议接入 | MCP 发现、调用、连接隔离与清理 | 08 持有中立 MCP 连接/工具；smol 类型转换留在 smol 接入侧 |

`skill` 要按职责拆：共享 SkillCatalog、范围和提案管理归平台；smol 的激活工具呈现和提示词注入归 smol 接线。08 交付中立激活入口，09 选择 Pi 的接入方式；同一技能只用一条激活路径，不同时启用两套自动发现。未授权的本地 Pi 资源发现继续禁用。

业务专用工具也按同一规则判断：若作用于 AgentLoom 管理的业务资产且需要换基座复用，提供中立工具入口；若只是补足自研 Agent 的基础执行能力，留在 smol。MCP 表示接入协议，不自动证明其中每个工具具有相同权限或可信记忆资格。

现有 codex 工具包装暂按显式选择的外部调用兼容保留；它不等于已经支持 Codex runtime。本轮不顺带接入 Claude/Codex 新基座，也不开发 Studio。

## 2. 基础工具实现与公共约束分开

| 留在对应基座 | AgentLoom 继续负责 |
| --- | --- |
| 读写算法、原生参数、分页默认、展示文本、读取去重缓存 | 路径/命令授权、Hook 顺序、修改后严格校验 |
| Shell 会话、后台任务、输出解释、基座拥有的进程句柄 | 取消与关闭合同、超时和失败状态、跨 Agent 身份关联 |
| 原生会话压缩、Todo、消息和 checkpoint payload | 应用 Goal、长期记忆、工具原文产物、ContextRef、同基座恢复协调 |

写前保护、文件历史、受授权查询范围和证据完整性等已有承诺不能随工具迁移变成 smol 私有功能。读取去重缓存与写入陈旧性检查即使同处一个旧文件，也应分别判定：前者是基座优化，后者按现有保护合同由公共治理落实。06 提取和接入这些已承诺规则，不能要求 Pi 调用 smol 的 read/write/bash 实现。

记忆边界同样按数据职责划分：基座维护“这次对话如何继续”的上下文和压缩状态；AgentLoom 维护“哪些知识已经认可、谁能使用、证据来自哪里”。切换基座后复用后者，不转换底层消息历史，也不新增一套必填 YAML 记忆配置。

03 先分离已有公共消费者依赖的辅助入口，例如 Hook 使用的进程环境构造，以及 Application 的资源关闭调用。04 迁移基础工具时保留保护行为，不扩展 native 治理；06 在 04 稳定的布局上提取共用策略并同时接回 smol 与 native executor。已位于公共 Hook/Gateway 的实现保持唯一，不复制到 adapter。

Application 通过运行时/资源关闭合同释放资源，不直接访问某个基座的 Shell 注册表。关闭范围必须与实例、Worker、Run 对齐，不终止同一应用中其他并发调用的进程。

## 3. 工具装配与 YAML 兼容

工具装配结果由三部分组成：当前基座的显式原生工具选择、应用选定的平台工具、应用选定的可选专业工具。不是把整个公共 catalog 注入每个 Agent。

- 03 冻结内部工具归属、提供方、逻辑能力、可见名称、schema、路径/操作 metadata 和冲突规则；不新增另一套用户必填 YAML。
- 历史全局默认 core_file/core_shell/core_search 解释为旧 smol 的默认工具选择，不把对应 Python 实现自动加载到 Pi。上下文、记忆等平台工具仍按原配置与实际能力选择；不额外强制开启。
- 旧 smol YAML 中的工具名、toolset、固定参数、权限和启动方式保持效果。
- 显式选择旧工具名后切换到 Pi，只能采用已经验证、含参数及权限映射的兼容解释；没有等价映射时明确报错。不得忽略选择、偷偷回退 smol 或按近似名字替换。
- 同一基础能力默认只暴露一个实现。同名冲突拒绝或由明确配置解决，不能靠注册顺序覆盖。
- toolsets: [] 关闭隐式 builtin 工具集合，显式 tools/MCP/Worker/Goal 仍按各自配置解释。明确无工具的 native 应用不配置这些额外入口，其 manifest 为空，基座不自行补回默认工具。
- 专业工具可供 Pi 按需使用，不因为能够复用就成为默认依赖。MCP 是工具来源，其具体工具同样受选择与治理约束。

## 4. 所有权与依赖

03 先提供兼容合同、注册分区及最小公共依赖拆分。随后 04、05、07、08 可以从同一冻结提交并行。

- 04：自研 smol 执行器、基础工具、私有状态、资源释放和对应测试；包括 Todo 装饰器迁移。
- 05：公共 Gateway 的只读 native 准备/结算路径；不搬 smol 基础工具。
- 06：等待 04 和 05，接入公共写入/Shell 保护；此时才接收 04 移交的保护调用点，避免和搬迁同时改文件。
- 07 → 09 → 10 → 12：Pi 接入链，独占 Pi adapter/bridge。
- 08：平台工具、可选专业工具、MCP 与中立 manifest；Goal 的旧 smol 装饰器在本票迁移，Todo 属于 04。
- 公共 catalog 聚合入口、配置默认解释、registry/readiness 由协调者串行接线。04 和 08 各交自己的工具分区，不同时编辑同一张全量表。

精确文件所有权见 [文件交接](03-file-ownership.md)，session 与 worktree 流程见 [开发计划](worktree-plan.md)，阻塞图以 [执行索引](README.md) 和每票 Blocked by 为准。

## 5. 完成标准

- 原 smol 应用继续使用自研基础工具并保持已有行为。
- Pi 基础工具实际委托官方实现，不导入或执行 smol 的基础工具包。
- Worker、Goal、长期记忆、历史和 ContextRef 可在两个基座下使用；记忆不新增必填配置。
- 明确的无工具应用不被默认工具污染；专业工具只按选择启用。
- 禁止操作在副作用前拒绝，允许操作保留应有产物和证据，资源关闭不串用实例。
- Pi-only 安装、平台工具构造和受支持调用不依赖 smol；旧 smol 配置的兼容测试仍存在。

本文是实施约束，不表示这些迁移已完成。01/02 的完成状态和历史验收记录不变。
