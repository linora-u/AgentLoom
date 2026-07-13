"""Persistent memory tool."""

from __future__ import annotations

import json
import threading

from src.extensions.self_learning.memory_store import MemoryStore, current_session_run_id

_LIST_CONTENT_PREVIEW_CHARS = 80
_LIST_MAX_ITEMS = 6

# A fragile memory write must not loop a run to death: after this many
# capacity_exceeded results in one run the tool answers terminally so the
# model stops retrying and finishes its task (the fact can wait a run).
_MAX_CAPACITY_FAILURES_PER_RUN = 3
_CAPACITY_FAILURE_KEYS_CAP = 32
_capacity_failures: dict[str, int] = {}
_capacity_failures_lock = threading.Lock()


def _register_capacity_failure(run_key: str) -> int:
    with _capacity_failures_lock:
        if len(_capacity_failures) > _CAPACITY_FAILURE_KEYS_CAP:
            for stale in list(_capacity_failures)[: len(_capacity_failures) // 2]:
                if stale != run_key:
                    _capacity_failures.pop(stale, None)
        _capacity_failures[run_key] = _capacity_failures.get(run_key, 0) + 1
        return _capacity_failures[run_key]


def _scrub_flagged_content(result: dict) -> dict:
    """Replace injection-flagged memory content in model-facing results.

    Snapshots quarantine flagged rows behind [BLOCKED]; tool results (list
    previews, duplicate-add echoes) must not become the side door.
    """
    from src.extensions.self_learning.redaction import scan_injection_patterns

    def scrub_item(item):
        if isinstance(item, dict) and scan_injection_patterns(str(item.get("content") or "")):
            return {**item, "content": "[BLOCKED: matched threat pattern(s)]"}
        return item

    scrubbed = dict(result)
    if isinstance(scrubbed.get("item"), dict):
        scrubbed["item"] = scrub_item(scrubbed["item"])
    if isinstance(scrubbed.get("items"), list):
        scrubbed["items"] = [scrub_item(item) for item in scrubbed["items"]]
    return scrubbed


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
                "trust_score",
                "created_at",
                "updated_at",
            )
            if item.get(key) not in (None, "")
        }
        current["has_conflicts"] = bool(item.get("conflicts_json"))
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
    operations: str = "",
    helpful: bool = True,
) -> str:
    """Save durable facts to AgentLoom memory so future runs stop rediscovering them.

    WHAT to save: a stable fact about this project or application that the next
    run would otherwise have to re-learn — environment quirks, data-source
    conventions, workflow lessons, recurring failure causes and their fixes.
    Priority: corrections of earlier wrong assumptions > environment facts >
    procedures.

    WRITE declarative facts, not imperatives. "The export API paginates at 100
    rows" is correct; "Always paginate the export API" is wrong — imperative
    phrasing gets re-read as a standing order in every future run.

    Do NOT save (these become self-imposed constraints that bite later runs):
    environment-dependent failures, negative claims about tools ("X is broken"),
    transient errors that a retry fixed (save the retry pattern instead),
    one-off task narratives, or anything stale within a week (run ids, file
    counts, progress markers). Task progress and raw data belong in
    session_search, not memory. If a failure came from setup state, save the FIX.

    Scopes: "project" = global facts for every run; "app" = facts for the
    current application only; "session" = run-local working notes that survive
    context compression and are distilled into proposals when the run ends.
    Project/app writes create pending proposals (auto-applied when they pass
    the safety gates, otherwise reviewed via the memory CLI); session writes
    take effect immediately.

    If a write is rejected with capacity_exceeded, consolidate in ONE call:
    pass action="batch" with an operations JSON array that removes or replaces
    stale entries and adds the new fact together — the budget is checked on the
    final state only.

    When an injected memory proved wrong or misleading during this run, mark it
    with action="feedback", helpful=false so it stops surfacing.

    Args:
        action: One of "add", "replace", "remove", "list", "batch", or "feedback".
        scope: Memory scope: "project", "app", or "session".
        content: Memory content for add/replace. Keep it a compact, standalone fact.
        target: Exact id or unique content substring for replace/remove/feedback.
            Ambiguous substrings are rejected with a candidate list.
        scope_id: Explicit application id (app scope) or run id (session scope).
            Defaults to the current application/run.
        operations: For action="batch": a JSON array like
            '[{"action":"remove","target":"12"},{"action":"add","content":"..."}]'
            applied atomically.
        helpful: For action="feedback": true if the memory helped, false if it
            was wrong or misleading.
    """
    from src.extensions.self_learning.paths import config_bool

    if not config_bool("enabled", True):
        return json.dumps({"ok": False, "error": "self_learning is disabled in config"}, ensure_ascii=False)
    parsed_operations = None
    if operations:
        try:
            parsed_operations = json.loads(operations)
        except json.JSONDecodeError as exc:
            raise ValueError(f"operations must be a JSON array of operation objects: {exc}") from None
    root_run_id = current_session_run_id()
    if not root_run_id:
        return json.dumps(
            {"ok": False, "error": "missing_run_context"},
            ensure_ascii=False,
            separators=(",", ":"),
        )
    result = MemoryStore().handle_tool_action(
        action,
        scope=scope,
        content=content,
        target=target,
        scope_id=scope_id,
        operations=parsed_operations,
        helpful=helpful,
        root_run_id=root_run_id,
    )
    if isinstance(result, dict) and result.get("error") == "capacity_exceeded":
        run_key = current_session_run_id() or "no_run"
        failures = _register_capacity_failure(run_key)
        if failures > _MAX_CAPACITY_FAILURES_PER_RUN:
            result = {
                "ok": False,
                "error": "capacity_exceeded_terminal",
                "hint": (
                    f"Memory consolidation failed {failures} times this run. Stop "
                    "retrying memory writes and continue your task; the fact can be "
                    "saved in a later run."
                ),
            }
    if isinstance(result, dict):
        result = _scrub_flagged_content(result)
        if (action or "").strip().lower() == "list":
            result = _compact_list_result(result)
    return json.dumps(result, ensure_ascii=False, separators=(",", ":"), default=str)
