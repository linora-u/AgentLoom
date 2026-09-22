# 开源 Agent 框架如何适配模型协议

日期：2026-09-18

范围：只读研究。本文只讨论 OpenAI Responses API、OpenAI Chat Completions、Anthropic Messages 这类模型 wire contract 如何接入 Agent 框架，不改 AgentLoom 运行时代码。

结论很直接：Python Agent 框架遇到新模型协议时，主流做法是保留自己的内部 message、item、part 或 event 模型，在 model/provider adapter 边界做投影，而不是重写 Agent loop。AgentLoom 继续保留 smolagents 基座是合理的，但 AgentLoom item 层必须成为真正的 checkpoint/replay 状态源；smolagents 的 `ChatMessage` 只能是兼容视图，不能继续当协议真相。

## 证据来源

本次主要看 PyPI wheel 源码和官方仓库。版本如下：

| 项目 | 检查版本 | 官方源码 |
| --- | --- | --- |
| OpenAI Agents SDK | `openai-agents==0.22.3` | <https://github.com/openai/openai-agents-python> |
| LangChain / LangGraph | `langchain-core==1.6.3`、`langchain-openai==1.6.2`、`langchain-anthropic==1.7.2`、`langgraph==1.2.11`、`langgraph-prebuilt==1.1.0` | <https://github.com/langchain-ai/langchain>、<https://github.com/langchain-ai/langgraph> |
| Pydantic AI | `pydantic-ai-slim==2.45.0` | <https://github.com/pydantic/pydantic-ai> |
| LlamaIndex | `llama-index-core==0.14.24`、`llama-index-llms-openai==0.8.1`、`llama-index-llms-anthropic==0.12.0`、`llama-index-llms-openai-like==0.8.0`、`llama-index-agent-openai==0.4.12` | <https://github.com/run-llama/llama_index> |
| Microsoft AutoGen | `autogen-core==0.7.5`、`autogen-agentchat==0.7.5`、`autogen-ext==0.7.5` | <https://github.com/microsoft/autogen> |
| CrewAI | `crewai==1.15.22` | <https://github.com/crewAIInc/crewAI> |
| smolagents | AgentLoom 锁定 `smolagents==1.26.0` | <https://github.com/huggingface/smolagents> |
| LiteLLM | AgentLoom 锁定 `litellm==1.80.7` | <https://github.com/BerriAI/litellm> |

本地 AgentLoom 当前锁定版本：`.venv` 里是 `smolagents==1.26.0`、`litellm==1.80.7`、`openai==2.8.1`。外层 Python 环境里有另一套 `smolagents==1.25.0`、`litellm==1.83.14`、`openai==2.37.0`，本文的 AgentLoom 判断以 `.venv` 锁定版本为准。

## 先回答设计问题

协议要暴露，但只暴露在模型配置和 adapter 边界。推荐字段含义是 wire contract，值用：

```yaml
model:
  powerful:
    adapter: litellm
    protocol: openai_responses  # openai_responses | openai_chat | anthropic_messages
    model: openai/gpt-5_5
```

如果实现里已经决定字段名叫 `wire_api`，也可以，但值仍然应该是 `openai_responses`、`openai_chat`、`anthropic_messages` 这种协议名。不要叫 `gpt-chat`，因为它把 wire contract 和品牌混在一起；也不要只叫 `chat`，因为后面看配置时不知道是 OpenAI chat completion、Anthropic messages，还是别的 chat-like 协议。

不存在的配置项不要留在 live examples / fixtures 里。历史 spec 可以保留上下文，但示例配置会被用户复制，里面不能再出现已经没有运行时意义的字段。:codex-annotation{index="1"}

新协议接入时不要全量改写 Agent loop。更像开源项目的做法是：

- AgentLoom 有自己的 canonical item/event/checkpoint。
- `openai_responses`、`openai_chat`、`anthropic_messages` 是 model adapter。
- smolagents 保留为执行基座、工具包装、生命周期兼容层。
- LiteLLM 只当 transport/bridge，不能当 AgentLoom 状态模型。

## 框架对照

