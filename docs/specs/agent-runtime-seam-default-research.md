# Agent runtime seam 默认设计研究

日期：2026-09-18

范围：只读研究。本文基于 OpenAI Agents SDK、Pydantic AI、LangGraph、AutoGen、LlamaIndex 和 smolagents 的一手源码，判断 AgentLoom 是否应该通过一个 adapter seam 切换 Agent runtime 基座，以及这个 seam 应该长什么样。不修改运行时代码，也不修改现有 spec。

## 结论

当前 AgentLoom spec 还没有真正支持切换 Agent runtime 基座。

现有架构已经把“AgentLoom 自有 runtime 责任”和“smolagents 适配代码”拆到了不同目录，但这只是代码归属 seam，不是可插拔 runtime seam。证据是现有 spec 明确写着“本次只存在一个实际 Agent 引擎 adapter，不为假想的第二框架增加通用 Agent 引擎 interface”（`docs/specs/architecture-and-application-definition.md:97`）。实现上也一样：`BaseAgent.build_runtime_agent()` 直接返回 smolagents `CodeAgent` / `ToolCallingAgentV2`；`AgentInvocation` 直接调用 `runtime_agent.run(return_full_result=True, reset=False)`；checkpoint coordinator 直接读写 `runtime_agent.memory.steps`，并把 smolagents `ActionStep` 注册为 step callback 类型。源码位置：`src/runtime/agent.py:788-994`、`src/runtime/invocation.py:264-371`、`src/runtime/checkpoint/coordinator.py:238-342`。

要支持切换基座，需要新增一个 AgentLoom-owned `AgentRuntimeAdapter` seam，并和 `ModelProtocolAdapter` 分开。

- `ModelProtocolAdapter` 只负责一次模型请求：把 AgentLoom canonical items 投影到 `openai_responses`、`openai_chat`、`anthropic_messages` 等 provider wire contract，再把模型返回投影回 AgentLoom items。
- `AgentRuntimeAdapter` 负责完整 agent loop：任务输入、消息/状态历史、工具执行、final answer、handoff/subagent、事件、checkpoint/resume、错误状态。它可以用 smolagents、Pydantic AI、OpenAI Agents SDK 或 LangGraph 做底层基座，但不能把底层对象暴露给默认调用者。

默认调用者体验应该保持简单：`loom run workflow.yaml "task"` 和 `run_app(...)` 不变。高级用户最多在 Agent 配置里写一个可选字段，例如：

```yaml
agent_runtime: smolagents  # 默认值；未来可选 pydantic_ai
```

模型协议仍然放在模型配置里：

```yaml
model:
  powerful:
    adapter: litellm
    protocol: openai_responses  # openai_responses | openai_chat | anthropic_messages
    model: openai/gpt-5_5
```

这两个 seam 不能合并。把 `protocol` 写成 runtime 选项，会让模型 wire contract 泄漏到 agent loop；把 `agent_runtime` 写进模型配置，会让 provider adapter 背上工具执行、checkpoint、handoff 这些不属于它的职责。

## 现有 AgentLoom 的真实耦合点

当前代码有一个清晰的事实：AgentLoom 的上层调用者还没有面对抽象 runtime，而是在调用一个被包装过的 smolagents runtime。

现有执行链路：

- `YamlConfiguredSupervisorAgent` 创建 supervisor，随后 `supervisor.run(...)` 被应用 runner 调用。源码位置：`src/application/runner.py:538-558`。
- `RoleDrivenAgent.run()` 进入 `AgentInvocation`，绑定 task id、run id、Hook Run、Goal、Todo、checkpoint。源码位置：`src/runtime/agent.py:1012-1055`、`src/runtime/invocation.py:86-194`。
- `AgentInvocation._execute_bound()` 调 `owner.build_runtime_agent()`，再调 `_run_runtime()`。源码位置：`src/runtime/invocation.py:264-277`。
- `_run_runtime()` 直接调用 `runtime_agent.run(task=..., return_full_result=True, reset=False)`，并要求返回对象有 smolagents `RunResult.state` / `output` 语义。源码位置：`src/runtime/invocation.py:344-415`。
- checkpoint restore/save 直接注入或读取 `runtime_agent.memory.steps`，step callback 注册的是 smolagents `ActionStep`。源码位置：`src/runtime/checkpoint/coordinator.py:238-342`。
- Worker 复用和恢复也依赖 smolagents：`SubTaskTrackedAgent` 快照 `_agent.memory.steps`，恢复时设置 `reset=False`，完成后把 `RunResult.output` 存成 worker 结果。源码位置：`src/runtime/agent.py:1094-1239`。

