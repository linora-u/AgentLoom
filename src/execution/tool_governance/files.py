"""File access and stale-write policies, independent of any Agent runtime."""
from __future__ import annotations
import os
from pathlib import Path


def mtime_ns(path: str | Path) -> int:
    try:
        return os.stat(path).st_mtime_ns
    except OSError:
        return 0


def check_staleness(path: str | Path, observed_mtime: int | None, observed_content: str | None = None) -> str | None:
    resolved = Path(path).resolve()
    if observed_mtime is None:
        return f"File '{resolved}' has not been read yet. Use read_file first before editing."
    current_mtime = mtime_ns(resolved)
    if current_mtime != observed_mtime:
        # Content comparison fallback (handles cloud-sync timestamp drift)
        try:
            disk_content = resolved.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            disk_content = None

        if disk_content is not None and disk_content == observed_content:
            # Content identical despite mtime difference — safe
            return None

        return (
            f"File '{resolved}' has been modified since the last read. "
            "Read the file again before editing."
        )

    return None

def normalize_path(path: str | Path) -> Path:
    """Expand ``~`` and normalize to a logical absolute path.

    Handles ``~`` expansion and ``..`` resolution.  Does **not** follow
    symlinks (use ``Path.resolve()`` for that).

    Leading/trailing whitespace is stripped so that LLM-generated paths
    like ``' /tmp/foo.txt'`` are handled gracefully.
    """
    expanded = os.path.expanduser(str(path).strip())
    return Path(os.path.abspath(os.path.normpath(expanded)))


def validate_file_access(
    file_path: str,
    operation: str = "read",
    tool_name: str = "read_file",
) -> None:
    """Validate that a file path is within allowed workspace boundaries.

    Calls the unified permissions library to check UNC paths, Windows
    tricks, symlink escapes, and workspace boundary violations.

    Path rules are looked up from ``path_validation`` entries matching
    *tool_name*.  ``include_paths`` / ``exclude_paths`` support ``~``,
    glob (fnmatch), and ``"*"``.  **exclude takes priority over include**.

    Security checks (UNC, Windows tricks) are always enforced.
    Workspace boundary checks are only enforced when the framework
    is properly initialized (i.e., ``C.agent_root`` is available).
    When called outside an agent context (e.g., in unit tests or
    standalone scripts), boundary checks are skipped gracefully.

    Args:
        file_path: The file path to validate.
        operation: ``"read"`` or ``"write"``.
        tool_name: Canonical tool name for rule lookup.

    Raises:
        ValueError: If the path is outside allowed directories or
            fails security checks.
    """
    from agentloom.execution.permissions.path_validation import (
        is_vulnerable_unc_path,
        has_suspicious_windows_pattern,
    )

    if not file_path or not file_path.strip():
        return  # Let downstream handle empty path

    # Always enforce security checks (UNC, Windows tricks)
    raw = file_path[7:] if file_path.startswith("file://") else file_path
    if is_vulnerable_unc_path(raw):
        raise ValueError(f"Access denied: Path '{file_path}' is a UNC/network path")
    if has_suspicious_windows_pattern(raw):
        raise ValueError(
            f"Access denied: Path '{file_path}' contains suspicious Windows path patterns"
        )

    # Workspace boundary check — only when running inside an agent context.
    # When tools are called standalone (unit tests, scripts), skip boundary
    # checks since there is no meaningful workspace to enforce.
    try:
        from agentloom.execution.trace.task_context import get_current_agent_config
        agent_cfg = get_current_agent_config()
        if agent_cfg is None:
            return  # No agent context, skip boundary check
    except Exception:
        return  # Tracing not available, skip boundary check

    from agentloom.execution.permissions import validate_path
    result = validate_path(file_path, operation=operation, tool_name=tool_name)
    if not result.allowed:
        raise ValueError(result.reason)
