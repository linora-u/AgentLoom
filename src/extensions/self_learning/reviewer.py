"""Built-in proposal-only learning reviewer hooks."""

from __future__ import annotations

import json
import os
import tempfile
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from src.lib.logging import get_logger

if TYPE_CHECKING:
    from src.lib.smolagents.hooks.types import HookContext, HookResult

from .application_scope import resolve_application_scope
from .event_schema import safe_run_id
from .ledger import SelfLearningLedger
from .paths import application_learning_runs_dir, config_bool, learning_runs_dir, memory_config
from .redaction import redact_text, sanitize_text_fragment, sanitize_value_fragments

logger = get_logger(__name__)


def _root_run_id(context: HookContext) -> str:
    """Return the sanitized explicit root identity; never infer from task data."""
    raw = str(context.root_run_id or "").strip()
    return safe_run_id(raw) if raw else ""


def _review_payload(context: HookContext) -> dict[str, Any]:
    app_scope = resolve_application_scope(context.agent_config or {})
    payload = sanitize_value_fragments(
        {
            "event": context.hook_event_name,
            "tool_name": context.tool_name,
            "session_id": context.session_id,
            "application_id": app_scope.application_id,
            "application_name": app_scope.application_name,
            "application_path": app_scope.application_path,
            "workflow_path": app_scope.workflow_path,
            "cwd": context.cwd,
            "step_number": context.step_number,
            "tool_input": context.tool_input or {},
            "tool_response": context.tool_response or {},
            "created_at": datetime.now().astimezone().isoformat(),
            "proposal_only": True,
        }
    )
    return payload if isinstance(payload, dict) else {"value": payload}


def _atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=str(path.parent), prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
        os.replace(tmp_name, path)
    except BaseException:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


def _append(path: Path, text: str) -> None:
    existing = path.read_text(encoding="utf-8") if path.exists() else ""
    sep = "\n\n" if existing.strip() else ""
    _atomic_write(path, existing.rstrip() + sep + text.rstrip() + "\n")


def _planned_append(path: Path, *fragments: str) -> str:
    """Return the exact safe bytes for an idempotent aggregate artifact."""
    existing = path.read_text(encoding="utf-8") if path.exists() else ""
    parts: list[str] = []
    if existing.strip():
        parts.append(sanitize_text_fragment(existing).rstrip())
    parts.extend(
        safe
        for fragment in fragments
        if fragment and (safe := sanitize_text_fragment(fragment).rstrip())
    )
    return "\n\n".join(parts) + "\n"


def _write_job_artifacts(
    run_dir: Path,
    job_id: int,
    *,
    result: dict[str, Any],
    markdown: str,
) -> None:
    """Atomically overwrite the audit artifacts owned by one durable job."""
    artifact_dir = run_dir / "learning_jobs"
    safe_result = sanitize_value_fragments(result)
    _atomic_write(
        artifact_dir / f"{job_id}.json",
        json.dumps(safe_result, ensure_ascii=False, indent=2, sort_keys=True, default=str) + "\n",
    )
    _atomic_write(artifact_dir / f"{job_id}.md", markdown.rstrip() + "\n")


class RetryableDistillError(RuntimeError):
    """The model path failed before its bounded outbox attempts were spent."""


