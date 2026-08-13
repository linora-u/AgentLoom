"""Canonical Tool call state and model-message projection."""

from __future__ import annotations

import json
import time
import uuid
from collections.abc import Callable
from copy import deepcopy
from dataclasses import asdict, dataclass, field
from functools import wraps
from typing import Any, Literal

from smolagents import validate_tool_arguments
from smolagents.memory import ActionStep
from smolagents.models import ChatMessage, MessageRole
from smolagents.tools import handle_agent_input_types

TOOL_CALL_RAW_KEY = "agentloom_tool_call"
TOOL_RESULT_RAW_KEY = "agentloom_tool_result"
TOOL_SETTLER_ATTR = "_agentloom_settle_tool_call"

ToolCallStatus = Literal[
    "completed",
    "error",
    "blocked",
]


class ToolPolicyBlockedError(ValueError):
    """A Tool rejected a request before executing its requested side effect."""

    blocked = True
    kind = "policy_blocked"
    stage = "security_policy"
    retryable = False


@dataclass(frozen=True, slots=True)
class ToolErrorRecord:
    kind: str
    message: str
    retryable: bool
    stage: str


@dataclass(frozen=True, slots=True)
class ToolCallRecord:
    """The canonical terminal state of one Tool invocation.

    Hook tracing, Agent memory, checkpoints, and model/provider projection all
    consume this value. ``exception`` is retained only so direct Python callers
    can observe the original Tool exception; it never crosses persistence or
    model boundaries.
    """

    call_id: str
    tool_name: str
    input: Any
    status: ToolCallStatus
    output: Any = None
    error: ToolErrorRecord | None = None
    metadata: dict[str, Any] = field(default_factory=dict)
    started_at: float | None = None
    ended_at: float | None = None
    exception: Exception | None = field(default=None, repr=False, compare=False)

    def __post_init__(self) -> None:
        if self.status not in {"completed", "error", "blocked"}:
            raise ValueError(f"ToolCallRecord requires a terminal status, got {self.status!r}")
        if self.status == "completed":
            if self.error is not None or self.exception is not None:
                raise ValueError("A completed Tool record cannot contain an error")
        else:
            if self.error is None:
                raise ValueError(f"A {self.status} Tool record requires an error")
            if self.output is not None:
                raise ValueError(f"A {self.status} Tool record cannot contain output")
        if self.status == "blocked" and self.error is not None and self.error.retryable:
            raise ValueError("A blocked Tool record cannot be retryable without changed input or policy")
        object.__setattr__(self, "input", deepcopy(self.input))
        object.__setattr__(self, "metadata", deepcopy(self.metadata))

    @classmethod
    def completed(
        cls,
        *,
        call_id: str,
        tool_name: str,
        input: Any,
        output: Any,
        started_at: float | None = None,
        ended_at: float | None = None,
    ) -> ToolCallRecord:
        return cls(
            call_id=call_id,
            tool_name=tool_name,
            input=input,
            status="completed",
            output=output,
            started_at=started_at,
            ended_at=ended_at,
        )

    @classmethod
    def blocked(
        cls,
        *,
        call_id: str,
        tool_name: str,
        input: Any,
        message: str,
        stage: str,
        kind: str = "policy_blocked",
        started_at: float | None = None,
        ended_at: float | None = None,
    ) -> ToolCallRecord:
        return cls(
            call_id=call_id,
            tool_name=tool_name,
            input=input,
            status="blocked",
            error=ToolErrorRecord(
                kind=kind,
                message=message or "Action blocked by Tool Runtime policy",
                retryable=False,
                stage=stage,
            ),
            started_at=started_at,
            ended_at=ended_at,
        )

    @classmethod
    def failed(
        cls,
        *,
        call_id: str,
        tool_name: str,
        input: Any,
        error: Exception,
        stage: str = "tool_execution",
        started_at: float | None = None,
        ended_at: float | None = None,
    ) -> ToolCallRecord:
        cause = error.__cause__ if isinstance(error.__cause__, Exception) else error
        semantic_error = (
            error
            if any(hasattr(error, name) for name in ("kind", "stage", "retryable", "blocked"))
            else cause
        )
        return cls(
            call_id=call_id,
            tool_name=tool_name,
            input=input,
            status="error",
            error=ToolErrorRecord(
                kind=str(getattr(semantic_error, "kind", "execution_error")),
                message=str(cause) or type(cause).__name__,
                retryable=bool(getattr(semantic_error, "retryable", False)),
                stage=str(getattr(semantic_error, "stage", stage)),
            ),
            started_at=started_at,
            ended_at=ended_at,
            exception=cause,
        )

    @classmethod
    def from_exception(
        cls,
        *,
        call_id: str,
        tool_name: str,
        input: Any,
        error: Exception,
        stage: str = "tool_execution",
        started_at: float | None = None,
        ended_at: float | None = None,
    ) -> ToolCallRecord:
        """Classify a typed Tool exception into the canonical terminal record."""

        cause = error.__cause__ if isinstance(error.__cause__, Exception) else error
        semantic_error = (
            error
            if any(hasattr(error, name) for name in ("kind", "stage", "retryable", "blocked"))
            else cause
        )
        if bool(getattr(semantic_error, "blocked", False)):
            return cls.blocked(
                call_id=call_id,
                tool_name=tool_name,
                input=input,
                message=str(cause) or type(cause).__name__,
                kind=str(getattr(semantic_error, "kind", "policy_blocked")),
                stage=str(getattr(semantic_error, "stage", stage)),
                started_at=started_at,
                ended_at=ended_at,
            )
        return cls.failed(
            call_id=call_id,
            tool_name=tool_name,
            input=input,
            error=error,
            stage=stage,
            started_at=started_at,
            ended_at=ended_at,
        )

    @property
    def outcome(self) -> Literal["executed", "blocked", "failed"]:
        if self.status == "completed":
            return "executed"
        if self.status == "blocked":
            return "blocked"
        return "failed"

    @property
    def tool_input(self) -> Any:
        return self.input

    @property
    def stage(self) -> str:
        return self.error.stage if self.error is not None else ""

    @property
    def reason(self) -> str:
        return self.error.message if self.error is not None else ""

    @property
    def value(self) -> Any:
        return self.output

    def to_dict(self) -> dict[str, Any]:
        return {
            "call_id": self.call_id,
            "tool_name": self.tool_name,
            "input": deepcopy(self.input),
            "status": self.status,
            "output": self.output,
            "error": asdict(self.error) if self.error is not None else None,
            "metadata": deepcopy(self.metadata),
            "started_at": self.started_at,
            "ended_at": self.ended_at,
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> ToolCallRecord:
        status = raw.get("status")
        output = raw.get("output")
        error_raw = raw.get("error")
        error = ToolErrorRecord(**error_raw) if isinstance(error_raw, dict) else None
        if status in {"pending", "running", "cancelled"}:
            # Old checkpoints could persist an in-flight record. Normalize it
            # explicitly at the persistence edge; the core record stays terminal.
            status = "error"
            error = ToolErrorRecord(
                kind="interrupted",
                message="Tool execution was interrupted before a terminal record was persisted.",
                retryable=True,
                stage="tool_execution",
            )
            output = None
        if status not in {"completed", "error", "blocked"}:
            raise ValueError(f"Unsupported Tool terminal status in checkpoint: {status!r}")
        return cls(
            call_id=str(raw.get("call_id", "")),
            tool_name=str(raw.get("tool_name", "")),
            input=raw.get("input"),
            status=status,
            output=output,
            error=error,
            metadata=dict(raw.get("metadata") or {}),
            started_at=raw.get("started_at"),
            ended_at=raw.get("ended_at"),
        )

    def with_output(self, output: Any) -> ToolCallRecord:
        if self.status != "completed":
            raise ValueError("Only a completed Tool record has model-visible output")
        return ToolCallRecord(
            call_id=self.call_id,
            tool_name=self.tool_name,
            input=self.input,
            status=self.status,
            output=output,
            error=self.error,
            metadata=self.metadata,
            started_at=self.started_at,
            ended_at=self.ended_at,
            exception=self.exception,
        )

    def direct_result(self) -> Any:
        """Project the terminal record to the ordinary Python Tool interface."""

        if self.status == "completed":
            return self.output
        if self.status == "blocked":
            return self.reason
        if self.exception is not None:
            raise self.exception
        raise RuntimeError(self.reason or "Tool execution failed")

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


class Executed(ToolCallRecord):
    """Compatibility name for a completed canonical Tool record."""

    __slots__ = ()

    def __init__(
        self,
        tool_input: dict[str, Any],
        value: Any,
        tool_name: str = "",
        *,
        call_id: str = "",
        started_at: float | None = None,
        ended_at: float | None = None,
    ) -> None:
        super().__init__(
            call_id=call_id,
            tool_name=tool_name,
            input=tool_input,
            status="completed",
            output=value,
            started_at=started_at,
            ended_at=ended_at,
        )


class Blocked(ToolCallRecord):
    """Compatibility name for a blocked canonical Tool record."""

    __slots__ = ()

    def __init__(
        self,
        tool_input: dict[str, Any],
        reason: str,
        stage: str,
        tool_name: str = "",
        *,
        call_id: str = "",
        kind: str = "policy_blocked",
        started_at: float | None = None,
        ended_at: float | None = None,
    ) -> None:
        super().__init__(
            call_id=call_id,
            tool_name=tool_name,
            input=tool_input,
            status="blocked",
            error=ToolErrorRecord(
                kind=kind,
                message=reason or "Action blocked by Tool Runtime policy",
                retryable=False,
                stage=stage,
            ),
            started_at=started_at,
            ended_at=ended_at,
        )

    def model_response(self) -> str:
        return self.reason


class Failed(ToolCallRecord):
    """Compatibility name for a failed canonical Tool record."""

    __slots__ = ()

    def __init__(
        self,
        tool_input: dict[str, Any],
        error: Exception,
        stage: str,
        tool_name: str = "",
        *,
        call_id: str = "",
        started_at: float | None = None,
        ended_at: float | None = None,
    ) -> None:
        cause = error.__cause__ if isinstance(error.__cause__, Exception) else error
        semantic_error = (
            error
            if any(hasattr(error, name) for name in ("kind", "stage", "retryable"))
            else cause
        )
        super().__init__(
            call_id=call_id,
            tool_name=tool_name,
            input=tool_input,
            status="error",
            error=ToolErrorRecord(
                kind=str(getattr(semantic_error, "kind", "execution_error")),
                message=str(cause) or type(cause).__name__,
                retryable=bool(getattr(semantic_error, "retryable", False)),
                stage=str(getattr(semantic_error, "stage", stage)),
            ),
            started_at=started_at,
            ended_at=ended_at,
            exception=cause,
        )


type ToolExecutionOutcome = ToolCallRecord


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
