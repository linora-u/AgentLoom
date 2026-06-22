"""Shell command security validation.

Detects dangerous patterns in shell commands that could lead to
command injection, privilege escalation, or system damage.
Each check can be individually toggled via YAML config:

    shell_settings:
      security_checks:
        command_substitution: true
        env_injection: true
        control_characters: true
        dangerous_shell_prefix: true
        zsh_dangerous_commands: true
        incomplete_commands: true
        process_substitution: true
        ifs_injection: true
        parameter_expansion: true
        destructive_patterns: true
"""

import re
from dataclasses import dataclass
from typing import List, Optional, Tuple

from src.lib.config import C
from src.lib.logging import get_logger

logger = get_logger(__name__)


# ---------------------------------------------------------------------------
# Result type
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class SecurityCheckResult:
    """Result of a security check on a shell command."""
    is_safe: bool
    check_id: str           # e.g. "command_substitution"
    message: str            # human-readable reason
    alternative: str = ""   # suggested alternative approach


# ---------------------------------------------------------------------------
# Dangerous pattern constants
# ---------------------------------------------------------------------------

# Command substitution patterns — $(), backticks
# Note: ${} is handled by a separate _check_parameter_expansion check.
_COMMAND_SUBSTITUTION_PATTERNS = [
    (re.compile(r'\$\('), "$() command substitution"),
    (re.compile(r'\$\['), "$[] legacy arithmetic expansion"),
]

# Process substitution — >(), <()
_PROCESS_SUBSTITUTION_PATTERNS = [
    (re.compile(r'<\('), "<() process substitution"),
    (re.compile(r'>\('), ">() process substitution"),
    (re.compile(r'=\('), "=() zsh process substitution"),
]

# Zsh-specific dangerous commands that can bypass security checks
ZSH_DANGEROUS_COMMANDS = frozenset({
    "zmodload", "emulate",
    "sysopen", "sysread", "syswrite", "sysseek",
    "zpty", "ztcp", "zsocket", "mapfile",
    "zf_rm", "zf_mv", "zf_ln", "zf_chmod",
    "zf_chown", "zf_mkdir", "zf_rmdir", "zf_chgrp",
})

# Shell interpreter prefixes — these can execute arbitrary code
DANGEROUS_SHELL_PREFIXES = frozenset({
    "sh", "bash", "zsh", "fish", "csh", "tcsh", "ksh", "dash",
    "cmd", "powershell", "pwsh",
    "env", "xargs",
    "sudo", "doas", "pkexec",
    "nice", "nohup", "timeout", "stdbuf",
})

# Dangerous environment variables that can hijack execution
_DANGEROUS_ENV_VARS = frozenset({
    "LD_PRELOAD", "LD_LIBRARY_PATH",
    "DYLD_INSERT_LIBRARIES", "DYLD_LIBRARY_PATH",
    "PATH", "IFS",
    "BASH_ENV", "ENV", "PROMPT_COMMAND",
    "PYTHONSTARTUP", "PERL5OPT", "RUBYOPT",
    "BASH_FUNC_",
})

# Safe environment variables that are allowed as command prefixes
SAFE_ENV_VARS = frozenset({
    # Go
    "GOEXPERIMENT", "GOOS", "GOARCH", "CGO_ENABLED", "GOFLAGS",
    "GOBIN", "GOPATH", "GOPROXY", "GONOSUMCHECK",
    # Rust
    "RUST_BACKTRACE", "RUST_LOG", "RUSTFLAGS", "CARGO_TARGET_DIR",
    # Python
    "PYTHONUNBUFFERED", "PYTHONDONTWRITEBYTECODE", "PYTHONPATH",
    "PYTHONHASHSEED", "VIRTUAL_ENV",
    # Node
    "NODE_ENV", "NODE_OPTIONS", "NODE_PATH", "NPM_CONFIG_REGISTRY",
    # Java
    "JAVA_HOME", "JAVA_OPTS", "MAVEN_OPTS", "GRADLE_OPTS",
    # Locale / Terminal
    "LANG", "LC_ALL", "LC_CTYPE", "LC_MESSAGES",
    "TZ", "TERM", "COLORTERM",
    "FORCE_COLOR", "NO_COLOR", "CLICOLOR", "CLICOLOR_FORCE",
    # CI / Build
    "CI", "GITHUB_ACTIONS", "GITLAB_CI", "JENKINS_URL",
    "DEBUG", "VERBOSE",
    # Credentials (read-only, commonly set in CI)
    "GH_TOKEN", "GITHUB_TOKEN",
})

