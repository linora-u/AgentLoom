"""smolagents Tool execution and provider message projections."""

from __future__ import annotations

import json
import time
import uuid
from collections.abc import Callable
from functools import wraps
from typing import Any

from agentloom.runtime.model_protocol import (
    MODEL_ITEMS_RAW_KEY,
    MODEL_RESPONSE_ID_RAW_KEY,
)
from agentloom.runtime.tool_protocol import (
    TOOL_CALL_RAW_KEY,
    TOOL_RESULT_RAW_KEY,
    TOOL_SETTLER_ATTR,
    ToolCallRecord,
)
from smolagents import validate_tool_arguments
from smolagents.memory import ActionStep
from smolagents.models import ChatMessage, MessageRole
from smolagents.tools import handle_agent_input_types


def settle_tool_call(
    tool: Any,
    arguments: dict[str, Any] | Any,
    *,
    call_id: str | None = None,
    sanitize_inputs_outputs: bool = False,
) -> ToolCallRecord:
    """Execute one Tool and return its only canonical terminal state."""

    stable_call_id = call_id or uuid.uuid4().hex
    tool_name = str(getattr(tool, "name", type(tool).__name__))
    started_at = time.time()
    settler = getattr(tool, TOOL_SETTLER_ATTR, None)
    if callable(settler):
        normalized_arguments = arguments
        if sanitize_inputs_outputs:
            if isinstance(arguments, dict):
                _, normalized_arguments = handle_agent_input_types(**arguments)
            else:
                normalized_args, _ = handle_agent_input_types(arguments)
                normalized_arguments = normalized_args[0]
        return settler(
            normalized_arguments,
            call_id=stable_call_id,
            sanitize_inputs_outputs=sanitize_inputs_outputs,
            started_at=started_at,
        )

    if isinstance(arguments, dict):
        try:
            validate_tool_arguments(tool, arguments)
        except (TypeError, ValueError) as error:
            return ToolCallRecord.blocked(
                call_id=stable_call_id,
                tool_name=tool_name,
                input=arguments,
                message=str(error) or type(error).__name__,
                kind="invalid_arguments",
                stage="input_validation",
                started_at=started_at,
                ended_at=time.time(),
            )
        except Exception as error:
            return ToolCallRecord.failed(
                call_id=stable_call_id,
                tool_name=tool_name,
                input=arguments,
                error=error,
                stage="input_validation",
                started_at=started_at,
                ended_at=time.time(),
            )

    try:
        if isinstance(arguments, dict):
            if sanitize_inputs_outputs:
                output = tool(**arguments, sanitize_inputs_outputs=True)
            else:
                output = tool(**arguments)
        else:
            if sanitize_inputs_outputs:
                output = tool(arguments, sanitize_inputs_outputs=True)
            else:
                output = tool(arguments)
    except Exception as error:
        return ToolCallRecord.from_exception(
            call_id=stable_call_id,
            tool_name=tool_name,
            input=arguments,
            error=error,
            started_at=started_at,
            ended_at=time.time(),
        )
    return ToolCallRecord.completed(
        call_id=stable_call_id,
        tool_name=tool_name,
        input=arguments,
        output=output,
        started_at=started_at,
        ended_at=time.time(),
    )

def _serialized_tool_record_is_error(message: dict[str, Any]) -> bool:
    """Decode the canonical wire record after OpenAI's Tool shape loses status."""

    if message.get("role") != "tool":
        return False
    content = message.get("content")
    if isinstance(content, list):
        content = "\n".join(
            str(item.get("text", ""))
            for item in content
            if isinstance(item, dict) and item.get("type") == "text"
        )
    if not isinstance(content, str):
        return False
    try:
        payload = json.loads(content)
    except (TypeError, ValueError):
        return False
    return isinstance(payload, dict) and payload.get("status") in {"error", "blocked"}


def _patch_provider_projection(
    original: Callable[[dict[str, Any]], dict[str, Any]],
    *,
    provider: str,
) -> Callable[[dict[str, Any]], dict[str, Any]]:
    @wraps(original)
    def project(message: dict[str, Any]) -> dict[str, Any]:
        result = original(message)
        if not _serialized_tool_record_is_error(message):
            return result
        if provider == "anthropic":
            result["is_error"] = True
        elif provider == "bedrock":
            tool_result = result.get("toolResult")
            if isinstance(tool_result, dict):
                tool_result["status"] = "error"
        return result

    project._agentloom_tool_error_patched = True  # type: ignore[attr-defined]
    return project


