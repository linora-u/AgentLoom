"""Command (shell) hook executor.

Spawns a subprocess to execute a shell command, passing hook input as
JSON via stdin and parsing the JSON response from stdout.  Uses the
shared ``build_subprocess_env()`` layer for environment sanitisation.

Aligned with the upstream ``execCommandHook()`` implementation:
- stdin: JSON payload + trailing newline (required for ``read -r``)
- Exit code protocol: 0 = success, 2 = blocking, other = non-blocking
- First-line async detection via streaming ``readline()``
- Process-group isolation (``os.setsid``) with SIGTERM->SIGKILL escalation
- Environment variables: AGENTLOOM_PROJECT_DIR, AGENT_NAME, etc.

Timeout defaults to 600 s (10 minutes), matching upstream.
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import tempfile
import threading
from typing import Any, Dict, Optional

from src.lib.logging import get_logger

from .hook_helpers import add_arguments_to_prompt, kill_hook_process_group
from .hook_schemas import (
    AsyncHookOutput,
    SyncHookOutput,
    is_async_output,
    parse_hook_output,
    process_hook_output,
)
from .types import CommandHook, HookResult

logger = get_logger(__name__)


class _TimeoutSentinel(Exception):
    """Internal sentinel raised when the watchdog timer fires during I/O."""
    pass


# Default timeout for command hooks: 10 minutes (aligned with upstream).
DEFAULT_COMMAND_TIMEOUT: float = 600.0

# Maximum env-var payload size before falling back to temp file.
_MAX_ENV_BYTES: int = 65_536


def _build_hook_env(hook_input: Dict[str, Any]) -> Dict[str, str]:
    """Build the subprocess environment with hook-specific variables.

    Uses the shared sanitisation layer from ``src.tools.shell``.
    """
    try:
        from src.tools.shell.subprocess_env import build_subprocess_env
        env = build_subprocess_env()
    except ImportError:
        env = os.environ.copy()

    from src.trace.task_context import (
        get_current_agent_name,
        get_current_task_id,
    )

    env["AGENTLOOM_PROJECT_DIR"] = os.getcwd()
    env["AGENT_NAME"] = get_current_agent_name() or "default"
    env["TASK_ID"] = get_current_task_id() or ""
    env["TOOL_NAME"] = str(hook_input.get("tool_name", ""))
    env["HOOK_EVENT"] = str(hook_input.get("hook_event_name", ""))
    env["STEP_NUMBER"] = str(hook_input.get("step_number") or "")

    return env


def exec_command_hook(
    hook: CommandHook,
    hook_input: Dict[str, Any],
    *,
    cwd: Optional[str] = None,
    timeout: Optional[float] = None,
    async_registry: Optional[Any] = None,
) -> HookResult:
    """Execute a command hook and return the result.

    Uses ``subprocess.Popen`` with ``os.setsid`` for process-group
    isolation.  Streaming ``readline()`` detects async markers on the
    first stdout line so the process can be immediately backgrounded.
    On timeout the entire process tree is killed via SIGTERM->SIGKILL
    escalation.

    Parameters
    ----------
    hook:
        The command hook definition.
    hook_input:
        Full hook input payload (serialised to JSON for stdin).
    cwd:
        Working directory for the subprocess.  Defaults to ``os.getcwd()``.
    timeout:
        Override timeout in seconds.  ``None`` uses the hook's own timeout
        or the default (600 s).
    async_registry:
        Optional ``AsyncHookRegistry`` instance.  If provided and the
        hook emits ``{"async": true}`` on its first stdout line, the
        running ``Popen`` handle is handed off to this registry.
    """
    effective_timeout = timeout or hook.timeout or DEFAULT_COMMAND_TIMEOUT
    effective_cwd = cwd or os.getcwd()

    env = _build_hook_env(hook_input)

    # Serialise hook input as JSON payload for stdin.
    json_payload = json.dumps(hook_input, ensure_ascii=False, default=str)

    # Write full payload to temp file for large inputs (aligned with
    # the existing executors.py pattern).
    context_tmp_path: Optional[str] = None
    try:
        ctx_tmp = tempfile.NamedTemporaryFile(
            mode="w", suffix=".json", prefix="hook_ctx_",
            delete=False, encoding="utf-8",
        )
        ctx_tmp.write(json_payload)
        ctx_tmp.close()
        context_tmp_path = ctx_tmp.name
        env["HOOK_CONTEXT_JSON_FILE"] = context_tmp_path
    except Exception as exc:
        logger.debug("Failed to write hook context temp file: %s", exc)

    if len(json_payload.encode("utf-8")) <= _MAX_ENV_BYTES:
        env["HOOK_CONTEXT_JSON"] = json_payload

    # Add trailing newline for bash ``read -r line`` compatibility.
    stdin_data = json_payload + "\n"

    # --- Spawn with process-group isolation ---
    proc: Optional[subprocess.Popen] = None
    watchdog: Optional[threading.Timer] = None
    timed_out = False

    def _on_timeout():
        nonlocal timed_out
        timed_out = True
        if proc is not None:
            kill_hook_process_group(proc)

    try:
        proc = subprocess.Popen(
            hook.command,
            shell=True,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            cwd=effective_cwd,
            env=env,
            text=True,
            preexec_fn=os.setsid,  # Isolate into own process group
        )

        # Start watchdog BEFORE any blocking I/O so that the timer can
        # kill the process even if readline() or communicate() blocks.
        watchdog = threading.Timer(effective_timeout, _on_timeout)
        watchdog.daemon = True
        watchdog.start()

        # Write stdin and close (non-blocking from our side).
        try:
            proc.stdin.write(stdin_data)
            proc.stdin.close()
        except (BrokenPipeError, OSError) as exc:
            logger.debug("stdin write failed (process may have exited): %s", exc)

        # --- First-line async detection (streaming) ---
        # readline() blocks until one line is available or the process
        # exits (returns "").  The watchdog timer will kill the process
        # if it takes too long, causing readline() to return "".
        first_line = ""
        try:
            first_line = proc.stdout.readline()
        except Exception:
            pass

        if timed_out:
            # Watchdog fired during readline — skip to timeout handling
            raise _TimeoutSentinel()

        if first_line:
            first_parsed = parse_hook_output(first_line.rstrip())
            if is_async_output(first_parsed):
                # Cancel the watchdog — async hooks manage their own timeout.
                watchdog.cancel()
                watchdog = None

                logger.info(
                    "Command hook declared async execution: %s",
                    hook.command[:80],
                )
                async_timeout_ms = DEFAULT_COMMAND_TIMEOUT * 1000
                if isinstance(first_parsed, AsyncHookOutput) and first_parsed.async_timeout:
                    async_timeout_ms = first_parsed.async_timeout

                if async_registry is not None:
                    from .async_hook_registry import PendingAsyncHook
                    async_registry.register(PendingAsyncHook(
                        process_id=f"async_hook_{proc.pid}",
                        hook_id=hook.command[:80],
                        hook_event=str(hook_input.get("hook_event_name", "")),
                        hook_name=hook.command[:80],
                        command=hook.command,
                        timeout_ms=int(async_timeout_ms),
                        process_handle=proc,
                    ))
                    # Do NOT clean up the temp file here -- the async
                    # process may still be reading it.
                    return HookResult(
                        success=True,
                        decision="allow",
                        outcome="success",
                        telemetry={"async": True, "command": hook.command},
                    )
                else:
                    return HookResult(
                        success=True,
                        decision="allow",
                        outcome="success",
                        telemetry={"async": True, "command": hook.command},
                    )

        # --- Synchronous path: read remaining output ---
        remaining_stdout = proc.stdout.read() or ""
        stderr_text = proc.stderr.read() or ""
        proc.wait()

    except _TimeoutSentinel:
        pass  # Fall through to timeout handling below
    except Exception as exc:
        logger.error("Command hook execution failed: %s", exc)
        if proc is not None:
            kill_hook_process_group(proc)
        return HookResult(
            success=False,
            decision="block",
            outcome="non_blocking_error",
            reason=f"Command hook execution failed: {exc}",
        )
    finally:
        if watchdog is not None:
            watchdog.cancel()
        if context_tmp_path:
            try:
                os.remove(context_tmp_path)
            except OSError:
                pass

    if timed_out:
        logger.warning(
            "Command hook timed out after %ss: %s",
            effective_timeout, hook.command[:80],
        )
        return HookResult(
            success=False,
            decision="block",
            outcome="cancelled",
            reason=f"Command hook timed out after {effective_timeout}s",
        )

    # Combine first line with remaining stdout.
    stdout_text = (first_line + remaining_stdout).rstrip()
    stderr_text = stderr_text.rstrip()
    exit_code = proc.returncode

    # --- Process output ---
    return _process_command_result(hook, hook_input, stdout_text, stderr_text, exit_code)


def _process_command_result(
    hook: CommandHook,
    hook_input: Dict[str, Any],
    stdout_text: str,
    stderr_text: str,
    exit_code: int,
) -> HookResult:
    """Process subprocess output into a HookResult.

    Factored out so both sync and async code paths can reuse it.
    """
    # No stdout -> derive result purely from exit code
    if not stdout_text:
        if exit_code == 0:
            return HookResult(success=True, decision="allow", outcome="success")
        if exit_code == 2:
            return HookResult(
                success=False,
                decision="block",
                outcome="blocking",
                reason=stderr_text or "Command hook exited with blocking error (exit code 2)",
                blocking_error={
                    "blocking_error": stderr_text or "exit code 2",
                    "command": hook.command,
                },
            )
        # Non-blocking error
        logger.warning(
            "Command hook exited with code %d (non-blocking): %s",
            exit_code, hook.command[:80],
        )
        return HookResult(
            success=False,
            decision="allow",
            outcome="non_blocking_error",
            reason=stderr_text or f"Command hook exited with code {exit_code}",
        )

    # Parse stdout as JSON
    parsed = parse_hook_output(stdout_text)

    # Valid sync JSON output
    if isinstance(parsed, SyncHookOutput):
        return process_hook_output(
            parsed,
            hook_event=str(hook_input.get("hook_event_name", "")),
            command=hook.command,
            exit_code=exit_code,
            stderr=stderr_text,
        )

    # Unparseable output -- treat based on exit code
    logger.warning("Command hook produced non-JSON output, treating as plain text")
    if exit_code == 0:
        return HookResult(success=True, decision="allow", outcome="success")
    if exit_code == 2:
        return HookResult(
            success=False,
            decision="block",
            outcome="blocking",
            reason=stderr_text or stdout_text,
            blocking_error={"blocking_error": stderr_text or stdout_text, "command": hook.command},
        )
    return HookResult(
        success=False,
        decision="allow",
        outcome="non_blocking_error",
        reason=stderr_text or f"Exit code {exit_code}",
    )
