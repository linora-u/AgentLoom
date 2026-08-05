"""Runtime recorder for canonical self-learning events."""

from __future__ import annotations

import uuid
from pathlib import Path
from threading import RLock
from typing import TYPE_CHECKING, Any

from src.lib.logging import get_logger
from src.lib.trusted_memory_evidence import (
    TRUSTED_MEMORY_EVIDENCE_RESPONSE_KEY,
    TrustedMemoryEvidenceEnvelope,
)

if TYPE_CHECKING:
    from src.lib.smolagents.hooks.types import HookContext, HookResult

from .application_scope import resolve_application_scope
from .event_schema import CanonicalSessionEvent, compact_content, now_iso, safe_run_id
from .paths import self_learning_enabled, session_events_dir
from .persistence.ledger import SelfLearningLedger
from .redaction import redact_mapping, sanitize_text_fragment

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


def hook_context_failed(context: HookContext) -> bool:
    """Return whether a lifecycle response explicitly carries an error.

    Exception messages are allowed to be empty (``KeyboardInterrupt()`` and
    ``RuntimeError()`` both commonly are), so truthiness is not an outcome
    signal. Presence of the error contract is.
    """
    response = context.tool_response
    if isinstance(response, dict) and ("error" in response or "error_type" in response):
        return True
    tool_input = context.tool_input
    return bool(
        context.hook_event_name == "SessionEnd"
        and isinstance(tool_input, dict)
        and ("error" in tool_input or "error_type" in tool_input)
    )


def _status(context: HookContext, event_type: str) -> str:
    if event_type in {"tool_error", "task_failed"}:
        return "failed"
    if isinstance(context.tool_input, dict):
        success = context.tool_input.get("success")
        if success is False:
            return "failed"
        if success is True:
            return "completed"
    if hook_context_failed(context):
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
            "tool_call_id": context.tool_call_id or "",
            "sub_task_id": context.sub_task_id or _payload_value(context, "sub_task_id"),
        }
    )


def _public_tool_response(context: HookContext) -> dict[str, Any] | None:
    if not isinstance(context.tool_response, dict):
        return context.tool_response
    response = dict(context.tool_response)
    response.pop(TRUSTED_MEMORY_EVIDENCE_RESPONSE_KEY, None)
    return response


def _trusted_runtime_evidence(
    context: HookContext,
) -> tuple[dict[str, str], ...]:
    if context.hook_event_name != "PostToolUse":
        return ()
    response = context.tool_response
    if not isinstance(response, dict):
        return ()
    envelope = response.get(TRUSTED_MEMORY_EVIDENCE_RESPONSE_KEY)
    if not isinstance(envelope, TrustedMemoryEvidenceEnvelope):
        return ()
    return tuple(dict(entry) for entry in envelope if isinstance(entry, dict))


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
    public_response = _public_tool_response(context)
    if public_response is not None:
        payload["tool_response"] = public_response
        if isinstance(public_response, dict):
            if "result" in public_response:
                payload["result"] = public_response["result"]
            if "error" in public_response:
                payload["error"] = public_response["error"]
    return compact_content(payload)


def event_from_hook_context(context: HookContext) -> CanonicalSessionEvent | None:
    event_type = _HOOK_EVENT_TYPES.get(context.hook_event_name)
    if event_type is None:
        return None
    if context.hook_event_name == "SessionEnd" and hook_context_failed(context):
        event_type = "run_failed"
    if (
        context.hook_event_name == "SubagentStop"
        and isinstance(context.tool_input, dict)
        and context.tool_input.get("success") is False
    ):
        event_type = "subagent_failed"

    run_id = safe_run_id(context.local_run_id)
    root_run_id = safe_run_id(context.root_run_id or "")
    if not run_id or not root_run_id:
        return None
    task_id = _task_id(context)
    metadata = _metadata(context)
    input_data = context.tool_input if isinstance(context.tool_input, dict) else {}
    public_response = _public_tool_response(context)
    output_data = public_response if isinstance(public_response, dict) else {}
    event = CanonicalSessionEvent(
        event_id=uuid.uuid4().hex,
        run_id=run_id,
        root_run_id=root_run_id,
        task_id=task_id,
        parent_task_id=context.task_id or "",
        tool_call_id=context.tool_call_id or "",
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

    def append(
        self,
        event: CanonicalSessionEvent,
        *,
        trusted_evidence: tuple[dict[str, str], ...] = (),
    ) -> dict[str, Any]:
        with _LOCK:
            indexed = SelfLearningLedger().append_runtime_event(
                event,
                trusted_evidence=trusted_evidence,
            )
        return indexed

    def record_hook(self, context: HookContext) -> HookResult:
        from src.lib.smolagents.hooks.types import HookResult

        agent_config = context.agent_config if isinstance(context.agent_config, dict) and context.agent_config else None
        if not self_learning_enabled(agent_config):
            return HookResult(decision="allow")
        try:
            event = event_from_hook_context(context)
            if event is not None:
                self.append(
                    event,
                    trusted_evidence=_trusted_runtime_evidence(context),
                )
        except Exception as exc:
            safe_error = sanitize_text_fragment(str(exc), max_chars=1000)
            logger.warning(
                "Self-learning session recorder skipped event: %s",
                safe_error,
            )
            raise RuntimeError(f"Self-learning recorder skipped: {safe_error}") from exc
        return HookResult(decision="allow")


def session_recorder_hook(context: HookContext) -> HookResult:
    return SessionRecorder().record_hook(context)
