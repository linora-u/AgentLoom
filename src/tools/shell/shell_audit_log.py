"""Per-agent shell security audit logger.

Writes structured, human-readable audit entries to a dedicated log file
that shares the same per-run timestamp directory as the main agent log::

    .logs/{agent_name}/{timestamp}/shell_audit.log

When no main agent log context is available (standalone usage), it
falls back to creating its own timestamp directory under the configured
log base directory.

Each entry records a shell security event (blocked command, path
violation, stall detection, timeout, etc.) together with an actionable
suggestion that tells the user which YAML setting to change.

The audit log is separate from the main application log so that users
can quickly find and diagnose shell-related issues without searching
through thousands of unrelated log lines.

Configuration (config/system.yaml)::

    shell_settings:
      audit_log:
        enabled: true        # master switch (default: true)
        log_success: false   # log successful executions too (default: false)

Usage::

    from src.tools.shell.shell_audit_log import get_shell_audit_logger

    audit = get_shell_audit_logger()
    audit.log_security_block(
        command="rm -rf /",
        check_id="destructive_patterns",
        message="Blocked: destructive command detected",
    )
"""

from __future__ import annotations

import os
import threading
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

from src.lib.config import C
from src.lib.logging import get_logger

logger = get_logger(__name__)

# ---------------------------------------------------------------------------
# Module-level cache
# ---------------------------------------------------------------------------
_AUDIT_LOGGERS: dict[str, "ShellAuditLogger"] = {}
_CACHE_LOCK = threading.Lock()

# ---------------------------------------------------------------------------
# Event type constants
# ---------------------------------------------------------------------------
EVENT_SECURITY_BLOCK = "SECURITY_BLOCK"
EVENT_PATH_VIOLATION = "PATH_VIOLATION"
EVENT_WHITELIST_REJECT = "WHITELIST_REJECT"
EVENT_STALL_DETECTED = "STALL_DETECTED"
EVENT_TIMEOUT = "TIMEOUT"
EVENT_BACKGROUND_PROMOTION = "BACKGROUND_PROMOTION"
EVENT_SANDBOX_WRAP = "SANDBOX_WRAP"
EVENT_SANDBOX_UNAVAILABLE = "SANDBOX_UNAVAILABLE"
EVENT_COMMAND_SUCCESS = "COMMAND_SUCCESS"
EVENT_POLICY_SNAPSHOT = "POLICY_SNAPSHOT"

# ---------------------------------------------------------------------------
# Suggestion templates
# ---------------------------------------------------------------------------
_SUGGESTIONS: dict[str, str] = {
    EVENT_SECURITY_BLOCK: (
        "To disable this check for a specific agent, add the following "
        "to the agent YAML:\n"
        "  shell_settings:\n"
        "    security_checks:\n"
        "      {check_id}: false"
    ),
    EVENT_PATH_VIOLATION: (
        "To allow access to this path, add a path_validation rule to the agent YAML:\n"
        "  tool_access_control:\n"
        "    path_validation:\n"
        "      - tools: [\"shell_tool\"]\n"
        "        include_paths: [\"{path}\"]"
    ),
    EVENT_WHITELIST_REJECT: (
        "To allow this command, add it to the agent YAML:\n"
        "  shell_settings:\n"
        "    allowed_commands: [\"{name}\", ...]"
    ),
    "OPERATOR_WHITELIST_REJECT": (
        "To allow this operator, add it to the agent YAML:\n"
        "  shell_settings:\n"
        "    allowed_operators: [\"{name}\", ...]"
    ),
    EVENT_STALL_DETECTED: (
        "The command appears stuck on an interactive prompt. "
        "Re-run with a non-interactive flag (--yes, -y, --non-interactive) "
        "or pipe input: echo y | <command>"
    ),
    EVENT_TIMEOUT: (
        "Increase the timeout parameter in the tool call, "
        "or set run_in_background=True for long-running commands."
    ),
    EVENT_BACKGROUND_PROMOTION: (
        "Command exceeded its timeout and was auto-promoted to a background task. "
        "Use check_background_task(task_id) to monitor progress."
    ),
    EVENT_SANDBOX_WRAP: (
        "Command was wrapped in a sandbox for OS-level isolation. "
        "No action needed unless sandbox is causing issues."
    ),
    EVENT_SANDBOX_UNAVAILABLE: (
        "Sandbox was requested but unavailable, so the command ran without "
        "OS-level sandbox isolation. Install the configured sandbox backend "
        "or disable shell_settings.sandbox.enabled for this agent."
    ),
    EVENT_COMMAND_SUCCESS: "",
    EVENT_POLICY_SNAPSHOT: (
        "This entry records the effective shell security policy for this run. "
        "If allowed_commands or allowed_operators is '*', command/operator "
        "allow-list checks are intentionally disabled."
    ),
}