所以，当前状态可以总结为：

- 已经有“smolagents 适配目录”；
- 已经有部分 AgentLoom-owned runtime 责任；
- 还没有“同一 AgentLoom 调用者可替换 smolagents / Pydantic AI / OpenAI Agents SDK / LangGraph”的 runtime adapter interface。

这个状态可以接受。按照 deep module 的判断，一条 seam 要等到至少有两个真实 adapter 才算成立。现在如果要引入第二基座，必须先定义这个 seam，否则第二基座会把自己的 `RunResult`、`AgentRunResult`、`WorkflowHandler`、`TaskResult`、checkpoint 状态一路泄漏到 `src/runtime/invocation.py` 和应用层。

## 开源框架给出的分层答案

### OpenAI Agents SDK

OpenAI Agents SDK 的 runtime seam 是 `Runner`，model seam 是 `Model`。

关键源码事实：

- `Runner.run()` 的输入是 `Agent`、`str | list[TResponseInputItem] | RunState`，输出是 `RunResult`；loop 语义写在 docstring 里：调用 agent、判断 final output、处理 handoff、执行 tool calls、继续下一轮。源码位置：`openai-agents 0.22.3: agents/run.py:259-327`。
- `Runner.run_sync()` 只是同步包装，同样输出 `RunResult`。源码位置：`agents/run.py:365-430`。
- `RunResultBase` 暴露 `input`、`new_items`、`raw_responses`、`final_output`、guardrail results、`context_wrapper`。它还能 `to_input_list()`，把 run 结果转换成下一轮输入。源码位置：`agents/result.py:312-482`。
- streaming result 有 `stream_events()`，事件类型分成 raw response event、run item event、agent updated event。源码位置：`agents/result.py:599-760`、`agents/stream_events.py:10-61`。
- lifecycle hooks 独立于 provider，覆盖 LLM start/end、agent start/end、handoff、tool start/end。源码位置：`agents/lifecycle.py:13-206`。
- `Model` 是单独 seam，输入/输出是 Responses-style items 和 `ModelResponse`，不是 Runner。源码位置：`agents/models/interface.py:67-135`。

判断：OpenAI Agents SDK 可以作为 item-first runtime 的强参考。但它的 Runner 把 handoff、session persistence、OpenAI conversation state、sandbox、guardrail 都纳入自己的语义，不能直接当“所有 runtime 的共同内部 step API”。AgentLoom 如果接入它，应该在外面包 `OpenAIAgentsRuntimeAdapter`，输出 AgentLoom 的 `LoomRunResult` 和 `LoomRuntimeEvent`。

### Pydantic AI

Pydantic AI 把 agent loop 显式建成 graph，model seam 独立成 `Model.request()` / `request_stream()`。

关键源码事实：

