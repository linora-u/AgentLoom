"""Public CLI/export contract for active memory and approval audit rows."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from click.testing import CliRunner

from src.extensions.self_learning.persistence.memory_store import MemoryStore


@pytest.fixture()
def seeded_store(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> MemoryStore:
    runtime_root = tmp_path / ".agentloom"
    monkeypatch.setattr(
        "src.extensions.self_learning.paths._runtime_config_section",
        lambda: {"root_dir": str(runtime_root)},
    )
    config = {
        "application_id": "export_app",
        "self_learning": {
            "review": {
                "enabled": True,
                "application": {"review_model": "summary"},
                "project": {"review_model": "summary"},
            }
        },
    }
    store = MemoryStore(agent_config=config)
    store.add(
        "project",
        "active project fact",
        memory_key="export:active-project-fact",
    )
    from src.extensions.self_learning.persistence.review_engine import ReviewEngine

    ReviewEngine(store.db_path).review(
        "project",
        "project",
        [
            {
                "kind": "fact",
                "memory_key": "export:pending-project-fact",
                "payload": {"text": "pending exact write"},
                "approval": "manual",
                "provenance": [{"root_run_id": "export-root"}],
                "source_run_ids": ["export-root"],
            }
        ],
        source_runs=[("export-root", "export_app")],
    )
    return store


def test_export_items_returns_active_only(seeded_store: MemoryStore) -> None:
    assert [item["content"] for item in seeded_store.export_items()] == [
        "active project fact"
    ]
    assert [json.loads(item["payload_json"])["text"] for item in seeded_store.list_pending()] == [
        "pending exact write"
    ]
    assert seeded_store.export_items()[0]["state"] == "active_confirmed"
    assert seeded_store.export_items()[0]["kind"] == "fact"


def test_export_json_payload_separates_active_and_pending(seeded_store: MemoryStore) -> None:
    from src.__main__ import memory_export

    result = CliRunner().invoke(memory_export, [])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert set(payload) == {
        "exported_at",
        "db_path",
        "stats",
        "items",
        "pending_writes",
    }
    assert [item["content"] for item in payload["items"]] == ["active project fact"]
    assert payload["items"][0]["scope_type"] == "project"
    assert payload["items"][0]["state"] == "active_confirmed"
    assert payload["pending_writes"][0]["state"] == "pending_pre_review"
    assert json.loads(payload["pending_writes"][0]["payload_json"]) == {
        "text": "pending exact write"
    }


def test_export_markdown_contains_only_active_memory(seeded_store: MemoryStore) -> None:
    from src.__main__ import memory_export

    result = CliRunner().invoke(memory_export, ["--format", "markdown"])
    assert result.exit_code == 0, result.output
    assert "# AgentLoom Memory Export" in result.output
    assert "active project fact" in result.output
    assert "pending exact write" not in result.output


def test_export_to_file(seeded_store: MemoryStore, tmp_path: Path) -> None:
    from src.__main__ import memory_export

    out_file = tmp_path / "memory_dump.json"
    result = CliRunner().invoke(memory_export, ["--out", str(out_file)])
    assert result.exit_code == 0, result.output
    payload = json.loads(out_file.read_text(encoding="utf-8"))
    assert payload["items"]
    assert payload["pending_writes"]
    assert "Exported" in result.output