def _sanitize_component(value: str) -> str:
    """Sanitize a string for use as a filesystem path component."""
    import re
    normalized = re.sub(r"[^A-Za-z0-9._-]+", "_", value.strip())
    normalized = normalized.strip("._")
    return normalized or "unknown"


def _coerce_bool(value: Any) -> bool:
    if isinstance(value, str):
        return value.lower() in ("true", "1", "yes", "on")
    return bool(value)


def _get_shell_config_with_source(key: str, *, default: Any = None) -> tuple[Any, str]:
    """Read effective shell config and identify where it came from."""
    try:
        from src.trace import get_current_agent_config
        agent_cfg = get_current_agent_config()
        if isinstance(agent_cfg, dict):
            shell = agent_cfg.get("shell_settings")
            if isinstance(shell, dict) and key in shell:
                return shell[key], "effective_agent_config"
    except Exception:
        pass
    return C.get_nested("shell_settings", key, default=default), "global"


def _format_allow_policy(value: Any, all_label: str) -> str:
    """Format command/operator allow-list config for audit readability."""
    if value is None:
        return f"unset ({all_label})"

    if isinstance(value, str):
        items = [value.strip()]
    elif isinstance(value, list):
        items = [str(item).strip() for item in value if str(item).strip()]
    else:
        return f"{value!r} (invalid type; validation may reject)"

    if not items:
        return f"[] ({all_label})"
    if "*" in items:
        return f"* ({all_label})"
    return ", ".join(items)


def _format_security_checks(value: Any) -> tuple[str, str]:
    if not isinstance(value, dict) or not value:
        return "default enabled", "none"

    enabled = sorted(str(key) for key, item in value.items() if _coerce_bool(item))
    disabled = sorted(str(key) for key, item in value.items() if not _coerce_bool(item))
    return ", ".join(enabled) or "none", ", ".join(disabled) or "none"


def _format_list(value: Any) -> str:
    if isinstance(value, list):
        return ", ".join(str(item) for item in value) or "none"
    if value is None:
        return "none"
    return str(value)


# ---------------------------------------------------------------------------
# ShellAuditLogger
# ---------------------------------------------------------------------------

