"""smolagents message projections for canonical Tool calls and results."""

from __future__ import annotations

import json
from typing import Any

from agentloom.runtime.model_protocol import (
    MODEL_ITEMS_RAW_KEY,
    MODEL_RESPONSE_ID_RAW_KEY,
)
from agentloom.runtime.tool_protocol import (
    TOOL_CALL_RAW_KEY,
    TOOL_RESULT_RAW_KEY,
    ToolCallRecord,
)
from smolagents.memory import ActionStep
from smolagents.models import ChatMessage, MessageRole


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
