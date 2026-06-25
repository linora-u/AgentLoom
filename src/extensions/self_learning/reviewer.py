"""Built-in proposal-only learning reviewer hooks."""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any

from src.lib.logging import get_logger
from src.lib.smolagents.hooks.types import HookContext, HookResult

from .application_scope import resolve_application_scope
from .ledger import SelfLearningLedger
from .paths import application_learning_runs_dir, config_bool, learning_runs_dir
from .redaction import redact_mapping, redact_text

logger = get_logger(__name__)


def _safe_run_id(context: HookContext) -> str:
    task_id = ""
    if isinstance(context.tool_input, dict):
        task_id = str(context.tool_input.get("task_id") or "")
    raw = task_id or context.session_id or "unknown"
    return "".join(ch if ch.isalnum() or ch in "._-" else "_" for ch in raw)[:120]


def _review_payload(context: HookContext) -> dict[str, Any]:
    app_scope = resolve_application_scope(context.agent_config or {})
    return {
        "event": context.hook_event_name,
        "tool_name": context.tool_name,
        "session_id": context.session_id,
        "application_id": app_scope.application_id,
        "application_name": app_scope.application_name,
        "application_path": app_scope.application_path,
        "workflow_path": app_scope.workflow_path,
        "cwd": context.cwd,
        "step_number": context.step_number,
        "tool_input": redact_mapping(context.tool_input or {}),
        "tool_response": redact_mapping(context.tool_response or {}),
        "created_at": datetime.now().astimezone().isoformat(),
        "proposal_only": True,
    }


def _append(path: Path, text: str) -> None:
    existing = path.read_text(encoding="utf-8") if path.exists() else ""
    sep = "\n\n" if existing.strip() else ""
    path.write_text(existing.rstrip() + sep + text.rstrip() + "\n", encoding="utf-8")


def learning_review_hook(context: HookContext) -> HookResult:
    """Write failure-safe learning review artifacts for selected lifecycle events."""
    if not config_bool("reviewer_enabled", True):
        return HookResult(success=True, decision="allow")

    try:
        run_id = _safe_run_id(context)
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
                source_run_id=context.session_id or run_id,
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
            _append(
                run_dir / "memory_proposals.md",
                "## Candidate Memory\n\n- Review this run manually for reusable project/user facts. No memory was applied automatically.",
            )
            _append(
                run_dir / "skill_proposals.md",
                "## Candidate Skill\n\n- Review this run manually for repeated workflows. No active skill was changed automatically.",
            )
        elif event_name == "SessionEnd":
            _append(
                run_dir / "session_summary.md",
                "## Session End\n\n- Session lifecycle completed. Reviewer stayed proposal-only.",
            )
    except Exception as exc:
        logger.warning("Self-learning reviewer skipped after error: %s", exc)
        return HookResult(
            success=False,
            decision="allow",
            outcome="non_blocking_error",
            reason=f"Self-learning reviewer skipped: {exc}",
        )
    return HookResult(success=True, decision="allow")
