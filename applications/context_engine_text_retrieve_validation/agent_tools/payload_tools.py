"""Deterministic payload tools for ContextEngine text retrieval validation."""

from __future__ import annotations


def make_context_engine_text_payload(case_id: str = "text") -> str:
    """Return a large plain-text payload with one hidden validation record.

    The returned string is intentionally larger than the ContextEngine
    threshold. The validation record sits near the middle so it is not
    guaranteed to survive a head/tail preview.

    Args:
        case_id: Label to include in ordinary filler rows.
    """

    lines: list[str] = []
    for idx in range(140):
        lines.append(
            f"section=alpha case={case_id} row={idx:03d} "
            f"notes=ordinary background filler for reversible compression validation"
        )

    lines.append(
        "TARGET_RECORD case=text verification_value=TEXT-CTX-7319 "
        "checksum=7d5b1c status=must_retrieve_from_context_store"
    )

    for idx in range(140, 305):
        lines.append(
            f"section=omega case={case_id} row={idx:03d} "
            f"notes=ordinary trailing filler for reversible compression validation"
        )

    return "\n".join(lines)