| 框架 | 内部模型 | 新协议放在哪里 | 是否重写 Agent loop | 对 AgentLoom 的启发 |
| --- | --- | --- | --- | --- |
| OpenAI Agents SDK | Responses-style item/event 原生模型 | `OpenAIResponsesModel`、`OpenAIChatCompletionsModel`、converter | 没有。Runner 只依赖统一 `Model` 接口 | 如果 AgentLoom 要支持 Responses，item 必须能做 replay 源，而不是日志附属品 |
| LangChain / LangGraph | `BaseMessage`、`AIMessage`、`ToolMessage`、`content_blocks`、`tool_calls` | `ChatOpenAI` 的 Responses 路由和 Anthropic adapter | 没有。LangGraph 仍 checkpoint 图状态里的 messages | 老 runtime 可以保留，但 provider 差异要在模型 wrapper 和 message converter 内收敛 |
| Pydantic AI | `ModelRequest`、`ModelResponse`、`TextPart`、`ThinkingPart`、`ToolCallPart`、`ToolReturnPart` | `OpenAIChatModel`、`OpenAIResponsesModel`、`AnthropicModel` | 没有。agent graph 继续跑统一节点 | 最接近 AgentLoom 的目标形态：part-first 状态源加 provider adapter |
| LlamaIndex | `ChatMessage`、`ChatResponse`、`ContentBlock`、`ToolCallBlock` | 独立 LLM adapter，如 `OpenAIResponses`、`Anthropic` | 没有。agent workflow 继续用 `ChatMessage` 和 `ToolCallResult` | message-first 框架也不改 loop，但会牺牲一部分 Responses item 保真度 |
| AutoGen | `LLMMessage` union、`CreateResult`、`FunctionExecutionResultMessage` | `ChatCompletionClient` 的 OpenAI / Anthropic 实现 | 没有。tool caller loop 只看统一 client 返回值 | 工具调用和工具结果必须先归一，再投影到 provider |
| CrewAI | dict messages、`BaseLLM`、`AgentAction` / `AgentFinish` | LLM provider wrapper 或 LiteLLM | 没有。executor 继续分 native tools / ReAct fallback | 如果只停在 LLM wrapper，协议能力会偏浅；不适合作 Responses 保真参考 |
| smolagents | `ChatMessage`、`ChatMessageToolCall`、`ActionStep` | 现有 LiteLLMModel 只走 chat-completion 风格 | 不支持 Responses item loop | 可以保留执行基座，但不能把 `ChatMessage` 当最终状态源 |
| LiteLLM | 不是 Agent runtime | `responses()`、`completion()`、双向转换 | 不涉及 Agent loop | 可复用传输和协议转换，但不要让 fallback 掩盖配置错误 |

## OpenAI Agents SDK 的做法

OpenAI Agents SDK 是 Responses 原生基座。它没有把 Chat Completions、Responses、Anthropic 等协议散落到 Runner 里，而是让 Runner 面向统一的 item/event 接口。

关键证据：

- `agents/items.py` 把 `TResponseInputItem`、`TResponseOutputItem`、`TResponseStreamEvent` 直接定义为 OpenAI SDK Responses 类型别名，并把每个 run item 包在 `RunItemBase` 里；`RunItemBase.to_input_item()` 用 `_output_item_to_input_item()` 把输出项转回下一轮输入项。源码位置：`openai-agents 0.22.3: agents/items.py:76-153`。
- `agents/models/interface.py` 的 `Model.get_response()` 和 `Model.stream_response()` 都接收 `str | list[TResponseInputItem]`，返回 `ModelResponse` 或 `TResponseStreamEvent`，并显式带 `previous_response_id` 和 `conversation_id`。源码位置：`agents/models/interface.py:67-135`。
- `agents/models/openai_chatcompletions.py` 走边界转换：在 `_fetch_response()` 里调用 `Converter.items_to_messages()`，把 Responses items 转成 Chat Completions messages；返回时又用 `Converter.message_to_output_items()` 变回 Responses output items。源码位置：`agents/models/openai_chatcompletions.py:610-768`、`agents/models/chatcmpl_converter.py:123-277`、`agents/models/chatcmpl_converter.py:534-620`。
- 工具执行后构造 `ToolCallOutputItem`，其 raw item 是 `function_call_output`。源码位置：`agents/run_internal/turn_resolution.py:785-930`、`agents/run_internal/items.py:824-849`。
- session persistence 会把 run items 转成 input items，处理 reasoning item id、OpenAI Conversations API 所需 id、去重和不可持久化 reasoning。源码位置：`agents/run_internal/session_persistence.py:592-735`、`agents/run_internal/session_persistence.py:1051-1118`。

