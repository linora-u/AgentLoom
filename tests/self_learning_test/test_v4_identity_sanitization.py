"""Legacy identity values cross the same v4 privacy boundary as content."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from src.extensions.self_learning.event_schema import CanonicalSessionEvent
from src.extensions.self_learning.learning_jobs import LearningJobQueue
from src.extensions.self_learning.ledger import SelfLearningLedger
from src.extensions.self_learning.proposal_writer import ProposalWriter
from src.extensions.self_learning.session_index import SessionIndex


@pytest.mark.parametrize(
    "existing_v4",
    (False, True),
    ids=("v3_upgrade", "pre_revision_v4_refresh"),
)
def test_v4_migration_rekeys_secret_identities_and_all_references(
    tmp_path: Path,
    existing_v4: bool,
) -> None:
    db_path = tmp_path / "self_learning.db"
    SelfLearningLedger(db_path)
    secret = "IDENTITYSECRET7"
    run_id = f"api_key={secret}"
    event_id = f"password={secret}"
    task_id = f"client_secret={secret}"
    application_id = f"authorization=Bearer {secret}"
    proposal_id = f"credential={secret}"
    timestamp = "2026-07-12T00:00:00+00:00"

    with sqlite3.connect(db_path) as conn:
        if not existing_v4:
            conn.execute("DELETE FROM schema_version WHERE version = 4")
        conn.execute(
            "DELETE FROM maintenance WHERE key = 'schema_v4_sanitizer_revision'"
        )
        conn.execute(
            """
            INSERT INTO runs (
                run_id, root_run_id, task_id, application_id, status,
                indexed_at, metadata_json
            ) VALUES (?, ?, ?, ?, 'completed', ?, '{}')
            """,
            (run_id, run_id, task_id, application_id, timestamp),
        )
        conn.execute(
            """
            INSERT INTO events (
                event_id, run_id, root_run_id, task_id, parent_task_id,
                parent_event_id, application_id, event_type, content_text,
                created_at, ordinal, metadata_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, 'tool_result', 'safe event', ?, 0, '{}')
            """,
            (
                event_id,
                run_id,
                run_id,
                task_id,
                task_id,
                event_id,
                application_id,
                timestamp,
            ),
        )
        item_id = int(
            conn.execute(
                """
                INSERT INTO memory_items (
                    scope_type, scope_id, content, content_hash, status, action,
                    source, source_run_id, source_event_id, created_at, updated_at
                ) VALUES ('session', ?, 'safe note', 'safe-note-hash', 'active',
                    'add', 'legacy', ?, ?, ?, ?)
                """,
                (run_id, run_id, event_id, timestamp, timestamp),
            ).lastrowid
        )
        conn.execute(
            "INSERT INTO memory_injections (run_id, item_id, injected_at) VALUES (?, ?, ?)",
            (run_id, item_id, timestamp),
        )
        conn.execute(
            """
            INSERT INTO memory_evidence (item_id, root_run_id, source, created_at)
            VALUES (?, ?, 'legacy', ?)
            """,
            (item_id, run_id, timestamp),
        )
        conn.execute(
            """
            INSERT INTO skill_proposals (
                proposal_id, name, action, status, proposal_path, application_id,
                source_run_id, source_event_id, manifest_json, created_at, updated_at
            ) VALUES (?, 'safe', 'add', 'pending', 'safe-path', ?, ?, ?, '{}', ?, ?)
            """,
            (
                proposal_id,
                application_id,
                run_id,
                event_id,
                timestamp,
                timestamp,
            ),
        )
        conn.execute(
            """
            INSERT INTO review_runs (
                source_run_id, trigger_event_id, hook_event, application_id,
                status, output_json, created_at
            ) VALUES (?, ?, 'SessionEnd', ?, 'completed', '{}', ?)
            """,
            (run_id, event_id, application_id, timestamp),
        )
        conn.execute(
            """
            INSERT INTO artifacts (event_id, run_id, kind, uri, metadata_json, created_at)
            VALUES (?, ?, 'safe', 'safe://artifact', '{}', ?)
            """,
            (event_id, run_id, timestamp),
        )
        job_id = int(
            conn.execute(
            """
            INSERT INTO learning_jobs (
                kind, dedupe_key, root_run_id, payload_json, status, attempts,
                available_at, created_at, updated_at
            ) VALUES ('session_review', ?, ?, ?, 'pending', 0, ?, ?, ?)
            """,
            (
                run_id,
                run_id,
                json.dumps(
                    {
                        "root_run_id": run_id,
                        "run_dir": f"/tmp/{run_id}",
                        "prepared_digest": {
                            "text": f'{{"ref":"event:{event_id}"}}',
                            "evidence_refs": [f"event:{event_id}"],
                            "sha256": "stale-digest-sha",
                        },
                        "semantic_plan": {
                            "evidence_refs": [f"event:{event_id}"],
                            "sha256": "stale-plan-sha",
                        },
                    }
                ),
                timestamp,
                timestamp,
                timestamp,
            ),
            ).lastrowid
        )
        conn.execute(
            """
            INSERT INTO learning_job_effects (
                job_id, effect_key, effect_hash, effect_type, result_json,
                created_at, updated_at
            ) VALUES (?, ?, ?, 'safe', '{}', ?, ?)
            """,
            (
                job_id,
                f"api_key={secret}",
                f"password={secret}",
                timestamp,
                timestamp,
            ),
        )
        conn.execute(
            "INSERT INTO maintenance (key, value) VALUES (?, 'safe')",
            (f"credential={secret}",),
        )
        conn.commit()

    SelfLearningLedger._initialized_paths.discard(str(db_path.resolve()))
    migrated = SelfLearningLedger(db_path)

    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        run = conn.execute(
            "SELECT run_id, root_run_id, task_id, application_id FROM runs"
        ).fetchone()
        event = conn.execute(
            """
            SELECT event_id, run_id, root_run_id, task_id, parent_task_id,
                parent_event_id, application_id
            FROM events
            """
        ).fetchone()
        memory = conn.execute(
            "SELECT scope_id, source_run_id, source_event_id FROM memory_items WHERE id = ?",
            (item_id,),
        ).fetchone()
        injection_run = conn.execute(
            "SELECT run_id FROM memory_injections WHERE item_id = ?", (item_id,)
        ).fetchone()[0]
        evidence_root = conn.execute(
            "SELECT root_run_id FROM memory_evidence WHERE item_id = ?", (item_id,)
        ).fetchone()[0]
        proposal = conn.execute(
            """
            SELECT proposal_id, application_id, source_run_id, source_event_id
            FROM skill_proposals
            """
        ).fetchone()
        review = conn.execute(
            "SELECT source_run_id, trigger_event_id, application_id FROM review_runs"
        ).fetchone()
        artifact = conn.execute("SELECT event_id, run_id FROM artifacts").fetchone()
        job = conn.execute(
            """
            SELECT id, dedupe_key, root_run_id, payload_json, status, last_error
            FROM learning_jobs
            """
        ).fetchone()
        logical_values = [
            str(value or "")
            for table_row in conn.execute(
                "SELECT name FROM sqlite_master "
                "WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
            )
            for table in [str(table_row[0]).replace('"', '""')]
            for row in conn.execute(f'SELECT * FROM "{table}"')
            for value in row
        ]
        integrity = conn.execute("PRAGMA integrity_check").fetchone()[0]
        sanitizer_revision = conn.execute(
            "SELECT value FROM maintenance WHERE key = 'schema_v4_sanitizer_revision'"
        ).fetchone()[0]

    assert run["run_id"] == run["root_run_id"]
    assert event["run_id"] == event["root_run_id"] == run["run_id"]
    assert event["task_id"] == event["parent_task_id"] == run["task_id"]
    assert event["parent_event_id"] == event["event_id"]
    assert memory["scope_id"] == memory["source_run_id"] == run["run_id"]
    assert injection_run == evidence_root == run["run_id"]
    assert proposal["source_run_id"] == review["source_run_id"] == run["run_id"]
    assert (
        artifact["run_id"]
        == job["root_run_id"]
        == job["dedupe_key"]
        == run["run_id"]
    )
    assert (
        proposal["source_event_id"]
        == review["trigger_event_id"]
        == event["event_id"]
    )
    assert artifact["event_id"] == event["event_id"]
    assert (
        proposal["application_id"]
        == review["application_id"]
        == run["application_id"]
    )
    assert event["application_id"] == run["application_id"]
    assert proposal["proposal_id"] != proposal_id
    migrated_payload = json.loads(job["payload_json"])
    assert migrated_payload["root_run_id"] == run["run_id"]
    assert run["run_id"] in migrated_payload["run_dir"]
    assert "prepared_digest" not in migrated_payload
    assert "semantic_plan" not in migrated_payload
    assert job["status"] == "dead"
    assert job["last_error"] == "legacy_v4_identity_sanitizer_changed_frozen_job_input"
    assert secret not in " ".join(logical_values)
    assert sanitizer_revision == "4"
    assert migrated.search_events(secret) == []
    assert str(integrity).casefold() == "ok"
    for path in (db_path, Path(f"{db_path}-wal"), Path(f"{db_path}-shm")):
        if path.exists():
            assert secret.encode() not in path.read_bytes()

    with pytest.raises(ValueError, match="cannot be retried"):
        LearningJobQueue(db_path).retry_job(int(job["id"]))


def test_v4_sanitizer_rolls_back_when_existing_fts_rebuild_fails(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "self_learning.db"
    secret = "FTS_REBUILD_ROLLBACK_SECRET_7"
    SelfLearningLedger(db_path)

    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "UPDATE maintenance SET value = '3' "
            "WHERE key = 'schema_v4_sanitizer_revision'"
        )
        conn.execute(
            """
            INSERT INTO runs (
                run_id, status, indexed_at, task_text, metadata_json
            ) VALUES ('fts-rebuild-run', 'completed', '2026-07-12T00:00:00+00:00', ?, '{}')
            """,
            (f"password={secret}",),
        )
        conn.execute("DROP TABLE events_fts")
        conn.execute("CREATE VIRTUAL TABLE events_fts USING fts5(content_text)")
        conn.commit()

    SelfLearningLedger._initialized_paths.discard(str(db_path.resolve()))
    with pytest.raises(sqlite3.OperationalError, match="no column named"):
        SelfLearningLedger(db_path)

    with sqlite3.connect(db_path) as conn:
        sanitizer_revision = conn.execute(
            "SELECT value FROM maintenance "
            "WHERE key = 'schema_v4_sanitizer_revision'"
        ).fetchone()[0]
        task_text = conn.execute(
            "SELECT task_text FROM runs WHERE run_id = 'fts-rebuild-run'"
        ).fetchone()[0]

    assert sanitizer_revision == "3"
    assert task_text == f"password={secret}"


def test_runtime_identity_write_seams_reject_sensitive_values(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "self_learning.db"
    queue = LearningJobQueue(db_path)
    ledger = queue.ledger
    unsafe = "password=RUNTIMEIDENTITYSECRET9"

    with pytest.raises(ValueError, match="sensitive or blocked"):
        queue.enqueue(unsafe, "safe-dedupe", "safe-root", {})
    with pytest.raises(ValueError, match="sensitive or blocked"):
        queue.acquire_worker_lease(unsafe)
    with pytest.raises(ValueError, match="sensitive or blocked"):
        ledger.record_review(
            source_run_id=unsafe,
            hook_event="SessionEnd",
            output={},
        )
    with pytest.raises(ValueError, match="sensitive or blocked"):
        ledger.upsert_skill_proposal(
            proposal_id=unsafe,
            name="safe",
            action="add",
            status="pending",
            proposal_path="safe-path",
        )
    with pytest.raises(ValueError, match="sensitive or blocked"):
        ledger.append_event(
            CanonicalSessionEvent(
                event_id="safe-event",
                run_id="safe-run",
                event_type="tool_result",
                content="safe",
            ),
            root_run_id=unsafe,
        )
    with pytest.raises(ValueError, match="sensitive or blocked"):
        ledger.set_maintenance(unsafe, "safe-value")
    with pytest.raises(ValueError, match="sensitive or blocked"):
        ledger.set_maintenance("safe-key", unsafe)
    with pytest.raises(ValueError, match="sensitive or blocked"):
        ledger.claim_maintenance_slot("safe-key", "", unsafe)

    with sqlite3.connect(db_path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM runs").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM events").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM learning_jobs").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM review_runs").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM skill_proposals").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM maintenance").fetchone()[0] == 2


def test_jsonl_import_rejects_secret_identity_before_filename_normalization(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "self_learning.db"
    event_file = tmp_path / "unsafe.jsonl"
    secret = "IMPORTSECRET7"
    unsafe = f"password={secret}"
    event_file.write_text(
        json.dumps(
            {
                "event_id": "safe-event",
                "run_id": unsafe,
                "root_run_id": unsafe,
                "event_type": "tool_result",
                "content": "safe",
            }
        )
        + "\n",
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="sensitive or blocked"):
        SessionIndex(db_path).index_run(event_file)

    with sqlite3.connect(db_path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM runs").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM events").fetchone()[0] == 0
    for path in (db_path, Path(f"{db_path}-wal"), Path(f"{db_path}-shm")):
        if path.exists():
            assert secret.encode() not in path.read_bytes()


def test_skill_proposal_rejects_secrets_before_path_normalization(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state_root = tmp_path / ".agentloom"
    proposals = tmp_path / "proposals"
    monkeypatch.setenv("AGENTLOOM_SELF_LEARNING_ROOT", str(state_root))
    writer = ProposalWriter(proposals_dir=proposals, skills_dir=tmp_path / "skills")
    secret = "SKILLSECRET7"

    with pytest.raises(ValueError, match="sensitive or blocked"):
        writer.create(action="create", name=f"password={secret}")

    writer.create(action="create", name="safe-skill", content="# Safe skill")
    with pytest.raises(ValueError, match="sensitive or blocked"):
        writer.create(
            action="write_file",
            name="safe-skill",
            path=f"references/password={secret}.txt",
            content="safe",
        )
    with pytest.raises(ValueError, match="sensitive or blocked"):
        writer.create(
            action="patch",
            name="safe-skill",
            target=f"api_key={secret}",
        )

    with sqlite3.connect(state_root / "self_learning.db") as conn:
        logical = " ".join(
            str(value or "")
            for row in conn.execute("SELECT * FROM skill_proposals")
            for value in row
        )
    artifacts = " ".join(
        str(path.relative_to(proposals)) + " " + path.read_text(encoding="utf-8")
        for path in proposals.rglob("*")
        if path.is_file()
    )
    assert secret not in logical
    assert secret not in artifacts
