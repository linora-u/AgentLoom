"""The single model-facing interface for curated durable memory."""

from __future__ import annotations

import json
from typing import Any, Literal

from src.extensions.self_learning.persistence.memory_store import (
    MemoryStore,
    current_session_run_id,
)

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
    action: Literal["list", "propose"],
    scope: Literal["project", "app"] = "app",
    kind: Literal["fact", "experience"] = "fact",
    memory_key: str = "",
    text: str = "",
    trigger: str = "",
    symptom: str = "",
    learned_action: str = "",
    verification: str = "",
) -> str:
    """List active memory or submit one typed Application candidate.

    A model candidate cannot activate, replace, remove, promote, or otherwise
    mutate durable memory. Code-owned evidence gates and the configured scoped
    approval policy decide what happens after submission. Project promotion is
    a separate human review action; ``propose`` therefore accepts only ``app``.
    ``scope="project"`` remains available for read-only ``list``.

    Propose a compact durable fact with ``kind="fact"``, ``memory_key`` and
    ``text``. Propose a short reusable heuristic with ``kind="experience"`` and
    all four fields: ``trigger``, ``symptom``, ``learned_action``, and
    ``verification``. Multi-step procedures, scripts, assets, plans, raw
    transcripts, transient failures, guesses, secrets, credentials, and prompt
    instructions are not memory; complex workflows belong in a separately
    reviewed Skill candidate.

    Args:
        action: ``list`` or add-only ``propose``.
        scope: Current ``app`` for proposals; ``project`` is read-only here.
        kind: ``fact`` or the compact ``experience`` schema.
        memory_key: Stable semantic key used for conflict and override checks.
        text: Standalone fact text.
        trigger: Condition under which an experience applies.
        symptom: Observable failure or situation.
        learned_action: Compact corrective action.
        verification: How success was verified.
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
    if action == "propose" and scope == "project":
        return json.dumps(
            {
                "ok": False,
                "error": "project_promotion_requires_review",
                "message": "Submit an Application candidate; Project promotion is human-reviewed.",
            },
            ensure_ascii=False,
            separators=(",", ":"),
        )
    payload = (
        {"text": text}
        if kind == "fact"
        else {
            "trigger": trigger,
            "symptom": symptom,
            "action": learned_action,
            "verification": verification,
        }
    )
    try:
        result = MemoryStore(agent_config=agent_config).handle_tool_action(
            action,
            scope=scope,
            kind=kind,
            memory_key=memory_key,
            payload=payload,
            root_run_id=root_run_id,
            agent_config=agent_config,
        )
    except (KeyError, ValueError) as exc:
        result = {"ok": False, "error": type(exc).__name__, "message": str(exc)}
    if action == "list" and isinstance(result, dict):
        result = _compact_list_result(result)
    return json.dumps(result, ensure_ascii=False, separators=(",", ":"), default=str)