判断：这个框架属于 Responses 原生基座，Responses items 本来就是内部模型；Chat Completions 是边界投影。

## LangChain / LangGraph 的做法

LangChain 是 message-first。它没有因为 OpenAI Responses 出现就重写 Runnable 或 LangGraph loop，而是扩展 `AIMessage` / `content_blocks`，再在 provider wrapper 里转换协议。

关键证据：

- `BaseMessage` 是 chat model 的输入输出基类，保留 `content`、`additional_kwargs`、`response_metadata`、`id` 等字段；`additional_kwargs` 专门承接 provider 额外 payload，例如工具调用。源码位置：`langchain-core 1.6.3: langchain_core/messages/base.py:93-180`。
- `AIMessage` 标准化了 `tool_calls`、`invalid_tool_calls` 和 `usage_metadata`，并通过 `content_blocks` 支持标准化内容块。源码位置：`langchain_core/messages/ai.py:160-260`。
- `ToolMessage` 用 `tool_call_id` 关联工具调用和工具结果。源码位置：`langchain_core/messages/tool.py:26-171`。
- `BaseChatModel` 对外仍是 `invoke()` 返回 `AIMessage`，stream 也是 `BaseMessageChunk`；`output_version` 允许把新输出格式渐进存进 `AIMessage.content`。源码位置：`langchain_core/language_models/chat_models.py:284-376`、`langchain_core/language_models/chat_models.py:474-520`。
- `ChatOpenAI` 暴露 `use_responses_api`、`use_previous_response_id`、`output_version="responses/v1"`、`reasoning`、`include`、`truncation` 等 Responses 相关配置，但 `_get_request_payload()` 决定路由：走 Responses 时构造 Responses payload，不走时构造 Chat Completions messages。源码位置：`langchain-openai 1.6.2: langchain_openai/chat_models/base.py:1128-1232`、`base.py:1924-1965`。
- `_construct_responses_api_input()` 把 `AIMessage` / `ToolMessage` 投影成 Responses input items，其中 `ToolMessage` 变成 `function_call_output`；`_construct_lc_result_from_responses_api()` 把 Responses output 重新变成 `AIMessage` 的 content blocks、tool calls、reasoning blocks。源码位置：`langchain_openai/chat_models/base.py:4773-4865`、`base.py:5015-5160`。
- LangGraph 的 prebuilt ReAct agent state 还是 `messages: Sequence[BaseMessage]`。它在 `_validate_chat_history()` 里检查每个 `AIMessage.tool_calls` 都有对应 `ToolMessage`，`call_model()` 只调用 `BaseChatModel.invoke()`，`should_continue()` 看最后一个 `AIMessage.tool_calls`。源码位置：`langgraph-prebuilt 1.1.0: langgraph/prebuilt/chat_agent_executor.py:57-60`、`chat_agent_executor.py:243-271`、`chat_agent_executor.py:660-721`、`chat_agent_executor.py:920-990`。
- LangGraph checkpoint 是图状态 checkpoint，不是 provider 协议 checkpoint；stream mode 有 `messages`、`checkpoints`、`tasks`、`debug`。源码位置：`langgraph 1.2.11: langgraph/pregel/main.py:2678-2720`、`main.py:2899-2995`。

判断：LangChain 把 Responses 暴露给模型 wrapper 配置，但 LangGraph loop 和 checkpoint 仍围绕自己的 messages/state。

## Pydantic AI 的做法

Pydantic AI 是最接近 AgentLoom 目标的样本：内部是自己的 message parts，provider 只是 adapter。

关键证据：

