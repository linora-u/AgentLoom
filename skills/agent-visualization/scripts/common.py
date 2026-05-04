"""Shared utilities for agent-visualization hook scripts."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Dict, List, Optional


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

SKILL_TAG = "[agent-visualization]"
VIZ_FILENAME = "visualization.json"

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

def _find_agent_loom_root() -> Path:
    """Derive the AgentLoom project root directory.

    Resolution order:
    1. ``$AGENT_LOOM_RUNTIME_ROOT`` env var (for tests with temp dirs).
    2. Walk upward from this file's location and look for
       ``config/llm.yaml`` — the globally unique AgentLoom root marker.
       This works regardless of how deeply the skill is nested.
    3. Fall back to ``pyproject.toml`` detection (backward compatibility).
    4. Fall back to CWD.
    """
    env_root = os.environ.get("AGENT_LOOM_RUNTIME_ROOT", "").strip()
    if env_root:
        return Path(env_root)

    # Walk upward looking for config/llm.yaml (globally unique marker).
    current = Path(__file__).resolve().parent
    while current != current.parent:
        if (current / "config" / "llm.yaml").exists():
            return current
        current = current.parent

    # Backward compatibility: fixed 4-level walk + pyproject.toml.
    candidate = Path(__file__).resolve().parent.parent.parent.parent
    if (candidate / "pyproject.toml").exists():
        return candidate

    return Path.cwd()


def get_runtime_agent_path() -> str:
    """Resolve hierarchical runtime path from ``$RUNTIME_AGENT_PATH``.

    Falls back to ``$AGENT_NAME`` then ``"default"``.  The runtime path
    may contain ``/`` separators (e.g. ``parent/child``) so that .runtime
    directories nest under the parent agent.
    """
    return os.environ.get("RUNTIME_AGENT_PATH", "").strip() or get_agent_name()


def viz_output_path(agent_name: str) -> Path:
    """Return the visualization JSON output path for an agent.

    ``<agent_loom_root>/.runtime/<agent_name>/visualization.json``
    """
    return _find_agent_loom_root() / ".runtime" / agent_name / VIZ_FILENAME


# ---------------------------------------------------------------------------
# Atomic JSON read/write
# ---------------------------------------------------------------------------

def read_viz_state(path: Path) -> Dict[str, Any]:
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


def write_viz_state(path: Path, data: Dict[str, Any]) -> None:
    """Atomically write visualization JSON (tmp + rename)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


# ---------------------------------------------------------------------------
# Agent config helpers
# ---------------------------------------------------------------------------

def ensure_agent_in_config(
    data: Dict[str, Any],
    agent_name: str,
    agent_type: str = "worker",
) -> bool:
    """Add an agent to config.agents if not already present. Returns True if added."""
    agents: List[Dict[str, str]] = data.get("config", {}).get("agents", [])
    for a in agents:
        if a.get("name") == agent_name:
            return False
    agents.append({"name": agent_name, "type": agent_type})
    return True


def get_next_step(data: Dict[str, Any]) -> int:
    """Return the next step number (max existing step + 1, or 1)."""
    timeline: List[Dict] = data.get("timeline", [])
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
    tool_name: Optional[str] = None,
    tool_args: Optional[Dict[str, Any]] = None,
    target_agent: Optional[str] = None,
    result: Optional[str] = None,
    progress: Optional[str] = None,
) -> Dict[str, Any]:
    """Append a timeline event to the visualization JSON and write it back.

    Returns the event dict that was appended.
    """
    data = read_viz_state(path)
    step = get_next_step(data)

    event: Dict[str, Any] = {
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
    write_viz_state(path, data)
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


def get_hook_context() -> Dict[str, Any]:
    """Parse ``$HOOK_CONTEXT_JSON`` into a dict."""
    raw = os.environ.get("HOOK_CONTEXT_JSON", "").strip()
    if not raw:
        return {}
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    return payload if isinstance(payload, dict) else {}


def get_tool_input() -> Dict[str, Any]:
    """Extract ``tool_input`` from the hook context."""
    ti = get_hook_context().get("tool_input")
    return ti if isinstance(ti, dict) else {}


def output(result: Dict[str, Any]) -> None:
    """Print a JSON result to stdout (consumed by the shell executor)."""
    print(json.dumps(result, ensure_ascii=False))


def find_supervisor_name(data: Dict[str, Any]) -> Optional[str]:
    """Find the supervisor agent name from config, if any."""
    agents: List[Dict[str, str]] = data.get("config", {}).get("agents", [])
    for a in agents:
        if a.get("type") == "supervisor":
            return a.get("name")
    return None


def find_supervisor_viz_path() -> Path:
    """Locate the supervisor's visualization.json.

    Searches all ``.runtime/*/visualization.json`` files and returns the one
    whose ``config.agents`` list contains an agent with ``type == "supervisor"``.
    Falls back to the first found file, or creates a path from ``$AGENT_NAME``.
    """
    env_root = os.environ.get("AGENT_LOOM_RUNTIME_ROOT", "").strip()
    if env_root:
        runtime_root = Path(env_root) / ".runtime"
    else:
        runtime_root = _find_agent_loom_root() / ".runtime"

    fallback: Optional[Path] = None
    if runtime_root.exists():
        for viz_file in runtime_root.glob("*/" + VIZ_FILENAME):
            data = read_viz_state(viz_file)
            if find_supervisor_name(data) is not None:
                return viz_file
            if fallback is None:
                fallback = viz_file

    if fallback is not None:
        return fallback

    return viz_output_path(get_runtime_agent_path())


def is_filtered_tool(tool_name: str) -> bool:
    """Return True if this tool should be filtered from visualization events."""
    return tool_name in FILTERED_TOOLS
