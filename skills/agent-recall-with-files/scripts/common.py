"""Shared utilities for agent-recall-with-files hook scripts."""

from __future__ import annotations

import json
import os
import shutil
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

SKILL_TAG = "[agent-recall-with-files]"


# ---------------------------------------------------------------------------
# Path helpers
# ---------------------------------------------------------------------------

def templates_dir() -> Path:
    """Return the path to the templates directory."""
    return Path(__file__).resolve().parent.parent / "templates"


def _required_injected_path(variable: str) -> Path:
    value = os.environ.get(variable, "").strip()
    if not value:
        raise RuntimeError(f"{variable} was not injected by AgentLoom RuntimeContext")
    return Path(value)


def task_workspace_dir() -> Path:
    """Return the exact task workspace injected by RuntimeContext."""

    return _required_injected_path("AGENTLOOM_AGENT_TASK_WORKSPACE")


def persistent_insights_path() -> Path:
    """Return the exact cross-task insights path injected by RuntimeContext."""

    return _required_injected_path("AGENTLOOM_AGENT_INSIGHTS_PATH")


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
# Environment helpers  (shell executor injects these env-vars)
# ---------------------------------------------------------------------------

def get_agent_name() -> str:
    """Resolve agent name from ``$AGENT_NAME``."""
    return os.environ.get("AGENT_NAME", "") or "default"


def get_tool_name() -> str:
    """Resolve tool name from ``$TOOL_NAME``."""
    return os.environ.get("TOOL_NAME", "") or "unknown"


def get_hook_context() -> dict[str, Any]:
    """Load hook context, preferring the temp-file over the env-var.

    The shell executor writes the full JSON payload to a temp file
    (``$HOOK_CONTEXT_JSON_FILE``) and keeps a possibly-truncated copy
    in ``$HOOK_CONTEXT_JSON`` for backward compatibility.  Prefer the
    file when available so that large ``tool_input`` / ``tool_response``
    values are never lost.
    """
    # 1) Try the temp-file first (always contains the full payload).
    json_file = os.environ.get("HOOK_CONTEXT_JSON_FILE", "").strip()
    if json_file:
        try:
            with open(json_file, encoding="utf-8") as fh:
                payload = json.load(fh)
            if isinstance(payload, dict):
                return payload
        except (OSError, json.JSONDecodeError):
            pass  # fall through to env-var

    # 2) Fall back to the env-var (may be truncated for large payloads).
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


# ---------------------------------------------------------------------------
# Step number (from framework via hook context)
# ---------------------------------------------------------------------------

def get_step_number() -> int:
    """Read step_number from hook context (env var or JSON).

    The smolagents framework maintains ``self.step_number`` on the agent
    instance, which is synced to HookManager and propagated to hook
    subprocesses via ``$STEP_NUMBER`` env var and ``HOOK_CONTEXT_JSON``.

    Falls back to 0 if unavailable (e.g. non-tool hooks like TaskCreated).
    """
    val = os.environ.get("STEP_NUMBER", "")
    if val.isdigit():
        return int(val)
    json_str = os.environ.get("HOOK_CONTEXT_JSON", "")
    if json_str:
        try:
            ctx = json.loads(json_str)
            return ctx.get("step_number", 0) or 0
        except (json.JSONDecodeError, KeyError):
            pass
    return 0


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
