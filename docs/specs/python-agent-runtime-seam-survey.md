# Python Agent runtime seam 开源调研

日期：2026-09-18

范围：只读架构研究。本文评估 AgentLoom 是否应建立可替换整个 Agent runtime 的 seam，并审查根目录 `RESPONSES_RUNTIME_SPEC.md` 是否已经支持该目标。本文不修改现有 spec 或运行时代码。

## 结论

当前 `RESPONSES_RUNTIME_SPEC.md` 不能实现“只加一个 adapter 就切换不同 Agent 基座”。

它定义的是 **model protocol seam**：`openai_chat`、`openai_responses`、`anthropic_messages` 选择一次模型调用的 wire protocol；同时又明确让 smolagents 永久拥有 Agent loop、工具执行、managed agents、callbacks、memory 和生命周期，并把“替换 smolagents”列为 Out of Scope。这只能切模型协议，不能切 Agent runtime。

如果“可替换基座”是正式目标，现有 spec 应在开发前重写，而不是在实现中顺手加一个抽象。需要明确拆成两个 seam：

1. **Agent runtime seam（外层）**：一次完整 Agent invocation。adapter 接收 AgentLoom 的运行请求、工具入口和运行上下文，返回标准化 Run events / result，并自行拥有多轮 Agent loop。
2. **Model protocol seam（内层）**：一次 model turn。它只负责 `chat`、`responses`、`messages` 等 wire protocol；可以由某个 runtime adapter 使用，也可以完全由第三方 runtime 自己实现。

推荐把 smolagents 保留为第一个 runtime adapter，而不是 AgentLoom 的核心继承基座。推荐 Strands Agents 作为第二个真实 adapter，用两个实现证明 seam 不是假想抽象。Pydantic AI 更适合作为 Responses item / message-part 保真设计参考，也可作为后续第三个 adapter 候选。

关键取舍：

- runtime adapter 必须包住**完整 run**，不能只包单步 model turn。
- AgentLoom 应拥有 Application、Run identity、Hook / 权限、ToolCallRecord、可审计事件和外层 checkpoint envelope。
- runtime adapter 应拥有自己的 conversation state、loop cursor、handoff 状态和可恢复 payload。
- AgentLoom 可保存一套 runtime-neutral audit projection，但不应声称它能无损替代所有 runtime 的内部状态。
- 第一版不承诺跨 runtime 恢复；checkpoint 必须带 `runtime_id` 和 runtime-specific opaque payload。

## 为什么 seam 必须在 Agent loop 外层

模型协议和 Agent runtime 不是同一个可变维度。

```text
Application / Supervisor / Worker
                |
                v
      AgentLoom Runtime Interface
       run(request, context)
                |
       +--------+---------+
       |                  |
smolagents adapter   Strands adapter
       |                  |
model protocol       model provider
adapter / LiteLLM    adapter
```

一个 runtime 决定的不只是如何请求模型，还包括：

- 何时继续下一轮；
- 哪些 tool call 可并发；
- 工具错误如何回灌；
- final answer 如何终止；
- handoff / managed agent 如何表达；
- callback / hook 时序；
- memory、context compaction 和 session 状态；
- checkpoint 在 model 前、model 后还是 tools 后；
- streaming event、usage 和 cancellation 语义。

把 seam 放在 model step 只能替换 provider。它无法替换以上行为。反过来，如果 AgentLoom 自己拥有这些行为，那么 AgentLoom 已经是 runtime，smolagents / Strands 就不再是“基座”，最多只是 model/tool 库。

因此必须先选清 ownership：

- **目标是可替换 Agent runtime**：完整 loop 归 runtime adapter；AgentLoom 只规范外层 run interface 和治理行为。
- **目标是 AgentLoom 自己的 item-native runtime**：AgentLoom 拥有 loop；smolagents 只能退化成工具和兼容库，不能再称为可替换基座。

