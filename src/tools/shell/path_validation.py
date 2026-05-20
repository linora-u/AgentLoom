"""Shell command path boundary validation.

Validates that shell commands only access files/directories within
the allowed workspace boundaries.  Prevents:
- Path traversal (``cd /etc``, ``cat ../../../etc/passwd``)
- Dangerous removal paths (``rm -rf /``)
- Output redirection to system paths (``echo x > /etc/passwd``)
- ``cd`` + write combination attacks

Path boundary configuration is unified under ``tool_access_control``
in ``config/system.yaml``.  Shell-specific settings (dangerous_paths,
block_destructive, allowed_commands, etc.) remain under ``shell_settings``.
"""

import os
import re
from pathlib import Path
from typing import Dict, List, Optional, Set, Callable

from src.lib.config import C
from src.lib.logging import get_logger
from src.lib.permissions.workspace import get_allowed_directories

logger = get_logger(__name__)

# Guidance suffix appended to path violation error messages
_PATH_GUIDANCE = (
    " Use paths within allowed directories, "
    "or use read_file/grep_search tools instead."
)


def _get_shell_config_path(key: str, *, default=None):
    """Read a shell config value, preferring per-agent effective config."""
    try:
        from src.trace import get_current_agent_config
        agent_cfg = get_current_agent_config()
        if isinstance(agent_cfg, dict):
            shell = agent_cfg.get("shell_settings")
            if isinstance(shell, dict) and key in shell:
                return shell[key]
    except Exception:
        pass
    return C.get_nested("shell_settings", key, default=default)


def _audit_path_violation(command: str, message: str, path: str = "") -> None:
    """Write a path violation event to the per-agent shell audit log."""
    try:
        from src.tools.shell.shell_audit_log import get_shell_audit_logger
        audit = get_shell_audit_logger()
        audit.log_path_violation(command=command, message=message, path=path)
    except Exception:
        pass  # Never let audit logging break the validation pipeline


# ---------------------------------------------------------------------------
# Path extraction helpers
# ---------------------------------------------------------------------------

def _filter_out_flags(args: List[str]) -> List[str]:
    """Extract positional (non-flag) arguments, handling POSIX ``--``.

    After ``--``, ALL subsequent arguments are positional, even if
    they start with ``-``.  This prevents attacks like:
        rm -- -/../.secret/config.json
    """
    result: List[str] = []
    after_double_dash = False
    for arg in args:
        if after_double_dash:
            result.append(arg)
        elif arg == '--':
            after_double_dash = True
        elif not arg.startswith('-'):
            result.append(arg)
    return result


def _parse_pattern_command(
    args: List[str],
    flags_with_args: Set[str],
    defaults: Optional[List[str]] = None,
) -> List[str]:
    """Parse grep/rg style commands: pattern then file paths."""
    paths: List[str] = []
    pattern_found = False
    after_double_dash = False

    for i, arg in enumerate(args):
        if arg is None:
            continue
        if not after_double_dash and arg == '--':
            after_double_dash = True
            continue
        if not after_double_dash and arg.startswith('-'):
            flag = arg.split('=')[0]
            if flag in ('-e', '--regexp', '-f', '--file'):
                pattern_found = True
            if flag in flags_with_args and '=' not in arg:
                continue  # next arg is consumed by this flag
            continue
        if not pattern_found:
            pattern_found = True
            continue
        paths.append(arg)

    return paths if paths else (defaults or [])


# ---------------------------------------------------------------------------
# Per-command path extractors
# ---------------------------------------------------------------------------

def _extract_cd(args: List[str]) -> List[str]:
    return [' '.join(args)] if args else [os.path.expanduser('~')]


def _extract_ls(args: List[str]) -> List[str]:
    paths = _filter_out_flags(args)
    return paths if paths else ['.']


def _extract_find(args: List[str]) -> List[str]:
    paths: List[str] = []
    after_double_dash = False
    found_flag = False

    for arg in args:
        if after_double_dash:
            paths.append(arg)
            continue
        if arg == '--':
            after_double_dash = True
            continue
        if arg.startswith('-'):
            if arg in ('-H', '-L', '-P'):
                continue
            found_flag = True
            continue
        if not found_flag:
            paths.append(arg)

    return paths if paths else ['.']


