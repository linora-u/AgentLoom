"""LiteLLM transports behind AgentLoom's model-turn contract."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any

from agentloom.configuration.model_adapters import MODEL_ADAPTERS, AdapterKind
from agentloom.runtime.model_protocol import (
    FunctionCallItem,
    FunctionCallOutputItem,
    MessageItem,
    ModelItem,
    ModelProtocolError,
    ModelTurnAdapter,
    ModelTurnRequest,
    ModelTurnResult,
    ModelUsage,
    ReasoningItem,
    ToolDefinition,
)

Transport = Callable[..., Any]

_SHARED_RESERVED_OPTIONS = frozenset({"adapter", "custom_llm_provider", "model"})
_STRUCTURAL_OPTIONS: dict[AdapterKind, frozenset[str]] = {
    "openai_chat": _SHARED_RESERVED_OPTIONS | frozenset({"messages", "tools"}),
    "openai_responses": frozenset(
        {
            "input",
            "include",
            "instructions",
            "tools",
            "previous_response_id",
        }
    )
    | _SHARED_RESERVED_OPTIONS,
    "anthropic_messages": _SHARED_RESERVED_OPTIONS | frozenset({"messages", "tools"}),
}


def _cached_text_content(
    text: str,
    *,
    boundary: str | None,
    block_type: str,
) -> list[dict[str, Any]]:
    if boundary and boundary in text:
        static_text, dynamic_text = text.split(boundary, 1)
        blocks: list[dict[str, Any]] = []
        if static_text.strip():
            blocks.append(
                {
                    "type": block_type,
                    "text": static_text,
                    "cache_control": {"type": "ephemeral"},
                }
            )
        if dynamic_text.strip():
            blocks.append({"type": block_type, "text": dynamic_text})
        return blocks
    return [
        {
            "type": block_type,
            "text": text,
            "cache_control": {"type": "ephemeral"},
        }
    ]


def _apply_system_prompt_cache(
    messages: list[dict[str, Any]],
    *,
    enabled: bool,
    boundary: str | None,
    block_type: str,
) -> None:
    if not enabled:
        return
    for message in messages:
        if message.get("role") != "system":
            continue
        content = message.get("content")
        if isinstance(content, str) and content:
            message["content"] = _cached_text_content(
                content,
                boundary=boundary,
                block_type=block_type,
            )
        return


def _as_dict(value: Any, *, context: str) -> dict[str, Any]:
    if isinstance(value, Mapping):
        return dict(value)
    model_dump = getattr(value, "model_dump", None)
    if callable(model_dump):
        dumped = model_dump()
        if isinstance(dumped, Mapping):
            return dict(dumped)
    raise ModelProtocolError(f"{context} must be an object")


def _validated_options(
    adapter_id: AdapterKind,
    options: Mapping[str, Any],
) -> dict[str, Any]:
    overlap = sorted(_STRUCTURAL_OPTIONS[adapter_id].intersection(options))
    if overlap:
        raise ValueError(
            f"reserved model turn option for {adapter_id}: {', '.join(overlap)}"
        )
    return dict(options)


def _tool_definition_for_chat(tool: ToolDefinition) -> dict[str, Any]:
    function: dict[str, Any] = {
        "name": tool.name,
        "description": tool.description,
        "parameters": dict(tool.parameters),
    }
    if tool.strict is not None:
        function["strict"] = tool.strict
    return {"type": "function", "function": function}


def _chat_assistant_message(
    items: list[MessageItem | FunctionCallItem],
) -> dict[str, Any]:
    text: list[str] = []
    tool_calls: list[dict[str, Any]] = []
    for item in items:
        if isinstance(item, MessageItem):
            text.append(item.text)
        else:
            tool_calls.append(
                {
                    "id": item.call_id,
                    "type": "function",
                    "function": {
                        "name": item.name,
                        "arguments": item.arguments_json,
                    },
                }
            )
    message: dict[str, Any] = {
        "role": "assistant",
        "content": "".join(text) or None,
    }
    if tool_calls:
        message["tool_calls"] = tool_calls
    return message


def _items_to_chat_messages(
    items: tuple[ModelItem, ...],
    *,
    instructions: str | None,
) -> list[dict[str, Any]]:
    messages: list[dict[str, Any]] = []
    if instructions is not None:
        messages.append({"role": "system", "content": instructions})

    assistant_items: list[MessageItem | FunctionCallItem] = []

    def flush_assistant_items() -> None:
        if assistant_items:
            messages.append(_chat_assistant_message(assistant_items))
            assistant_items.clear()

    for item in items:
        if isinstance(item, MessageItem) and item.role == "assistant":
            assistant_items.append(item)
        elif isinstance(item, FunctionCallItem):
            assistant_items.append(item)
        elif isinstance(item, ReasoningItem):
            continue
        else:
            flush_assistant_items()
            if isinstance(item, MessageItem):
                messages.append({"role": item.role, "content": item.text})
            elif isinstance(item, FunctionCallOutputItem):
                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": item.call_id,
                        "content": item.output,
                    }
                )
            else:
                raise ModelProtocolError(
                    f"openai_chat cannot replay canonical item {type(item).__name__}"
                )
    flush_assistant_items()
    return messages


def _usage_from_chat(raw_usage: Any) -> ModelUsage:
    if raw_usage is None:
        return ModelUsage()
    usage = _as_dict(raw_usage, context="chat usage")
    prompt_details = _as_dict(
        usage.get("prompt_tokens_details") or {},
        context="chat prompt token details",
    )
    completion_details = _as_dict(
        usage.get("completion_tokens_details") or {},
        context="chat completion token details",
    )
    return ModelUsage(
        input_tokens=int(usage.get("prompt_tokens") or 0),
        output_tokens=int(usage.get("completion_tokens") or 0),
        total_tokens=int(usage.get("total_tokens") or 0),
        cached_input_tokens=int(prompt_details.get("cached_tokens") or 0),
        reasoning_tokens=int(completion_details.get("reasoning_tokens") or 0),
        details=usage,
    )


def _default_completion_transport(**request: Any) -> Any:
    import litellm

    return litellm.completion(**request)


def _default_responses_transport(**request: Any) -> Any:
    import litellm

    return litellm.responses(**request)


def _tool_definition_for_responses(tool: ToolDefinition) -> dict[str, Any]:
    wire: dict[str, Any] = {
        "type": "function",
        "name": tool.name,
        "description": tool.description,
        "parameters": dict(tool.parameters),
    }
    if tool.strict is not None:
        wire["strict"] = tool.strict
    return wire


def _item_to_responses_input(item: ModelItem) -> dict[str, Any] | None:
    if isinstance(item, MessageItem):
        if item.replay_payload.get("type") == "message":
            allowed = {"id", "type", "role", "status", "content"}
            return {
                key: value
                for key, value in item.replay_payload.items()
                if key in allowed
            }
        return {"role": item.role, "content": item.text}
    if isinstance(item, FunctionCallItem):
        if item.replay_payload.get("type") == "function_call":
            allowed = {
                "id",
                "type",
                "status",
                "call_id",
                "name",
                "arguments",
            }
            return {
                key: value
                for key, value in item.replay_payload.items()
                if key in allowed
            }
        wire: dict[str, Any] = {
            "type": "function_call",
            "call_id": item.call_id,
            "name": item.name,
            "arguments": item.arguments_json,
        }
        if item.item_id is not None:
            wire["id"] = item.item_id
        if item.status is not None:
            wire["status"] = item.status
        return wire
    if isinstance(item, FunctionCallOutputItem):
        wire = {
            "type": "function_call_output",
            "call_id": item.call_id,
            "output": item.output,
        }
        if item.item_id is not None:
            wire["id"] = item.item_id
        # Tool outcomes (completed/error/blocked) belong to the output record,
        # not Responses' optional item-lifecycle status field.
        return wire
    if isinstance(item, ReasoningItem):
        if item.replay_payload.get("type") != "reasoning":
            raise ModelProtocolError(
                "openai_responses cannot replay reasoning from another protocol"
            )
        allowed = {
            "id",
            "type",
            "status",
            "summary",
            "content",
            "encrypted_content",
        }
        return {
            key: value
            for key, value in item.replay_payload.items()
            if key in allowed
        }
    raise ModelProtocolError(
        f"openai_responses cannot replay canonical item {type(item).__name__}"
    )


def _summary_texts(raw_summary: Any) -> tuple[str, ...]:
    if raw_summary is None:
        return ()
    if not isinstance(raw_summary, list):
        raise ModelProtocolError("responses reasoning summary must be a list")
    result: list[str] = []
    for raw_part in raw_summary:
        part = _as_dict(raw_part, context="responses reasoning summary part")
        if part.get("type") != "summary_text" or not isinstance(part.get("text"), str):
            raise ModelProtocolError("unsupported responses reasoning summary part")
        result.append(part["text"])
    return tuple(result)


def _message_text(raw_content: Any) -> str:
    if not isinstance(raw_content, list):
        raise ModelProtocolError("responses message content must be a list")
    text: list[str] = []
    for raw_part in raw_content:
        part = _as_dict(raw_part, context="responses message content")
        if part.get("type") != "output_text" or not isinstance(part.get("text"), str):
            raise ModelProtocolError(
                f"unsupported responses message content type: {part.get('type')!r}"
            )
        text.append(part["text"])
    return "".join(text)


def _items_from_responses(raw_output: Any) -> tuple[ModelItem, ...]:
    if not isinstance(raw_output, list):
        raise ModelProtocolError("responses output must be a list")
    result: list[ModelItem] = []
    for raw_item in raw_output:
        item = _as_dict(raw_item, context="responses output item")
        item_type = item.get("type")
        if item_type == "reasoning":
            result.append(
                ReasoningItem(
                    item_id=str(item["id"]) if item.get("id") is not None else None,
                    status=(
                        str(item["status"]) if item.get("status") is not None else None
                    ),
                    summary=_summary_texts(item.get("summary")),
                    replay_payload=item,
                )
            )
        elif item_type == "function_call":
            try:
                result.append(
                    FunctionCallItem(
                        item_id=(
                            str(item["id"]) if item.get("id") is not None else None
                        ),
                        status=(
                            str(item["status"])
                            if item.get("status") is not None
                            else None
                        ),
                        call_id=str(item["call_id"]),
                        name=str(item["name"]),
                        arguments_json=str(item["arguments"]),
                        replay_payload=item,
                    )
                )
            except KeyError as exc:
                raise ModelProtocolError(
                    f"responses function call is missing {exc.args[0]}"
                ) from exc
            except (TypeError, ValueError) as exc:
                raise ModelProtocolError(
                    f"malformed responses function call for {item.get('name')!r}: {exc}"
                ) from exc
        elif item_type == "message":
            role = item.get("role")
            if role not in {"system", "developer", "user", "assistant"}:
                raise ModelProtocolError(f"unsupported responses message role: {role!r}")
            result.append(
                MessageItem(
                    role=role,
                    text=_message_text(item.get("content")),
                    item_id=str(item["id"]) if item.get("id") is not None else None,
                    status=(
                        str(item["status"]) if item.get("status") is not None else None
                    ),
                    replay_payload=item,
                )
            )
        else:
            raise ModelProtocolError(
                f"unsupported responses output item type: {item_type!r}"
            )
    return tuple(result)


def _usage_from_responses(raw_usage: Any) -> ModelUsage:
    if raw_usage is None:
        return ModelUsage()
    usage = _as_dict(raw_usage, context="responses usage")
    input_details = _as_dict(
        usage.get("input_tokens_details") or {},
        context="responses input token details",
    )
    output_details = _as_dict(
        usage.get("output_tokens_details") or {},
        context="responses output token details",
    )
    return ModelUsage(
        input_tokens=int(usage.get("input_tokens") or 0),
        output_tokens=int(usage.get("output_tokens") or 0),
        total_tokens=int(usage.get("total_tokens") or 0),
        cached_input_tokens=int(input_details.get("cached_tokens") or 0),
        reasoning_tokens=int(output_details.get("reasoning_tokens") or 0),
        details=usage,
    )


class OpenAIChatModelTurnAdapter:
    """OpenAI-compatible Chat Completions through LiteLLM."""

    adapter_id = "openai_chat"

    def __init__(
        self,
        *,
        transport: Transport | None = None,
        context_cache: bool = False,
        system_prompt_boundary: str | None = None,
    ) -> None:
        self._transport = transport or _default_completion_transport
        self._context_cache = context_cache
        self._system_prompt_boundary = system_prompt_boundary

    def turn(self, request: ModelTurnRequest) -> ModelTurnResult:
        messages = _items_to_chat_messages(
            request.items,
            instructions=request.instructions,
        )
        _apply_system_prompt_cache(
            messages,
            enabled=self._context_cache,
            boundary=self._system_prompt_boundary,
            block_type="text",
        )
        wire_request: dict[str, Any] = {
            "model": request.model,
            "messages": messages,
            "custom_llm_provider": "openai",
        }
        if request.tools:
            wire_request["tools"] = [
                _tool_definition_for_chat(tool) for tool in request.tools
            ]
        wire_request.update(_validated_options(self.adapter_id, request.options))
        raw_response = self._transport(**wire_request)
        response = _as_dict(raw_response, context="chat response")
        choices = response.get("choices")
        if not isinstance(choices, list) or len(choices) != 1:
            raise ModelProtocolError("chat response must contain exactly one choice")
        choice = _as_dict(choices[0], context="chat choice")
        message = _as_dict(choice.get("message"), context="chat message")

        output: list[ModelItem] = []
        content = message.get("content")
        if content is not None:
            if not isinstance(content, str):
                raise ModelProtocolError("chat message content must be text")
            if content:
                output.append(MessageItem(role="assistant", text=content))

        tool_calls = message.get("tool_calls") or []
        if not isinstance(tool_calls, list):
            raise ModelProtocolError("chat tool_calls must be a list")
        for raw_tool_call in tool_calls:
            tool_call = _as_dict(raw_tool_call, context="chat tool call")
            if tool_call.get("type") != "function":
                raise ModelProtocolError(
                    f"unsupported chat tool call type: {tool_call.get('type')!r}"
                )
            function = _as_dict(
                tool_call.get("function"),
                context="chat tool call function",
            )
            try:
                output.append(
                    FunctionCallItem(
                        call_id=str(tool_call["id"]),
                        name=str(function["name"]),
                        arguments_json=str(function["arguments"]),
                    )
                )
            except KeyError as exc:
                raise ModelProtocolError(
                    f"chat tool call is missing {exc.args[0]}"
                ) from exc
            except (TypeError, ValueError) as exc:
                raise ModelProtocolError(
                    f"malformed chat tool call for {function.get('name')!r}: {exc}"
                ) from exc

        return ModelTurnResult(
            items=tuple(output),
            response_id=(
                str(response["id"]) if response.get("id") is not None else None
            ),
            usage=_usage_from_chat(response.get("usage")),
        )


class OpenAIResponsesModelTurnAdapter:
    """OpenAI Responses through LiteLLM with full supported-item replay."""

    adapter_id = "openai_responses"

    def __init__(
        self,
        *,
        transport: Transport | None = None,
        context_cache: bool = False,
        system_prompt_boundary: str | None = None,
    ) -> None:
        self._transport = transport or _default_responses_transport
        # Responses uses provider-managed prompt caching. Shared cache settings
        # must not introduce Anthropic's cache_control content-block extension.

    def turn(self, request: ModelTurnRequest) -> ModelTurnResult:
        input_items = [
            wire_item
            for item in request.items
            if (wire_item := _item_to_responses_input(item)) is not None
        ]
        wire_request: dict[str, Any] = {
            "model": request.model,
            "input": input_items,
            "include": ["reasoning.encrypted_content"],
            "custom_llm_provider": "openai",
        }
        if request.instructions is not None:
            wire_request["instructions"] = request.instructions
        if request.tools:
            wire_request["tools"] = [
                _tool_definition_for_responses(tool) for tool in request.tools
            ]
        wire_request.update(_validated_options(self.adapter_id, request.options))
        raw_response = self._transport(**wire_request)
        response = _as_dict(raw_response, context="responses response")
        return ModelTurnResult(
            items=_items_from_responses(response.get("output")),
            response_id=(
                str(response["id"]) if response.get("id") is not None else None
            ),
            usage=_usage_from_responses(response.get("usage")),
        )


def _assistant_message_from_anthropic_items(
    items: list[MessageItem | FunctionCallItem | ReasoningItem],
) -> dict[str, Any]:
    text: list[str] = []
    thinking_blocks: list[dict[str, Any]] = []
    tool_calls: list[dict[str, Any]] = []
    for item in items:
        if isinstance(item, MessageItem):
            text.append(item.text)
        elif isinstance(item, ReasoningItem):
            if not item.replay_payload:
                raise ModelProtocolError(
                    "anthropic_messages reasoning replay requires the original replay_payload"
                )
            block = dict(item.replay_payload)
            if block.get("type") not in {"thinking", "redacted_thinking"}:
                raise ModelProtocolError(
                    f"unsupported anthropic reasoning block: {block.get('type')!r}"
                )
            thinking_blocks.append(block)
        else:
            tool_calls.append(
                {
                    "id": item.call_id,
                    "type": "function",
                    "function": {
                        "name": item.name,
                        "arguments": item.arguments_json,
                    },
                }
            )
    message: dict[str, Any] = {
        "role": "assistant",
        "content": "".join(text) or None,
    }
    if thinking_blocks:
        message["thinking_blocks"] = thinking_blocks
    if tool_calls:
        message["tool_calls"] = tool_calls
    return message


def _items_to_anthropic_messages(
    items: tuple[ModelItem, ...],
    *,
    instructions: str | None,
) -> list[dict[str, Any]]:
    messages: list[dict[str, Any]] = []
    if instructions is not None:
        messages.append({"role": "system", "content": instructions})

    assistant_items: list[MessageItem | FunctionCallItem | ReasoningItem] = []

    def flush_assistant_items() -> None:
        if assistant_items:
            messages.append(_assistant_message_from_anthropic_items(assistant_items))
            assistant_items.clear()

    for item in items:
        if isinstance(item, MessageItem) and item.role == "assistant":
            assistant_items.append(item)
        elif isinstance(item, FunctionCallItem):
            assistant_items.append(item)
        elif isinstance(item, ReasoningItem):
            if item.replay_payload.get("type") in {
                "thinking",
                "redacted_thinking",
            }:
                assistant_items.append(item)
                continue
            raise ModelProtocolError(
                "anthropic_messages cannot replay reasoning from another protocol"
            )
        else:
            flush_assistant_items()
            if isinstance(item, MessageItem):
                messages.append({"role": item.role, "content": item.text})
            elif isinstance(item, FunctionCallOutputItem):
                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": item.call_id,
                        "content": item.output,
                    }
                )
            else:
                raise ModelProtocolError(
                    f"anthropic_messages cannot replay canonical item {type(item).__name__}"
                )
    flush_assistant_items()
    return messages


def _items_from_anthropic_message(raw_message: Any) -> tuple[ModelItem, ...]:
    message = _as_dict(raw_message, context="anthropic message")
    result: list[ModelItem] = []

    thinking_blocks = message.get("thinking_blocks") or []
    if not isinstance(thinking_blocks, list):
        raise ModelProtocolError("anthropic thinking_blocks must be a list")
    for raw_block in thinking_blocks:
        block = _as_dict(raw_block, context="anthropic thinking block")
        block_type = block.get("type")
        if block_type == "thinking":
            thinking = block.get("thinking")
            if not isinstance(thinking, str):
                raise ModelProtocolError("anthropic thinking block must contain text")
            result.append(ReasoningItem(text=thinking, replay_payload=block))
        elif block_type == "redacted_thinking":
            if not isinstance(block.get("data"), str):
                raise ModelProtocolError(
                    "anthropic redacted thinking block must contain data"
                )
            result.append(ReasoningItem(replay_payload=block))
        else:
            raise ModelProtocolError(
                f"unsupported anthropic thinking block: {block_type!r}"
            )

    content = message.get("content")
    if content is not None:
        if not isinstance(content, str):
            raise ModelProtocolError("anthropic message content must be text")
        if content:
            result.append(MessageItem(role="assistant", text=content))

    tool_calls = message.get("tool_calls") or []
    if not isinstance(tool_calls, list):
        raise ModelProtocolError("anthropic tool_calls must be a list")
    for raw_tool_call in tool_calls:
        tool_call = _as_dict(raw_tool_call, context="anthropic tool call")
        if tool_call.get("type") != "function":
            raise ModelProtocolError(
                f"unsupported anthropic tool call type: {tool_call.get('type')!r}"
            )
        function = _as_dict(
            tool_call.get("function"),
            context="anthropic tool call function",
        )
        try:
            result.append(
                FunctionCallItem(
                    call_id=str(tool_call["id"]),
                    name=str(function["name"]),
                    arguments_json=str(function["arguments"]),
                )
            )
        except KeyError as exc:
            raise ModelProtocolError(
                f"anthropic tool call is missing {exc.args[0]}"
            ) from exc
        except (TypeError, ValueError) as exc:
            raise ModelProtocolError(
                f"malformed anthropic tool call for {function.get('name')!r}: {exc}"
            ) from exc
    return tuple(result)


class AnthropicMessagesModelTurnAdapter:
    """Anthropic Messages projected through LiteLLM's chat contract."""

    adapter_id = "anthropic_messages"

    def __init__(
        self,
        *,
        transport: Transport | None = None,
        context_cache: bool = False,
        system_prompt_boundary: str | None = None,
    ) -> None:
        self._transport = transport or _default_completion_transport
        self._context_cache = context_cache
        self._system_prompt_boundary = system_prompt_boundary

    def turn(self, request: ModelTurnRequest) -> ModelTurnResult:
        messages = _items_to_anthropic_messages(
            request.items,
            instructions=request.instructions,
        )
        _apply_system_prompt_cache(
            messages,
            enabled=self._context_cache,
            boundary=self._system_prompt_boundary,
            block_type="text",
        )
        wire_request: dict[str, Any] = {
            "model": request.model,
            "messages": messages,
            "custom_llm_provider": "anthropic",
        }
        if request.tools:
            wire_request["tools"] = [
                _tool_definition_for_chat(tool) for tool in request.tools
            ]
        wire_request.update(_validated_options(self.adapter_id, request.options))
        raw_response = self._transport(**wire_request)
        response = _as_dict(raw_response, context="anthropic response")
        choices = response.get("choices")
        if not isinstance(choices, list) or len(choices) != 1:
            raise ModelProtocolError(
                "anthropic response must contain exactly one choice"
            )
        choice = _as_dict(choices[0], context="anthropic choice")
        return ModelTurnResult(
            items=_items_from_anthropic_message(choice.get("message")),
            response_id=(
                str(response["id"]) if response.get("id") is not None else None
            ),
            usage=_usage_from_chat(response.get("usage")),
        )


_ADAPTER_TYPES: dict[AdapterKind, type[ModelTurnAdapter]] = {
    "openai_chat": OpenAIChatModelTurnAdapter,
    "openai_responses": OpenAIResponsesModelTurnAdapter,
    "anthropic_messages": AnthropicMessagesModelTurnAdapter,
}
assert set(_ADAPTER_TYPES) == set(MODEL_ADAPTERS)


def create_model_turn_adapter(
    adapter_id: str,
    *,
    transport: Transport | None = None,
    context_cache: bool = False,
    system_prompt_boundary: str | None = None,
) -> ModelTurnAdapter:
    """Create one explicitly selected model protocol adapter."""

    adapter_type = _ADAPTER_TYPES.get(adapter_id)
    if adapter_type is None:
        raise ValueError(
            f"unknown model adapter {adapter_id!r}; "
            f"expected {', '.join(MODEL_ADAPTERS)}"
        )
    return adapter_type(
        transport=transport,
        context_cache=context_cache,
        system_prompt_boundary=system_prompt_boundary,
    )
