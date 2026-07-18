"""Shared utilities for agent-recall-with-files hook scripts."""

from __future__ import annotations

import json
import os
import re
import shutil
import sys
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# Runtime file names
# ---------------------------------------------------------------------------

CONTEXT_FILE = "context.md"
TRACE_FILE = "trace.md"
INSIGHTS_FILE = "insights.md"

LEGACY_ROOT_FILES = (
    "task_plan.md",
    "findings.md",
    "progress.md",
    "trace.md",
    "insights.md",
    "context.md",
)

# How many tail lines to inject into agent_context during PreToolUse.
PRE_TOOL_TRACE_LINES = 20
PRE_TOOL_INSIGHTS_LINES = 30

# When insights.md exceeds this many lines, summarize at TaskStart.
MAX_INSIGHTS_LINES = 80
# After summarization, keep the most recent N lines verbatim.
INSIGHTS_RECENT_KEEP = 30

HOOK_TAG = "[agent-recall-with-files]"
HOOK_INPUT_SCHEMA_VERSION = 1
_hook_context_cache: dict[str, Any] | None = None


# ---------------------------------------------------------------------------
# Path helpers
# ---------------------------------------------------------------------------

def templates_dir() -> Path:
    """Return the path to the templates directory."""
    return Path(__file__).resolve().parent.parent / "templates"


def _find_agent_loom_root() -> Path:
    """Derive the AgentLoom project root directory.

    Resolution order:
    1. The versioned Hook stdin payload's ``project_root`` field.
    2. ``$AGENT_LOOM_RUNTIME_ROOT`` (test-only helper).
    3. Walk upward from ``common.py``'s own location and look for
       ``config/llm.yaml`` — the globally unique AgentLoom root marker.
       This works regardless of how deeply the Bundle is nested.
    4. Fall back to ``pyproject.toml`` detection.
    5. Fall back to the current working directory.
    """
    project_root = get_hook_context().get("project_root")
    if isinstance(project_root, str) and project_root.strip():
        return Path(project_root).expanduser().resolve()

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

    # Fall back: CWD (keeps backward compatibility for subprocess tests).
    return Path.cwd()


def get_runtime_agent_path() -> str:
    """Resolve the hierarchical path from the versioned Hook payload.

    Falls back to ``agent_name`` then ``"default"``.  The runtime path
    may contain ``/`` separators (e.g. ``parent/child``) so that .runtime
    directories nest under the parent agent.
    """
    value = get_hook_context().get("runtime_agent_path")
    return value.strip() if isinstance(value, str) and value.strip() else get_agent_name()


def runtime_dir(agent_name: str) -> Path:
    """Return ``<agent_loom_root>/.runtime/<agent_name>`` as an absolute path.

    The runtime directory is always located under the AgentLoom project root,
    regardless of the current working directory.  This prevents ``.runtime/``
    from being accidentally created inside the Hook Bundle directory.
    """
    return _find_agent_loom_root() / ".runtime" / agent_name


def read_template(name: str, title: str) -> str:
    """Read a template file, falling back to a simple heading."""
    path = templates_dir() / name
    if path.exists():
        return path.read_text(encoding="utf-8")
    return f"# {title}\n"


def remove_path(path: Path) -> None:
    """Safely delete a file or directory."""
    if not path.exists():
        return
    if path.is_dir() and not path.is_symlink():
        shutil.rmtree(path)
    else:
        path.unlink()


# ---------------------------------------------------------------------------
# File reading helpers
# ---------------------------------------------------------------------------

def tail(path: Path, lines: int) -> str:
    """Return the last *lines* lines of a file."""
    if not path.exists():
        return ""
    content = path.read_text(encoding="utf-8", errors="replace").splitlines()
    if not content:
        return ""
    return "\n".join(content[-lines:]).strip()


def read_full(path: Path) -> str:
    """Return the full content of a file, or empty string if missing."""
    if not path.exists():
        return ""
    return path.read_text(encoding="utf-8", errors="replace").strip()


# ---------------------------------------------------------------------------
# Insights summarization
# ---------------------------------------------------------------------------

