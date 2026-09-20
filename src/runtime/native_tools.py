"""Version 1 native executor contracts; no executor or durable journal implementation.

The host prepares and durably settles calls. An adapter executes an authorized
operation with its own implementation. Ticket 05 supplies the production host.
"""

from __future__ import annotations

import json
from collections.abc import Iterator, Mapping
from dataclasses import dataclass, field
from typing import Any, Literal, Protocol

from agentloom.runtime.tool_protocol import ToolCallRecord, ToolErrorRecord

NATIVE_TOOL_CONTRACT_VERSION = 1
ToolOwner = Literal["runtime", "platform", "optional", "external"]
JournalState = Literal["prepared", "authorized", "executing", "committed", "uncertain", "cancelled"]


def _json(value: Any) -> str:
    def validate(item: Any) -> None:
        if isinstance(item, Mapping):
            for key, child in item.items():
                if not isinstance(key, str):
                    raise ValueError("JSON object keys must be strings")
                validate(child)
        elif isinstance(item, (list, tuple)):
            for child in item:
                validate(child)
        elif item is not None and not isinstance(item, (str, bool, int, float)):
            raise ValueError("Contract payload must be JSON data")

    validate(value)
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
        default=lambda item: dict(item) if isinstance(item, Mapping) else item,
    )


class JsonObject(Mapping[str, Any]):
    """Detached JSON object: reading a nested value cannot mutate the snapshot."""

    def __init__(self, value: Mapping[str, Any]):
        if not isinstance(value, Mapping):
            raise ValueError("Expected a JSON object")
        self._data = _json(value)

    def __getitem__(self, key: str) -> Any:
        return json.loads(self._data)[key]

    def __iter__(self) -> Iterator[str]:
        return iter(json.loads(self._data))

    def __len__(self) -> int:
        return len(json.loads(self._data))

    def __repr__(self) -> str:
        return "JsonObject(<private>)"


def _identifiers(*values: str) -> None:
    if any(not isinstance(value, str) or not value.strip() for value in values):
        raise ValueError("Contract identifiers must be non-empty strings")


@dataclass(frozen=True, slots=True)
class ToolManifestEntry:
    logical_name: str
    visible_name: str
    owner: ToolOwner
    provider: str
    capability: str
    operation: Literal["read", "write", "shell", "platform", "control"]
    parameters: Mapping[str, Any] = field(repr=False)
    path_parameters: tuple[str, ...] = ()
    command_parameter: str | None = None
    fixed_arguments: Mapping[str, Any] = field(default_factory=dict, repr=False)

    def __post_init__(self) -> None:
        _identifiers(self.logical_name, self.visible_name, self.provider, self.capability)
        if self.owner not in {"runtime", "platform", "optional", "external"}:
            raise ValueError("Unknown tool owner")
        if self.operation not in {"read", "write", "shell", "platform", "control"}:
            raise ValueError("Unknown tool operation")
        object.__setattr__(self, "parameters", JsonObject(self.parameters))
        object.__setattr__(self, "fixed_arguments", JsonObject(self.fixed_arguments))
        object.__setattr__(self, "path_parameters", tuple(self.path_parameters))


@dataclass(frozen=True, slots=True)
class NativeCallIdentity:
    application_id: str
    task_id: str
    run_id: str
    instance_id: str
    call_id: str
    native_session_id: str | None = None
    native_parent_id: str | None = None

    def __post_init__(self) -> None:
        _identifiers(self.application_id, self.task_id, self.run_id, self.instance_id, self.call_id)
        for value in (self.native_session_id, self.native_parent_id):
            if value is not None:
                _identifiers(value)


@dataclass(frozen=True, slots=True)
class NativePrepareRequest:
    identity: NativeCallIdentity
    tool: ToolManifestEntry
    cwd: str
    raw_arguments: Mapping[str, Any] = field(repr=False)

    def __post_init__(self) -> None:
        _identifiers(self.cwd)
        object.__setattr__(self, "raw_arguments", JsonObject(self.raw_arguments))


@dataclass(frozen=True, slots=True)
class NativeAuthorization:
    authorization_id: str
    identity: NativeCallIdentity
    tool: ToolManifestEntry
    cwd: str
    final_arguments: Mapping[str, Any] = field(repr=False)

    def __post_init__(self) -> None:
        _identifiers(self.authorization_id, self.cwd)
        object.__setattr__(self, "final_arguments", JsonObject(self.final_arguments))

    def require_match(
        self, identity: NativeCallIdentity, tool: ToolManifestEntry, cwd: str, final_arguments: Mapping[str, Any]
    ) -> None:
        if (
            identity != self.identity
            or tool != self.tool
            or cwd != self.cwd
            or _json(final_arguments) != _json(self.final_arguments)
        ):
            raise ValueError("Native authorization mismatch")