- `pydantic_ai.models` 模块开头写明目标：给不同 LLM 做 common interface，让其余代码不用关心具体 LLM。源码位置：`pydantic_ai/models/__init__.py:1-5`。
- `Model.request()` 输入 `list[ModelMessage]`、`ModelSettings`、`ModelRequestParameters`，输出 `ModelResponse`；`request_stream()` 输出 `StreamedResponse`。源码位置：`pydantic_ai/models/__init__.py:528-578`。
- `GraphAgentState` 保存 `message_history`、usage、run step、run id、conversation id、pending messages、event stream buffer 等 runtime 状态。源码位置：`pydantic_ai/_agent_graph.py:315-352`。
- `GraphAgentDeps` 保存当前 model、model selector、tool manager、capabilities、usage limits、tracer、cancellation 等 runtime 依赖。源码位置：`pydantic_ai/_agent_graph.py:401-502`。
- Agent 的 `iter()` 构造内部 agent graph，返回可遍历节点的 `AgentRun`；文档里直接列出节点序列：`UserPromptNode`、`ModelRequestNode`、`CallToolsNode`、`End`。源码位置：`pydantic_ai/agent/__init__.py:1273-1360`。
- `Agent.iter()` 接收 `message_history`、`conversation_id`、`run_id`、model override、usage limits、cancellation token、toolsets、capabilities 等。源码位置：`pydantic_ai/agent/__init__.py:1362-1400`。
- `ModelRequestNode` 把 `ModelRequest` 追加进 `state.message_history`，调用 model，得到 `ModelResponse` 后再追加回 history。源码位置：`pydantic_ai/_agent_graph.py:1206-1321`、`_agent_graph.py:1580-1671`、`_agent_graph.py:1927-1976`。
- `CallToolsNode` 从 `ModelResponse.parts` 中抽 `ToolCallPart`，调用 `process_tool_calls()`，并把结果包装进下一轮 `ModelRequest`。源码位置：`pydantic_ai/_agent_graph.py:1997-2355`。
- run result 能返回 `all_messages()`、`new_messages()`、usage，适合接入 checkpoint/replay。源码位置：`pydantic_ai/run.py:712-902`、`pydantic_ai/result.py:596-658`。

判断：Pydantic AI 是第二基座的最佳可行样本。它的内部 part 模型和 AgentLoom 想要的 item 层接近，且 model seam 和 runtime seam 分得清楚。代价是要把 AgentLoom 工具、Hook、checkpoint 语义包进 Pydantic AI tool/capability，而不是直接使用它的默认工具执行结果作为 AgentLoom 真相。

### LangGraph

LangGraph 的 runtime seam 是 Pregel / StateGraph / checkpointer，不是模型协议 seam。

关键源码事实：

- prebuilt ReAct agent 的 state 是 `messages: Sequence[BaseMessage]`。源码位置：`langgraph-prebuilt 1.1.0: langgraph/prebuilt/chat_agent_executor.py:57-60`。
- 它检查 `AIMessage.tool_calls` 是否有对应 `ToolMessage.tool_call_id`。源码位置：`chat_agent_executor.py:243-271`。
- `call_model()` 调的是 LangChain `BaseChatModel.invoke()` / `ainvoke()`，返回 `AIMessage` 后写回 state。源码位置：`chat_agent_executor.py:660-721`。
- 路由逻辑看最后一个 `AIMessage.tool_calls`，有未完成工具调用就 `Send(\"tools\", ...)`，否则结束或生成结构化响应。源码位置：`chat_agent_executor.py:920-990`。
- Pregel runner 的 stream mode 是图级别的：`values`、`updates`、`custom`、`messages`、`checkpoints`、`tasks`、`debug`。源码位置：`langgraph 1.2.11: langgraph/pregel/main.py:2678-2720`。
- Pregel loop 负责 superstep、stream、checkpointer、durability。源码位置：`langgraph/pregel/main.py:2899-2995`。

判断：LangGraph 很适合参考 checkpoint、interrupt、stream event 和图状态，但它的 runtime seam 太强，直接接入会把 AgentLoom 的 Supervisor/Worker 编排改成 graph 编排。它适合成为后续“graph runtime adapter”，不适合作最小第二基座。

### AutoGen

AutoGen 有两个 seam：`AgentRuntime` 是 agent messaging runtime；`ChatCompletionClient` 是模型 seam。

关键源码事实：

