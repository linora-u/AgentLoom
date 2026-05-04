"""
Unified path validation for all tools.

Provides security checks (UNC, Windows tricks, symlink resolution) and
workspace boundary validation.  This is the single canonical implementation
— all tools (file_ops, shell, search) and the hook layer delegate here.
"""

from __future__ import annotations

import os
import re
import stat
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional

from src.lib.logging import get_logger

logger = get_logger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
_SYMLINK_MAX_DEPTH = 40

# 8 regex patterns covering all UNC path variations
_UNC_PATTERNS = [
    re.compile(r"\\\\[^\s\\/]+(?:@(?:\d+|ssl))?(?:[\\/]|$|\s)", re.IGNORECASE),
    re.compile(r"(?<!:)//[^\s\\/]+(?:@(?:\d+|ssl))?(?:[\\/]|$|\s)", re.IGNORECASE),
    re.compile(r"/\\{2,}[^\s\\/]"),
    re.compile(r"\\{2,}/[^\s\\/]"),
    re.compile(r"@SSL@\d+", re.IGNORECASE),
    re.compile(r"DavWWWRoot", re.IGNORECASE),
    re.compile(r"^\\\\(\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3})[\\/]"),
    re.compile(r"^//(\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3})[\\/]"),
]


# ---------------------------------------------------------------------------
# Result type
# ---------------------------------------------------------------------------
@dataclass
class PathValidationResult:
    """Result of a path validation check."""
    allowed: bool
    reason: str = ""
    resolved_path: Optional[Path] = None


# ---------------------------------------------------------------------------
# Security detection functions
# ---------------------------------------------------------------------------
def is_vulnerable_unc_path(path_str: str) -> bool:
    """Detect UNC paths that could leak credentials or access network resources.

    Checks all platforms (NTFS can be mounted on Linux via ntfs-3g/cifs).
    """
    for pattern in _UNC_PATTERNS:
        if pattern.search(path_str):
            return True
    return False


def has_suspicious_windows_pattern(path_str: str) -> bool:
    """Detect Windows path canonicalization tricks that bypass deny rules.

    Categories: NTFS ADS, 8.3 short names, long-path prefixes, trailing
    dots/spaces, DOS device names, triple-dot traversal.
    """
    # 1. NTFS Alternate Data Streams (colon after drive letter position)
    colon_idx = path_str.find(":", 2)
    if colon_idx != -1:
        return True

    # 2. 8.3 short names (~digit pattern)
    if re.search(r"~\d", path_str):
        return True

    # 3. Long-path prefixes that suppress API normalization
    if path_str.startswith("\\\\?\\") or path_str.startswith("\\\\.\\"):
        return True
    if path_str.startswith("//?/") or path_str.startswith("//./"):
        return True

    # 4. Trailing dots or spaces (Windows strips them during resolution)
    if re.search(r"[.\s]+$", path_str):
        return True

    # 5. DOS device names as file extension
    if re.search(r"\.(CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])$", path_str, re.IGNORECASE):
        return True

    # 6. Triple-or-more consecutive dots as path component
    if re.search(r"(^|/|\\)\.{3,}(/|\\|$)", path_str):
        return True

    return False


def resolve_symlink_chain(path_str: str) -> List[str]:
    """Follow the symlink chain and return all intermediate target paths.

    Stops at real files, dangling links, circular links, or special files
    (FIFO, socket, device).  Max depth = ``_SYMLINK_MAX_DEPTH``.
    """
    paths: List[str] = [path_str]
    visited: set = set()
    current = path_str

    for _ in range(_SYMLINK_MAX_DEPTH):
        try:
            p = Path(current)
            if not p.exists():
                break
            st = p.lstat()
            # Skip special files that could block
            if stat.S_ISFIFO(st.st_mode) or stat.S_ISSOCK(st.st_mode):
                break
            if stat.S_ISCHR(st.st_mode) or stat.S_ISBLK(st.st_mode):
                break
            if not p.is_symlink():
                break
            target = os.readlink(current)
            if not os.path.isabs(target):
                target = str(Path(current).parent / target)
            target = str(Path(target).resolve())
            if target in visited:
                break  # circular
            visited.add(target)
            paths.append(target)
            current = target
        except OSError:
            break

    return paths


