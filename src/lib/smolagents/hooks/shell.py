"""Bounded Shell Hook transport for standalone Hook declarations."""

from __future__ import annotations

import json
import os
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

from src.lib.runtime.process import CapturedProcessTimeout, run_captured_process
from src.tools.shell.subprocess_env import build_subprocess_env

from .config import ShellHookSpec
from .types import HookContext, HookResult

HOOK_STDIN_SCHEMA_VERSION = 1
HOOK_STDOUT_MAX_BYTES = 1024 * 1024
HOOK_STDERR_PREVIEW_MAX_BYTES = 16 * 1024
_RESULT_FIELDS = frozenset(
    {
        "decision",
        "modified_input",
        "agent_context",
        "user_message",
        "reason",
        "telemetry",
    }
)


class ShellHookExecutionError(RuntimeError):
    """Raised when Shell Hook transport or output violates its contract."""


def _prepend_active_python(env: dict[str, str]) -> None:
    executable_dir = str(Path(sys.executable).resolve().parent)
    current = [part for part in env.get("PATH", "").split(os.pathsep) if part]
    env["PATH"] = os.pathsep.join([executable_dir, *[p for p in current if p != executable_dir]])


def _payload(spec: ShellHookSpec, context: HookContext) -> dict[str, Any]:
    return {
        "schema_version": HOOK_STDIN_SCHEMA_VERSION,
        "hook_id": spec.hook_id,
        "hook_event_name": context.hook_event_name,
        "local_run_id": context.local_run_id,
        "root_run_id": context.root_run_id,
        "agent_name": context.agent_name,
        "runtime_agent_path": context.runtime_agent_path,
        "task_id": context.task_id,
        "sub_task_id": context.sub_task_id,
        "step_number": context.step_number,
        "project_root": str(spec.project_root),
        "cwd": context.cwd,
        "tool_name": context.tool_name,
        "tool_call_id": context.tool_call_id,
        "tool_input": context.tool_input,
        "tool_response": context.tool_response,
        "tool_inputs_schema": context.tool_inputs_schema,
        "agent_task_workspace": context.agent_task_workspace,
        "agent_insights_path": context.agent_insights_path,
        "agent_visualization_path": context.agent_visualization_path,
    }


def _unique_json_object(text: str) -> dict[str, Any]:
    def unique_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        value: dict[str, Any] = {}
        for key, item in pairs:
            if key in value:
                raise ShellHookExecutionError(f"Shell Hook output contains duplicate field {key!r}")
            value[key] = item
        return value

    try:
        parsed = json.loads(text, object_pairs_hook=unique_pairs)
    except ShellHookExecutionError:
        raise
    except (json.JSONDecodeError, TypeError, ValueError) as exc:
        raise ShellHookExecutionError(f"Shell Hook output must be one valid JSON object: {exc}") from exc
    if not isinstance(parsed, dict):
        raise ShellHookExecutionError("Shell Hook output must be a JSON object")
    return parsed


def _parse_result(
    text: str,
    *,
    spec: ShellHookSpec,
    transport_telemetry: dict[str, Any],
) -> HookResult:
    if not text.strip():
        raise ShellHookExecutionError("Shell Hook must emit one JSON object on stdout")
    payload = _unique_json_object(text)
    unsupported = sorted(str(key) for key in payload if key not in _RESULT_FIELDS)
    if unsupported:
        raise ShellHookExecutionError(f"Shell Hook output contains unsupported field(s): {', '.join(unsupported)}")

    expected_types: dict[str, type[Any]] = {
        "decision": str,
        "modified_input": dict,
        "agent_context": str,
        "user_message": str,
        "reason": str,
        "telemetry": dict,
    }
    errors = [
        f"{key} must be {expected.__name__}"
        for key, expected in expected_types.items()
        if key in payload and not isinstance(payload[key], expected)
    ]
    if errors:
        raise ShellHookExecutionError(f"Invalid Shell Hook output: {', '.join(errors)}")
    telemetry = dict(payload.get("telemetry", {}))
    telemetry["shell_hook"] = transport_telemetry
    try:
        result = HookResult(
            decision=payload.get("decision", "allow"),
            modified_input=payload.get("modified_input"),
            agent_context=payload.get("agent_context"),
            user_message=payload.get("user_message"),
            reason=payload.get("reason"),
            telemetry=telemetry,
        )
    except ValueError as exc:
        raise ShellHookExecutionError(str(exc)) from exc
    if result.decision == "modify" and result.modified_input is None:
        raise ShellHookExecutionError("Shell Hook decision=modify requires modified_input")
    if result.decision != "modify" and result.modified_input is not None:
        raise ShellHookExecutionError("Shell Hook modified_input requires decision=modify")
    return result


def create_shell_hook_executor(
    spec: ShellHookSpec,
) -> Callable[[HookContext], HookResult]:
    """Create the runtime callback for one validated Shell Hook spec."""

    def execute(context: HookContext) -> HookResult:
        payload_bytes = json.dumps(
            _payload(spec, context),
            ensure_ascii=False,
            separators=(",", ":"),
            default=str,
        ).encode("utf-8")
        env = build_subprocess_env()
        _prepend_active_python(env)
        try:
            captured = run_captured_process(
                spec.command,
                cwd=str(spec.cwd),
                env=env,
                stdin=payload_bytes,
                timeout=spec.timeout,
                stdout_limit=HOOK_STDOUT_MAX_BYTES,
                stderr_limit=HOOK_STDERR_PREVIEW_MAX_BYTES,
            )
        except CapturedProcessTimeout as exc:
            raise ShellHookExecutionError(f"Shell Hook {spec.hook_id!r} timed out after {spec.timeout:g}s") from exc
        except OSError as exc:
            raise ShellHookExecutionError(f"Shell Hook {spec.hook_id!r} failed to start: {exc}") from exc

        stderr = captured.stderr.rstrip()
        if captured.returncode != 0:
            suffix = f": {stderr}" if stderr else ""
            raise ShellHookExecutionError(f"Shell Hook {spec.hook_id!r} exited with code {captured.returncode}{suffix}")
        if captured.stdout_truncated:
            raise ShellHookExecutionError(f"Shell Hook {spec.hook_id!r} stdout exceeds {HOOK_STDOUT_MAX_BYTES} bytes")
        transport: dict[str, Any] = {
            "hook_id": spec.hook_id,
            "returncode": captured.returncode,
            "duration_seconds": captured.duration_seconds,
            "stdout_bytes": captured.stdout_bytes,
            "stderr_bytes": captured.stderr_bytes,
        }
        if stderr:
            transport["stderr"] = stderr
        if captured.stderr_truncated:
            transport["stderr_truncated"] = True
        return _parse_result(
            captured.stdout,
            spec=spec,
            transport_telemetry=transport,
        )

    return execute


__all__ = [
    "HOOK_STDIN_SCHEMA_VERSION",
    "HOOK_STDOUT_MAX_BYTES",
    "ShellHookExecutionError",
    "create_shell_hook_executor",
]