- `AgentRuntime` 管 `send_message()`、`publish_message()`、agent factory、agent instance、runtime state save/load、single-agent state save/load、subscription。源码位置：`autogen-core 0.7.5: autogen_core/_agent_runtime.py:20-280`。
- `SingleThreadedAgentRuntime` 用 asyncio queue 处理 agent message envelope，可注册 agent factory 和实例。源码位置：`autogen_core/_single_threaded_agent_runtime.py:57-280`。
- `ChatCompletionClient` 是模型 seam：`create()` 输入 `Sequence[LLMMessage]`、tools、tool_choice、json_output，输出 `CreateResult`；`create_stream()` 输出字符串 chunk 并以 `CreateResult` 收尾。源码位置：`autogen_core/models/_model_client.py:209-260`。
- `LLMMessage` 是 `SystemMessage | UserMessage | AssistantMessage | FunctionExecutionResultMessage`；工具结果有 `call_id`。源码位置：`autogen_core/models/_types.py:10-82`。
- `tool_agent_caller_loop()` 只看 `ChatCompletionClient.create()` 是否返回 `FunctionCall` 列表；工具执行后追加 `FunctionExecutionResultMessage`。源码位置：`autogen_core/tool_agent/_caller_loop.py:16-80`。
- `AssistantAgent` 的运行说明明确：`run()` 返回 `TaskResult`，`run_stream()` 产出 inner messages，最后产出 `TaskResult`；它自己维护跨调用状态，且非线程/协程安全。源码位置：`autogen_agentchat/agents/_assistant_agent.py:90-120`。
- `AssistantAgent` 的工具行为、handoff 行为、streaming 行为都在 runtime 层，不属于 model adapter。源码位置：`autogen_agentchat/agents/_assistant_agent.py:137-192`。

判断：AutoGen 证明 Agent runtime seam 可以是“消息运行时”，但这个 seam 比 AgentLoom 当前需要更大。它适合多 agent message bus 型运行，不适合直接作为默认替换 seam。若接入，AgentLoom 应该把 `AgentRuntime.send_message()` 包在自己的 `AgentRuntimeAdapter` 后面。

### LlamaIndex

LlamaIndex 的 runtime seam 是 Workflow / AgentWorkflow，model seam 是 LLM `chat()` / `stream_chat()`。

关键源码事实：

- `BaseLLM` 的 chat seam 输入输出是 `ChatMessage` / `ChatResponse`。源码位置：`llama-index-core 0.14.24: llama_index/core/base/llms/base.py:27-296`。
- `ChatMessage` 支持 `TextBlock`、`ImageBlock`、`ThinkingBlock`、`ToolCallBlock` 等 content blocks。源码位置：`llama_index/core/base/llms/types.py:53-260`、`types.py:1125-1190`。
- `BaseWorkflowAgent` 同时继承 `Workflow` 和 `BaseModel`，配置里包含 `tools`、`tool_retriever`、`can_handoff_to`、`llm`、`initial_state`、streaming 和 structured output。源码位置：`llama_index/core/agent/workflow/base_agent.py:87-195`。
- `BaseWorkflowAgent._get_llm_response()` 调 `target_llm.astream_chat()` 或 `target_llm.achat()`，并把流式内容写成 `AgentStream`。源码位置：`base_agent.py:314-345`。
- Agent workflow 的 tool loop 使用 `ToolCall`、`ToolCallResult`、`current_tool_calls`、`ctx.store`。源码位置：`base_agent.py:523-718`。
- `OpenAIResponses` 是 LLM adapter，调 `responses.create()` 后投影回 `ChatResponse` 和 `ToolCallBlock`。源码位置：`llama-index-llms-openai 0.8.1: llama_index/llms/openai/responses.py:147-180`、`responses.py:533-553`、`responses.py:920-960`。

判断：LlamaIndex 可以作为第二基座，但它是 message/block-first，和 smolagents 一样会压扁一部分 Responses 语义。若 AgentLoom 的目标是 Responses 保真，Pydantic AI 比 LlamaIndex 更适合做第二 adapter。

### smolagents

smolagents 的 runtime seam 是 `MultiStepAgent.run()` / `_run_stream()`，model seam 是 `Model.generate()`。

