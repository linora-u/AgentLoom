"""Strict data model for an Agent's current-task Todo snapshot."""

from __future__ import annotations

import json
from typing import Any

from src.lib.runtime import safe_agent_path

TODO_SCHEMA_VERSION = 1
TODO_STATUSES = ("pending", "in_progress", "completed", "cancelled")
MAX_TODO_ITEMS = 100
MAX_TODO_CONTENT_CHARS = 2_000
MAX_TODO_REASON_CHARS = 2_000
MAX_TODO_SNAPSHOT_BYTES = 64 * 1024

_ITEM_KEYS = frozenset({"content", "status", "cancel_reason"})


def validate_todo_items(raw: Any) -> list[dict[str, str]]:
    """Return a canonical complete list or raise before any state mutation."""

    if not isinstance(raw, list):
        raise ValueError("todos must be an array of objects")
    if len(raw) > MAX_TODO_ITEMS:
        raise ValueError(f"todos must contain at most {MAX_TODO_ITEMS} items")

    canonical: list[dict[str, str]] = []
    in_progress_count = 0
    for index, item in enumerate(raw):
        if not isinstance(item, dict):
            raise ValueError(f"todo item {index} must be an object")

        unexpected = sorted(set(item) - _ITEM_KEYS)
        if unexpected:
            raise ValueError(f"todo item {index} has unexpected field(s): {', '.join(unexpected)}")

        content = item.get("content")
        if not isinstance(content, str) or not content.strip():
            raise ValueError(f"todo item {index} content must be a non-empty string")
        content = content.strip()
        if len(content) > MAX_TODO_CONTENT_CHARS:
            raise ValueError(f"todo item {index} content exceeds {MAX_TODO_CONTENT_CHARS} characters")

        status = item.get("status")
        if not isinstance(status, str) or status not in TODO_STATUSES:
            raise ValueError(f"todo item {index} status must be one of: {', '.join(TODO_STATUSES)}")

        reason = item.get("cancel_reason")
        canonical_item = {"content": content, "status": status}
        if status == "cancelled":
            if not isinstance(reason, str) or not reason.strip():
                raise ValueError(f"todo item {index} cancel_reason is required when status is cancelled")
            reason = reason.strip()
            if len(reason) > MAX_TODO_REASON_CHARS:
                raise ValueError(f"todo item {index} cancel_reason exceeds {MAX_TODO_REASON_CHARS} characters")
            canonical_item["cancel_reason"] = reason
        elif reason is not None:
            raise ValueError(f"todo item {index} cancel_reason is only allowed when status is cancelled")

        if status == "in_progress":
            in_progress_count += 1
        canonical.append(canonical_item)

    if in_progress_count > 1:
        raise ValueError("todos may contain at most one in_progress item")

    serialized = json.dumps(canonical, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    if len(serialized) > MAX_TODO_SNAPSHOT_BYTES:
        raise ValueError(f"serialized todos exceed {MAX_TODO_SNAPSHOT_BYTES} bytes")
    return canonical


def empty_todo_snapshot(*, corrupt: bool = False) -> dict[str, Any]:
    return {"revision": 0, "items": [], "corrupt": corrupt}


def todo_counts(items: list[dict[str, str]]) -> dict[str, int]:
    counts = {status: 0 for status in TODO_STATUSES}
    for item in items:
        counts[item["status"]] += 1
    return counts


def validate_todo_document(raw: Any, *, task_id: str) -> dict[str, Any]:
    """Strictly decode the task-scoped persisted Todo document."""

    if not isinstance(raw, dict):
        raise ValueError("Todo document must be an object")
    expected_keys = {"schema_version", "task_id", "agents"}
    if set(raw) != expected_keys:
        raise ValueError("Todo document fields do not match schema")
    if raw.get("schema_version") != TODO_SCHEMA_VERSION:
        raise ValueError("unsupported Todo schema_version")
    if raw.get("task_id") != task_id:
        raise ValueError("Todo document task_id does not match checkpoint task")
    agents = raw.get("agents")
    if not isinstance(agents, dict):
        raise ValueError("Todo document agents must be an object")

    canonical_agents: dict[str, dict[str, Any]] = {}
    for raw_agent_path, raw_snapshot in agents.items():
        agent_path = safe_agent_path(raw_agent_path)
        if agent_path != raw_agent_path:
            raise ValueError("Todo document contains a non-canonical agent path")
        if not isinstance(raw_snapshot, dict) or set(raw_snapshot) != {"revision", "items"}:
            raise ValueError(f"Todo snapshot for {agent_path} does not match schema")
        agent_revision = raw_snapshot.get("revision")
        if isinstance(agent_revision, bool) or not isinstance(agent_revision, int) or agent_revision < 0:
            raise ValueError(f"Todo snapshot revision for {agent_path} is invalid")
        canonical_agents[agent_path] = {
            "revision": agent_revision,
            "items": validate_todo_items(raw_snapshot.get("items")),
        }

    return {
        "schema_version": TODO_SCHEMA_VERSION,
        "task_id": task_id,
        "agents": canonical_agents,
    }
