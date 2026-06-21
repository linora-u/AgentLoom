"""
WriteFile tool implementation.

Creates new files or completely overwrites existing ones.
Includes staleness detection via ReadFileState.
"""

from __future__ import annotations

from pathlib import Path

from src.lib.logging import get_logger

from .._safety import normalize_path, validate_file_access
from .._read_file_state import get_read_file_state

logger = get_logger(__name__)


def write_file(
    file_path: str,
    content: str,
) -> str:
    """Writes a file to the local filesystem.

    This tool will overwrite the existing file if there is one at the
    provided path.  If the file already exists, you should read it first
    with read_file before overwriting — this tool will check that you
    have done so.
    Prefer the edit_file tool for modifying existing files — it only sends
    the diff.  Use this tool to create new files or for complete rewrites.
    Parent directories are created automatically if they do not exist.

    Args:
        file_path: Absolute path to the file to write.
        content: The complete file content to write.

    Returns:
        A success message indicating whether the file was created or
        updated, including the number of characters written.

    Raises:
        ValueError: If file_path is empty or points to a directory.

    Examples:
        >>> write_file("/tmp/hello.py", "print('hello')")
        'Created /tmp/hello.py (14 chars)'

        >>> write_file("/tmp/existing.py", "# rewritten")
        'Updated /tmp/existing.py (11 chars)'
    """
    # -- Validate inputs ---------------------------------------------------
    if not file_path or not file_path.strip():
        raise ValueError("file_path is required and cannot be empty")
    file_path = file_path.strip()

    if content is None:
        raise ValueError("content is required and cannot be None")

    # -- Path access control -----------------------------------------------
    validate_file_access(file_path, "write", tool_name="write_file")

    path = normalize_path(file_path)

    if path.exists() and path.is_dir():
        raise ValueError(f"Path is a directory, not a file: {file_path}")

    # -- Determine create vs update ----------------------------------------
    is_create = not path.exists()
    state = get_read_file_state()

    # -- Staleness check for existing files --------------------------------
    if not is_create:
        stale_msg = state.check_staleness(path)
        if stale_msg is not None:
            return stale_msg

    # -- Preserve line endings for existing files --------------------------
    if not is_create:
        try:
            existing_bytes = path.read_bytes()
        except (OSError,):
            existing_bytes = None

        if existing_bytes is not None and b"\r\n" in existing_bytes and "\r\n" not in content:
            # File uses CRLF but new content is LF — convert
            content = content.replace("\n", "\r\n")

    # -- Ensure parent directories -----------------------------------------
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        return f"Failed to create directories for '{file_path}': {exc}"

    # -- Write file --------------------------------------------------------
    try:
        path.write_text(content, encoding="utf-8")
    except PermissionError as exc:
        return f"Permission denied writing '{file_path}': {exc}"
    except OSError as exc:
        return f"Failed to write '{file_path}': {exc}"

    # -- Update state cache ------------------------------------------------
    state.update_after_write(path, content)

    action = "Created" if is_create else "Updated"
    logger.debug("write_file %s (%s, %d chars)", file_path, action.lower(), len(content))
    return f"{action} {file_path} ({len(content)} chars)"
