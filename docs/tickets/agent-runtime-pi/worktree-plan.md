# Pi 并行开发与合并计划

日期：2026-09-20。配套规格：[AgentLoom 可替换 Agent 基座与 Pi 接入](../../specs/agent-runtime-pi-integration.md)。

执行细分：见 [14 张编号票据与 session 执行指南](README.md)。该索引将本计划的宽阶段拆成可独立验收的任务，并细化阻塞关系与文件所有权。07 可先接通能力受限的无工具 Pi；09/10 通过治理验收后逐步启用工具，12 通过后启用恢复。下文关于完整 Pi 注册的要求不禁止这种明确受限的阶段交付。

本文件是执行计划，不是已完成记录。本轮只写文档；以下分支、worktree、测试产物和合并动作均待开发阶段创建。

## 1. 基线与约束

- 当前主分支：main；远端主分支引用：origin/main。
- 研究 revision：c697b24f602e71d56c9aa2c532e6079c0db5aa9d。
- 仓库：/Users/bytedance/code/data_clear/AgentLoom。
- 实施开始时重新检查 HEAD、工作区和分支，记录包含已审阅 spec 的实际起始提交，不直接假定研究 revision 仍是最新主线。
- 检查时 tracked 文件干净；codex/、temp/ 和七份研究 Markdown 是已有 untracked，不能纳入本任务提交或清理。
- pi/ 是被本地 exclude 忽略的独立 checkout，不是 submodule，不会自动出现在新 worktree。当前参考提交为 1aa3c02d56635ec40e7c8448d7eff35022e95740。
- 参考 Pi 包为 @earendil-works/pi-coding-agent 0.79.4，Node 至少 22.19.0。npm 版本/engine 元数据已核对；发布 tarball 的接口与资源仍需实际验证。
- 只改本规格涉及的产品代码。保留 Studio、上游参考仓库、用户配置、既有 memory DB 和运行数据。

## 2. 任务图：先契约，再三路并行

~~~mermaid
flowchart TD
  B[确认基线与spec提交] --> C0[C0 公共契约和smol过渡适配]
  B --> D[Pi发布包接口验证与依赖锁定准备]
  C0 --> F[冻结绿色公共提交]
  D --> F
  F --> S1[S1 smol专属实现收口]
  F --> G1[G1 工具治理/MCP/公共服务]
  F --> P1[P1 Pi SDK/bridge/runtime adapter]
  F --> V1[V1 验收fixture与独立oracle]
  S1 --> I0[I0 串行集成与发行接线]
  G1 --> I0
  P1 --> I0
  V1 --> A[最终Application/安装/故障验收]
  I0 --> A
  A --> M[合并main并核对交付revision]
~~~

关键路径：C0 → 较慢的 G1/P1 → I0 → 最终验收 → main。S1 与测试准备不必等待 Pi 实现完成。

建议同时运行三个实现者：S1、G1、P1；集成负责人维护契约和 V1。若有独立测试开发者，V1 可使用单独 worktree，但共用验收资产仍只有这一个所有者。开发资源不足时，先结束 S1，再将该实现者转到 V1。

Pi 发布包验证可与 C0 的代码调查、测试基线同时进行。它必须在契约冻结前完成，防止根据本地源码里未发布的接口设计整个桥接器。

## 3. 分支和 worktree

建议将 worktree 放在仓库外的同级目录 /Users/bytedance/code/data_clear/AgentLoom-worktrees，避免被 Application 搜索、打包或工具索引误纳入。

| 角色 | 分支 | worktree 目录名 | 从哪里创建 |
| --- | --- | --- | --- |
| 集成负责人 | codex/pi-integration | integration | 实施开始时确认的 main |
| C0 契约 | codex/pi-contracts | contracts | integration 起始提交 |
| S1 smol | codex/pi-smol-boundary | smol | C0 合入后的同一冻结提交 |
| G1 工具与服务 | codex/pi-tool-governance | tools | 同一冻结提交 |
| P1 Pi | codex/pi-runtime | pi-runtime | 同一冻结提交 |
| V1 验收，可选独立人员 | codex/pi-acceptance | acceptance | 同一冻结提交 |