关键源码事实：

- `MultiStepAgent` 初始化参数包括 tools、model、prompt_templates、managed_agents、step_callbacks、planning_interval、final_answer_checks、return_full_result。源码位置：`smolagents 1.26.0: smolagents/agents.py:268-351`。
- `run()` 支持 `task`、`stream`、`reset`、`images`、`additional_args`、`max_steps`、`return_full_result`，最终返回 output 或 `RunResult`。源码位置：`smolagents/agents.py:436-538`。
- `_run_stream()` 是 step loop：planning step、action step、final answer、step callbacks、max steps。源码位置：`smolagents/agents.py:540-625`。
- `write_memory_to_messages()` 把 smolagents memory 投影为 `ChatMessage` 列表。源码位置：`smolagents/agents.py:758-770`。
- `ToolCallingAgent._step_stream()` 依赖 `model.generate()` 返回的 `ChatMessage.tool_calls`。源码位置：`smolagents/agents.py:1260-1365`。

判断：smolagents 可以继续做第一 runtime adapter，因为 AgentLoom 已经围绕它做了大量 Hook、checkpoint、ToolCallRecord 扩展。但如果要切换基座，AgentLoom 不能再让上层代码直接依赖 `memory.steps`、`ActionStep`、`RunResult`。

## 哪些东西能统一

可以统一的是 AgentLoom 自己的外层 run contract：

- 输入：task text、agent definition、run identity、resume identity、additional args、event sink、checkpoint handle。
- 输出：final output、state、usage、run id、task id、events、canonical history delta、checkpoint snapshot。
- 事件：run started/completed/failed/interrupted，model request/response，tool call/result，subagent start/stop，reasoning delta，checkpoint saved。
- checkpoint：由 AgentLoom 保存 canonical history 和 runtime snapshot，不让调用者碰底层 runtime 的私有状态。

tool execution seam 也可以统一：

- 工具输入是 `FunctionCallItem`。
- 工具输出是 `ToolResult`，带 `call_id`、`status`、`model_content`、`artifact/metadata`。
- Hook、权限、审计、错误格式都在 AgentLoom ToolExecutor 里统一。

model protocol seam 也可以统一：

- `ModelProtocolAdapter.request()` 和 `stream()` 只处理模型输入输出。
- 它不知道 Supervisor/Worker、Goal、checkpoint、handoff，也不执行工具。

不能统一的是每个基座的内部 step 模型：

- OpenAI Agents SDK 是 turn + item + handoff resolver。
- Pydantic AI 是 graph node。
- LangGraph 是 Pregel superstep。
- AutoGen 是 message bus。
- LlamaIndex 是 Workflow event。
- smolagents 是 ReAct `ActionStep`。

这些内部模型不能出现在默认调用者 interface 里。否则 `AgentRuntimeAdapter` 会变成浅 wrapper，调用者仍要懂每个基座。

## 推荐 interface

推荐用一个小而深的 `AgentRuntimeAdapter`。它的 interface 尽量少，复杂性留在 adapter implementation 里。

```python
class AgentRuntimeAdapter(Protocol):
    name: str
    capabilities: AgentRuntimeCapabilities

    def build(self, spec: RuntimeAgentSpec, deps: RuntimeDeps) -> RuntimeAgentHandle: ...


class RuntimeAgentHandle(Protocol):
    def run(self, request: AgentRunRequest) -> AgentRunResult: ...
    def close(self) -> None: ...
```

默认不把 stream 做成第二条必学路径。`AgentRunRequest.event_sink` 承接事件；需要 streaming 时，adapter 往 sink 推 `LoomRuntimeEvent`。如果未来要异步调用，再加一个 `AsyncAgentRuntimeAdapter` 或在外层 runner 做 sync/async wrapper，不要让默认 CLI 调用者同时面对两套接口。

最小数据结构：