def summarize_insights(path: Path) -> None:
    """Compress *insights.md* when it exceeds ``MAX_INSIGHTS_LINES``.

    Strategy:
    - Keep the header block (lines before the first ``## `` section or
      the first tagged entry ``- [``).
    - Split remaining entries into archive (older) and recent (newest).
    - Deduplicate archive entries (exact match).
    - Write back with ``## Archive`` and ``## Recent`` sections.
    """
    if not path.exists():
        return
    content = path.read_text(encoding="utf-8", errors="replace")
    all_lines = content.splitlines()
    if len(all_lines) <= MAX_INSIGHTS_LINES:
        return

    # Separate header block from entry lines.
    header_lines: list[str] = []
    entry_lines: list[str] = []
    in_entries = False
    for line in all_lines:
        if not in_entries:
            # Detect start of actual entries.
            if line.startswith("- [") or line.startswith("## Log") or line.startswith("## Archive") or line.startswith("## Recent"):
                in_entries = True
                entry_lines.append(line)
            else:
                header_lines.append(line)
        else:
            entry_lines.append(line)

    if not entry_lines:
        return  # Nothing to summarize.

    # Filter out section headers from entry lines for dedup.
    pure_entries: list[str] = []
    for line in entry_lines:
        stripped = line.strip()
        if stripped.startswith("## "):
            continue  # Skip old section headers.
        if stripped:
            pure_entries.append(line)

    if len(pure_entries) <= INSIGHTS_RECENT_KEEP:
        return  # Not enough to warrant splitting.

    # Split into archive and recent.
    archive = pure_entries[:-INSIGHTS_RECENT_KEEP]
    recent = pure_entries[-INSIGHTS_RECENT_KEEP:]

    # Deduplicate archive (preserve order, keep last occurrence).
    seen: set[str] = set()
    deduped_archive: list[str] = []
    for line in reversed(archive):
        key = line.strip()
        if key not in seen:
            seen.add(key)
            deduped_archive.append(line)
    deduped_archive.reverse()

    # Reassemble.
    result_lines = header_lines + ["", "## Archive", ""]
    result_lines.extend(deduped_archive)
    result_lines += ["", "## Recent", ""]
    result_lines.extend(recent)
    result_lines.append("")  # Trailing newline.

    path.write_text("\n".join(result_lines), encoding="utf-8")


# ---------------------------------------------------------------------------
# Runtime path normalization
# ---------------------------------------------------------------------------

def normalize_runtime_aliases(agent_name: str, value: str) -> str:
    """Rewrite ``.runtime/<any>/`` references to use *agent_name*."""
    root = f".runtime/{agent_name}"
    value = re.sub(r"\.runtime/[^/\s\"']+/trace\.md", f"{root}/trace.md", value)
    value = re.sub(r"\.runtime/[^/\s\"']+/insights\.md", f"{root}/insights.md", value)
    value = re.sub(r"\.runtime/[^/\s\"']+/context\.md", f"{root}/context.md", value)
    # Legacy file names — rewrite to new names.
    value = re.sub(r"\.runtime/[^/\s\"']+/progress\.md", f"{root}/trace.md", value)
    value = re.sub(r"\.runtime/[^/\s\"']+/findings\.md", f"{root}/insights.md", value)
    # Generic directory reference.
    value = re.sub(r"\.runtime/[^/\s\"']+/", f"{root}/", value)
    value = re.sub(r"\.runtime/[^/\s\"']+(?=(?:[\s\"']|$|&&|;|\|\|))", root, value)
    return value


def normalize_tool_input(agent_name: str, tool_input: dict[str, Any]) -> dict[str, Any] | None:
    """Return a modified copy of *tool_input* if any runtime paths were rewritten, else ``None``."""
    if not tool_input:
        return None

    updated = dict(tool_input)
    changed = False

    for key in ("file_path", "file", "path"):
        raw = updated.get(key)
        if not isinstance(raw, str) or ".runtime/" not in raw:
            continue
        norm = normalize_runtime_aliases(agent_name, raw)
        if norm != raw:
            updated[key] = norm
            changed = True

    commands = updated.get("commands")
    if isinstance(commands, list):
        new_cmds: list[Any] = []
        cmds_changed = False
        for cmd in commands:
            if isinstance(cmd, str) and ".runtime/" in cmd:
                norm_cmd = normalize_runtime_aliases(agent_name, cmd)
                new_cmds.append(norm_cmd)
                cmds_changed = cmds_changed or norm_cmd != cmd
            else:
                new_cmds.append(cmd)
        if cmds_changed:
            updated["commands"] = new_cmds
            changed = True

    return updated if changed else None


# ---------------------------------------------------------------------------
# Versioned Hook stdin helpers
# ---------------------------------------------------------------------------

def get_agent_name() -> str:
    """Resolve the active agent name from the Hook payload."""
    value = get_hook_context().get("agent_name")
    return value.strip() if isinstance(value, str) and value.strip() else "default"


def get_tool_name() -> str:
    """Resolve the tool name from the Hook payload."""
    value = get_hook_context().get("tool_name")
    return value.strip() if isinstance(value, str) and value.strip() else "unknown"


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


def get_tool_input() -> dict[str, Any]:
    """Extract ``tool_input`` from the hook context."""
    ti = get_hook_context().get("tool_input")
    return ti if isinstance(ti, dict) else {}