class ShellAuditLogger:
    """Writes structured shell security events to a per-agent audit file.

    Each instance is bound to a specific agent name and writes to a
    single audit log file for the lifetime of the process.  Instances
    are cached by agent name via :func:`get_shell_audit_logger`.

    Thread-safe: all writes are serialized through an internal lock.
    """

    def __init__(self, agent_name: str, log_dir: Optional[str] = None):
        self._agent_name = agent_name
        self._lock = threading.Lock()
        self._file_path: Optional[Path] = None
        self._enabled: Optional[bool] = None
        self._log_success: Optional[bool] = None
        self._log_policy_snapshot: Optional[bool] = None
        self._policy_snapshot_logged = False
        self._log_dir = log_dir

    # -- lazy initialization ------------------------------------------------

    @property
    def enabled(self) -> bool:
        """Check if audit logging is enabled (cached after first read)."""
        if self._enabled is None:
            self._enabled = C.get_nested(
                "shell_settings", "audit_log", "enabled",
                default=True,
            )
            # Coerce string "true"/"false" from YAML
            if isinstance(self._enabled, str):
                self._enabled = self._enabled.lower() in ("true", "1", "yes")
        return bool(self._enabled)

    @property
    def log_success(self) -> bool:
        """Whether successful command executions are logged."""
        if self._log_success is None:
            self._log_success = C.get_nested(
                "shell_settings", "audit_log", "log_success",
                default=False,
            )
            if isinstance(self._log_success, str):
                self._log_success = self._log_success.lower() in (
                    "true", "1", "yes",
                )
        return bool(self._log_success)

    @property
    def log_policy_snapshot(self) -> bool:
        """Whether to log the effective shell policy once per run."""
        if self._log_policy_snapshot is None:
            self._log_policy_snapshot = C.get_nested(
                "shell_settings", "audit_log", "log_policy_snapshot",
                default=True,
            )
            if isinstance(self._log_policy_snapshot, str):
                self._log_policy_snapshot = self._log_policy_snapshot.lower() in (
                    "true", "1", "yes",
                )
        return bool(self._log_policy_snapshot)

    @property
    def file_path(self) -> Path:
        """Lazily resolve and create the audit log file path."""
        if self._file_path is None:
            self._file_path = self._resolve_path()
        return self._file_path

    def _resolve_path(self) -> Path:
        """Resolve the audit log file path.

        Strategy:
        1. If a process-level run directory already exists (set by the
           main logger), place ``shell_audit.log`` in that same directory
           so that agent log and audit log live side-by-side::

               .logs/{agent_name}/{timestamp}/shell_audit.log

        2. Otherwise (standalone usage / tests with explicit *log_dir*),
           create our own timestamp sub-directory under
           ``{log_dir}/{agent_name}/{timestamp}/shell_audit.log``.
        """
        # -- try to share the run directory created by the main logger ------
        if not self._log_dir:
            try:
                from src.lib.logging.logger_manager import get_current_run_log_dir
                run_dir = get_current_run_log_dir()
                if run_dir is not None:
                    run_dir.mkdir(parents=True, exist_ok=True)
                    return run_dir / "shell_audit.log"
            except Exception:
                pass

        # -- fallback: build our own timestamp directory --------------------
        base_dir_str = self._log_dir or C.get_nested(
            "logging", "dir", default=".logs",
        )
        base_dir = Path(base_dir_str).expanduser()
        if not base_dir.is_absolute():
            try:
                agent_root = Path(C.agent_root).resolve()
            except Exception:
                agent_root = Path.cwd().resolve()
            base_dir = agent_root / base_dir
        base_dir = base_dir.resolve()

        safe_name = _sanitize_component(self._agent_name)
        now = datetime.now()
        ts = now.strftime("%Y%m%d_%H%M%S")

        target_dir = base_dir / safe_name / ts
        suffix = 1
        while target_dir.exists():
            target_dir = base_dir / safe_name / f"{ts}_{suffix}"
            suffix += 1
        target_dir.mkdir(parents=True, exist_ok=True)

        return target_dir / "shell_audit.log"

    # -- core write ---------------------------------------------------------

    def _write_entry(
        self,
        event_type: str,
        command: str,
        *,
        check_id: str = "",
        message: str = "",
        suggestion: str = "",
        extra: Optional[dict] = None,
    ) -> None:
        """Write a single structured entry to the audit log file."""
        if not self.enabled:
            return
        if event_type != EVENT_POLICY_SNAPSHOT:
            self.log_effective_policy()

        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        lines = [
            f"[{now}] [{event_type}] agent={self._agent_name}",
        ]
        # Truncate very long commands for readability
        cmd_display = command if len(command) <= 500 else command[:497] + "..."
        lines.append(f"  command: {cmd_display}")

        if check_id:
            lines.append(f"  check: {check_id}")
        if message:
            lines.append(f"  message: {message}")
        if suggestion:
            lines.append(f"  suggestion: {suggestion}")
        if extra:
            for k, v in extra.items():
                lines.append(f"  {k}: {v}")

        lines.append("")  # blank separator between entries
        entry = "\n".join(lines) + "\n"

        with self._lock:
            try:
                with open(self.file_path, "a", encoding="utf-8") as f:
                    f.write(entry)
            except OSError as exc:
                # Never let audit logging crash the shell pipeline
                logger.debug(
                    "Failed to write shell audit log entry: %s", exc,
                )

    # -- public API ---------------------------------------------------------

    def log_effective_policy(self) -> None:
        """Log the effective shell security policy once per logger instance."""
        if not self.enabled or not self.log_policy_snapshot:
            return

        with self._lock:
            if self._policy_snapshot_logged:
                return
            self._policy_snapshot_logged = True

        allowed_commands, commands_source = _get_shell_config_with_source(
            "allowed_commands", default=None,
        )
        allowed_operators, operators_source = _get_shell_config_with_source(
            "allowed_operators", default=None,
        )
        security_checks, security_source = _get_shell_config_with_source(
            "security_checks", default={},
        )
        dangerous_paths, dangerous_source = _get_shell_config_with_source(
            "dangerous_paths", default=[],
        )
        block_destructive, block_source = _get_shell_config_with_source(
            "block_destructive", default=True,
        )
        sandbox, sandbox_source = _get_shell_config_with_source(
            "sandbox", default={},
        )

        security_enabled, security_disabled = _format_security_checks(
            security_checks,
        )
        sandbox = sandbox if isinstance(sandbox, dict) else {}
        sandbox_enabled = _coerce_bool(sandbox.get("enabled", False))
        sandbox_mode = str(sandbox.get("mode", "bwrap"))
        sandbox_network = _coerce_bool(sandbox.get("network_isolation", False))

        extra = {
            "allowed_commands": _format_allow_policy(
                allowed_commands, "all command names allowed",
            ),
            "allowed_commands_source": commands_source,
            "allowed_operators": _format_allow_policy(
                allowed_operators, "all shell operators allowed",
            ),
            "allowed_operators_source": operators_source,
            "command_success_logging": str(self.log_success).lower(),
            "security_checks_enabled": security_enabled,
            "security_checks_disabled": security_disabled,
            "security_checks_source": security_source,
            "block_destructive": (
                f"{str(_coerce_bool(block_destructive)).lower()} ({block_source})"
            ),
            "dangerous_paths": f"{_format_list(dangerous_paths)} ({dangerous_source})",
            "sandbox_enabled": f"{str(sandbox_enabled).lower()} ({sandbox_source})",
            "sandbox_mode": sandbox_mode,
            "sandbox_network_isolation": str(sandbox_network).lower(),
        }

        self._write_entry(
            EVENT_POLICY_SNAPSHOT,
            "<shell policy>",
            message=(
                "Effective shell policy captured before command execution. "
                "This records all-allow defaults even when no command is blocked."
            ),
            suggestion=_SUGGESTIONS[EVENT_POLICY_SNAPSHOT],
            extra=extra,
        )

    def log_security_block(
        self,
        command: str,
        check_id: str,
        message: str,
    ) -> None:
        """Log a command blocked by a security check.

        Args:
            command: The raw shell command string.
            check_id: The security check identifier (e.g. ``destructive_patterns``).
            message: The human-readable block reason.
        """
        suggestion = _SUGGESTIONS[EVENT_SECURITY_BLOCK].format(
            check_id=check_id,
        )
        self._write_entry(
            EVENT_SECURITY_BLOCK,
            command,
            check_id=check_id,
            message=message,
            suggestion=suggestion,
        )

    def log_path_violation(
        self,
        command: str,
        message: str,
        path: str = "",
    ) -> None:
        """Log a path boundary violation.

        Args:
            command: The raw shell command string.
            message: The human-readable violation description.
            path: The offending path (used in the suggestion).
        """
        suggestion = _SUGGESTIONS[EVENT_PATH_VIOLATION].format(
            path=path or "<target_path>",
        )
        self._write_entry(
            EVENT_PATH_VIOLATION,
            command,
            message=message,
            suggestion=suggestion,
        )

    def log_whitelist_rejection(
        self,
        command: str,
        message: str,
        name: str = "",
    ) -> None:
        """Log a command/operator whitelist rejection.

        Args:
            command: The raw shell command string.
            message: The human-readable rejection reason.
            name: The rejected command or operator name.
        """
        is_operator = (
            message.startswith("Operator not allowed:")
            or (name in {"&&", "|", ";", "||", "&"})
        )
        suggestion_key = (
            "OPERATOR_WHITELIST_REJECT"
            if is_operator
            else EVENT_WHITELIST_REJECT
        )
        suggestion = _SUGGESTIONS[suggestion_key].format(
            name=name or ("<operator>" if is_operator else "<command>"),
        )
        self._write_entry(
            EVENT_WHITELIST_REJECT,
            command,
            message=message,
            suggestion=suggestion,
        )

    def log_stall_detected(
        self,
        command: str,
        pid: int,
        elapsed: float,
        stall_message: str = "",
    ) -> None:
        """Log a foreground stall detection event.

        Args:
            command: The shell command that stalled.
            pid: Process ID of the stalled process.
            elapsed: Seconds elapsed before stall was detected.
            stall_message: The stall watchdog's message.
        """
        self._write_entry(
            EVENT_STALL_DETECTED,
            command,
            message=stall_message or f"Process {pid} stalled after {elapsed:.0f}s",
            suggestion=_SUGGESTIONS[EVENT_STALL_DETECTED],
            extra={"pid": str(pid), "elapsed_seconds": f"{elapsed:.1f}"},
        )

    def log_timeout(
        self,
        command: str,
        timeout: float,
        promoted: bool = False,
        task_id: str = "",
    ) -> None:
        """Log a command timeout event.

        Args:
            command: The shell command that timed out.
            timeout: The timeout value in seconds.
            promoted: Whether the command was promoted to background.
            task_id: Background task ID if promoted.
        """
        if promoted:
            event = EVENT_BACKGROUND_PROMOTION
            suggestion = _SUGGESTIONS[EVENT_BACKGROUND_PROMOTION]
            extra = {"timeout_seconds": str(timeout), "task_id": task_id}
        else:
            event = EVENT_TIMEOUT
            suggestion = _SUGGESTIONS[EVENT_TIMEOUT]
            extra = {"timeout_seconds": str(timeout)}

        self._write_entry(
            event,
            command,
            message=f"Command exceeded timeout of {timeout}s",
            suggestion=suggestion,
            extra=extra,
        )

    def log_sandbox_wrap(self, command: str, sandbox_mode: str) -> None:
        """Log a sandbox wrapping event.

        Args:
            command: The original command before wrapping.
            sandbox_mode: The sandbox mode used (e.g. ``seatbelt``).
        """
        self._write_entry(
            EVENT_SANDBOX_WRAP,
            command,
            message=f"Command sandboxed via {sandbox_mode}",
            suggestion=_SUGGESTIONS[EVENT_SANDBOX_WRAP],
        )

    def log_sandbox_unavailable(
        self,
        command: str,
        sandbox_mode: str,
        reason: str,
    ) -> None:
        """Log when sandboxing was requested but could not be applied."""
        self._write_entry(
            EVENT_SANDBOX_UNAVAILABLE,
            command,
            message=f"Sandbox mode {sandbox_mode} unavailable: {reason}",
            suggestion=_SUGGESTIONS[EVENT_SANDBOX_UNAVAILABLE],
        )

    def log_command_success(
        self,
        command: str,
        exit_code: int = 0,
        duration: float = 0.0,
    ) -> None:
        """Log a successful command execution (only if ``log_success`` is enabled).

        Args:
            command: The executed shell command.
            exit_code: Process exit code.
            duration: Execution time in seconds.
        """
        if not self.log_success:
            return
        self._write_entry(
            EVENT_COMMAND_SUCCESS,
            command,
            message=f"exit_code={exit_code}, duration={duration:.2f}s",
        )

    def get_log_path(self) -> Optional[str]:
        """Return the audit log file path, or None if audit is disabled."""
        if not self.enabled:
            return None
        return str(self.file_path)