```python
@dataclass(frozen=True)
class AgentRunRequest:
    task: str
    task_id: str
    run_id: str
    resume: bool = False
    reset: bool = True
    additional_args: Mapping[str, Any] = field(default_factory=dict)
    event_sink: Callable[[LoomRuntimeEvent], None] | None = None
    checkpoint: RuntimeCheckpointHandle | None = None


@dataclass(frozen=True)
class AgentRunResult:
    output: str
    state: Literal["success", "max_steps", "interrupted", "failed", "budget_limited"]
    usage: Usage | None
    items: tuple[LoomItem, ...]
    events: tuple[LoomRuntimeEvent, ...]
    checkpoint_ref: str | None
    raw_result: Any | None = None
```

`RuntimeAgentSpec` 放运行时定义，不放 provider 协议：

```python
@dataclass(frozen=True)
class RuntimeAgentSpec:
    name: str
    description: str
    instructions: str
    role: Literal["supervisor", "worker"]
    max_steps: int
    planning_interval: int | None
    tools: tuple[LoomToolSpec, ...]
    workers: tuple[WorkerSpec, ...]
    final_answer_checks: tuple[FinalAnswerCheck, ...]
    prompt_profile: PromptProfile
```

`RuntimeDeps` 注入 runtime 外部依赖：

```python
@dataclass(frozen=True)
class RuntimeDeps:
    model_adapter: ModelProtocolAdapter
    tool_executor: ToolExecutor
    checkpoint_store: CheckpointStore
    hook_bus: HookBus
    context_engine: ContextEngine | None
    goal_provider: GoalStateProvider | None
    logger: AgentLogger
```

`ModelProtocolAdapter` 单独存在：

```python
class ModelProtocolAdapter(Protocol):
    protocol: Literal["openai_responses", "openai_chat", "anthropic_messages"]

    async def request(
        self,
        input: list[LoomItem],
        tools: list[LoomToolSpec],
        settings: LoomModelSettings,
        *,
        previous_response_id: str | None = None,
    ) -> LoomModelResponse: ...

    def stream(
        self,
        input: list[LoomItem],
        tools: list[LoomToolSpec],
        settings: LoomModelSettings,
        *,
        previous_response_id: str | None = None,
    ) -> AsyncIterator[LoomModelStreamEvent]: ...
```

这个分法的好处是：从 `openai_chat` 切到 `openai_responses`，只换 model adapter；从 smolagents 切到 Pydantic AI，才换 runtime adapter。两类变化互不污染。

## smolagents adapter 的真实可行性

`SmolagentsRuntimeAdapter` 可以先包住现有实现，作为默认 runtime。

实现形状：

- `build()` 调现有 `_create_agent()` 或迁移后的构造逻辑，产出 `ToolCallingAgentV2` / response-aware `ToolCallingAgentV2`。
- `run()` 内部仍调 `runtime_agent.run(task=..., return_full_result=True, reset=...)`。
- step callback 从 smolagents `ActionStep` 转为 `LoomRuntimeEvent`，再交给 checkpoint/event sink。
- checkpoint 暂时可继续反序列化旧 `memory_steps`，但新 checkpoint 必须同时写 AgentLoom canonical items。迁移期可以双写：`memory_steps` 兼容旧 resume，`items` 作为新真相。
- Worker 继续用 managed agent / Agent-as-Tool，但 `SubTaskTrackedAgent` 输出也要转成 canonical `subagent_start`、`subagent_stop`、`function_call_output`。

这个 adapter 能保持默认调用体验不变：

```python
result = runtime_adapter.build(spec, deps).run(
    AgentRunRequest(task=task, task_id=task_id, run_id=run_id, resume=is_resume)
)
```

现有 smolagents 耦合可以逐步藏进 adapter：

- `RunResult.output/state/steps` 转为 `AgentRunResult.output/state/items/events`。
- `runtime_agent.memory.steps` 转为 adapter 内部快照，不再被 `AgentInvocation` 和 `CheckpointCoordinator` 直接读取。
- `ActionStep` callback 转为 `RuntimeStepSnapshot`。
- `reset=False` 转为 AgentLoom 的 `resume` / `continue_same_runtime` 语义，外层不再直接知道 smolagents reset 参数。

