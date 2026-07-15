"""Regression tests for SQLite INTEGER-affinity write boundaries."""

from __future__ import annotations

import pytest

from src.extensions.self_learning.event_schema import CanonicalSessionEvent


@pytest.mark.parametrize("invalid", [True, False, "7", "password=STEPSECRET71"])
def test_canonical_event_rejects_non_integer_step_number(invalid: object) -> None:
    with pytest.raises(TypeError, match="step_number must be an integer"):
        CanonicalSessionEvent(
            event_id="event-safe",
            run_id="run-safe",
            step_number=invalid,  # type: ignore[arg-type]
        )