def _auto_apply_proposals(
    run_id: str,
    application_id: str,
    run_dir: Path,
    *,
    write_artifacts: bool = True,
    record_audit: bool = True,
    job_id: int | None = None,
    lease_token: str = "",
    semantic_plan: dict[str, Any] | None = None,
    guard_now: str | None = None,
    db_path: str | Path | None = None,
) -> dict[str, Any]:
    """Auto-apply gated pending proposals and leave a full audit trail."""
    if str(memory_config().get("auto_apply") or "off").strip().lower() != "safe":
        return {"applied": 0, "skipped": 0, "mode": "off"}
    from .memory_store import MemoryStore

    store = MemoryStore(db_path)
    if job_id is not None and semantic_plan is not None:
        result = store.auto_apply_pending_for_job(
            application_id=application_id,
            run_id=run_id,
            job_id=job_id,
            lease_token=lease_token,
            semantic_plan=semantic_plan,
            now=guard_now,
        )
    else:
        result = store.auto_apply_pending(
            application_id=application_id,
            run_id=run_id,
        )
    if record_audit:
        try:
            SelfLearningLedger(db_path).record_review(
                source_run_id=run_id,
                hook_event="SessionEnd",
                application_id=application_id,
                output=result,
                status="auto_apply",
            )
        except Exception:
            pass
    applied = result.get("applied") or []
    skipped = result.get("skipped") or []
    artifact_text = ""
    if applied or skipped:
        lines = ["## Auto-Applied Proposals", ""]
        for item in applied:
            lines.append(f"- applied [{item['id']}] {item.get('content_preview', '')}")
        for item in skipped:
            lines.append(f"- skipped [{item['id']}]: {item.get('reason', '')}")
        artifact_text = "\n".join(lines)
        if write_artifacts:
            _append(run_dir / "memory_proposals.md", artifact_text)
    return {
        "applied": len(applied),
        "skipped": len(skipped),
        "artifact_text": artifact_text,
    }


def _auto_retention(
    run_dir: Path,
    *,
    write_artifacts: bool = True,
    job_id: int | None = None,
    lease_token: str = "",
    guard_now: str | None = None,
    db_path: str | Path | None = None,
) -> dict[str, Any]:
    """Idempotent retention work owned by the date-deduplicated outbox job."""
    from .memory_store import MemoryStore
    from .paths import config_int

    store = MemoryStore(db_path)
    try:
        session_ttl_days = int(memory_config().get("session_ttl_days", 14))
    except (TypeError, ValueError):
        session_ttl_days = 14
    retention_days = config_int("events_retention_days", 90)
    if job_id is not None:
        summary = store.apply_retention_job(
            job_id=job_id,
            lease_token=lease_token,
            session_ttl_days=session_ttl_days,
            events_retention_days=retention_days,
            now=guard_now,
        )
    else:
        # Explicit/manual maintenance keeps the public synchronous seam. The
        # durable outbox path above is the only path used by SessionEnd jobs.
        ledger = SelfLearningLedger(db_path)
        summary = {"memory": store.prune(session_ttl_days=session_ttl_days)}
        if retention_days > 0:
            summary["events"] = ledger.prune_events(retention_days=retention_days)
    artifact_text = (
        "## Auto Retention\n\n```json\n" + json.dumps(summary, ensure_ascii=False, indent=2, default=str) + "\n```"
    )
    if write_artifacts:
        _append(run_dir / "session_summary.md", artifact_text)
    return {**summary, "artifact_text": artifact_text}