# ---------------------------------------------------------------------------
# Factory / singleton accessor
# ---------------------------------------------------------------------------

def get_shell_audit_logger(
    agent_name: Optional[str] = None,
    log_dir: Optional[str] = None,
) -> ShellAuditLogger:
    """Get or create the audit logger for the current agent.

    If *agent_name* is not provided, it is resolved from the current
    task context (``get_current_agent_name()``).  Falls back to
    ``"_global"`` when no agent context is available.

    Instances are cached per agent name for the lifetime of the process.

    Args:
        agent_name: Override the agent name (useful for testing).
        log_dir: Override the base log directory (useful for testing).

    Returns:
        A :class:`ShellAuditLogger` bound to the resolved agent name.
    """
    if agent_name is None:
        try:
            from src.trace import get_current_agent_name
            agent_name = get_current_agent_name() or "_global"
        except Exception:
            agent_name = "_global"

    with _CACHE_LOCK:
        key = f"{agent_name}:{log_dir or ''}"
        if key not in _AUDIT_LOGGERS:
            _AUDIT_LOGGERS[key] = ShellAuditLogger(agent_name, log_dir=log_dir)
        return _AUDIT_LOGGERS[key]


def reset_audit_loggers() -> None:
    """Clear the cached audit logger instances (for testing only)."""
    with _CACHE_LOCK:
        _AUDIT_LOGGERS.clear()
