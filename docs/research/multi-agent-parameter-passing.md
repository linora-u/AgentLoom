# 主流开源 Agent 框架的多 Agent 参数传递

日期：2026-09-22

范围：只读调研。本文只使用各项目的官方文档与官方 GitHub 源码，回答 Agent-as-tool、handoff、参数 schema、被委派 Agent 的实际输入，以及 instructions、description、task、output schema 的职责边界。本文不代表已实施产品改动。

## 结论

六个框架的共同设计不是把 Agent prompt、工具参数和输出要求包装进一套自定义文本协议，而是保留四个独立通道：

1. Agent 的长期行为约束属于 `instructions`、system message 或 Agent 自身配置。
2. Agent/tool description 只告诉上游模型这个 Agent 能做什么、何时调用。
3. 本轮 task 通过工具 JSON Schema 生成的参数传入，再成为下游的 user message、messages/state 或 typed object。
4. output schema 独立约束和校验返回值，不属于本轮输入文案。

因此，AgentLoom 可以删除 `<task_spec>`、`<workflow>`、`<task_request>`、`<inputs>`、`<output>`、固定 guidance、标题、桥接语和相应模板加载器。最小边界应是：

```text
workflow                    -> Worker 的 system/instructions
agent_function_schema       -> 上游可见的 tool name/description/JSON parameters
本次 tool arguments         -> Worker 的 user input 或 typed input
真实的 structured output    -> 独立 output schema/validator
```

不建议把 `workflow + 两个换行 + task` 固化为新的协议。若 runtime 原生支持消息角色，应直接传独立 system 和 user 消息；只有 runtime 只有单字符串接口时，拼接才是 adapter 的实现限制，不应泄漏到 Application 领域模型。

## 横向对比

| 框架 | Agent-as-tool / delegation 参数 | 被委派 Agent 实际收到 | 固定 prompt 与 task 是否分离 | output schema |
| --- | --- | --- | --- | --- |
| OpenAI Agents SDK | 默认严格 JSON `{"input": string}`；可用 Pydantic/dataclass 自定义参数 | 默认单字段直接成为 nested run 的原始字符串；handoff 默认转交 conversation items | 是 | `output_type` 独立；tool 结果可单独提取 |
| LangGraph / LangChain | 普通 tool JSON args，也可注入 graph state | `messages/state`；典型 subagent 工具把 `query` 放进独立 human message | 是 | structured response 属于 Agent/graph 输出 |
| Microsoft AutoGen | Pydantic/JSON `{"task": string}` | `TextMessage(content=task, source="user")` | 是 | `output_content_type` 独立 |
| CrewAI | Pydantic/JSON `{task, context, coworker}` | 新建 `Task`，再把 task、expected-output 文案和 context 格式化为 user prompt | Agent 身份与 task 分离，但 task 内部有文本拼接 | 普通 Task 支持 typed output；delegation 临时 Task 默认返回字符串 |
| Google ADK | `input_schema` 生成 typed JSON；无 schema 时 `{request: string}` | 推荐 single-turn 路径保留 typed input，再转成 user content；旧路径传原文或裸 JSON | 是 | `output_schema` 独立声明和校验 |
| PydanticAI | Python 函数签名生成 JSON Schema | delegation tool 显式调用下游 `agent.run(user_prompt)`；也可显式传 message history | 是 | `output_type` 独立，工具返回值也可 typed |

CrewAI 证明“格式化单个 user prompt”是一种可行实现，但不是六个框架的共同协议。它也没有把 Agent 身份 prompt、tool description 和 typed output schema全部塞入该字符串。不能用这个特例证明 AgentLoom 需要保留当前 Prompt Protocol。

## OpenAI Agents SDK