- `ToolReturnPart` 有 `tool_name`、`tool_call_id`、`outcome`，并区分发送给模型的内容、应用 metadata、多模态文件；失败工具返回可包装成错误对象。源码位置：`pydantic-ai-slim 2.45.0: pydantic_ai/messages.py:1397-1679`。
- `ModelRequest` 和 `ModelResponse` 是统一历史项；`ModelResponse.state` 区分 `complete`、`incomplete`、`suspended`、`interrupted`。源码位置：`pydantic_ai/messages.py:2059-2767`。
- `ThinkingPart`、`ToolCallPart`、`NativeToolCallPart`、`NativeToolReturnPart` 把 reasoning、普通函数工具、provider native tools 分成独立 part，而不是混进普通文本。源码位置：`pydantic_ai/messages.py:2156-2504`。
- `OpenAIResponsesModel` 是独立 model class。它在 `_process_response()` 中把 `ResponseReasoningItem` 映射为 `ThinkingPart`，把 `ResponseFunctionToolCall` 映射为 `ToolCallPart`，把 file/search/code/MCP 等 built-in tool output 映射为 native call/return parts。源码位置：`pydantic_ai/models/openai.py:1960-2070`、`openai.py:2349-2538`。
- OpenAI Responses adapter 的 `_map_messages()` 会把 `ToolReturnPart` 投影成 `function_call_output`，并处理 `previous_response_id`、reasoning id、response-scoped tool call id。源码位置：`pydantic_ai/models/openai.py:3302-3430`、`openai.py:3176-3254`。
- `AnthropicModel` 是独立 adapter。它把 Anthropic `thinking` 映射为 `ThinkingPart`，把 `tool_use` 映射为 `ToolCallPart`，把 `ToolReturnPart` 投影成 Anthropic `tool_result`。源码位置：`pydantic_ai/models/anthropic.py:815-910`、`anthropic.py:1600-1700`、`anthropic.py:2010-2220`。
- Agent 对外接收 `message_history: Sequence[ModelMessage]`，run result 也能返回 `all_messages()`、`new_messages()`；stream 返回 `ModelResponse` snapshot 和 agent stream events。源码位置：`pydantic_ai/agent/__init__.py:1229-1400`、`result.py:52-229`、`result.py:596-626`。

判断：Pydantic AI 的模式最能说明“新协议接入 = 新 adapter + 内部 part 模型扩展”，不是重写 agent graph。

## LlamaIndex 的做法

LlamaIndex 是 message-first，但 provider adapter 比 smolagents 丰富。它保留 `ChatMessage` / `ChatResponse`，Responses 由独立 LLM adapter 投影回来。

关键证据：

- `BaseLLM.chat()`、`stream_chat()`、`achat()`、`astream_chat()` 的输入输出都是 `ChatMessage` / `ChatResponse`。源码位置：`llama-index-core 0.14.24: llama_index/core/base/llms/base.py:27-296`。
- `ToolSelection` 是内部工具选择结构，`FunctionCallingLLM` 子类负责从 `ChatResponse` 里抽工具调用。源码位置：`llama_index/core/llms/llm.py:70-110`、`llama_index/core/llms/function_calling.py`。
- `OpenAIResponses` 是独立 LLM adapter。它调用 `self._client.responses.create(...)`，然后用 `_parse_response_output(response.output)` 生成 `ChatResponse`；streaming 时处理 Responses stream event，再 yield 当前 `ChatResponse`。源码位置：`llama-index-llms-openai 0.8.1: llama_index/llms/openai/responses.py:533-553`、`responses.py:691-745`、`responses.py:785-865`。
- `OpenAIResponses.get_tool_calls_from_response()` 从 `ChatResponse.message.blocks` 里的 `ToolCallBlock` 提取工具调用，说明 Responses output 已投影回 LlamaIndex 自己的 block。源码位置：`llama_index/llms/openai/responses.py:920-960`。
- agent workflow 基类仍用 `ChatMessage`、`ChatResponse`、`ToolCall`、`ToolCallResult`，并通过 `ctx.write_event_to_stream()` 输出 agent/tool 事件。源码位置：`llama-index-core 0.14.24: llama_index/core/agent/workflow/base_agent.py:249-352`、`base_agent.py:523-718`。
- 旧 `llama-index-agent-openai` 也仍然围绕 `ChatMessage` 和 OpenAI tool call 处理。源码位置：`llama-index-agent-openai 0.4.12: llama_index/agent/openai/step.py:224-241`、`step.py:594-736`。

判断：LlamaIndex 证明 message-first 框架也能接 Responses，但代价是必须把 Responses item 投影成自己的 block；没有看到为 Responses 重写 agent workflow。

## AutoGen 的做法

AutoGen 的核心抽象是 `ChatCompletionClient` 和 `LLMMessage`，provider wire protocol 只在 client adapter 内部出现。

关键证据：