风险：如果第一版只包一层、不移动 checkpoint 真相源，仍然不能切第二基座。因为 checkpoint coordinator 还会继续要求 `memory.steps` 和 `ActionStep`。

## Pydantic AI adapter 的真实可行性

`PydanticAIRuntimeAdapter` 是可行的第二基座，原因有三点：它有清晰的 `Model` seam，有 item/part-first 的 message history，有 run result/history/usage 读取接口。

实现形状：

- 把 `RuntimeAgentSpec` 构造成 `pydantic_ai.Agent`：instructions 对应 Agent instructions，tools 通过 AgentLoom tool wrapper 转成 Pydantic AI tools 或 toolset。
- 提供一个 `PydanticAIModelBridge`，实现 Pydantic AI 的 `Model.request()` / `request_stream()`，内部调用 AgentLoom `ModelProtocolAdapter`。这样 model 协议仍由 AgentLoom 管，Pydantic AI 只做 agent loop。
- `message_history` 从 AgentLoom checkpoint 的 canonical items 投影成 Pydantic AI `ModelMessage`；运行结束后用 `result.all_messages()` / `new_messages()` 投影回 canonical items。
- `ToolExecutor` 仍由 AgentLoom 持有。Pydantic AI tool wrapper 只负责把 `ToolCallPart` 转成 AgentLoom `FunctionCallItem`，调用统一 executor，再把 `ToolResult` 转回 `ToolReturnPart`。
- streaming 用 Pydantic AI `run_stream_events()` 或 `Agent.iter()` 的 node/event 输出，投影成 `LoomRuntimeEvent`。
- checkpoint 保存 AgentLoom items 和必要的 Pydantic AI runtime snapshot。不要把 Pydantic AI `GraphAgentState` 直接暴露给应用层。

为什么选 Pydantic AI 做第二基座：

- 它已经把 agent state 和 model protocol 分开。
- 它的 `ModelMessage` / `ModelResponsePart` 能表达 text、thinking、tool call、tool return、native tool return，比 smolagents `ChatMessage` 更接近 AgentLoom item 层。
- 它支持 `message_history` 传入和 `all_messages()` / `new_messages()` 取出，适合做 checkpoint/replay adapter。

仍需注意：Pydantic AI 自己的 ToolManager、capability、durable execution 很强。如果 AgentLoom 直接采用，会和现有 Hook、Goal、ContextEngine、Tool 权限、worker checkpoint 发生职责重叠。第二基座 adapter 必须让 AgentLoom 的 tool executor 和 checkpoint store 继续做真相源。

## OpenAI Agents SDK adapter 的可行性

OpenAI Agents SDK 也可做第二或第三基座，但不建议作为第一条第二基座。

它的优点：

- item/replay 语义最接近 OpenAI Responses。
- `RunResult.new_items`、`raw_responses`、`to_input_list()` 很适合映射 AgentLoom canonical items。
- streaming event 已经分成 raw response event、run item event、agent updated event。

它的成本：

- 当前 AgentLoom 锁定 `openai>=2.8.1,<3.0.0`，而前一份研究看到 `openai-agents==0.22.3` 要求 `openai>=3.0.0,<4`。
- 它的 runtime 语义很完整，包括 guardrails、handoff、session persistence、OpenAI conversation state、sandbox。直接接入容易和 AgentLoom 现有 Hook、checkpoint、worker 语义抢职责。

建议：先学它的 item/replay 形状，不先引依赖。等 AgentLoom item/checkpoint seam 稳定后，再做 `OpenAIAgentsRuntimeAdapter`。

## LangGraph、AutoGen、LlamaIndex 作为 runtime adapter 的位置

这三个都能接，但都不适合作第一条第二基座。

LangGraph 适合图编排和 durable checkpoint：

- 优点是 Pregel、checkpointer、interrupt/resume、stream modes 成熟。
- 成本是它会重塑 Supervisor/Worker 为 graph nodes，和 AgentLoom 当前 YAML/Worker/Hook 模型差异大。