def patch_litellm_tool_error_projection() -> None:
    """Preserve canonical Tool error state in Anthropic/Bedrock projection."""

    from litellm.litellm_core_utils.prompt_templates import factory

    anthropic: Any = factory.convert_to_anthropic_tool_result
    if not getattr(anthropic, "_agentloom_tool_error_patched", False):
        factory.convert_to_anthropic_tool_result = _patch_provider_projection(
            anthropic,
            provider="anthropic",
        )

    bedrock: Any = factory._convert_to_bedrock_tool_call_result
    if not getattr(bedrock, "_agentloom_tool_error_patched", False):
        factory._convert_to_bedrock_tool_call_result = _patch_provider_projection(
            bedrock,
            provider="bedrock",
        )


def action_step_to_protocol_messages(
    step: ActionStep,
    *,
    summary_mode: bool = False,
) -> list[ChatMessage]:
    """Render a canonical action step without losing Tool call/result identity."""

    records: list[ToolCallRecord] | None = getattr(step, "tool_results", None)
    if not records:
        return step.to_messages(summary_mode=summary_mode)

    messages: list[ChatMessage] = []
    tool_calls = list(step.model_output_message.tool_calls or []) if step.model_output_message else []
    if not tool_calls and step.tool_calls:
        from smolagents.models import ChatMessageToolCall, ChatMessageToolCallFunction

        tool_calls = [
            ChatMessageToolCall(
                id=call.id,
                type="function",
                function=ChatMessageToolCallFunction(name=call.name, arguments=call.arguments),
            )
            for call in step.tool_calls
        ]

    call_content = "" if summary_mode else (step.model_output or "")
    model_raw = (
        step.model_output_message.raw
        if step.model_output_message is not None
        and isinstance(step.model_output_message.raw, dict)
        else {}
    )
    call_raw = {TOOL_CALL_RAW_KEY: True}
    for key in (MODEL_ITEMS_RAW_KEY, MODEL_RESPONSE_ID_RAW_KEY):
        if key in model_raw:
            call_raw[key] = model_raw[key]
    messages.append(
        ChatMessage(
            role=MessageRole.TOOL_CALL,
            content=call_content,
            tool_calls=tool_calls,
            raw=call_raw,
        )
    )
    for record in records:
        messages.append(
            ChatMessage(
                role=MessageRole.TOOL_RESPONSE,
                content=record.model_content(),
                raw={TOOL_RESULT_RAW_KEY: record.to_dict()},
            )
        )
    return messages


def has_native_tool_marker(message: ChatMessage | dict[str, Any]) -> bool:
    if not isinstance(message, ChatMessage) or not isinstance(message.raw, dict):
        return False
    return bool(message.raw.get(TOOL_CALL_RAW_KEY) or message.raw.get(TOOL_RESULT_RAW_KEY))


def native_tool_message_dict(message: ChatMessage) -> dict[str, Any]:
    """Project one marked internal message to the OpenAI/LiteLLM canonical shape."""

    raw = message.raw if isinstance(message.raw, dict) else {}
    if raw.get(TOOL_CALL_RAW_KEY):
        tool_calls = []
        for call in message.tool_calls or []:
            arguments = call.function.arguments
            if not isinstance(arguments, str):
                arguments = json.dumps(arguments, ensure_ascii=False, separators=(",", ":"), default=str)
            tool_calls.append(
                {
                    "id": call.id,
                    "type": call.type,
                    "function": {"name": call.function.name, "arguments": arguments},
                }
            )
        return {"role": "assistant", "content": message.content or "", "tool_calls": tool_calls}

    record = ToolCallRecord.from_dict(raw[TOOL_RESULT_RAW_KEY])
    content = message.content
    if isinstance(content, list):
        content = "\n".join(
            str(item.get("text", ""))
            for item in content
            if isinstance(item, dict) and item.get("type") == "text"
        )
    return {
        "role": "tool",
        "tool_call_id": record.call_id,
        "content": content if isinstance(content, str) else record.model_content(),
    }