只在依赖门禁通过后创建对应功能分支；不要现在把所有分支从未经 C0 改造的 main 拉出去。

每个 worktree 拥有独立虚拟环境、Node 安装目录和测试缓存。使用该 worktree 自身的源码安装，记录实际 import origin，避免 editable install 指向其他 worktree。uv 下载缓存可以共享，运行数据库、checkpoint、Pi session 和工具工作目录不能共享。

开发证据放在 worktree 外独立目录，按任务 ID、revision、attempt 区分。真实模型凭证通过现有未跟踪配置机制提供，不提交或输出到验证报告；只给对应 Worker/bridge 所需的凭证。

## 4. C0：公共契约与可运行过渡

**必须先完成，验收通过才能开放 S1/G1/P1。**

目标：让 Pi 可以接入，而现有 smol 应用在此提交仍然可运行。这里是实现公共接口和最小过渡代码，不是只写一份接口文档。

必须交付：

1. 更新完整运行定义：模型选择与 ModelTurnBinding 解耦；backend options 与公共语义分离；统一调用结果、取消、checkpoint/能力要求。
2. 确定 runtime_options 的默认、旧字段兼容、来源和冲突语义；防止 smol 全局默认流入 Pi。不得新增记忆配置。
3. 同时调整公共构造、validation/readiness 和 runtime registry，并完成 smol factory 的最小消费适配。不允许先破坏 smol，再留给 S1 修复。
4. 去掉 fresh Worker 对“有没有 Python model binding”的判断；真实 Worker 创建和隔离不能在 Pi 路径退化为共用对象。
5. 冻结 native tool prepare/settle 与普通 AgentLoom tool invoke 的共同语义、工具 provider/operation metadata、身份关联和错误类别。
6. 冻结 Python/Node JSONL 协议版本及 golden fixtures。明确最终参数、拒绝、结果提交确认、取消、native 状态引用及进程死亡语义。
7. 明确公共 Goal/Stop 与 native completion 的边界；保持 smol 当前完成规则，Pi 不强制 final_answer 或 smol Todo。C0 同批移走 tool_gateway 中的 final_answer_binding 并调整 agent 构造的通用注入，让 smol 自己提供 terminal 工具；同时修正所有依赖旧入口的现有测试，不留给 S1/G1 互相等待。
8. 对实际发布 Pi 包验证 SDK 导出、工具包装/覆盖、受控资源加载、原生模型接口、自动压缩和 session 恢复；将准确版本、Node版本、包 integrity 和验证结果交给 P1。
9. 将后续需要移动的公共 smol-only prompt/error-recovery 文件整文件移交 S1，或者在本阶段搬完。写清移交清单。
10. 冻结独立调用结算日志的公共合同：保留 ToolCallRecord 的 completed/error/blocked 终态；pending、执行中、已提交和 uncertain 在单独的 task-scoped journal 表达。提交确认不依赖 fail-open observer。G1 实现持久化结算，I0 串行接入原生会话与 checkpoint 恢复。

发布包 PoC 还有两个必须明确的兼容门禁：

- Pi 当前先校验参数、再执行 beforeToolCall；普通 tool_call/execute 包装不能自动满足现有 Hook 的顺序。用“原始参数不合法→Hook修正→最终参数合法”的案例验证真正可行的入口。仅修改原本合法的参数不够。若选定SDK无法满足，C0不能以绿色完成，须先明确SDK/合同调整方案；不能让S1/G1/P1各自绕过ADR。
- 源码中已找到两个初验前候选：同步 prepareArguments 不能直接等待 Python IPC；异步 message_end 扩展可返回替换后的 assistant message，发生在工具初验前。后者可作为 PoC 起点，但扩展异常会被捕获，执行门必须独立核验变换/授权状态。验收保持严格模型 schema，覆盖非法参数被异步修正、失败不执行、Hook不重复运行、多工具批次时序和原始/最终参数证据。源码可行不等于发布包 gate 已通过。
- 模拟 Python 已提交工具结果、Pi 尚未写入对应 toolResult 的崩溃；验证可用已提交结果补齐原生会话而不再次执行。无法安全对齐的状态应明确拒绝自动恢复，并作为支持边界记录。

