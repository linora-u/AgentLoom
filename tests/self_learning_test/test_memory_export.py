"""loom memory export: full-corpus dump with trust metadata."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from click.testing import CliRunner

from src.extensions.self_learning.memory_store import MemoryStore


@pytest.fixture()
def seeded_store(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> MemoryStore:
    monkeypatch.setenv("AGENTLOOM_SELF_LEARNING_ROOT", str(tmp_path / ".agentloom"))
    store = MemoryStore()
    store.add("project", "active project fact", proposal=False, source="cli")
    store.add("project", "pending proposal fact", proposal=True, source="model_tool")
    removed = store.add("project", "fact to remove", proposal=False, source="cli")
    store.remove("project", str(removed["id"]), proposal=False)
    store.add("session", "session note", proposal=False, source="test", scope_id="run_x")
    store.archive_session_notes("run_x")
    return store


def test_export_items_returns_all_statuses(seeded_store):
    items = seeded_store.export_items()
    statuses = {item["status"] for item in items}
    assert {"active", "pending", "removed", "archived"} <= statuses


def test_export_items_scope_filter(seeded_store):
    items = seeded_store.export_items(scope="session", scope_id="run_x")
    assert len(items) == 1
    assert items[0]["scope_type"] == "session"


def test_export_json_payload_shape(seeded_store, monkeypatch):
    from src.__main__ import memory_export

    runner = CliRunner()
    result = runner.invoke(memory_export, ["--include-events"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert set(payload) >= {"exported_at", "db_path", "stats", "items", "injections"}
    assert any(item["status"] == "pending" for item in payload["items"])


def test_export_markdown_shows_pending_and_trust(seeded_store):
    from src.__main__ import memory_export

    runner = CliRunner()
    result = runner.invoke(memory_export, ["--format", "markdown"])
    assert result.exit_code == 0, result.output
    assert "# AgentLoom Memory Export" in result.output
    assert "## project:project" in result.output
    assert "(pending/add, trust=0.50" in result.output
    assert "budget" in result.output


def test_export_to_file(seeded_store, tmp_path):
    from src.__main__ import memory_export

    out_file = tmp_path / "memory_dump.json"
    runner = CliRunner()
    result = runner.invoke(memory_export, ["--out", str(out_file)])
    assert result.exit_code == 0, result.output
    payload = json.loads(out_file.read_text(encoding="utf-8"))
    assert payload["items"]
    assert "Exported" in result.output
