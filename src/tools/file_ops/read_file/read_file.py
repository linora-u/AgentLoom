"""
ReadFile tool implementation.

Reads a file from the local filesystem with line-numbered output (cat -n
format), device-file blocking, binary detection, and size limiting.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Union

from src.lib.logging import get_logger

from .._safety import (
    DEFAULT_READ_LIMIT,
    MAX_READ_SIZE_BYTES,
    is_binary_file,
    is_device_file,
    normalize_path,
    validate_file_access,
)
from .._read_file_state import get_read_file_state

logger = get_logger(__name__)


def read_file(
    file_path: str,
    offset: int = 1,
    limit: int = 0,
) -> str:
    """Reads a file from the local filesystem.

    The file_path parameter must be an absolute path, not a relative path.
    By default it reads up to 2000 lines starting from the beginning of
    the file.  You can optionally specify a line offset and limit
    (especially handy for long files), but it is recommended to read the
    whole file first by not providing these parameters.
    Results are returned using ``cat -n`` format, with line numbers
    starting at 1.
    This tool can only read text files, not directories or binary files.
    To list a directory use the ``list_directory`` or ``glob_search`` tool.

    Args:
        file_path: Absolute path to the file to read.
        offset: Line number to start reading from (1-based, default 1).
        limit: Maximum number of lines to read.  0 means the default
            (2000 lines).

    Returns:
        File content with line numbers in ``cat -n`` format, plus a
        metadata footer showing total lines, displayed range, and
        whether the output was truncated.

    Raises:
        ValueError: If the path is a directory, device file, or binary.
        FileNotFoundError: If the file does not exist.
        PermissionError: If insufficient permissions.
        OSError: On other I/O errors.

    Examples:
        >>> read_file("/home/user/app.py")
        >>> read_file("/home/user/large.py", offset=100, limit=50)
    """
    # -- Validate inputs ---------------------------------------------------
    if not file_path or not file_path.strip():
        raise ValueError("file_path is required and cannot be empty")
    file_path = file_path.strip()

    if offset < 1:
        raise ValueError("offset must be >= 1")

    if limit < 0:
        raise ValueError("limit must be >= 0")

    effective_limit = limit if limit > 0 else DEFAULT_READ_LIMIT

    # -- Path access control -----------------------------------------------
    validate_file_access(file_path, "read", tool_name="read_file")

    # -- Resolve and guard path --------------------------------------------
    path = normalize_path(file_path)

    if is_device_file(path):
        raise ValueError(
            f"Cannot read device/special file: {file_path}"
        )

    if not path.exists():
        raise FileNotFoundError(f"File not found: {file_path}")

    if path.is_dir():
        raise ValueError(
            f"Path is a directory, not a file: {file_path}. "
            "Use list_directory or glob_search instead."
        )

    if not path.is_file():
        raise ValueError(f"Path is not a regular file: {file_path}")

    if is_binary_file(path):
        raise ValueError(
            f"Cannot read binary file: {file_path}. "
            "Only text files are supported."
        )

    # -- Size guard --------------------------------------------------------
    try:
        file_size = path.stat().st_size
    except OSError as exc:
        raise OSError(f"Cannot stat file '{file_path}': {exc}") from exc

    if file_size > MAX_READ_SIZE_BYTES:
        raise ValueError(
            f"File '{file_path}' is too large "
            f"({file_size / 1024:.1f} KB, max {MAX_READ_SIZE_BYTES / 1024:.0f} KB). "
            "Use offset/limit to read a portion."
        )

    # -- Dedup check -------------------------------------------------------
    state = get_read_file_state()
    stub = state.check_dedup(path, offset, effective_limit)
    if stub is not None:
        return stub

    # -- Read file ---------------------------------------------------------
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            all_lines = fh.readlines()
    except PermissionError as exc:
        raise PermissionError(
            f"Permission denied reading '{file_path}': {exc}"
        ) from exc
    except OSError as exc:
        raise OSError(f"Error reading '{file_path}': {exc}") from exc

    total_lines = len(all_lines)

    # -- Slice by offset / limit -------------------------------------------
    start_idx = offset - 1  # convert to 0-based
    if start_idx >= total_lines:
        # Record in state even if nothing to show
        content_for_cache = "".join(all_lines)
        mtime_ns = os.stat(path).st_mtime_ns
        state.set(path, content_for_cache, mtime_ns, offset, effective_limit)
        if total_lines == 0:
            return (
                f"\n[Total lines: 0 | File is empty]"
            )
        return (
            f"File '{file_path}' has {total_lines} lines. "
            f"Offset {offset} is beyond the end of the file."
        )

    end_idx = min(start_idx + effective_limit, total_lines)
    selected = all_lines[start_idx:end_idx]
    truncated = end_idx < total_lines and effective_limit < total_lines

    # -- Format with line numbers (cat -n) ---------------------------------
    formatted_lines: list[str] = []
    for i, line in enumerate(selected, start=offset):
        # cat -n format: right-justified 6-char number, tab, content
        formatted_lines.append(f"{i:6d}\t{line.rstrip()}")

    body = "\n".join(formatted_lines)

    # -- Metadata footer ---------------------------------------------------
    meta_parts: list[str] = [
        f"[Total lines: {total_lines}",
        f"Showing: {offset}-{end_idx}",
    ]
    if truncated:
        remaining = total_lines - end_idx
        meta_parts.append(f"Truncated: {remaining} lines remaining")
    meta_parts[-1] += "]"
    footer = " | ".join(meta_parts)

    result = f"{body}\n\n{footer}"

    # -- Record in state ---------------------------------------------------
    content_for_cache = "".join(all_lines)
    mtime_ns = os.stat(path).st_mtime_ns
    state.set(path, content_for_cache, mtime_ns, offset, effective_limit)

    logger.debug(
        "read_file %s (offset=%d, limit=%d, total=%d)",
        file_path, offset, effective_limit, total_lines,
    )
    return result
