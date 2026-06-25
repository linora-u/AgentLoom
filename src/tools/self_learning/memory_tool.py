"""Persistent memory tool."""

from __future__ import annotations

import json

from src.extensions.self_learning.memory_store import MemoryStore

_LIST_CONTENT_PREVIEW_CHARS = 80
_LIST_MAX_ITEMS = 6


def _compact_list_result(result: dict) -> dict:
    items = result.get("items")
    if not isinstance(items, list):
        return result
    ordered_items = sorted(
        items,
        key=lambda item: int(item.get("id") or 0) if isinstance(item, dict) else 0,
        reverse=True,
    )
    compact_items = []
    for item in ordered_items[:_LIST_MAX_ITEMS]:
        if not isinstance(item, dict):
            continue
        current = {
            key: item.get(key)
            for key in (
                "id",
                "scope",
                "scope_type",
                "scope_id",
                "application_id",
                "status",
                "action",
                "target",
                "source",
                "created_at",
                "updated_at",
            )
            if item.get(key) not in (None, "")
        }
        content = str(item.get("content") or "")
        current["content_chars"] = len(content)
        if len(content) > _LIST_CONTENT_PREVIEW_CHARS:
            current["content"] = content[:_LIST_CONTENT_PREVIEW_CHARS]
            current["content_truncated"] = True
        else:
            current["content"] = content
            current["content_truncated"] = False
        compact_items.append(current)
    return {
        **result,
        "items": compact_items,
        "item_count": len(items),
        "items_truncated": len(items) > _LIST_MAX_ITEMS,
        "sort": "id_desc",
        "content_policy": "redacted_real_memory_preview",
    }


def memory(
    action: str,
    scope: str = "project",
    content: str = "",
    target: str = "",
    scope_id: str = "",
) -> str:
    """List or propose changes to durable AgentLoom memory.

    Model-facing writes are always proposal-only. Apply accepted proposals
    explicitly with the memory CLI.

    Args:
        action: One of "add", "replace", "remove", or "list".
        scope: Memory scope: "project" or "app".
        content: Memory content for add/replace.
        target: Target id or content substring for replace/remove.
        scope_id: Explicit application id for app scope. Defaults to current app.
    """
    result = MemoryStore().handle_tool_action(
        action,
        scope=scope,
        content=content,
        target=target,
        scope_id=scope_id,
    )
    if (action or "").strip().lower() == "list" and isinstance(result, dict):
        result = _compact_list_result(result)
    return json.dumps(result, ensure_ascii=False, separators=(",", ":"), default=str)
