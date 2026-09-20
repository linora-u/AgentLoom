# smolagents + Responses item runtime options

Date: 2026-09-18

Scope: research only. No runtime code was changed. This note answers the Q3 follow-up: AgentLoom should keep smolagents as the runtime base if possible, while removing `code_act`, making tool calling the only execution mode, and supporting an internal Responses-item model with LiteLLM-backed protocol adapters.

## Short answer

Do not replace smolagents wholesale in this pass.

smolagents 1.25 / 1.26 does not natively run an OpenAI Responses item loop. Its public model contract is still `Model.generate(...) -> ChatMessage`, and `ToolCallingAgent` consumes `ChatMessage.tool_calls`. However, AgentLoom already subclasses smolagents in `ToolCallingAgentV2`, overrides tool execution, and overrides memory projection through `LoomAgentMixin`. That gives a better path than a full rewrite:

1. Keep smolagents as the base object model for tools, managed agents, logging, callbacks, `RunResult`, and existing lifecycle.
2. Add an AgentLoom-owned item layer for `message`, `function_call`, `function_call_output`, and `reasoning`.
3. Add a new LiteLLM-backed model adapter that can call OpenAI Responses, OpenAI Chat Completions, and Anthropic Messages explicitly.
4. Add a new `ResponsesToolCallingAgentV2` / response-aware `ToolCallingAgentV2` path that overrides only the step boundary where smolagents currently assumes `ChatMessage.tool_calls`.

This preserves smolagents as the AgentLoom runtime base, avoids forking upstream, and limits conversion to adapter boundaries instead of translating back and forth throughout the whole loop.

## Constraints from current decisions

- `adapter` / protocol selection in `llm.yaml` should be required. There should be no implicit fallback to chat.
- `code_act` is removed from the main runtime, not deprecated as a user-selectable mode.
- `tool_call_type` should have no runtime meaning. If old YAML contains it, ignore it silently.
- `execution_env` should have no runtime meaning for this migration. If old YAML contains it, ignore it silently.
- TUI is out of scope for this pass.
- Documentation scope is targeted: update only the user-facing configuration/runtime docs affected by `adapter`, `tool_call_type`, `execution_env`, and the Agent YAML contract.

## 1. Does smolagents natively support Responses item?

No.

The pinned local package is `smolagents==1.26.0` in `pyproject.toml`, and the virtualenv confirms smolagents 1.26.0 with LiteLLM 1.80.7 and OpenAI SDK 2.8.1.

Evidence from smolagents 1.26.0:

- `ChatMessage` is the central model response object. It has `role`, `content`, `tool_calls`, `raw`, and `token_usage`, but no item sequence, no `function_call_output`, and no reasoning item field: `.venv/lib/python3.12/site-packages/smolagents/models.py:123`.
- `Model.generate(...)` is defined to return a `ChatMessage`: `.venv/lib/python3.12/site-packages/smolagents/models.py:553`.
- `LiteLLMModel.generate(...)` builds chat-completion kwargs, calls `self.client.completion(...)`, then returns the first choice as a `ChatMessage`: `.venv/lib/python3.12/site-packages/smolagents/models.py:1266`.
- `ToolCallingAgent._step_stream(...)` calls `model.generate(...)`, reads `chat_message.tool_calls`, optionally calls `model.parse_tool_calls(...)`, then executes those tool calls: `.venv/lib/python3.12/site-packages/smolagents/agents.py:1276`.
- `ActionStep.to_messages(...)` projects tool calls and observations back into chat-shaped messages with roles like `tool-call` and `tool-response`, not Responses input items: `.venv/lib/python3.12/site-packages/smolagents/memory.py:50`.
- `CodeAgent` is the Python-code execution path and owns `executor_type` / `executor_kwargs`: `.venv/lib/python3.12/site-packages/smolagents/agents.py:1505`.

I also downloaded and inspected the smolagents 1.25.0 wheel. Its `models.py`, `agents.py`, and `memory.py` have the same core class positions (`ChatMessage`, `Model`, `LiteLLMModel`, `MultiStepAgent`, `ToolCallingAgent`, `AgentMemory`, `ActionStep`), and no source hits for `function_call_output` or `ResponseReasoningItem`. So 1.25 and 1.26 are effectively the same for this decision.