这两个PoC可使用治理fixture与真SDK，不要求提前完成G1整个生产管线。实际Pi发布物的每一种启用工具还须记录查询限额、展示截断和原文捕获入口；未收集到的搜索结果不能宣称可从artifact恢复。

冻结产物至少覆盖以下字段/语义：

| 合同 | 最少内容 |
| --- | --- |
| Runtime | Agent角色/描述、模型profile、backend options、工具manifest、task/Run/instance、requirements、result/error、checkpoint |
| Model projection | 显式协议/API、provider/model、endpoint、支持参数、每实例凭证来源、不支持映射的诊断 |
| Tools | logical ID、native visible name、provider、schema、操作类型/目标参数、证据与完整产物处理 |
| Native prepare | call身份、原始输入、最终允许输入、实现提供方、工作目录、一次性授权或拒绝 |
| Native settle | 授权关联、实际执行结果/error、完整artifact引用、证据、task-scoped journal提交确认/不确定结果、native session位置 |
| IPC | handshake、request/response/event、版本、关联ID、序号、启动与终态区别、snapshot/cancel/close、超时和进程死亡 |

C0 验收：

- 现有 smol runtime/config/模型协议与工具管线定向测试通过；公共改造没有破坏已有 smol Application。
- 受控 native runtime fixture 不构造 ModelTurnBinding 也能被创建和调用；并发 Worker 确实得到不同实例与上下文。
- 协议 fixture/合同状态转换测试表达拒绝不执行、最终参数、稳定 call ID、blocked/error 和写前保护顺序；现有 Python 工具治理保持通过。此阶段不宣称真实 native prepare/settle 生产管线已经实现，真实执行门禁归 G1。
- 尚未完成 Pi 时不宣称生产可用；smol 正常注册，Pi 未就绪明确失败，没有偷偷回退。
- 公共协议和包版本验证可复现；三条后续分支从同一个绿色提交出发。
- fake runtime 在此仅证明合同，不计入 Pi 功能完成度。

## 5. 三条并行实现线

### S1：smol 专属实现收口

依赖：C0。分支：codex/pi-smol-boundary。

交付：

- 将 planning_interval、smart_summary、smol prompt template、模型错误恢复和原生 terminal 实现收进 smol adapter。
- 保持 smol 的原生消息/模型协议回放、现有 Todo 回填、上下文压缩和同基座恢复行为。
- 清理 C0 指定移交的公共 smol-only 文件及内部引用，不顺手移动平台记忆、ContextRef 或 Goal 所有权。
- 适配器返回共同结果，不向公共调用者暴露 smol 类型。

完成证据：smol 定向测试、公共 import 边界测试、旧 YAML 兼容与现有 checkpoint/Goal 行为回归。不得以删除原验收来获得绿色。

### G1：工具治理、MCP 与公共服务接入

依赖：C0。分支：codex/pi-tool-governance。

交付：

- 在现有 Tool Gateway 实现 native executor 的 prepare/settle 路径，共用 Hook、解码、权限、文件历史、记录、产物和可信证据规则。
- 明确逻辑工具与 Pi native name/schema 的映射；排除重复工具，拒绝意外覆盖。无需 Node 即可用 executor fixture 验证治理。
- 处理名称变化下的 path metadata、写前备份、Shell 命令限制和递归搜索过滤。不得把只检查显式 path 参数当作 Shell 隔离。
- 去掉 MCP 工具构造的 smol 前置依赖，保留连接/工具实例隔离和清理。
- 让 Goal、memory/history、ContextRef、Worker 工具的定义与执行可供 Pi 桥接。Goal/Todo 的少量 smol decorator 迁移由本任务负责。
- 长期记忆保持当前存储、scope、候选和审核规则；只改确需的 runtime 接入，不新增 policy 产品。
- native 完整输出、失败、blocked 和证据的处理有测试，不将截断文本冒充完整原文。完整性限定在本次授权查询及其限额内，保留coverage/limit信息；输出重查不冒充原产物。