def process_session_review_job(job: dict[str, Any], *, queue: Any) -> Any:
    """Execute the slow SessionEnd review after the hook transaction commits."""
    from .learning_jobs import JobExecution, build_artifact_delivery

    payload = job.get("payload") if isinstance(job.get("payload"), dict) else {}
    run_id = str(payload.get("root_run_id") or job.get("root_run_id") or "")
    application_id = str(payload.get("application_id") or "default")
    db_path = Path(queue.db_path)
    default_run_dir = (
        application_learning_runs_dir(application_id, root=db_path.parent) / run_id
        if application_id != "default"
        else learning_runs_dir(db_path.parent) / run_id
    )
    run_dir = Path(str(payload.get("run_dir") or default_run_dir))
    config = memory_config()
    distill_enabled = bool(config.get("distill_enabled", True))
    model_type = str(config.get("distill_model") or "").strip()
    guard_now = job.get("_clock_now")

    def require_live_claim() -> None:
        queue.require_active_claim(
            int(job["id"]),
            str(job["lease_token"]),
            now=guard_now,
        )

    from .distiller import (
        build_semantic_plan,
        distill_with_model,
        load_semantic_plan,
        prepare_run_digest,
    )

    prepared_digest = payload.get("prepared_digest")
    if distill_enabled and "prepared_digest" not in payload:
        prepared_digest = prepare_run_digest(
            run_id,
            application_id,
            fallback_task=str(payload.get("fallback_task") or ""),
            fallback_final_answer=str(payload.get("fallback_final_answer") or ""),
            db_path=db_path,
        )
        payload = queue.persist_payload_fields(
            int(job["id"]),
            str(job["lease_token"]),
            {"prepared_digest": prepared_digest},
            now=guard_now,
        )
        prepared_digest = payload.get("prepared_digest")

    semantic_plan = payload.get("semantic_plan")
    if semantic_plan is not None:
        semantic_plan = load_semantic_plan(
            semantic_plan,
            prepared_digest=(prepared_digest if isinstance(prepared_digest, dict) else None),
            application_id=application_id,
        )
        if semantic_plan is None:
            raise ValueError("persisted semantic plan failed integrity validation")
    else:
        if not distill_enabled:
            mode = "disabled"
            proposals: list[dict[str, Any]] = []
        elif prepared_digest is None:
            mode = "no_signal"
            proposals = []
        elif model_type:
            model_proposals = distill_with_model(
                run_id,
                application_id=application_id,
                model_type=model_type,
                fallback_task=str(payload.get("fallback_task") or ""),
                fallback_final_answer=str(payload.get("fallback_final_answer") or ""),
                prepared_digest=prepared_digest,
                db_path=db_path,
            )
            if model_proposals is None and int(job.get("attempts") or 0) < 3:
                raise RetryableDistillError("distillation model failed")
            if model_proposals is None:
                mode = "deterministic_fallback"
                proposals = []
            else:
                mode = "llm"
                proposals = model_proposals
        else:
            mode = "deterministic"
            proposals = []
        semantic_plan = build_semantic_plan(
            prepared_digest=(prepared_digest if isinstance(prepared_digest, dict) else None),
            application_id=application_id,
            mode=mode,
            proposals=proposals,
        )
        payload = queue.persist_payload_fields(
            int(job["id"]),
            str(job["lease_token"]),
            {"semantic_plan": semantic_plan},
            now=guard_now,
        )
        semantic_plan = load_semantic_plan(
            payload.get("semantic_plan"),
            prepared_digest=(prepared_digest if isinstance(prepared_digest, dict) else None),
            application_id=application_id,
        )
        if semantic_plan is None:
            raise ValueError("frozen semantic plan failed readback validation")

    from .memory_store import MemoryStore

    distill_result = MemoryStore(db_path).apply_job_semantic_plan(
        job_id=int(job["id"]),
        lease_token=str(job["lease_token"]),
        root_run_id=run_id,
        application_id=application_id,
        semantic_plan=semantic_plan,
        prepared_digest=(prepared_digest if isinstance(prepared_digest, dict) else None),
        now=guard_now,
    )
    if distill_result.get("distilled_by") == "deterministic_fallback":
        distill_result["distilled_by"] = "deterministic(fallback)"
    proposal_effects = list(distill_result.pop("proposals", []) or [])
    proposal_lines = [
        f"- [{effect.get('id')}] ({effect.get('scope')}) {effect.get('content_preview', '')}"
        for effect in proposal_effects
        if effect.get("ok") and not effect.get("duplicate")
    ]
    if distill_result.get("archived"):
        proposal_lines.append(
            f"- Archived {int(distill_result['archived'])} pinned session note(s) consumed by distillation"
        )
    distill_artifact = ""
    if proposal_lines:
        distill_artifact = (
            "## Distilled Memory Proposals\n\n"
            f"- distilled_by: {distill_result.get('distilled_by')}\n"
            + "\n".join(proposal_lines)
            + "\n\nReview with `loom memory list` and promote with `loom memory apply <id>` "
            "or discard with `loom memory reject <id>`."
        )

    apply_result = _auto_apply_proposals(
        run_id,
        application_id,
        run_dir,
        write_artifacts=False,
        record_audit=False,
        job_id=int(job["id"]),
        lease_token=str(job["lease_token"]),
        semantic_plan=semantic_plan,
        guard_now=guard_now,
        db_path=db_path,
    )
    apply_artifact = str(apply_result.pop("artifact_text", "") or "")
    result = {"distill": distill_result, "auto_apply": apply_result}
    require_live_claim()
    queue.record_review_fenced(
        int(job["id"]),
        str(job["lease_token"]),
        source_run_id=run_id,
        hook_event="SessionEnd",
        application_id=application_id,
        output=result,
        status="session_review",
        now=guard_now,
    )

    summary_line = (
        f"Self-learning session summary [run {run_id}]: "
        f"distilled {distill_result.get('distilled', 0)} "
        f"({distill_result.get('distilled_by', '?')}), "
        f"auto-applied {apply_result.get('applied', 0)}, "
        f"skipped {apply_result.get('skipped', 0)}"
    )

    job_markdown = (
        f"# Learning Job {int(job['id'])}\n\n"
        f"- kind: session_review\n- root_run_id: {run_id}\n- {summary_line}\n"
    )
    if distill_artifact:
        job_markdown += "\n" + sanitize_text_fragment(distill_artifact).rstrip() + "\n"
    if apply_artifact:
        job_markdown += "\n" + sanitize_text_fragment(apply_artifact).rstrip() + "\n"
    safe_result = sanitize_value_fragments(result)
    files = {
        f"learning_jobs/{int(job['id'])}.json": json.dumps(
            safe_result,
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
            default=str,
        )
        + "\n",
        f"learning_jobs/{int(job['id'])}.md": job_markdown.rstrip() + "\n",
        "session_summary.md": _planned_append(
            run_dir / "session_summary.md",
            f"## Session End\n\n- {summary_line}\n"
            "- Memory changes are audited in memory_proposals.md.",
        ),
    }
    if distill_artifact or apply_artifact:
        files["memory_proposals.md"] = _planned_append(
            run_dir / "memory_proposals.md",
            distill_artifact,
            apply_artifact,
        )
    delivery = build_artifact_delivery(
        job_id=int(job["id"]),
        kind="session_review",
        root_dir=run_dir,
        files=files,
    )
    logger.info(summary_line)
    return JobExecution(result=result, artifact_delivery=delivery)


