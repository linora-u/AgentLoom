"""Shell process management — stateless subprocess execution engine.

Each command is executed as an independent subprocess call.  Session
state (CWD) is persisted through out-of-band temp files and replayed
into each new subprocess via the environment snapshot mechanism.

Architecture aligned with Claude Code's "stateless process + environment
snapshot + file-based output" design (Shell.ts, bashProvider.ts,
ShellSnapshot.ts, ShellCommand.ts).

Key design decisions:
- No long-lived PTY — avoids fragility, buffer limits, and hang risks
  from interactive commands.
- CWD tracked via ``pwd >| cwd_file`` (out-of-band, not embedded
  in stdout).
- Environment restored via snapshot (functions, aliases, options, PATH)
  captured once at session start.  No per-command ``env`` capture —
  ``export`` statements are ephemeral (same as Claude Code).
- Snapshot and login shell (``-l``) are mutually exclusive: when a
  snapshot is available the ``-l`` flag is skipped, avoiding the
  overhead of sourcing ``~/.bashrc`` on every command.
- Large output written directly to file descriptors; Python never
  buffers the full stream in memory.
- Process tree cleanup via ``os.killpg`` (SIGTERM -> SIGKILL).
"""

import functools
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
from dataclasses import dataclass, field
from typing import Dict, List, Optional

from src.lib.config import C
from src.lib.logging import get_logger
from src.tools.shell.ansi_stripper import strip_ansi
from src.tools.shell.pipe_redirect import rearrange_pipe_command
from src.tools.shell.shell_session import ShellSession
from src.tools.shell.shell_snapshot import create_snapshot, remove_snapshot
from src.tools.shell.stall_watchdog import detect_stall_prompt
from src.tools.shell.subprocess_env import build_subprocess_env as _build_subprocess_env
from src.tools.shell.tree_kill import SizeWatchdog, graceful_kill

logger = get_logger(__name__)


# ---------------------------------------------------------------------------
# Execution result
# ---------------------------------------------------------------------------

@dataclass
class ExecResult:
    """Structured result from a subprocess execution.

    Replaces the raw ``(output, timed_out)`` tuple with richer
    information about how the command finished.
    """

    output: str = ""
    timed_out: bool = False
    exit_code: Optional[int] = None
    background_task_id: Optional[str] = None
    interrupted: bool = False


# ---------------------------------------------------------------------------
# Shell detection
# ---------------------------------------------------------------------------

# Only bash and zsh are supported — other shells (sh, csh, ksh, dash, tcsh)
# have incompatible syntax that causes unpredictable command behaviour.
_SUPPORTED_SHELLS = ("bash", "zsh")

# Directories to scan for shell binaries when $SHELL and which() both fail.
_SHELL_SEARCH_DIRS = (
    "/bin",
    "/usr/bin",
    "/usr/local/bin",
    "/opt/homebrew/bin",
)

WINDOWS_SHELL_FALLBACK_PATHS = (
    "C:\\Program Files\\PowerShell\\7\\pwsh.exe",
    "C:\\Windows\\System32\\WindowsPowerShell\\v1.0\\powershell.exe",
    "C:\\Windows\\System32\\cmd.exe",
    "C:\\Program Files\\Git\\bin\\bash.exe",
)

# Legacy constant kept for backward compatibility with tests.
AGENT_SHELL_PROMPT_ENV = "AGENT_SHELL_PROMPT"

# Maximum output file size before the size watchdog kills the process.
_MAX_OUTPUT_BYTES = 100_000_000  # 100 MB


def _foreground_stall_threshold(timeout: int) -> float:
    """Bound foreground stall detection so it can fire before tool timeout.

    Background tasks can afford a 45s prompt threshold.  A foreground tool call
    blocks the agent executor, so prompt-like output must be classified sooner
    than the command timeout; otherwise it is promoted to background and leaves
    the interactive process alive.
    """
    configured = float(C.get_nested(
        "shell_settings", "background_tasks",
        "stall_threshold_seconds", default=45,
    ))
    half_timeout = max(float(timeout) * 0.5, 1.0)
    return max(1.0, min(configured, half_timeout, 15.0))


