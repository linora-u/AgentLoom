"""Session search tools."""

from __future__ import annotations

import json

from src.extensions.self_learning.application_scope import current_application_scope
from src.extensions.self_learning.event_schema import safe_run_id
from src.extensions.self_learning.persistence.ledger import SelfLearningLedger

_SEARCH_CONTENT_PREVIEW_CHARS = 80
_SCROLL_CONTENT_PREVIEW_CHARS = 240
_TOOL_MAX_RESULTS = 5
_TOOL_RESULT_FIELDS = (
    "id",
    "run_id",
    "task_id",
    "agent_name",
    "worker_name",
    "application_id",
    "application_name",
    "tool_name",
    "event_type",
    "content",
    "step_number",
    "created_at",
)


def _disabled_response() -> str | None:
    from src.extensions.self_learning.paths import self_learning_enabled
    from src.trace import capture_explicit_execution_context

    context = capture_explicit_execution_context()
    agent_config = (
        context.agent_config
        if isinstance(context.agent_config, dict) and context.agent_config
        else None
    )
    if self_learning_enabled(agent_config):
        return None
    return json.dumps(
        {"ok": False, "error": "self_learning is disabled in config"},
        ensure_ascii=False,
    )


def _current_run_id() -> str:
    from src.trace import MissingRunContextError, require_root_run_id

    try:
        return require_root_run_id()
    except MissingRunContextError:
        return ""


def _compact_records(records: list[dict], *, preview_chars: int) -> list[dict]:
    compact: list[dict] = []
    for item in records:
        current = {field: item.get(field) for field in _TOOL_RESULT_FIELDS if item.get(field) not in (None, "")}
        output_data = item.get("output_data") if isinstance(item.get("output_data"), dict) else {}
        preferred = output_data.get("result") or output_data.get("error") or current.get("content")
        content = str(preferred or "")
        current["content_chars"] = len(content)
        if len(content) > preview_chars:
            current["content"] = content[:preview_chars]
            current["content_truncated"] = True
        else:
            current["content"] = content
            current["content_truncated"] = False
        compact.append(current)
    return compact


def session_search(
    query: str,
    limit: int = 10,
    agent: str | None = None,
    app: str | None = None,
    since: str | None = None,
    scope: str = "current_app",
) -> str:
    """Search redacted real records from prior AgentLoom runs.

    Args:
        query: Full-text query to search in indexed run history.
        limit: Maximum number of matching records to return.
        agent: Optional agent name filter.
        app: Optional app filter.
        since: Optional ISO timestamp lower bound.
        scope: "current_app", "project", or "all". Defaults to current app.
    """
    disabled = _disabled_response()
    if disabled is not None:
        return disabled
    root_run_id = _current_run_id()
    if not root_run_id:
        return json.dumps(
            {"ok": False, "error": "missing_run_context"},
            ensure_ascii=False,
            separators=(",", ":"),
        )
    if not query or not str(query).strip():
        raise ValueError("query is required")
    limit = max(1, min(int(limit or 10), _TOOL_MAX_RESULTS))
    scope = (scope or "current_app").strip().lower()
    if scope not in {"current_app", "project", "all"}:
        raise ValueError("scope must be one of current_app, project, all")
    if scope == "current_app" and not app:
        app = current_application_scope().application_id or None
    results = SelfLearningLedger().search_events(
        query,
        limit=limit,
        agent=agent,
        app=app,
        since=since,
        exclude_run_id=root_run_id,
        scope=scope,
    )
    payload = {
        "ok": True,
        "query": query,
        "scope": scope,
        "app": app,
        "results": _compact_records(results, preview_chars=_SEARCH_CONTENT_PREVIEW_CHARS),
        "content_policy": "redacted_real_record_preview",
    }
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"), default=str)


def session_scroll(
    run_id: str,
    event_id: int,
    direction: str = "after",
    window: int = 5,
) -> str:
    """Scroll around an indexed session event.

    Args:
        run_id: Indexed run id returned by session_search.
        event_id: Event id returned by session_search.
        direction: "before" or "after".
        window: Number of neighboring events to return.
    """
    disabled = _disabled_response()
    if disabled is not None:
        return disabled
    current_run_id = _current_run_id()
    if not current_run_id:
        return json.dumps(
            {"ok": False, "error": "missing_run_context"},
            ensure_ascii=False,
            separators=(",", ":"),
        )
    if not run_id or not str(run_id).strip():
        raise ValueError("run_id is required")
    if direction not in {"before", "after"}:
        raise ValueError("direction must be 'before' or 'after'")
    ledger = SelfLearningLedger()
    requested_root = ledger.root_run_id_for(safe_run_id(run_id))
    if requested_root == current_run_id:
        raise ValueError(
            "session_scroll rejected a run in the current root; active context is already available"
        )
    window = max(1, min(int(window or 5), _TOOL_MAX_RESULTS))
    results = _compact_records(
        ledger.scroll_events(run_id, event_id, direction=direction, window=window),
        preview_chars=_SCROLL_CONTENT_PREVIEW_CHARS,
    )
    return json.dumps(
        {"ok": True, "run_id": run_id, "event_id": event_id, "direction": direction, "results": results},
        ensure_ascii=False,
        separators=(",", ":"),
        default=str,
    )
