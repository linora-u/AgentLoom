"""Canonical Tool call state and model-message projection."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from typing import Any, Literal

from smolagents.memory import ActionStep
from smolagents.models import ChatMessage, MessageRole

TOOL_CALL_RAW_KEY = "agentloom_tool_call"
TOOL_RESULT_RAW_KEY = "agentloom_tool_result"

ToolCallStatus = Literal[
    "pending",
    "running",
    "completed",
    "error",
    "blocked",
    "cancelled",
]


@dataclass(frozen=True, slots=True)
class ToolErrorRecord:
    kind: str
    message: str
    retryable: bool
    stage: str


@dataclass(slots=True)
class ToolCallRecord:
    """One provider-originated Tool call and its current lifecycle state."""

    call_id: str
    tool_name: str
    input: Any
    status: ToolCallStatus = "pending"
    output: Any = None
    error: ToolErrorRecord | None = None
    metadata: dict[str, Any] = field(default_factory=dict)
    started_at: float | None = None
    ended_at: float | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> ToolCallRecord:
        error_raw = raw.get("error")
        error = ToolErrorRecord(**error_raw) if isinstance(error_raw, dict) else None
        return cls(
            call_id=str(raw.get("call_id", "")),
            tool_name=str(raw.get("tool_name", "")),
            input=raw.get("input"),
            status=raw.get("status", "pending"),
            output=raw.get("output"),
            error=error,
            metadata=dict(raw.get("metadata") or {}),
            started_at=raw.get("started_at"),
            ended_at=raw.get("ended_at"),
        )

    def model_content(self) -> str:
        if self.status == "completed":
            payload = {"ok": True, "status": self.status, "output": self.output}
        else:
            error = self.error or ToolErrorRecord(
                kind="interrupted",
                message="Tool execution did not reach a terminal result.",
                retryable=True,
                stage="tool_execution",
            )
            payload = {
                "ok": False,
                "status": self.status,
                "error": asdict(error),
            }
        return json.dumps(payload, ensure_ascii=False, separators=(",", ":"), default=str)


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
    messages.append(
        ChatMessage(
            role=MessageRole.TOOL_CALL,
            content=call_content,
            tool_calls=tool_calls,
            raw={TOOL_CALL_RAW_KEY: True},
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
