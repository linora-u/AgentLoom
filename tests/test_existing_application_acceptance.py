"""Negative controls for the independent real-Application evidence verifier."""
import importlib.util
import json
import sqlite3
from pathlib import Path

import pytest

SPEC = importlib.util.spec_from_file_location(
    "existing_acceptance", Path(__file__).parent / "acceptance/existing_application_validation.py"
)
validation = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(validation)


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value))


def test_model_claim_and_model_code_are_not_tool_execution_evidence(tmp_path):
    write_json(tmp_path / "checkpoint.json", {
        "output": "write_file completed",
        "code_action": "write_file(file_path='result.txt', content='ok')",
    })
    with pytest.raises(AssertionError, match="tool completion evidence"):
        validation.assert_tools(tmp_path, {"write_file"})
    write_json(tmp_path / "checkpoint.json", {"tool_results": [{
        "call_id": "one", "tool_name": "write_file", "input": {}, "output": None,
        "status": "blocked", "error": {"kind": "policy"},
    }]})
    with pytest.raises(AssertionError, match="tool completion evidence"):
        validation.assert_tools(tmp_path, {"write_file"})


def test_codeact_accepts_only_durable_successful_tool_results(tmp_path):
    with sqlite3.connect(tmp_path / "self_learning.db") as connection:
        connection.execute("CREATE TABLE events (event_id, tool_name, status, input_json, output_json, root_run_id, run_id, event_type)")
        connection.execute("INSERT INTO events VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                           ("evt", "write_file", "completed", "{}", '{"result":"written"}', "root", "local", "tool_result"))
    evidence = validation.assert_tools(tmp_path, {"write_file"})
    assert evidence[0]["root_run_id"] == "root"
    assert evidence[0]["run_id"] == "local"
    assert evidence[0]["evidence_source"] == "session_recorder"


def test_worker_manifest_copy_does_not_double_count_calls(tmp_path):
    event = {"type": "worker_call_started", "agent_name": "real_worker", "call_index": 0,
             "task_input": "Mentioning fake_worker does not execute it"}
    for relative in ("checkpoints/app/task/task_events.jsonl", "runs/app/run/audit/task_events.jsonl"):
        path = tmp_path / relative
        path.parent.mkdir(parents=True)
        path.write_text(json.dumps(event) + "\n")
    assert len(validation.assert_workers(tmp_path, {"real_worker"}, count=1)) == 1
    with pytest.raises(AssertionError, match="fake_worker"):
        validation.assert_workers(tmp_path, {"fake_worker"})


def test_context_requires_matching_real_retrieval_output_and_source(tmp_path):
    runtime = tmp_path / "runtime"
    entry = {"ref": "ctx_one", "source": "tool_result:make_context_engine_text_payload", "kind": "text",
             "original": "TARGET_RECORD case=text verification_value=TEXT-CTX-7319"}
    write_json(runtime / "checkpoints/app/task/context_store/entries/ctx_one.json", entry)
    path = runtime / "checkpoints/app/task/context_store/events.jsonl"
    path.write_text(json.dumps({"type": "retrieved", "ref": "ctx_one", "query": "TARGET_RECORD", "offset": 0, "limit": 20, "retrieved_chars": 50}) + "\n")
    write_json(runtime / "checkpoint.json", {"tool_results": [{
        "call_id": "one", "tool_name": "loom_retrieve_context", "status": "completed",
        "input": {"ref": "ctx_wrong", "query": "TARGET_RECORD", "offset": 0, "limit": 20},
        "output": "TEXT-CTX-7319", "error": None,
    }]})
    with pytest.raises(AssertionError, match="correlated real retrieval"):
        validation.verify_context("text", tmp_path)
