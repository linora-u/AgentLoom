# OpenAI Responses runtime reuse research

Date: 2026-09-18

Scope: research only. This note does not change runtime code. It answers one implementation question for the main AgentLoom runtime: if we remove `code_act`, keep only structured tool calling, and add configurable `chat` / `responses` model adapters, what should be reused instead of built from scratch?

## Decision summary

Use a small AgentLoom-owned Responses item model as the internal runtime protocol, and reuse existing libraries at the transport and schema boundaries:

1. Keep AgentLoom's runtime state item-first: `message`, `function_call`, `function_call_output`, `reasoning`, usage, response ID, and request metadata.
2. For provider calls, reuse LiteLLM's `responses()` / `aresponses()` when `llm.yaml` says `wire_api: responses`, and existing `completion()` / `acompletion()` when `wire_api: chat`.
3. For OpenAI-first typed boundaries, reuse the installed OpenAI Python SDK 2.x generated Responses models and helpers.
4. Do not adopt OpenAI Agents SDK as a dependency in this change. Its item and runner design is useful, but the current version requires OpenAI SDK 3.x and newer LiteLLM than AgentLoom currently allows.
5. Do not use smolagents as the Responses protocol layer. smolagents 1.26.0 is still centered on `ChatMessage`, chat-completion-shaped `tool_calls`, and `CodeAgent` / `ToolCallingAgent` loops.

This matches the product direction decided for this change: no `code_act` mode, no `tool_call_type` compatibility surface, no `execution_env` handling in the main runtime, no hidden fallback, and no TUI changes in the first implementation pass.

## Current AgentLoom facts

AgentLoom currently depends on:

- `smolagents[litellm,mcp,openai,telemetry]==1.26.0`, `litellm>=1.72.0`, and `openai>=2.8.1,<3.0.0` in `pyproject.toml`.
- The lockfile resolves `openai==2.8.1`, `litellm==1.80.7`, and `smolagents==1.26.0`.

Runtime coupling today:

- `agentloom.runtime.agent.BaseAgent` creates models through `get_model(..., "smolagents", ...)`.
- `RoleDrivenAgent` still allows `("tool_call", "code_act")`, defaults to `"tool_call"`, and selects between `ToolCallingAgentV2` and `CodeAgentV2`.
- `build_runtime_agent()` currently branches on `self.tool_call_type`: `tool_call` builds `ToolCallingAgentV2`; otherwise it builds `CodeAgentV2`.
- `NormalizedExecutionConfig` and `build_normalized_execution_config()` still normalize `execution_env`, `executor_type`, and `executor_kwargs`.
- `LiteLLMModelV2` subclasses smolagents `LiteLLMModel`, calls the parent `generate()`, then validates / repairs chat-style tool calls.

Sources:

- `pyproject.toml`
- `uv.lock`
- `src/runtime/agent.py`
- `src/application/validation.py`
- `src/adapters/smolagents/models/litellm_model.py`
- `src/adapters/smolagents/models/model_manager.py`

## OpenAI Python SDK 2.x

Verdict: reuse it for OpenAI Responses API transport and typed model boundaries, but do not make its generated classes the only internal AgentLoom state model.

Evidence from locked version `openai==2.8.1`:

- `client.responses.create(...)` exists, with first-class parameters for `input`, `instructions`, `max_output_tokens`, `parallel_tool_calls`, `previous_response_id`, `reasoning`, `tool_choice`, `tools`, streaming, headers, query/body passthrough, and timeout.
- `Response` has `output: List[ResponseOutputItem]`; the SDK explicitly warns not to assume the first output item is the assistant message, and offers `output_text` as a convenience aggregator.
- `ResponseFunctionToolCall` has `type="function_call"`, `call_id`, `name`, and JSON-string `arguments`.
- `ResponseInputItemParam.FunctionCallOutput` has `type="function_call_output"`, `call_id`, and `output`.
- `ResponseReasoningItem` has `type="reasoning"`, summary content, optional reasoning text, optional encrypted content, and status.
- `client.responses.parse(...)` and `parse_response()` can parse structured output text and strict/Pydantic function-call arguments.

