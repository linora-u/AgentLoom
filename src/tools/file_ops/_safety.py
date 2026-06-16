"""
Shared safety utilities for file operation tools.

Provides device-file blocking, binary-file detection, path normalization,
and size-limit constants aligned with industry best practices.
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Union

from src.lib.logging import get_logger

logger = get_logger(__name__)

# ---------------------------------------------------------------------------
# Size-limit constants
# ---------------------------------------------------------------------------
MAX_READ_SIZE_BYTES: int = 256 * 1024          # 256 KB pre-read guard
MAX_EDIT_FILE_SIZE: int = 1 * 1024 * 1024 * 1024  # 1 GiB (V8/Bun string limit equivalent)
DEFAULT_READ_LIMIT: int = 2000                  # Default lines to read
MAX_GLOB_RESULTS: int = 100                     # Default glob cap

# ---------------------------------------------------------------------------
# Blocked device / special file paths
# ---------------------------------------------------------------------------
BLOCKED_DEVICE_PATHS: tuple[str, ...] = (
    "/dev/zero",
    "/dev/null",
    "/dev/random",
    "/dev/urandom",
    "/dev/stdin",
    "/dev/stdout",
    "/dev/stderr",
    "/dev/tty",
    "/dev/console",
    "/dev/ptmx",
    "/dev/fd/0",
    "/dev/fd/1",
    "/dev/fd/2",
)

# Pattern for /proc/*/fd/[0-2] style paths
_PROC_FD_PATTERN = re.compile(r"^/proc/\d+/fd/[012]$")


def is_device_file(path: Union[str, Path]) -> bool:
    """Check whether *path* refers to a blocked device or special file.

    Returns ``True`` for paths in ``BLOCKED_DEVICE_PATHS`` and for
    ``/proc/<pid>/fd/0-2`` aliases.  Checks both the raw path string
    and the resolved path to catch symlinks like ``/dev/stdout``.
    """
    raw = str(path)
    # Check raw path first (before resolution, which might fail)
    if raw in BLOCKED_DEVICE_PATHS:
        return True
    if _PROC_FD_PATTERN.match(raw):
        return True
    # Also check resolved path (e.g. /dev/stdout -> /proc/self/fd/1)
    try:
        normalized = str(Path(path).resolve())
    except OSError:
        return False
    if normalized in BLOCKED_DEVICE_PATHS:
        return True
    if _PROC_FD_PATTERN.match(normalized):
        return True
    return False


# ---------------------------------------------------------------------------
# Binary file detection via magic bytes
# ---------------------------------------------------------------------------
# First 8 bytes of common binary formats.
_BINARY_MAGIC: tuple[bytes, ...] = (
    b"\x7fELF",            # ELF (Linux executables/libraries)
    b"MZ",                 # PE / DOS executable
    b"\xfe\xed\xfa",      # Mach-O (macOS, both endians)
    b"\xcf\xfa\xed\xfe",  # Mach-O 64-bit
    b"\xca\xfe\xba\xbe",  # Mach-O universal / Java class
    b"PK\x03\x04",        # ZIP / JAR / DOCX / XLSX
    b"\x1f\x8b",          # gzip
    b"BZh",               # bzip2
    b"\xfd7zXZ\x00",      # xz
    b"\x89PNG",            # PNG image
    b"\xff\xd8\xff",      # JPEG image
    b"GIF8",               # GIF image
    b"RIFF",               # RIFF container (WAV / AVI / WebP)
    b"\x00\x00\x01\x00",  # ICO
    b"\x00asm",            # WebAssembly
    b"SQLite format 3",   # SQLite database
)

# Extensions that are always considered binary (skip magic check).
_BINARY_EXTENSIONS: frozenset[str] = frozenset({
    ".exe", ".dll", ".so", ".dylib", ".o", ".obj", ".a", ".lib",
    ".pyc", ".pyo", ".class",
    ".zip", ".tar", ".gz", ".bz2", ".xz", ".7z", ".rar",
    ".png", ".jpg", ".jpeg", ".gif", ".bmp", ".ico", ".webp",
    ".mp3", ".mp4", ".avi", ".mov", ".mkv", ".wav", ".flac",
    ".woff", ".woff2", ".ttf", ".otf", ".eot",
    ".pdf",
    ".wasm",
    ".sqlite", ".db",
})


def is_binary_file(path: Union[str, Path]) -> bool:
    """Heuristic check whether *path* is a binary file.

    Uses extension-based fast-path and magic-byte detection.
    Returns ``False`` if the file does not exist or cannot be read.
    """
    p = Path(path)

    # Fast-path: extension check
    if p.suffix.lower() in _BINARY_EXTENSIONS:
        return True

    # Magic byte check
    try:
        with open(p, "rb") as fh:
            header = fh.read(16)
    except (OSError, PermissionError):
        return False

    for magic in _BINARY_MAGIC:
        if header.startswith(magic):
            return True

    # Heuristic: check for null bytes in first 8KB (common binary indicator)
    try:
        with open(p, "rb") as fh:
            chunk = fh.read(8192)
        if b"\x00" in chunk:
            return True
    except (OSError, PermissionError):
        return False

    return False


# ---------------------------------------------------------------------------
# Path normalization
# ---------------------------------------------------------------------------

def normalize_path(path: Union[str, Path]) -> Path:
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
    from src.lib.permissions.path_validation import (
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
        from src.trace.task_context import get_current_agent_config
        agent_cfg = get_current_agent_config()
        if agent_cfg is None:
            return  # No agent context, skip boundary check
    except Exception:
        return  # Tracing not available, skip boundary check

    from src.lib.permissions import validate_path
    result = validate_path(file_path, operation=operation, tool_name=tool_name)
    if not result.allowed:
        raise ValueError(result.reason)
