"""The single model-facing interface for curated durable memory."""

from __future__ import annotations

import json
from typing import Any, Literal

from src.extensions.self_learning.memory_store import MemoryStore, current_session_run_id

_LIST_CONTENT_PREVIEW_CHARS = 160
_LIST_MAX_ITEMS = 20


def _current_agent_config() -> dict[str, Any] | None:
    try:
        from src.trace import capture_explicit_execution_context

        # Never consult task_context's process-global fallback here: two
        # concurrent roots may have different Application identities/policies.
        value = capture_explicit_execution_context().agent_config
        return value if isinstance(value, dict) else None
    except Exception:
        return None


def _compact_list_result(result: dict[str, Any]) -> dict[str, Any]:
    items = result.get("items")
    if not isinstance(items, list):
        return result
    compact: list[dict[str, Any]] = []
    for item in items[:_LIST_MAX_ITEMS]:
        if not isinstance(item, dict):
            continue
        content = str(item.get("content") or "")
        compact.append(
            {
                "id": item.get("id"),
                "scope": item.get("scope"),
                "content": content[:_LIST_CONTENT_PREVIEW_CHARS],
                "content_truncated": len(content) > _LIST_CONTENT_PREVIEW_CHARS,
                "updated_at": item.get("updated_at"),
            }
        )
    return {
        **result,
        "items": compact,
        "item_count": len(items),
        "items_truncated": len(items) > _LIST_MAX_ITEMS,
    }


def memory(
    action: Literal["list", "add", "replace", "remove"],
    scope: Literal["project", "app"] = "project",
    content: str = "",
    target: str = "",
) -> str:
    """Read or change durable facts that should help future AgentLoom runs.

    Call this only for a compact fact that is expected to remain useful after
    the current task ends: a verified project/application convention, a
    corrected assumption, or a reusable solution whose success was observed.

    Never store task progress, TODOs, plans, raw transcripts, run ids, current
    counts, temporary failures, guesses, secrets, credentials, or instructions
    aimed at a future model. History already records the run; memory is only the
    small curated subset worth showing to later runs.

    The canonical write is
    ``memory(action="add", scope="project", content="<standalone fact>")``.
    Never use action="store" or argument names other than this schema: the fact
    belongs in ``content``, not ``fact``, ``key``, or ``value``.

    Repository-wide or checkout-wide facts must use ``project``. Use ``app``
    only when the source explicitly limits the fact to the current Application.
    The tool cannot write another Application's scope.

    Args:
        action: One of ``list``, ``add``, ``replace``, or ``remove``.
        scope: ``project`` or ``app``.
        content: Standalone declarative fact for add/replace.
        target: Exact memory id or a unique content substring for replace/remove.
    """
    from src.extensions.self_learning.paths import self_learning_enabled

    agent_config = _current_agent_config()
    if agent_config is not None and not self_learning_enabled(agent_config):
        return json.dumps(
            {"ok": False, "error": "self_learning_disabled"},
            ensure_ascii=False,
            separators=(",", ":"),
        )
    root_run_id = current_session_run_id()
    if not root_run_id:
        return json.dumps(
            {"ok": False, "error": "missing_run_context"},
            ensure_ascii=False,
            separators=(",", ":"),
        )
    if agent_config is None:
        return json.dumps(
            {"ok": False, "error": "missing_agent_context"},
            ensure_ascii=False,
            separators=(",", ":"),
        )
    try:
        result = MemoryStore(agent_config=agent_config).handle_tool_action(
            action,
            scope=scope,
            content=content,
            target=target,
            root_run_id=root_run_id,
            agent_config=agent_config,
        )
    except (KeyError, ValueError) as exc:
        result = {"ok": False, "error": type(exc).__name__, "message": str(exc)}
    if (action or "").strip().casefold() == "list" and isinstance(result, dict):
        result = _compact_list_result(result)
    return json.dumps(result, ensure_ascii=False, separators=(",", ":"), default=str)