def get_task_id() -> str:
    """Return the root task identifier from the Hook payload."""
    value = get_hook_context().get("task_id")
    return value.strip() if isinstance(value, str) and value.strip() else ""


def output(result: dict[str, Any]) -> None:
    """Print a JSON result to stdout (consumed by the shell executor)."""
    print(json.dumps(result, ensure_ascii=False))


# ---------------------------------------------------------------------------
# Step number (from framework via hook context)
# ---------------------------------------------------------------------------

def get_step_number() -> int:
    """Read ``step_number`` from the versioned Hook payload.

    The smolagents framework maintains ``self.step_number`` on the agent
    instance and the active Hook Run includes it in subprocess stdin. Returns
    zero for lifecycle events where no model step exists.
    """
    value = get_hook_context().get("step_number", 0)
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else 0


# ---------------------------------------------------------------------------
# Template detection
# ---------------------------------------------------------------------------

TEMPLATE_SIGNATURES = {
    CONTEXT_FILE: "(What is this task trying to achieve?)",
    TRACE_FILE: "[YYYY-MM-DD HH:MM:SS] Task started.",
    INSIGHTS_FILE: "Be specific and actionable. Not vague impressions.",
}

# Maximum size in characters for a file to be considered "template only".
_TEMPLATE_MAX_SIZE = 500


def is_template_only(path: Path, file_type: str) -> bool:
    """Check if a runtime file still contains only template content.

    Returns True if the file is missing, empty, or contains a known
    template signature and is shorter than ``_TEMPLATE_MAX_SIZE`` chars.
    """
    if not path.exists():
        return True
    content = path.read_text(encoding="utf-8", errors="replace").strip()
    if not content:
        return True
    sig = TEMPLATE_SIGNATURES.get(file_type, "")
    return bool(sig) and sig in content and len(content) < _TEMPLATE_MAX_SIZE


# ---------------------------------------------------------------------------
# Write tracker (freshness detection)
# ---------------------------------------------------------------------------

TRACKED_FILES = [TRACE_FILE, CONTEXT_FILE, INSIGHTS_FILE]

# Staleness thresholds (in steps since last write).
STALENESS_CONFIG = {
    TRACE_FILE: {
        "gentle_after": 4,     # trace should be updated frequently
        "urgent_after": 7,
    },
    CONTEXT_FILE: {
        "gentle_after": 6,     # context changes less frequently
        "urgent_after": 10,
    },
    INSIGHTS_FILE: {
        "gentle_after": None,  # insights have no staleness enforcement
        "urgent_after": None,  # only emptiness check at subtask end
    },
}

# Minimum gap between reminders (prevents spamming).
TURNS_BETWEEN_REMINDERS = 3


def load_write_tracker(rd: Path) -> dict:
    """Load the write tracker JSON, or create default."""
    tracker_file = rd / ".write_tracker.json"
    if tracker_file.exists():
        try:
            return json.loads(tracker_file.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            pass
    tracker: dict[str, Any] = {"last_reminded_at_step": 0}
    for fname in TRACKED_FILES:
        tracker[fname] = {"last_mtime": 0, "last_written_at_step": 0}
    return tracker


def save_write_tracker(rd: Path, tracker: dict) -> None:
    """Persist the write tracker JSON."""
    tracker_file = rd / ".write_tracker.json"
    tracker_file.write_text(
        json.dumps(tracker, indent=2), encoding="utf-8",
    )


def detect_writes_and_update(rd: Path, tracker: dict, step: int) -> dict:
    """Compare current file mtimes against tracker; update if changed.

    Args:
        rd: Runtime directory path.
        tracker: Write tracker dict (mutated in-place on detection).
        step: Current step_number from smolagents framework.

    Returns:
        Dict of ``{filename: stale_steps}`` for each tracked file.
        * ``stale_steps = -1``: file is still empty template (never written).
        * ``stale_steps = 0``: just written this step (fresh).
        * ``stale_steps = N``: N steps since last write (stale).
    """
    staleness: dict[str, int] = {}
    for fname in TRACKED_FILES:
        fpath = rd / fname
        entry = tracker.get(fname, {"last_mtime": 0, "last_written_at_step": 0})

        if not fpath.exists():
            staleness[fname] = -1
            continue

        current_mtime = os.path.getmtime(str(fpath))

        if is_template_only(fpath, fname):
            staleness[fname] = -1
            continue

        if current_mtime != entry["last_mtime"]:
            # File was modified since last check.
            entry["last_mtime"] = current_mtime
            entry["last_written_at_step"] = step
            tracker[fname] = entry
            staleness[fname] = 0
        else:
            last_at = entry.get("last_written_at_step", 0)
            staleness[fname] = step - last_at if last_at > 0 else -1

    return staleness
