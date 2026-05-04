"""Hook JSON I/O schema validation and output processing.

Provides Pydantic v2 models that mirror the upstream Zod schemas
(``syncHookResponseSchema``, ``hookJSONOutputSchema``) and a
``process_hook_output()`` function aligned with the upstream
``processHookJSONOutput()``.
"""

from __future__ import annotations

import json
from typing import Any, Dict, Literal, Optional, Union

from pydantic import BaseModel, Field

from src.lib.logging import get_logger

from .types import HookResult

logger = get_logger(__name__)


# ---------------------------------------------------------------------------
# Output schemas (aligned with upstream Zod schemas)
# ---------------------------------------------------------------------------

class HookSpecificOutput(BaseModel):
    """Event-specific output fields from a hook."""
    hook_event_name: Optional[str] = Field(None, alias="hookEventName")
    permission_decision: Optional[str] = Field(None, alias="permissionDecision")
    permission_decision_reason: Optional[str] = Field(None, alias="permissionDecisionReason")
    updated_input: Optional[Dict[str, Any]] = Field(None, alias="updatedInput")
    additional_context: Optional[str] = Field(None, alias="additionalContext")
    watch_paths: Optional[list] = Field(None, alias="watchPaths")

    model_config = {"populate_by_name": True, "extra": "allow"}


class SyncHookOutput(BaseModel):
    """Synchronous hook JSON output — aligned with upstream ``syncHookResponseSchema``.

    Fields use aliases matching the upstream camelCase JSON keys.
    Supports a top-level ``agent_context`` shorthand that hook scripts
    can use instead of the nested ``hookSpecificOutput.additionalContext``.
    """
    continue_: Optional[bool] = Field(True, alias="continue")
    suppress_output: Optional[bool] = Field(False, alias="suppressOutput")
    stop_reason: Optional[str] = Field(None, alias="stopReason")
    decision: Optional[Literal["approve", "block"]] = None
    reason: Optional[str] = None
    system_message: Optional[str] = Field(None, alias="systemMessage")
    hook_specific_output: Optional[HookSpecificOutput] = Field(
        None, alias="hookSpecificOutput"
    )
    # Shorthand: hook scripts can output {"agent_context": "..."} directly
    # instead of nesting inside hookSpecificOutput.additionalContext.
    agent_context: Optional[str] = Field(None, alias="agent_context")

    model_config = {"populate_by_name": True, "extra": "allow"}


class AsyncHookOutput(BaseModel):
    """Asynchronous hook JSON output — first-line detection marker.

    A hook emitting ``{"async": true}`` as its first stdout line signals
    that it should be backgrounded immediately.
    """
    async_: Literal[True] = Field(..., alias="async")
    async_timeout: Optional[int] = Field(None, alias="asyncTimeout")

    model_config = {"populate_by_name": True, "extra": "allow"}


# Union type for all hook JSON outputs.
HookJSONOutput = Union[SyncHookOutput, AsyncHookOutput]


# ---------------------------------------------------------------------------
# Output parsing
# ---------------------------------------------------------------------------

def parse_hook_output(stdout: str) -> Union[SyncHookOutput, AsyncHookOutput, str]:
    """Parse hook stdout into a validated output model.

    Aligned with upstream ``parseHookOutput()``:
    - Non-JSON output (doesn't start with ``{``) -> returned as plain str
    - JSON that validates against async schema -> ``AsyncHookOutput``
    - JSON that validates against sync schema -> ``SyncHookOutput``
    - JSON that fails validation -> returned as plain str with warning
    """
    trimmed = stdout.strip()

    if not trimmed or not trimmed.startswith("{"):
        return stdout

    try:
        parsed = json.loads(trimmed)
    except (json.JSONDecodeError, ValueError):
        return stdout

    if not isinstance(parsed, dict):
        return stdout

    # Check async first (simple discriminant: "async" key with True value)
    if parsed.get("async") is True:
        try:
            return AsyncHookOutput.model_validate(parsed)
        except Exception:
            pass

    # Try sync schema
    try:
        return SyncHookOutput.model_validate(parsed)
    except Exception as exc:
        logger.debug(
            "Hook JSON output failed schema validation: %s. Raw: %s",
            exc, trimmed[:200],
        )
        return stdout


def is_async_output(output: Any) -> bool:
    """Check if parsed output is an async marker."""
    return isinstance(output, AsyncHookOutput)


# ---------------------------------------------------------------------------
# Output processing -> HookResult (aligned with processHookJSONOutput)
# ---------------------------------------------------------------------------

def process_hook_output(
    output: SyncHookOutput,
    *,
    hook_event: str = "",
    command: str = "",
    exit_code: int = 0,
    stderr: str = "",
) -> HookResult:
    """Transform a validated ``SyncHookOutput`` into a ``HookResult``.

    Aligned with upstream ``processHookJSONOutput()``.
    Handles ``continue``, ``decision``, ``hookSpecificOutput`` routing.
    """
    # Determine outcome from exit code
    if exit_code == 0:
        outcome = "success"
    elif exit_code == 2:
        outcome = "blocking"
    else:
        outcome = "non_blocking_error"

    result = HookResult(outcome=outcome)

    # continue=false -> prevent continuation
    if output.continue_ is False:
        result.prevent_continuation = True
        result.stop_reason = output.stop_reason or "Hook requested stop"
        result.outcome = "blocking"
        result.decision = "block"
        result.success = False

    # decision field
    if output.decision == "block":
        result.permission_behavior = "deny"
        result.outcome = "blocking"
        result.decision = "block"
        result.success = False
        result.blocking_error = {
            "blocking_error": output.reason or "Blocked by hook",
            "command": command,
        }
        result.reason = output.reason
    elif output.decision == "approve":
        result.permission_behavior = "allow"

    # Hook-specific output routing
    hso = output.hook_specific_output
    if hso:
        # Permission decision (PreToolUse)
        if hso.permission_decision:
            result.permission_behavior = hso.permission_decision
            result.permission_decision_reason = hso.permission_decision_reason
            if hso.permission_decision == "deny":
                result.decision = "block"
                result.success = False
                result.outcome = "blocking"

        # Updated input (PreToolUse)
        if hso.updated_input:
            result.updated_input = hso.updated_input
            result.modified_input = hso.updated_input
            if result.decision == "allow":
                result.decision = "modify"

        # Additional context (many events)
        if hso.additional_context:
            result.additional_context = hso.additional_context
            result.agent_context = hso.additional_context

    # Fallback: top-level agent_context shorthand.
    # Hook scripts can output {"agent_context": "..."} directly instead
    # of nesting inside hookSpecificOutput.additionalContext.
    if not result.agent_context and output.agent_context:
        result.additional_context = output.agent_context
        result.agent_context = output.agent_context

    # System message -> user_message
    if output.system_message:
        result.user_message = output.system_message

    # Reason fallback
    if output.reason and not result.reason:
        result.reason = output.reason

    return result