当前 spec 同时写了“smolagents 保留 loop ownership”和“AgentLoom items 是唯一模型历史源”。这会产生双重 ownership：smolagents 的 `AgentMemory/ActionStep` 与 AgentLoom items 都需要驱动 replay。这个形态既不深，也不具备 locality；任何 loop 行为都要在两套状态间同步。

## 横向比较

“可替换整个 runtime 的 public interface”在本文中的含义是：调用方可以只依赖一个稳定、公开、完整 run interface，而无需理解实现内部的 model loop、tool loop 或会话结构。仅有模型接口不算。

| 项目 | canonical state / message / event 归属 | loop / tools / checkpoint / handoff 归属 | provider seam | 可替换整个 runtime 的 public interface | 接入 AgentLoom 的正确粒度 |
| --- | --- | --- | --- | --- | --- |
| OpenAI Agents SDK | SDK 自有 Responses-style items、run items、session items | `Runner` 和内部 run implementation；工具、handoff、guardrail、session 都在 Runner | `Model.get_response/stream_response` | 没有独立于 Runner 语义的 runtime plugin；`Runner.run` 可被外层包装 | 完整 run |
| Pydantic AI | `ModelRequest/ModelResponse` 及 Text/Thinking/ToolCall/ToolReturn parts | Agent graph / nodes；工具执行、usage、message history 和 durable execution 由框架拥有 | `Model` 及 provider model 实现 | 没有通用第三方 runtime interface；`Agent.run` 可被外层包装 | 完整 run |
| LangGraph | 用户定义 graph state，常用 `BaseMessage/AIMessage/ToolMessage` | Pregel graph runtime、ToolNode、checkpointer、Command / handoff | `BaseChatModel` | 编译后的 graph 是 Runnable，但这是 LangGraph runtime interface，不是跨框架 Agent 标准 | 完整 graph run |
| AutoGen | `LLMMessage`、AgentChat message/events 和 runtime message | AgentChat team / core runtime；工具 loop 和 handoff 由 agent/team 实现 | `ChatCompletionClient` | Core Agent runtime 有 agent/message interface，但 AgentChat 的语义仍需完整包装 | 完整 run / team run |
| smolagents | `ChatMessage`、`ActionStep`、`AgentMemory` | `MultiStepAgent/ToolCallingAgent`；工具执行、managed agents、final answer 都在其中 | `Model.generate -> ChatMessage` | 只有具体 Agent 的 `run`，没有 runtime-neutral protocol | 完整 run |
| Google ADK | `google.genai.Content` 加 ADK `Event`；`Session.events` 是持久历史 | `BaseAgent`、`BaseLlmFlow`、`Runner`；工具、resume、transfer 都在 ADK flow | `BaseLlm.generate_content_async` | `BaseAgent.run_async` / BaseNode 是可包装的完整执行 interface，但不是可注入的“runtime implementation” | 完整 run |
| AWS Strands Agents | Strands `Message/ContentBlock`、typed events、Agent state | `event_loop_cycle`、ToolExecutor、SessionManager、multiagent Graph/Swarm | `Model.stream` | **有**：公开 `AgentBase` Protocol 定义 invoke / stream | 完整 run |
| Microsoft Semantic Kernel | `ChatMessageContent` + item union、`AgentThread` | 各 Agent implementation；ChatCompletion 与 OpenAI Responses 有不同 loop/thread actions；orchestration 管 handoff | chat service / model connector | **有**：抽象 `Agent.invoke/invoke_stream`；另有 experimental runtime Agent protocol | 完整 run |
| Agno | Agno `Message`、`ModelResponse`、`RunOutput/RunEvent`、`AgentSession` | Agent run 管外层生命周期；`Model.response` 甚至包含多轮工具 loop | `Model.invoke/ainvoke` | **有**：`AgentProtocol.arun` 明确支持 native、remote、external adapters | 完整 run |
| Letta | 当前产品由 Letta Code runtime / App Server 持有 agent identity、memory 和 conversation state | runtime / server 持有 loop、tools 和 persistence | runtime 内部 model integration | Agent SDK / App Server 是远端完整-run interface，不是 Python runtime plugin | 完整远端 run |
| LlamaIndex AgentWorkflow | LlamaIndex `ChatMessage/ContentBlock`、workflow Context/Store、AgentStream/ToolCallResult events | AgentWorkflow steps / Workflow runtime；工具、handoff、state store 在 workflow | `LLM` / function-calling LLM | Workflow `run` 是可包装的完整执行 interface，但不是跨框架 runtime 标准 | 完整 workflow run |

