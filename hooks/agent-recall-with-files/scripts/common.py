"""Shared utilities for agent-recall-with-files hook scripts."""

from __future__ import annotations

import json
import os
import sys
from collections import Counter
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# Runtime file names
# ---------------------------------------------------------------------------

CONTEXT_FILE = "context.md"
TRACE_FILE = "trace.md"
INSIGHTS_FILE = "insights.md"

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


def _required_payload_path(field: str) -> Path:
    value = get_hook_context().get(field)
    value = value.strip() if isinstance(value, str) else ""
    if not value:
        raise RuntimeError(f"{field} was not injected by AgentLoom RuntimeContext")
    return Path(value)


def task_workspace_dir() -> Path:
    """Return the exact task workspace injected by RuntimeContext."""

    return _required_payload_path("agent_task_workspace")


def project_root_dir() -> Path:
    """Return the project root carried by the validated Hook payload."""

    return _required_payload_path("project_root")


def persistent_insights_path() -> Path:
    """Return the exact cross-task insights path injected by RuntimeContext."""

    return _required_payload_path("agent_insights_path")


def read_template(name: str, title: str) -> str:
    """Read a template file, falling back to a simple heading."""
    path = templates_dir() / name
    if path.exists():
        return path.read_text(encoding="utf-8")
    return f"# {title}\n"


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
    """Bound *insights.md* when it exceeds ``MAX_INSIGHTS_LINES``.

    Strategy:
    - Keep the header block (lines before the first ``## `` section or
      the first tagged entry ``- [``).
    - Keep the newest entries verbatim.
    - Replace older entries with a deterministic archive summary grouped by tag.
    - Keep the final file under the configured line bound.
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

    # Split into archive and recent. Archive details are intentionally replaced
    # by a bounded index: old entries remain countable by type while recent,
    # actionable knowledge stays verbatim.
    archive = pure_entries[:-INSIGHTS_RECENT_KEEP]
    recent = pure_entries[-INSIGHTS_RECENT_KEEP:]

    tag_counts: Counter[str] = Counter()
    for line in archive:
        tag = "untagged"
        for candidate in ("pitfall", "decision", "fact", "dependency", "perf", "config"):
            if f"[{candidate}]" in line:
                tag = candidate
                break
        tag_counts[tag] += 1

    archive_summary = [
        f"- Older entries compacted: {len(archive)} total; "
        + ", ".join(f"{tag}={tag_counts[tag]}" for tag in sorted(tag_counts))
        + "."
    ]

    # Reassemble.
    result_lines = header_lines + ["", "## Archive", ""]
    result_lines.extend(archive_summary)
    result_lines += ["", "## Recent", ""]
    result_lines.extend(recent)
    result_lines.append("")  # Trailing newline.

    path.write_text("\n".join(result_lines), encoding="utf-8")


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


def load_write_tracker(task_workspace: Path) -> dict:
    """Load the write tracker JSON, or create default."""
    tracker_file = task_workspace / ".write_tracker.json"
    if tracker_file.exists():
        try:
            return json.loads(tracker_file.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            pass
    tracker: dict[str, Any] = {"last_reminded_at_step": 0}
    for fname in TRACKED_FILES:
        tracker[fname] = {"last_mtime": 0, "last_written_at_step": 0}
    return tracker


def save_write_tracker(task_workspace: Path, tracker: dict) -> None:
    """Persist the write tracker JSON."""
    tracker_file = task_workspace / ".write_tracker.json"
    tracker_file.write_text(
        json.dumps(tracker, indent=2), encoding="utf-8",
    )


def detect_writes_and_update(
    task_workspace: Path,
    tracker: dict,
    step: int,
    *,
    persistent_insights: Path,
) -> dict:
    """Compare current file mtimes against tracker; update if changed.

    Args:
        task_workspace: Current agent's task-scoped workspace.
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
        fpath = (
            persistent_insights
            if fname == INSIGHTS_FILE
            else task_workspace / fname
        )
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
