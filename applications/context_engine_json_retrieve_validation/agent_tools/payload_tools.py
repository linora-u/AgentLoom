"""Deterministic payload tools for ContextEngine JSON retrieval validation."""

from __future__ import annotations

import json


def make_context_engine_json_payload(case_id: str = "json") -> str:
    """Return a large JSON list with one hidden validation item.

    Args:
        case_id: Label to include in ordinary filler items.
    """

    items: list[dict[str, object]] = []
    for idx in range(90):
        items.append(
            {
                "case": case_id,
                "index": idx,
                "category": "background",
                "payload": "ordinary filler item for smart crusher validation",
            }
        )

    items.append(
        {
            "case": "json",
            "index": 90,
            "category": "JSON_TARGET_RECORD",
            "verification_value": "JSON-CTX-4927",
            "checksum": "0a31ce",
            "status": "must_retrieve_from_context_store",
        }
    )

    for idx in range(91, 185):
        items.append(
            {
                "case": case_id,
                "index": idx,
                "category": "background",
                "payload": "ordinary trailing filler item for smart crusher validation",
            }
        )

    return json.dumps(items, ensure_ascii=False, indent=2)