What smolagents can support is a custom `Model.generate()` implementation that internally calls Responses and returns a `ChatMessage`. That is not native Responses-item runtime support; it is an adapter shim.

## 2. LiteLLM protocol support relevant to this migration

LiteLLM is the right transport layer to reuse, but the first version should explicitly gate which protocol/provider combinations AgentLoom supports.

In the local pinned LiteLLM 1.80.7:

- `litellm.responses(...)` exists and accepts Responses-like inputs: `input`, `model`, `instructions`, `max_output_tokens`, `parallel_tool_calls`, `previous_response_id`, `reasoning`, `tool_choice`, `tools`, `temperature`, `stream`, `extra_headers`, `extra_query`, `extra_body`, and `timeout`: `.venv/lib/python3.12/site-packages/litellm/responses/main.py:498`.
- `litellm.responses(...)` first checks `ProviderConfigManager.get_provider_responses_api_config(...)`: `.venv/lib/python3.12/site-packages/litellm/responses/main.py:620`.
- Native Responses provider configs in 1.80.7 are OpenAI, Azure OpenAI, XAI, GitHub Copilot, and LiteLLM Proxy. Anthropic is not returned here: `.venv/lib/python3.12/site-packages/litellm/utils.py:7392`.
- If no native Responses provider config exists, LiteLLM falls back through `litellm_completion_transformation_handler.response_api_handler(...)`, which converts Responses-shaped requests to chat-completion calls: `.venv/lib/python3.12/site-packages/litellm/responses/main.py:635`.
- The fallback transformer understands `function_call`, `function_call_output`, `web_search_call`, and `computer_call_output` input items: `.venv/lib/python3.12/site-packages/litellm/responses/litellm_completion_transformation/transformation.py:291`.
- The transformer builds chat-completion messages from Responses input, including `function_call_output` as a tool message with `tool_call_id`: `.venv/lib/python3.12/site-packages/litellm/responses/litellm_completion_transformation/transformation.py:354`.
- It also maps chat completion responses back into `ResponsesAPIResponse.output`, including message items, tool calls, and reasoning-content items when present: `.venv/lib/python3.12/site-packages/litellm/responses/litellm_completion_transformation/transformation.py:680`.
- `litellm.completion(...)` has a reverse Responses bridge: a model marked `mode: responses` or prefixed with `responses/` routes chat-completion calls through a Responses adapter: `.venv/lib/python3.12/site-packages/litellm/main.py:915` and `.venv/lib/python3.12/site-packages/litellm/main.py:1427`.
- Anthropic / Claude is supported by LiteLLM through normal chat completion conversion and an Anthropic Messages pass-through interface. `litellm.anthropic_interface.messages.create(...)` exposes the Anthropic `/v1/messages` shape with `messages`, `system`, `thinking`, `tool_choice`, and `tools`: `.venv/lib/python3.12/site-packages/litellm/anthropic_interface/messages/__init__.py:82`.
- The Anthropic chat transformer maps OpenAI-style `tools`, `tool_choice`, `parallel_tool_calls`, `response_format`, and reasoning/thinking params to Anthropic Messages fields: `.venv/lib/python3.12/site-packages/litellm/llms/anthropic/chat/transformation.py:122`.

Recommended first-version protocol surface:

```yaml
model:
  powerful:
    adapter: litellm        # required
    protocol: openai_responses  # openai_responses | openai_chat | anthropic_messages
    model: openai/gpt-5_5
```

Rules:

- `adapter: litellm` is required for this migration.
- `protocol` is required; do not silently default to chat.
- First supported protocols should be:
  - `openai_responses`: use `litellm.responses(...)`; intended for OpenAI/Azure/XAI/LiteLLM-proxy Responses-capable deployments.
  - `openai_chat`: use `litellm.completion(...)` with OpenAI-compatible chat schema.
  - `anthropic_messages`: use LiteLLM Anthropic Messages or LiteLLM chat completion’s Anthropic adapter for Claude.
- First supported providers should be OpenAI-compatible and Anthropic/Claude. Other LiteLLM providers should fail fast until explicitly enabled and tested.
- If `protocol: openai_responses` is configured with Anthropic in LiteLLM 1.80.7, AgentLoom should not pretend that Claude has a native Responses API. Either reject it or explicitly document that it is a LiteLLM fallback-to-chat transformation. Given the “no fallback” constraint, the safer first version is to reject it.

