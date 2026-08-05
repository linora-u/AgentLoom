"""Model-facing tools for observing and completing the root Goal."""

from __future__ import annotations

import json

from src.lib.smolagents.tools.tools import tool


def _require_root_goal_provider():
    from src.lib.goal import get_current_goal_provider, normalize_goal_config
    from src.trace import (
        get_current_hook_run,
        require_local_run_id,
        require_root_run_id,
    )

    if require_local_run_id() != require_root_run_id():
        raise PermissionError("Goal tools are available only to the root Supervisor Agent")
    hook_run = get_current_hook_run(required=True)
    if hook_run.parent is not None:
        raise PermissionError("Goal tools are available only to the root Supervisor Agent")
    config = hook_run.agent_config
    if not isinstance(config, dict) or not normalize_goal_config(
        config,
        source="active Supervisor",
    ).enabled:
        raise PermissionError("Goal mode is not enabled for the root Supervisor Agent")
    return get_current_goal_provider(required=True)


@tool
def get_goal() -> str:
    """Return the root task's canonical Goal status and cumulative token usage.

    Use this to inspect the objective, completion state, and remaining soft
    token budget. This tool is restricted to the root Supervisor Agent.

    Returns:
        JSON containing the complete canonical Goal snapshot.
    """

    provider = _require_root_goal_provider()
    return json.dumps(
        provider.snapshot().to_dict(),
        ensure_ascii=False,
        separators=(",", ":"),
    )


@tool
def update_goal(status: str, evidence: str) -> str:
    """Mark the root Goal complete with durable, non-empty evidence.

    Call this only after the full objective has been delivered and verified.
    A normal final answer does not complete a Goal. The only accepted status is
    complete. Completion is terminal and idempotent. This tool is restricted
    to the root Supervisor Agent.

    Args:
        status: Must be exactly complete.
        evidence: Concise evidence describing what was delivered and verified.

    Returns:
        JSON containing the committed canonical Goal snapshot.
    """

    if status != "complete":
        raise ValueError("update_goal status must be 'complete'")
    from src.trace import require_local_run_id

    provider = _require_root_goal_provider()
    state = provider.complete(
        evidence,
        settlement_run_id=require_local_run_id(),
    )
    return json.dumps(state.to_dict(), ensure_ascii=False, separators=(",", ":"))