- `LLMMessage` 是 `SystemMessage | UserMessage | AssistantMessage | FunctionExecutionResultMessage` 的 union；`AssistantMessage.content` 可以是文本或 `FunctionCall` 列表，`thought` 存 reasoning；`FunctionExecutionResultMessage` 承载多个工具结果。源码位置：`autogen-core 0.7.5: autogen_core/models/_types.py:10-82`。
- `CreateResult` 统一返回 `finish_reason`、`content`、`usage`、`cached`、`thought`。源码位置：`autogen_core/models/_types.py:107-127`。
- `ChatCompletionClient.create()` / `create_stream()` 是统一模型接口，输入是 `Sequence[LLMMessage]`，工具和 tool choice 是框架参数。源码位置：`autogen_core/models/_model_client.py:209-260`。
- `tool_agent_caller_loop()` 只关心 `ChatCompletionClient.create()` 是否返回 `FunctionCall` 列表；执行工具后追加 `FunctionExecutionResultMessage` 再请求模型。源码位置：`autogen_core/tool_agent/_caller_loop.py:16-80`。
- OpenAI adapter 有专门 `_message_transform.py` 把 `LLMMessage` 变成 OpenAI Chat Completions message；OpenAI client 调 `chat.completions.create()`，再把 `choice.message.tool_calls` 变成 `FunctionCall`，把 reasoning content 放到 `thought`。源码位置：`autogen-ext 0.7.5: autogen_ext/models/openai/_message_transform.py:175-465`、`autogen_ext/models/openai/_openai_client.py:700-807`。
- Anthropic adapter 把 `AssistantMessage` 的 `FunctionCall` 映射为 Anthropic `tool_use`，把 `FunctionExecutionResultMessage` 映射为 `tool_result`，再把返回的 `tool_use` 映射回 `FunctionCall`。源码位置：`autogen_ext/models/anthropic/_anthropic_client.py:237-314`、`_anthropic_client.py:552-765`。

判断：AutoGen 更偏 ChatCompletionClient 抽象。它没有把 OpenAI Responses 作为一等协议放进这个版本，但已经证明跨 provider 的惯例是 client adapter 映射到内部 message/result，而不是改 tool loop。

## CrewAI 的做法

CrewAI 的协议抽象比 Pydantic AI 和 OpenAI Agents SDK 浅一些。它偏向 provider wrapper 和执行器复用，适合参考“不要动 executor”，不适合作 Responses 保真参考。

关键证据：

- Agent 通过 `_LLM_TYPE_REGISTRY` 选择 `litellm`、`openai`、`anthropic`、`azure`、`bedrock`、`gemini` 等 LLM 实现；如果配置里只是 `model`，默认构造 `crewai.llm.LLM`。源码位置：`crewai 1.15.22: crewai/agents/agent_builder/base_agent.py:77-127`。
- `BaseLLM` 是统一边界，字段包括 `model`、`temperature`、`stream`、`api_key`、`base_url`、`provider`、`additional_params`。源码位置：`crewai/llms/base_llm.py:159-210`。
- 默认 `crewai.llm.LLM` 延迟加载 LiteLLM，并主要面向 `completion` / OpenAI-compatible 参数。源码位置：`crewai/llm.py:76-163`。
- `CrewAgentExecutor._invoke_loop()` 根据 LLM 是否支持 native function calling 选择 native tools 或 ReAct text fallback；native tools 路径仍是同一个 executor loop，不是为 provider 重写。源码位置：`crewai/agents/crew_agent_executor.py:331-350`、`crew_agent_executor.py:505-615`。
- `OpenAICompletion` 已经导入 OpenAI Responses 类型，也维护 responses-only model 和 reasoning-effort 兼容逻辑；但 executor 仍只看到 messages / tools / answer。源码位置：`crewai/llms/providers/openai/completion.py:37-45`、`completion.py:77-87`。
- `AnthropicCompletion` 是单独 provider wrapper，围绕 Anthropic `Message`、`ToolUseBlock`、`ThinkingBlock` 做映射。源码位置：`crewai/llms/providers/anthropic/completion.py:32-45`、`completion.py:166-220`。

判断：CrewAI 说明“执行器不因 provider 协议改变而重写”，但它的内部状态不是 item-first。如果 AgentLoom 想保真支持 reasoning、Responses built-in tools、replay，不能只学 CrewAI 的浅 wrapper。

## smolagents 的限制

smolagents 1.26.0 仍是 chat-completions 风格。