# Destructive command patterns
_DESTRUCTIVE_PATTERNS = [
    (re.compile(r'\brm\s+(-[a-zA-Z]*f[a-zA-Z]*\s+|--force\s+)*/\s*$'), "rm -rf /"),
    (re.compile(r'\brm\s+(-[a-zA-Z]*f[a-zA-Z]*\s+|--force\s+)*~'), "rm -rf ~"),
    (re.compile(r'\bgit\s+reset\s+--hard'), "git reset --hard"),
    (re.compile(r'\bgit\s+clean\s+-[a-zA-Z]*f'), "git clean -f"),
    (re.compile(r'\bgit\s+push\s+.*--force'), "git push --force"),
    (re.compile(r'\bgit\s+push\s+.*-f\b'), "git push -f"),
    (re.compile(r'\bmkfs\b'), "mkfs (format filesystem)"),
    (re.compile(r'\bdd\s+if='), "dd (disk write)"),
    (re.compile(r':\(\)\s*\{\s*:\|\s*:\s*&\s*\}\s*;'), "fork bomb"),
    (re.compile(r'\bchmod\s+777\s+/'), "chmod 777 / (open permissions on root)"),
    (re.compile(r'\btruncate\s+-s\s*0\b'), "truncate -s 0 (empty file)"),
    (re.compile(r'\bDROP\s+(TABLE|DATABASE)\b', re.IGNORECASE), "DROP TABLE/DATABASE"),
    (re.compile(r'\bDELETE\s+FROM\b(?!.*\bWHERE\b)', re.IGNORECASE), "DELETE FROM without WHERE"),
    (re.compile(r'\bterraform\s+destroy\b'), "terraform destroy"),
    (re.compile(r'\bkubectl\s+delete\b'), "kubectl delete"),
]

# Control character pattern (0x00-0x08, 0x0E-0x1F except tab/newline/CR)
_CONTROL_CHAR_PATTERN = re.compile(r'[\x00-\x08\x0e-\x1f]')


# ---------------------------------------------------------------------------
# Quote-aware content extraction
# ---------------------------------------------------------------------------

def _extract_unquoted_content(command: str) -> str:
    """Extract content outside of single and double quotes.

    This allows patterns like `echo '$HOME'` to pass ($ is inside quotes),
    while `echo $HOME` is detected ($ is unquoted).
    """
    result = []
    in_single_quote = False
    in_double_quote = False
    escaped = False

    for ch in command:
        if escaped:
            escaped = False
            if not in_single_quote:
                result.append(ch)
            continue

        if ch == '\\' and not in_single_quote:
            escaped = True
            if not in_double_quote:
                result.append(ch)
            continue

        if ch == "'" and not in_double_quote:
            in_single_quote = not in_single_quote
            continue

        if ch == '"' and not in_single_quote:
            in_double_quote = not in_double_quote
            continue

        if not in_single_quote and not in_double_quote:
            result.append(ch)

    return "".join(result)


def _has_unescaped_backtick(content: str) -> bool:
    """Check for unescaped backtick characters in content."""
    i = 0
    while i < len(content):
        if content[i] == '\\' and i + 1 < len(content):
            i += 2  # skip escaped char
            continue
        if content[i] == '`':
            return True
        i += 1
    return False


# ---------------------------------------------------------------------------
# Individual security checks
# ---------------------------------------------------------------------------

def _check_command_substitution(command: str, unquoted: str) -> Optional[SecurityCheckResult]:
    """Detect $(), $[] command substitution in unquoted context.

    Quote-aware: single-quoted content is excluded from ``unquoted``,
    so ``echo '$(...)'`` will NOT trigger this check.  This aligns
    with Claude Code's security model which allows single-quoted
    literal command substitution.

    Note: ${} parameter expansion is handled by a separate check
    (_check_parameter_expansion) for clarity.
    """
    # Only check $() and $[] — ${} has its own check
    _CMD_SUB_ONLY = [
        (re.compile(r'\$\('), "$() command substitution"),
        (re.compile(r'\$\['), "$[] legacy arithmetic expansion"),
    ]
    _alt = (
        "Use write_file or edit_file tool for multi-line content "
        "containing backticks or $()"
    )
    for pattern, desc in _CMD_SUB_ONLY:
        if pattern.search(unquoted):
            return SecurityCheckResult(
                is_safe=False,
                check_id="command_substitution",
                message=f"Blocked: {desc} detected in command",
                alternative=_alt,
            )
    # Check backticks separately (need escape-aware check)
    if _has_unescaped_backtick(unquoted):
        return SecurityCheckResult(
            is_safe=False,
            check_id="command_substitution",
            message="Blocked: backtick command substitution detected",
            alternative=_alt,
        )
    return None