Implementation implication:

- The OpenAI SDK already owns the canonical shape for OpenAI Responses. AgentLoom should use these types at the adapter boundary and for tests that validate OpenAI-compatible payloads.
- AgentLoom should still define its own narrow internal item abstraction. SDK generated models change as the API evolves, while AgentLoom needs stable storage, replay, hook, logging, goal-budget, and tool-result semantics.

Primary sources:

- OpenAI SDK repository: <https://github.com/openai/openai-python>
- OpenAI SDK v2.8.1 Responses resource: <https://github.com/openai/openai-python/tree/v2.8.1/src/openai/resources/responses>
- OpenAI SDK v2.8.1 typed Responses models: <https://github.com/openai/openai-python/tree/v2.8.1/src/openai/types/responses>
- OpenAI Responses API reference: <https://platform.openai.com/docs/api-reference/responses>
- OpenAI function-calling guide: <https://platform.openai.com/docs/guides/function-calling>
- OpenAI conversation-state guide: <https://platform.openai.com/docs/guides/conversation-state>

## LiteLLM

Verdict: reuse it as the configurable transport layer. Do not hand-write a full chat/responses compatibility bridge unless LiteLLM proves insufficient in a specific failing case.

Evidence from locked version `litellm==1.80.7`:

- `litellm.responses(...)` and `litellm.aresponses(...)` exist.
- The Responses path accepts the same important fields AgentLoom needs: `input`, `model`, `instructions`, `max_output_tokens`, `parallel_tool_calls`, `previous_response_id`, `reasoning`, `tool_choice`, `tools`, `temperature`, `stream`, headers, extra body/query, and timeout.
- If a provider has no native Responses adapter, LiteLLM's `responses()` path falls back through `LiteLLMCompletionResponsesConfig`, converting Responses requests into chat-completion requests and chat-completion responses back into `ResponsesAPIResponse`.
- LiteLLM also exposes a reverse bridge for chat-completion calls with a `responses/` model prefix: chat messages are converted into Responses input items, Responses output items are converted back into chat-completion choices/tool calls.
- Its transformations know about `function_call`, `function_call_output`, reasoning output, tool call conversion, `max_output_tokens` ↔ `max_tokens`, response format/text format, and usage conversion.

Implementation implication:

- Add explicit model-profile configuration, for example:

```yaml
model:
  powerful:
    adapter: litellm
    wire_api: responses  # or chat
    model: openai/gpt-5_5
```

- `wire_api: responses` should call `litellm.responses` / `litellm.aresponses` and return AgentLoom response items.
- `wire_api: chat` should call `litellm.completion` / `litellm.acompletion`, then adapt chat output into AgentLoom response items.
- Do not silently retry a failed `responses` request as `chat`, or vice versa. The selected `wire_api` is an operator-visible contract, not a best-effort hint.

Primary sources:

- LiteLLM repository: <https://github.com/BerriAI/litellm>
- LiteLLM documentation site: <https://docs.litellm.ai>
- Locked wheel source recorded in `uv.lock`: `litellm-1.80.7-py3-none-any.whl`, SHA `sha256:f7d993f78c1e0e4e1202b2a925cc6540b55b6e5fb055dd342d88b145ab3102ed`.
- Inspected files inside that wheel:
  - `litellm/responses/main.py`
  - `litellm/responses/litellm_completion_transformation/transformation.py`
  - `litellm/completion_extras/litellm_responses_transformation/handler.py`
  - `litellm/completion_extras/litellm_responses_transformation/transformation.py`
  - `litellm/types/llms/openai.py`

Note: the exact GitHub tag `v1.80.7` was not present when checked; the lockfile PyPI wheel is the precise source for this project.

## smolagents 1.26.0 / 1.25.0

Verdict: do not use smolagents as the Responses protocol layer. Keep only the minimal loop behavior we still need during the transition, or remove it from the main runtime if we own the new item loop.

