import re
from typing import Any, List, Tuple

from src.lib.config import C
from src.tools.shell.shell_command_ast import ShellCommandInvocation, analyze_shell_command
from src.tools.shell.security import validate_command_security
from src.tools.shell.path_validation import check_path_constraints


def _get_shell_config(key: str, *, default: Any = None) -> Any:
    """Read a shell config value, preferring the per-agent effective config.

    Lookup order:
    1. Per-agent effective config (from ``get_current_agent_config()``)
    2. Global config singleton (``C.get_nested()``)

    This ensures that per-agent shell security overrides (set via the
    application-level ``config/system.yaml`` or the agent YAML overlay)
    actually take effect at runtime.
    """
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


def _audit_whitelist_rejection(command: str, message: str, name: str = "") -> None:
    """Write a whitelist rejection event to the per-agent shell audit log."""
    try:
        from src.tools.shell.shell_audit_log import get_shell_audit_logger
        audit = get_shell_audit_logger()
        audit.log_whitelist_rejection(command=command, message=message, name=name)
    except Exception:
        pass  # Never let audit logging break the validation pipeline


def load_allowed_commands() -> List[str]:
    """Loads allowed shell commands from config.

    Reads from the per-agent effective config first, then falls back
    to the global ``C.get_nested()`` singleton.
    """
    raw = _get_shell_config("allowed_commands", default=None)

    if raw is None:
        return []
    if isinstance(raw, str):
        raw = [raw]
    if not isinstance(raw, list):
        raise ValueError("shell_settings.allowed_commands must be a list of strings")
        
    commands: List[str] = []
    wildcard = False
    for item in raw:
        if not isinstance(item, str) or not item.strip():
            raise ValueError("tools.shell.allowed_commands must be a list of non-empty strings")
            
        cleaned = item.strip()
        if cleaned == "*":
            wildcard = True
            continue
        if any(ch.isspace() for ch in cleaned):
            raise ValueError("tools.shell.allowed_commands must be bare command names (no spaces)")
        commands.append(cleaned)

    if wildcard:
        return []

    return commands


def load_allowed_operators() -> List[str]:
    """Loads allowed shell operators from config.

    Reads from the per-agent effective config first, then falls back
    to the global ``C.get_nested()`` singleton.
    """
    raw = _get_shell_config("allowed_operators", default=[])
    
    if raw is None:
        return []
    if isinstance(raw, str):
        raw = [raw]
    if not isinstance(raw, list):
        raise ValueError("shell_settings.allowed_operators must be a list of strings")
        
    operators: List[str] = []
    wildcard = False
    for item in raw:
        if not isinstance(item, str) or not item.strip():
            raise ValueError("shell_settings.allowed_operators must be a list of non-empty strings")
        cleaned = item.strip()
        if cleaned == "*":
            wildcard = True
            continue
        operators.append(cleaned)

    if wildcard:
        return []

    return operators


def _normalize_token(token: str) -> str:
    normalized = token.strip()
    if len(normalized) >= 2:
        if normalized[0] == normalized[-1] and normalized[0] in {"'", '"'}:
            return normalized[1:-1].strip()
    return normalized


def _extract_whitelisted_names(invocation: ShellCommandInvocation, command: str) -> List[str]:
    name = _normalize_token(invocation.name)
    if not name:
        raise ValueError(f"Unable to determine command name for: {command}")

    # Command substitution used as command name is validated by inner command nodes.
    if name.startswith("$("):
        return []

    if name != "command":
        return [name]

    normalized_args: List[str] = []
    for arg in invocation.args:
        normalized = _normalize_token(arg)
        if normalized:
            normalized_args.append(normalized)

    # Prevent whitelist bypass via `command <anything>`.
    if len(normalized_args) < 2 or normalized_args[0] not in {"-v", "-V"}:
        raise ValueError(
            "Unsupported use of shell builtin 'command'. Only `command -v <name>` is allowed."
        )

    targets = normalized_args[1:]
    for target in targets:
        if target.startswith("-") or target.startswith("$("):
            raise ValueError(
                "Unsupported use of shell builtin 'command'. Only `command -v <name>` is allowed."
            )
    return targets


