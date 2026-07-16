"""Independent oracle and case plan for the v5 offline memory campaign.

This module is deliberately stdlib-only.  Expected classifications are fixed
here instead of being derived through the self-learning implementation under
test.  The runner is the only module that imports production APIs.
"""

from __future__ import annotations

import hashlib
from collections import OrderedDict
from dataclasses import dataclass
from typing import Any

DEFAULT_EVENTS = 100_000
DEFAULT_SEED = 20_260_711

CATEGORY_WEIGHTS = OrderedDict(
    (
        ("ledger_fts_search_scroll", 50),
        ("redaction_injection", 20),
        ("root_isolation", 20),
        ("active_pending_memory", 10),
    )
)

_VARIANTS: dict[str, tuple[tuple[str, str], ...]] = {
    "ledger_fts_search_scroll": (
        ("task", "searchable"),
        ("tool_call", "searchable"),
        ("tool_result", "searchable"),
        ("checkpoint", "searchable"),
        ("run_completed", "searchable"),
    ),
    "redaction_injection": (
        ("secret_assignment", "redacted"),
        ("short_password", "redacted"),
        ("nested_secret", "redacted"),
        ("camel_credential", "redacted"),
        ("unicode_secret_key", "redacted"),
        ("authorization_short", "redacted"),
        ("injection_override", "blocked"),
        ("injection_unicode", "blocked"),
        ("injection_fence", "blocked"),
        ("injection_shell", "blocked"),
        ("safe_sort_key", "safe"),
        ("safe_token_count", "safe"),
        ("safe_monkey", "safe"),
        ("safe_public_key", "safe"),
        ("safe_recurring_rule", "safe"),
        ("safe_cjk", "safe"),
        ("safe_joiner", "safe"),
        ("safe_path", "safe"),
        ("safe_version", "safe"),
        ("safe_unit", "safe"),
    ),
    "root_isolation": (
        ("safe_worker", "searchable"),
        ("taint_worker", "blocked"),
        ("tainted_root_completion", "blocked"),
        ("isolated_root_completion", "safe"),
    ),
    "active_pending_memory": (
        ("active_project_add", "active"),
        ("active_application_add", "active"),
        ("pending_add", "pending"),
        ("approve_pending", "approved"),
        ("reject_pending", "rejected"),
        ("stale_replace", "stale"),
        ("normalized_duplicate", "duplicate"),
        ("missing_root", "missing_run_context"),
        ("application_isolation", "isolated"),
        ("direct_replace_remove", "removed"),
    ),
}


@dataclass(frozen=True)
class OfflineCase:
    global_index: int
    category_index: int
    category: str
    case_id: str
    variant: str
    expected_class: str
    payload_bytes: int
    private_token: str


def allocate_quotas(events: int) -> dict[str, int]:
    """Allocate every event with stable largest-remainder rounding."""
    events = int(events)
    if events < 1:
        raise ValueError("events must be a positive integer")
    total_weight = sum(CATEGORY_WEIGHTS.values())
    raw = {category: events * weight / total_weight for category, weight in CATEGORY_WEIGHTS.items()}
    quotas = {category: int(value) for category, value in raw.items()}
    remaining = events - sum(quotas.values())
    order = sorted(
        CATEGORY_WEIGHTS,
        key=lambda category: (
            -(raw[category] - quotas[category]),
            list(CATEGORY_WEIGHTS).index(category),
        ),
    )
    for category in order[:remaining]:
        quotas[category] += 1
    return quotas


def payload_size_for_position(index: int, total: int) -> int:
    """Return the fixed 160 B / 2 KB / 32 KB / 60 KB payload profile."""
    index = int(index)
    total = int(total)
    if total < 1 or index < 0 or index >= total:
        raise ValueError("payload position must satisfy 0 <= index < total")
    slot = 999 if total == 1 else (index * 999) // (total - 1)
    if slot <= 499:
        return 96 + (slot * 63) // 499
    if slot <= 949:
        return 160 + ((slot - 500) * 1_887) // 449
    if slot <= 989:
        return 2_048 + ((slot - 950) * 29_951) // 39
    if slot <= 998:
        return 32_000 + ((slot - 990) * 27_999) // 8
    return 60_000


def _token(seed: int, category: str, category_index: int) -> str:
    value = f"offline-v5:{seed}:{category}:{category_index}"
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:20]


def build_case_plan(events: int, seed: int) -> list[OfflineCase]:
    """Build a deterministic, independently classified event plan."""
    events = int(events)
    seed = int(seed)
    quotas = allocate_quotas(events)
    plan: list[OfflineCase] = []
    global_index = 0
    for category, quota in quotas.items():
        variants = _VARIANTS[category]
        for category_index in range(quota):
            variant, expected_class = variants[category_index % len(variants)]
            case_id = f"{category}-{category_index:06d}"
            plan.append(
                OfflineCase(
                    global_index=global_index,
                    category_index=category_index,
                    category=category,
                    case_id=case_id,
                    variant=variant,
                    expected_class=expected_class,
                    # Every production path receives the complete payload
                    # profile.  Using the global, category-contiguous index
                    # would put all 32-60 KB payloads in the final category
                    # and leave FTS/redaction effectively untested at size.
                    payload_bytes=payload_size_for_position(category_index, quota),
                    private_token=_token(seed, category, category_index),
                )
            )
            global_index += 1
    if len(plan) != events:
        raise AssertionError(f"offline plan changed event count: {len(plan)} != {events}")
    return plan


def case_artifact_row(case: OfflineCase) -> dict[str, Any]:
    """Return the only case metadata allowed in persistent artifacts."""
    return {
        "case_id": case.case_id,
        "category": case.category,
        "variant": case.variant,
        "expected_class": case.expected_class,
        "payload_bytes": case.payload_bytes,
    }


def private_marker(case: OfflineCase) -> str:
    """Build an in-memory sensitive marker; callers must never persist it."""
    prefix = "MVINJECT_" if case.expected_class == "blocked" else "MVSECRET_"
    return f"{prefix}{case.private_token}"


def safe_marker(case: OfflineCase) -> str:
    """Build a non-sensitive unique search marker."""
    return f"MVSAFE_{case.private_token}"