## 3. OpenAI SDK and open-source references worth reusing

The OpenAI Python SDK 2.8.1 already has the canonical item shapes:

- `ResponseFunctionToolCall` has `arguments`, `call_id`, `name`, `type="function_call"`, optional `id`, and `status`: `.venv/lib/python3.12/site-packages/openai/types/responses/response_function_tool_call.py:11`.
- `ResponseReasoningItem` has `type="reasoning"`, `summary`, optional reasoning `content`, optional `encrypted_content`, and `status`: `.venv/lib/python3.12/site-packages/openai/types/responses/response_reasoning_item.py:27`.
- Responses input supports `FunctionCallOutput` with `type="function_call_output"`, `call_id`, and `output`: `.venv/lib/python3.12/site-packages/openai/types/responses/response_input_param.py:112`.
- `client.responses.create(...)` supports `previous_response_id`, `reasoning`, `tool_choice`, and `tools`: `.venv/lib/python3.12/site-packages/openai/resources/responses/responses.py:87`.

OpenAI Agents Python is useful as a reference implementation, but not as a direct dependency in this pass:

- `openai-agents==0.22.3` requires `openai>=3.0.0,<4`, while AgentLoom currently pins `openai>=2.8.1,<3.0.0`.
- Its LiteLLM extra requires `litellm>=1.83.0`, while this repo currently locks 1.80.7.
- It has exactly the runtime concepts AgentLoom should imitate: `TResponseInputItem`, `ToolCallItem`, `ToolCallOutputItem`, `to_input_item`, and an OpenAI Responses model path calling `responses.create(...)`.

Use this as design inspiration, not as a dependency replacement for smolagents.

Official docs checked:

- smolagents agents reference: <https://huggingface.co/docs/smolagents/main/en/reference/agents>
- smolagents models reference: <https://huggingface.co/docs/smolagents/main/en/reference/models>
- LiteLLM Responses API docs: <https://docs.litellm.ai/docs/response_api>
- LiteLLM Anthropic provider docs: <https://docs.litellm.ai/docs/providers/anthropic>
- Anthropic Messages API docs: <https://docs.anthropic.com/en/api/messages>

## 4. Options

### Option A — Thin model shim, keep upstream `ToolCallingAgent._step_stream`

Shape:

- Implement `LiteLLMResponsesModel`, a smolagents `Model` subclass.
- It calls `litellm.responses(...)`.
- It converts `Response.output` into one smolagents `ChatMessage`:
  - `message` output -> `ChatMessage.content`
  - `function_call` output -> `ChatMessage.tool_calls`
  - `reasoning` output -> `ChatMessage.raw` or side-channel metadata
- Keep upstream `ToolCallingAgent._step_stream()` unchanged.

Work: low to medium.

Risk:

- This does not really make the runtime item-first. It hides Responses inside the model boundary.
- `function_call_output` still becomes smolagents `tool-response` / chat message text on replay.
- Reasoning items are easy to lose or accidentally expose as ordinary content.
- Some conversion is unavoidable on every model step, because smolagents still stores `ActionStep` as chat-shaped state.

When to use:

- Only as a short spike to prove `litellm.responses(...)` works with configured OpenAI endpoints.
- Not enough for the final architecture if “内部也改成 Responses item 模型” is a hard requirement.

### Option B — Keep smolagents base, override the item boundary in AgentLoom

Shape:

- Keep `MultiStepAgent` / `ToolCallingAgent` inheritance and AgentLoom’s existing `ToolCallingAgentV2` integration.
- Add a response-aware subclass or mode, for example `ResponsesToolCallingAgentV2`.
- Override `_step_stream(...)` so the action step operates on AgentLoom-owned items instead of smolagents `ChatMessage.tool_calls`.
- Reuse existing smolagents and AgentLoom pieces:
  - smolagents `run()` / `_run_stream()` lifecycle;
  - `Tool`, `managed_agents`, `FinalAnswerTool`, tool schema conversion;
  - AgentLoom `execute_tool_call_record(...)`, hook injection, subtask tracking, logging, goal accounting, context compression, checkpoint lifecycle.
