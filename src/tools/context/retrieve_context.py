"""Retrieve original content stored by ContextEngine."""

from __future__ import annotations

import re

from agentloom.execution.context_engine.runtime import get_active_context_engine

_DURABLE_REF = re.compile(r"^ctx_[0-9a-f]{32}$")


def _retrieve_durable(ref: str, query: str, offset: int, limit: int) -> str:
    from agentloom.execution import SecureDirectory, get_current_run_context
    from agentloom.execution.observability import (
        MAX_DURABLE_CONTEXT_PAGE_BYTES,
        RunTrace,
        TraceStorageError,
        get_current_trace_recorder,
    )
    from agentloom.execution.trace import capture_explicit_execution_context

    context = get_current_run_context()
    if context is None:
        return "No active Task; durable context refs require a running Application."
    try:
        with RunTrace(SecureDirectory(context.trace_dir, create=False), context.run_id) as trace:
            metadata = trace.reference_metadata(ref)
            producer = metadata.get("agent_path")
            reader = capture_explicit_execution_context().runtime_agent_path
            if producer != reader and not (
                isinstance(producer, str) and isinstance(reader, str)
                and producer.startswith(reader + "/")
            ):
                return f"ContextRef not found or unavailable: {ref}"
            if query:
                needle = query.encode("utf-8")
                if len(needle) > 256:
                    return "ContextRef search query must be at most 256 bytes."
                max_matches = min(limit or 5, 100)
                page = trace.search_page(ref, query, offset=offset, limit=max_matches)
                return (
                    f"[ContextRef {ref} search query={query!r} offset={offset} "
                    f"total_bytes={page.total_bytes} next_offset={page.next_offset if page.next_offset is not None else 'none'}]\n"
                    + "\n".join(f"byte_offset={position} {excerpt}" for position, excerpt in page.matches)
                )
            recorder = get_current_trace_recorder()
            budget_limit = (
                recorder.context_page_byte_limit()
                if recorder is not None else MAX_DURABLE_CONTEXT_PAGE_BYTES
            )
            page_limit = min(limit or MAX_DURABLE_CONTEXT_PAGE_BYTES, budget_limit)
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
    limit: int | None = None,
) -> str:
    """Retrieve retained content behind a ContextRef.

    Args:
        ref: Context reference, for example ``ctx_0123abcd4567ef89``.
        query: Optional search query. A durable ref scans at most one page per call;
            follow ``next_offset`` until a match is found or it says ``none``.
        offset: Byte offset for durable refs; line offset for older ContextEngine
            refs. Repeat the same offset to reread a page; use ``next_offset``
            from the result to continue.
        limit: For durable refs, maximum bytes or search matches per call;
            omitted or ``0`` reads up to 65536 bytes when not searching, further
            bounded by the receiving model's input budget. For older refs,
            maximum lines; omitted selects 200 and ``0`` returns all remaining
            lines.

    Returns:
        Retained content or search excerpts with a continuation offset.
    """
    if not ref or not str(ref).strip():
        raise ValueError("ref is required")

    safe_offset = max(0, int(offset or 0))
    requested_limit = None if limit is None else max(0, int(limit or 0))
    ref = str(ref).strip()
    if _DURABLE_REF.fullmatch(ref):
        return _retrieve_durable(ref, str(query or ""), safe_offset, requested_limit or 0)

    safe_limit = 200 if requested_limit is None else requested_limit

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