def _check_process_substitution(command: str, unquoted: str) -> Optional[SecurityCheckResult]:
    """Detect <(), >(), =() process substitution."""
    for pattern, desc in _PROCESS_SUBSTITUTION_PATTERNS:
        if pattern.search(unquoted):
            return SecurityCheckResult(
                is_safe=False,
                check_id="process_substitution",
                message=f"Blocked: {desc} detected in command",
                alternative=(
                    "Write intermediate results to a file within "
                    "workspace, then read it"
                ),
            )
    return None


def _check_env_injection(command: str, unquoted: str) -> Optional[SecurityCheckResult]:
    """Detect dangerous environment variable injection in command prefix."""
    # Match VAR=value at the start of command
    env_assign_pattern = re.compile(r'^(\s*[A-Za-z_]\w*=\S*\s*)+')
    match = env_assign_pattern.match(command.strip())
    if not match:
        return None

    prefix = match.group(0)
    assignments = re.findall(r'([A-Za-z_]\w*)=', prefix)

    for var_name in assignments:
        if var_name in SAFE_ENV_VARS:
            continue
        # Check if it matches a dangerous pattern
        for dangerous in _DANGEROUS_ENV_VARS:
            if var_name == dangerous or var_name.startswith(dangerous):
                return SecurityCheckResult(
                    is_safe=False,
                    check_id="env_injection",
                    message=f"Blocked: dangerous environment variable '{var_name}' in command prefix",
                    alternative=(
                        "Configure environment in agent YAML config, "
                        "not in shell commands"
                    ),
                )
    return None


def _check_ifs_injection(command: str, unquoted: str) -> Optional[SecurityCheckResult]:
    """Detect IFS variable manipulation."""
    if re.search(r'\bIFS\s*=', unquoted):
        return SecurityCheckResult(
            is_safe=False,
            check_id="ifs_injection",
            message="Blocked: IFS variable manipulation detected",
        )
    return None


def _check_control_characters(command: str, unquoted: str) -> Optional[SecurityCheckResult]:
    """Detect control characters that could hide malicious content."""
    if _CONTROL_CHAR_PATTERN.search(command):
        return SecurityCheckResult(
            is_safe=False,
            check_id="control_characters",
            message="Blocked: control characters detected in command",
        )
    return None


def _check_incomplete_commands(command: str, unquoted: str) -> Optional[SecurityCheckResult]:
    """Detect incomplete command fragments that suggest injection attempts."""
    # Check raw command for leading tab BEFORE stripping
    if command and command[0] == '\t':
        return SecurityCheckResult(
            is_safe=False,
            check_id="incomplete_commands",
            message="Blocked: command appears to be an incomplete fragment (starts with tab)",
        )

    trimmed = command.strip()

    # Starts with a flag (no command before it)
    if trimmed.startswith('-'):
        return SecurityCheckResult(
            is_safe=False,
            check_id="incomplete_commands",
            message="Blocked: command appears to be an incomplete fragment (starts with flags)",
        )

    # Starts with an operator (continuation of another command)
    if re.match(r'^\s*(&&|\|\||;|>>?|<)', command):
        return SecurityCheckResult(
            is_safe=False,
            check_id="incomplete_commands",
            message="Blocked: command appears to be a continuation fragment (starts with operator)",
        )

    return None


def _check_dangerous_shell_prefix(command: str, unquoted: str) -> Optional[SecurityCheckResult]:
    """Detect dangerous shell interpreter invocations."""
    # Strip env var assignments to get the actual command
    stripped = re.sub(r'^(\s*[A-Za-z_]\w*=\S*\s*)+', '', command.strip()).strip()
    if not stripped:
        return None

    first_word = stripped.split()[0] if stripped.split() else ""
    # Normalize: /usr/bin/bash → bash
    base_name = first_word.rsplit('/', 1)[-1] if '/' in first_word else first_word

    if base_name in DANGEROUS_SHELL_PREFIXES:
        return SecurityCheckResult(
            is_safe=False,
            check_id="dangerous_shell_prefix",
            message=f"Blocked: '{base_name}' invocation can execute arbitrary commands",
            alternative=(
                "Run commands directly without sudo or shell wrappers"
            ),
        )
    return None


