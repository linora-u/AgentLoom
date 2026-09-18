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


def test_model_claim_is_not_tool_execution_evidence(tmp_path):
    write_json(tmp_path / "checkpoint.json", {
        "output": "write_file completed",
    })
    with pytest.raises(AssertionError, match="tool completion evidence"):
        validation.assert_tools(tmp_path, {"write_file"})
    write_json(tmp_path / "checkpoint.json", {"tool_results": [{
        "call_id": "one", "tool_name": "write_file", "input": {}, "output": None,
        "status": "blocked", "error": {"kind": "policy"},
    }]})
    with pytest.raises(AssertionError, match="tool completion evidence"):
        validation.assert_tools(tmp_path, {"write_file"})


def test_tool_evidence_accepts_only_durable_successful_tool_results(tmp_path):
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


def test_checkpoint_handoff_captures_exact_returns_and_rejects_duplicate_writes(tmp_path):
    import time
    from concurrent.futures import ThreadPoolExecutor

    spec = importlib.util.spec_from_file_location(
        "checkpoint_probe", Path(__file__).parent / "acceptance/checkpoint_probe_tools.py"
    )
    probe = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(probe)
    exact = "A real Worker return with \"quotes\", a newline\nand Unicode: 验证"
    with ThreadPoolExecutor(max_workers=1) as pool:
        waiting = pool.submit(probe.record_checkpoint_worker_output, str(tmp_path), exact)
        deadline = time.monotonic() + 2
        before = tmp_path / "worker_output_before.txt"
        try:
            while (not before.exists() or before.read_text() != exact) and time.monotonic() < deadline:
                time.sleep(0.01)
            assert before.read_text() == exact
        finally:
            (tmp_path / "handoff_release").touch()
        assert "proceed" in waiting.result(timeout=2)
    probe.record_checkpoint_worker_output(str(tmp_path), exact)
    assert (tmp_path / "worker_output_after.txt").read_text() == exact
    with pytest.raises(FileExistsError):
        probe.record_checkpoint_worker_output(str(tmp_path), "duplicate")


@pytest.fixture
def checkpoint_helper():
    spec = importlib.util.spec_from_file_location(
        "checkpoint_helper", Path(__file__).parent / "agent_test/real_checkpoint_validation.py"
    )
    helper = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(helper)
    return helper


def test_completed_probe_uses_the_published_query_without_a_second_text_source(tmp_path, checkpoint_helper):
    import yaml

    helper = checkpoint_helper
    source = helper.ROOT / helper.YAML_PATH
    copied = tmp_path / "application/workflows/supervisor.yaml"
    copied.parent.mkdir(parents=True)
    config = yaml.safe_load(source.read_text())
    config["workflow"] = config["workflow"].replace("/tmp/agentloom_ckpt_complex", str(tmp_path / "distinct-workspace"))
    copied.write_text(yaml.safe_dump(config))
    expected = helper._canonical_worker_query(config["workflow"])
    helper.YAML_PATH = str(copied)
    helper.SESSION_ROOT = tmp_path / "evidence"
    helper._configure_completed_worker_probe()
    configured = yaml.safe_load(copied.read_text())["workflow"]
    assert helper._canonical_worker_query(configured) == expected
    assert "`record_checkpoint_worker_output`" in configured


@pytest.mark.parametrize("body", [
    "Call the Worker with a report query.",
    "Call `artifact_worker` through its native structured tool schema.\n  `one`\n  `two`",
    "Call `artifact_worker` with query assembled from earlier text.",
    "  `one`",
])
def test_completed_probe_rejects_missing_multiple_or_dynamic_queries(checkpoint_helper, body):
    with pytest.raises(ValueError):
        checkpoint_helper._canonical_worker_query(f"## Phase 2:\n{body}\n## Phase 3:\nVerify.")


def test_completed_probe_rejects_missing_phase_boundary(checkpoint_helper):
    with pytest.raises(ValueError, match="unique Phase 2"):
        checkpoint_helper._canonical_worker_query("No canonical phase boundaries.")