@dataclass(frozen=True, slots=True)
class NativePreparation:
    authorization: NativeAuthorization | None = None
    rejection: ToolCallRecord | None = None

    def __post_init__(self) -> None:
        if (self.authorization is None) == (self.rejection is None):
            raise ValueError("Preparation must authorize or reject exactly once")
        if self.rejection is not None and self.rejection.status == "completed":
            raise ValueError("Preparation cannot report an executed result")


@dataclass(frozen=True, slots=True)
class NativeExecutionOutcome:
    identity: NativeCallIdentity
    authorization_id: str
    status: Literal["completed", "error", "uncertain"]
    output: Any = field(default=None, repr=False)
    error: ToolErrorRecord | None = None

    def __post_init__(self) -> None:
        _identifiers(self.authorization_id)
        if self.status not in {"completed", "error", "uncertain"}:
            raise ValueError("Invalid native execution outcome")
        if self.status == "completed" and self.error is not None:
            raise ValueError("Completed execution cannot contain an error")
        if self.status != "completed" and self.error is None:
            raise ValueError("Failed or uncertain execution needs an error")
        if self.status != "completed" and self.output is not None:
            raise ValueError("Failed or uncertain execution cannot contain successful output")
        object.__setattr__(self, "output", json.loads(_json(self.output)))


@dataclass(frozen=True, slots=True)
class NativeResultCapture:
    """Adapter-verified first query output, independent of the executor display."""
    raw_output: Any = field(repr=False)
    complete: bool = True
    display_truncated: bool = False

    def __post_init__(self) -> None:
        if type(self.complete) is not bool or type(self.display_truncated) is not bool:
            raise ValueError("Capture flags must be booleans")
        object.__setattr__(self, "raw_output", json.loads(_json(self.raw_output)))


@dataclass(frozen=True, slots=True)
class NativeCommitAck:
    identity: NativeCallIdentity
    authorization_id: str
    commit_id: str
    record: ToolCallRecord = field(repr=False)

    def __post_init__(self) -> None:
        _identifiers(self.authorization_id, self.commit_id)
        if self.record.call_id != self.identity.call_id:
            raise ValueError("Commit acknowledgement call identity mismatch")


@dataclass(frozen=True, slots=True)
class NativeJournalEntry:
    authorization: NativeAuthorization
    state: JournalState
    request: NativePrepareRequest
    commit: NativeCommitAck | None = None

    def __post_init__(self) -> None:
        if (
            self.request.identity != self.authorization.identity
            or self.request.tool != self.authorization.tool
            or self.request.cwd != self.authorization.cwd
        ):
            raise ValueError("Journal preparation does not match authorization")
        if self.state not in {"prepared", "authorized", "executing", "committed", "uncertain", "cancelled"}:
            raise ValueError("Unknown native journal state")
        if (self.state == "committed") != (self.commit is not None):
            raise ValueError("Only durable committed entries contain a commit acknowledgement")
        if self.commit is not None:
            grant, ack = self.authorization, self.commit
            if (
                ack.identity != grant.identity
                or ack.authorization_id != grant.authorization_id
                or ack.record.tool_name != grant.tool.visible_name
                or _json(ack.record.input) != _json(grant.final_arguments)
            ):
                raise ValueError("Journal commit does not match authorization")

    @property
    def recovery_action(self) -> Literal["replay_result", "no_reexecute", "not_executed"]:
        if self.state == "committed":
            return "replay_result"
        if self.state in {"executing", "uncertain"}:
            return "no_reexecute"
        return "not_executed"


class NativeToolHost(Protocol):
    """The implementation must commit before returning an acknowledgement.

    Authorization is one-use; durable host state, not this value type, enforces
    replay protection. Uncertain execution never returns a fake terminal record.
    """

    def prepare(self, request: NativePrepareRequest) -> NativePreparation: ...
    def settle(self, outcome: NativeExecutionOutcome, *, capture: NativeResultCapture | None = None) -> NativeCommitAck | NativeJournalEntry: ...
    def cancel(self, identity: NativeCallIdentity) -> NativeJournalEntry: ...