def _extract_grep(args: List[str]) -> List[str]:
    flags = {'-e', '--regexp', '-f', '--file', '--exclude', '--include',
             '--exclude-dir', '-m', '--max-count', '-A', '-B', '-C'}
    paths = _parse_pattern_command(args, flags)
    if not paths and any(a in ('-r', '-R', '--recursive') for a in args):
        return ['.']
    return paths


def _extract_rg(args: List[str]) -> List[str]:
    flags = {'-e', '--regexp', '-f', '--file', '-t', '--type', '-T',
             '-g', '--glob', '-m', '--max-count', '--max-depth',
             '-r', '--replace', '-A', '-B', '-C'}
    return _parse_pattern_command(args, flags, ['.'])


def _extract_sed(args: List[str]) -> List[str]:
    paths: List[str] = []
    skip_next = False
    script_found = False
    after_double_dash = False

    for i, arg in enumerate(args):
        if skip_next:
            skip_next = False
            continue
        if not after_double_dash and arg == '--':
            after_double_dash = True
            continue
        if not after_double_dash and arg.startswith('-'):
            if arg in ('-f', '--file'):
                if i + 1 < len(args):
                    paths.append(args[i + 1])
                    skip_next = True
                script_found = True
            elif arg in ('-e', '--expression'):
                skip_next = True
                script_found = True
            elif 'e' in arg or 'f' in arg:
                script_found = True
            continue
        if not script_found:
            script_found = True
            continue
        paths.append(arg)

    return paths


def _extract_git(args: List[str]) -> List[str]:
    if args and args[0] == 'diff' and '--no-index' in args:
        return _filter_out_flags(args[1:])[:2]
    return []


# Map of command → path extractor function
PATH_EXTRACTORS: Dict[str, Callable[[List[str]], List[str]]] = {
    'cd': _extract_cd,
    'ls': _extract_ls,
    'find': _extract_find,
    'mkdir': _filter_out_flags,
    'touch': _filter_out_flags,
    'rm': _filter_out_flags,
    'rmdir': _filter_out_flags,
    'mv': _filter_out_flags,
    'cp': _filter_out_flags,
    'cat': _filter_out_flags,
    'head': _filter_out_flags,
    'tail': _filter_out_flags,
    'sort': _filter_out_flags,
    'uniq': _filter_out_flags,
    'wc': _filter_out_flags,
    'cut': _filter_out_flags,
    'paste': _filter_out_flags,
    'file': _filter_out_flags,
    'stat': _filter_out_flags,
    'diff': _filter_out_flags,
    'awk': _filter_out_flags,
    'strings': _filter_out_flags,
    'hexdump': _filter_out_flags,
    'od': _filter_out_flags,
    'base64': _filter_out_flags,
    'nl': _filter_out_flags,
    'grep': _extract_grep,
    'rg': _extract_rg,
    'sed': _extract_sed,
    'git': _extract_git,
    'sha256sum': _filter_out_flags,
    'sha1sum': _filter_out_flags,
    'md5sum': _filter_out_flags,
    'chmod': _filter_out_flags,
    'chown': _filter_out_flags,
}

# Command operation type classification
COMMAND_OPERATION_TYPE: Dict[str, str] = {
    'cd': 'read', 'ls': 'read', 'find': 'read',
    'mkdir': 'create', 'touch': 'create',
    'rm': 'write', 'rmdir': 'write', 'mv': 'write', 'cp': 'write',
    'cat': 'read', 'head': 'read', 'tail': 'read',
    'sort': 'read', 'uniq': 'read', 'wc': 'read',
    'cut': 'read', 'paste': 'read',
    'file': 'read', 'stat': 'read', 'diff': 'read',
    'awk': 'read', 'strings': 'read', 'hexdump': 'read',
    'od': 'read', 'base64': 'read', 'nl': 'read',
    'grep': 'read', 'rg': 'read',
    'sed': 'write', 'git': 'read',
    'sha256sum': 'read', 'sha1sum': 'read', 'md5sum': 'read',
    'chmod': 'write', 'chown': 'write',
}

# Default dangerous removal paths
DEFAULT_DANGEROUS_PATHS = frozenset({
    '/', '/etc', '/usr', '/var', '/boot', '/sys', '/proc',
    '/home', '/root', '/dev', '/lib', '/lib64',
    '/bin', '/sbin', '/opt', '/srv', '/tmp',
})


# ---------------------------------------------------------------------------
# Config loading
# ---------------------------------------------------------------------------




