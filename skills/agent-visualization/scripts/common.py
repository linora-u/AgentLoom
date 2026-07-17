"""Shared utilities for agent-visualization hook scripts."""

from __future__ import annotations

import fcntl
import json
import os
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

SKILL_TAG = "[agent-visualization]"

# Tools whose PreToolUse/PostToolUse events should NOT generate timeline
# events (internal framework hooks, not user-visible actions).
FILTERED_TOOLS = frozenset({
    "validate_workspace_path",
    "shell_hook_wrapper",
    "final_answer",
})

# ---------------------------------------------------------------------------
# Path helpers
# ---------------------------------------------------------------------------

def visualization_path() -> Path:
    """Return the exact root timeline path injected by RuntimeContext."""

    injected = os.environ.get("AGENTLOOM_VISUALIZATION_PATH", "").strip()
    if not injected:
        raise RuntimeError(
            "AGENTLOOM_VISUALIZATION_PATH was not injected by AgentLoom RuntimeContext"
        )
    return Path(injected)


# ---------------------------------------------------------------------------
# Atomic JSON read/write
# ---------------------------------------------------------------------------

def read_viz_state(path: Path) -> dict[str, Any]:
    """Read the visualization JSON state, returning empty structure on error."""
    if not path.exists():
        return {"config": {"title": "", "agents": []}, "timeline": []}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(data, dict) and "config" in data and "timeline" in data:
            return data
    except (json.JSONDecodeError, OSError):
        pass
    return {"config": {"title": "", "agents": []}, "timeline": []}


@contextmanager
def _viz_lock(path: Path):
    path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = path.parent / f".{path.name}.lock"
    with lock_path.open("a+", encoding="utf-8") as lock_stream:
        fcntl.flock(lock_stream.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lock_stream.fileno(), fcntl.LOCK_UN)


def _write_viz_state_unlocked(path: Path, data: dict[str, Any]) -> None:
    tmp = path.parent / f".{path.name}.{uuid.uuid4().hex}.tmp"
    try:
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(path)
    finally:
        try:
            tmp.unlink()
        except FileNotFoundError:
            pass


def write_viz_state(path: Path, data: dict[str, Any]) -> None:
    """Atomically write visualization JSON under an inter-process lock."""

    with _viz_lock(path):
        _write_viz_state_unlocked(path, data)


# ---------------------------------------------------------------------------
# Agent config helpers
# ---------------------------------------------------------------------------

def ensure_agent_in_config(
    data: dict[str, Any],
    agent_name: str,
    agent_type: str = "worker",
) -> bool:
    """Add an agent to config.agents if not already present. Returns True if added."""
    agents: list[dict[str, str]] = data.get("config", {}).get("agents", [])
    for a in agents:
        if a.get("name") == agent_name:
            return False
    agents.append({"name": agent_name, "type": agent_type})
    return True


def register_agent_in_config(
    path: Path,
    agent_name: str,
    agent_type: str = "worker",
) -> bool:
    """Atomically register an agent without losing concurrent registrations."""

    with _viz_lock(path):
        data = read_viz_state(path)
        added = ensure_agent_in_config(data, agent_name, agent_type)
        if added:
            _write_viz_state_unlocked(path, data)
        return added


def update_latest_tool_event(
    path: Path,
    *,
    agent_name: str,
    tool_name: str,
    description: str,
    status: str | None = None,
    result: str | None = None,
    error: str | None = None,
) -> bool:
    """Atomically update the latest matching tool event for one agent."""

    with _viz_lock(path):
        data = read_viz_state(path)
        event = next(
            (
                item
                for item in reversed(data.get("timeline", []))
                if item.get("event_type") == "tool_call"
                and item.get("agent_name") == agent_name
                and item.get("tool_name") == tool_name
            ),
            None,
        )
        if event is None:
            return False
        event["description"] = description
        if status is not None:
            event["status"] = status
        if result is not None and "result" not in event:
            event["result"] = result
        if error is not None and "error" not in event:
            event["error"] = error
        _write_viz_state_unlocked(path, data)
        return True


def get_next_step(data: dict[str, Any]) -> int:
    """Return the next step number (max existing step + 1, or 1)."""
    timeline: list[dict] = data.get("timeline", [])
    if not timeline:
        return 1
    return max(ev.get("step", 0) for ev in timeline) + 1


# ---------------------------------------------------------------------------
# Event helpers
# ---------------------------------------------------------------------------

def append_event(
    path: Path,
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
    """Append a timeline event to the visualization JSON and write it back.

    Returns the event dict that was appended.
    """
    with _viz_lock(path):
        data = read_viz_state(path)
        step = get_next_step(data)

        event: dict[str, Any] = {
            "step": step,
            "agent_name": agent_name,
            "agent_type": agent_type,
            "event_type": event_type,
            "status": status,
            "description": description,
        }
        if tool_name is not None:
            event["tool_name"] = tool_name
        if tool_args is not None:
            event["tool_args"] = tool_args
        if target_agent is not None:
            event["target_agent"] = target_agent
        if result is not None:
            event["result"] = result
        if progress is not None:
            event["progress"] = progress

        data["timeline"].append(event)
        _write_viz_state_unlocked(path, data)
    return event


# ---------------------------------------------------------------------------
# Environment helpers (shell executor injects these env-vars)
# ---------------------------------------------------------------------------

def get_agent_name() -> str:
    """Resolve agent name from ``$AGENT_NAME``."""
    return os.environ.get("AGENT_NAME", "") or "default"


def get_tool_name() -> str:
    """Resolve tool name from ``$TOOL_NAME``."""
    return os.environ.get("TOOL_NAME", "") or "unknown"


def get_hook_context() -> dict[str, Any]:
    """Parse ``$HOOK_CONTEXT_JSON`` into a dict."""
    raw = os.environ.get("HOOK_CONTEXT_JSON", "").strip()
    if not raw:
        return {}
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    return payload if isinstance(payload, dict) else {}


def get_tool_input() -> dict[str, Any]:
    """Extract ``tool_input`` from the hook context."""
    ti = get_hook_context().get("tool_input")
    return ti if isinstance(ti, dict) else {}


def output(result: dict[str, Any]) -> None:
    """Print a JSON result to stdout (consumed by the shell executor)."""
    print(json.dumps(result, ensure_ascii=False))


def find_supervisor_name(data: dict[str, Any]) -> str | None:
    """Find the supervisor agent name from config, if any."""
    agents: list[dict[str, str]] = data.get("config", {}).get("agents", [])
    for a in agents:
        if a.get("type") == "supervisor":
            return a.get("name")
    return None


def find_supervisor_viz_path() -> Path:
    """Return the root supervisor timeline selected by RuntimeContext."""

    return visualization_path()


def is_filtered_tool(tool_name: str) -> bool:
    """Return True if this tool should be filtered from visualization events."""
    return tool_name in FILTERED_TOOLS
