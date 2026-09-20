# 高热度 Python Agent runtime 候选调研

日期：2026-09-18

范围：为 AgentLoom 选择第二个真实 `AgentRuntimeAdapter` 候选。本文只做开源项目和当前依赖约束的研究，不修改根目录 `RESPONSES_RUNTIME_SPEC.md`，也不修改运行时代码。

## 结论

AWS Strands 的 stars 确实显著少于主流项目：截至本次查询，迁移后的 [`strands-agents/harness-sdk`](https://github.com/strands-agents/harness-sdk) 约 7.3k stars。它依然是一个很好的 **runtime interface 参考**，因为公开的 [`AgentBase` Protocol](https://github.com/strands-agents/harness-sdk/blob/270686077c97a5f9c8647ad6f9bc35246392e822/strands-py/src/strands/agent/base.py) 很小，完整 run、model seam、event loop 和 session ownership 的边界也很清楚；但如果第二个实现必须同时满足“更热门、多人维护、长期方向明确”，不应该把它排在首位。

推荐 shortlist：

1. **Microsoft Agent Framework（MAF）**：最适合当 AgentLoom runtime seam 的接口标杆，也是“完整运行时可替换”最直接的一手样本。它公开了结构化的 [`SupportsAgentRun`](https://github.com/microsoft/agent-framework/blob/b0437908a2df93271296dd388b0747363811a42f/python/packages/core/agent_framework/_agents.py#L230-L363) Protocol，明确允许完全自定义的 agent；同时提供 `AgentSession`、function-invocation middleware、Agent-as-Tool、multi-agent workflow 和 workflow checkpoint。AutoGen 官方把它指为后继项目，MAF 也同时提供 Semantic Kernel 迁移指南。缺点是当前 AgentLoom 的 OpenTelemetry 1.26 固定版本与 MAF core 的 `opentelemetry-api>=1.39` 不兼容，接入前必须先解决依赖边界。
2. **Pydantic AI**：最适合做第一个可运行的第二 adapter。它是进程内 Python runtime，内部 message/part、toolset、run event 和可序列化 run result 都很清楚；AgentLoom 可以用自定义 `AbstractToolset` 强制所有工具经过 Tool Gateway。它比 MAF 更容易做单 Agent 纵切，也最接近 Responses item 保真目标。缺点是内建多 Agent 更偏 delegation / subagent pattern，durable resume 要接 DBOS、Temporal、Prefect 或应用自己的持久化；当前包也要求 `opentelemetry-api>=1.28`，仍需升级 AgentLoom 的 OTel 版本。
3. **OpenAI Agents SDK**：最适合验证 Responses-native 语义和 item replay，但不应是唯一第二基座。它的 `Runner.run()`、`RunResult.new_items/to_input_list()`、handoff、session、guardrail 很成熟；也有 LiteLLM 扩展。缺点是当前版本要求 `openai>=3,<4`，而 AgentLoom 固定 `openai>=2.8.1,<3`，直接进程内安装会冲突；跨 provider 路径也不是它的核心优势。
4. **LangGraph**：最适合后续作为 graph runtime adapter，尤其适合检验 checkpoint/resume、interrupt、并发和事件模型，但不适合作为最小第二 adapter。它的 Pregel runtime 太强，接入时容易把 AgentLoom 的 Supervisor/Worker 编排和 checkpoint ownership 一并改写，无法只证明一个小的 runtime seam。

不建议：

- **AutoGen**：历史热度最高，但官方 README 已明确进入 maintenance mode，并要求新用户转向 Microsoft Agent Framework；不能把 61k stars 当成当前技术方向。
- **Semantic Kernel**：仍在维护，但 MAF 已提供从 Semantic Kernel 迁移的官方路径，而 AutoGen README 也明确把 MAF 指为自己的后继。新接入 Semantic Kernel 旧抽象的收益不如直接评估 MAF。
- **CrewAI**：很热门、更新活跃，但核心抽象是 Crew / Task / Flow，不是一个可被 AgentLoom 安全包住的最小完整-run agent protocol；事件系统主要适合观察，难以保证 AgentLoom Tool Gateway 在所有工具副作用前拥有唯一控制权。
- **LlamaIndex AgentWorkflow**：成熟且热门，但项目重心和抽象都偏数据/RAG + Workflow。作为完整 adapter 可行，作为第二个最小验证实现会引入不必要的 Workflow/Context/Store 语义。
- **Agno**：社区大、更新非常活跃，也公开了极小的 `AgentProtocol`；但其 `Agent` 自己同时管理 session、memory、reasoning、hooks、tools、persistence 和 model tool loop。它很适合参考外层 protocol，不适合在第一轮作为受 AgentLoom Tool Gateway 强治理的第二实现。
- **Google ADK**：组织和活跃度都强，事件与 session 也完整；但 `Runner` 同时接管 artifact/session/memory/credential/plugin services，核心依赖较重，且 OTel 版本与 AgentLoom 当前固定版本冲突。它更适合后续企业 runtime adapter。

最终建议不是“选一个新框架替换 smolagents”，而是：

- 先把 `smolagents` 降为第一个 `AgentRuntimeAdapter`；
- 用 **MAF 的 `SupportsAgentRun` 形状**定义 AgentLoom 自己的小型完整-run contract；
- 用 **Pydantic AI**做第一个第二 adapter 纵切；
- 用 **OpenAI Agents SDK**校验 Responses item/replay 语义；
- 等 seam 成立后再接 **LangGraph** 这种强 graph runtime。

这样既避免被某个底座绑死，也不会为了“支持多底座”把所有框架内部 step 模型抽成一个失控的最大公约数。

## 项目热度与维护状态

数据来自 GitHub repository API / 官方仓库，查询于 2026-09-18。stars 和 forks 只是生态信号，不等于架构适配性；“最近活动”取仓库 metadata 的 `pushed_at`，并结合主分支近期提交和官方 README 判断。“贡献者”是 GitHub contributors endpoint 返回的去重账号数，“近百提交作者”是最近 100 个 commit 中的去重 GitHub 账号或 author email，包含 bot，只用来判断是否明显依赖单一维护者。

| 项目 | Stars | Forks | 贡献者 / 近百提交作者 | 最近活动 | License | 维护组织 | 当前判断 |
| --- | ---: | ---: | ---: | --- | --- | --- | --- |
| [Microsoft AutoGen](https://github.com/microsoft/autogen) | 61,042 | 9,230 | 443 / 33 | `pushed_at` 2026-04-15 | 代码为 MIT（仓库另有 CC-BY 文档许可） | Microsoft / 社区 | 官方已进入 maintenance mode，不选 |
| [CrewAI](https://github.com/crewAIInc/crewAI) | 58,728 | 8,498 | 335 / 44 | 2026-09-18 | MIT | CrewAI Inc. | 热门，但 seam/治理适配性低 |
| [LlamaIndex](https://github.com/run-llama/llama_index) | 52,209 | 8,166 | 476 / 61 | 2026-09-18 | MIT | Run Llama | 热门，但 Agent runtime 不是项目唯一重心 |
| [Agno](https://github.com/agno-agi/agno) | 42,234 | 5,950 | 442 / 39 | 2026-09-18 | Apache-2.0 | Agno | protocol 很好，runtime ownership 太宽 |
| [LangGraph](https://github.com/langchain-ai/langgraph) | 41,876 | 7,075 | 279 / 18 | 2026-09-18 | MIT | LangChain | 强 graph runtime，推荐后续接 |
| [OpenAI Agents SDK](https://github.com/openai/openai-agents-python) | 29,545 | 4,765 | 378 / 28 | 2026-09-17 | MIT | OpenAI | Responses-native，依赖当前冲突 |
| [smolagents](https://github.com/huggingface/smolagents) | 29,380 | 2,974 | 202 / 38 | 2026-08-25 | Apache-2.0 | Hugging Face | 保留为第一 adapter，不再当不可替换核心 |
| [Semantic Kernel](https://github.com/microsoft/semantic-kernel) | 28,575 | 4,774 | 402 / 17 | 2026-09-18 | MIT | Microsoft | 有完整 Agent API，但新项目优先 MAF |
| [Google ADK](https://github.com/google/adk-python) | 21,569 | 4,028 | 422 / 34 | 2026-09-18 | Apache-2.0 | Google | 活跃、完整、接入较重 |
| [Pydantic AI](https://github.com/pydantic/pydantic-ai) | 20,024 | 2,732 | 476 / 16 | 2026-09-18 | MIT | Pydantic | 推荐首个第二 adapter |
| [Microsoft Agent Framework](https://github.com/microsoft/agent-framework) | 13,575 | 2,336 | 259 / 33 | 2026-09-18 | MIT | Microsoft | 推荐 contract 标杆 / 强候选 |
| [AWS Strands Harness SDK](https://github.com/strands-agents/harness-sdk) | 7,347 | 1,153 | 290 / 32 | 2026-09-18 | Apache-2.0 | AWS / Strands Agents | 接口参考强，生态规模较小 |

补充：

- AutoGen 仓库顶层 license metadata 显示 CC-BY-4.0，是因为仓库文档使用 CC-BY；其 Python package 的 `LICENSE-CODE` 和 `autogen-agentchat` package metadata 是 MIT。[代码许可](https://github.com/microsoft/autogen/blob/main/LICENSE-CODE)；[package metadata](https://github.com/microsoft/autogen/blob/main/python/packages/autogen-agentchat/pyproject.toml)
- “最近有 commit”不能独立证明维护方向。AutoGen 就是反例：官方 [`README`](https://github.com/microsoft/autogen/blob/main/README.md) 明确声明 maintenance mode，并指向 MAF。
- MAF、Pydantic AI、LangGraph、OpenAI Agents SDK、Agno、Google ADK、CrewAI 和 Strands 的近期主分支都有多笔功能/修复提交；维护活动不是只靠一次版本 bump。

## 评估标准

AgentLoom 需要的不是另一个 model provider，而是能包住一次完整 Agent run 的底座。候选按以下问题评估：

1. **完整 run seam**：能否在进程内调用一个稳定的 run/stream interface，而不让上层理解底层 step。
2. **Tool Gateway 可控性**：能否把模型看到的所有本地工具替换成 AgentLoom proxy，保证权限、Hook、side effect guard 和 `ToolCallRecord` 不被绕过。
3. **checkpoint/resume**：是否有可序列化的会话或运行状态；是否能把 runtime-specific state 装进 AgentLoom 的 versioned opaque payload。
4. **multi-agent**：是否支持 Agent-as-Tool、handoff、team/workflow；能否通过 capability 声明，而不是假装所有 runtime 语义相同。
5. **事件/观测**：能否稳定产出 model/tool/agent/terminal events，或至少有 hooks/middleware 可转成 AgentLoom events。
6. **进程内可用**：是否是 Python package，而不是只能通过远端 server。
7. **依赖兼容**：能否和 AgentLoom 当前 Python 3.12、Pydantic 2、LiteLLM、OpenAI 2.x、OpenTelemetry 1.26 共存。

## Shortlist 详评

### 1. Microsoft Agent Framework

#### 为什么是最好的 interface 标杆

MAF 的 [`SupportsAgentRun`](https://github.com/microsoft/agent-framework/blob/b0437908a2df93271296dd388b0747363811a42f/python/packages/core/agent_framework/_agents.py#L230-L363) 是目前调研项目里最接近 AgentLoom 目标的公开 contract：

- 结构化 Protocol，不要求继承框架基类；
- `run(..., stream=False)` 返回完整 response；
- `run(..., stream=True)` 返回 `ResponseStream`；
- 显式接收 `AgentSession`；
- 单独提供 `function_invocation_kwargs` 与 client kwargs；
- 官方注释直接说“完全自定义 agent，无需使用任何框架类，也可以满足该 protocol”。

它还明确区分：

- Agent run：`SupportsAgentRun`；
- conversation state：`AgentSession`；
- tool interception：function-invocation middleware；
- graph orchestration：Workflow；
- workflow persistence：`CheckpointStorage`。

这和 AgentLoom 应该做的分层高度一致：外层是完整 run，工具治理是独立 boundary，workflow checkpoint 不冒充 conversation history。

MAF README 也明确把 durability、restartability、observability、governance、HITL、multi-provider 和 graph workflow 作为一等能力。[官方 README](https://github.com/microsoft/agent-framework/blob/main/README.md)

#### 对 AgentLoom 的适配

- **Tool Gateway：高**。function-invocation middleware 可在框架执行工具前后拦截；也可以只注册 AgentLoom proxy tools。
- **Checkpoint/resume：中高**。AgentSession 可保存会话；Workflow 有 checkpoint/resume。但 AgentLoom 仍要把它们作为 runtime-owned payload，不应转成全局统一 step。
- **Multi-agent：高**。支持 Agent-as-Tool、sequential/concurrent/handoff/group workflow。
- **事件：高**。run stream、middleware、workflow event 足以映射最小 AgentLoom event envelope。
- **进程内：是**。

#### 现实阻碍

AgentLoom 当前固定：

- `opentelemetry-sdk==1.26.0`
- `opentelemetry-exporter-otlp==1.26.0`
- `openai>=2.8.1,<3.0.0`

MAF core 当前要求 `opentelemetry-api>=1.39,<2`；根 package 还会拉取很多 provider/hosting package。应优先尝试更小的 `agent-framework-core` 加所需 provider package，而不是 `agent-framework[all]`。[MAF core package](https://github.com/microsoft/agent-framework/blob/main/python/packages/core/pyproject.toml)；[OpenAI integration package](https://github.com/microsoft/agent-framework/blob/main/python/packages/openai/pyproject.toml)

**判断**：最适合定义 AgentLoom runtime contract，也可以成为真实第二 adapter；但在当前仓库直接实现前，必须先做依赖解析实验和最小纵切，不能假定现有 OTel pin 可共存。

### 2. Pydantic AI

#### 为什么最适合第一个第二 adapter

Pydantic AI 已经有 AgentLoom 需要的大部分深 seam：

- `Agent.run()` / `iter()` 拥有完整模型与工具循环；
- [`AgentRunResult`](https://github.com/pydantic/pydantic-ai/blob/41da7485fbc3b6aff7a98793c874e090171d7f84/pydantic_ai_slim/pydantic_ai/run.py#L712-L902) 能序列化 output、messages、usage、run id、conversation id 和 metadata；
- `all_messages()` / `new_messages()` 为继续运行和审计提供稳定输入；
- [`AbstractToolset`](https://github.com/pydantic/pydantic-ai/blob/41da7485fbc3b6aff7a98793c874e090171d7f84/pydantic_ai_slim/pydantic_ai/toolsets/abstract.py#L73-L260) 把工具列举、参数验证和执行统一到可替换接口；
- event stream 同时覆盖模型流和工具执行；
- cancellation 会保留已完成的 message history，可用于新 run 的 continuation。

因此可以实现一个 `AgentLoomToolset`：

- `get_tools()` 只暴露 AgentLoom 已批准的工具描述；
- `call_tool()` 只调用 AgentLoom Tool Gateway；
- Pydantic AI 永远拿不到真实 tool implementation；
- 返回值再映射为 AgentLoom `ToolCallRecord` / runtime event。

这比在底层 runtime 已执行工具后再“补审计”可靠。

#### 对 AgentLoom 的适配

- **Tool Gateway：高**。`AbstractToolset.call_tool()` 是明确的执行 seam。
- **Checkpoint/resume：中**。message history 可序列化；真正 durable step replay 需要 DBOS/Temporal/Prefect，或者 AgentLoom 自己保存 runtime payload。
- **Multi-agent：中**。官方支持 delegation/subagent，但不是像 AutoGen/MAF 那样统一 team runtime；适合 AgentLoom 当前 Supervisor/Worker 的 Agent-as-Tool 形态。
- **事件：高**。typed model/tool/run events 适合归一化。
- **进程内：是**。
- **Responses 保真：高**。framework-owned message parts 和 provider model adapter 是当前开源项目中最接近本次 spec 目标的实现之一。

#### 现实阻碍

当前 PyPI 的 `pydantic-ai-slim==2.45.0` 核心要求 `pydantic>=2.12`、`opentelemetry-api>=1.28`；`openai` extra 要求 `openai>=3.8`。[package metadata](https://github.com/pydantic/pydantic-ai/blob/main/pydantic_ai_slim/pyproject.toml)

两种可行路线：

1. **短期纵切**：只装 slim core，不装 OpenAI extra；实现一个 Pydantic AI `Model` wrapper，内部调用 AgentLoom 现有 `ModelProtocolAdapter` / LiteLLM。这样验证 runtime seam，不同时换 transport。
2. **后续原生 provider**：升级 AgentLoom OpenAI/OTel 依赖后，再直接使用 Pydantic AI 的 OpenAI / Anthropic models。

**判断**：如果目标是用最低改造成本证明“smolagents 真的可替换”，Pydantic AI 是首选第二 adapter。

### 3. OpenAI Agents SDK

[`Runner.run()`](https://github.com/openai/openai-agents-python/blob/fdf21db62c303a3db54b0dfbee82de2141fa2799/src/agents/run.py#L260-L379) 明确拥有完整 loop：agent invocation、final output、handoff、tool calls、下一轮。[`RunResultBase`](https://github.com/openai/openai-agents-python/blob/fdf21db62c303a3db54b0dfbee82de2141fa2799/src/agents/result.py#L312-L482) 保存 `new_items`、raw responses、final output、guardrails，并能 `to_input_list()`。

#### 对 AgentLoom 的适配

- **Tool Gateway：中高**。可用 function tool wrapper 把调用统一转到 AgentLoom；run hooks 可转事件。但必须确认 hosted/server tools 不会绕过本地 Tool Gateway。
- **Checkpoint/resume：高（Responses 语义）**。item replay、session、`RunState` 和 interruption 都很完整。
- **Multi-agent：高**。handoff 和 Agent-as-Tool 都是核心能力。
- **事件：高**。stream events 与 lifecycle hooks 都明确。
- **进程内：是**。
- **跨 provider：中**。有 LiteLLM/any-llm extras，但核心语义仍然偏 OpenAI Responses。

当前 PyPI 的 `openai-agents==0.22.3` 要求 `openai>=3,<4`，和 AgentLoom 的 `<3` 硬冲突。[package metadata](https://github.com/openai/openai-agents-python/blob/main/pyproject.toml)

**判断**：适合作为 Responses-native 语义 oracle，以及依赖升级后的第三 adapter；不适合在当前依赖图里直接作为第一实现。

### 4. LangGraph

LangGraph 的优势是 durable graph runtime，不是小型 Agent 类。它公开的 stream modes 包括 values、updates、custom、messages、checkpoints、tasks、debug，并在 Pregel loop 里管理 durability、interrupt、subgraph、checkpoint 和 superstep。[Pregel runtime](https://github.com/langchain-ai/langgraph/blob/c81c13533ee48c1ae0ef2de314737ef0c455f2be/libs/langgraph/langgraph/pregel/main.py#L2678-L3010)

#### 对 AgentLoom 的适配

- **Tool Gateway：中高**。可以用自定义 ToolNode / Runnable 调 AgentLoom proxy；但 prebuilt agent 不应被默认信任为治理边界。
- **Checkpoint/resume：最高**。这是它最强的理由。
- **Multi-agent：高**。graph/subgraph 是天然模型。
- **事件：高**。
- **进程内：是**。
- **接入复杂度：高**。它会和 AgentLoom 自己的 Application/Supervisor/Worker orchestration 产生 ownership 冲突。

**判断**：seam 稳定后接入。第二 adapter 首轮就选 LangGraph，会把“runtime 可替换性”与“重做 AgentLoom 编排”混成一个工程。

## 其他热门项目为什么不进 shortlist

### AutoGen

AutoGen 的 `AgentRuntime`、AgentChat team 和 typed messages 都很成熟，历史社区规模也最大。但官方已明确：

> AutoGen is now in maintenance mode. It will not receive new features or enhancements.

并要求新用户使用 Microsoft Agent Framework。[官方 README](https://github.com/microsoft/autogen/blob/main/README.md)

**判断**：可用于理解 message bus / team runtime，不应新增生产依赖。

### Semantic Kernel

Semantic Kernel 的抽象 [`Agent.invoke/invoke_stream`](https://github.com/microsoft/semantic-kernel/blob/ca40aa7226531d28a721d0ca0e451d0aaf86dafc/python/semantic_kernel/agents/agent.py#L191-L413) 很适合包装，且 Chat Completion 和 OpenAI Responses 有不同实现；但 MAF 已经提供官方 Semantic Kernel migration guide。Semantic Kernel Python package 依赖也很宽，包括 Azure agents、OpenAI、OTel、MCP 等。[package metadata](https://github.com/microsoft/semantic-kernel/blob/main/python/pyproject.toml)

**判断**：接口思想可参考，不新增第二个 Microsoft runtime 依赖。

### CrewAI

CrewAI 非常热门且当前更新活跃。它支持 Crew、Task、Flow、event listener、pause/human feedback 和 flow persistence。但这些能力围绕 CrewAI 自己的 orchestration 设计：

- 入口通常是 `Crew.kickoff()` / Flow；
- 事件 listener 主要用于订阅和观测；
- 工具与 agent/task lifecycle 深度绑定；
- 依赖面很重，包括 `opentelemetry-sdk>=1.42`、OpenAI 2.x、ChromaDB、LanceDB 等。[package metadata](https://github.com/crewAIInc/crewAI/blob/main/lib/crewai/pyproject.toml)

AgentLoom 若强制所有工具经过自己的 Tool Gateway，需要深入 CrewAI tool executor，而不是只包 `kickoff()`。这会形成一个浅 adapter。

**判断**：适合业务 Crew 编排，不适合首个 runtime seam 验证。

### LlamaIndex AgentWorkflow

LlamaIndex 生态成熟，Python core 维护者和包边界都清楚。[core package metadata](https://github.com/run-llama/llama_index/blob/main/llama-index-core/pyproject.toml)

但 AgentWorkflow 的 canonical state 是 Workflow Context / Store + LlamaIndex message/content blocks；tool loop、handoff 和 event 都属于 Workflow。要保持语义，AgentLoom 必须包完整 Workflow run。它可行，但引入的 RAG/data/runtime 依赖与 AgentLoom 当前目标不成比例。

**判断**：如果未来需要把 AgentLoom Application 映射为数据/RAG workflow，可以接；不用于当前最小第二 adapter。

### Agno

Agno 是一个值得重视的反例：它公开的 [`AgentProtocol`](https://github.com/agno-agi/agno/blob/cc6467641248e3e76ce42e1740e31d16814ea407/libs/agno/agno/agent/protocol.py#L7-L35) 非常小，明确服务 native、remote 和 external framework adapter。这强力支持了 AgentLoom 应该定义小型完整-run protocol 的判断。

但 Agno 的真实 Agent run 同时负责 session、metadata、dependencies、hooks、tool determination、reasoning、model tool loop、memory、structured output、storage 和 telemetry。[run implementation](https://github.com/agno-agi/agno/blob/cc6467641248e3e76ce42e1740e31d16814ea407/libs/agno/agno/agent/_run.py#L367-L620)

这意味着：

- 外层 `AgentProtocol` 很适合学习；
- 把 Agno Agent 嵌进 AgentLoom 后，AgentLoom Tool Gateway、checkpoint 和 memory ownership 仍需逐项夺回或显式让渡；
- 高 stars 不会自动降低这项语义整合成本。

**判断**：接口参考优先，真实 adapter 排在 Pydantic AI / MAF / OpenAI Agents SDK 之后。

### Google ADK

Google ADK 的 [`BaseAgent.run_async`](https://github.com/google/adk-python/blob/f33d4923388a963d0c5cbf8f7855a88d255b0dea/src/google/adk/agents/base_agent.py#L312-L438) 是完整 Agent event stream；[`Runner`](https://github.com/google/adk-python/blob/f33d4923388a963d0c5cbf8f7855a88d255b0dea/src/google/adk/runners.py#L192-L320) 管 session、artifact、memory、credential 和 plugin services。它还有很强的 subagent/transfer、event 和 resumability 设计。

问题在于接入边界很重：

- AgentLoom 必须把这些 service ownership 显式映射或禁用；
- core package 直接依赖 FastAPI、Google GenAI、OTel SDK 1.39+ 等；[package metadata](https://github.com/google/adk-python/blob/main/pyproject.toml)
- 它能进程内运行，但不等于适合轻量嵌入。

**判断**：适合后续企业 runtime adapter，不是当前最短路径。

### AWS Strands

Strands 仍然值得保留在架构参考集中：

- [`AgentBase`](https://github.com/strands-agents/harness-sdk/blob/270686077c97a5f9c8647ad6f9bc35246392e822/strands-py/src/strands/agent/base.py) 定义同步 invoke、异步 invoke 和异步 stream；
- Graph 接受任意 `AgentBase` implementation；
- model、event loop、tool executor、session manager 分层清晰；
- 支持 local/remote A2A agent。

但当前 Python package 要求 `opentelemetry-sdk>=1.30`，也和 AgentLoom 的 1.26 pin 冲突。[package metadata](https://github.com/strands-agents/harness-sdk/blob/main/strands-py/pyproject.toml)

**判断**：stars 不是唯一问题。即使暂不把它做第二实现，它依然是验证 AgentLoom runtime contract 是否足够小的优秀参考。

## Tool Gateway 的硬约束

第二 runtime 不是“能跑起来”就合格。它必须满足：

1. runtime 看到的每一个 local tool 都只是 AgentLoom proxy；
2. proxy 在执行真实副作用前进入统一 Tool Gateway；
3. pre/post hooks、权限、超时、取消、幂等键、敏感输出处理和 `ToolCallRecord` 都发生在 Gateway 内；
4. runtime 自己的 retry 不能让一个不幂等工具在 AgentLoom 不知情时重复执行；
5. provider-hosted tools、server-side tools、code execution 和 framework built-ins 必须在 capability contract 中显式声明；默认不开放；
6. Agent-as-Tool 同样通过 Gateway 或明确的 nested-run boundary，不能作为绕过路径。

按这个标准：

| 候选 | Tool Gateway 可控性 | 主要办法 | 风险 |
| --- | --- | --- | --- |
| Pydantic AI | 高 | 自定义 `AbstractToolset.call_tool()` | deferred/durable tool 需要单独映射 |
| MAF | 高 | function-invocation middleware + proxy tools | 不是所有 hosted agent 都保证支持 function middleware |
| OpenAI Agents SDK | 中高 | function tool wrapper + hooks | hosted/server tools 需禁用或单列 capability |
| LangGraph | 中高 | 自定义 ToolNode / Runnable | graph 可直接包含任意副作用节点，不能只管 tools |
| Google ADK | 中 | 自定义 BaseTool / plugin callback | Runner services 和 built-in tools 会扩大治理面 |
| Agno | 中 | proxy Toolkit / hooks | Agent/Model 两层都可能拥有工具 loop |
| CrewAI | 中低 | 自定义 BaseTool + listener/hook | listener 不等于执行前强制治理 |
| LlamaIndex | 中 | 自定义 AsyncBaseTool / workflow event | workflow node 可绕过 tool seam |

## Checkpoint / resume 的正确边界

不同 runtime 的恢复状态不能强行归一成同一套 AgentLoom items：

- Pydantic AI：message history + optional durable engine state；
- MAF：AgentSession + Workflow checkpoint；
- OpenAI Agents SDK：Responses-style items + `RunState` / session；
- LangGraph：graph channels、pending writes、next nodes、checkpoint ancestry；
- Google ADK：session events + resumability state；
- CrewAI：Crew/Flow-specific persistence；
- Agno：AgentSession、RunOutput、workflow/team state；
- smolagents：AgentMemory / ActionStep。

所以 AgentLoom checkpoint 应分两层：

```text
AgentLoom envelope
  runtime_id
  runtime_version
  state_schema_version
  neutral audit events
  runtime_payload (opaque, versioned)
```

第一版只允许由产生 checkpoint 的同一个 runtime adapter 恢复。所谓“统一 items”可以用于审计和展示，不能承诺无损恢复所有底座。

## 依赖兼容性

AgentLoom 当前关键约束来自根 `pyproject.toml`：

- Python `>=3.12`
- `pydantic>=2.7`
- `openai>=2.8.1,<3`
- `opentelemetry-sdk==1.26.0`
- `opentelemetry-exporter-otlp==1.26.0`
- `litellm>=1.72`
- `smolagents==1.26.0`

| 候选 | 关键依赖 | 与当前 AgentLoom |
| --- | --- | --- |
| Pydantic AI slim | Pydantic >=2.12；OTel API >=1.28；OpenAI extra >=3.8 | core 需要 OTel/Pydantic 升级；不用 provider extra 可避开 OpenAI 3 |
| MAF core | Pydantic >=2；OTel API >=1.39 | OTel pin 冲突；建议只装 core + 必要 provider |
| OpenAI Agents SDK | OpenAI >=3,<4；Pydantic >=2.12 | OpenAI pin 硬冲突 |
| LangGraph | Pydantic >=2.7.4；langchain-core/checkpoint/sdk | 核心相对可解，但新增 LangChain 生态依赖 |
| Google ADK | Pydantic >=2.12；OTel SDK/API >=1.39；FastAPI/Google GenAI | OTel 冲突且依赖重 |
| Strands | OTel SDK/API >=1.30；MCP；Boto3 | OTel 冲突，默认还引入 AWS SDK |
| CrewAI | OTel SDK/API >=1.42；OpenAI 2.30；ChromaDB/LanceDB | OTel 冲突，依赖面最大 |
| Agno | Pydantic/HTTPX/Rich 为主，provider 可选 | core 相对易装，但语义整合成本高 |
| LlamaIndex core | Pydantic >=2.8；SQLAlchemy、Numpy、NLTK、Workflow 等 | 可解但依赖面偏数据/RAG |
| Semantic Kernel | OpenAI >=2；OTel ~1.24；Azure agents 等 | 可能与 OTel pin 冲突，默认依赖宽 |

这说明第二 adapter 应先作为 **optional dependency** 或独立 package extra，不应把两个完整 Agent 框架都塞进 AgentLoom 默认安装。

本次还用 `uv pip install --dry-run` 对当前项目做了真实解析：

- `agent-framework-core==1.19.0` 与 AgentLoom 无解：OTel API 1.39+ 对 1.26；
- `pydantic-ai-slim==2.45.0` 与 AgentLoom 无解：OTel API 1.28+ 对 1.26；
- `openai-agents==0.22.3` 与 AgentLoom 无解：OpenAI SDK 3.x 对 `<3`。

如果不给候选依赖设置版本下限，解析器会“成功”选择旧版 `pydantic-ai-slim==0.0.30` 或 `openai-agents==0.18.1`。这种成功没有意义：设计依据和目标 runtime API 来自当前版本，不能靠自动降级到旧 API 假装兼容。正式 adapter 必须锁定经过验证的最低版本，并让依赖冲突显式失败。

## 对 `RESPONSES_RUNTIME_SPEC.md` 的影响

根 spec 当前写法仍不能实现“只加一个适配层就能切换不同基座”，因为它：

- 把 smolagents 定义成固定 runtime base；
- 把 AgentLoom items 定义成唯一 replay source；
- 把替换 smolagents 列为 Out of Scope；
- 只有 model wire adapter，没有完整-run runtime adapter；
- 测试 seam 仍是 item → smolagents `ChatMessage` projection。

应在实现前改成两层：

```text
AgentRuntimeAdapter
  capabilities
  build(agent_spec, runtime_deps)
  run(request, context) -> events + result
  resume(runtime_checkpoint, context) -> events + result

ModelProtocolAdapter
  request(items, tools, settings) -> model_turn
  stream(items, tools, settings) -> model_events
```

其中：

- `smolagents`、`pydantic_ai`、`microsoft_agent_framework`、`langgraph` 是 runtime adapter 名；
- `openai_chat`、`openai_responses`、`anthropic_messages` 是 model protocol 名；
- 两者不能共用同一个 `adapter` 配置字段。

## 推荐实施次序

### Phase 0：先修 spec

- 把 smolagents 从“不可替换基座”改成“默认 runtime adapter”。
- 新增 runtime capability、event、result、checkpoint envelope 和 Tool Gateway contract。
- 明确 runtime adapter 与 model protocol adapter 正交。

### Phase 1：机械包住 smolagents

- 外层调用者不再碰 smolagents `RunResult`、`ActionStep`、`memory.steps` 和 `reset`。
- smolagents adapter 内部行为先不改；先证明删除 adapter 时耦合集中在一个模块。
- code_act 清理仍可按原计划完成，但它属于 smolagents adapter 收敛，不是跨 runtime 抽象本身。

### Phase 2：Pydantic AI 最小纵切

先做一个受控 Application：

- 一个 Supervisor；
- 一个 Worker，Worker 通过 Agent-as-Tool 或 AgentLoom nested-run boundary；
- 两个 AgentLoom proxy tools；
- `final_answer`；
- tool call / result / terminal event；
- checkpoint envelope；
- 中断后同 runtime resume；
- 同一测试分别跑 smolagents 和 Pydantic AI。

先使用 Pydantic AI slim + AgentLoom 自有 model wrapper，避免同时升级 OpenAI SDK。这个纵切通过后，runtime seam 才算真实存在。

### Phase 3：MAF compatibility spike

- 尝试 `agent-framework-core` + 最小 provider package；
- 验证 OTel 升级后的 Langfuse / OpenInference / smolagents instrumentation；
- 用 `SupportsAgentRun` 自定义 agent 和 MAF native Agent 各跑一次；
- 验证 function middleware 是否能完整包住 AgentLoom Tool Gateway；
- 再决定 MAF 是正式第三 adapter，还是只作为 contract oracle。

### Phase 4：Responses-native oracle

用 OpenAI Agents SDK 的 Runner/RunResult 做行为对照：

- reasoning item；
- function_call / function_call_output；
- handoff；
- interrupted run；
- local item replay；
- response ID 仅作证据。

这一步可以先是测试 fixture / reference harness，不必立刻成为生产 adapter。

### Phase 5：LangGraph

只有在前三阶段稳定后才接：

- graph checkpoint/resume；
- interrupts / HITL；
- parallel tool groups；
- subgraph/multi-agent events；
- AgentLoom Application 到 graph 的显式编译。

## 最终推荐

如果现在必须选一个“比 Strands 更热门、多人维护、能真正验证 AgentLoom runtime seam”的项目：

**选 Pydantic AI 做第一个第二 adapter，选 Microsoft Agent Framework 做 contract 标杆和下一候选。**

原因：

- Pydantic AI 的 toolset、typed parts、run events 和 serializable result 与 AgentLoom 当前方向最接近，纵切成本最低；
- MAF 的 `SupportsAgentRun` 是跨实现 interface 的最佳公开样本，但当前 OTel 依赖冲突使它不适合无准备直接落地；
- OpenAI Agents SDK 用于 Responses 保真验证；
- LangGraph 用于后续 durable graph runtime；
- 不因 stars 选择已经 maintenance 的 AutoGen，也不因热度把 CrewAI/LlamaIndex 的编排语义硬塞进 AgentLoom。

这也回答了“用 smolagents 这个 runtime 是否不好”：问题不在 smolagents 小或差，而在 AgentLoom 当前把它当成不可替换的内部类型系统。smolagents 可以继续存在，但必须被包在 adapter 后面。只要 `ActionStep`、`RunResult`、`memory.steps` 还泄漏到 AgentLoom 主运行时，换任何更热门的框架都只是换一种绑定。