def _load_dangerous_paths() -> Set[str]:
    """Load dangerous removal paths from config.

    Reads from per-agent effective config first, then global config.
    """
    raw = _get_shell_config_path("dangerous_paths", default=None)
    if raw is None:
        return set(DEFAULT_DANGEROUS_PATHS)
    if isinstance(raw, list):
        return {str(p) for p in raw if p}
    return set(DEFAULT_DANGEROUS_PATHS)


def _is_block_destructive() -> bool:
    """Check if destructive path blocking is enabled.

    Reads from per-agent effective config first, then global config.
    """
    return bool(_get_shell_config_path("block_destructive", default=True))


# ---------------------------------------------------------------------------
# Core validation
# ---------------------------------------------------------------------------

def _resolve_path(path_str: str, cwd: str) -> str:
    """Resolve a path to a canonical absolute path.

    Uses ``os.path.realpath`` when the target exists on disk so that
    symlinks are fully resolved.  Falls back to ``os.path.normpath``
    for non-existent paths (still collapses ``..`` correctly).
    """
    expanded = os.path.expanduser(path_str.strip("'\""))
    if os.path.isabs(expanded):
        normed = os.path.normpath(expanded)
    else:
        normed = os.path.normpath(os.path.join(cwd, expanded))
    # Resolve symlinks when the path exists on disk
    if os.path.exists(normed):
        return os.path.realpath(normed)
    return normed


def _is_path_within_allowed(resolved_path: str, allowed_roots: List[str]) -> bool:
    """Check if a resolved path is within any allowed root directory.

    Both the target path and the allowed roots are normalised via
    ``realpath`` (when they exist on disk) to prevent symlink-based
    escapes.
    """
    resolved = os.path.normpath(resolved_path)
    if os.path.exists(resolved):
        resolved = os.path.realpath(resolved)
    for root in allowed_roots:
        if root == "*":
            return True
        norm_root = os.path.normpath(root)
        if os.path.exists(norm_root):
            norm_root = os.path.realpath(norm_root)
        # Use os.path.commonpath to check containment
        try:
            common = os.path.commonpath([resolved, norm_root])
            if common == norm_root:
                return True
        except ValueError:
            continue
    return False


def _is_dangerous_removal_path(resolved_path: str, dangerous: Set[str]) -> bool:
    """Check if a path is in the dangerous removal list."""
    normalized = os.path.normpath(resolved_path)
    return normalized in dangerous


def _extract_redirect_targets(command: str) -> List[str]:
    """Extract output redirection targets from a command string."""
    targets: List[str] = []
    # Match > or >> followed by a path (but not >&, 2>&1, etc.)
    for match in re.finditer(r'(?<![0-9&])>{1,2}\s*(\S+)', command):
        target = match.group(1).strip("'\"")
        if target and target != '/dev/null' and not target.startswith('&'):
            targets.append(target)
    return targets


def _has_cd_in_compound(command: str) -> bool:
    """Check if a compound command contains a cd segment."""
    segments = re.split(r'\s*(?:&&|\|\||;)\s*', command)
    for seg in segments:
        stripped = seg.strip()
        first_word = stripped.split()[0] if stripped.split() else ''
        if first_word == 'cd':
            return True
    return False


def _parse_command_segments(command: str) -> List[str]:
    """Split a compound command into individual segments."""
    return re.split(r'\s*(?:&&|\|\||;|\|)\s*', command.strip())


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def _build_allowed_roots(cwd: str) -> List[str]:
    """Build the list of allowed root directories.

    Uses the unified permissions library to get workspace root +
    include_paths from path_validation rules for ``shell_tool``.
    """
    allowed_dirs = get_allowed_directories(tool_name="shell_tool")
    return [str(d) for d in allowed_dirs]