共同规律不是“所有框架都有一个统一 Agent runtime 标准”，而是：

1. 每个框架都有自己的 canonical state。
2. model/provider adapter 是单轮 seam。
3. Agent loop、tool execution、checkpoint、handoff 往往共同构成一个不可拆散的 runtime module。
4. 少数项目在更外层提供小型 Agent interface（Strands `AgentBase`、Semantic Kernel `Agent.invoke`、Agno `AgentProtocol`），用它们容纳 native / remote / external Agent；这正是 AgentLoom 应借鉴的层级。

## 项目证据

### OpenAI Agents SDK

- `Model` interface 接收 Responses input items并返回 `ModelResponse`；这是 model-turn seam，不是 runtime seam。[官方源码](https://github.com/openai/openai-agents-python/blob/58a6d2c932810fcf9cb7a1a5a68e666ff04553ed/src/agents/models/interface.py)
- run items 能转换回下一轮 input items，SDK 自己拥有 item replay 语义。[官方源码](https://github.com/openai/openai-agents-python/blob/58a6d2c932810fcf9cb7a1a5a68e666ff04553ed/src/agents/items.py)
- `Runner` 拥有完整 run loop、handoffs、tools、guardrails 和 session 行为。[官方源码](https://github.com/openai/openai-agents-python/blob/58a6d2c932810fcf9cb7a1a5a68e666ff04553ed/src/agents/run.py)

结论：如果把 OpenAI Agents SDK 当基座，AgentLoom adapter 应包 `Runner.run`，而不是尝试逐步接管它的 model turn。

### Pydantic AI

- framework-owned messages 把 text、thinking、tool call、tool return 建模为 parts。[官方源码](https://github.com/pydantic/pydantic-ai/blob/main/pydantic_ai_slim/pydantic_ai/messages.py)
- OpenAI Responses 与 Anthropic 是不同 `Model` implementations，投影到同一套 message parts。[OpenAI model](https://github.com/pydantic/pydantic-ai/blob/main/pydantic_ai_slim/pydantic_ai/models/openai.py)；[Anthropic model](https://github.com/pydantic/pydantic-ai/blob/main/pydantic_ai_slim/pydantic_ai/models/anthropic.py)
- Agent 的 graph / nodes 拥有调用和工具循环。[官方源码](https://github.com/pydantic/pydantic-ai/tree/main/pydantic_ai_slim/pydantic_ai/_agent_graph.py)

结论：它是 canonical item 设计的强参考，但切换到 Pydantic AI 仍然是完整 Agent run adapter，不是更换一次模型调用。

### LangGraph

- graph state 和 checkpoint 由 LangGraph runtime 持有。[官方源码](https://github.com/langchain-ai/langgraph/tree/main/libs/langgraph/langgraph/pregel)
- prebuilt ReAct agent 的状态是 messages；它检查 tool call / tool result 配对并决定是否继续。[官方源码](https://github.com/langchain-ai/langgraph/blob/main/libs/prebuilt/langgraph/prebuilt/chat_agent_executor.py)
- provider 差异在 LangChain chat model adapter；OpenAI Responses 仍投影为 LangChain messages/content blocks。[官方源码](https://github.com/langchain-ai/langchain/blob/master/libs/partners/openai/langchain_openai/chat_models/base.py)

结论：编译 graph 可作为 AgentLoom runtime adapter 的实现，但 AgentLoom 不应试图把 LangGraph 降成一个 model-step adapter。

### AutoGen

- Core `ChatCompletionClient` 是 provider seam，输入输出是 AutoGen 自有 LLM messages。[官方源码](https://github.com/microsoft/autogen/blob/main/python/packages/autogen-core/src/autogen_core/models/_model_client.py)
- tool caller loop 只消费统一的 FunctionCall / FunctionExecutionResult。[官方源码](https://github.com/microsoft/autogen/blob/main/python/packages/autogen-core/src/autogen_core/tool_agent/_caller_loop.py)
- AgentChat 另外拥有 agent/team runtime 和事件模型。[官方源码](https://github.com/microsoft/autogen/tree/main/python/packages/autogen-agentchat/src/autogen_agentchat)

结论：model client 和 Agent runtime 是两层。接 AgentLoom 时应包装一次 agent/team run。

### smolagents

- `Model.generate()` 返回 `ChatMessage`，不是 Responses item stream。[官方源码](https://github.com/huggingface/smolagents/blob/v1.26.0/src/smolagents/models.py)
- `ToolCallingAgent` / `MultiStepAgent` 拥有 loop、memory、tool execution、final answer 和 managed agents。[官方源码](https://github.com/huggingface/smolagents/blob/v1.26.0/src/smolagents/agents.py)
- `ActionStep.to_messages()` 把 observation 投影回 chat messages。[官方源码](https://github.com/huggingface/smolagents/blob/v1.26.0/src/smolagents/memory.py)

结论：smolagents 作为现有第一 adapter 仍有价值；但它不是 Responses-native，也没有提供可替换整个 runtime 的 interface。AgentLoom 目前通过继承、monkey patch 和 `ActionStep` 扩展深度耦合它，直接把这套 implementation 当核心会降低 locality。

### Google ADK

检查 revision：[`f33d492`](https://github.com/google/adk-python/tree/f33d4923388a963d0c5cbf8f7855a88d255b0dea)。

- `BaseLlm.generate_content_async(LlmRequest) -> AsyncGenerator[LlmResponse]` 是单个 model turn 的 seam。[源码](https://github.com/google/adk-python/blob/f33d4923388a963d0c5cbf8f7855a88d255b0dea/src/google/adk/models/base_llm.py#L36-L151)
- `BaseAgent.run_async` 是完整 Agent invocation interface；生命周期在 `_run_with_lifecycle` 内。[源码](https://github.com/google/adk-python/blob/f33d4923388a963d0c5cbf8f7855a88d255b0dea/src/google/adk/agents/base_agent.py#L312-L438)
- `BaseLlmFlow.run_async` 循环执行 one-step，并在 flow 内处理 function calls、resume 和 transfer。[源码](https://github.com/google/adk-python/blob/f33d4923388a963d0c5cbf8f7855a88d255b0dea/src/google/adk/flows/llm_flows/base_llm_flow.py#L317-L505)
- ADK `Event` 继承 `LlmResponse`，`Session.events` 保存 user/model/tool 有序历史。[Event](https://github.com/google/adk-python/blob/f33d4923388a963d0c5cbf8f7855a88d255b0dea/src/google/adk/events/event.py#L86-L150)；[Session](https://github.com/google/adk-python/blob/f33d4923388a963d0c5cbf8f7855a88d255b0dea/src/google/adk/sessions/session.py#L28-L63)
- `Runner` 接收 root Agent / Node，并持有 session、artifact、memory、credential 和 plugin services。[源码](https://github.com/google/adk-python/blob/f33d4923388a963d0c5cbf8f7855a88d255b0dea/src/google/adk/runners.py#L192-L320)

结论：ADK 有完整 Agent interface，但 loop 仍属于 ADK flow。适配 AgentLoom 应包 `BaseAgent.run_async` / Runner，而非适配 `BaseLlm` 后声称换了基座。

### AWS Strands Agents

检查 revision：[`2706860`](https://github.com/strands-agents/sdk-python/tree/270686077c97a5f9c8647ad6f9bc35246392e822)。

- `AgentBase` 是很小的 public Protocol：同步 invoke、异步 invoke、异步 stream。[源码](https://github.com/strands-agents/sdk-python/blob/270686077c97a5f9c8647ad6f9bc35246392e822/strands-py/src/strands/agent/base.py#L13-L67)
- `Model.stream` 是独立的 provider seam，消费 Strands `Messages` 和 tool specs。[源码](https://github.com/strands-agents/sdk-python/blob/270686077c97a5f9c8647ad6f9bc35246392e822/strands-py/src/strands/models/model.py#L186-L295)
- Strands 自己拥有 `Message/ContentBlock`，消息有稳定 tracking id，session persistence 持久化这些消息。[Message](https://github.com/strands-agents/sdk-python/blob/270686077c97a5f9c8647ad6f9bc35246392e822/strands-py/src/strands/types/content.py#L235-L285)；[SessionManager](https://github.com/strands-agents/sdk-python/blob/270686077c97a5f9c8647ad6f9bc35246392e822/strands-py/src/strands/session/session_manager.py#L32-L104)
- `event_loop_cycle` 明确拥有 model call、tool execution、recursive turns、limits、interrupt 和 checkpoint position。[源码](https://github.com/strands-agents/sdk-python/blob/270686077c97a5f9c8647ad6f9bc35246392e822/strands-py/src/strands/event_loop/event_loop.py#L189-L410)
- `OpenAIResponsesModel` 是这个相同 loop 下的 provider adapter。[源码](https://github.com/strands-agents/sdk-python/blob/270686077c97a5f9c8647ad6f9bc35246392e822/strands-py/src/strands/models/openai_responses.py)

重要限制：Strands 的 Responses adapter 当前会警告 multi-turn reasoning content 尚未完整支持，并将输出投影回 Strands messages；它适合验证 runtime seam，不应被当成 Responses 无损 replay 的金标准。

结论：这是最适合作为 AgentLoom 第二个真实 runtime adapter 的项目。原因不是功能最多，而是它同时给出小型完整-run `AgentBase` interface、独立 model seam、明确 event loop、ToolExecutor 和 SessionManager，能直接验证两层 seam 是否分清。

### Microsoft Semantic Kernel

检查 revision：[`ca40aa7`](https://github.com/microsoft/semantic-kernel/tree/ca40aa7226531d28a721d0ca0e451d0aaf86dafc)。

- 抽象 `Agent` 暴露 `get_response`、`invoke`、`invoke_stream`，返回统一 `AgentResponseItem`。[源码](https://github.com/microsoft/semantic-kernel/blob/ca40aa7226531d28a721d0ca0e451d0aaf86dafc/python/semantic_kernel/agents/agent.py#L191-L413)
- canonical message 是 `ChatMessageContent`，内部 items 包括 Text、Reasoning、FunctionCall、FunctionResult 等。[源码](https://github.com/microsoft/semantic-kernel/blob/ca40aa7226531d28a721d0ca0e451d0aaf86dafc/python/semantic_kernel/contents/chat_message_content.py)
- `ChatCompletionAgent` 通过 chat service 执行；OpenAI Responses 使用独立 `OpenAIResponsesAgent` 和 `ResponsesAgentThreadActions`。[Chat Agent](https://github.com/microsoft/semantic-kernel/blob/ca40aa7226531d28a721d0ca0e451d0aaf86dafc/python/semantic_kernel/agents/chat_completion/chat_completion_agent.py#L116-L420)；[Responses loop](https://github.com/microsoft/semantic-kernel/blob/ca40aa7226531d28a721d0ca0e451d0aaf86dafc/python/semantic_kernel/agents/open_ai/responses_agent_thread_actions.py#L77-L235)
- experimental runtime 另有 message-driven Agent Protocol，包含 `on_message/save_state/load_state/close`。[源码](https://github.com/microsoft/semantic-kernel/blob/ca40aa7226531d28a721d0ca0e451d0aaf86dafc/python/semantic_kernel/agents/runtime/core/agent.py)

结论：统一完整-run interface 可遮蔽不同 agent implementation，但内部状态和 tool loop 仍可不同。AgentLoom 应复制这个思想，而不是要求所有 runtime 先转换成同一个 smolagents step。

### Agno

检查 revision：[`cc64676`](https://github.com/agno-agi/agno/tree/cc6467641248e3e76ce42e1740e31d16814ea407)。

- `AgentProtocol.arun` 的注释明确说 native Agent、RemoteAgent、external framework adapter 都满足同一最小 interface。[源码](https://github.com/agno-agi/agno/blob/cc6467641248e3e76ce42e1740e31d16814ea407/libs/agno/agno/agent/protocol.py#L7-L35)
- provider `Model` 有 `invoke/ainvoke` seam，但其高层 `response/aresponse` 本身还拥有工具多轮 loop。[源码](https://github.com/agno-agi/agno/blob/cc6467641248e3e76ce42e1740e31d16814ea407/libs/agno/agno/models/base.py#L133-L185)
- Agent run 负责 session、hooks、memory、reasoning、model execution、persistence 和 output 生命周期。[源码](https://github.com/agno-agi/agno/blob/cc6467641248e3e76ce42e1740e31d16814ea407/libs/agno/agno/agent/_run.py#L367-L620)
- OpenAI Responses 是 `Model` adapter，转成 Agno `Message/ModelResponse`；response id 和 reasoning provider payload 放在 provider data。[源码](https://github.com/agno-agi/agno/blob/cc6467641248e3e76ce42e1740e31d16814ea407/libs/agno/agno/models/openai/responses.py#L1188-L1270)

结论：Agno 是“两层 adapter”最直接的一手样本：`Model` 解决 provider，`AgentProtocol` 解决外部 Agent runtime。

### Letta

检查 revision：[`5bcdd17`](https://github.com/letta-ai/letta/tree/5bcdd177d70fa2b31a754cfcd801e77b2e1ab16a)。

当前 `letta-ai/letta` 主分支已不再包含历史 Python runtime。官方 README 明确说明：当前源码迁到 `letta-ai/letta-code`，历史 Letta V1 Python server 在 `archive` 分支；当前公开入口是 Letta Code、App Server 和 Agent SDK。[迁移说明](https://github.com/letta-ai/letta/blob/5bcdd177d70fa2b31a754cfcd801e77b2e1ab16a/README.md)

这意味着 Letta 对本次调研最可靠的启发是部署形态：它应被视为拥有 agent identity、memory、conversation 和 loop 的远端 runtime。AgentLoom 若接入，应通过 App Server / Agent SDK 包完整 run，而不是把它的模型调用抽出来。历史 Python V1 可用于考古，但不应作为新 seam 的实现依据。

结论：Letta 不适合作为本轮第二个 Python in-process adapter；若未来接入，应做 full remote run adapter，checkpoint 保存 Letta resource identity + AgentLoom envelope。

### LlamaIndex AgentWorkflow

检查 revision：[`c60937d`](https://github.com/run-llama/llama_index/tree/c60937d3099b89e66ff9040f1c45030bc4e407e9)。

- LLM interface 以 LlamaIndex `ChatMessage/ChatResponse/ContentBlock` 为 canonical model state。[官方源码](https://github.com/run-llama/llama_index/tree/c60937d3099b89e66ff9040f1c45030bc4e407e9/llama-index-core/llama_index/core/llms)
- AgentWorkflow 通过 Workflow Context / Store、AgentStream、ToolCall 和 ToolCallResult 事件运行工具和 handoff。[官方源码](https://github.com/run-llama/llama_index/tree/c60937d3099b89e66ff9040f1c45030bc4e407e9/llama-index-core/llama_index/core/agent/workflow)
- OpenAI Responses 是 LLM adapter，输出重新投影为 LlamaIndex blocks。[官方源码](https://github.com/run-llama/llama_index/blob/c60937d3099b89e66ff9040f1c45030bc4e407e9/llama-index-integrations/llms/llama-index-llms-openai/llama_index/llms/openai/responses.py)

结论：若保留 AgentWorkflow 的 workflow state、handoff 和 tool semantics，AgentLoom 必须适配完整 `Workflow.run`。

## 对当前 AgentLoom 与 spec 的审查

### 当前代码没有 runtime seam

当前主路径直接：

- 从 runtime module 导入 smolagents `CodeAgent`、`RunResult`、`Tool` 等类型；
- 构造 `ToolCallingAgentV2` / `CodeAgentV2`；
- 直接调用 runtime agent 的 `.run(...)`；
- 在 smolagents subclass 中覆盖 `_step_stream`、`process_tool_calls` 并写 `ActionStep`；
- 通过 monkey patch 修改 smolagents 行为。

这些都属于 smolagents adapter implementation，却仍泄漏进 AgentLoom runtime owner。删除 smolagents 后，复杂度不会消失，而会散落回 Agent construction、invocation、checkpoint、hooks、tools、logging 和 tests；按 deletion test，这还不是一个深 module。

### 当前 spec 的正确部分

- 删除 `code_act` 并收敛到结构化 tool call。
- 把 `chat/responses/messages` 作为显式协议配置，不从 model 名称推断。
- LiteLLM 只负责 transport/provider mapping。
- 不做协议间静默 fallback。
- tool call 必须有稳定 call id。
- Responses reasoning 不能当普通 assistant text。
- 用真实 Application 验证。

这些决策应保留，但属于“smolagents adapter 的 Responses 支持”或共享 model protocol module，不足以定义可替换 runtime。

### 必须修改的部分

1. 把“smolagents remains the runtime base”改成“smolagents is the first runtime adapter”。
2. 从 Out of Scope 删除“Replacing smolagents as the AgentLoom runtime base”；改为“本阶段不删除 smolagents adapter”。
3. 新增完整-run `AgentRuntime` interface、capabilities、runtime factory 和 adapter selection。
4. model 配置的 `adapter` 不能兼任 runtime 选择。建议分开：

   ```yaml
   runtime: smolagents
   model:
     adapter: openai_responses
     model: gpt-5
   ```

5. 把“AgentLoom items 是所有 runtime 唯一 replay source”改为两类状态：
   - runtime-neutral audit events：AgentLoom owned；
   - resumable runtime state：adapter owned，以 versioned opaque payload 保存。
6. checkpoint envelope 至少记录 `runtime_id`、`runtime_version`、`state_schema_version` 和 `payload`。第一版只允许原 runtime 恢复。
7. AgentLoom Tool Gateway 继续拥有 Hook Plan、权限、side-effect guard 和 `ToolCallRecord`；每个 runtime adapter 只拿到代理工具，避免换 runtime 绕过治理。
8. handoff / managed agents 是 runtime capability，不要假定所有 runtime 都支持 smolagents managed agent 语义。Application compiler 应根据 capability 明确失败。
9. 测试的最高 seam 改成同一 Application contract 在两个 runtime adapters 下运行，而不是只测 item -> smolagents ChatMessage projection。

## 推荐的 runtime interface

不要把第三方 message class 放进 interface。保持一个深 module：

```text
AgentRuntime
  capabilities
  run(request, context) -> async RuntimeEvent stream + RuntimeResult
  resume(checkpoint, context) -> async RuntimeEvent stream + RuntimeResult
```

interface 的重要契约：

- request：Agent 定义、输入、runtime-neutral tool descriptors、模型 profile、run limits；
- context：Run identity、Hook Run、workspace、cancellation、checkpoint sink；
- events：run/model/tool/handoff/usage/checkpoint/terminal 的最小规范化事件；
- result：terminal status、final output、usage、artifacts、runtime checkpoint；
- errors：configuration、unsupported capability、provider、tool、interrupted、budget-limited；
- ordering：tool side effect 必须穿过 AgentLoom Tool Gateway；
- resume：同 runtime、同 state schema；不承诺跨 runtime。

不要在第一版 interface 暴露 `step()`。一旦 Host 驱动 step，Host 就必须理解各 runtime 的 loop cursor、reasoning items、parallel tool groups、HITL 和 handoff，interface 会变成所有框架能力的并集，是一个浅 module。

若未来确有 AgentLoom-native runtime，再单独定义内部 `ModelTurnAdapter`：

```text
ModelTurnAdapter.generate(items, tools, settings) -> ModelTurn
```

它与 `AgentRuntime` 是嵌套关系，不是同一个 adapter。

## 推荐第二基座

推荐 **AWS Strands Agents**，用于第二个真实 runtime adapter。

原因：

- 有明确且很小的 `AgentBase` public Protocol，正好对应完整-run seam。
- model provider、event loop、tool executor、session manager 的 ownership 清楚。
- 已有 OpenAI Responses adapter，同时也支持其他 providers。
- 支持 tool use、并发执行、interrupt、checkpoint、multi-agent Graph/Swarm，足以暴露 AgentLoom seam 中过度依赖 smolagents 的地方。
- 和 smolagents 的实现差异足够大；如果同一 interface 能承载这两个 adapter，seam 才是真实的。

不把它当 Responses 保真金标准。Responses item-native 设计继续参考 Pydantic AI 和 OpenAI Agents SDK。

## 最小迁移顺序

### 1. 先改 spec，不改行为

先把两个 seam、ownership、checkpoint 规则和 capability contract 写清。否则“Responses item 唯一状态”会继续把实现锁死在自有 loop 与 smolagents loop 的夹层。

### 2. 提取完整-run interface

在 Application / invocation 与 smolagents implementation 之间放 `AgentRuntime` seam。先让现有调用只依赖 runtime-neutral request/event/result。不要同时重写 tool loop。

### 3. 做 smolagents adapter 的机械迁移

把现有 `ToolCallingAgentV2` construction、`.run()`、`ActionStep` projection、model construction 和相关 monkey patches全部收进 smolagents adapter。行为保持不变，先通过现有 Application tests。

### 4. 固化 AgentLoom-owned 治理

所有 runtime 的 tools 都由 AgentLoom Tool Gateway 提供，继续统一 Hook、权限、workspace、ToolCallRecord 和 side-effect evidence。checkpoint 使用 runtime envelope；normalized events 只作为审计，不冒充可恢复的完整内部状态。

### 5. 在 smolagents adapter 内完成 code_act 删除

这一步不影响外层 runtime interface：只剩 tool-calling implementation，旧字段不再进入有效配置。

### 6. 在 smolagents adapter 内接 Responses

可继续用 LiteLLM 和必要的 message/item projection，但必须如实标注能力：

- native protocol 可用；
- reasoning / provider item replay 是否 lossless；
- built-in tool families 是否支持；
- checkpoint 是否包含 runtime-specific provider payload。

不要把“可调用 Responses endpoint”写成“整个 runtime 已 item-native”。

### 7. 实现 Strands adapter 的最小纵切

先支持一个 Worker / 单 Agent Application：

- 同一个 AgentLoom tool；
- 同一个 model profile；
- tool call -> Tool Gateway -> result；
- normalized events；
- terminal result；
- same-runtime checkpoint / resume（若第一纵切范围允许）。

第二个 adapter 实跑后再冻结 interface。一个 adapter 只能证明假想 seam，两个 adapter 才能证明真实 seam。

### 8. 扩到 Supervisor / Worker topology

把 Application topology 编译成 runtime-native managed agents / Graph / agent-as-tool。先声明 capabilities，不支持的 handoff 模式直接失败；不做静默降级。

## 最终判断

smolagents 不是“不好”，而是当前用法把一个具体 implementation 当成了 AgentLoom 的 interface。它适合继续承载现有行为、作为第一 runtime adapter；不适合继续作为所有 AgentLoom 状态和扩展的不可替换 superclass。

当前 Responses spec 若原样实施，会得到“支持三种模型协议的 smolagents runtime”，不会得到“可以切换不同 Agent 基座的 AgentLoom”。如果后者是确定目标，应先按本文重写 spec，再开始代码；否则现在做的 item-native replay 和 smolagents compatibility projection 很可能在第二个 runtime 接入时被推翻。