Evidence from locked version `smolagents==1.26.0`:

- `ChatMessage` is the central model object and has `role`, `content`, `tool_calls`, `raw`, and `token_usage`.
- `LiteLLMModel.generate()` calls `litellm.completion(...)` and returns `ChatMessage` with `response.choices[0].message.tool_calls`.
- `ToolCallingAgent` calls `model.generate(..., tools_to_call_from=...)`; if `chat_message.tool_calls` is empty, it calls `model.parse_tool_calls(chat_message)`, then executes tool calls.
- `CodeAgent` is explicitly the mode where the model writes code and a Python executor runs it.

I also checked smolagents 1.25.0: it follows the same chat-message/tool-call architecture and does not provide a Responses item-first runtime.

Implementation implication:

- Deleting `code_act` means deleting AgentLoom's `CodeAgentV2` construction path and all execution-env-specific runtime propagation in the main runtime.
- The new runtime should not try to force Responses items through smolagents `ChatMessage` as its primary state. That would recreate the mismatch this change is meant to remove.
- If smolagents remains temporarily, it should be behind a chat-wire adapter only.

Primary sources:

- smolagents v1.26.0 source: <https://github.com/huggingface/smolagents/tree/v1.26.0/src/smolagents>
- smolagents v1.25.0 source: <https://github.com/huggingface/smolagents/tree/v1.25.0/src/smolagents>
- Locked wheel source recorded in `uv.lock`: `smolagents-1.26.0-py3-none-any.whl`, SHA `sha256:70e1cfb1576f782da93190ee31d9bb2659e5ca4bd84fda0c412e1f20498f28b6`.

## OpenAI Agents SDK

Verdict: do not add this dependency for the current AgentLoom change. Use it as a reference design for item modeling, replay, and Responses model adapters.

Evidence from `openai-agents==0.22.3`:

- The package requires `openai>=3.0.0,<4`, while AgentLoom currently constrains `openai>=2.8.1,<3.0.0`.
- Its `litellm` extra requires `litellm>=1.83.0`, while AgentLoom's lockfile currently uses `litellm==1.80.7`.
- It defines item aliases directly over OpenAI SDK Responses types: `TResponseInputItem = ResponseInputItemParam`, `TResponseOutputItem = ResponseOutputItem`, and `TResponseStreamEvent = ResponseStreamEvent`.
- `RunItemBase.to_input_item()` and `ModelResponse.to_input_items()` formalize the key replay rule: output items can be converted into input items for a later model call, after stripping output-only fields.
- `ToolCallItem` and `ToolCallOutputItem` model tool calls and outputs around the raw Responses item and `call_id`.
- `ItemHelpers.tool_call_output_item(...)` builds the model-visible `{"type": "function_call_output", "call_id": ..., "output": ...}` item.
- Its OpenAI Responses model adapter takes `str | list[TResponseInputItem]`, builds `responses.create()` kwargs, passes `previous_response_id`, converts tools, and calls `client.responses.create(...)`.

Implementation implication:

- Do not import `agents.*` in AgentLoom now; it forces a dependency migration bigger than this change.
- Copy the design pattern, not the package:
  - one internal item type per model-visible event;
  - output-to-input replay;
  - tool call output tied to original `call_id`;
  - model response object carrying `output`, usage, response ID, and request ID;
  - adapter-owned conversion to provider-specific payloads.

Primary sources:

- OpenAI Agents SDK docs: <https://openai.github.io/openai-agents-python/>
- PyPI package metadata: <https://pypi.org/project/openai-agents/>
- Source: <https://github.com/openai/openai-agents-python/tree/v0.22.3/src/agents>
- Inspected wheel files:
  - `agents/items.py`
  - `agents/models/interface.py`
  - `agents/models/openai_responses.py`

## Recommended AgentLoom implementation shape

### 1. Configuration

Add model-profile fields to `config/llm.yaml`:

```yaml
model:
  powerful:
    adapter: litellm
    wire_api: responses  # responses | chat
```