def check_path_constraints(command: str, cwd: Optional[str] = None) -> None:
    """Validate all file paths in a shell command against workspace boundaries.

    Checks:
    1. ``cd`` targets must be within allowed directories (workspace +
       ``include_paths``).  This prevents the shell session from
       escaping the workspace via ``cd`` and then accessing arbitrary
       files through relative paths.
    2. Compound commands with ``cd`` are tracked: each ``cd`` updates
       the *effective CWD* and subsequent commands are validated
       against the new CWD.
    3. Command path arguments are within allowed directories.
    4. Output redirection targets are within allowed directories.
    5. Dangerous removal paths are blocked (``rm -rf /``).
    6. ``cd`` + write combination attacks are caught.

    Args:
        command: The full shell command string.
        cwd: Current working directory of the shell session.  When
            provided, all relative paths are resolved against this
            directory.  Defaults to ``os.getcwd()`` for backward
            compatibility.

    Raises:
        ValueError: If any path constraint is violated.
    """
    if not command or not command.strip():
        return

    if cwd is None:
        cwd = os.getcwd()

    allowed_roots = _build_allowed_roots(cwd)
    dangerous = _load_dangerous_paths()
    block_destructive = _is_block_destructive()
    has_cd = _has_cd_in_compound(command)

    # Check output redirections
    redirect_targets = _extract_redirect_targets(command)
    for target in redirect_targets:
        resolved = _resolve_path(target, cwd)
        if not _is_path_within_allowed(resolved, allowed_roots):
            msg = (
                f"Output redirection to '{resolved}' is outside allowed workspace. "
                f"Allowed directories: {allowed_roots}."
                + _PATH_GUIDANCE
            )
            _audit_path_violation(command, msg, resolved)
            raise ValueError(msg)
        if has_cd:
            msg = (
                "Commands that change directories and write via output redirection "
                "require explicit review. Cannot determine final working directory "
                "when 'cd' is used in compound commands."
            )
            _audit_path_violation(command, msg, resolved)
            raise ValueError(msg)

    # Track effective CWD across compound command segments.
    # Each ``cd`` updates effective_cwd so that subsequent segments
    # resolve relative paths correctly.
    effective_cwd = cwd
    segments = _parse_command_segments(command)
    for segment in segments:
        segment = segment.strip()
        if not segment:
            continue

        # Strip env var assignments
        stripped = re.sub(r'^(\s*[A-Za-z_]\w*=\S*\s*)+', '', segment).strip()
        if not stripped:
            continue

        words = stripped.split()
        if not words:
            continue

        base_cmd = words[0].rsplit('/', 1)[-1] if '/' in words[0] else words[0]
        args = words[1:]

        # --- cd boundary enforcement ---
        # The cd target MUST be within allowed_roots.  This prevents
        # the shell session from escaping to arbitrary directories.
        if base_cmd == 'cd':
            cd_target_raw = ' '.join(args) if args else os.path.expanduser('~')
            cd_resolved = _resolve_path(cd_target_raw, effective_cwd)
            if not _is_path_within_allowed(cd_resolved, allowed_roots):
                msg = (
                    f"'cd' targeting '{cd_resolved}' is outside allowed workspace. "
                    f"Allowed directories: {allowed_roots}."
                    + _PATH_GUIDANCE
                )
                _audit_path_violation(command, msg, cd_resolved)
                raise ValueError(msg)
            # Update effective CWD for subsequent segments
            effective_cwd = cd_resolved
            continue

        # Check if this is a path-extractable command
        extractor = PATH_EXTRACTORS.get(base_cmd)
        if extractor is None:
            continue

        op_type = COMMAND_OPERATION_TYPE.get(base_cmd, 'read')

        # Block cd + write combinations (redirect already caught above)
        if has_cd and op_type != 'read':
            msg = (
                f"Commands that change directories and perform '{base_cmd}' (write operation) "
                "require explicit review. Cannot safely determine the final working directory."
            )
            _audit_path_violation(command, msg)
            raise ValueError(msg)

        # Extract paths for this command
        extracted_paths = extractor(args)

        for path_str in extracted_paths:
            if not path_str:
                continue

            resolved = _resolve_path(path_str, effective_cwd)

            # Check dangerous removal paths
            if block_destructive and base_cmd in ('rm', 'rmdir'):
                if _is_dangerous_removal_path(resolved, dangerous):
                    msg = (
                        f"Dangerous '{base_cmd}' operation on critical path: '{resolved}'. "
                        "This command would remove a critical system directory."
                    )
                    _audit_path_violation(command, msg, resolved)
                    raise ValueError(msg)

            # Check if path is within allowed workspace
            if not _is_path_within_allowed(resolved, allowed_roots):
                msg = (
                    f"'{base_cmd}' targeting '{resolved}' is outside allowed workspace. "
                    f"Allowed directories: {allowed_roots}."
                    + _PATH_GUIDANCE
                )
                _audit_path_violation(command, msg, resolved)
                raise ValueError(msg)
