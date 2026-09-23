"""Canonical, provider-independent terminal Tool-call state."""

from __future__ import annotations

import json
from copy import deepcopy
from dataclasses import asdict, dataclass, field
from typing import Any, Literal

TOOL_CALL_RAW_KEY = "agentloom_tool_call"
TOOL_RESULT_RAW_KEY = "agentloom_tool_result"
MODEL_OUTPUT_METADATA_KEY = "agentloom_model_output"

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
        metadata: dict[str, Any] | None = None,
        started_at: float | None = None,
        ended_at: float | None = None,
    ) -> ToolCallRecord:
        return cls(
            call_id=call_id,
            tool_name=tool_name,
            input=input,
            status="completed",
            output=output,
            metadata=metadata or {},
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
            error if any(hasattr(error, name) for name in ("kind", "stage", "retryable", "blocked")) else cause
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
            error if any(hasattr(error, name) for name in ("kind", "stage", "retryable", "blocked")) else cause
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
        metadata = deepcopy(self.metadata)
        metadata.pop(MODEL_OUTPUT_METADATA_KEY, None)
        return ToolCallRecord(
            call_id=self.call_id,
            tool_name=self.tool_name,
            input=self.input,
            status=self.status,
            output=output,
            error=self.error,
            metadata=metadata,
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

    def model_output(self) -> Any:
        """Return the model-visible projection without changing canonical output."""

        if self.status != "completed":
            return None
        return self.metadata.get(MODEL_OUTPUT_METADATA_KEY, self.output)

    def model_content(self) -> str:
        if self.status == "completed":
            output = self.model_output()
            if isinstance(output, str):
                return output
            return json.dumps(
                output,
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
                default=str,
            )
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
        semantic_error = error if any(hasattr(error, name) for name in ("kind", "stage", "retryable")) else cause
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
