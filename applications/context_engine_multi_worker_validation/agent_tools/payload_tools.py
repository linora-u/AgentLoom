"""Deterministic payload tools for ContextEngine multi-worker validation."""

from __future__ import annotations


def make_context_engine_log_payload(case_id: str = "log") -> str:
    """Return a large log-like payload with one hidden validation record.

    Args:
        case_id: Label to include in ordinary log lines.
    """

    lines: list[str] = []
    for idx in range(140):
        lines.append(f"2026-06-21T10:00:{idx % 60:02d}Z INFO case={case_id} phase=warmup row={idx:03d}")

    lines.extend(
        [
            "2026-06-21T10:03:01Z ERROR case=log failure=synthetic-but-preserved",
            "Traceback (most recent call last):",
            "  File \"context_engine_validation.py\", line 42, in validate_log_retrieve",
            "RuntimeError: preserve recent error context",
        ]
    )

    for idx in range(140, 220):
        lines.append(f"2026-06-21T10:04:{idx % 60:02d}Z INFO case={case_id} phase=middle row={idx:03d}")

    lines.append("LOG_TARGET_RECORD case=log verification_value=LOG-CTX-8842 checksum=f1b293")

    for idx in range(220, 360):
        lines.append(f"2026-06-21T10:05:{idx % 60:02d}Z INFO case={case_id} phase=tail row={idx:03d}")

    return "\n".join(lines)


def make_context_engine_search_payload(case_id: str = "search") -> str:
    """Return a large grep-like payload with one hidden validation match.

    Args:
        case_id: Label to include in ordinary search result lines.
    """

    lines: list[str] = []
    for idx in range(170):
        lines.append(
            f"src/example/module_{idx % 17}.py:{idx + 10}: "
            f"case={case_id} ordinary_match_{idx:03d}=background"
        )

    lines.append(
        "src/example/target_module.py:777: SEARCH_TARGET_RECORD "
        "case=search verification_value=SEARCH-CTX-6194 checksum=bb72e0"
    )

    for idx in range(170, 350):
        lines.append(
            f"src/example/module_{idx % 19}.py:{idx + 20}: "
            f"case={case_id} ordinary_match_{idx:03d}=background"
        )

    return "\n".join(lines)