AutoGen 适合 message-bus 多 agent：

- 优点是 `AgentRuntime` 天然支持 `send_message`、`publish_message`、agent state save/load。
- 成本是它把“agent runtime”定义成 actor/message runtime，接入后 AgentLoom 的 Worker-as-tool 语义要被重新映射。

LlamaIndex 适合 workflow agent：

- 优点是 AgentWorkflow 的工具和事件结构清楚。
- 成本是内部仍是 message/block-first，Responses 保真度不如 Pydantic AI。

## 默认调用者体验

默认调用者不要感知 runtime seam。用户仍然运行：

```bash
loom run applications/foo/workflows/bar.yaml "任务"
```

Python 调用也保持：

```python
result = run_app(path, task_override="任务")
print(result.output)
```

高级开关才出现：

```yaml
agent_runtime: smolagents
```

未来支持第二基座时：

```yaml
agent_runtime: pydantic_ai
```

模型协议还是模型配置：

```yaml
model:
  powerful:
    adapter: litellm
    protocol: anthropic_messages
    model: anthropic/claude-sonnet-4-5
```

如果一个 runtime adapter 不支持某能力，启动前 fail fast：

- 不支持 Worker-as-tool；
- 不支持 streaming；
- 不支持 checkpoint resume；
- 不支持 final answer check；
- 不支持 parallel tool calls；
- 不支持 reasoning replay。

不要在运行到一半时退化，也不要 silently fallback 到 smolagents。

## 最小实施顺序

先做 seam，不急着换基座：

- 定义 `AgentRuntimeAdapter`、`RuntimeAgentHandle`、`AgentRunRequest`、`AgentRunResult`、`LoomRuntimeEvent`、`RuntimeStepSnapshot`。
- 把现有 smolagents 路径包成 `SmolagentsRuntimeAdapter`，先保证行为不变。
- 把 `AgentInvocation` 对 `runtime_agent.memory.steps`、`step_callbacks`、`RunResult`、`reset=False` 的直接访问，改成访问 adapter interface。
- checkpoint manager 开始读写 AgentLoom canonical runtime snapshot；迁移期保留旧 `memory_steps` 兼容。
- 做一个 fake runtime adapter 跑单测，证明 seam 不依赖 smolagents。
- 再接 `PydanticAIRuntimeAdapter`，只覆盖最小能力：text final、function call、tool result、usage、new_messages、checkpoint round-trip。

验收标准：

- 同一个 Application，不改 CLI 调用，能在 `agent_runtime: smolagents` 下跑过现有回归。
- 同一个最小 Worker 工具调用 Application，切到 `agent_runtime: pydantic_ai` 后能输出同样的 final answer，并生成同一类 AgentLoom events/checkpoint records。
- checkpoint/resume 不再直接引用 smolagents `ActionStep`；旧 checkpoint 可以恢复，新 checkpoint 以 canonical items 为主。
- `ModelProtocolAdapter` 单测不需要启动 Agent runtime；`AgentRuntimeAdapter` 单测可以用 fake model adapter。

## 最终判断

当前 spec 不允许真正切换 Agent runtime 基座，只是把 smolagents 相关实现放进了 `src/adapters/smolagents/`。如果用户目标是“以后能换 OpenAI Agents SDK / Pydantic AI / LangGraph / AutoGen / LlamaIndex”，AgentLoom 需要新增 runtime seam。

这个 seam 应该粗，不应该细。默认调用者只看到 `run()`、结果、事件和 checkpoint；具体是 `MultiStepAgent`、Pydantic graph、Pregel superstep、AutoGen message runtime，全部留在 adapter implementation 里。

第一条真实第二基座建议选 Pydantic AI。它和 AgentLoom item-first 目标最接近，能用最少胶水验证 seam 不是空接口。OpenAI Agents SDK 可以作为 item/replay 设计参考，等依赖迁移和 AgentLoom checkpoint seam 稳住后再评估接入。
