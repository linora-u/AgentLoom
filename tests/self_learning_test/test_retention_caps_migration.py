"""Schema v3 migration, capacity failure caps, auto retention, and tool guidance."""

from __future__ import annotations

import json
import sqlite3
import uuid
from pathlib import Path

import pytest

from src.extensions.self_learning.event_schema import CanonicalSessionEvent, now_iso
from src.extensions.self_learning.ledger import SelfLearningLedger
from src.extensions.self_learning.memory_store import MemoryStore
from src.extensions.self_learning.reviewer import _auto_retention

_V3_COLUMNS = {
    "trust_score",
    "injected_count",
    "last_injected_at",
    "helpful_count",
    "unhelpful_count",
    "applied_by",
    "conflicts_json",
    "corroboration_runs_json",
}


def _event(run_id: str, text: str) -> CanonicalSessionEvent:
    return CanonicalSessionEvent(
        event_id=uuid.uuid4().hex,
        run_id=run_id,
        event_type="tool_call",
        content=text,
        content_text=text,
        created_at=now_iso(),
    )


# -- Migration ---------------------------------------------------------------------


def test_pre_v2_db_migrates_v2_then_v3_with_defaults(tmp_path: Path):
    db_path = tmp_path / ".agentloom" / "self_learning.db"
    db_path.parent.mkdir(parents=True)
    with sqlite3.connect(db_path) as conn:
        conn.executescript(
            """
            CREATE TABLE schema_version (version INTEGER NOT NULL);
            CREATE TABLE memory_items (
                id INTEGER PRIMARY KEY AUTOINCREMENT, scope_type TEXT NOT NULL,
                scope_id TEXT NOT NULL DEFAULT '', content TEXT NOT NULL, status TEXT NOT NULL,
                action TEXT NOT NULL, target TEXT, source TEXT, source_run_id TEXT,
                source_event_id TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL, applied_at TEXT);
            INSERT INTO memory_items (scope_type, scope_id, content, status, action, created_at, updated_at)
            VALUES ('project','project','pre-v2 pending fact','pending','add','2026-01-01','2026-01-01');
            """
        )
    SelfLearningLedger._initialized_paths.discard(str(db_path.resolve()))
    store = MemoryStore(db_path)
    with sqlite3.connect(store.db_path) as conn:
        versions = [row[0] for row in conn.execute("SELECT version FROM schema_version ORDER BY version")]
        columns = {row[1] for row in conn.execute("PRAGMA table_info(memory_items)")}
        tables = {
            row[0]
            for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
        }
        row = conn.execute(
            "SELECT trust_score, injected_count, content_hash FROM memory_items WHERE id = 1"
        ).fetchone()
    assert versions == [1, 2, 3, 4]
    assert _V3_COLUMNS <= columns
    assert {"memory_injections", "maintenance"} <= tables
    assert row[0] == 0.5  # trust default backfilled
    assert row[1] == 0
    assert row[2]  # v2 hash backfill ran in the same pass


def test_fresh_db_lands_directly_on_v3(tmp_path: Path):
    store = MemoryStore(tmp_path / ".agentloom" / "self_learning.db")
    with sqlite3.connect(store.db_path) as conn:
        versions = [row[0] for row in conn.execute("SELECT version FROM schema_version ORDER BY version")]
        columns = {row[1] for row in conn.execute("PRAGMA table_info(memory_items)")}
    assert versions == [1, 2, 3, 4]
    assert _V3_COLUMNS <= columns


# -- Capacity failure cap ------------------------------------------------------------


def _capacity_call(scope_id: str) -> dict:
    from src.tools.self_learning.memory_tool import memory

    return json.loads(
        memory("add", scope="session", content="another oversized note " + "y" * 1500, scope_id=scope_id)
    )