@pytest.fixture
def main_checkpoint_gate(tmp_path, monkeypatch):
    from types import SimpleNamespace

    spec = importlib.util.spec_from_file_location(
        "real_checkpoint_validation", Path(__file__).parent / "agent_test/real_checkpoint_validation.py"
    )
    checkpoint = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(checkpoint)
    checkpoint.WORK_DIR = tmp_path / "work"
    checkpoint.SIDE_EFFECT_LOG = tmp_path / "side_effects.log"
    task_dir = tmp_path / "runtime/checkpoints/app/task_one"
    task_dir.mkdir(parents=True)
    monkeypatch.setattr(checkpoint, "_latest_task_dir", lambda: task_dir)

    def wait():
        ticks = iter([0.0, 0.1, 0.2])
        with monkeypatch.context() as clock:
            clock.setattr(checkpoint.time, "monotonic", lambda: next(ticks))
            clock.setattr(checkpoint.time, "sleep", lambda _: None)
            return checkpoint._wait_for_main_interrupt_point(SimpleNamespace(poll=lambda: None), timeout=0.2)

    return checkpoint, task_dir, wait


def committed_action(
    tool_name,
    arguments,
    observations="Execution logs: setup completed",
):
    return {
        "_step_type": "ActionStep", "step_number": 1,
        "tool_calls": [{
            "id": "call_1",
            "type": "function",
            "function": {"name": tool_name, "arguments": arguments},
        }],
        "observations": observations, "error": None,
        "timing": {"start_time": 1, "end_time": 2, "duration": 1},
    }


def write_supervisor_setup(checkpoint):
    import yaml

    expected = {
        "items/a.txt": "alpha=11\n", "items/b.txt": "beta=22\n", "items/c.txt": "gamma=33\n",
        "ledger.txt": "supervisor:setup\n",
        "supervisor_manifest.txt": "alpha=11\nbeta=22\ngamma=33\nmanifest_status=ready\n",
    }
    for relative, content in expected.items():
        path = checkpoint.WORK_DIR / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
    checkpoint.SIDE_EFFECT_LOG.write_text("supervisor_setup\n")
    workflow_path = Path(__file__).parents[1] / "applications/test_demo/workflows/test_checkpoint_complex_supervisor.yaml"
    workflow = yaml.safe_load(workflow_path.read_text())["workflow"]
    command = workflow.split("Use shell_tool once to run this exact command:\n", 1)[1].split("\n\n", 1)[0].strip()
    command = command.replace("/tmp/agentloom_ckpt_complex", str(checkpoint.WORK_DIR))
    command = command.replace("/tmp/agentloom_ckpt_side_effects.log", str(checkpoint.SIDE_EFFECT_LOG))
    return committed_action("shell_tool", {"command": command})


@pytest.mark.parametrize("setup_on_disk", [False, True], ids=["early-todo", "setup-not-checkpointed"])
def test_main_interrupt_waits_for_setup_action_commit(main_checkpoint_gate, setup_on_disk):
    checkpoint, task_dir, wait = main_checkpoint_gate
    if setup_on_disk:
        write_supervisor_setup(checkpoint)
    todo = committed_action(
        "todo_write",
        {"items": [{"content": "supervisor_setup", "status": "in_progress"}]},
        "Todo saved",
    )
    write_json(task_dir / "checkpoint.json", {"step_count": 3, "memory_steps": [todo]})
    with pytest.raises(TimeoutError, match="main interrupt point"):
        wait()


def test_main_interrupt_accepts_committed_setup_before_worker(main_checkpoint_gate):
    checkpoint, task_dir, wait = main_checkpoint_gate
    setup = write_supervisor_setup(checkpoint)
    write_json(task_dir / "checkpoint.json", {"step_count": 3, "memory_steps": [setup]})
    assert wait() == task_dir


