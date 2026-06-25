"""Session search tools."""

from __future__ import annotations

import json

from src.extensions.self_learning.application_scope import current_application_scope
from src.extensions.self_learning.event_schema import safe_run_id
from src.extensions.self_learning.session_index import SessionIndex

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


def _current_run_id() -> str:
    try:
        from src.trace import get_current_hook_manager

        manager = get_current_hook_manager()
        raw = getattr(manager, "_session_id", "") if manager is not None else ""
        return safe_run_id(raw) if raw else ""
    except Exception:
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
    if not query or not str(query).strip():
        raise ValueError("query is required")
    limit = max(1, min(int(limit or 10), _TOOL_MAX_RESULTS))
    scope = (scope or "current_app").strip().lower()
    if scope not in {"current_app", "project", "all"}:
        raise ValueError("scope must be one of current_app, project, all")
    if scope == "current_app" and not app:
        app = current_application_scope().application_id or None
    results = SessionIndex().search(
        query,
        limit=limit,
        agent=agent,
        app=app,
        since=since,
        exclude_run_id=_current_run_id(),
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
    if not run_id or not str(run_id).strip():
        raise ValueError("run_id is required")
    if direction not in {"before", "after"}:
        raise ValueError("direction must be 'before' or 'after'")
    current_run_id = _current_run_id()
    if current_run_id and safe_run_id(run_id) == current_run_id:
        raise ValueError("session_scroll rejected current run; active context is already available")
    window = max(1, min(int(window or 5), _TOOL_MAX_RESULTS))
    results = _compact_records(
        SessionIndex().scroll(run_id, event_id, direction=direction, window=window),
        preview_chars=_SCROLL_CONTENT_PREVIEW_CHARS,
    )
    return json.dumps(
        {"ok": True, "run_id": run_id, "event_id": event_id, "direction": direction, "results": results},
        ensure_ascii=False,
        separators=(",", ":"),
        default=str,
    )
