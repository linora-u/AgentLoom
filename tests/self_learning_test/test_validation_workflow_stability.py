"""Deterministic contracts for the real summary-model validation workflows."""

from __future__ import annotations

import importlib
import json
import sqlite3
from pathlib import Path

import pytest
import yaml

from src.extensions.self_learning.memory_store import MemoryStore
from src.trace import bind_root_run

_REPO_ROOT = Path(__file__).resolve().parents[2]
_APP_ROOT = _REPO_ROOT / "applications" / "memory_feature_validation"


def _workflow(name: str) -> dict:
    return yaml.safe_load((_APP_ROOT / "workflows" / name).read_text(encoding="utf-8"))


def test_capacity_probe_uses_memory_api_and_proves_atomic_rollback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.syspath_prepend(str(_REPO_ROOT))
    monkeypatch.setenv("AGENTLOOM_SELF_LEARNING_ROOT", str(tmp_path / ".agentloom"))
    import src.tools.self_learning.memory_tool as memory_tool_module

    validation_probes = importlib.import_module(
        "applications.memory_feature_validation.agent_tools.validation_probes"
    )

    monkeypatch.setattr(memory_tool_module, "_capacity_failures", {})
    with bind_root_run("capacity-probe-root"):
        result = json.loads(validation_probes.validation_capacity_atomic_batch())

    assert result == {
        "third_add_error": "capacity_exceeded",
        "batch_ok": True,
        "failed_batch_error": "capacity_exceeded",
        "rollback_verified": True,
    }

    store = MemoryStore()
    with sqlite3.connect(store.db_path) as conn:
        rows = conn.execute(
            "SELECT content, status FROM memory_items "
            "WHERE scope_type = 'session' AND scope_id = ? ORDER BY id",
            ("capacity-probe-root",),
        ).fetchall()

    by_prefix = {
        prefix: [row for row in rows if str(row[0]).startswith(prefix)]
        for prefix in ("note-1:", "note-2:", "note-3-compact:", "must-not-commit:")
    }
    assert len(by_prefix["note-1:"][0][0]) == 1400
    assert by_prefix["note-1:"][0][1] == "removed"
    assert len(by_prefix["note-2:"][0][0]) == 1400
    assert by_prefix["note-2:"][0][1] == "active"
    assert by_prefix["note-3-compact:"][0][1] == "active"
    assert by_prefix["must-not-commit:"] == []


def test_capacity_workflow_is_one_composite_production_probe() -> None:
    workflow = _workflow("mem_capacity_agent.yaml")

    assert workflow["tool_call_type"] == "code_act"
    assert workflow["max_steps"] == 4
    assert workflow["toolsets"] == []
    assert workflow["tools"] == [
        {
            "name": "validation_capacity_atomic_batch",
            "module": "applications.memory_feature_validation.agent_tools.validation_probes",
            "function": "validation_capacity_atomic_batch",
        }
    ]
    body = workflow["workflow"]
    assert body.count("validation_capacity_atomic_batch()") == 1
    assert "final_answer(result)" in body
    assert "memory(action=" not in body


def test_root_attribution_workflow_keeps_worker_in_one_supervisor_code_step() -> None:
    workflow = _workflow("mem_worker_notes_agent.yaml")

    assert workflow["tool_call_type"] == "code_act"
    assert workflow["max_steps"] == 4
    assert workflow["toolsets"] == []
    assert workflow["worker_agents"] == [
        {
            "path": "applications/memory_feature_validation/workflows/worker_agents/note_taker.yaml"
        }
    ]
    body = workflow["workflow"]
    assert body.count('note_taker(query="record your assigned note")') == 1
    assert body.count('action="add"') == 1
    assert "supervisor_note_ok" in body
    assert body.count("final_answer(") == 1