def test_main_interrupt_observes_files_then_waits_for_checkpoint_commit(main_checkpoint_gate, monkeypatch):
    from types import SimpleNamespace

    checkpoint, task_dir, _ = main_checkpoint_gate
    todo = committed_action("todo_write", {"items": []}, "Todo saved")
    write_json(task_dir / "checkpoint.json", {"step_count": 3, "memory_steps": [todo]})
    states = []

    def advance(_):
        if not states:
            states.append(write_supervisor_setup(checkpoint))
        elif len(states) == 1:
            write_json(task_dir / "checkpoint.json", {"step_count": 4, "memory_steps": [todo, states[0]]})
            states.append("committed")
        else:
            pytest.fail("committed setup was not accepted")

    ticks = iter([0.0, 0.1, 0.2, 0.3, 0.4])
    monkeypatch.setattr(checkpoint.time, "monotonic", lambda: next(ticks))
    monkeypatch.setattr(checkpoint.time, "sleep", advance)
    assert checkpoint._wait_for_main_interrupt_point(SimpleNamespace(poll=lambda: None), timeout=1) == task_dir
    assert states[-1] == "committed"


def test_main_prepare_rechecks_worker_race_before_seeding_probes(main_checkpoint_gate, monkeypatch):
    from types import SimpleNamespace

    checkpoint, task_dir, _ = main_checkpoint_gate
    setup = write_supervisor_setup(checkpoint)
    write_json(task_dir / "checkpoint.json", {"step_count": 3, "memory_steps": [setup]})
    write_json(task_dir / "task_tree.json", {"status": "interrupted"})
    events = task_dir / "task_events.jsonl"
    events.write_text(json.dumps({"type": "run_started"}) + "\n")
    checkpoint.SESSION_ROOT = checkpoint.WORK_DIR.parent
    monkeypatch.setattr(checkpoint, "_start_run", lambda _: SimpleNamespace(poll=lambda: 130))

    def ready(_):
        assert checkpoint._main_setup_is_committed(task_dir)
        return task_dir

    def interrupt(_):
        with events.open("a") as stream:
            stream.write(json.dumps({"type": "worker_call_started"}) + "\n")
        return 130

    monkeypatch.setattr(checkpoint, "_wait_for_main_interrupt_point", ready)
    monkeypatch.setattr(checkpoint, "_interrupt", interrupt)
    monkeypatch.setattr(checkpoint, "_seed_resume_probes", lambda _: pytest.fail("seeded probes after Worker start"))
    with pytest.raises(AssertionError, match="committed setup before Worker start"):
        checkpoint.prepare("main")


@pytest.mark.parametrize("defect", ["unobserved", "unfinished", "error", "no-tool-call", "worker-started", "missing-file", "wrong-manifest", "duplicate-setup"])
def test_main_interrupt_rejects_incomplete_or_late_setup(main_checkpoint_gate, defect):
    checkpoint, task_dir, wait = main_checkpoint_gate
    setup = write_supervisor_setup(checkpoint)
    if defect == "unobserved":
        setup["observations"] = None
    elif defect == "unfinished":
        setup["timing"]["end_time"] = None
    elif defect == "error":
        setup["error"] = {"type": "AgentExecutionError", "message": "shell failed"}
    elif defect == "no-tool-call":
        setup["tool_calls"] = []
    elif defect == "worker-started":
        (task_dir / "task_events.jsonl").write_text(json.dumps({"type": "worker_call_started"}) + "\n")
    elif defect == "missing-file":
        (checkpoint.WORK_DIR / "items/c.txt").unlink()
    elif defect == "wrong-manifest":
        (checkpoint.WORK_DIR / "supervisor_manifest.txt").write_text("manifest_status=ready\n")
    elif defect == "duplicate-setup":
        checkpoint.SIDE_EFFECT_LOG.write_text("supervisor_setup\nsupervisor_setup\n")
    write_json(task_dir / "checkpoint.json", {"step_count": 3, "memory_steps": [setup]})
    with pytest.raises(TimeoutError, match="main interrupt point"):
        wait()
