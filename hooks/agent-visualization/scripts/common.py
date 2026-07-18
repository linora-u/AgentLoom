"""Shared runtime primitives for the agent-visualization Hook Bundle."""

from __future__ import annotations

import fcntl
import json
import os
import sys
import tempfile
from collections.abc import Callable
from contextlib import contextmanager
from pathlib import Path
from typing import Any

HOOK_TAG = "[agent-visualization]"
HOOK_INPUT_SCHEMA_VERSION = 1
_hook_context_cache: dict[str, Any] | None = None

FILTERED_TOOLS = frozenset(
    {
        "validate_workspace_path",
        "shell_hook_wrapper",
        "final_answer",
    }
)


# ---------------------------------------------------------------------------
# Versioned Hook stdin and deterministic run identity
# ---------------------------------------------------------------------------

def get_hook_context() -> dict[str, Any]:
    """Read and validate the v1 Hook input object from stdin exactly once."""
    global _hook_context_cache
    if _hook_context_cache is not None:
        return _hook_context_cache

    raw = sys.stdin.read().strip()
    if not raw:
        raise ValueError("Hook stdin must contain a JSON object")
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError("Hook stdin must contain valid JSON") from exc
    if not isinstance(payload, dict):
        raise ValueError("Hook stdin must contain a JSON object")
    if payload.get("schema_version") != HOOK_INPUT_SCHEMA_VERSION:
        raise ValueError(
            f"Unsupported Hook stdin schema_version: {payload.get('schema_version')!r}"
        )
    _hook_context_cache = payload
    return payload


def _set_hook_context_for_testing(payload: dict[str, Any] | None) -> None:
    """Replace the process-local stdin cache for deterministic unit tests."""
    global _hook_context_cache
    _hook_context_cache = payload


def _required_text(field: str) -> str:
    value = get_hook_context().get(field)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"Hook stdin field {field!r} must be a non-empty string")
    return value.strip()


def get_agent_name() -> str:
    """Return the active Agent's display name."""
    value = get_hook_context().get("agent_name")
    return value.strip() if isinstance(value, str) and value.strip() else "default"


def get_runtime_agent_path() -> str:
    """Return the hierarchical runtime Agent path from Hook stdin."""
    return _required_text("runtime_agent_path")


def get_root_run_id() -> str:
    """Return the root invocation identity used for storage isolation."""
    return _required_text("root_run_id")


def get_tool_name() -> str:
    """Return the current tool name."""
    value = get_hook_context().get("tool_name")
    return value.strip() if isinstance(value, str) and value.strip() else "unknown"


def get_tool_input() -> dict[str, Any]:
    """Return the current tool input object."""
    value = get_hook_context().get("tool_input")
    return value if isinstance(value, dict) else {}


def viz_output_path() -> Path:
    """Return the canonical root timeline path injected by RuntimeContext."""

    return Path(_required_text("agent_visualization_path"))


# ---------------------------------------------------------------------------
# Locked JSON transactions
# ---------------------------------------------------------------------------

def _empty_state() -> dict[str, Any]:
    return {"config": {"title": "", "agents": []}, "timeline": []}


def _read_state_unlocked(path: Path) -> dict[str, Any]:
    if not path.exists():
        return _empty_state()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return _empty_state()
    if not isinstance(data, dict):
        return _empty_state()
    config = data.get("config")
    timeline = data.get("timeline")
    if not isinstance(config, dict) or not isinstance(timeline, list):
        return _empty_state()
    return data


def _write_state_unlocked(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as stream:
            temp_path = Path(stream.name)
            json.dump(data, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp_path, path)
        temp_path = None
    finally:
        if temp_path is not None:
            temp_path.unlink(missing_ok=True)


@contextmanager
def _state_lock(path: Path):
    path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = path.with_name(f".{path.name}.lock")
    with lock_path.open("a+b") as stream:
        fcntl.flock(stream.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(stream.fileno(), fcntl.LOCK_UN)


def update_viz_state[T](
    path: Path,
    mutation: Callable[[dict[str, Any]], T],
) -> T:
    """Execute a complete cross-process read-modify-write transaction."""
    with _state_lock(path):
        data = _read_state_unlocked(path)
        result = mutation(data)
        _write_state_unlocked(path, data)
        return result


def read_viz_state(path: Path) -> dict[str, Any]:
    """Read one consistent visualization snapshot under the process lock."""
    with _state_lock(path):
        return _read_state_unlocked(path)


# ---------------------------------------------------------------------------
# State mutation helpers
# ---------------------------------------------------------------------------

def ensure_agent_in_config(
    data: dict[str, Any],
    agent_name: str,
    agent_type: str = "worker",
) -> bool:
    """Add an Agent to config exactly once."""
    config = data.setdefault("config", {"title": "", "agents": []})
    agents = config.setdefault("agents", [])
    for agent in agents:
        if agent.get("name") == agent_name:
            return False
    agents.append({"name": agent_name, "type": agent_type})
    return True


def find_supervisor_name(data: dict[str, Any]) -> str | None:
    """Return the supervisor display name, if initialized."""
    agents = data.get("config", {}).get("agents", [])
    for agent in agents:
        if agent.get("type") == "supervisor":
            value = agent.get("name")
            return value if isinstance(value, str) else None
    return None


def append_event_to_state(
    data: dict[str, Any],
    *,
    agent_name: str,
    agent_type: str,
    event_type: str,
    status: str,
    description: str = "",
    tool_name: str | None = None,
    tool_args: dict[str, Any] | None = None,
    target_agent: str | None = None,
    result: str | None = None,
    progress: str | None = None,
) -> dict[str, Any]:
    """Append an event to a state object already protected by its lock."""
    timeline = data.setdefault("timeline", [])
    step = max((event.get("step", 0) for event in timeline), default=0) + 1
    event: dict[str, Any] = {
        "step": step,
        "agent_name": agent_name,
        "agent_type": agent_type,
        "event_type": event_type,
        "status": status,
        "description": description,
    }
    optional = {
        "tool_name": tool_name,
        "tool_args": tool_args,
        "target_agent": target_agent,
        "result": result,
        "progress": progress,
    }
    event.update({key: value for key, value in optional.items() if value is not None})
    timeline.append(event)
    return event


def append_event(path: Path, **fields: Any) -> dict[str, Any]:
    """Append one event in a complete locked transaction."""
    return update_viz_state(
        path,
        lambda data: append_event_to_state(data, **fields),
    )


def output(result: dict[str, Any]) -> None:
    """Write the strict Hook result object to stdout."""
    print(json.dumps(result, ensure_ascii=False))


def is_filtered_tool(tool_name: str) -> bool:
    """Return whether a tool should be hidden from visualization events."""
    return tool_name in FILTERED_TOOLS
