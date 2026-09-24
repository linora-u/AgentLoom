"""Retrieve original content stored by ContextEngine."""

from __future__ import annotations

import re

from agentloom.execution.context_engine.runtime import get_active_context_engine

_DURABLE_REF = re.compile(r"^ctx_[0-9a-f]{32}$")
_MAX_PAGE_BYTES = 65536


def _retrieve_durable(ref: str, query: str, offset: int, limit: int) -> str:
    from agentloom.execution import SecureDirectory, get_current_run_context
    from agentloom.execution.observability import RunTrace, TraceStorageError
    from agentloom.execution.trace import capture_explicit_execution_context

    context = get_current_run_context()
    if context is None:
        return "No active Task; durable context refs require a running Application."
    try:
        with RunTrace(SecureDirectory(context.trace_dir, create=False), context.run_id) as trace:
            metadata = trace.reference_metadata(ref)
            if metadata.get("agent_path") != capture_explicit_execution_context().runtime_agent_path:
                return f"ContextRef not found or unavailable: {ref}"
            if query:
                needle = query.encode("utf-8")
                if len(needle) > 256:
                    return "ContextRef search query must be at most 256 bytes."
                content = trace.read_text(ref).encode("utf-8")
                matches: list[str] = []
                cursor = offset
                max_matches = min(limit or 5, 100)
                while len(matches) < max_matches:
                    found = content.find(needle, cursor)
                    if found < 0:
                        break
                    start = max(0, found - 128)
                    line_start = content.rfind(b"\n", start, found)
                    if line_start >= 0:
                        start = line_start + 1
                    end = min(len(content), found + len(needle) + 256)
                    line_end = content.find(b"\n", found, end)
                    if line_end >= 0:
                        end = line_end
                    excerpt = content[start:end].decode("utf-8", errors="replace")
                    matches.append(f"byte_offset={found} {excerpt}")
                    cursor = found + max(1, len(needle))
                return (
                    f"[ContextRef {ref} search query={query!r} offset={offset} "
                    f"total_bytes={len(content)} next_offset={cursor if len(matches) == max_matches else 'none'}]\n"
                    + "\n".join(matches)
                )
            page_limit = min(limit or 8192, _MAX_PAGE_BYTES)
            page = trace.read_page(ref, offset=offset, limit=page_limit)
            data = page.data
            while data:
                try:
                    body = data.decode("utf-8")
                    break
                except UnicodeDecodeError as exc:
                    if exc.start == 0:
                        return "ContextRef offset splits a UTF-8 character or limit is too small."
                    data = data[:exc.start]
            else:
                body = ""
            next_offset = offset + len(data)
            return (
                f"[ContextRef {ref} retrieved offset={offset} limit={page_limit} "
                f"next_offset={next_offset if next_offset < page.total_bytes else 'none'} "
                f"total_bytes={page.total_bytes}]\n" + body
            )
    except (FileNotFoundError, ValueError, TraceStorageError):
        return f"ContextRef not found or invalid: {ref}"


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

    safe_offset = max(0, int(offset or 0))
    safe_limit = max(0, int(limit or 0))
    ref = str(ref).strip()
    if _DURABLE_REF.fullmatch(ref):
        return _retrieve_durable(ref, str(query or ""), safe_offset, safe_limit)

    engine = get_active_context_engine()
    if engine is None:
        return "No active ContextEngine; context refs require an active task-scoped store."
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