核验版本：官方仓库 commit [`32edd3c3ecde37a7fb6bf4b082f35f1d8f7f086b`](https://github.com/openai/openai-agents-python/tree/32edd3c3ecde37a7fb6bf4b082f35f1d8f7f086b)。

`Agent.as_tool()` 默认使用 `AgentAsToolInput`，只有一个 `input: str`；也允许传 Pydantic model 或 dataclass 作为 `parameters`，由 `TypeAdapter` 生成严格 JSON Schema。[源码](https://github.com/openai/openai-agents-python/blob/32edd3c3ecde37a7fb6bf4b082f35f1d8f7f086b/src/agents/agent.py#L606-L705)

默认单字段参数会被还原为原始字符串，再作为 nested `Runner.run(input=...)` 的输入；它不会自动增加 XML 标签或 AgentLoom 式指导语。[输入解析](https://github.com/openai/openai-agents-python/blob/32edd3c3ecde37a7fb6bf4b082f35f1d8f7f086b/src/agents/agent_tool_input.py#L79-L107)；[nested run](https://github.com/openai/openai-agents-python/blob/32edd3c3ecde37a7fb6bf4b082f35f1d8f7f086b/src/agents/agent.py#L721-L753)

自定义多字段参数默认会被渲染成结构化输入文本，但 `input_builder` 可以完全替换这个投影，解析后的 payload 也独立保存在 `RunContextWrapper.tool_input`。这说明“模型生成的 typed args”和“下游模型看到的 input”是两层，框架文案不是参数协议本身。[源码](https://github.com/openai/openai-agents-python/blob/32edd3c3ecde37a7fb6bf4b082f35f1d8f7f086b/src/agents/agent_tool_input.py#L13-L107)

handoff 的 `input_type` 只定义 handoff tool-call 的元数据，并传给 `on_handoff`；官方明确说明它不替代下一 Agent 的 main input。接收 Agent 默认继续看到 conversation history。[官方文档](https://github.com/openai/openai-agents-python/blob/32edd3c3ecde37a7fb6bf4b082f35f1d8f7f086b/docs/handoffs.md#L62-L103)

`instructions` 是 system prompt，`handoff_description` 用于上游选择 handoff，`tool_description` 描述工具用途，`output_type` 负责输出类型。这些字段在 Agent API 中彼此独立。[Agent 定义](https://github.com/openai/openai-agents-python/blob/32edd3c3ecde37a7fb6bf4b082f35f1d8f7f086b/src/agents/agent.py#L318-L356)；[Agent-as-tool 文档](https://github.com/openai/openai-agents-python/blob/32edd3c3ecde37a7fb6bf4b082f35f1d8f7f086b/docs/tools.md#L665-L719)

## LangGraph / LangChain

核验版本：LangGraph 官方仓库 commit [`49cce0ca852be4cfb567a1cbe0e511ff325a1682`](https://github.com/langchain-ai/langgraph/tree/49cce0ca852be4cfb567a1cbe0e511ff325a1682)；官方 supervisor 仓库 commit [`88859b34017ac3569bbd4a3092c7e77593a0a960`](https://github.com/langchain-ai/langgraph-supervisor-py/tree/88859b34017ac3569bbd4a3092c7e77593a0a960)。

LangGraph Agent 的典型状态是 `messages: Sequence[BaseMessage]`。字符串 prompt 会被转换为单独的 `SystemMessage`，再放到 state messages 前面，不会和本轮 human message拼成一个字符串。[源码](https://github.com/langchain-ai/langgraph/blob/49cce0ca852be4cfb567a1cbe0e511ff325a1682/libs/prebuilt/langgraph/prebuilt/chat_agent_executor.py#L57-L170)

`ToolNode` 接收 graph state、messages 或直接 tool calls。模型填写 JSON args；`InjectedState` 等运行态参数由框架注入，并从模型可见的 tool schema 中排除。[ToolNode](https://github.com/langchain-ai/langgraph/blob/49cce0ca852be4cfb567a1cbe0e511ff325a1682/libs/prebuilt/langgraph/prebuilt/tool_node.py#L622-L669)；[InjectedState](https://github.com/langchain-ai/langgraph/blob/49cce0ca852be4cfb567a1cbe0e511ff325a1682/libs/prebuilt/langgraph/prebuilt/tool_node.py#L1753-L1818)

官方 Subagents 示例的最小模式是 `call_research_agent(query: str)`，再用 `{"messages": [{"role": "user", "content": query}]}` 调用 subagent。tool description 与 `query` 分开定义。[官方文档](https://docs.langchain.com/oss/python/langchain/multi-agent/subagents#basic-implementation)

handoff 则通过 `Command` 更新 state 和 routing。官方 supervisor 的默认 handoff 工具注入 state 与 tool call id，并把 messages state 交给 worker，而不是生成新的 workflow/task 文本协议。[源码](https://github.com/langchain-ai/langgraph-supervisor-py/blob/88859b34017ac3569bbd4a3092c7e77593a0a960/langgraph_supervisor/handoff.py#L55-L129)

## Microsoft AutoGen

核验版本：官方仓库 commit [`027ecf0a379bcc1d09956d46d12d44a3ad9cee14`](https://github.com/microsoft/autogen/tree/027ecf0a379bcc1d09956d46d12d44a3ad9cee14)。

`AgentTool` 继承 `TaskRunnerTool`，其参数模型只有必填 `task: str`。基础 Tool 用 Pydantic `model_json_schema()` 生成模型可见的 JSON Schema。[TaskRunnerTool](https://github.com/microsoft/autogen/blob/027ecf0a379bcc1d09956d46d12d44a3ad9cee14/python/packages/autogen-agentchat/src/autogen_agentchat/tools/_task_runner_tool.py#L14-L52)；[BaseTool schema](https://github.com/microsoft/autogen/blob/027ecf0a379bcc1d09956d46d12d44a3ad9cee14/python/packages/autogen-core/src/autogen_core/tools/_base.py#L114-L148)

工具把 `args.task` 原样传给 `agent.run(task=...)`，`BaseChatAgent.run` 再把字符串转为 `TextMessage(content=task, source="user")`。[源码](https://github.com/microsoft/autogen/blob/027ecf0a379bcc1d09956d46d12d44a3ad9cee14/python/packages/autogen-agentchat/src/autogen_agentchat/agents/_base_chat_agent.py#L111-L153)

AgentTool 的 name/description 来自 Agent name/description；`system_message` 独立保存为 `SystemMessage`；`output_content_type` 是独立的 Pydantic 输出模型。[AgentTool](https://github.com/microsoft/autogen/blob/027ecf0a379bcc1d09956d46d12d44a3ad9cee14/python/packages/autogen-agentchat/src/autogen_agentchat/tools/_agent.py#L79-L83)；[AssistantAgent 配置](https://github.com/microsoft/autogen/blob/027ecf0a379bcc1d09956d46d12d44a3ad9cee14/python/packages/autogen-agentchat/src/autogen_agentchat/agents/_assistant_agent.py#L724-L770)

AutoGen 的 handoff 是 `HandoffMessage` 加 LLM context 的消息/上下文转交，并非再次拼接 task spec。[handoff 定义](https://github.com/microsoft/autogen/blob/027ecf0a379bcc1d09956d46d12d44a3ad9cee14/python/packages/autogen-agentchat/src/autogen_agentchat/base/_handoff.py#L12-L57)；[接收上下文](https://github.com/microsoft/autogen/blob/027ecf0a379bcc1d09956d46d12d44a3ad9cee14/python/packages/autogen-agentchat/src/autogen_agentchat/agents/_assistant_agent.py#L1013-L1025)

## CrewAI

核验版本：官方仓库 commit [`bdd1bc62007fcee732c912f0daa093e1f760f3fc`](https://github.com/crewAIInc/crewAI/tree/bdd1bc62007fcee732c912f0daa093e1f760f3fc)。

delegation tool 的参数是明确的 Pydantic 模型：`task: str`、`context: str`、`coworker: str`。[源码](https://github.com/crewAIInc/crewAI/blob/bdd1bc62007fcee732c912f0daa093e1f760f3fc/lib/crewai/src/crewai/tools/agent_tools/delegate_work_tool.py#L8-L30)

执行时，CrewAI 创建 `Task(description=task, agent=selected_agent, expected_output=manager_request)`，再调用被选 Agent。[源码](https://github.com/crewAIInc/crewAI/blob/bdd1bc62007fcee732c912f0daa093e1f760f3fc/lib/crewai/src/crewai/tools/agent_tools/base_agent_tools.py#L110-L123)

这一框架确实会在 user task 内拼接 task、expected-output 指导语和 context。[Task prompt](https://github.com/crewAIInc/crewAI/blob/bdd1bc62007fcee732c912f0daa093e1f760f3fc/lib/crewai/src/crewai/task.py#L971-L1061)；[执行准备](https://github.com/crewAIInc/crewAI/blob/bdd1bc62007fcee732c912f0daa093e1f760f3fc/lib/crewai/src/crewai/agent/core.py#L677-L703)

但 Agent 的长期身份仍来自 role/goal/backstory，并可作为独立 system prompt；一般 Task 的 `output_json`、`output_pydantic`、`response_model` 也独立存在。[prompt 分层](https://github.com/crewAIInc/crewAI/blob/bdd1bc62007fcee732c912f0daa093e1f760f3fc/lib/crewai/src/crewai/utilities/prompts.py#L93-L140)；[Task 输出字段](https://github.com/crewAIInc/crewAI/blob/bdd1bc62007fcee732c912f0daa093e1f760f3fc/lib/crewai/src/crewai/task.py#L120-L204)

CrewAI delegation 创建的临时 Task 没有设置 typed output，默认把结果作为字符串返回。这说明 output prose 是该 delegation adapter 的选择，不等于输出契约必须进入输入 prompt。

## Google ADK

核验版本：官方仓库 commit [`fc16a78a2bba393f35c9b44025cf45548cdda3f9`](https://github.com/google/adk-python/tree/fc16a78a2bba393f35c9b44025cf45548cdda3f9)。

官方当前推荐 single-turn sub-agent 取代旧 `AgentTool`。single-turn Agent 可声明 `input_schema`、`output_schema`，并以 `mode="single_turn"` 注册到 `sub_agents`。[官方 sample](https://github.com/google/adk-python/blob/fc16a78a2bba393f35c9b44025cf45548cdda3f9/contributing/samples/multi_agent/single_turn_sub_agent/README.md#L1-L51)

有 `input_schema` 时，ADK 从 Pydantic model 生成 function declaration；没有时退化为必填 `request: string`。执行时先用 `input_schema.model_validate(args)` 得到 typed object，否则取 `args["request"]`。[schema](https://github.com/google/adk-python/blob/fc16a78a2bba393f35c9b44025cf45548cdda3f9/src/google/adk/tools/agent_tool.py#L159-L217)；[执行](https://github.com/google/adk-python/blob/fc16a78a2bba393f35c9b44025cf45548cdda3f9/src/google/adk/tools/agent_tool.py#L393-L431)

single-turn wrapper 把 node input 转成 user-role content。旧 AgentTool 也只是：单字符串原样传递，typed args 则验证后发送裸 JSON 文档；源码注释明确禁止混入额外 prose，否则下游 schema 验证会失败。[single-turn wrapper](https://github.com/google/adk-python/blob/fc16a78a2bba393f35c9b44025cf45548cdda3f9/src/google/adk/workflow/_llm_agent_wrapper.py#L313-L357)；[旧 AgentTool](https://github.com/google/adk-python/blob/fc16a78a2bba393f35c9b44025cf45548cdda3f9/src/google/adk/tools/agent_tool.py#L220-L253)

Agent `instruction`、工具使用的 `description`、`input_schema` 和回复使用的 `output_schema` 是独立配置。最终输出单独经过 output schema 校验。[Agent 字段](https://github.com/google/adk-python/blob/fc16a78a2bba393f35c9b44025cf45548cdda3f9/src/google/adk/agents/llm_agent.py#L448-L460)；[输出校验](https://github.com/google/adk-python/blob/fc16a78a2bba393f35c9b44025cf45548cdda3f9/src/google/adk/workflow/_llm_agent_wrapper.py#L360-L390)

`transfer_to_agent` 只携带目标 Agent 名和可选 reason，并通过 action 转移当前 query/conversation 的控制权，不携带另一份 task spec。[源码](https://github.com/google/adk-python/blob/fc16a78a2bba393f35c9b44025cf45548cdda3f9/src/google/adk/tools/transfer_to_agent_tool.py#L27-L132)

## PydanticAI

核验版本：官方仓库 commit [`8809abe5f740e35dd559d34e7c222b97cb8130f6`](https://github.com/pydantic/pydantic-ai/tree/8809abe5f740e35dd559d34e7c222b97cb8130f6)。

官方的 Agent delegation 是在父 Agent 的 function tool 内显式调用另一个 `agent.run(...)`。示例工具参数 `count: int` 由 Python 签名生成 schema，下游 Agent 收到普通 user prompt `Please generate {count} jokes.`；下游的 `output_type=list[str]` 独立校验结果，工具直接返回 typed list。[官方文档](https://github.com/pydantic/pydantic-ai/blob/8809abe5f740e35dd559d34e7c222b97cb8130f6/docs/multi-agent-applications.md#L13-L80)

当前 `SubAgents` convenience 暴露统一的 `delegate_task(agent_name, task)`；每次 delegation 有自己的 message history，delegate 默认看不到 parent conversation。[同一官方文档](https://github.com/pydantic/pydantic-ai/blob/8809abe5f740e35dd559d34e7c222b97cb8130f6/docs/multi-agent-applications.md#L18-L25)

PydanticAI 从函数签名提取参数 JSON Schema，从 docstring 提取工具及参数 description。[Tool schema 文档](https://github.com/pydantic/pydantic-ai/blob/8809abe5f740e35dd559d34e7c222b97cb8130f6/docs/tools.md#L256-L267)

`Agent.run` 将 `user_prompt`、`output_type`、`message_history`、`instructions` 和 `deps` 保持为独立参数，框架没有要求把它们编码为一个 Prompt Protocol。[API 源码](https://github.com/pydantic/pydantic-ai/blob/8809abe5f740e35dd559d34e7c222b97cb8130f6/pydantic_ai_slim/pydantic_ai/agent/abstract.py#L473-L576)

## 对 AgentLoom 当前实现的判断

当前实现并非只“读取了一个 prompts 文件”，而是把这个文件实现成运行时协议：

- `src/application/prompts/agent_tool_behavior_spec.yaml` 定义全部标签、标题、guidance、output rules 和 bridge instruction。
- `src/application/factory.py` 在模块导入时加载并校验整套 YAML。
- Worker agent-as-tool 把 workflow、参数 description/value、output description 和固定规则重新拼成一个 `formatted_query`。
- Supervisor 也把 description、workflow 和本轮 task 包成 `<task_spec>` / `<task_request>`。
- `workflow: list[str]` 还被解释为多次顺序 run，并把原 task 以 `<inputs>` 附到第一段。

这套协议同时重复表达了 runtime 本来已经拥有的 system/user role、tool JSON Schema 和 output contract，增加 token、耦合测试，并让 `workflow` 的语义从“Agent prompt”漂移成“本轮 task 的包装内容”。

另一个独立问题是，当前 `agent_function_schema.inputs` 全部强制归一为 `string`。这比上述框架的 typed args 能力更弱。删除 Prompt Protocol 时不必立刻扩展类型系统，但应避免继续把参数 description 拼给 Worker；description 属于上游 tool schema。

## 最小改造建议

### 应删除

- `src/application/prompts/agent_tool_behavior_spec.yaml`。
- `factory.py` 内 Prompt Protocol 路径、加载、变量展开、required keys 和全部协议常量。
- task-spec / Mermaid workflow 渲染与 warning 注入中只为 Prompt Protocol 服务的逻辑。
- Worker 的 inputs/output/task-spec 格式化器。
- Supervisor 的 `<task_spec>` / `<task_request>` 包装器与 list workflow 的 `<inputs>` 注入。
- 所有只验证上述协议文本与标签的测试。

### 应保留并重新接线

- `name` 与 `agent_function_schema.description`：上游 tool metadata。
- `agent_function_schema.inputs`：上游 JSON Schema 和调用参数校验。
- `workflow`：Worker/Supervisor 的 Agent instructions/system prompt。若 runtime 支持独立 instructions，应在 runtime definition 构建时一次设置，不要每轮复制。
- 本轮 task：独立 user message。
- Agent-as-tool 单字符串参数：直接作为 user text。
- Agent-as-tool 多参数：优先保留 typed dict/object；若当前 runtime 只接字符串，只做稳定 JSON 序列化，不添加 description、标题、XML 或 Markdown 包装。

### `agent_function_schema.output` 是否保留

当前代码只读取 `output.description`，再把它渲染进 `<output>` 文本；它没有形成 JSON Schema，也没有驱动 runtime structured-output validation。因此，**按当前能力，Prompt Protocol 删除后它没有运行时约束价值**。

合理选择只有两个：

1. 本次彻底删除 `agent_function_schema.output`，等真正实现 structured output 时再引入明确的 typed schema；这是最小、最诚实的方案。
2. 若必须保持 YAML 兼容，可暂时保留为非运行时 metadata，但不得继续注入 prompt，并明确标记 deprecated。它不能被描述为“输出校验”。

不建议只保留现有 `output.description` 并暗示它能约束输出。OpenAI Agents SDK、AutoGen、Google ADK 和 PydanticAI 保留的是可执行的 output type/schema，而不是一段被拼进 user task 的自然语言。

### `workflow: list[str]`

如果产品定义已经明确“workflow 就是传给 Agent 的 prompt”，最干净的合同是收敛成一个字符串。兼容期可以仅在 system/instructions 通道用 `\n\n` 合并列表，但不应把每项解释成一次新的 user task/run。六个框架都没有把 instructions 列表默认解释为串行多 Agent workflow。

## 最终建议

删除整套 Prompt Protocol，不要用一个更短的自定义文本模板替代它：

```text
Agent definition:
  instructions = workflow

Agent-as-tool definition:
  name
  description
  parameters = JSON Schema

Invocation:
  one string argument -> user message
  multiple/typed arguments -> typed object, or bare JSON fallback

Result:
  plain result until a real output schema/validator exists
```

这既是六个主流框架的共同最小交集，也直接回答了两个容易混淆的问题：

- 是，`workflow` 应当是传给 Agent 的固定 prompt/instructions。
- 否，不应把 workflow、参数 description、本轮 task 和 output description 再拼成一个 AgentLoom 专属字符串。