Rules:

- `adapter` starts as `litellm`; direct OpenAI SDK can be added later only if there is a concrete need.
- `wire_api` is explicit. No automatic fallback.
- Unknown `adapter` / `wire_api` should fail fast.
- Remove the runtime meaning of `tool_call_type`. If old YAML still contains `tool_call_type`, ignore it silently.
- Remove the runtime meaning of `execution_env`. If old YAML still contains `execution_env`, ignore it silently.

### 2. Internal items

Create an AgentLoom-owned internal model, kept intentionally smaller than the full OpenAI SDK schema:

- `LoomMessageItem`: role + content.
- `LoomFunctionCallItem`: `call_id`, `name`, JSON object arguments, optional provider/raw ID.
- `LoomFunctionCallOutputItem`: `call_id`, output text or structured content.
- `LoomReasoningItem`: summary/encrypted/provider payload fields needed for replay and trace.
- `LoomModelResponse`: output items, usage, response ID, request ID, raw provider response.

The invariant should be: every model turn consumes a list of AgentLoom items and returns a `LoomModelResponse`. Chat is an adapter format; Responses is the internal shape.

### 3. Runtime loop

Replace the `tool_call_type` branch with one structured loop:

1. Build model input items from task, memory, previous model output items, and tool outputs.
2. Call the selected model adapter.
3. Persist all output items, not only final text and chat-style tool calls.
4. For every `function_call` item, execute exactly one AgentLoom tool with the same `call_id`.
5. Append a `function_call_output` item for each result.
6. Continue until final-answer semantics are reached.

This is the part AgentLoom should own because it must integrate Hook Plan, file history, goal-budget accounting, sub-task tracking, MCP wrapping, tool authorization, logging, checkpoint/resume, and self-learning.

### 4. Adapter boundaries

Implement two adapters behind one interface:

- `LiteLLMResponsesAdapter`
  - input: internal items
  - output: internal items
  - transport: `litellm.responses` / `litellm.aresponses`
  - schema source: OpenAI SDK / LiteLLM Responses models

- `LiteLLMChatAdapter`
  - input: internal items
  - output: internal items
  - transport: `litellm.completion` / `litellm.acompletion`
  - conversion: reuse LiteLLM bridge behavior where possible; keep AgentLoom's stricter validation for tool name and JSON object arguments.

### 5. Non-goals for the first implementation pass

- No TUI bridge changes.
- No compatibility mode for `code_act`.
- No compatibility mode for `tool_call_type`.
- No `execution_env` processing in main runtime.
- No OpenAI Agents SDK dependency.
- No silent fallback between `responses` and `chat`.
- No broad documentation rewrite until the runtime is passing targeted tests.

## Suggested validation

Minimum targeted tests before changing the full suite:

1. Config parsing accepts `adapter: litellm`, `wire_api: responses`, and `wire_api: chat`.
2. Config parsing silently ignores legacy `tool_call_type` and `execution_env`.
3. Main runtime never instantiates `CodeAgentV2`.
4. Responses adapter round-trips:
   - user message -> model call;
   - `function_call` -> tool execution;
   - tool result -> `function_call_output`;
   - final message -> final answer.
5. Chat adapter maps chat `tool_calls` into the same internal `LoomFunctionCallItem` shape.
6. Reasoning items are preserved in memory/replay and logs without being treated as visible final text.
7. Goal token accounting still records usage from both wire APIs.

## Bottom line

The shortest clean path is:

1. Build AgentLoom's own small Responses-item runtime model.
2. Reuse OpenAI SDK 2.x for official types/helpers.
3. Reuse LiteLLM 1.80.7 for `responses` and `chat` transports and provider-specific conversions.
4. Treat smolagents as legacy scaffolding to retire from the main runtime, not as the new protocol abstraction.
5. Treat OpenAI Agents SDK as reference architecture only until AgentLoom is ready to migrate to OpenAI SDK 3.x and LiteLLM 1.83+.
