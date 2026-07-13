"""Regression tests for SQLite INTEGER-affinity write boundaries."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from src.extensions.self_learning.event_schema import CanonicalSessionEvent
from src.extensions.self_learning.ledger import SelfLearningLedger
from src.extensions.self_learning.memory_store import MemoryStore


@pytest.mark.parametrize("invalid", [True, False, "7", "password=STEPSECRET71"])
def test_canonical_event_rejects_non_integer_step_number(invalid: object) -> None:
    with pytest.raises(TypeError, match="step_number must be an integer"):
        CanonicalSessionEvent(
            event_id="event-safe",
            run_id="run-safe",
            step_number=invalid,  # type: ignore[arg-type]
        )


def test_integer_affinity_rejections_are_atomic_and_leave_no_secret_bytes(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "self_learning.db"
    ledger = SelfLearningLedger(db_path)
    store = MemoryStore(db_path)
    item_id = int(
        store.add(
            "project",
            "safe memory used to detect partial injection writes",
            proposal=False,
            source="test",
        )["id"]
    )
    secret = "INTEGERAFFINITYSECRET92"
    unsafe = f"password={secret}"

    # Keep one WAL connection alive so both the main database and WAL are
    # present while we prove that rejected values never reach either file.
    keeper = sqlite3.connect(db_path)
    try:
        assert keeper.execute("PRAGMA journal_mode=WAL").fetchone()[0] == "wal"
        keeper.execute(
            "INSERT OR REPLACE INTO maintenance(key, value) VALUES (?, ?)",
            ("integer-affinity-test", "safe"),
        )
        keeper.commit()

        forged_event = CanonicalSessionEvent(
            event_id="event-safe",
            run_id="run-safe",
            step_number=1,
        )
        object.__setattr__(forged_event, "step_number", unsafe)
        with pytest.raises(TypeError, match="step_number must be an integer"):
            ledger.append_event(forged_event)

        for invalid_job_id in (True, "7", unsafe):
            with pytest.raises(
                TypeError,
                match="learning_job_id must be an integer",
            ):
                ledger.record_review(
                    source_run_id="run-safe",
                    hook_event="SessionEnd",
                    learning_job_id=invalid_job_id,  # type: ignore[arg-type]
                )

        for invalid_item_id in (True, "7", unsafe):
            with pytest.raises(TypeError, match="item id must be an integer"):
                store.record_injections(
                    "run-safe",
                    [item_id, invalid_item_id],  # type: ignore[list-item]
                )

        assert keeper.execute("SELECT COUNT(*) FROM runs").fetchone()[0] == 0
        assert keeper.execute("SELECT COUNT(*) FROM events").fetchone()[0] == 0
        assert keeper.execute("SELECT COUNT(*) FROM review_runs").fetchone()[0] == 0
        assert keeper.execute("SELECT COUNT(*) FROM memory_injections").fetchone()[0] == 0
        assert keeper.execute(
            "SELECT injected_count FROM memory_items WHERE id = ?",
            (item_id,),
        ).fetchone()[0] == 0

        wal_path = Path(f"{db_path}-wal")
        assert wal_path.exists()
        for path in (db_path, wal_path):
            assert secret.encode() not in path.read_bytes()
    finally:
        keeper.close()
