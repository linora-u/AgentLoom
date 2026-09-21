"""Provider-neutral contracts for one model turn.

The model protocol is deliberately smaller than any provider SDK.  Adapters
translate these immutable values at the wire boundary and never expose
LiteLLM or provider response classes to callers.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any, Literal, Protocol

from agentloom.configuration.model_adapters import AdapterKind as _AdapterKind

type MessageRole = Literal["system", "developer", "user", "assistant"]

MODEL_ITEMS_RAW_KEY = "agentloom_model_items"
MODEL_RESPONSE_ID_RAW_KEY = "agentloom_model_response_id"


class ModelProtocolError(RuntimeError):
    """The selected wire adapter could not satisfy its declared contract."""


def _frozen_mapping(value: Mapping[str, Any], *, field_name: str) -> Mapping[str, Any]:
    try:
        normalized = json.loads(json.dumps(dict(value), ensure_ascii=False))
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field_name} must be JSON serializable") from exc
    return MappingProxyType(normalized)


@dataclass(frozen=True, slots=True)
class MessageItem:
    """One textual conversation message."""

    role: MessageRole
    text: str
    item_id: str | None = None
    status: str | None = None
    replay_payload: Mapping[str, Any] = field(default_factory=dict, repr=False)

    def __post_init__(self) -> None:
        if not isinstance(self.text, str):
            raise TypeError("message text must be a string")
        object.__setattr__(
            self,
            "replay_payload",
            _frozen_mapping(self.replay_payload, field_name="message replay_payload"),
        )


@dataclass(frozen=True, slots=True)
class FunctionCallItem:
    """One native structured function call emitted by a model."""

    call_id: str
    name: str
    arguments_json: str
    item_id: str | None = None
    status: str | None = None
    replay_payload: Mapping[str, Any] = field(default_factory=dict, repr=False)

    def __post_init__(self) -> None:
        if not self.call_id:
            raise ValueError("function call_id must be non-empty")
        if not self.name:
            raise ValueError("function name must be non-empty")
        try:
            arguments = json.loads(self.arguments_json)
        except (TypeError, json.JSONDecodeError) as exc:
            raise ValueError("function arguments_json must contain valid JSON") from exc
        if not isinstance(arguments, dict):
            raise ValueError("function arguments_json must contain a JSON object")
        object.__setattr__(
            self,
            "replay_payload",
            _frozen_mapping(self.replay_payload, field_name="function call replay_payload"),
        )


@dataclass(frozen=True, slots=True)
class FunctionCallOutputItem:
    """The result correlated to one function call."""

    call_id: str
    output: str
    item_id: str | None = None
    status: str | None = None
    is_error: bool = False
    replay_payload: Mapping[str, Any] = field(default_factory=dict, repr=False)

    def __post_init__(self) -> None:
        if not self.call_id:
            raise ValueError("function call output call_id must be non-empty")
        if not isinstance(self.output, str):
            raise TypeError("function call output must be a string")
        object.__setattr__(
            self,
            "replay_payload",
            _frozen_mapping(
                self.replay_payload,
                field_name="function call output replay_payload",
            ),
        )


@dataclass(frozen=True, slots=True)
class ReasoningItem:
    """Replay-safe reasoning metadata that is never final-answer text."""

    text: str | None = None
    summary: tuple[str, ...] = ()
    item_id: str | None = None
    status: str | None = None
    replay_payload: Mapping[str, Any] = field(default_factory=dict, repr=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "summary", tuple(self.summary))
        object.__setattr__(
            self,
            "replay_payload",
            _frozen_mapping(self.replay_payload, field_name="reasoning replay_payload"),
        )


type ModelItem = MessageItem | FunctionCallItem | FunctionCallOutputItem | ReasoningItem


def model_item_to_dict(item: ModelItem) -> dict[str, Any]:
    """Serialize one canonical item without exposing provider SDK objects."""

    base = {
        "item_id": item.item_id,
        "status": item.status,
        "replay_payload": dict(item.replay_payload),
    }
    if isinstance(item, MessageItem):
        return {
            "type": "message",
            "role": item.role,
            "text": item.text,
            **base,
        }
    if isinstance(item, FunctionCallItem):
        return {
            "type": "function_call",
            "call_id": item.call_id,
            "name": item.name,
            "arguments_json": item.arguments_json,
            **base,
        }
    if isinstance(item, FunctionCallOutputItem):
        return {
            "type": "function_call_output",
            "call_id": item.call_id,
            "output": item.output,
            "is_error": item.is_error,
            **base,
        }
    if isinstance(item, ReasoningItem):
        return {
            "type": "reasoning",
            "text": item.text,
            "summary": list(item.summary),
            **base,
        }
    raise TypeError(f"unsupported model item: {type(item).__name__}")


def model_item_from_dict(value: Mapping[str, Any]) -> ModelItem:
    """Deserialize one canonical item and revalidate all invariants."""

    item_type = value.get("type")
    item_id = value.get("item_id")
    status = value.get("status")
    replay_payload = value.get("replay_payload") or {}
    if item_id is not None and not isinstance(item_id, str):
        raise ValueError("item_id must be a string when provided")
    if status is not None and not isinstance(status, str):
        raise ValueError("status must be a string when provided")
    if not isinstance(replay_payload, Mapping):
        raise ValueError("replay_payload must be a mapping")
    if item_type == "message":
        role = value.get("role")
        text = value.get("text")
        if role not in {"system", "developer", "user", "assistant"}:
            raise ValueError(f"unsupported message role: {role!r}")
        if not isinstance(text, str):
            raise ValueError("message text must be a string")
        return MessageItem(
            role=role,
            text=text,
            item_id=item_id,
            status=status,
            replay_payload=replay_payload,
        )
    if item_type == "function_call":
        call_id = value.get("call_id")
        name = value.get("name")
        arguments_json = value.get("arguments_json")
        if (
            not isinstance(call_id, str)
            or not isinstance(name, str)
            or not isinstance(arguments_json, str)
        ):
            raise ValueError("function call fields must be strings")
        return FunctionCallItem(
            call_id=call_id,
            name=name,
            arguments_json=arguments_json,
            item_id=item_id,
            status=status,
            replay_payload=replay_payload,
        )
    if item_type == "function_call_output":
        call_id = value.get("call_id")
        output = value.get("output")
        if not isinstance(call_id, str) or not isinstance(output, str):
            raise ValueError("function call output fields must be strings")
        return FunctionCallOutputItem(
            call_id=call_id,
            output=output,
            item_id=item_id,
            status=status,
            is_error=bool(value.get("is_error", False)),
            replay_payload=replay_payload,
        )
    if item_type == "reasoning":
        summary = value.get("summary") or ()
        if not isinstance(summary, (list, tuple)) or any(
            not isinstance(part, str) for part in summary
        ):
            raise ValueError("reasoning summary must be a list of strings")
        text = value.get("text")
        if text is not None and not isinstance(text, str):
            raise ValueError("reasoning text must be a string when provided")
        return ReasoningItem(
            text=text,
            summary=tuple(summary),
            item_id=item_id,
            status=status,
            replay_payload=replay_payload,
        )
    raise ValueError(f"unsupported model item type: {item_type!r}")


@dataclass(frozen=True, slots=True)
class ToolDefinition:
    """A provider-neutral function tool declaration."""

    name: str
    description: str
    parameters: Mapping[str, Any]
    strict: bool | None = None

    def __post_init__(self) -> None:
        if not self.name:
            raise ValueError("tool name must be non-empty")
        object.__setattr__(
            self,
            "parameters",
            _frozen_mapping(self.parameters, field_name="tool parameters"),
        )


@dataclass(frozen=True, slots=True)
class ModelUsage:
    """Normalized token accounting plus lossless JSON-safe details."""

    input_tokens: int = 0
    output_tokens: int = 0
    total_tokens: int = 0
    cached_input_tokens: int = 0
    reasoning_tokens: int = 0
    details: Mapping[str, Any] = field(default_factory=dict, repr=False)

    def __post_init__(self) -> None:
        for name in (
            "input_tokens",
            "output_tokens",
            "total_tokens",
            "cached_input_tokens",
            "reasoning_tokens",
        ):
            value = getattr(self, name)
            if isinstance(value, bool) or value < 0:
                raise ValueError(f"{name} must be a non-negative integer")
        object.__setattr__(
            self,
            "details",
            _frozen_mapping(self.details, field_name="usage details"),
        )


@dataclass(frozen=True, slots=True)
class ModelTurnRequest:
    """Input to one model invocation."""

    model: str
    items: tuple[ModelItem, ...]
    tools: tuple[ToolDefinition, ...] = ()
    instructions: str | None = None
    options: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.model:
            raise ValueError("model must be non-empty")
        object.__setattr__(self, "items", tuple(self.items))
        object.__setattr__(self, "tools", tuple(self.tools))
        object.__setattr__(
            self,
            "options",
            _frozen_mapping(self.options, field_name="model turn options"),
        )


@dataclass(frozen=True, slots=True)
class ModelTurnResult:
    """Provider-neutral output from one model invocation."""

    items: tuple[ModelItem, ...]
    response_id: str | None = None
    usage: ModelUsage = field(default_factory=ModelUsage)

    def __post_init__(self) -> None:
        object.__setattr__(self, "items", tuple(self.items))


class ModelTurnAdapter(Protocol):
    """Deep seam for exactly one model request and response."""

    adapter_id: _AdapterKind

    def turn(self, request: ModelTurnRequest) -> ModelTurnResult: ...