def analyze_command(command: str) -> Tuple[List[str], List[str]]:
    """Analyzes a shell command and extracts its names and operators."""
    try:
        analysis = analyze_shell_command(command)
    except ImportError as e:
        raise ImportError(
            "tree-sitter-bash is required for command whitelisting. Install it with `uv add tree-sitter-bash`."
        ) from e
    except ValueError as e:
        raise ValueError(f"Invalid shell command: {command}") from e

    names: List[str] = []
    for invocation in analysis.commands:
        names.extend(_extract_whitelisted_names(invocation, command))

    if not names:
        raise ValueError(f"No command found in: {command}")

    return names, list(analysis.operators)

# Safe wrapper commands that should be stripped before security analysis.
# e.g. "timeout 30 rm -rf /" → analyze "rm -rf /" not "timeout".
_SAFE_WRAPPER_RE = re.compile(
    r'^(?:'
    r'timeout\s+\S+\s+'           # timeout DURATION cmd
    r'|nice\s+(?:-n\s+\S+\s+)?'  # nice [-n N] cmd
    r'|nohup\s+'                  # nohup cmd
    r'|time\s+'                   # time cmd
    r'|stdbuf\s+(?:-[ioe]\S*\s+)*'  # stdbuf -oL cmd
    r')'
)


def strip_safe_wrappers(command: str) -> str:
    """Strip safe wrapper commands (timeout, nice, nohup, time, stdbuf).

    Iterates until no more wrappers are found (fixed-point).
    This ensures "timeout 30 nice -n 5 rm -rf /" strips
    down to "rm -rf /" for security analysis.
    """
    stripped = command.strip()
    for _ in range(10):  # max iterations to prevent infinite loop
        new = _SAFE_WRAPPER_RE.sub('', stripped).strip()
        if new == stripped:
            break
        stripped = new
    return stripped


def validate_command(command: str, cwd: str | None = None):
    """Full validation pipeline for shell commands.

    Stages (in order):
    1. Security checks (injection, substitution, destructive patterns)
    2. Path boundary validation (workspace containment)
    3. Command whitelist / operator whitelist

    Args:
        command: The shell command string to validate.
        cwd: Current working directory of the shell session.  When
            provided, path boundary checks resolve relative paths
            against this directory instead of ``os.getcwd()``.
            This closes a security gap where the session-scoped shell
            CWD can diverge from the Python process CWD.
    """
    # Stage 1: Security checks (on raw command)
    validate_command_security(command)

    # Stage 2: Path boundary validation
    check_path_constraints(command, cwd=cwd)

    # Stage 3: Command name + operator whitelist
    allowed_commands = load_allowed_commands()
    allowed_operators = load_allowed_operators()

    if allowed_commands or allowed_operators:
        # Strip wrappers before whitelist check so "timeout 30 ls"
        # is validated as "ls", not "timeout"
        stripped_for_whitelist = strip_safe_wrappers(command)

        allowed_set = set(allowed_commands)
        allowed_text = ", ".join(sorted(allowed_set))
        allowed_ops = set(allowed_operators)
        allowed_ops_text = ", ".join(sorted(allowed_ops))

        names, operators = analyze_command(stripped_for_whitelist)

        if allowed_commands:
            for name in names:
                if name not in allowed_set:
                    msg = f"Command not allowed: {name}. Allowed commands: {allowed_text}"
                    _audit_whitelist_rejection(command, msg, name)
                    raise ValueError(msg)

        if allowed_operators:
            for op in operators:
                if op not in allowed_ops:
                    msg = f"Operator not allowed: {op}. Allowed operators: {allowed_ops_text}"
                    _audit_whitelist_rejection(command, msg, op)
                    raise ValueError(msg)


def validate_commands(commands: List[str]):
    """Backward-compatible wrapper: validate a list of command strings."""
    for cmd in commands:
        validate_command(cmd)
