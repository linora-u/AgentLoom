"""Runtime recorder for canonical self-learning events."""

from __future__ import annotations

import uuid
from pathlib import Path
from threading import RLock
from typing import Any

from src.lib.logging import get_logger
from src.lib.smolagents.hooks.types import HookContext, HookResult

from .application_scope import resolve_application_scope
from .event_schema import CanonicalSessionEvent, compact_content, now_iso, safe_run_id
from .paths import config_bool, session_events_dir
from .redaction import redact_mapping
from .session_index import SessionIndex

logger = get_logger(__name__)

_LOCK = RLock()

_HOOK_EVENT_TYPES = {
    "SessionStart": "run_started",
    "SessionEnd": "run_completed",
    "TaskCreated": "task_created",
    "TaskCompleted": "task_completed",
    "StopFailure": "task_failed",
    "SubagentStart": "subagent_started",
    "SubagentStop": "subagent_completed",
    "PreToolUse": "tool_call",
    "PostToolUse": "tool_result",
    "PostToolUseFailure": "tool_error",
}


def event_file_for_run(run_id: str) -> Path:
    safe_name = safe_run_id(run_id)
    return session_events_dir() / f"{safe_name}.jsonl"


def _payload_value(context: HookContext, name: str) -> str:
    if isinstance(context.tool_input, dict):
        value = context.tool_input.get(name)
        if value is not None:
            return str(value)
    return ""


def _agent_name(context: HookContext) -> str:
    return _payload_value(context, "agent_name") or context.agent_name or ""


def _task_id(context: HookContext) -> str:
    return _payload_value(context, "task_id") or context.sub_task_id or context.task_id or ""


def _worker_name(context: HookContext) -> str:
    if context.hook_event_name in {"SubagentStart", "SubagentStop"}:
        return _payload_value(context, "agent_name") or context.tool_name
    return _payload_value(context, "worker_name")


def _status(context: HookContext, event_type: str) -> str:
    if event_type in {"tool_error", "task_failed"}:
        return "failed"
    if isinstance(context.tool_input, dict):
        success = context.tool_input.get("success")
        if success is False:
            return "failed"
        if success is True:
            return "completed"
    if isinstance(context.tool_response, dict) and context.tool_response.get("error"):
        return "failed"
    if event_type in {"run_completed", "task_completed", "subagent_completed", "tool_result"}:
        return "completed"
    return ""


def _metadata(context: HookContext) -> dict[str, Any]:
    config = context.agent_config or {}
    app_scope = resolve_application_scope(config)
    return redact_mapping(
        {
            "cwd": context.cwd,
            "app": app_scope.application_id or config.get("app") or config.get("name") or _agent_name(context),
            "application_id": app_scope.application_id,
            "application_name": app_scope.application_name,
            "application_path": app_scope.application_path,
            "workflow_path": app_scope.workflow_path,
            "yaml_path": app_scope.workflow_path or config.get("yaml_path") or config.get("_yaml_path") or "",
            "run_dir": config.get("run_dir") or "",
            "hook_event": context.hook_event_name,
            "sub_task_id": context.sub_task_id or _payload_value(context, "sub_task_id"),
        }
    )


def _content(context: HookContext, event_type: str) -> str:
    payload: dict[str, Any] = {
        "event_type": event_type,
        "hook_event": context.hook_event_name,
        "tool_name": context.tool_name,
        "task_id": _task_id(context),
        "agent_name": _agent_name(context),
        "worker_name": _worker_name(context),
        "tool_input": context.tool_input or {},
    }
    if isinstance(context.tool_input, dict):
        for key in ("task_text", "task", "final_answer", "error", "error_type", "success"):
            if key in context.tool_input:
                payload[key] = context.tool_input[key]
    if context.tool_response is not None:
        payload["tool_response"] = context.tool_response
        if isinstance(context.tool_response, dict):
            if "result" in context.tool_response:
                payload["result"] = context.tool_response["result"]
            if "error" in context.tool_response:
                payload["error"] = context.tool_response["error"]
    return compact_content(payload)


def event_from_hook_context(context: HookContext) -> CanonicalSessionEvent | None:
    event_type = _HOOK_EVENT_TYPES.get(context.hook_event_name)
    if event_type is None:
        return None
    if context.hook_event_name == "SessionEnd" and isinstance(context.tool_response, dict) and context.tool_response.get("error"):
        event_type = "run_failed"
    if context.hook_event_name == "SubagentStop" and isinstance(context.tool_input, dict) and context.tool_input.get("success") is False:
        event_type = "subagent_failed"

    run_id = safe_run_id(context.session_id)
    task_id = _task_id(context)
    metadata = _metadata(context)
    input_data = context.tool_input if isinstance(context.tool_input, dict) else {}
    output_data = context.tool_response if isinstance(context.tool_response, dict) else {}
    event = CanonicalSessionEvent(
        event_id=uuid.uuid4().hex,
        run_id=run_id,
        task_id=task_id,
        parent_task_id=context.task_id or "",
        application_id=str(metadata.get("application_id") or ""),
        application_name=str(metadata.get("application_name") or ""),
        application_path=str(metadata.get("application_path") or ""),
        workflow_path=str(metadata.get("workflow_path") or metadata.get("yaml_path") or ""),
        agent_name=_agent_name(context),
        worker_name=_worker_name(context),
        event_type=event_type,
        phase="tool" if event_type.startswith("tool_") else "lifecycle",
        source="hook",
        role="tool" if event_type.startswith("tool_") else "lifecycle",
        tool_name=context.tool_name if event_type.startswith("tool_") else "",
        content=_content(context, event_type),
        content_text=_content(context, event_type),
        status=_status(context, event_type),
        step_number=context.step_number,
        created_at=now_iso(),
        input_data=input_data,
        output_data=output_data,
        metadata=metadata,
    )
    return event


class SessionRecorder:
    """Append canonical events and keep the SQLite index warm."""

    def append(self, event: CanonicalSessionEvent) -> dict[str, Any]:
        with _LOCK:
            indexed = SessionIndex().record_event(event)
        return indexed

    def record_hook(self, context: HookContext) -> HookResult:
        if not config_bool("enabled", True):
            return HookResult(success=True, decision="allow")
        try:
            event = event_from_hook_context(context)
            if event is not None:
                self.append(event)
        except Exception as exc:
            logger.warning("Self-learning session recorder skipped event: %s", exc)
            return HookResult(
                success=False,
                decision="allow",
                outcome="non_blocking_error",
                reason=f"Self-learning recorder skipped: {exc}",
            )
        return HookResult(success=True, decision="allow")


def session_recorder_hook(context: HookContext) -> HookResult:
    return SessionRecorder().record_hook(context)