def process_retention_job(job: dict[str, Any], *, queue: Any) -> Any:
    """Run daily retention once and freeze its artifacts before delivery."""
    from .learning_jobs import JobExecution, build_artifact_delivery

    payload = job.get("payload") if isinstance(job.get("payload"), dict) else {}
    db_path = Path(queue.db_path)
    run_dir = Path(str(payload.get("run_dir") or learning_runs_dir(db_path.parent)))
    summary = _auto_retention(
        run_dir,
        write_artifacts=False,
        job_id=int(job["id"]),
        lease_token=str(job["lease_token"]),
        guard_now=job.get("_clock_now"),
        db_path=db_path,
    )
    artifact_text = str(summary.pop("artifact_text", "") or "")

    retention_body = sanitize_text_fragment(
        artifact_text
        or "```json\n"
        + json.dumps(summary, ensure_ascii=False, indent=2, default=str)
        + "\n```"
    )
    retention_markdown = (
        f"# Learning Job {int(job['id'])}\n\n- kind: retention\n\n"
        + retention_body.rstrip()
        + "\n"
    )
    safe_summary = sanitize_value_fragments(summary)
    files = {
        f"learning_jobs/{int(job['id'])}.json": json.dumps(
            safe_summary,
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
            default=str,
        )
        + "\n",
        f"learning_jobs/{int(job['id'])}.md": retention_markdown,
    }
    if artifact_text:
        files["session_summary.md"] = _planned_append(
            run_dir / "session_summary.md",
            artifact_text,
        )
    delivery = build_artifact_delivery(
        job_id=int(job["id"]),
        kind="retention",
        root_dir=run_dir,
        files=files,
    )
    return JobExecution(result=summary, artifact_delivery=delivery)