完成证据：治理契约、MCP隔离、文件/Shell保护、完整artifact检索、可信证据和跨应用边界测试。

### P1：Pi adapter、原生 provider 和 Node bridge

依赖：C0 及发布包验证。分支：codex/pi-runtime。

交付：

- 自有 Pi adapter 与独立 bridge package，精确 dependency/lock；不依赖本机 pi/ checkout 或 TUI 的 Node 环境。
- 使用实际发布的 AgentSession SDK；模型配置转换给 Pi native provider，原生上下文与自动压缩留 Pi。
- 显式控制工具、资源、auth、settings；为官方基础工具包装 native prepare/settle，为 AgentLoom 工具实现跨进程 invoke。
- 提供声明式原生工具manifest与参数映射，由 G1 的共同合同校验；不能在 Node 私写另一套权限引擎。
- 实现协议持续读帧、双向回调、事件/终态、进程隔离、取消、清理、原生session artifact及同基座恢复。
- 使用协议 fixture 模拟 Python 服务独立开发，使用真实 Pi SDK 和确定性 provider 驱动 native 循环；不能把返回固定答案的假 runtime 作为交付。

完成证据：真 SDK 的基础文件/Shell工具、平台工具回调、原生provider映射、压缩/session恢复、双向调用与取消测试；构建和锁文件可复现。

## 6. V1：并行准备验收，集成后执行全矩阵

依赖：C0 后可以准备；G1/P1/S1 集成后才能完成最终验证。

唯一所有者准备：

- 一个经 execute_app 执行的最小真实 Application 及可切换 runtime 的变体。
- smol Supervisor/Pi Worker 和 Pi Supervisor/smol Worker 两个混合方向，均通过平台 Worker 工具调用，以及并发身份隔离。
- 真 Pi SDK＋确定性 provider 响应；文件、Shell、平台工具、记忆与产物使用真实实现。
- 独立 oracle 检查实际文件、记录、工具参数、call identity、产物原文和 memory，不接受模型自述成功。
- 每个尝试独立 evidence 目录和结果摘要，区分 PASS、FAIL、NOT-RUN。
- cancellation、进程退出、协议损坏、副作用已发生但未提交，以及host已提交但native toolResult尚未持久化这两个不同窗口的可复现故障点。

最终必过项与 spec 的 A01–A14 一一对应。真实 provider smoke 单独报告；凭证不可用不能算通过，也不能阻塞可独立完成的确定性测试准备。

## 7. 文件唯一所有权与移交

下表路径相对仓库根目录，用于开发调度；规范正文只约束 module 和行为，不把这些路径当稳定 API。