def _check_zsh_dangerous_commands(command: str, unquoted: str) -> Optional[SecurityCheckResult]:
    """Detect Zsh-specific dangerous builtin commands."""
    # Extract all command names from the unquoted content
    words = unquoted.split()
    for word in words:
        clean = word.strip(';|&')
        if clean in ZSH_DANGEROUS_COMMANDS:
            return SecurityCheckResult(
                is_safe=False,
                check_id="zsh_dangerous_commands",
                message=f"Blocked: Zsh dangerous command '{clean}' detected",
            )
    return None


def _check_parameter_expansion(command: str, unquoted: str) -> Optional[SecurityCheckResult]:
    """Detect ${} parameter expansion in unquoted context."""
    if '${' in unquoted:
        return SecurityCheckResult(
            is_safe=False,
            check_id="parameter_expansion",
            message="Blocked: ${} parameter expansion detected in unquoted context",
        )
    return None


def _check_destructive_patterns(command: str, unquoted: str) -> Optional[SecurityCheckResult]:
    """Detect known destructive command patterns."""
    for pattern, desc in _DESTRUCTIVE_PATTERNS:
        if pattern.search(command):
            return SecurityCheckResult(
                is_safe=False,
                check_id="destructive_patterns",
                message=f"Blocked: destructive command detected — {desc}",
                alternative=(
                    "Use targeted operations: rm specific-file, "
                    "git revert commit-hash"
                ),
            )
    return None


# ---------------------------------------------------------------------------
# Check registry
# ---------------------------------------------------------------------------

# Maps config key → checker function
_SECURITY_CHECKS = {
    "command_substitution": _check_command_substitution,
    "process_substitution": _check_process_substitution,
    "env_injection": _check_env_injection,
    "ifs_injection": _check_ifs_injection,
    "control_characters": _check_control_characters,
    "incomplete_commands": _check_incomplete_commands,
    "dangerous_shell_prefix": _check_dangerous_shell_prefix,
    "zsh_dangerous_commands": _check_zsh_dangerous_commands,
    "parameter_expansion": _check_parameter_expansion,
    "destructive_patterns": _check_destructive_patterns,
}


def _get_shell_config_security(key: str, *, default=None):
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


def _load_enabled_checks() -> dict:
    """Load security check toggles from config.

    Returns dict of check_id → bool.  Missing keys default to True (enabled).
    Reads from the per-agent effective config first, then falls back
    to the global config singleton.
    """
    raw = _get_shell_config_security("security_checks", default=None)
    if raw is None or not isinstance(raw, dict):
        return {}
    return {str(k): bool(v) for k, v in raw.items()}


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def check_command_security(command: str) -> List[SecurityCheckResult]:
    """Run all enabled security checks against a shell command.

    Returns a list of failed checks (empty list = all checks passed).
    Each check can be toggled via ``tools.shell.security_checks.<id>``
    in ``config/system.yaml``.

    Args:
        command: The raw shell command string.

    Returns:
        List of SecurityCheckResult for each failed check.
    """
    if not command or not command.strip():
        return []

    enabled_overrides = _load_enabled_checks()
    unquoted = _extract_unquoted_content(command)

    failures: List[SecurityCheckResult] = []
    for check_id, checker_fn in _SECURITY_CHECKS.items():
        # Check if this specific check is disabled in config
        if not enabled_overrides.get(check_id, True):
            continue

        result = checker_fn(command, unquoted)
        if result is not None:
            failures.append(result)
            logger.info(
                "Security check '%s' blocked command: %s",
                check_id, result.message,
            )
            # Write to per-agent shell audit log
            try:
                from src.tools.shell.shell_audit_log import get_shell_audit_logger
                audit = get_shell_audit_logger()
                audit.log_security_block(
                    command=command,
                    check_id=check_id,
                    message=result.message,
                )
            except Exception:
                pass  # Never let audit logging break the security pipeline

    return failures


def validate_command_security(command: str) -> None:
    """Validate a command and raise ValueError if any security check fails.

    This is the main entry point used by the shell validator pipeline.

    Args:
        command: The raw shell command string.

    Raises:
        ValueError: If any security check fails.
    """
    failures = check_command_security(command)
    if failures:
        # Report the first failure (most critical)
        msg = failures[0].message
        if failures[0].alternative:
            msg = f"{msg}\nSuggested alternative: {failures[0].alternative}"
        raise ValueError(msg)
