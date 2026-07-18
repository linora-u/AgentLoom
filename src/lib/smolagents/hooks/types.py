"""The reachable contract for trusted Python and configured Shell Hooks."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Literal


class HookEvent(Enum):
    PRE_TOOL_USE = "PreToolUse"
    POST_TOOL_USE = "PostToolUse"
    POST_TOOL_USE_FAILURE = "PostToolUseFailure"
    SESSION_START = "SessionStart"
    SESSION_END = "SessionEnd"
    STOP = "Stop"
    STOP_FAILURE = "StopFailure"
    SUBAGENT_START = "SubagentStart"
    SUBAGENT_STOP = "SubagentStop"
    TASK_CREATED = "TaskCreated"
    TASK_COMPLETED = "TaskCompleted"


HOOK_EVENT_NAMES = [event.value for event in HookEvent]
GATE_EVENTS = frozenset({HookEvent.PRE_TOOL_USE, HookEvent.STOP})
OBSERVER_EVENTS = frozenset(event for event in HookEvent if event not in GATE_EVENTS)
SUPPORTED_HOOK_DECISIONS = frozenset({"allow", "block", "modify"})


@dataclass
class HookContext:
    """One event observation passed to a Hook Handler."""

    local_run_id: str
    cwd: str
    hook_event_name: str
    tool_name: str
    tool_input: dict[str, Any]
    root_run_id: str | None = None
    tool_response: dict[str, Any] | None = None
    tool_inputs_schema: dict[str, Any] | None = None
    step_number: int | None = None
    task_id: str | None = None
    sub_task_id: str | None = None
    agent_name: str | None = None
    agent_config: dict[str, Any] | None = None
    runtime_agent_path: str | None = None
    project_root: str | None = None


@dataclass
class HookResult:
    """The single, event-aware result contract for Python and Shell Hooks."""

    decision: str = "allow"
    modified_input: dict[str, Any] | None = None
    agent_context: str | None = None
    user_message: str | None = None
    reason: str | None = None
    telemetry: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not isinstance(self.decision, str) or self.decision not in SUPPORTED_HOOK_DECISIONS:
            supported = ", ".join(sorted(SUPPORTED_HOOK_DECISIONS))
            raise ValueError(f"Unsupported hook decision '{self.decision}'. Supported decisions: {supported}.")
        if self.modified_input is not None and not isinstance(self.modified_input, dict):
            raise TypeError("modified_input must be a mapping or None")
        for field_name in ("agent_context", "user_message", "reason"):
            value = getattr(self, field_name)
            if value is not None and not isinstance(value, str):
                raise TypeError(f"{field_name} must be a string or None")
        if not isinstance(self.telemetry, dict):
            raise TypeError("telemetry must be a mapping")

    def should_block(self) -> bool:
        return self.decision == "block"

    def get_blocked_response(self) -> str:
        return self.reason or "Action blocked by hook policy"


HookCallable = Callable[[HookContext], HookResult | None]


@dataclass(frozen=True, slots=True)
class HookHandler:
    """A Hook Plan entry in deterministic registration order."""

    event: HookEvent
    pattern: str
    callback: HookCallable
    source: str = "internal"
    hook_id: str | None = None
    source_path: str | None = None
    cwd: str | None = None
    shell_spec: Any | None = field(default=None, repr=False, compare=False)


@dataclass(frozen=True, slots=True)
class Executed:
    """A tool call that crossed every gate and produced a model-visible value."""

    tool_input: dict[str, Any]
    value: Any
    tool_name: str = ""
    outcome: Literal["executed"] = field(default="executed", init=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "tool_input", dict(self.tool_input))


@dataclass(frozen=True, slots=True)
class Blocked:
    """A policy/schema rejection that occurred before tool side effects."""

    tool_input: dict[str, Any]
    reason: str
    stage: str
    tool_name: str = ""
    outcome: Literal["blocked"] = field(default="blocked", init=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "tool_input", dict(self.tool_input))

    def model_response(self) -> str:
        return self.reason or "Action blocked by tool runtime policy"


@dataclass(frozen=True, slots=True)
class Failed:
    """A failure after the tool execution boundary was entered."""

    tool_input: dict[str, Any]
    error: Exception
    stage: str
    tool_name: str = ""
    outcome: Literal["failed"] = field(default="failed", init=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "tool_input", dict(self.tool_input))


type ToolExecutionOutcome = Executed | Blocked | Failed
