"""
Unified path access control library.

Provides a single source of truth for workspace boundary checking,
path validation (UNC, Windows tricks, symlink resolution), and
allowed-directory management.  All tools (file_ops, shell, search)
and the hook layer delegate to this library.

Architecture:
  - Path rules are defined in ``tool_access_control.path_validation``
  - Each rule specifies ``tools``, ``include_paths``, ``exclude_paths``
  - ``include_paths`` / ``exclude_paths`` support ``~``, glob (fnmatch), ``"*"``
  - exclude takes priority over include (security-first)
"""

from .path_validation import (
    PathValidationResult,
    has_suspicious_windows_pattern,
    is_vulnerable_unc_path,
    resolve_symlink_chain,
    validate_path,
)
from .workspace import (
    get_allowed_directories,
    get_rule_exclude_paths,
    get_rule_include_paths,
    get_workspace_root,
    match_path_pattern,
    path_in_allowed_directory,
)

__all__ = [
    "PathValidationResult",
    "get_allowed_directories",
    "get_rule_exclude_paths",
    "get_rule_include_paths",
    "get_workspace_root",
    "has_suspicious_windows_pattern",
    "is_vulnerable_unc_path",
    "match_path_pattern",
    "path_in_allowed_directory",
    "resolve_symlink_chain",
    "validate_path",
]