def _foreground_stall_poll_interval(stall_threshold: float) -> float:
    return max(0.2, min(1.0, stall_threshold / 2.0))


def _is_executable(shell_path: str) -> bool:
    """Two-tier executability check.

    Tier 1: Fast permission-bit check via os.access(X_OK).
    Tier 2: Fallback — actually run ``<shell> --version`` (handles Nix and
            other environments where permission bits are unreliable).
    """
    if not shell_path or not os.path.isfile(shell_path):
        return False
    try:
        if os.access(shell_path, os.X_OK):
            return True
    except (OSError, ValueError):
        pass

    try:
        subprocess.run(
            [shell_path, "--version"],
            timeout=2,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        return True
    except Exception:
        return False


def _is_supported_shell(shell_path: str) -> bool:
    """Return True if *shell_path* refers to a bash or zsh binary."""
    if not shell_path:
        return False
    name = os.path.basename(shell_path).lower()
    return any(s in name for s in _SUPPORTED_SHELLS)


@functools.lru_cache(maxsize=1)
def find_suitable_shell() -> str:
    """Determine the best available shell binary.

    Resolution order:

    1. ``$SHELL`` — only if it refers to bash/zsh **and** passes
       ``_is_executable()``.
    2. ``shutil.which("zsh")`` / ``shutil.which("bash")`` — order
       respects user preference inferred from ``$SHELL``.
    3. Hardcoded directory scan — ``/bin``, ``/usr/bin``,
       ``/usr/local/bin``, ``/opt/homebrew/bin`` x preferred order.
    4. All failed -> ``FileNotFoundError``.

    The result is cached (``lru_cache``) so detection runs at most once
    per process lifetime.  Call ``find_suitable_shell.cache_clear()`` to
    force re-detection.
    """
    env_shell = os.environ.get("SHELL", "").strip()
    prefer_bash = "bash" in env_shell if env_shell else True

    if env_shell and _is_supported_shell(env_shell) and _is_executable(env_shell):
        logger.debug("Using $SHELL: %s", env_shell)
        return env_shell

    if env_shell and not _is_supported_shell(env_shell):
        logger.debug(
            "$SHELL=%s is not bash/zsh -- skipping, falling back to detection",
            env_shell,
        )

    zsh_path = shutil.which("zsh")
    bash_path = shutil.which("bash")

    shell_order = ("bash", "zsh") if prefer_bash else ("zsh", "bash")
    candidates: list[str] = []

    which_results = {"bash": bash_path, "zsh": zsh_path}
    for name in shell_order:
        path = which_results.get(name)
        if path and path not in candidates:
            candidates.append(path)

    for name in shell_order:
        for directory in _SHELL_SEARCH_DIRS:
            full = os.path.join(directory, name)
            if full not in candidates:
                candidates.append(full)

    for path in candidates:
        if _is_executable(path):
            logger.debug("Detected shell: %s", path)
            return path

    raise FileNotFoundError(
        "No suitable shell found. AgentLoom requires bash or zsh. "
        "Please ensure a valid shell is installed and the SHELL "
        "environment variable is set."
    )


def _resolve_shell_path_windows() -> str:
    """Resolve the shell executable for Windows systems (best-effort)."""
    shell_path = os.environ.get("COMSPEC", "").strip()
    if shell_path and _is_executable(shell_path):
        return shell_path

    for path in WINDOWS_SHELL_FALLBACK_PATHS:
        if os.path.isfile(path) and os.access(path, os.X_OK):
            logger.warning(
                "Windows shell not found via $COMSPEC. Falling back to: %s", path
            )
            return path

    raise FileNotFoundError(
        "No suitable Windows shell found. Tried $COMSPEC and standard "
        "PowerShell/cmd.exe locations."
    )


# ---------------------------------------------------------------------------
# ShellProcess — the main execution engine
# ---------------------------------------------------------------------------

class ShellProcess:
    """Execute shell commands via stateless subprocesses.

    Two execution modes:

    **Session-scoped** (session_scoped=True):
        Each run() call spawns a fresh subprocess, but session state
        (CWD only) is carried over from the previous call via the
        ShellSession state tracker.  A shell snapshot restores aliases,
        functions, shell options, and PATH; per-command ``export`` changes
        are not persisted.

    **Standalone** (session_scoped=False):
        Each run() is fully isolated — no state is preserved.
    """

    def __init__(
        self,
        strip_newlines: bool = False,
        return_err_output: bool = False,
        session_scoped: bool = False,
        timeout: int = 120,
        load_profile: bool = True,
    ):
        self.strip_newlines = strip_newlines
        self.return_err_output = return_err_output
        self.session_scoped = session_scoped
        self.timeout = timeout
        self.load_profile = load_profile

        self.is_windows = sys.platform.startswith("win")

        # Resolve shell path once and cache on the instance.
        if self.is_windows:
            self._shell_path = _resolve_shell_path_windows()
        else:
            self._shell_path = find_suitable_shell()

        # Session state for session-scoped mode.
        self._session: Optional[ShellSession] = None
        self._snapshot_path: Optional[str] = None

        if self.session_scoped:
            self._init_session()

    # ------------------------------------------------------------------
    # Session initialisation
    # ------------------------------------------------------------------

    def _init_session(self) -> None:
        """Initialise the session state tracker and environment snapshot."""
        self._session = ShellSession()

        if self.load_profile and not self.is_windows:
            env = _build_subprocess_env()
            self._snapshot_path = create_snapshot(self._shell_path, env=env)
            if self._snapshot_path:
                logger.debug("Session snapshot ready: %s", self._snapshot_path)
            else:
                logger.debug("Snapshot unavailable -- will use login shell mode")

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    @property
    def cwd(self) -> Optional[str]:
        """Current working directory of the shell session.

        Updated after every session-scoped command via the out-of-band CWD
        tracking file.  Returns None for standalone processes or before
        any command has been executed.
        """
        if self._session:
            return self._session.cwd
        return None

    def run(self, command: str) -> str:
        """Execute a shell command.

        In session-scoped mode, CWD state is preserved across calls.
        In standalone mode, each call is fully isolated.
        """
        if self.session_scoped:
            return self._run_session_scoped(command)
        else:
            return self._run_standalone(command)

    # ------------------------------------------------------------------
    # Standalone execution
    # ------------------------------------------------------------------

    def _run_standalone(self, command: str) -> str:
        """Run a command as an isolated subprocess."""
        if self.is_windows:
            command = f"chcp 65001 >nul & {command}"
            try:
                result = subprocess.run(
                    command,
                    shell=True,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    timeout=self.timeout,
                    text=True,
                    errors="replace",
                    env=_build_subprocess_env(),
                )
                return self._format_output(result.stdout or "")
            except subprocess.TimeoutExpired as e:
                partial = e.stdout or ""
                if isinstance(partial, bytes):
                    partial = partial.decode(errors="replace")
                return self._format_output(
                    f"{partial}\n\n"
                    f"[Timeout Error: Command took longer than "
                    f"{self.timeout} seconds]"
                )
            except Exception as e:
                return self._format_output(f"Execution failed: {str(e)}")

        env = _build_subprocess_env()
        is_zsh = "zsh" in os.path.basename(self._shell_path).lower()
        composite = command
        if not self.load_profile and is_zsh:
            composite = (
                "setopt NO_EXTENDED_GLOB 2>/dev/null || true ; "
                f"{command}"
            )

        if self.load_profile:
            shell_args = [self._shell_path, "-l", "-c", composite]
        elif "bash" in os.path.basename(self._shell_path).lower():
            shell_args = [
                self._shell_path,
                "--noprofile",
                "--norc",
                "-c",
                composite,
            ]
        else:
            shell_args = [self._shell_path, "-c", composite]

        try:
            result = self._exec_subprocess(
                shell_args, env, self.timeout, command=command,
            )
        except Exception as e:
            result = ExecResult(output=f"Execution failed: {str(e)}")

        return self._render_exec_result(result)

    # ------------------------------------------------------------------
    # Session-scoped execution (stateless subprocess + CWD state)
    # ------------------------------------------------------------------

    def _run_session_scoped(self, command: str) -> str:
        """Run a command with session state preservation.

        Each call spawns a new subprocess.  Previous session CWD is
        restored and the environment is replayed from the snapshot:

        1. cd <cwd>            -- restore working directory
        2. source snapshot.sh  -- restore functions/aliases/options/PATH
        3. eval "user_command" -- run the actual command
        4. pwd >| cwd_file    -- capture new logical CWD

        Snapshot and login-shell are mutually exclusive (aligned with
        Claude Code's ``skipLoginShell = lastSnapshotFilePath !== undefined``):
        - Snapshot available  → source snapshot only, no ``-l`` flag
        - Snapshot unavailable → use ``-l`` so the shell reads ~/.bashrc
        """
        if self._session is None:
            self._init_session()

        session = self._session
        env = _build_subprocess_env()

        # Build the composite command string.
        parts = []

        # 1. Restore CWD
        if session.cwd and os.path.isdir(session.cwd):
            escaped_cwd = session.cwd.replace("'", "'\\''")
            parts.append(f"cd '{escaped_cwd}'")

        # 2. Source snapshot OR fall back to login shell.
        # When the snapshot is available it already contains functions,
        # aliases, shell options, and PATH captured from the login shell
        # at session start.  Re-sourcing ~/.bashrc on every call is
        # unnecessary overhead (100-300ms).
        has_snapshot = (
            self._snapshot_path
            and os.path.exists(self._snapshot_path)
        )
        is_zsh = "zsh" in os.path.basename(self._shell_path).lower()

        if has_snapshot:
            parts.append(f"source '{self._snapshot_path}' 2>/dev/null || true")
        elif self.load_profile:
            # No snapshot — extglob protection must be injected manually.
            if is_zsh:
                parts.append("setopt NO_EXTENDED_GLOB 2>/dev/null || true")
            else:
                parts.append("shopt -u extglob 2>/dev/null || true")

        if not self.load_profile and not has_snapshot:
            # Neither profile nor snapshot — still inject extglob protection.
            if is_zsh:
                parts.append("setopt NO_EXTENDED_GLOB 2>/dev/null || true")
            else:
                parts.append("shopt -u extglob 2>/dev/null || true")

        # 3. User command (eval for alias expansion after sourcing).
        # Apply pipe redirect normalization to prevent stdin-related hangs
        # in piped commands (e.g. rg foo | wc -l).
        normalized_cmd = rearrange_pipe_command(command)
        escaped_cmd = normalized_cmd.replace("'", "'\\''")
        parts.append(f"eval '{escaped_cmd}'")
        parts.append("__agentloom_ec=$?")

        # 4. CWD tracking (out-of-band file write).  Use shell-logical
        # pwd so session state preserves the path spelling a user cd'ed to.
        parts.append(f"pwd >| '{session.cwd_file}'")

        # 5. Propagate original exit code
        parts.append("exit $__agentloom_ec")

        # Assemble: CWD restore must succeed (&&), tracking always runs (;)
        if session.cwd and os.path.isdir(session.cwd):
            cwd_part = parts[0]
            rest_parts = parts[1:]
            composite = cwd_part + " && " + " ; ".join(rest_parts)
        else:
            composite = " ; ".join(parts)

        # Determine spawn args.
        # Snapshot and -l are mutually exclusive: when the snapshot is
        # available the login shell flag is skipped (the snapshot already
        # contains everything from the login initialisation).
        if has_snapshot:
            shell_args = [self._shell_path, "-c", composite]
        elif self.load_profile:
            shell_args = [self._shell_path, "-l", "-c", composite]
        elif "bash" in os.path.basename(self._shell_path).lower():
            shell_args = [
                self._shell_path,
                "--noprofile",
                "--norc",
                "-c",
                composite,
            ]
        else:
            shell_args = [self._shell_path, "-c", composite]

        # Execute as a new subprocess
        try:
            result = self._exec_subprocess(
                shell_args, env, self.timeout, command=command,
            )
        except Exception as e:
            result = ExecResult(output=f"Execution failed: {str(e)}")

        # Update session state from tracking files (CWD only).
        session.update_cwd_from_file()

        return self._render_exec_result(result)

    def _render_exec_result(self, result: ExecResult) -> str:
        output = result.output
        if result.background_task_id:
            output = (
                f"{output}\n\n"
                f"[Background Task: {result.background_task_id}]\n"
                f"Command promoted to background after {self.timeout}s timeout.\n"
                f"Use check_background_task('{result.background_task_id}') to monitor."
            )
        elif result.timed_out:
            output = (
                f"{output}\n\n"
                f"[Timeout Error: Command took longer than "
                f"{self.timeout} seconds]"
            )
        return self._format_output(output)

    def _exec_subprocess(
        self,
        args: list,
        env: dict,
        timeout: int,
        command: str = "",
    ) -> ExecResult:
        """Spawn a subprocess and collect output.

        stdout/stderr are written directly to a temp file FD.  A
        SizeWatchdog monitors the file and kills the process if it
        grows too large.

        On timeout, if background tasks are enabled the process is
        promoted to a background task instead of being killed.  The
        output file is preserved and the process continues running.

        Returns:
            An ExecResult with output, status flags, and optional
            background_task_id.
        """
        output_file = None
        watchdog = None
        fg_stall = None
        out_fd = -1
        promoted = False  # True if promoted to background

        try:
            fd, output_file = tempfile.mkstemp(
                prefix="agentloom_output_", suffix=".txt"
            )
            # Close the fd from mkstemp and open with O_WRONLY | O_APPEND
            os.close(fd)
            out_fd = os.open(output_file, os.O_WRONLY | os.O_APPEND)

            proc = subprocess.Popen(
                args,
                stdout=out_fd,
                stderr=out_fd,
                stdin=subprocess.DEVNULL,
                env=env,
                start_new_session=True,
            )

            # Close our copy of the write FD — child has its own.
            os.close(out_fd)
            out_fd = -1

            # Start size watchdog for long-running commands.
            watchdog = SizeWatchdog(
                proc.pid, output_file, max_bytes=_MAX_OUTPUT_BYTES
            )
            watchdog.start()

            # Start foreground stall watchdog to detect interactive
            # prompts (y/n, Continue?, etc.) during the wait period.
            from src.tools.shell.stall_watchdog import StallWatchdog

            stall_threshold = _foreground_stall_threshold(timeout)

            fg_stall = StallWatchdog(
                task_id=f"fg-{proc.pid}",
                output_path=output_file,
                poll_interval=_foreground_stall_poll_interval(stall_threshold),
                stall_threshold=stall_threshold,
            )
            fg_stall.start()

            # ---- Polling loop (V4.1) ----
            # Instead of a single blocking proc.wait(timeout=120), we
            # poll every 1 second.  This allows the main thread to
            # detect a stall (set by the StallWatchdog daemon thread)
            # within ~1 second and kill the hung process immediately —
            # rather than waiting the full timeout.
            stall_killed = False
            elapsed = 0.0
            _POLL_INTERVAL = 1.0

            while elapsed < timeout:
                try:
                    proc.wait(timeout=_POLL_INTERVAL)
                    break  # Process exited normally.
                except subprocess.TimeoutExpired:
                    elapsed += _POLL_INTERVAL
                    # Check if the stall watchdog detected an
                    # interactive prompt while the process is still
                    # running.  The proc.poll() guard prevents a
                    # race condition where the process exits at the
                    # exact moment the watchdog sets stall_message.
                    if (
                        fg_stall
                        and fg_stall.stall_message
                        and proc.poll() is None
                    ):
                        logger.info(
                            "Foreground stall detected for pid %d "
                            "after %.0fs — killing process",
                            proc.pid, elapsed,
                        )
                        # Write to per-agent shell audit log
                        try:
                            from src.tools.shell.shell_audit_log import (
                                get_shell_audit_logger,
                            )
                            audit = get_shell_audit_logger()
                            audit.log_stall_detected(
                                command=command,
                                pid=proc.pid,
                                elapsed=elapsed,
                                stall_message=fg_stall.stall_message,
                            )
                        except Exception:
                            pass
                        graceful_kill(proc.pid)
                        try:
                            proc.wait(timeout=5)
                        except subprocess.TimeoutExpired:
                            pass
                        stall_killed = True
                        break
            else:
                # elapsed >= timeout, no stall detected — fall through
                # to existing timeout handling (background promotion or
                # kill).
                partial = ""
                try:
                    with open(output_file, "r", errors="replace") as f:
                        partial = f.read()
                except Exception:
                    pass

                prompt_message = detect_stall_prompt(f"fg-{proc.pid}", output_file)
                if prompt_message and proc.poll() is None:
                    logger.info(
                        "Foreground prompt detected at timeout for pid %d — "
                        "killing instead of promoting",
                        proc.pid,
                    )
                    try:
                        from src.tools.shell.shell_audit_log import (
                            get_shell_audit_logger,
                        )
                        audit = get_shell_audit_logger()
                        audit.log_stall_detected(
                            command=command,
                            pid=proc.pid,
                            elapsed=elapsed,
                            stall_message=prompt_message,
                        )
                    except Exception:
                        pass
                    graceful_kill(proc.pid)
                    try:
                        proc.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        pass
                    return ExecResult(
                        output=(
                            f"{partial}\n\n"
                            f"[Stall Warning: {prompt_message}]"
                        ),
                        interrupted=True,
                        exit_code=-9,
                    )

                # Check if auto-background is enabled.
                bg_enabled = C.get_nested(
                    "shell_settings", "background_tasks", "enabled",
                    default=True,
                )
                auto_bg = C.get_nested(
                    "shell_settings", "background_tasks",
                    "auto_background_on_timeout", default=True,
                )

                if bg_enabled and auto_bg:
                    # Promote to background instead of killing.
                    task_id = self._promote_to_background(
                        proc, output_file, command, watchdog,
                    )
                    promoted = True
                    # Audit the background promotion
                    try:
                        from src.tools.shell.shell_audit_log import (
                            get_shell_audit_logger,
                        )
                        audit = get_shell_audit_logger()
                        audit.log_timeout(
                            command=command,
                            timeout=timeout,
                            promoted=True,
                            task_id=task_id,
                        )
                    except Exception:
                        pass
                    return ExecResult(
                        output=partial,
                        timed_out=True,
                        background_task_id=task_id,
                    )

                # Background disabled — kill the process.
                # Audit the timeout kill
                try:
                    from src.tools.shell.shell_audit_log import (
                        get_shell_audit_logger,
                    )
                    audit = get_shell_audit_logger()
                    audit.log_timeout(
                        command=command, timeout=timeout, promoted=False,
                    )
                except Exception:
                    pass
                graceful_kill(proc.pid)
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    pass
                return ExecResult(output=partial, timed_out=True)

            # ---- Handle stall-kill result ----
            if stall_killed:
                partial = ""
                try:
                    with open(output_file, "r", errors="replace") as f:
                        partial = f.read()
                except Exception:
                    pass
                return ExecResult(
                    output=(
                        f"{partial}\n\n"
                        f"[Stall Warning: {fg_stall.stall_message}]"
                    ),
                    interrupted=True,
                    exit_code=-9,
                )

            # ---- Process completed normally ----
            exit_code = proc.returncode
            try:
                with open(output_file, "r", errors="replace") as f:
                    output = f.read()
            except Exception:
                output = ""

            # Append foreground stall warning if the watchdog detected
            # a prompt but the process still exited on its own (e.g.
            # a timeout built into the command itself).
            if fg_stall.stall_message:
                output = (
                    f"{output}\n\n"
                    f"[Stall Warning: {fg_stall.stall_message}]"
                )

            return ExecResult(
                output=output, timed_out=False, exit_code=exit_code,
            )

        finally:
            # Stop foreground stall watchdog and wait for its thread to
            # finish before cleaning up the output file.  This prevents
            # a race where the watchdog thread tries to read a file
            # that has already been deleted.
            if fg_stall is not None:
                fg_stall.stop()
                if fg_stall._thread is not None:
                    fg_stall._thread.join(timeout=2.0)

            if not promoted:
                # Only clean up if we did NOT promote to background.
                if watchdog:
                    watchdog.stop()
                if out_fd >= 0:
                    try:
                        os.close(out_fd)
                    except OSError:
                        pass
                if output_file and os.path.exists(output_file):
                    try:
                        os.remove(output_file)
                    except OSError:
                        pass
            else:
                # Promoted — only close our FD, keep the file.
                if out_fd >= 0:
                    try:
                        os.close(out_fd)
                    except OSError:
                        pass

    def _promote_to_background(
        self,
        proc,
        output_path: str,
        command: str,
        watchdog: Optional[SizeWatchdog] = None,
    ) -> str:
        """Register a running process as a background task.

        The process keeps running; the output file is preserved.
        A background monitoring thread tracks the process exit.

        Returns the background task ID.
        """
        from src.tools.shell.background_task import BackgroundTaskRegistry

        registry = BackgroundTaskRegistry.get_instance()
        task_id = registry.register(
            process=proc,
            command=command,
            output_path=output_path,
            description=command[:80],
            size_watchdog=watchdog,
        )
        logger.info(
            "Command promoted to background task %s: pid=%d",
            task_id, proc.pid,
        )
        return task_id

    # ------------------------------------------------------------------
    # Output formatting
    # ------------------------------------------------------------------

    def _format_output(self, output: str) -> str:
        """Clean up raw subprocess output."""
        output = strip_ansi(output)
        output = output.strip()

        # Clean up terminal-style artifacts at the end (prompt chars).
        output = re.sub(r'[%$#>]\s*$', '', output).strip()
        return output

    # ------------------------------------------------------------------
    # Cleanup
    # ------------------------------------------------------------------

    def cleanup(self) -> None:
        """Release all resources held by this ShellProcess."""
        if self._session:
            self._session.cleanup()
            self._session = None
        if self._snapshot_path:
            remove_snapshot(self._snapshot_path)
            self._snapshot_path = None

    def __del__(self):
        try:
            self.cleanup()
        except Exception:
            pass


# ---------------------------------------------------------------------------
# ShellProcessRegistry — per-agent process management (singleton)
# ---------------------------------------------------------------------------

class ShellProcessRegistry:
    """Thread-safe singleton registry for per-agent ShellProcess instances.

    Each agent (identified by agent_id) gets its own dedicated
    ShellProcess, which is created on first use and reused on subsequent
    calls.  This allows the shell session to maintain CWD across multiple
    tool invocations within the same agent run. Environment variable exports
    remain per-command and do not persist.
    """

    _instance: Optional["ShellProcessRegistry"] = None
    _instance_lock: threading.Lock = threading.Lock()

    def __new__(cls) -> "ShellProcessRegistry":
        with cls._instance_lock:
            if cls._instance is None:
                cls._instance = super().__new__(cls)
                cls._instance._registry: Dict[str, ShellProcess] = {}
                cls._instance._registry_lock = threading.Lock()
        return cls._instance

    @classmethod
    def get_instance(cls) -> "ShellProcessRegistry":
        """Return the global singleton instance."""
        return cls()

    def get_or_create(
        self,
        agent_id: str,
        timeout: int = 120,
        session_scoped: bool = True,
        strip_newlines: bool = False,
        return_err_output: bool = True,
        load_profile: bool = True,
    ) -> ShellProcess:
        """Return the ShellProcess bound to *agent_id*, creating if needed."""
        with self._registry_lock:
            if agent_id not in self._registry:
                self._registry[agent_id] = ShellProcess(
                    timeout=timeout,
                    session_scoped=session_scoped,
                    strip_newlines=strip_newlines,
                    return_err_output=return_err_output,
                    load_profile=load_profile,
                )
            process = self._registry[agent_id]
            process.timeout = timeout
            process.strip_newlines = strip_newlines
            process.return_err_output = return_err_output
            process.load_profile = load_profile
            return process

    def release(self, agent_id: str) -> None:
        """Release and destroy the ShellProcess associated with *agent_id*."""
        with self._registry_lock:
            process = self._registry.pop(agent_id, None)
            if process is not None:
                try:
                    process.cleanup()
                except Exception:
                    pass

    def get_session_cwd(self, agent_id: str) -> Optional[str]:
        """Return the current working directory of the shell session for *agent_id*.

        Returns ``None`` if no session exists yet for the given agent or
        if the session has not executed any commands.  This is used by
        path validation to resolve relative paths against the shell
        session's actual CWD rather than the Python process's CWD.
        """
        with self._registry_lock:
            process = self._registry.get(agent_id)
            if process is not None:
                return process.cwd
        return None

    def registered_agent_ids(self) -> List[str]:
        """Return a snapshot of all currently registered agent IDs."""
        with self._registry_lock:
            return list(self._registry.keys())
