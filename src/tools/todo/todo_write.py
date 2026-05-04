"""
TodoWrite tool — session task tracking for planning-driven agents.

Provides a lightweight todo list that persists to .runtime/<agent>/todos.md.
Automatically injected when planning_interval is configured.
"""

import json
from pathlib import Path
from typing import Any, Dict, List, Optional, Union

from src.lib.logging import get_logger
from src.lib.smolagents.tools.tools import tool

logger = get_logger(__name__)

# ---------------------------------------------------------------------------
# Module-level state (per-process, single-threaded)
# ---------------------------------------------------------------------------

_current_todos: List[Dict[str, str]] = []

VALID_STATUSES = {"pending", "in_progress", "completed"}


def _reset_state() -> None:
    """Clear all module-level state. Used by tests to avoid cross-pollution."""
    global _current_todos
    _current_todos = []


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _get_agent_name() -> str:
    """Return current agent name via ContextVar, fallback to 'default'."""
    try:
        from src.trace import get_current_agent_name
        name = get_current_agent_name()
        return name if name else "default"
    except Exception:
        return "default"


def _get_runtime_agent_path() -> str:
    """Return hierarchical runtime path for .runtime dir nesting.

    Prefers the dedicated ``runtime_agent_path`` ContextVar (e.g.
    ``parent/child``).  Falls back to the flat ``agent_name``.
    """
    try:
        from src.trace import get_current_runtime_agent_path
        path = get_current_runtime_agent_path()
        if path:
            return path
    except Exception:
        pass
    return _get_agent_name()


def _get_project_root() -> Path:
    """Return project root via C.agent_root, fallback to cwd."""
    try:
        from src.lib.config import C
        return Path(C.agent_root).resolve()
    except Exception:
        return Path.cwd().resolve()


def _persist_todos(todos: List[Dict[str, str]], agent_name: str) -> None:
    """Write todos to .runtime/<agent>/todos.md in Markdown checkbox format."""
    try:
        root = _get_project_root()
        runtime_dir = root / ".runtime" / agent_name
        runtime_dir.mkdir(parents=True, exist_ok=True)
        todos_file = runtime_dir / "todos.md"

        logger.debug(
            "Persisting %d todos to %s",
            len(todos), todos_file,
        )

        lines = ["# Task Progress\n"]
        for item in todos:
            status = item["status"]
            content = item["content"]
            if status == "completed":
                lines.append(f"- [x] {content}")
            elif status == "in_progress":
                lines.append(f"- [ ] **IN PROGRESS** {content}")
            else:  # pending
                lines.append(f"- [ ] {content}")

        todos_file.write_text("\n".join(lines) + "\n", encoding="utf-8")
    except Exception as exc:
        logger.warning("Failed to persist todos: %s", exc)


def _validate_todos(raw: Any) -> tuple:
    """Validate and parse todo input.

    Returns (parsed_list, error_string). If error_string is non-empty,
    parsed_list should be ignored.
    """
    # Accept pre-parsed list
    if isinstance(raw, list):
        items = raw
    elif isinstance(raw, str):
        try:
            items = json.loads(raw)
        except (json.JSONDecodeError, ValueError) as exc:
            return [], f"Error: Invalid JSON input. {exc}"
    else:
        return [], f"Error: Expected JSON string or list, got {type(raw).__name__}."

    if not isinstance(items, list):
        return [], "Error: Input must be a JSON array of todo objects."

    validated: List[Dict[str, str]] = []
    for idx, item in enumerate(items):
        if not isinstance(item, dict):
            return [], f"Error: Item at index {idx} is not an object."

        content = item.get("content")
        if content is None:
            return [], f"Error: Item at index {idx} is missing 'content' field."

        if not isinstance(content, str) or not content.strip():
            return [], f"Error: Item at index {idx} has empty or invalid 'content'."

        status = item.get("status", "pending")
        if status not in VALID_STATUSES:
            return [], (
                f"Error: Item at index {idx} has invalid status '{status}'. "
                f"Must be one of: {', '.join(sorted(VALID_STATUSES))}."
            )

        validated.append({
            "content": content.strip(),
            "status": status,
        })

    return validated, ""


# ---------------------------------------------------------------------------
# Tool function
# ---------------------------------------------------------------------------

@tool
def todo_write(todos: str) -> str:
    """Update the todo list to track task progress during multi-step work.

    Use this tool proactively to register planned tasks and update their
    status as you work. Each call fully replaces the previous list (not
    append). Ensure at least one task is in_progress at all times while
    work is ongoing.

    When to use:
    - After creating a plan: register all tasks with appropriate statuses
    - After completing a task: mark it completed and next one in_progress
    - When plan changes: update the full list to reflect new tasks

    When NOT to use:
    - Single trivial tasks that need no tracking
    - Purely conversational requests

    Task statuses:
    - pending: not yet started
    - in_progress: currently working on (keep to ONE at a time)
    - completed: finished successfully

    Args:
        todos: JSON string containing an array of todo objects, each with
            'content' (str, imperative task description) and 'status'
            (str, one of 'pending', 'in_progress', 'completed').
            Example: '[{"content": "Run tests", "status": "in_progress"},
                       {"content": "Update docs", "status": "pending"}]'

    Returns:
        Status summary string with counts per status, or an error message
        if the input is invalid.
    """
    global _current_todos

    validated, error = _validate_todos(todos)
    if error:
        return error

    # Guard: reject empty list to prevent clearing existing content
    if not validated:
        logger.debug("todo_write called with empty list, skipping persist")
        return "Skipped: empty task list, no changes made."

    agent_name = _get_runtime_agent_path()

    # Check if all completed
    all_completed = len(validated) > 0 and all(
        t["status"] == "completed" for t in validated
    )

    if all_completed:
        # Keep completed records visible instead of clearing
        _current_todos = validated
        _persist_todos(validated, agent_name)

        # Verification nudge: 3+ tasks, none mention "verif"
        if (
            len(validated) >= 3
            and not any("verif" in t["content"].lower() for t in validated)
        ):
            return (
                "IMPORTANT: You completed 3+ tasks without a verification step. "
                "Consider running tests or verification before reporting final "
                f"answer.\n\nAll {len(validated)} tasks completed."
            )
        return f"All {len(validated)} tasks completed."

    # Normal update: full replace
    _current_todos = validated
    _persist_todos(validated, agent_name)

    # Build summary
    counts: Dict[str, int] = {"completed": 0, "in_progress": 0, "pending": 0}
    for t in validated:
        counts[t["status"]] += 1

    parts = []
    if counts["completed"]:
        parts.append(f"{counts['completed']} completed")
    if counts["in_progress"]:
        parts.append(f"{counts['in_progress']} in progress")
    if counts["pending"]:
        parts.append(f"{counts['pending']} pending")

    summary = f"Updated: {len(validated)} todos ({', '.join(parts)})."

    # Soft warning for multiple in_progress
    if counts["in_progress"] > 1:
        summary += (
            f" Warning: {counts['in_progress']} tasks marked as in_progress. "
            "Best practice is exactly ONE at a time."
        )

    return summary
