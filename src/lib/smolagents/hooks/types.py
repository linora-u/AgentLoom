"""Core data models and types for the hook system.

Defines hook events, hook command types, matchers, and result structures.
Aligned with the upstream hook protocol: 16 event types, 4 command hook
types (command/prompt/http/agent), discriminated-union matcher config,
and aggregated result model with permission precedence semantics.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Literal, Optional, Union


# ---------------------------------------------------------------------------
# Supported decisions (legacy + new)
# ---------------------------------------------------------------------------

SUPPORTED_HOOK_DECISIONS = {"allow", "block", "modify"}

# Outcome values aligned with upstream HookResult.outcome
HOOK_OUTCOMES = {"success", "blocking", "non_blocking_error", "cancelled"}

# Permission behaviour values aligned with upstream precedence model
PERMISSION_BEHAVIORS = {"allow", "deny", "ask", "passthrough"}


# ---------------------------------------------------------------------------
# Hook events
# ---------------------------------------------------------------------------

class HookEvent(Enum):
    """Events that can trigger hooks.

    Aligned with upstream HOOK_EVENTS constant.  The string values must
    match the upstream protocol exactly so that shell hooks, YAML configs
    and skill definitions can reference them by name.

    Backward-compatible aliases are provided for renamed events to avoid
    breaking existing skill definitions.
    """

    # --- Tool lifecycle ---
    PRE_TOOL_USE = "PreToolUse"
    POST_TOOL_USE = "PostToolUse"
    POST_TOOL_USE_FAILURE = "PostToolUseFailure"

    # --- Session lifecycle ---
    SESSION_START = "SessionStart"
    SESSION_END = "SessionEnd"

    # --- Stop / completion ---
    STOP = "Stop"
    STOP_FAILURE = "StopFailure"

    # --- Sub-agent lifecycle ---
    SUBAGENT_START = "SubagentStart"
    SUBAGENT_STOP = "SubagentStop"

    # --- Compaction ---
    PRE_COMPACT = "PreCompact"
    POST_COMPACT = "PostCompact"

    # --- Task lifecycle ---
    TASK_CREATED = "TaskCreated"
    TASK_COMPLETED = "TaskCompleted"

    # --- Setup / config ---
    SETUP = "Setup"
    CONFIG_CHANGE = "ConfigChange"

    # --- Notification ---
    NOTIFICATION = "Notification"



# All canonical event names.  Used by config loaders for validation.
HOOK_EVENT_NAMES: List[str] = [
    "PreToolUse",
    "PostToolUse",
    "PostToolUseFailure",
    "SessionStart",
    "SessionEnd",
    "Stop",
    "StopFailure",
    "SubagentStart",
    "SubagentStop",
    "PreCompact",
    "PostCompact",
    "TaskCreated",
    "TaskCompleted",
    "Setup",
    "ConfigChange",
    "Notification",
]


# ---------------------------------------------------------------------------
# Hook command types (discriminated union)
# ---------------------------------------------------------------------------

@dataclass
class CommandHook:
    """Shell command hook -- spawns a subprocess.

    Aligned with upstream ``BashCommandHookSchema``.
    """
    type: Literal["command"] = "command"
    command: str = ""
    if_condition: Optional[str] = None
    shell: str = "bash"
    timeout: Optional[float] = None
    status_message: Optional[str] = None
    once: bool = False
    async_mode: bool = False
    async_rewake: bool = False


@dataclass
class PromptHook:
    """LLM prompt hook -- evaluates a prompt via a small/fast model.

    Aligned with upstream ``PromptHookSchema``.
    Use ``$ARGUMENTS`` placeholder in *prompt* for hook input JSON.
    """
    type: Literal["prompt"] = "prompt"
    prompt: str = ""
    if_condition: Optional[str] = None
    timeout: Optional[float] = None
    model: Optional[str] = None
    status_message: Optional[str] = None
    once: bool = False


@dataclass
class HttpHook:
    """HTTP POST hook -- sends hook input as JSON to a URL.

    Aligned with upstream ``HttpHookSchema``.
    Header values may reference environment variables via ``$VAR_NAME``
    syntax; only variables listed in *allowed_env_vars* are interpolated.
    """
    type: Literal["http"] = "http"
    url: str = ""
    if_condition: Optional[str] = None
    timeout: Optional[float] = None
    headers: Optional[Dict[str, str]] = None
    allowed_env_vars: Optional[List[str]] = None
    status_message: Optional[str] = None
    once: bool = False


@dataclass
class AgentHook:
    """Multi-turn agent verifier hook.

    Aligned with upstream ``AgentHookSchema``.
    Spawns a sub-agent that verifies a condition and returns a structured
    ``{ok: bool, reason?: str}`` result.
    """
    type: Literal["agent"] = "agent"
    prompt: str = ""
    if_condition: Optional[str] = None
    timeout: Optional[float] = None
    model: Optional[str] = None
    status_message: Optional[str] = None
    once: bool = False


# Discriminated union of all persistable hook command types.
HookCommand = Union[CommandHook, PromptHook, HttpHook, AgentHook]


# ---------------------------------------------------------------------------
# Hook matcher
# ---------------------------------------------------------------------------

@dataclass
class HookMatcher:
    """Associates a name-matching pattern with a list of hook commands.

    Aligned with upstream ``HookMatcherSchema``.
    *matcher* is a pattern string: ``None``/``""``/``"*"`` matches all;
    pipe-separated values like ``"Write|Edit"`` match any listed name;
    other strings are treated as regular expressions.
    """
    matcher: Optional[str] = None
    hooks: List[HookCommand] = field(default_factory=list)


# Type alias for the top-level hooks configuration mapping.
HooksSettings = Dict[str, List[HookMatcher]]


# ---------------------------------------------------------------------------
# Hook context (passed to every hook)
# ---------------------------------------------------------------------------

@dataclass
class HookContext:
    """Context information passed to hooks during execution."""
    session_id: str
    cwd: str
    hook_event_name: str
    tool_name: str
    tool_input: Dict[str, Any]
    tool_call_id: Optional[str] = None
    root_run_id: Optional[str] = None
    tool_response: Optional[Dict[str, Any]] = None
    tool_inputs_schema: Optional[Dict[str, Any]] = None
    step_number: Optional[int] = None
    task_id: Optional[str] = None
    sub_task_id: Optional[str] = None
    agent_name: Optional[str] = None
    agent_config: Optional[Dict[str, Any]] = None


# ---------------------------------------------------------------------------
# Hook result (single hook)
# ---------------------------------------------------------------------------

@dataclass
class HookResult:
    """Result returned by a single hook execution.

    Contains both the **new** outcome-based fields (aligned with upstream
    ``HookResult`` type) and the **legacy** decision-based fields for
    backward compatibility with existing Python function hooks.

    New code should prefer reading *outcome* / *permission_behavior* /
    *additional_context* / *updated_input*.  Legacy callers can continue
    using *success* / *decision* / *modified_input* / *agent_context*.
    """

    # --- Legacy fields (kept for backward compatibility) ---
    success: bool = True
    decision: str = "allow"  # "allow" | "block" | "modify"
    modified_input: Optional[Dict[str, Any]] = None
    modified_response: Optional[Dict[str, Any]] = None
    agent_context: Optional[str] = None
    user_message: Optional[str] = None
    reason: Optional[str] = None
    telemetry: Dict[str, Any] = field(default_factory=dict)

    # --- New fields (aligned with upstream HookResult type) ---
    outcome: str = "success"  # "success" | "blocking" | "non_blocking_error" | "cancelled"
    blocking_error: Optional[Dict[str, str]] = None
    prevent_continuation: bool = False
    stop_reason: Optional[str] = None
    permission_behavior: Optional[str] = None  # "allow" | "deny" | "ask" | "passthrough"
    permission_decision_reason: Optional[str] = None
    additional_context: Optional[str] = None
    updated_input: Optional[Dict[str, Any]] = None
    updated_response: Optional[Any] = None

    def __post_init__(self) -> None:
        if self.telemetry is None:
            self.telemetry = {}
        if self.decision not in SUPPORTED_HOOK_DECISIONS:
            supported = ", ".join(sorted(SUPPORTED_HOOK_DECISIONS))
            raise ValueError(
                f"Unsupported hook decision '{self.decision}'. "
                f"Supported decisions: {supported}."
            )

    # --- Convenience helpers ---

    def should_block(self) -> bool:
        """Return True if this result should block tool execution."""
        return (
            self.decision == "block"
            or self.outcome == "blocking"
            or self.prevent_continuation
            or self.permission_behavior == "deny"
        )

    def get_blocked_response(self) -> Any:
        """Return the reason text for a blocked action."""
        return (
            self.reason
            or self.stop_reason
            or (self.blocking_error or {}).get("blocking_error")
            or "Action blocked by security policy"
        )

    def merge_with_tool_result(self, original_result: Any) -> Any:
        """Merge hook modifications into the original tool result."""
        # Prefer new field, fall back to legacy
        effective_response = self.updated_response or self.modified_response

        if self.decision == "modify" and effective_response:
            if isinstance(original_result, dict) and isinstance(effective_response, dict):
                return {**original_result, **effective_response}
            if isinstance(effective_response, dict) and "result" in effective_response:
                return effective_response["result"]
            return effective_response

        return original_result


# ---------------------------------------------------------------------------
# Aggregated hook result (multiple hooks combined)
# ---------------------------------------------------------------------------

@dataclass
class AggregatedHookResult:
    """Combined result from executing multiple hooks for a single event.

    Aligned with upstream ``AggregatedHookResult`` type.

    Permission precedence: deny > ask > allow > passthrough.
    Blocking errors are collected as a list.
    Additional contexts are collected as a list (not concatenated).
    """
    blocking_errors: List[Dict[str, str]] = field(default_factory=list)
    prevent_continuation: bool = False
    stop_reason: Optional[str] = None
    permission_behavior: Optional[str] = None
    permission_decision_reason: Optional[str] = None
    additional_contexts: List[str] = field(default_factory=list)
    updated_input: Optional[Dict[str, Any]] = None
    updated_response: Optional[Any] = None

    # --- Legacy compat: expose aggregated state via legacy interface ---

    @property
    def success(self) -> bool:
        return not self.blocking_errors and not self.prevent_continuation

    @property
    def decision(self) -> str:
        if self.permission_behavior == "deny" or self.blocking_errors:
            return "block"
        if self.updated_input is not None or self.updated_response is not None:
            return "modify"
        return "allow"

    def should_block(self) -> bool:
        return self.decision == "block"

    def get_blocked_response(self) -> Any:
        if self.blocking_errors:
            return self.blocking_errors[0].get("blocking_error", "Blocked by hook")
        return self.stop_reason or "Action blocked by security policy"

    @property
    def reason(self) -> Optional[str]:
        if self.blocking_errors:
            return self.blocking_errors[0].get("blocking_error")
        return self.stop_reason

    @property
    def modified_input(self) -> Optional[Dict[str, Any]]:
        return self.updated_input

    @property
    def modified_response(self) -> Optional[Any]:
        return self.updated_response

    @property
    def agent_context(self) -> Optional[str]:
        return "\n".join(self.additional_contexts) if self.additional_contexts else None

    def merge_with_tool_result(self, original_result: Any) -> Any:
        effective_response = self.updated_response
        if effective_response:
            if isinstance(original_result, dict) and isinstance(effective_response, dict):
                return {**original_result, **effective_response}
            if isinstance(effective_response, dict) and "result" in effective_response:
                return effective_response["result"]
            return effective_response
        return original_result