def test_capacity_failures_capped_then_terminal_per_run(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("AGENTLOOM_SELF_LEARNING_ROOT", str(tmp_path / ".agentloom"))
    import src.tools.self_learning.memory_tool as memory_tool_module

    monkeypatch.setattr(memory_tool_module, "_capacity_failures", {})
    monkeypatch.setattr(memory_tool_module, "current_session_run_id", lambda: "run_cap")

    store = MemoryStore()
    # Fill the 4000-char session budget almost completely.
    store.add("session", "base note " + "x" * 3500, proposal=False, source="test", scope_id="run_cap")

    results = [_capacity_call("run_cap") for _ in range(5)]
    assert [r["error"] for r in results[:3]] == ["capacity_exceeded"] * 3
    assert all("items_oldest_first" in r for r in results[:3])
    assert results[3]["error"] == "capacity_exceeded_terminal"
    assert "Stop" in results[3]["hint"] and "items_oldest_first" not in results[3]
    assert results[4]["error"] == "capacity_exceeded_terminal"


def test_capacity_counter_is_isolated_between_runs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("AGENTLOOM_SELF_LEARNING_ROOT", str(tmp_path / ".agentloom"))
    import src.tools.self_learning.memory_tool as memory_tool_module

    monkeypatch.setattr(memory_tool_module, "_capacity_failures", {})
    store = MemoryStore()
    for run_id in ("run_one", "run_two"):
        store.add("session", "base note " + "x" * 3500, proposal=False, source="test", scope_id=run_id)

    monkeypatch.setattr(memory_tool_module, "current_session_run_id", lambda: "run_one")
    for _ in range(4):
        result = _capacity_call("run_one")
    assert result["error"] == "capacity_exceeded_terminal"

    monkeypatch.setattr(memory_tool_module, "current_session_run_id", lambda: "run_two")
    fresh = _capacity_call("run_two")
    assert fresh["error"] == "capacity_exceeded", "a new run starts with a fresh failure budget"


# -- Auto retention -------------------------------------------------------------------


def _age_run(db_path: Path, run_id: str) -> None:
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "UPDATE runs SET started_at = '2020-01-01T00:00:00+00:00', ended_at = '2020-01-01T00:00:00+00:00', "
            "indexed_at = '2020-01-01T00:00:00+00:00' WHERE run_id = ?",
            (run_id,),
        )
        conn.commit()


def test_auto_retention_prunes_every_claimed_job(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("AGENTLOOM_SELF_LEARNING_ROOT", str(tmp_path / ".agentloom"))
    ledger = SelfLearningLedger()
    ledger.append_event(_event("run_ancient", "ancient event"))
    _age_run(ledger.db_path, "run_ancient")
    run_dir = tmp_path / "run_dir"
    run_dir.mkdir()

    _auto_retention(run_dir)
    assert ledger.count_events()["runs_indexed"] == 0
    assert "Auto Retention" in (run_dir / "session_summary.md").read_text(encoding="utf-8")

    # Date-level throttling is owned by the outbox's UNIQUE(kind, dedupe_key).
    # Once a retention job is claimed it must always execute; a stale marker
    # must never turn a crash/retry into a false success.
    ledger.append_event(_event("run_ancient_2", "second ancient event"))
    _age_run(ledger.db_path, "run_ancient_2")
    _auto_retention(run_dir)
    assert ledger.count_events()["runs_indexed"] == 0


def test_auto_retention_zero_days_disables_event_pruning(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("AGENTLOOM_SELF_LEARNING_ROOT", str(tmp_path / ".agentloom"))
    monkeypatch.setattr("src.extensions.self_learning.paths.config_int", lambda name, default=0: 0)
    ledger = SelfLearningLedger()
    ledger.append_event(_event("run_ancient", "ancient event"))
    _age_run(ledger.db_path, "run_ancient")
    run_dir = tmp_path / "run_dir"
    run_dir.mkdir()
    _auto_retention(run_dir)
    assert ledger.count_events()["runs_indexed"] == 1, "events must survive with retention disabled"


# -- Legacy artifacts & guidance -------------------------------------------------------


def test_stats_lists_orphan_legacy_dbs(tmp_path: Path):
    root = tmp_path / ".agentloom"
    (root / "memory").mkdir(parents=True)
    (root / "sessions").mkdir(parents=True)
    (root / "memory" / "memory.db").write_bytes(b"stale")
    (root / "sessions" / "session_index.db").write_bytes(b"stale")
    store = MemoryStore(root / "self_learning.db")
    stats = store.stats()
    paths = {entry["path"] for entry in stats["legacy_files"]}
    assert str(root / "memory" / "memory.db") in paths
    assert str(root / "sessions" / "session_index.db") in paths
    assert all("safe to delete manually" in entry["note"] for entry in stats["legacy_files"])


def test_memory_docstring_carries_declarative_and_anti_capture_guard():
    from src.tools.self_learning.memory_tool import memory

    doc = memory.__doc__
    assert "declarative facts, not imperatives" in doc
    assert "environment-dependent failures" in doc
    assert "transient errors" in doc
    assert "one-off task narratives" in doc
    assert '"feedback"' in doc
