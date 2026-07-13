"""Hook execution engine (Shell) with validation and result building.

Provides ``create_hook_executor()`` as the single entry point for constructing
hook callables from skill-defined command code.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any, Callable, Dict, Optional

from src.lib.logging import get_logger
from ..hooks.types import HookContext, HookEvent, HookResult

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

SUPPORTED_HOOK_ACTION_TYPE = "command"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def merge_text(existing: Optional[str], extra: Optional[str]) -> Optional[str]:
    """Merge two optional text fields, joining with newline if both present."""
    if extra is None:
        return existing
    normalized = str(extra).rstrip()
    if not normalized:
        return existing
    if existing is None or not str(existing).strip():
        return normalized
    return f"{existing}\n{normalized}"


def invalid_hook_contract(
    reason: str,
    *,
    skill_name: Optional[str] = None,
    hook_event_name: Optional[str] = None,
    telemetry: Optional[Dict[str, Any]] = None,
) -> HookResult:
    """Return a blocking ``HookResult`` for a contract violation."""
    payload = dict(telemetry or {})
    if skill_name is not None:
        payload.setdefault("skill_name", skill_name)
    if hook_event_name is not None:
        payload.setdefault("hook_event_name", hook_event_name)
    return HookResult(
        success=False,
        decision="block",
        reason=reason,
        telemetry=payload,
    )


def _resolve_uv_managed_binary(binary_name: str) -> Optional[str]:
    """Find a binary installed in the same Python environment as the current interpreter.

    Works with any Python environment: venv, virtualenv, conda, poetry, uv, system Python, etc.
    ``sys.prefix`` always points to the active environment root, regardless of how it was created.
    """
    bin_subdir = "Scripts" if os.name == "nt" else "bin"
    env_bin = Path(sys.prefix) / bin_subdir / binary_name
    if os.name == "nt":
        env_bin_exe = Path(sys.prefix) / bin_subdir / f"{binary_name}.exe"
        if env_bin_exe.exists():
            return str(env_bin_exe)
    if env_bin.exists():
        return str(env_bin)
    return shutil.which(binary_name)


def _find_project_roots_for_hook_path(*starts: Optional[str]) -> list[Path]:
    """Find likely AgentLoom roots so hook commands can see local runtimes."""
    roots: list[Path] = []
    for raw_start in starts:
        if not raw_start:
            continue
        try:
            current = Path(raw_start).resolve()
        except OSError:
            continue
        if current.is_file():
            current = current.parent
        while current != current.parent:
            if (current / "config" / "llm.yaml").exists() or (current / "pyproject.toml").exists():
                if current not in roots:
                    roots.append(current)
                break
            current = current.parent
    return roots


def _prepend_hook_python_paths(
    env: Dict[str, str],
    *,
    skill_dir: Optional[str],
    execution_cwd: str,
) -> None:
    """Put the active Python environment on PATH for shell hook commands.

    Built-in skills historically use commands like ``python ./scripts/foo.py``.
    Some macOS/Linux environments only provide ``python3`` on the inherited
    PATH, while AgentLoom itself is launched through an explicit venv Python.
    Hook scripts should resolve against the same runtime without requiring a
    user-level ``python`` shim.
    """
    candidates: list[Path] = []

    executable = Path(sys.executable)
    candidates.append(executable.parent)
    try:
        candidates.append(executable.resolve().parent)
    except OSError:
        pass

    bin_subdir = "Scripts" if os.name == "nt" else "bin"
    candidates.append(Path(sys.prefix) / bin_subdir)

    for root in _find_project_roots_for_hook_path(
        skill_dir,
        execution_cwd,
        str(Path(__file__).resolve()),
    ):
        candidates.append(root / ".venv" / bin_subdir)

    existing_parts = [
        part for part in env.get("PATH", "").split(os.pathsep)
        if part
    ]
    merged: list[str] = []
    candidate_parts = [
        str(candidate) for candidate in candidates
        if candidate.is_dir()
    ]
    for path in [*candidate_parts, *existing_parts]:
        if path not in merged:
            merged.append(path)
    env["PATH"] = os.pathsep.join(merged)


# ---------------------------------------------------------------------------
# Structured result builder  (JSON payload → HookResult)
# ---------------------------------------------------------------------------

def build_structured_hook_result(
    payload: Dict[str, Any],
    context: HookContext,
    *,
    skill_name: Optional[str] = None,
    success: bool,
    fallback_reason: Optional[str] = None,
    telemetry: Optional[Dict[str, Any]] = None,
) -> HookResult:
    """Convert a JSON-like dict into a validated ``HookResult``."""
    structured_telemetry = dict(telemetry or {})
    payload_telemetry = payload.get("telemetry")
    if isinstance(payload_telemetry, dict):
        structured_telemetry.update(payload_telemetry)

    allowed_keys = {
        "decision",
        "modified_input",
        "modified_response",
        "agent_context",
        "user_message",
        "reason",
        "telemetry",
    }
    unsupported_keys = sorted(key for key in payload if key not in allowed_keys)
    if unsupported_keys:
        structured_telemetry["unsupported_keys"] = unsupported_keys
        return invalid_hook_contract(
            (
                "Unsupported hook payload keys for "
                f"{context.hook_event_name}: {', '.join(unsupported_keys)}"
            ),
            skill_name=skill_name,
            hook_event_name=context.hook_event_name,
            telemetry=structured_telemetry,
        )

    try:
        hook_result = HookResult(
            success=success,
            decision=str(payload.get("decision", "allow")),
            modified_input=(
                payload.get("modified_input")
                if isinstance(payload.get("modified_input"), dict)
                else None
            ),
            modified_response=(
                payload.get("modified_response")
                if isinstance(payload.get("modified_response"), dict)
                else None
            ),
            agent_context=(
                payload.get("agent_context")
                if isinstance(payload.get("agent_context"), str)
                else None
            ),
            user_message=(
                payload.get("user_message")
                if isinstance(payload.get("user_message"), str)
                else None
            ),
            reason=(
                payload.get("reason")
                if isinstance(payload.get("reason"), str)
                else None
            ),
            telemetry=structured_telemetry,
        )
    except ValueError as error:
        structured_telemetry["decision"] = payload.get("decision")
        return invalid_hook_contract(
            str(error),
            skill_name=skill_name,
            hook_event_name=context.hook_event_name,
            telemetry=structured_telemetry,
        )

    if fallback_reason is not None:
        hook_result.reason = merge_text(hook_result.reason, fallback_reason)

    return hook_result


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------

def validate_hook(
    code: str,
    skill_name: str,
    event_name: str,
    logger=None,
) -> None:
    """Validate hook source code using shellcheck."""
    _validate_shell_hook(code, skill_name, event_name, logger=logger)


def _validate_shell_hook(code: str, skill_name: str, event_name: str, logger=None) -> None:
    log = get_logger(logger, __name__)
    shellcheck_binary = _resolve_uv_managed_binary("shellcheck")
    if shellcheck_binary is None:
        log.warning(
            "shellcheck not found, skipping shell hook validation for skill '%s' event '%s'. "
            "Install via: uv add shellcheck-py && uv sync",
            skill_name,
            event_name,
        )
        return

    with tempfile.TemporaryDirectory(prefix="shell-hook-lint-") as temp_dir:
        temp_path = Path(temp_dir) / "__hook__.sh"
        temp_path.write_text(code, encoding="utf-8")
        completed = subprocess.run(
            [shellcheck_binary, "-s", "bash", "-S", "error", str(temp_path)],
            capture_output=True,
            text=True,
            check=False,
        )

    if completed.returncode == 0:
        return

    details = (completed.stdout or completed.stderr or "shellcheck rejected the hook").strip()
    raise ValueError(
        f"Skill '{skill_name}' hook {event_name} failed shell validation via shellcheck:\n{details}"
    )


# ---------------------------------------------------------------------------
# Executor factory
# ---------------------------------------------------------------------------

def create_hook_executor(
    code: str,
    skill_name: str,
    skill_dir: Optional[str],
    logger=None,
    timeout: Optional[float] = None,
) -> Callable:
    """Create a callable shell hook executor.

    Parameters
    ----------
    code:
        The shell command from the skill hook definition.
    skill_name:
        Name of the owning skill (for diagnostics).
    skill_dir:
        Absolute path to the directory containing the skill file.
    logger:
        Optional logger instance.
    timeout:
        Maximum execution time in seconds.  ``None`` means no limit.

    Returns
    -------
    A callable ``(HookContext) -> HookResult``.
    """
    return _create_shell_executor(code, skill_name, skill_dir, logger, timeout=timeout)


# ---------------------------------------------------------------------------
# Shell executor
# ---------------------------------------------------------------------------

def _create_shell_executor(
    command: str,
    skill_name: str,
    skill_dir: Optional[str],
    logger=None,
    timeout: Optional[float] = None,
) -> Callable:
    """Create a closure that executes a shell hook command."""
    resolved_logger = get_logger(logger, __name__)

    def shell_hook_wrapper(context: HookContext) -> HookResult:
        runtime_logger = resolved_logger
        # Use shared subprocess environment builder — filters sensitive vars
        # (API keys, tokens) and injects protective defaults, aligned with
        # the same sanitisation layer used by the shell tool.
        try:
            from src.tools.shell.subprocess_env import build_subprocess_env
            env = build_subprocess_env()
        except ImportError:
            env = os.environ.copy()
        execution_cwd = skill_dir if skill_dir and os.path.isdir(skill_dir) else os.getcwd()
        _prepend_hook_python_paths(env, skill_dir=skill_dir, execution_cwd=execution_cwd)

        from src.trace.task_context import (
            get_current_agent_name,
            get_current_runtime_agent_path,
            get_current_task_id,
        )

        # Runtime context takes precedence: get_current_agent_name() reflects the
        # agent that is *currently executing*, while tool_input.agent_name is an
        # explicit payload field (used by SubagentStart/SubagentStop hooks).
        # Previous order (tool_input first) caused PreToolUse events to be
        # attributed to the wrong agent when tool_input carried a stale name.
        _ctx_agent = get_current_agent_name()
        _ti_agent = (context.tool_input or {}).get("agent_name")
        _resolved = _ctx_agent or _ti_agent or "default"
        env["AGENT_NAME"] = _resolved
        # Hierarchical path for .runtime directory nesting (e.g. "parent/child").
        # Hook scripts use this to place their runtime files under the correct
        # nested directory.  Falls back to AGENT_NAME if not set.
        env["RUNTIME_AGENT_PATH"] = get_current_runtime_agent_path() or _resolved
        env["TASK_ID"] = (
            get_current_task_id()
            or (context.tool_input or {}).get("task_id")
            or ""
        )
        env["TOOL_NAME"] = context.tool_name or ""
        env["HOOK_EVENT"] = context.hook_event_name or ""
        # Propagate step_number so hook scripts can detect staleness.
        # Without this, get_step_number() in hook scripts always returns 0,
        # causing the grace-period check (step <= 3) to suppress all reminders.
        env["STEP_NUMBER"] = str(context.step_number) if context.step_number is not None else ""

        # Build the full hook context payload.
        hook_context_payload = {
            "session_id": context.session_id,
            "cwd": context.cwd,
            "hook_event_name": context.hook_event_name,
            "tool_name": context.tool_name,
            "tool_input": context.tool_input or {},
            "tool_response": context.tool_response,
            "step_number": context.step_number,
        }
        from src.extensions.self_learning.redaction import (
            sanitize_campaign_artifact_value,
        )

        sanitized_payload = sanitize_campaign_artifact_value(hook_context_payload)
        hook_context_payload = (
            sanitized_payload
            if isinstance(sanitized_payload, dict)
            else {"value": sanitized_payload}
        )
        full_json = json.dumps(
            hook_context_payload, ensure_ascii=False, default=str,
        )

        # Write full JSON to a temp file so hook scripts can read it
        # regardless of size.  The env-var is kept (truncated) for
        # backward compatibility with scripts that read it directly.
        # This mirrors the tempfile strategy used in
        # src/tools/shell/process.py (_run_persistent_via_tempfile).
        _MAX_ENV_BYTES = 65_536  # 64 KB safety margin for env-var payload
        context_tmp_path: Optional[str] = None
        try:
            ctx_tmp = tempfile.NamedTemporaryFile(
                mode="w", suffix=".json", prefix="hook_ctx_",
                delete=False, encoding="utf-8",
            )
            ctx_tmp.write(full_json)
            ctx_tmp.close()
            context_tmp_path = ctx_tmp.name
            env["HOOK_CONTEXT_JSON_FILE"] = context_tmp_path
        except Exception as tmp_err:
            if runtime_logger:
                runtime_logger.warning(
                    f"Failed to write hook context temp file: {tmp_err}; "
                    "falling back to env-var only"
                )

        if len(full_json.encode("utf-8")) <= _MAX_ENV_BYTES:
            env["HOOK_CONTEXT_JSON"] = full_json
        else:
            # Truncate large fields to fit within env-var limits.
            truncated_payload = dict(hook_context_payload)
            for key in ("tool_input", "tool_response"):
                val_str = json.dumps(
                    truncated_payload.get(key, ""),
                    ensure_ascii=False, default=str,
                )
                if len(val_str.encode("utf-8")) > _MAX_ENV_BYTES // 4:
                    truncated_payload[key] = (
                        str(truncated_payload.get(key, ""))[:2048]
                        + "... [TRUNCATED — read $HOOK_CONTEXT_JSON_FILE for full data]"
                    )
            env["HOOK_CONTEXT_JSON"] = json.dumps(
                truncated_payload, ensure_ascii=False, default=str,
            )

        try:
            if runtime_logger:
                runtime_logger.debug(f"Executing shell hook from skill {skill_name}")
            completed = subprocess.run(
                command,
                shell=True,
                cwd=execution_cwd,
                env=env,
                capture_output=True,
                text=True,
                timeout=timeout,
            )
        except subprocess.TimeoutExpired:
            if runtime_logger:
                runtime_logger.error(
                    f"Shell hook from skill {skill_name} timed out after {timeout}s"
                )
            return invalid_hook_contract(
                f"Shell hook timed out after {timeout}s",
                skill_name=skill_name,
                hook_event_name=context.hook_event_name,
                telemetry={"exception": "TimeoutExpired", "timeout": timeout},
            )
        except Exception as e:
            if runtime_logger:
                runtime_logger.error(f"Error executing shell hook in skill {skill_name}: {e}")
            return invalid_hook_contract(
                f"Shell hook execution failed: {e}",
                skill_name=skill_name,
                hook_event_name=context.hook_event_name,
                telemetry={"exception": type(e).__name__},
            )
        finally:
            # Clean up the temp file (mirrors process.py cleanup pattern).
            if context_tmp_path:
                try:
                    os.remove(context_tmp_path)
                except OSError:
                    pass

        stdout_text = completed.stdout.rstrip()
        stderr_text = completed.stderr.rstrip()
        telemetry: Dict[str, Any] = {"returncode": completed.returncode}
        if stderr_text:
            telemetry["stderr"] = stderr_text

        # Exit code protocol (aligned with upstream convention):
        #   0   = success
        #   2   = blocking error (hard block, always prevents continuation)
        #   1/3+ = non-blocking error (logged as warning, does not halt)
        is_blocking = completed.returncode == 2
        is_success = completed.returncode == 0
        is_non_blocking_error = (not is_success) and (not is_blocking)

        if not stdout_text:
            if is_success:
                return HookResult(success=True, decision="allow", telemetry=telemetry)

            if is_blocking:
                return HookResult(
                    success=False,
                    decision="block",
                    reason=stderr_text or "Shell hook exited with blocking error (exit code 2)",
                    telemetry={
                        **telemetry,
                        "skill_name": skill_name,
                        "hook_event_name": context.hook_event_name,
                        "exit_code_class": "blocking",
                    },
                )

            # Non-blocking error: log warning but allow continuation
            if runtime_logger:
                runtime_logger.warning(
                    "Shell hook from skill %s exited with code %d (non-blocking)",
                    skill_name, completed.returncode,
                )
            return HookResult(
                success=False,
                decision="allow",
                reason=stderr_text or f"Shell hook exited with code {completed.returncode} (non-blocking)",
                telemetry={
                    **telemetry,
                    "skill_name": skill_name,
                    "hook_event_name": context.hook_event_name,
                    "exit_code_class": "non_blocking_error",
                },
            )

        try:
            payload = json.loads(stdout_text)
        except (json.JSONDecodeError, TypeError, ValueError):
            return invalid_hook_contract(
                "Shell hook output must be structured JSON.",
                skill_name=skill_name,
                hook_event_name=context.hook_event_name,
                telemetry={**telemetry, "stdout": stdout_text},
            )

        if not isinstance(payload, dict):
            return invalid_hook_contract(
                "Shell hook output must be a JSON object.",
                skill_name=skill_name,
                hook_event_name=context.hook_event_name,
                telemetry={**telemetry, "stdout": stdout_text},
            )

        fallback_reason = None
        if not is_success:
            fallback_reason = stderr_text or f"Shell hook exited with code {completed.returncode}"

        telemetry["exit_code_class"] = (
            "success" if is_success
            else "blocking" if is_blocking
            else "non_blocking_error"
        )

        structured = build_structured_hook_result(
            payload,
            context,
            skill_name=skill_name,
            success=is_success,
            fallback_reason=fallback_reason,
            telemetry=telemetry,
        )
        if is_blocking:
            structured.decision = "block"
            structured.success = False
        elif is_non_blocking_error:
            # Non-blocking: preserve payload decision if explicit, else allow
            if structured.decision not in ("block", "modify"):
                structured.decision = "allow"
            structured.success = False
        return structured

    return shell_hook_wrapper