关键证据：

- `ChatMessage` 只有 `role`、`content`、`tool_calls`、`raw`、`token_usage`。没有 Responses item 序列，没有 `function_call_output`，没有 reasoning item。源码位置：`smolagents 1.26.0: smolagents/models.py:123-155`。
- `Model.generate()` 抽象返回 `ChatMessage`。源码位置：`smolagents/models.py:553-578`。
- `LiteLLMModel.generate()` 调 `litellm.completion(...)`，取 `response.choices[0].message.content/tool_calls`，再返回 `ChatMessage`。源码位置：`smolagents/models.py:1266-1307`。
- `ToolCallingAgent._step_stream()` 调 `self.model.generate(...)`，如果没有 `chat_message.tool_calls` 就调用 `parse_tool_calls()`，然后执行 `process_tool_calls()`。源码位置：`smolagents/agents.py:1276-1345`。
- `ActionStep.to_messages()` 把 tool call 和 observation 投影成 `MessageRole.TOOL_CALL` / `MessageRole.TOOL_RESPONSE` 的 chat messages。源码位置：`smolagents/memory.py:50-120`。

AgentLoom 当前已经在 smolagents 上做了本地扩展：

- `BaseAgent` 通过 `get_model(..., "smolagents")` 创建模型，并在运行时选择 `ToolCallingAgentV2` / `CodeAgentV2`。源码位置：`src/runtime/agent.py:31-45`、`src/runtime/agent.py:124-150`。
- `ToolCallingAgentV2` 已经覆盖 `_step_stream()` 和 `process_tool_calls()`，并用 `ToolCallRecord` 管工具执行、稳定 call id、并发工具调用。源码位置：`src/adapters/smolagents/agents.py:116-295`。
- `LiteLLMModelV2` 仍然继承 smolagents `LiteLLMModel`，走 `super().generate()`，然后校验 `ChatMessage.tool_calls`。源码位置：`src/adapters/smolagents/models/litellm_model.py:47-99`、`litellm_model.py:152-260`。

判断：smolagents 不支持 Responses，不是靠配置一开就能解决。可行做法是把它降级为执行基座和兼容视图，AgentLoom 自己持有 item 状态。

## LiteLLM 的位置

LiteLLM 适合做 transport 和部分转换，但不适合做 AgentLoom 的 canonical state。

关键证据来自 AgentLoom `.venv` 锁定的 `litellm==1.80.7`：

- `responses()` 会先找 provider 的 native Responses config；找不到时走 `litellm_completion_transformation_handler.response_api_handler(...)`，把 Responses-shaped request 变成 chat-completion provider 调用。源码位置：`.venv/lib/python3.12/site-packages/litellm/responses/main.py:619-645`。
- fallback transformer 能把 `function_call_output`、`function_call` 等 Responses input item 转成 Chat Completions message。源码位置：`.venv/lib/python3.12/site-packages/litellm/responses/litellm_completion_transformation/transformation.py:291-370`。
- 它也能把 Chat Completion response 转成 `ResponsesAPIResponse`，包括 output、tool choice、parallel tool calls、previous response id、reasoning 字段。源码位置：`.venv/lib/python3.12/site-packages/litellm/responses/litellm_completion_transformation/transformation.py:680-725`。
- Chat Completions 主路径支持 `responses/` model prefix 来做反向 bridge。源码位置：`.venv/lib/python3.12/site-packages/litellm/main.py:915-937`。

判断：可以用 LiteLLM 降低 provider 接入成本，但 AgentLoom 不应该让 LiteLLM 的 fallback 悄悄改变协议语义。`protocol: openai_responses` 配到不支持 native Responses 的 provider 时，要么显式声明“使用 LiteLLM fallback”，要么 fail fast。更稳的是第一版 fail fast。

## 工具调用怎么处理

开源项目的共同点是：工具调用和工具结果都有稳定关联 id。

- OpenAI Agents SDK 的 `Model` 接口要求每个工具调用有非空 call ID，并要求工具输出保留 call ID。源码位置：`agents/models/interface.py:37-44`。
- LangGraph 校验每个 `AIMessage.tool_calls` 都有对应 `ToolMessage.tool_call_id`。源码位置：`langgraph/prebuilt/chat_agent_executor.py:243-271`。
- Pydantic AI 的 `ToolReturnPart` 有 `tool_call_id`，失败、拒绝、中断都作为 outcome 进入历史。源码位置：`pydantic_ai/messages.py:1397-1449`。
- AutoGen 的 `FunctionExecutionResult` 带 `call_id`，`FunctionExecutionResultMessage` 可承载一组结果。源码位置：`autogen_core/models/_types.py:56-78`。

