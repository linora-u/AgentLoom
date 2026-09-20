"""Pi-specific JSONL protocol v1, independent of the AgentRuntime interface.

Only framing and value validation live here. Ticket 07 owns transport, request
correlation, liveness and capability negotiation; tickets 09/10 own tool wiring.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import fields, is_dataclass
from typing import Annotated, Any, Literal, Union

from agentloom.runtime.agent_runtime import RuntimeCapabilities, RuntimeCheckpointEnvelope, RuntimeState
from agentloom.runtime.native_tools import (
    NativeAuthorization,
    NativeCallIdentity,
    NativeExecutionOutcome,
    NativePrepareRequest,
    ToolManifestEntry,
)
from agentloom.runtime.tool_protocol import ToolCallRecord, ToolErrorRecord
from pydantic import BaseModel, ConfigDict, Field, JsonValue, TypeAdapter, model_validator

PI_BRIDGE_PROTOCOL_VERSION = 1
NonEmpty = Annotated[str, Field(min_length=1)]
Method = Literal["handshake", "run", "snapshot", "cancel", "close", "tool_prepare", "tool_settle", "tool_dispatch", "platform_invoke", "model_prepare"]


class WireValue(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True, hide_input_in_errors=True, allow_inf_nan=False)


class Handshake(WireValue):
    method: Literal["handshake"]
    native_tool_contract: Literal[1]


class ModelSelection(WireValue):
    model_type: NonEmpty
    model_id: NonEmpty
    protocol: NonEmpty
    settings: dict[str, JsonValue] = Field(repr=False)
    request_headers: dict[str, str] = Field(repr=False)


class Run(WireValue):
    method: Literal["run"]
    application_id: NonEmpty
    task_id: NonEmpty
    task: NonEmpty
    cwd: NonEmpty
    instructions: str
    model: ModelSelection = Field(repr=False)
    tools: list[ToolManifestEntry]
    serial_tools: list[NonEmpty] = Field(default_factory=list)
    runtime_options: dict[str, JsonValue] = Field(repr=False)
    continue_session: bool = False
    record_task: bool = True
    additional_args: dict[str, JsonValue] = Field(default_factory=dict, repr=False)
    checkpoint: RuntimeCheckpointEnvelope | None = Field(default=None, repr=False)


class Snapshot(WireValue):
    method: Literal["snapshot"]


class Cancel(WireValue):
    method: Literal["cancel"]
    target_request_id: NonEmpty


class Close(WireValue):
    method: Literal["close"]


class Prepare(WireValue):
    method: Literal["tool_prepare"]
    call: NativePrepareRequest = Field(repr=False)


class Dispatch(WireValue):
    method: Literal["tool_dispatch"]
    authorization: NativeAuthorization = Field(repr=False)


class CaptureFile(WireValue):
    sha256: Annotated[str, Field(pattern="^[0-9a-f]{64}$")]


class Settle(WireValue):
    method: Literal["tool_settle"]
    outcome: NativeExecutionOutcome = Field(repr=False)
    capture: CaptureFile


class ModelPrepare(WireValue):
    method: Literal["model_prepare"]
    identity: NativeCallIdentity


class PlatformInvoke(WireValue):
    method: Literal["platform_invoke"]
    identity: NativeCallIdentity
    tool_name: NonEmpty
    arguments: dict[str, JsonValue] = Field(repr=False)


RequestPayload = Annotated[
    Union[Handshake, Run, Snapshot, Cancel, Close, Prepare, Dispatch, Settle, PlatformInvoke, ModelPrepare], Field(discriminator="method")
]


class BridgeError(WireValue):
    category: Literal[
        "protocol", "configuration", "unsupported_capability", "provider", "tool", "interrupted", "internal"
    ]
    message: str
    retryable: bool = False


class TerminalRecord(WireValue):
    call_id: NonEmpty
    tool_name: NonEmpty
    input: JsonValue = Field(repr=False)
    status: Literal["completed", "error", "blocked"]
    output: JsonValue = Field(default=None, repr=False)
    error: ToolErrorRecord | None = None
    metadata: dict[str, JsonValue] = Field(default_factory=dict)
    started_at: float | None = None
    ended_at: float | None = None

    @model_validator(mode="after")
    def canonical_terminal(self):
        ToolCallRecord.from_dict(self.model_dump())
        return self


class HandshakeResult(WireValue):
    method: Literal["handshake"]
    runtime_id: Literal["pi"]
    sdk_version: NonEmpty
    node_version: NonEmpty
    native_tool_contract: Literal[1]
    capabilities: RuntimeCapabilities


class RunResult(WireValue):
    method: Literal["run"]
    state: RuntimeState
    output: JsonValue = Field(default=None, repr=False)
    usage: dict[str, JsonValue] = Field(default_factory=dict)
    artifacts: list[dict[str, JsonValue]] = Field(default_factory=list)
    checkpoint: RuntimeCheckpointEnvelope | None = Field(default=None, repr=False)
    error: BridgeError | None = None

    @model_validator(mode="after")
    def terminal_state(self):
        if self.state == "failed" and self.error is None:
            raise ValueError("Failed run requires an error")
        if self.state == "success" and self.error is not None:
            raise ValueError("Successful run cannot contain an error")
        return self


class SnapshotResult(WireValue):
    method: Literal["snapshot"]
    checkpoint: RuntimeCheckpointEnvelope | None = Field(repr=False)


class ControlResult(WireValue):
    method: Literal["cancel", "close"]
    accepted: bool


class PrepareResult(WireValue):
    method: Literal["tool_prepare", "tool_dispatch"]
    authorization: NativeAuthorization | None = Field(default=None, repr=False)
    rejection: TerminalRecord | None = None

    @model_validator(mode="after")
    def exclusive_result(self):
        if (self.authorization is None) == (self.rejection is None):
            raise ValueError("Preparation must authorize or reject")
        if self.rejection is not None and self.rejection.status == "completed":
            raise ValueError("Preparation cannot execute a tool")
        return self


class SettleResult(WireValue):
    method: Literal["tool_settle"]
    identity: NativeCallIdentity
    authorization_id: NonEmpty
    state: Literal["committed", "uncertain"]
    commit_id: NonEmpty | None = None
    record: TerminalRecord | None = Field(default=None, repr=False)

    @model_validator(mode="after")
    def durable_result(self):
        if self.state == "committed":
            if self.commit_id is None or self.record is None:
                raise ValueError("Settlement requires persistent commit acknowledgement")
            if self.record.call_id != self.identity.call_id:
                raise ValueError("Settlement call identity mismatch")
        elif self.commit_id is not None or self.record is not None:
            raise ValueError("Uncertain execution has no terminal result")
        return self


class ModelPermit(WireValue):
    method: Literal["model_prepare"]
    identity: NativeCallIdentity
    state: Literal["work", "final", "denied"]
    agent_context: list[str] = Field(default_factory=list, repr=False)


class PlatformResult(WireValue):
    method: Literal["platform_invoke"]
    record: TerminalRecord = Field(repr=False)


ResultPayload = Annotated[
    Union[HandshakeResult, RunResult, SnapshotResult, ControlResult, PrepareResult, SettleResult, PlatformResult, ModelPermit],
    Field(discriminator="method"),
]


class Envelope(WireValue):
    version: Literal[2]
    instance_id: NonEmpty


class Request(Envelope):
    run_id: NonEmpty | None = None
    kind: Literal["request"]
    request_id: NonEmpty
    payload: RequestPayload = Field(repr=False)

    @model_validator(mode="after")
    def run_identity(self):
        if self.payload.method not in {"handshake", "close"} and self.run_id is None:
            raise ValueError("Run-scoped request requires run_id")
        identity = (
            self.payload.call.identity
            if isinstance(self.payload, Prepare)
            else self.payload.authorization.identity
            if isinstance(self.payload, Dispatch)
            else self.payload.outcome.identity
            if isinstance(self.payload, Settle)
            else self.payload.identity
            if isinstance(self.payload, (PlatformInvoke, ModelPrepare))
            else None
        )
        if identity is not None and (identity.run_id != self.run_id or identity.instance_id != self.instance_id):
            raise ValueError("Tool callback identity mismatch")
        return self


class Response(Envelope):
    run_id: NonEmpty | None = None
    kind: Literal["response"]
    request_id: NonEmpty
    payload: ResultPayload | None = Field(default=None, repr=False)
    error: BridgeError | None = None

    @model_validator(mode="after")
    def exclusive_result(self):
        if (self.payload is None) == (self.error is None):
            raise ValueError("Response requires exactly one result or error")
        if self.payload is not None:
            if self.payload.method not in {"handshake", "close"} and self.run_id is None:
                raise ValueError("Run-scoped response requires run_id")
            identity = (
                self.payload.identity
                if isinstance(self.payload, (SettleResult, ModelPermit))
                else self.payload.authorization.identity
                if isinstance(self.payload, PrepareResult) and self.payload.authorization is not None
                else None
            )
            if identity is not None and (identity.run_id != self.run_id or identity.instance_id != self.instance_id):
                raise ValueError("Tool response identity mismatch")
        return self


class Event(Envelope):
    kind: Literal["event"]
    request_id: NonEmpty
    run_id: NonEmpty
    sequence: Annotated[int, Field(ge=1)]
    event: Literal["run", "model", "tool", "subagent", "usage", "checkpoint", "terminal"]
    payload: dict[str, JsonValue] = Field(repr=False)


Message = Annotated[Union[Request, Response, Event], Field(discriminator="kind")]
_message: TypeAdapter[Request | Response | Event] = TypeAdapter(Message)


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate JSON key")
        result[key] = value
    return result


def decode_message(line: str) -> Request | Response | Event:
    """Reject invalid frames without including private payloads in diagnostics."""
    try:
        raw = json.loads(line, object_pairs_hook=_unique_object)
        if not isinstance(raw, dict) or type(raw.get("version")) is not int:
            raise ValueError("Invalid version")
        payload = raw.get("payload")
        if isinstance(payload, dict) and "native_tool_contract" in payload:
            if type(payload["native_tool_contract"]) is not int:
                raise ValueError("Invalid native tool contract version")
        # Reject non-finite numbers even in open-ended JSON payload fields.
        json.dumps(raw, allow_nan=False)
        return _message.validate_json(line)
    except ValueError:
        raise ValueError("Invalid Pi bridge message") from None


def encode_message(message: Request | Response | Event) -> str:
    def wire(value: Any) -> Any:
        if isinstance(value, BaseModel):
            return {name: wire(getattr(value, name)) for name in type(value).model_fields}
        if is_dataclass(value) and not isinstance(value, type):
            return {item.name: wire(getattr(value, item.name)) for item in fields(value)}
        if isinstance(value, Mapping):
            return {key: wire(item) for key, item in value.items()}
        if isinstance(value, (tuple, list)):
            return [wire(item) for item in value]
        return value

    encoded = json.dumps(wire(message), allow_nan=False, separators=(",", ":"))
    decode_message(encoded)
    return encoded + "\n"


def protocol_schema() -> dict:
    return _message.json_schema()
