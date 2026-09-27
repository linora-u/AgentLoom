# Agent 系统指令与连续任务的开源实现

日期：2026-09-26。范围：只读调研，依据官方文档和官方源码。问题是 Application 如何表达常驻的 system prompt，以及“做完任务 A，再给同一个 Agent 发任务 B”。这里的“连续任务”指两次完成边界明确的 Agent 调用，而不是一次输入里写两句要求。

## 核心发现

四个已核验框架都区分 **Agent 的常驻指令**、**本次用户输入**和 **跨调用保存的会话状态**。需要等 A 完成才投递 B 时，调用方依次运行两次，并复用同一会话的历史或 ID。列表形态的 `messages` / `input items` 表示一次调用携带的消息，并不自动形成“逐项运行、等待结果、再运行下一项”的任务队列。[LangChain invocation](https://docs.langchain.com/oss/python/langchain/agents#invocation)、[OpenAI Runner 输入与会话管理](https://openai.github.io/openai-agents-python/running_agents/#the-agent-loop)、[PydanticAI message history](https://ai.pydantic.dev/message-history/#reusing-messages-in-a-conversation)、[smolagents `run()` 源码](https://github.com/huggingface/smolagents/blob/227ef5e49ddd82339295939072f0223249aa8d38/src/smolagents/agents.py#L436-L484)。

在这四个公开 API 中，没有找到“一个 `task` 字段接受字符串或字符串列表，并由 runtime 自动把列表逐项执行”的约定。这是**样本范围内的观察**，不能推断所有开源项目都不这样做。实现这种 YAML 形式仍可行，但其逐项调度、失败停止和恢复位置应由 AgentLoom 的 Application 层定义，不能把下层框架的消息列表当成现成的顺序执行语义。

| 框架 | 常驻指令 | 本次任务 | 做完后继续同一会话 |
| --- | --- | --- | --- |
| LangChain Agent / LangGraph | `create_agent(system_prompt=...)` | `agent.invoke({"messages": [{"role": "user", ...}]})` | 再次 `invoke`，复用 `thread_id`，前提是配置 checkpointer。[[配置](https://docs.langchain.com/oss/python/langchain/agents#system-prompt)] [[调用与续聊](https://docs.langchain.com/oss/python/langchain/agents#invocation)] |
| OpenAI Agents SDK | `Agent(instructions=...)` | `Runner.run(agent, input)`；`input` 可是字符串或 Responses API input items 列表 | 再调用 `Runner.run`，传 `result.to_input_list()` 加新 user item，或复用 `session`。[[Agent](https://openai.github.io/openai-agents-python/agents/)] [[运行与历史](https://openai.github.io/openai-agents-python/running_agents/#conversations-chat-sessions)] |
| PydanticAI | `Agent(instructions=...)` | `agent.run_sync(user_prompt)` | 再次 `run_sync(new_prompt, message_history=result.new_messages())`。[[官方示例](https://ai.pydantic.dev/message-history/#reusing-messages-in-a-conversation)] |
| smolagents | `prompt_templates["system_prompt"]` 构造 Agent system prompt | `agent.run(task: str)` | 再次 `agent.run(next_task, reset=False)` 保留之前的 memory。[[源码](https://github.com/huggingface/smolagents/blob/227ef5e49ddd82339295939072f0223249aa8d38/src/smolagents/agents.py#L298-L358)] [[run 方法](https://github.com/huggingface/smolagents/blob/227ef5e49ddd82339295939072f0223249aa8d38/src/smolagents/agents.py#L436-L484)] |

## 关键区别

OpenAI Agents SDK 的 `Runner.run` 确实允许 `input` 是列表，但官方将它定义为 **Responses API 格式的 input items**；官方多轮示例在第一次运行完成后，才将 `result.to_input_list()` 与新的 user item 拼接并再次运行。它也提供 `SQLiteSession`，在两次 `Runner.run` 中使用同一个 session。[官方文档 `Runner` 输入](https://github.com/openai/openai-agents-python/blob/588826c5be27cad21a3067463e21972ffea38561/docs/running_agents.md#L24-L40)、[手动与 Session 续聊](https://github.com/openai/openai-agents-python/blob/588826c5be27cad21a3067463e21972ffea38561/docs/running_agents.md#L300-L352)。

LangChain Agent 的 `messages` 是会话 state；其官方示例在两次 `invoke` 中各传一个新 user message，复用 `thread_id`。文档也明确说持久化需要 checkpointer。因此，把两段任务文字放入一次 `messages` 列表，并不等于获得两次 Agent 完成边界。[Agent state](https://docs.langchain.com/oss/python/langchain/agents#agent-state)、[Invocation](https://docs.langchain.com/oss/python/langchain/agents#invocation)。

PydanticAI 官方示例分别调用 `run_sync('Tell me a joke.')` 和 `run_sync('Explain?', message_history=result1.new_messages())`。文档还指出，传入非空 `message_history` 时，不再生成新 system prompt，因为历史应包含原 system prompt；这进一步说明跨调用会话历史不是一份新的任务列表。[官方文档](https://github.com/pydantic/pydantic-ai/blob/5badf40a3c5156cdc893c7f9993f856d4de8a2bb/docs/message-history.md#L151-L170)。

smolagents 的 `run` 参数是 `task: str`，`reset` 默认 `True`；设为 `False` 才保留上一轮 memory。每次 `run` 都向 memory 加入新的 `TaskStep`。这是 AgentLoom 目前两种 runtime 中最直接的参照。[官方源码](https://github.com/huggingface/smolagents/blob/227ef5e49ddd82339295939072f0223249aa8d38/src/smolagents/agents.py#L436-L497)。

## 对 AgentLoom 配置的建议

`system_prompt` 与 `task` 分属不同消息角色；用户提出的 `system_prompt: {path: ...}` 可以作为 **AgentLoom 自己的文件加载语法**，加载后仍作为 system 指令发送。上述框架并没有共同的 `path` 配置标准，因此不应宣称它来自某个 runtime 的原生 API。

若需要一个配置键同时覆盖单任务和预设的连续任务，建议保留一个顶层 `task`，用**显式子字段**表达多轮语义：

```yaml
task: 先分析需求。
```

```yaml
task:
  sequence:
    - 先分析需求。
    - 根据上一步的结论提出设计。
```

这样仍是一个顶层键；`sequence` 明确要求“上一轮完成后，在同一会话追加一条新的 user message 并再次运行”。`task: [A, B]` 也可以自行定义为相同语义，但从上述框架看，裸列表容易和“一次调用包含多条消息”混淆；在用户可见的 YAML 中，显式标记更容易解释。真正执行时，Application 编排层应逐项调用 runtime，并保存当前项的序号和会话状态，避免恢复时重发已经完成的任务。这是从框架调用方式推出的 **AgentLoom 设计建议**，不是某框架自带的 YAML 功能。

若第二条任务是在运行期间才出现（例如人继续输入），它不是静态 `sequence`：应直接以新的 user message 调用同一会话，不必预先写入 YAML。[LangChain 续聊示例](https://docs.langchain.com/oss/python/langchain/agents#invocation)、[OpenAI Session 示例](https://openai.github.io/openai-agents-python/running_agents/#conversations-chat-sessions)。