# ---------------------------------------------------------------------------
# Core validation
# ---------------------------------------------------------------------------
def validate_path(
    path_str: str,
    operation: str = "read",
    tool_name: Optional[str] = None,
    extra_include: Optional[List[str]] = None,
    extra_exclude: Optional[List[str]] = None,
) -> PathValidationResult:
    """Validate a file path against workspace boundaries and security rules.

    This is the unified validation entry point used by all tools.

    Steps:
    1. Strip ``file://`` protocol prefix
    2. UNC path detection → block
    3. Windows special pattern detection → block
    4. Tilde expansion
    5. Resolve to absolute path
    6. Symlink chain resolution + intermediate target validation
    7. Workspace boundary check (include_paths from rules)
    8. Exclude paths check (exclude_paths from rules)

    Path rules come from ``path_validation`` entries matching *tool_name*.
    ``include_paths`` / ``exclude_paths`` support ``~``, glob (fnmatch),
    and ``"*"`` (match all).  **exclude takes priority over include**.

    Args:
        path_str: The path string to validate.
        operation: The operation type (``"read"`` or ``"write"``).
        tool_name: Canonical tool name used to look up path_validation rules.
        extra_include: Additional allowed directories from caller context.
        extra_exclude: Additional excluded directories from caller context.

    Returns:
        PathValidationResult with ``allowed=True/False`` and reason.
    """
    # Lazy import to avoid circular dependency at module load time
    from .workspace import (
        get_allowed_directories,
        get_rule_exclude_paths,
        get_workspace_root,
        match_path_pattern,
        path_in_allowed_directory,
    )

    if not path_str or not path_str.strip():
        return PathValidationResult(
            allowed=False,
            reason="Path is empty",
        )

    # Step 1: Strip file:// protocol
    if path_str.startswith("file://"):
        path_str = path_str[7:]

    # Step 2: UNC path detection (fast reject, all platforms)
    if is_vulnerable_unc_path(path_str):
        return PathValidationResult(
            allowed=False,
            reason=f"Access denied: Path '{path_str}' is a UNC/network path",
        )

    # Step 3: Windows special pattern detection (fast reject)
    if has_suspicious_windows_pattern(path_str):
        return PathValidationResult(
            allowed=False,
            reason=f"Access denied: Path '{path_str}' contains suspicious Windows path patterns",
        )

    # Step 4: Tilde expansion
    path_str = os.path.expanduser(path_str)

    # Step 5: Resolve to absolute path
    try:
        path_obj = Path(path_str)
        if not path_obj.is_absolute():
            path_obj = Path(os.getcwd()).resolve() / path_obj
        path_obj = path_obj.resolve()
    except (OSError, ValueError) as exc:
        return PathValidationResult(
            allowed=False,
            reason=f"Path resolution error: {exc}",
        )

    # Step 6: Symlink chain resolution
    all_paths_to_verify = resolve_symlink_chain(str(path_obj))

    # Build allowed directories list from rules
    allowed_dirs = get_allowed_directories(
        tool_name=tool_name,
        extra_include=extra_include,
    )

    # Build exclude paths list from rules
    rule_excludes: List[str] = []
    if tool_name:
        rule_excludes = get_rule_exclude_paths(tool_name)
    all_excludes = list(rule_excludes)
    if extra_exclude:
        all_excludes.extend([str(e) for e in extra_exclude if e])

    workspace_root = get_workspace_root()

    # Step 8 (pre-check): If exclude is "*", deny everything immediately
    if "*" in all_excludes:
        return PathValidationResult(
            allowed=False,
            reason=f"Access denied: Path '{path_str}' — all paths excluded by wildcard rule",
            resolved_path=path_obj,
        )

    # Step 6+7+8: Check every path in symlink chain
    for check_path_str in all_paths_to_verify:
        try:
            check_path = Path(check_path_str).resolve()
        except (OSError, ValueError):
            return PathValidationResult(
                allowed=False,
                reason=f"Cannot resolve symlink target: {check_path_str}",
            )

        # Step 8: Exclude paths check FIRST (exclude > include, security-first)
        for excl in all_excludes:
            excl_stripped = excl.strip()
            # Glob / pattern matching
            if any(c in excl_stripped for c in ("*", "?", "[")):
                if match_path_pattern(str(check_path), excl_stripped):
                    return PathValidationResult(
                        allowed=False,
                        reason=f"Access denied: Path '{path_str}' matches excluded pattern '{excl}'",
                        resolved_path=check_path,
                    )
                continue
            # Standard prefix matching
            excl_path = Path(os.path.expanduser(excl_stripped))
            if not excl_path.is_absolute():
                excl_full = (workspace_root / excl_path).resolve()
            else:
                excl_full = excl_path.resolve()
            try:
                check_path.relative_to(excl_full)
                return PathValidationResult(
                    allowed=False,
                    reason=f"Access denied: Path '{path_str}' is in excluded directory '{excl}'",
                    resolved_path=check_path,
                )
            except ValueError:
                continue

        # Step 7: Workspace boundary check (include)
        if not path_in_allowed_directory(check_path, allowed_dirs):
            return PathValidationResult(
                allowed=False,
                reason=(
                    f"Access denied: Path '{path_str}' resolves to "
                    f"'{check_path}' which is outside allowed directories"
                ),
                resolved_path=check_path,
            )

    return PathValidationResult(
        allowed=True,
        reason="",
        resolved_path=path_obj,
    )