AgentLoom 应该落成同一条规则：

- canonical item 层保存 `function_call` 和 `function_call_output`。
- `function_call.call_id` 是工具执行、日志、checkpoint、replay 的关联键。
- smolagents 兼容视图可以继续投影为 `ChatMessageToolCall` 和 observation，但不能丢 call id。
- 对工具失败、权限拒绝、参数错误，要生成模型可见的 `function_call_output`，同时在 AgentLoom metadata 里保留结构化错误。

## Reasoning items 怎么处理

不要把 reasoning 当普通 assistant text。

开源项目的做法：

- OpenAI Agents SDK 把 `reasoning` 当 run item，并在 persistence 里专门处理 reasoning id 和 encrypted content。源码位置：`agents/items.py:76-153`、`agents/run_internal/session_persistence.py:1048-1118`。
- LangChain 把 Responses `reasoning` 放进 `AIMessage.content_blocks`，usage 里也有 reasoning token details。源码位置：`langchain_openai/chat_models/base.py:5137-5153`、`langchain_core/messages/ai.py:74-104`。
- Pydantic AI 用 `ThinkingPart` 表示 OpenAI reasoning 和 Anthropic thinking，并保留 signature、provider details。源码位置：`pydantic_ai/models/openai.py:2367-2400`、`pydantic_ai/models/anthropic.py:1643-1648`。
- AutoGen 只有较浅的 `thought` 字段，OpenAI/Anthropic adapter 会把 reasoning/thinking 映射进去。源码位置：`autogen_core/models/_types.py:41-49`、`autogen_ext/models/openai/_openai_client.py:779-807`、`autogen_ext/models/anthropic/_anthropic_client.py:702-756`。

AgentLoom 应该有单独的 `reasoning` item 类型，至少保存：

- provider；
- response id；
- item id；
- summary；
- raw content；
- encrypted content 或 signature；
- 是否可 replay；
- replay 时需要的 policy。

smolagents 视图不要显示 encrypted reasoning，不要把它拼进 assistant text。

## Streaming events 怎么处理

开源项目把 streaming 当 event 或 chunk 投影，不让 provider stream event 直接污染业务 loop。

- OpenAI Agents SDK 的 `Model.stream_response()` 返回 `TResponseStreamEvent`，而 Chat Completions 用 `ChatCmplStreamHandler` 转成 Responses-style stream event。源码位置：`agents/models/interface.py:102-135`、`agents/models/openai_chatcompletions.py:425-531`。
- LangChain stream 仍是 `BaseMessageChunk` / `AIMessageChunk`，Responses stream chunk 被 `_convert_responses_chunk_to_generation_chunk()` 转成 message chunk。源码位置：`langchain_openai/chat_models/base.py:5228-5507`。
- Pydantic AI 的 streaming result 能输出未验证的 `ModelResponse` snapshot，也能输出 agent stream events。源码位置：`pydantic_ai/result.py:52-229`。
- LlamaIndex 的 OpenAI Responses adapter 在每个 Responses event 后 yield 当前 `ChatResponse`。源码位置：`llama_index/llms/openai/responses.py:691-745`、`responses.py:809-865`。
- LangGraph stream mode 是图运行事件：`values`、`updates`、`messages`、`checkpoints`、`tasks`、`debug`。源码位置：`langgraph/pregel/main.py:2678-2720`。

AgentLoom 应该定义自己的 stream event：

- `item_started`；
- `item_delta`；
- `item_done`；
- `tool_call_delta`；
- `reasoning_delta`；
- `usage_delta`；
- `response_done`。

Responses event 可以较完整映射；Chat Completions 通常只能映射 text、tool call、usage；smolagents `ChatMessageStreamDelta` 只能作为兼容输出。

## Checkpoint 和 replay 怎么处理

开源经验里，checkpoint 存的是框架内部状态，不是某个 provider 的原始 wire payload。

