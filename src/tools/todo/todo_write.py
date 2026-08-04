"""Model-facing full-snapshot Todo writer for the current Agent task."""

from __future__ import annotations

import json

from src.lib.smolagents.tools.tools import tool


@tool
def todo_write(todos: list[dict[str, str]]) -> str:
    """Replace this Agent's complete Todo list for the current task.

    Use Todo tracking when a task has three or more meaningful steps, multiple
    deliverables, substantial uncertainty, or work that benefits from explicit
    verification and recovery. Skip it for one-step work, pure questions or
    answers, and casual conversation.

    Each call replaces the complete ordered list. Omitted items are removed and
    an empty array clears the list. Keep at most one item in_progress. Update the
    list immediately when work starts, completes, or the plan changes. Call this
    tool alone: never issue parallel Todo writes or combine it with another tool
    or final_answer. Finish the last Todo update before calling final_answer.

    Every item must contain content and one status: pending, in_progress,
    completed, or cancelled. Use cancelled only when an item is no longer
    needed or was superseded, and include a non-empty cancel_reason. Blocked,
    failed, partial, or unverified work is not cancelled.

    Limits: at most 100 items, 2000 characters per content or cancel_reason,
    and 65536 serialized bytes for the complete list.

    Args:
        todos: Complete ordered array of Todo objects. Each object contains
            content and status, plus cancel_reason only for cancelled items.

    Returns:
        JSON containing the exact committed todos, per-status counts, and the
        new canonical list revision.
    """
    from src.lib.todo import get_current_todo_provider, todo_counts
    from src.trace import get_current_agent_name, get_current_runtime_agent_path

    provider = get_current_todo_provider(required=True)
    agent_path = get_current_runtime_agent_path() or get_current_agent_name() or "default"
    snapshot = provider.replace(agent_path, todos)
    payload = {
        "revision": snapshot["revision"],
        "todos": snapshot["items"],
        "counts": todo_counts(snapshot["items"]),
    }
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