- Add explicit protocol adapters:
  - `LiteLLMOpenAIResponsesAdapter`: AgentLoom items -> `litellm.responses(...)` -> AgentLoom items.
  - `LiteLLMOpenAIChatAdapter`: AgentLoom items -> `litellm.completion(...)` -> AgentLoom items.
  - `LiteLLMAnthropicMessagesAdapter`: AgentLoom items -> Anthropic Messages via LiteLLM -> AgentLoom items.
- Store raw provider output on the step for observability, but persist the canonical AgentLoom item list as the replay source.

Work: medium.

Risk:

- Need careful tests around `planning_interval`, `provide_final_answer`, context compression, and `reset=False`, because upstream helpers still assume `ChatMessage`.
- Need a clean bridge for existing callbacks/tests that read `ActionStep.model_output_message`, `tool_calls`, or `observations`.
- `ActionStep` is a smolagents dataclass but not slotted, so AgentLoom can attach item metadata without modifying upstream. That is useful but should be wrapped in explicit helper functions to avoid hidden dynamic fields spreading.

Why this is better than a full rewrite:

- It honors the fact that smolagents is AgentLoom’s runtime base.
- It avoids forking smolagents.
- It limits “Responses item <-> smolagents chat” conversion to the compatibility edge: legacy callbacks and smolagents `RunResult`, not the model/tool protocol itself.
- It keeps the main semantic loop in one place under AgentLoom control: model items in, tool calls out, tool outputs back as `function_call_output`.

### Option C — Full AgentLoom runner, stop using smolagents `ToolCallingAgent`

Shape:

- Implement a new AgentLoom `run()` / step loop / memory / result / callback stack.
- Reuse only smolagents `Tool` wrappers, or remove smolagents entirely later.

Work: high.

Risk:

- Rebuilds many behaviors that are already embedded in smolagents + AgentLoom extensions: managed agents, logging, `RunResult`, callback timing, max-step handling, final answer checks, replay, streaming event shape, and current tests.
- Highest chance of behavior drift in Worker/Supervisor orchestration.
- Forces too many decisions at once while the real product goal is narrower: remove `code_act` and add explicit protocol adapters.

When to use:

- Only if Option B discovers an upstream hard wall, such as `_run_stream` or `ActionStep` assumptions that cannot be safely isolated.

## 5. Recommendation

Choose Option B.

The first implementation should keep smolagents as the runtime base but move the model/tool protocol inside AgentLoom to Responses-style items. In concrete terms:

1. Delete the `CodeAgentV2` construction path and remove `code_act` from main runtime selection.
2. Stop treating `tool_call_type` as configuration. If present, ignore it.
3. Stop treating `execution_env` as main-runtime configuration. If present, ignore it.
4. Require `adapter: litellm` and an explicit `protocol`.
5. Support only `openai_responses`, `openai_chat`, and `anthropic_messages` in the first version.
6. Add an AgentLoom item model:
   - `message`
   - `function_call`
   - `function_call_output`
   - `reasoning`
   - usage / response id / provider request id metadata
7. Add a response-aware smolagents subclass that overrides `_step_stream(...)` and uses the new item adapters.
8. Keep compatibility projection from items to `ActionStep` fields for existing logs, callbacks, tests, and `RunResult`.

This is the smallest design that answers all constraints: no code-act compatibility, no user-selected tool-call mode, no hidden fallback, LiteLLM-owned provider protocol handling, and no abandonment of smolagents as the AgentLoom base.

## 6. Suggested validation for implementation

Minimum tests before broad refactor:

1. Config requires `adapter` and `protocol`; missing or unsupported values fail before model calls.
2. Old `tool_call_type` and `execution_env` keys are silently ignored.
3. Main runtime never instantiates `CodeAgentV2`.
4. `openai_responses` adapter round-trips `function_call -> tool execution -> function_call_output`.
5. `openai_chat` adapter maps chat `tool_calls` into the same internal `function_call` items.
6. `anthropic_messages` adapter maps Claude `tool_use/tool_result` into the same internal item model.
7. Reasoning items are persisted/replayed as reasoning metadata, not appended to visible assistant text.
8. Existing Hook, goal accounting, context compression, subtask tracking, and final answer checks still see compatible `ActionStep` / `RunResult` projections.