- OpenAI Agents SDK session persistence 把 run items 转成 input items，并用 fingerprint / dedupe 控制重复工具输出。源码位置：`agents/run_internal/session_persistence.py:592-735`。
- LangGraph checkpoint 存 Pregel 图状态和 channel writes；`durability` 决定同步或异步持久化。源码位置：`langgraph/pregel/main.py:2899-2995`。
- Pydantic AI 的 run result 能返回 `all_messages()` / `new_messages()`；agent run 接收 `message_history`，并用 `conversation_id`、`run_id` 管 resume 边界。源码位置：`pydantic_ai/result.py:596-626`、`pydantic_ai/agent/__init__.py:1229-1400`。

AgentLoom 的 checkpoint 应该保存 canonical items，而不是只保存 smolagents memory：

- model input items；
- model output items；
- provider response id；
- provider request id；
- tool call id；
- tool result item；
- reasoning replay payload；
- protocol；
- adapter；
- lossy / lossless 投影标记。

OpenAI Responses 可选 `previous_response_id` / `conversation_id`，但这只能是 adapter 能力，不应变成全局 replay 假设。Chat 和 Anthropic 默认从 canonical history 全量投影。

## 对 AgentLoom 当前方案的判断

“保留 smolagents 基座 + AgentLoom item 层 + LiteLLM adapter 投影”符合开源经验，但要加几条硬约束。

推荐结构：

```text
AgentLoom runtime
  └─ AgentLoom canonical items / events / checkpoint
      ├─ smolagents compatibility bridge
      └─ model protocol adapters
          ├─ openai_responses
          ├─ openai_chat
          └─ anthropic_messages
```

符合开源经验的地方：

- 保留既有 runtime base，不为新协议全量重写。
- 在 model adapter 边界做 provider 投影。
- 内部先归一 tool call、tool result、reasoning、usage、stream event。
- checkpoint 存内部 canonical state。

不符合开源经验、需要避免的地方：

- 只写一个 `LiteLLMModelV2.responses()`，然后继续让 smolagents memory 当唯一状态源。这会丢 reasoning、Responses item id、`previous_response_id`、built-in tool 结果。
- 让 `protocol: openai_responses` 在 LiteLLM 里静默 fallback 到 chat completion。用户以为走 Responses，实际走 chat，这会破坏调试和回放。
- 把 Anthropic Messages 伪装成 OpenAI Responses。Anthropic 有 `tool_use` / `tool_result` / `thinking` 的真实语义，应该进 `anthropic_messages` adapter。
- 把 reasoning summary、encrypted content、signature 混成普通 assistant text。
- 在配置里继续保留已删除字段或模糊协议名。

## 建议的落地顺序

先做最小闭环：

- 新增 AgentLoom item model：`message`、`function_call`、`function_call_output`、`reasoning`、`usage`、`response_id`、`request_id`。
- 配置支持 `adapter: litellm` 和 `protocol: openai_responses | openai_chat | anthropic_messages`。
- `openai_responses` adapter 支持 text、function call、function call output、reasoning summary。
- `openai_chat` adapter 把 chat `tool_calls` 投影成同一套 `function_call` item。
- `anthropic_messages` adapter 把 `tool_use` / `tool_result` / `thinking` 投影成同一套 item。
- smolagents 继续跑工具执行、final answer、日志和现有生命周期，但只消费 AgentLoom items 的兼容投影。

再补状态能力：

- checkpoint 保存 canonical items；
- replay 从 canonical items 生成 provider payload；
- OpenAI Responses 可选 `previous_response_id`；
- reasoning 的 replay policy 明确写入 checkpoint；
- stream event 归一到 AgentLoom event bus。

最后再考虑 Responses-only 高阶能力：

- built-in tools；
- background / suspended response；
- tool search；
- computer use；
- provider-managed conversation。

这些能力不要在第一版假装支持。先让 text、function call、tool result、reasoning summary 跑通，且 checkpoint/replay 不丢语义。

## 底线

smolagents 不支持 Responses，所以不能让 smolagents 直接“适配 Responses”。正确做法是让 AgentLoom 自己拥有协议状态，再把能被 smolagents 表达的部分投影给它。

一句话：开源项目普遍会保留旧 loop，把协议差异收在 adapter 里；如果新协议带来旧内部模型表达不了的语义，就先升级自己的内部 item/event/checkpoint 模型，再给旧 loop 一个兼容视图。
