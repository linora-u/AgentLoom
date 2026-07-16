from __future__ import annotations

import uuid
from pathlib import Path

import pytest
from click.testing import CliRunner

from src.extensions.self_learning.event_schema import CanonicalSessionEvent, now_iso
from src.extensions.self_learning.ledger import SelfLearningLedger


def test_sessions_prune_rejects_negative_days_before_constructing_ledger(
    monkeypatch,
) -> None:
    import src.extensions.self_learning.ledger as ledger_module
    from src.__main__ import sessions_prune

    constructed = False

    class LedgerMustNotBeConstructed:
        def __init__(self) -> None:
            nonlocal constructed
            constructed = True
            raise AssertionError("invalid CLI input reached the ledger")

    monkeypatch.setattr(
        ledger_module,
        "SelfLearningLedger",
        LedgerMustNotBeConstructed,
    )

    result = CliRunner().invoke(
        sessions_prune,
        ["--retention-days", "-1"],
    )

    assert result.exit_code == 2
    assert "Invalid value for '--retention-days'" in result.output
    assert constructed is False


def test_sessions_prune_accepts_zero_as_the_explicit_full_history_cutoff(
    monkeypatch,
) -> None:
    import src.extensions.self_learning.ledger as ledger_module
    from src.__main__ import sessions_prune

    received_days: list[int] = []

    class RecordingLedger:
        def prune_events(self, *, retention_days: int) -> dict[str, object]:
            received_days.append(retention_days)
            return {"ok": True, "retention_days": retention_days}

    monkeypatch.setattr(ledger_module, "SelfLearningLedger", RecordingLedger)

    result = CliRunner().invoke(
        sessions_prune,
        ["--retention-days", "0"],
    )

    assert result.exit_code == 0
    assert received_days == [0]
    assert '"retention_days": 0' in result.output


def test_prune_events_rejects_negative_days_without_deleting_history(
    tmp_path: Path,
) -> None:
    ledger = SelfLearningLedger(tmp_path / "self_learning.db")
    ledger.append_event(
        CanonicalSessionEvent(
            event_id=uuid.uuid4().hex,
            run_id="run_must_survive_invalid_retention",
            event_type="tool_result",
            content="history marker",
            content_text="history marker",
            created_at=now_iso(),
        )
    )
    before = ledger.count_events()

    with pytest.raises(ValueError, match="retention_days must be non-negative"):
        ledger.prune_events(retention_days=-1)

    assert ledger.count_events() == before