| 所有者 | 独占范围 | 不得自行修改 |
| --- | --- | --- |
| C0，结束后移交 I0 | src/runtime/agent_runtime.py、agent.py、factory.py、invocation.py、tool_protocol.py；src/application/definition.py、validation.py、readiness.py、runner.py；src/configuration/**；共享 runtime/结算journal 合约和协议 fixtures | 其他任务拥有的功能实现，除明确约定的最小 smol 过渡及 terminal 入口拆分补丁 |
| S1 | src/adapters/smolagents/**；tests/smolagents_test/**；C0 明确整文件移交的 runtime/prompts 中 smol-only 文件及 runtime/error_recovery.py | tool_gateway、公共构造/registry/configuration、self_learning、MCP、锁文件 |
| G1 | src/runtime/tool_gateway.py、hooks/path_validators.py、hooks/runtime.py、trusted_memory_evidence.py、checkpoint/file_history_hook.py、context_engine/**；新 runtime/checkpoint/tool_journal.py 的持久化实现；src/tools/catalog.py、tool_meta.py、loader.py、goal/、todo/ 和确需的基础工具治理提取；src/adapters/mcp/**；确需的 src/self_learning/** 适配 | smol adapter、Pi实现、共享 runtime/结算合同、注册和模型选择、锁文件 |
| P1 | 新 src/adapters/pi/**；该目录下独立 bridge manifest/lock/build；Pi 专属测试 | 公共 gateway/config/registry、Python manifest/lock、TUI manifest/lock |
| V1 | 新 tests/pi_acceptance_test/**、applications/pi_runtime_validation/** 与独立oracle/fixtures | 生产模块、各adapter专属单测、公共contract fixture |
| I0 | C0 移交范围；src/__main__.py；src/runtime/checkpoint/coordinator.py、checkpoint_manager.py；pyproject.toml、uv.lock、安装/CI；公共验收接线、文档 | 未接收的开发分支内部修改 |

细化规则：

- G1 如需提取 Shell 或搜索中的治理逻辑，在开工清单中明确整文件所有权；S1/P1 不触碰这些文件。
- C0→S1 的公共旧文件移交在分支创建前登记，不允许 G1 同时修改其中 Goal/Todo 引用。公共 orchestration 的剩余接线统一提交给 I0。
- 现有 tests/agent_test、tests/skills_test、根级恢复测试中依赖旧 terminal/prompt 入口的地方由 C0 先修正；需继续交给 S1 的测试必须列入整文件移交清单。其余公共测试归 I0，G1 独占自己的 hooks/tools/MCP/self_learning 定向测试，避免共同修改宽泛的测试目录。
- 不通过一次“全局格式化”“批量rename”跨越所有权边界。
- P1 可在自己的 bridge 中增加 Node 依赖；Python dependency/lock 与 installer 由 I0 一次整合。C0 的包验证使用临时隔离环境，不修改 TUI 锁文件。
- 合同修订由 I0/C0 生成独立提交和新fixture。受影响分支统一更新到该提交，再继续；禁止三条分支各加私有临时字段。
- 不在多个分支重复 cherry-pick 功能补丁。共用合同通过统一祖先/集成提交传播；功能提交只合入一次。

## 8. I0：串行集成、依赖和发行

集成不是最后一次大合并。每条任务完成后就合入 integration 并跑受影响检查，较早暴露接线问题。

建议顺序：

1. C0 合入 integration，记录绿色 commit 和协议版本，建立 S1/G1/P1 worktree。
2. S1 完成即可合入；它和 G1 没有功能依赖，先完成的可以先合，合后保住 smol。
3. G1 合入后验证现有 smol 工具治理与平台记忆仍正确。
4. P1 在 G1 协议已落地后合入。同次由 I0 更新注册、配置/readiness、平台服务注入和 native checkpoint 协调，并验证一个真实 Pi Application。P1 可以提前完成SDK实现，但不能在缺治理时以生产可用状态注册。
5. I0 拆分可选 smol 依赖、移除 CLI 失败路径的硬依赖，完成 bridge 构建/分发、干净安装与 Node readiness。保留现有推荐 smol 安装路径的体验；smol 专属 instrumentation 等间接依赖也必须一起归入其安装profile。
6. 接入 V1，全量执行 A01–A14、既有必需 CI 和安装矩阵，形成最终revision报告。

Pi注册、依赖、shared construction 接线不能拖到“测试之后”。只对实现、依赖和治理均已就绪的 backend 宣告能力；未知或未装runtime仍在preflight失败。

目标安装profile在I0落地：保留推荐smol路径，显式选择 smolagents extra；新增Pi-only路径，选择 pi extra而不选择smol或携带smol的开发group。现有 uv sync --locked --all-groups 不会自动安装新增extra，installer和CI须同步修改。新增profile命令在I0文档中给出并实际执行，不能冒称当前仓库已经支持；两种profile都要在干净环境验证已安装分发物，而不是复用开发机已装依赖。

## 9. 测试执行与证据

以下命令在当前基线已存在，实施时按实际受影响范围选择。它们是计划，不是本轮已经运行的测试。

从待验证 worktree 根目录执行契约与配置检查：

~~~sh
uv run pytest tests/application_test/test_runtime_adapter_config.py tests/lib_test/runtime/test_agent_runtime_contract.py tests/lib_test/runtime/test_runtime_definition_seam.py -q
~~~

治理与记忆先例：

~~~sh
uv run pytest tests/lib_test/runtime/test_tool_gateway_pipeline.py tests/hooks_test/test_tool_runtime_boundary.py tests/self_learning_test/test_evidence_gate_v6.py tests/self_learning_test/test_session_event_importer.py -q
~~~

生命周期与CLI先例：

~~~sh
uv run pytest tests/test_runner.py tests/test_cli_run_observability.py tests/test_cli_run_transport_exit.py -q
~~~

新增 Pi acceptance 命令由 V1 交付并接入 CI，不把尚不存在的测试文件写成已可执行命令。Pi suite 必须安装锁定的真实 SDK；跳过 Pi suite 不能让新增功能显示绿色。

现有高成本真实模型检查可选取受影响场景在最终集成执行：

~~~sh
python tests/acceptance/existing_application_validation.py --case all --workspace /new/private/evidence
python tests/agent_test/real_checkpoint_validation.py --scenario all --workspace /new/private/checkpoints
~~~

上面的证据路径是占位符，执行时必须换成新的私有目录，不复用用户已有运行目录。真实模型调用需要实际配置；不可用则记录 NOT-RUN 和原因。不要照搬旧文档中已删除的 CodeAct 或 Goal budget 场景。

验收层级：

- 分支门禁：所属模块定向测试、既有公开合同；P1 必须包含真 Pi SDK 测试。
- 集成门禁：A01–A14 确定性端到端、已有全套要求、Python/Node构建、发行资源、Pi-only无smol环境。
- 真实provider验证：按实际可用配置运行并保留证据；不能用旧revision成功记录替代。未运行时交付说明必须明确写出限制。已运行而失败必须修复后复验，或撤回该映射的支持声明并验证明确拒绝；不得以“可选”或 NOT-RUN 掩盖已知失败。
- 最终smol和Pi状态：主入口返回值、持久化Run receipt、真实tool ledger和独立文件检查一致。

重点回归不是逐个私有函数重测：旧应用兼容、治理在副作用前生效、长期记忆可跨基座复用、完整产物可检索、同基座恢复不重跑已提交工作、进程故障不会伪造成功。

## 10. 合并 main 的流程

1. 各实现者在自己的 worktree 提交范围明确的 commit，附测试命令、退出码、revision、Node/Pi版本和未完成项。
2. I0 检查分支基于约定合同且没有越界改共享文件。冲突由该文件所有者和I0解决，不能用 ours/theirs 批量覆盖。
3. 功能分支逐一 merge 到 integration，保留任务边界；不要让每个开发者分别 merge main。
4. 锁文件由唯一所有者根据合并后的manifest重新生成并验证，不手工拼冲突片段。
5. 所有修改完成后记录候选提交，运行最终门禁。验证中发生修复，要在修复后的候选重新运行受影响检查；不得引用修复前的报告宣称完成。
6. 合并前重新检查 main 是否前进。若已前进，先把最新 main 合入 integration，解决冲突并验证最终树；不能将旧 main 上的测试视为最终结果。
7. main 未前进时优先将已验证 integration fast-forward 到 main。仓库若要求 PR/merge commit，则验证最终合并树与候选一致，并保留候选和merge SHA的对应关系。
8. 合并后核对 main 的内容、来源revision、版本和验收报告；先保留worktree与证据，确认无未提交成果后再单独清理。

禁止用重置 main、强推或删除用户运行数据来消除冲突。该条保护实际协作状态，不引入本轮之外的发布流程。

## 11. 交付清单

- [ ] C0绿色合同提交与Pi实际发布包验证记录。
- [ ] S1旧smol兼容证据。
- [ ] G1权限、备份、完整产物、MCP和memory证据。
- [ ] P1真Pi SDK、协议、模型、取消与恢复证据。
- [ ] V1独立Application验收及A01–A14映射。
- [ ] I0安装/注册/分发接线和Pi-only无smol环境结果。
- [ ] 最终集成revision及真实provider PASS/FAIL/NOT-RUN说明。
- [ ] main合并记录与旧规范替代关系。

全部复选框保持未勾选，直到对应实现和验证真实完成。当前只完成本规格及开发计划的编写。
