"""Retrieve original content stored by ContextEngine."""

from __future__ import annotations

from src.lib.context_engine.runtime import get_active_context_engine


def loom_retrieve_context(
    ref: str,
    query: str = "",
    offset: int = 0,
    limit: int = 200,
) -> str:
    """Retrieve original content behind a ContextRef.

    Args:
        ref: Context reference, for example ``ctx_0123abcd4567ef89``.
        query: Optional search query. When provided, only matching lines are returned.
        offset: Line offset for pagination.
        limit: Maximum lines to return. Use ``0`` to return all remaining lines.

    Returns:
        Original content or matching lines from the local ContextEngine store.
    """
    if not ref or not str(ref).strip():
        raise ValueError("ref is required")

    engine = get_active_context_engine()
    if engine is None:
        return "No active ContextEngine; context refs require an active task-scoped store."

    safe_offset = max(0, int(offset or 0))
    safe_limit = max(0, int(limit or 0))
    ref = str(ref).strip()
    entry = engine.get_entry(ref)
    if entry is None:
        return f"ContextRef not found or expired: {ref}"

    content = engine.retrieve(ref, query=str(query or ""), offset=safe_offset, limit=safe_limit)
    if content is None:
        return f"ContextRef not found or expired: {ref}"

    header = (
        f"[ContextRef {entry.ref} retrieved kind={entry.kind.value} source={entry.tool_name} "
        f"query={query!r} offset={safe_offset} limit={safe_limit} "
        f"original_chars={entry.original_chars}]\n"
    )
    return header + content
