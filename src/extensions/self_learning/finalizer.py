"""Single synchronous SessionEnd hook: commit facts, enqueue slow work, return."""

from __future__ import annotations

import uuid
from dataclasses import replace
from datetime import datetime
from typing import TYPE_CHECKING, Any

from src.lib.logging import get_logger

if TYPE_CHECKING:
    from src.lib.smolagents.hooks.types import HookContext, HookResult

from .application_scope import resolve_application_scope
from .event_schema import safe_run_id
from .learning_jobs import kick_learning_worker
from .ledger import SelfLearningLedger
from .paths import (
    application_learning_runs_dir,
    config_bool,
    learning_runs_dir,
)
from .redaction import redact_mapping, sanitize_text_fragment
from .session_recorder import event_from_hook_context, hook_context_failed

logger = get_logger(__name__)


def _finalizer_payload(
    context: HookContext, *, root_run_id: str, run_dir: str, application_id: str
) -> dict[str, Any]:
    tool_input = context.tool_input if isinstance(context.tool_input, dict) else {}
    tool_response = (
        context.tool_response if isinstance(context.tool_response, dict) else {}
    )
    return redact_mapping(
        {
            "root_run_id": root_run_id,
            "session_id": context.session_id,
            "application_id": application_id,
            "run_dir": run_dir,
            "fallback_task": tool_input.get("task_text") or tool_input.get("task") or "",
            "fallback_final_answer": tool_response.get("result") or "",
            "succeeded": not hook_context_failed(context),
        }
    )


def session_finalize_hook(context: HookContext) -> HookResult:
    """Atomically finish one root run and kick its durable background work."""
    from src.lib.smolagents.hooks.types import HookResult

    if not config_bool("enabled", True):
        return HookResult(success=True, decision="allow")
    if context.hook_event_name != "SessionEnd":
        return HookResult(success=True, decision="allow")

    root_run_id = safe_run_id(str(context.root_run_id or ""))
    if not root_run_id:
        return HookResult(
            success=False,
            decision="allow",
            outcome="non_blocking_error",
            reason="Self-learning SessionEnd skipped: missing_root_run_context",
        )

    try:
        app_scope = resolve_application_scope(context.agent_config or {})
        app_id = str(app_scope.application_id or "default")
        if app_id != "default":
            run_dir = application_learning_runs_dir(app_id) / root_run_id
        else:
            run_dir = learning_runs_dir() / root_run_id
        payload = _finalizer_payload(
            context,
            root_run_id=root_run_id,
            run_dir=str(run_dir),
            application_id=app_id,
        )
        event = event_from_hook_context(context)
        if event is None:
            raise ValueError("SessionEnd did not produce a canonical event")
        # A repeated SessionEnd for the same root must not append another
        # final event.  Job dedupe and outcome CAS use the same identity.
        event = replace(
            event,
            event_id=uuid.uuid5(
                uuid.NAMESPACE_URL, f"agentloom:session-end:{root_run_id}"
            ).hex,
            metadata={**event.metadata, "root_run_id": root_run_id},
        )
        ledger = SelfLearningLedger()
        result = ledger.finalize_session(
            event,
            root_run_id=root_run_id,
            succeeded=bool(payload.get("succeeded")),
            review_payload=payload,
            enqueue_review=config_bool("reviewer_enabled", True),
            retention_dedupe_key=datetime.now().astimezone().date().isoformat(),
        )
        # Failure to start a process is harmless: the committed pending outbox
        # is discovered by the next run. `retry-job` is reserved for dead rows.
        kick_learning_worker(ledger.db_path)
        return HookResult(
            success=True,
            decision="allow",
            telemetry={
                "self_learning_finalize": {
                    "root_run_id": root_run_id,
                    "jobs_enqueued": result.get("jobs_enqueued", []),
                    "outcome_recorded": result.get("outcome_recorded", False),
                }
            },
        )
    except Exception as exc:
        safe_error = sanitize_text_fragment(str(exc), max_chars=1000)
        logger.warning("Self-learning SessionEnd finalizer skipped: %s", safe_error)
        return HookResult(
            success=False,
            decision="allow",
            outcome="non_blocking_error",
            reason=f"Self-learning SessionEnd skipped: {safe_error}",
        )