def learning_review_hook(context: HookContext) -> HookResult:
    """Write failure-safe learning review artifacts for selected lifecycle events."""
    from src.lib.smolagents.hooks.types import HookResult

    if context.hook_event_name == "SessionEnd":
        # Backward-compatible callable seam; builtin registration points
        # directly at this same single finalizer.
        from .finalizer import session_finalize_hook

        return session_finalize_hook(context)
    # `enabled` is the subsystem master switch: with it off, no runtime entry
    # point may mutate memory — including distillation, auto-apply, and
    # retention below. `reviewer_enabled` narrows just this hook.
    if not (config_bool("enabled", True) and config_bool("reviewer_enabled", True)):
        return HookResult(success=True, decision="allow")

    run_id = _root_run_id(context)
    if not run_id:
        return HookResult(
            success=False,
            decision="allow",
            outcome="non_blocking_error",
            reason="Self-learning reviewer skipped: missing_root_run_context",
        )

    try:
        event_name = context.hook_event_name
        event_key = "".join(ch if ch.isalnum() else "_" for ch in event_name.lower())
        payload = _review_payload(context)
        app_id = str(payload.get("application_id") or "default")
        if app_id != "default":
            run_dir = application_learning_runs_dir(app_id) / run_id
        else:
            run_dir = learning_runs_dir() / run_id
        run_dir.mkdir(parents=True, exist_ok=True)
        marker = run_dir / f"{event_key}.json"
        if marker.exists():
            return HookResult(success=True, decision="allow")
        marker.write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
        try:
            SelfLearningLedger().record_review(
                source_run_id=run_id,
                hook_event=event_name,
                application_id=app_id,
                output=payload,
                status="proposal",
            )
        except Exception:
            pass

        md = [
            f"# Learning Review: {event_name}",
            "",
            f"- task_id: {payload['tool_input'].get('task_id', '')}",
            f"- agent: {payload['tool_input'].get('agent_name', '')}",
            f"- application_id: {app_id}",
            "- proposal_only: true",
            "",
            "## Evidence",
            "",
            "```json",
            json.dumps(payload, ensure_ascii=False, indent=2, default=str),
            "```",
        ]
        (run_dir / f"{event_key}.md").write_text(redact_text("\n".join(md)), encoding="utf-8")

        if event_name in {"StopFailure", "PostToolUseFailure"}:
            _append(
                run_dir / "failure_patterns.md",
                f"## {event_name}\n\n```json\n{json.dumps(payload, ensure_ascii=False, indent=2, default=str)}\n```",
            )
        elif event_name == "TaskCompleted":
            task_text = redact_text(
                str(payload["tool_input"].get("task_text") or payload["tool_input"].get("task") or "")
            )[:400]
            result_text = redact_text(str(payload["tool_response"].get("result") or ""))[:400]
            _append(
                run_dir / "memory_proposals.md",
                "## Candidate Memory\n\n"
                + (f"- task: {task_text}\n" if task_text else "")
                + (f"- result: {result_text}\n" if result_text else "")
                + "- Review this run for reusable project/application facts. No memory was applied automatically.",
            )
            _append(
                run_dir / "skill_proposals.md",
                "## Candidate Skill\n\n- Review this run manually for repeated workflows. No active skill was changed automatically.",
            )
    except Exception as exc:
        safe_error = sanitize_text_fragment(str(exc), max_chars=1000)
        logger.warning("Self-learning reviewer skipped after error: %s", safe_error)
        return HookResult(
            success=False,
            decision="allow",
            outcome="non_blocking_error",
            reason=f"Self-learning reviewer skipped: {safe_error}",
        )
    return HookResult(success=True, decision="allow")
