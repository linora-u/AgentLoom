"""Non-JSON scalars and late job fields cross the same storage boundary."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from src.extensions.self_learning.digest import DigestBuilder
from src.extensions.self_learning.event_schema import CanonicalSessionEvent
from src.extensions.self_learning.learning_jobs import LearningJobQueue
from src.extensions.self_learning.ledger import SelfLearningLedger
from src.extensions.self_learning.memory_store import MemoryStore
from src.extensions.self_learning.redaction import sanitize_value_fragments

_NOW = "2026-07-12T00:00:00+00:00"


class _SecretScalar:
    def __init__(self, secret: str):
        self.secret = secret

    def __str__(self) -> str:
        return f"password={self.secret}"


def _assert_secret_absent_from_sqlite_files(db_path: Path, secret: str) -> None:
    for path in (db_path, Path(f"{db_path}-wal"), Path(f"{db_path}-shm")):
        if path.exists():
            assert secret.encode() not in path.read_bytes(), path


def test_arbitrary_scalar_is_sanitized_before_default_str_serialization(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "self_learning.db"
    secret = "OBJECTSCALARSECRET71"
    value = _SecretScalar(secret)

    sanitized = sanitize_value_fragments({"value": value})
    digest = DigestBuilder().add(ref="event:safe", kind="event", value=value).to_json()
    assert secret not in str(sanitized)
    assert secret not in digest

    ledger = SelfLearningLedger(db_path)
    ledger.append_event(
        CanonicalSessionEvent(
            event_id="safe-event",
            run_id="safe-run",
            event_type="tool_result",
            content="safe",
            input_data={"value": value},
        )
    )
    LearningJobQueue(db_path).enqueue(
        "retention",
        "safe-job",
        "safe-run",
        {"value": value},
        now=_NOW,
    )

    with sqlite3.connect(db_path) as conn:
        stored = " ".join(
            str(value or "")
            for query in (
                "SELECT input_json FROM events",
                "SELECT payload_json FROM learning_jobs",
            )
            for row in conn.execute(query)
            for value in row
        )
        assert secret not in stored
        assert "[REDACTED]" in stored
    _assert_secret_absent_from_sqlite_files(db_path, secret)


def test_generic_late_job_field_honors_sensitive_key_ownership(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "self_learning.db"
    secret = "GENERICJOBFIELDSECRET83"
    queue = LearningJobQueue(db_path)
    queued = queue.enqueue(
        "retention",
        "generic-field",
        "safe-run",
        {},
        now=_NOW,
    )
    worker_token = queue.acquire_worker_lease(
        "safe-worker",
        now=_NOW,
        lease_seconds=30,
    )
    assert worker_token
    claimed = queue.claim(
        "safe-worker",
        worker_token,
        now=_NOW,
        lease_seconds=30,
    )
    assert claimed

    payload = queue.persist_payload_fields(
        int(queued["id"]),
        str(claimed["lease_token"]),
        {"password": secret},
        now=_NOW,
    )

    assert payload["password"] == "[REDACTED]"
    _assert_secret_absent_from_sqlite_files(db_path, secret)


def test_job_effect_timestamp_and_job_id_reject_affinity_secrets_before_write(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "self_learning.db"
    secret = "JOBEFFECTCLOCKSECRET94"
    unsafe = f"password={secret}"
    store = MemoryStore(db_path)

    with store._connect() as conn:
        with pytest.raises(ValueError, match="timestamp contains sensitive"):
            store._record_job_effect_tx(
                conn,
                job_id=1,
                effect_key="safe-effect",
                effect_hash="safe-hash",
                effect_type="safe",
                result={"ok": True},
                now=unsafe,
            )
        with pytest.raises(TypeError, match="job_id must be an integer"):
            store._record_job_effect_tx(
                conn,
                job_id=unsafe,  # type: ignore[arg-type]
                effect_key="safe-effect",
                effect_hash="safe-hash",
                effect_type="safe",
                result={"ok": True},
                now=_NOW,
            )
        assert conn.execute("SELECT COUNT(*) FROM learning_job_effects").fetchone()[0] == 0

    _assert_secret_absent_from_sqlite_files(db_path, secret)
