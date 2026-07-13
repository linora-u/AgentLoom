"""Public-contract regressions for exact evidence and immutable revisions."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from src.extensions.self_learning.memory_store import MemoryStore


def _store(tmp_path: Path) -> MemoryStore:
    return MemoryStore(tmp_path / ".agentloom" / "self_learning.db")


def _items(store: MemoryStore) -> dict[int, dict]:
    return {int(item["id"]): item for item in store.export_items()}


def test_only_normalized_exact_content_can_corroborate(tmp_path: Path):
    store = _store(tmp_path)
    first = store.add(
        "project",
        "The API rate limit is 100 requests per minute",
        proposal=True,
        source="llm_distill",
        source_run_id="run_1",
    )

    changed_value = store.add(
        "project",
        "The API rate limit is 500 requests per minute",
        proposal=True,
        source="llm_distill",
        source_run_id="run_2",
    )
    changed_punctuation = store.add(
        "project",
        "The API rate limit is 100 requests per minute.",
        proposal=True,
        source="llm_distill",
        source_run_id="run_2",
    )

    assert changed_value.get("duplicate") is not True
    assert changed_punctuation.get("duplicate") is not True
    result = store.auto_apply_pending(run_id="run_2")
    assert result["applied"] == []
    assert {item["id"] for item in store.list("project")} == {
        first["id"],
        changed_value["id"],
        changed_punctuation["id"],
    }


def test_case_and_whitespace_normalized_duplicate_from_two_roots_auto_applies(tmp_path: Path):
    store = _store(tmp_path)
    first = store.add(
        "project",
        "Repo   uses  PyTest",
        proposal=True,
        source="model_tool",
        source_run_id="root_a",
    )
    second = store.add(
        "project",
        "repo uses pytest",
        proposal=True,
        source="model_tool",
        source_run_id="root_b",
    )

    assert second["duplicate"] is True
    assert second["corroborated"] is True
    applied = store.auto_apply_pending(run_id="root_b")
    assert [entry["id"] for entry in applied["applied"]] == [first["id"]]


def test_exact_evidence_uses_unicode_casefold_not_ascii_lower(tmp_path: Path):
    store = _store(tmp_path)
    first = store.add(
        "project",
        "Die Straße ist gesperrt",
        proposal=True,
        source="model_tool",
        source_run_id="root-a",
    )
    second = store.add(
        "project",
        "DIE STRASSE IST GESPERRT",
        proposal=True,
        source="model_tool",
        source_run_id="root-b",
    )

    assert second["duplicate"] is True
    assert second["item"]["id"] == first["id"]
    assert second["evidence_count"] == 2


def test_replace_creates_new_revision_and_old_run_cannot_vouch_for_it(tmp_path: Path):
    store = _store(tmp_path)
    old = store.add("project", "batch size is 100", proposal=False, source="test")
    store.snapshot_for_prompt(session_run_id="root_old", record_usage=True)

    replaced = store.replace(
        "project",
        str(old["id"]),
        "batch size is 500",
        proposal=False,
        source="test",
    )
    assert replaced["old_id"] == old["id"]
    assert replaced["new_id"] != old["id"]

    store.record_run_outcome("root_old", succeeded=True)
    items = _items(store)
    old_revision = items[old["id"]]
    new_revision = items[replaced["new_id"]]
    assert old_revision["status"] == "superseded"
    assert old_revision["trust_score"] == pytest.approx(0.52)
    assert new_revision["status"] == "active"
    assert new_revision["generation"] == 2
    assert new_revision["supersedes_id"] == old["id"]
    assert new_revision["trust_score"] == pytest.approx(0.5)

    feedback = store.feedback(
        str(replaced["new_id"]), helpful=True, restrict_to_run="root_old"
    )
    assert feedback["ok"] is False
    assert feedback["error"] == "feedback_requires_injection"

    delayed_feedback = store.feedback(
        str(old["id"]), helpful=False, restrict_to_run="root_old"
    )
    assert delayed_feedback["ok"] is True
    items = _items(store)
    assert items[old["id"]]["trust_score"] == pytest.approx(0.42)
    assert items[replaced["new_id"]]["trust_score"] == pytest.approx(0.5)


def test_replace_proposal_is_stale_when_target_revision_changed(tmp_path: Path):
    store = _store(tmp_path)
    old = store.add("project", "retry limit is 3", proposal=False, source="test")
    proposal = store.replace(
        "project",
        str(old["id"]),
        "retry limit is 5",
        proposal=True,
        source="curator",
        source_run_id="root_a",
    )
    direct = store.replace(
        "project",
        str(old["id"]),
        "retry limit is 4",
        proposal=False,
        source="human",
    )

    result = store.apply(str(proposal["id"]))
    assert result == {
        "ok": False,
        "error": "stale_target",
        "proposal_id": proposal["id"],
        "target_id": old["id"],
    }
    items = _items(store)
    assert items[proposal["id"]]["status"] == "stale"
    assert items[direct["new_id"]]["status"] == "active"


def test_batch_replace_uses_same_revision_contract(tmp_path: Path):
    store = _store(tmp_path)
    old = store.add(
        "session",
        "cache TTL is one hour",
        proposal=False,
        source="test",
        scope_id="root_batch",
    )
    result = store.batch(
        "session",
        [
            {
                "action": "replace",
                "target": str(old["id"]),
                "content": "cache TTL is two hours",
            }
        ],
        proposal=False,
        source="test",
        scope_id="root_batch",
    )

    replacement = result["results"][0]
    assert replacement["old_id"] == old["id"]
    assert replacement["new_id"] != old["id"]
    items = _items(store)
    assert items[old["id"]]["status"] == "superseded"
    assert items[replacement["new_id"]]["generation"] == 2


def test_model_mutation_without_root_context_fails_closed(tmp_path: Path):
    store = _store(tmp_path)
    result = store.handle_tool_action(
        "add", scope="project", content="must not be persisted"
    )
    assert result["ok"] is False
    assert result["error"] == "missing_run_context"
    assert store.export_items() == []

    listed = store.handle_tool_action("list", scope="project")
    assert listed["ok"] is False
    assert listed["error"] == "missing_run_context"


def test_direct_replace_rolls_back_supersede_when_revision_insert_fails(tmp_path: Path):
    store = _store(tmp_path)
    old = store.add("project", "worker lease is 30 seconds", proposal=False, source="test")
    with store._connect() as conn:
        conn.execute(
            """
            CREATE TRIGGER fail_replacement_revision
            BEFORE INSERT ON memory_items
            WHEN NEW.action = 'replace' AND NEW.status = 'active'
            BEGIN
                SELECT RAISE(ABORT, 'injected revision failure');
            END
            """
        )

    result = store.replace(
        "project",
        str(old["id"]),
        "worker lease is 60 seconds",
        proposal=False,
        source="test",
    )

    assert result["ok"] is False
    items = _items(store)
    assert items[old["id"]]["status"] == "active"
    assert [item["content"] for item in items.values()] == ["worker lease is 30 seconds"]


def test_proposal_replace_rolls_back_supersede_when_revision_activation_fails(
    tmp_path: Path,
):
    store = _store(tmp_path)
    old = store.add("project", "worker lease is 30 seconds", proposal=False, source="test")
    proposal = store.replace(
        "project",
        str(old["id"]),
        "worker lease is 60 seconds",
        proposal=True,
        source="test",
    )
    with store._connect() as conn:
        conn.execute(
            f"""
            CREATE TRIGGER fail_proposal_revision_activation
            BEFORE UPDATE OF status ON memory_items
            WHEN OLD.id = {int(proposal["id"])} AND NEW.status = 'active'
            BEGIN
                SELECT RAISE(ABORT, 'injected proposal activation failure');
            END
            """
        )

    with pytest.raises(sqlite3.IntegrityError, match="injected proposal activation failure"):
        store.apply(str(proposal["id"]))

    items = _items(store)
    assert items[old["id"]]["status"] == "active"
    assert items[proposal["id"]]["status"] == "pending"


def test_snapshot_requires_explicit_root_before_recording_injections(tmp_path: Path):
    from src.trace import MissingRunContextError

    store = _store(tmp_path)
    item = store.add("project", "the repository uses ruff", proposal=False, source="test")

    with pytest.raises(MissingRunContextError, match="missing_run_context"):
        store.snapshot_for_prompt(record_usage=True)

    with store._connect() as conn:
        count = conn.execute(
            "SELECT COUNT(*) FROM memory_injections WHERE item_id = ?", (item["id"],)
        ).fetchone()[0]
    assert count == 0
