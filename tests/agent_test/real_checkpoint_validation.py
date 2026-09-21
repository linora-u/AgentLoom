"""Manual real-LLM checkpoint validation.

This script intentionally calls the configured LLM. It is not a pytest test.

Usage:
    python tests/agent_test/real_checkpoint_validation.py --scenario all --workspace /new/evidence
    python tests/agent_test/real_checkpoint_validation.py --scenario all --prepare-only --workspace /new/evidence
    python tests/agent_test/real_checkpoint_validation.py --resume-state /new/evidence/main/resume_state.json

Every attempt is retained. Split prepare/resume commands must use the same
source revision and smolagents runtime contract.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from textwrap import dedent

ROOT = Path(__file__).resolve().parents[2]
PYTHON = Path(sys.executable)
YAML_PATH = "applications/test_demo/workflows/test_checkpoint_complex_supervisor.yaml"
APP_NAME = "test_checkpoint_complex_supervisor"
SESSION_ROOT: Path
WORK_DIR: Path
RUNTIME_ROOT: Path
PROBE_STEP = 9001
CONTEXT_NEEDLE = "old context survives the real resume"
SIDE_EFFECT_LOG: Path
EXPECTED_SIDE_EFFECTS = {
    "supervisor_setup": 1,
    "worker_step_1": 1,
    "worker_step_2": 1,
    "worker_step_3": 1,
    "supervisor_finalize": 1,
}


def _checkpoint_root() -> Path:
    return RUNTIME_ROOT / "checkpoints"


def _env() -> dict[str, str]:
    env = os.environ.copy()
    env["PYTHONPATH"] = str(ROOT)
    return env


def _run_code(resume_task_id: str | None = None) -> str:
    resume_arg = f", resume_task_id={resume_task_id!r}" if resume_task_id else ""
    return dedent(
        f"""
        from agentloom.configuration import C
        C.raw.setdefault("runtime", {{}})["root_dir"] = {str(RUNTIME_ROOT)!r}
        C.raw.setdefault("checkpoint", {{}})["cleanup_on_success"] = False
        C.raw.setdefault("lsp_servers", {{}})["enabled"] = False
        C.raw["skills"] = {{"paths": []}}
        from agentloom.application.runner import execute_app
        result = execute_app({YAML_PATH!r}{resume_arg}, file_logging=True)
        print("RESULT_PREFIX=" + result.output[:200].replace("\\n", " "))
        """
    )


def _configure_session(session_root: Path, *, existing: bool = False) -> None:
    """Never reuse or clear shared output. Only rewrite our own copied definitions."""
    global SESSION_ROOT, WORK_DIR, RUNTIME_ROOT, SIDE_EFFECT_LOG, YAML_PATH
    SESSION_ROOT = session_root.resolve()
    WORK_DIR = SESSION_ROOT / "work"
    RUNTIME_ROOT = SESSION_ROOT / "runtime"
    SIDE_EFFECT_LOG = SESSION_ROOT / "side_effects.log"
    if not existing:
        SESSION_ROOT.mkdir(parents=True, exist_ok=False)
        app_root = ROOT / "applications" / ("architecture_acceptance_" + hashlib.sha256(str(SESSION_ROOT).encode()).hexdigest()[:12])
        source_root = ROOT / "applications" / "test_demo"
        workers = app_root / "workflows" / "worker_agents"
        workers.mkdir(parents=True)
        for relative in (
            "workflows/test_checkpoint_complex_supervisor.yaml",
            "workflows/worker_agents/test_checkpoint_complex_worker.yaml",
        ):
            content = (source_root / relative).read_text(encoding="utf-8")
            content = content.replace("/tmp/agentloom_ckpt_complex", str(WORK_DIR))
            content = content.replace("/tmp/agentloom_ckpt_side_effects.log", str(SIDE_EFFECT_LOG))
            content = content.replace(
                "applications/test_demo/workflows/worker_agents/",
                str(workers) + "/",
            )
            (app_root / relative).write_text(content, encoding="utf-8")
    YAML_PATH = str(ROOT / "applications" / ("architecture_acceptance_" + hashlib.sha256(str(SESSION_ROOT).encode()).hexdigest()[:12]) / "workflows/test_checkpoint_complex_supervisor.yaml")


def _canonical_worker_query(workflow: str) -> str:
    """Read the single published structured-call query without inventing one."""
    if workflow.count("## Phase 2:") != 1 or workflow.count("## Phase 3:") != 1:
        raise ValueError("workflow must contain unique Phase 2 and Phase 3 boundaries")
    phase = workflow.split("## Phase 2:", 1)[1].split("## Phase 3:", 1)[0]
    if "artifact_worker" not in phase or "native structured tool" not in phase:
        raise ValueError("Phase 2 must require one native artifact_worker call")
    queries = re.findall(r"(?m)^\s*`([^`\n]+)`\s*$", phase)
    if len(queries) != 1 or not queries[0]:
        raise ValueError("Phase 2 must contain exactly one canonical Worker query")
    return queries[0]


def _configure_completed_worker_probe() -> None:
    """Keep the original task, adding a host-controlled pause after Worker commit."""
    import yaml

    path = Path(YAML_PATH)
    app_root = path.parents[1]
    shutil.copyfile(ROOT / "tests/acceptance/checkpoint_probe_tools.py", app_root / "checkpoint_probe_tools.py")
    config = yaml.safe_load(path.read_text())
    workflow = config["workflow"]
    start = workflow.index("## Phase 2:")
    end = workflow.index("## Phase 3:")
    query = _canonical_worker_query(workflow)
    phase = (
        "## Phase 2: Completed Worker handoff and intentional interruption\n"
        "Execute exactly two native structured tool calls. "
        "The host interrupts the first handoff, after the Worker has completed. "
        "On resume, continue from the committed Worker result in runtime state. "
        "Do not call the Worker again, invent, or reconstruct the result.\n"
        "1. Call `artifact_worker` with this exact `query`:\n"
        f"  `{query}`\n"
        "2. Call `record_checkpoint_worker_output` with "
        f"`workspace={str(SESSION_ROOT)}` and the exact Worker result as `output`.\n\n"
    )
    config["workflow"] = workflow[:start] + phase + workflow[end:]
    config["tools"].append({"name": "record_checkpoint_worker_output", "module": f"applications.{app_root.name}.checkpoint_probe_tools",
                            "function": "record_checkpoint_worker_output"})
    path.write_text(yaml.safe_dump(config, allow_unicode=True, sort_keys=False))


def _wait_for_completed_worker_interrupt_point(proc: subprocess.Popen, timeout: float = 240) -> Path:
    started = time.monotonic()
    while time.monotonic() - started < timeout:
        if proc.poll() is not None:
            raise RuntimeError(f"run exited before completed Worker handoff: {proc.returncode}")
        task_dir = _latest_task_dir()
        if task_dir is not None:
            calls = _worker_calls(task_dir)
            output_path = SESSION_ROOT / "worker_output_before.txt"
            if (len(calls) == 1 and calls[0].get("status") == "completed"
                    and output_path.is_file()
                    and _worker_handoff_output(output_path.read_text())
                    == _worker_final_output(_worker_ckpt(task_dir))):
                if (WORK_DIR / "final_manifest.txt").exists():
                    raise AssertionError("Supervisor finalized before the interruption")
                return task_dir
        time.sleep(0.2)
    raise TimeoutError("timed out waiting for completed Worker handoff")


def _runtime_memory_steps(checkpoint: dict) -> list[dict]:
    envelope = checkpoint.get("runtime_checkpoint")
    if not isinstance(envelope, dict):
        raise AssertionError("checkpoint lacks a runtime checkpoint envelope")
    if envelope.get("runtime_id") != "smolagents":
        raise AssertionError(
            f"checkpoint runtime is not smolagents: {envelope.get('runtime_id')!r}"
        )
    if envelope.get("state_schema_version") != 2:
        raise AssertionError(
            "checkpoint has unsupported smolagents state schema: "
            f"{envelope.get('state_schema_version')!r}"
        )
    if not isinstance(envelope.get("runtime_version"), str) or not envelope["runtime_version"]:
        raise AssertionError("smolagents checkpoint lacks runtime_version")
    audit_metadata = envelope.get("audit_metadata")
    if (
        not isinstance(audit_metadata, dict)
        or not isinstance(audit_metadata.get("model_adapter_id"), str)
        or not audit_metadata["model_adapter_id"]
    ):
        raise AssertionError("smolagents checkpoint lacks model_adapter_id")
    payload = envelope.get("payload")
    if not isinstance(payload, dict):
        raise AssertionError("smolagents checkpoint payload is not a mapping")
    steps = payload.get("memory_steps")
    if not isinstance(steps, list):
        raise AssertionError("smolagents checkpoint payload lacks memory_steps")
    if not isinstance(payload.get("canonical_model_items"), list):
        raise AssertionError(
            "smolagents checkpoint payload lacks canonical_model_items"
        )
    return steps


def _current_runtime_contract() -> dict[str, object]:
    from agentloom.runtimes.smolagents.runtime_adapter import (
        SmolagentsRuntimeAdapter,
    )
    from agentloom.configuration import C

    adapter_id = C.get_model_config("powerful", "adapter")
    if not isinstance(adapter_id, str) or not adapter_id:
        raise ValueError("powerful model profile lacks adapter")
    return {
        "runtime_id": SmolagentsRuntimeAdapter.runtime_id,
        "runtime_version": SmolagentsRuntimeAdapter.runtime_version,
        "state_schema_version": SmolagentsRuntimeAdapter.state_schema_version,
        "model_adapter_id": adapter_id,
    }


def _source_revision() -> str:
    return subprocess.check_output(
        ["git", "rev-parse", "HEAD"],
        cwd=ROOT,
        text=True,
    ).strip()


def _require_same_runtime_contract(state: dict) -> None:
    prepared_revision = state.get("source_revision")
    current_revision = _source_revision()
    if prepared_revision != current_revision:
        raise ValueError(
            "prepared checkpoint must resume from the same source revision: "
            f"{prepared_revision!r} != {current_revision!r}"
        )
    prepared_contract = state.get("runtime_contract")
    current_contract = _current_runtime_contract()
    if prepared_contract != current_contract:
        raise ValueError(
            "prepared checkpoint runtime contract does not match this checkout: "
            f"{prepared_contract!r} != {current_contract!r}"
        )
    task_dir = Path(state["task_dir"])
    checkpoint = json.loads(
        (task_dir / "checkpoint.json").read_text(encoding="utf-8")
    )
    envelope = checkpoint.get("runtime_checkpoint")
    if not isinstance(envelope, dict):
        raise ValueError("prepared checkpoint lacks a runtime checkpoint envelope")
    actual_contract = {
        "runtime_id": envelope.get("runtime_id"),
        "runtime_version": envelope.get("runtime_version"),
        "state_schema_version": envelope.get("state_schema_version"),
        "model_adapter_id": (
            envelope.get("audit_metadata") or {}
        ).get("model_adapter_id"),
    }
    if actual_contract != prepared_contract:
        raise ValueError(
            "prepared checkpoint envelope does not match its recorded runtime "
            f"contract: {actual_contract!r} != {prepared_contract!r}"
        )
    _runtime_memory_steps(checkpoint)


def _worker_usage(checkpoint: dict) -> dict[str, int]:
    fields = ("input_tokens", "output_tokens")
    return {field: sum((step.get("token_usage") or {}).get(field, 0)
                       for step in _runtime_memory_steps(checkpoint)) for field in fields}


def _worker_handoff_output(raw: str) -> str:
    try:
        envelope = json.loads(raw)
    except json.JSONDecodeError:
        if not raw:
            raise AssertionError("Worker handoff output is empty") from None
        return raw
    if (
        not isinstance(envelope, dict)
        or envelope.get("ok") is not True
        or envelope.get("status") != "completed"
        or not isinstance(envelope.get("output"), str)
    ):
        raise AssertionError(
            "Worker handoff is not a completed Agent-as-Tool result envelope"
        )
    return envelope["output"]


def _worker_final_output(checkpoint: dict):
    final = [step for step in _runtime_memory_steps(checkpoint)
             if step.get("_step_type") == "ActionStep" and step.get("is_final_answer") is True and step.get("error") is None]
    if len(final) != 1:
        raise AssertionError("Worker checkpoint lacks one successful final ActionStep output")
    calls = [
        call
        for call in final[0].get("tool_calls") or []
        if call.get("function", {}).get("name") == "final_answer"
    ]
    results = [
        result
        for result in final[0].get("tool_results") or []
        if result.get("tool_name") == "final_answer"
        and result.get("status") == "completed"
    ]
    if (
        len(calls) != 1
        or len(results) != 1
        or calls[0].get("id") != results[0].get("call_id")
        or not isinstance(results[0].get("output"), str)
    ):
        raise AssertionError(
            "Worker checkpoint lacks one correlated final_answer ToolCallRecord"
        )
    return results[0]["output"]


def _latest_task_dir() -> Path | None:
    candidates = sorted(_checkpoint_root().glob("*/task_*"))
    return candidates[-1] if candidates else None


def _events(task_dir: Path) -> list[dict]:
    path = task_dir / "task_events.jsonl"
    if not path.exists():
        return []
    events: list[dict] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            events.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return events


def _event_counts(task_dir: Path) -> dict[str, int]:
    counts: dict[str, int] = {}
    for event in _events(task_dir):
        event_type = event.get("type", "")
        counts[event_type] = counts.get(event_type, 0) + 1
    return counts


def _worker_calls(task_dir: Path) -> list[dict]:
    tree_path = task_dir / "task_tree.json"
    if not tree_path.exists():
        return []
    tree = json.loads(tree_path.read_text(encoding="utf-8"))
    calls = tree.get("workers", {}).get("artifact_worker", [])
    return calls if isinstance(calls, list) else [calls]


def _worker_ckpt(task_dir: Path, call_index: int = 0) -> dict:
    path = task_dir / "workers" / "artifact_worker" / "calls" / str(call_index) / "checkpoint.json"
    return json.loads(path.read_text(encoding="utf-8"))


def _seed_resume_probes(task_dir: Path) -> tuple[str, Path]:
    """Add old ContextRef and file-history state before the real resume."""

    from agentloom.runtime.checkpoint import CheckpointManager
    from agentloom.runtime.checkpoint.file_history import FileHistoryManager
    from agentloom.runtime.context_engine.engine import ContextEngine

    manager = CheckpointManager("resume-probe", checkpoint_dir=task_dir)
    context_engine = ContextEngine(
        task_dir / "context_store",
        storage=manager.directory_storage(task_dir.name, task_dir / "context_store"),
    )
    preview = context_engine.compress_tool_result(
        (CONTEXT_NEEDLE + "\n") * 800,
        tool_name="shell_tool",
        source="real-resume-probe",
    )
    if preview is None:
        raise AssertionError("failed to create ContextRef resume probe")
    match = re.search(r"ContextRef (ctx_[0-9a-f]{16})", preview)
    if match is None:
        raise AssertionError(f"ContextRef missing from preview: {preview[:200]}")
    ref = match.group(1)
    context_engine.close()

    probe_path = WORK_DIR / "resume_rewind_probe.txt"
    probe_path.parent.mkdir(parents=True, exist_ok=True)
    probe_path.write_text("before-resume-edit\n", encoding="utf-8")
    history = FileHistoryManager(
        task_dir / "file-history",
        storage=manager.directory_storage(task_dir.name, task_dir / "file-history"),
    )
    history.track_edit(str(probe_path), step_number=PROBE_STEP)
    probe_path.write_text("mutated-before-resume\n", encoding="utf-8")
    history.close()
    manager.close()
    return ref, probe_path


def _verify_resume_probes(task_dir: Path, ref: str, probe_path: Path) -> None:
    from agentloom.runtime.checkpoint import CheckpointManager
    from agentloom.runtime.checkpoint.file_history import FileHistoryManager
    from agentloom.runtime.context_engine.store import ContextStore

    manager = CheckpointManager("resume-probe", checkpoint_dir=task_dir)
    store = ContextStore(
        task_dir / "context_store",
        storage=manager.directory_storage(task_dir.name, task_dir / "context_store"),
    )
    retrieved = store.retrieve(ref, offset=0, limit=1)
    if retrieved is None or CONTEXT_NEEDLE not in retrieved:
        raise AssertionError(f"old ContextRef was not retrievable after resume: {ref}")
    store.close()

    history = FileHistoryManager(
        task_dir / "file-history",
        storage=manager.directory_storage(task_dir.name, task_dir / "file-history"),
    )
    if not history.restore_persisted_index():
        raise AssertionError("file-history index was not restored after resume")
    if probe_path.read_text(encoding="utf-8") != "mutated-before-resume\n":
        raise AssertionError("resume unexpectedly rewound the probe before validation")
    restored = history.rewind_to_step(PROBE_STEP)
    if os.path.abspath(probe_path) not in restored:
        raise AssertionError(f"file-history did not rewind probe: {restored}")
    if probe_path.read_text(encoding="utf-8") != "before-resume-edit\n":
        raise AssertionError("file-history rewind restored the wrong probe content")
    history.close()
    manager.close()


def _run_ids_for_task(task_id: str) -> set[str]:
    run_ids: set[str] = set()
    for manifest_path in (RUNTIME_ROOT / "runs").glob("**/manifest.json"):
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if manifest.get("task_id") == task_id and isinstance(manifest.get("run_id"), str):
            run_ids.add(manifest["run_id"])
    return run_ids


def _assert_final_files() -> None:
    expected = {
        "items/a.txt": "alpha=11",
        "items/b.txt": "beta=22",
        "items/c.txt": "gamma=33",
        "supervisor_manifest.txt": "manifest_status=ready",
        "worker_report.txt": "worker_status=complete",
        "final_manifest.txt": "checkpoint_complex=complete",
        "ledger.txt": "supervisor:complete",
    }
    missing: list[str] = []
    for rel_path, needle in expected.items():
        path = WORK_DIR / rel_path
        if not path.exists() or needle not in path.read_text(encoding="utf-8"):
            missing.append(f"{rel_path} missing {needle!r}")
    if missing:
        raise AssertionError("; ".join(missing))


def _assert_side_effects_executed_once() -> None:
    if not SIDE_EFFECT_LOG.is_file():
        raise AssertionError("append-only side-effect log was not created")
    counts = Counter(SIDE_EFFECT_LOG.read_text(encoding="utf-8").splitlines())
    if dict(counts) != EXPECTED_SIDE_EFFECTS:
        raise AssertionError(f"resume duplicated or skipped side effects: {dict(counts)}")


def _assert_interrupted_attempt(
    task_dir: Path,
    returncode: int,
    counts: dict[str, int],
) -> None:
    if returncode == 0:
        raise AssertionError("interrupt attempt unexpectedly exited successfully")
    if counts.get("run_started") != 1 or counts.get("run_resumed", 0) != 0:
        raise AssertionError(f"invalid pre-resume run events: {counts}")
    tree = json.loads((task_dir / "task_tree.json").read_text(encoding="utf-8"))
    if tree.get("status") == "completed":
        raise AssertionError("task completed before the requested interruption")
    completed_before_resume = [
        event
        for event in _events(task_dir)
        if event.get("type") == "task_status_changed"
        and event.get("status") == "completed"
    ]
    if completed_before_resume:
        raise AssertionError("completed task event exists before resume")


def _assert_completed_resume(
    task_dir: Path,
    resume_text: str,
    counts: dict[str, int],
    *,
    scenario: str,
) -> tuple[dict, list[dict]]:
    if "CHECKPOINT COMPLEX COMPLETE" not in resume_text:
        raise AssertionError("resume result did not contain the required final answer")
    if counts.get("run_started") != 1 or counts.get("run_resumed") != 1:
        raise AssertionError(f"resume attempt events are not one-to-one: {counts}")
    _assert_worker_resume_events(task_dir, counts, scenario=scenario)
    tree = json.loads((task_dir / "task_tree.json").read_text(encoding="utf-8"))
    if tree.get("status") != "completed":
        raise AssertionError(f"task tree did not complete: {tree.get('status')}")
    calls = _worker_calls(task_dir)
    if len(calls) != 1 or calls[0].get("status") != "completed":
        raise AssertionError(f"expected exactly one completed worker call: {calls}")
    _assert_side_effects_executed_once()
    return tree, calls


def _assert_worker_resume_events(
    task_dir: Path,
    counts: dict[str, int],
    *,
    scenario: str,
) -> None:
    worker_finished = [
        event
        for event in _events(task_dir)
        if event.get("type") == "worker_call_finished"
    ]
    expected_statuses = (
        ["interrupted", "completed"]
        if scenario == "worker"
        else ["completed"]
    )
    expected_resume_claims = 1 if scenario == "worker" else 0
    if (
        counts.get("worker_call_started") != 1
        or counts.get("worker_call_finished") != len(expected_statuses)
        or [event.get("status") for event in worker_finished]
        != expected_statuses
        or counts.get("worker_call_resume_claimed", 0)
        != expected_resume_claims
    ):
        raise AssertionError(f"worker call was duplicated or left unfinished: {counts}")


def _start_run(log_path: Path) -> subprocess.Popen:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_file = log_path.open("w", encoding="utf-8")
    return subprocess.Popen(
        [str(PYTHON), "-c", _run_code()],
        cwd=ROOT,
        env=_env(),
        stdout=log_file,
        stderr=subprocess.STDOUT,
        text=True,
    )


def _interrupt(proc: subprocess.Popen) -> int:
    proc.send_signal(signal.SIGINT)
    try:
        proc.wait(timeout=12)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(timeout=12)
    return proc.returncode


def _resume(task_id: str, log_path: Path) -> str:
    with log_path.open("w", encoding="utf-8") as log_file:
        proc = subprocess.run(
            [str(PYTHON), "-c", _run_code(resume_task_id=task_id)],
            cwd=ROOT,
            env=_env(),
            stdout=log_file,
            stderr=subprocess.STDOUT,
            text=True,
            timeout=360,
        )
    text = log_path.read_text(encoding="utf-8")
    if proc.returncode != 0:
        raise RuntimeError(text[-6000:])
    return text


def _wait_for_main_interrupt_point(proc: subprocess.Popen, timeout: float = 180) -> Path:
    started = time.monotonic()
    while time.monotonic() - started < timeout:
        if proc.poll() is not None:
            raise RuntimeError(f"run exited before main interrupt point: {proc.returncode}")
        task_dir = _latest_task_dir()
        if task_dir is not None and _main_setup_is_committed(task_dir):
            return task_dir
        time.sleep(0.2)
    raise TimeoutError("timed out waiting for main interrupt point")


def _main_setup_is_committed(task_dir: Path) -> bool:
    """Wait past destructive setup, including its durable ActionStep commit.

    A successful Todo step is too early, and setup files can precede the step
    checkpoint. Both would let resumed setup delete the later file-history probe.
    """
    expected = {
        "items/a.txt": "alpha=11\n",
        "items/b.txt": "beta=22\n",
        "items/c.txt": "gamma=33\n",
        "ledger.txt": "supervisor:setup\n",
        "supervisor_manifest.txt": "alpha=11\nbeta=22\ngamma=33\nmanifest_status=ready\n",
    }
    try:
        if Counter(SIDE_EFFECT_LOG.read_text(encoding="utf-8").splitlines()) != {"supervisor_setup": 1}:
            return False
        if any((WORK_DIR / name).read_text(encoding="utf-8") != content for name, content in expected.items()):
            return False
        checkpoint = json.loads((task_dir / "checkpoint.json").read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return False
    for step in _runtime_memory_steps(checkpoint):
        if (step.get("_step_type") != "ActionStep" or step.get("error") is not None
                or not step.get("observations") or (step.get("timing") or {}).get("end_time") is None):
            continue
        shell_calls = [
            call for call in step.get("tool_calls") or []
            if call.get("function", {}).get("name") == "shell_tool"
        ]
        if len(shell_calls) != 1:
            continue
        arguments = shell_calls[0].get("function", {}).get("arguments")
        command = arguments.get("command", "") if isinstance(arguments, dict) else ""
        # Code identifies the setup action; the committed observation, exact
        # files and independent ledger establish that its effects occurred.
        evidence = command + "\n" + step["observations"]
        setup_markers = ("supervisor_setup", "manifest_status=ready", str(SIDE_EFFECT_LOG),
                         *(str(WORK_DIR / name) for name in expected))
        if all(marker in evidence for marker in setup_markers):
            return _event_counts(task_dir).get("worker_call_started", 0) == 0
    return False


def _wait_for_worker_interrupt_point(proc: subprocess.Popen, timeout: float = 240) -> Path:
    started = time.monotonic()
    while time.monotonic() - started < timeout:
        if proc.poll() is not None:
            raise RuntimeError(f"run exited before worker interrupt point: {proc.returncode}")
        task_dir = _latest_task_dir()
        if task_dir is not None:
            counts = _event_counts(task_dir)
            worker_ckpt_path = task_dir / "workers" / "artifact_worker" / "calls" / "0" / "checkpoint.json"
            if counts.get("worker_call_started", 0) == 1 and worker_ckpt_path.exists():
                ckpt = json.loads(worker_ckpt_path.read_text(encoding="utf-8"))
                if ckpt.get("status") == "running" and ckpt.get("step_count", 0) >= 1:
                    return task_dir
        time.sleep(0.2)
    raise TimeoutError("timed out waiting for worker interrupt point")


def prepare(scenario: str) -> dict:
    """Retain an interrupted run for same-version resume."""
    from agentloom.configuration import C
    log_dir = SESSION_ROOT / "logs"
    started_at = datetime.now(UTC).isoformat()
    proc = _start_run(log_dir / "initial.log")
    try:
        wait = {"main": _wait_for_main_interrupt_point, "worker": _wait_for_worker_interrupt_point,
                "completed": _wait_for_completed_worker_interrupt_point}[scenario]
        task_dir = wait(proc)
        before_worker = _worker_ckpt(task_dir) if scenario in {"worker", "completed"} else None
        returncode = _interrupt(proc)
    finally:
        if proc.poll() is None:
            _interrupt(proc)
    before = _event_counts(task_dir)
    _assert_interrupted_attempt(task_dir, returncode, before)
    if scenario == "main" and not _main_setup_is_committed(task_dir):
        raise AssertionError("main interruption did not retain committed setup before Worker start")
    old_run_ids = _run_ids_for_task(task_dir.name)
    context_ref, probe_path = _seed_resume_probes(task_dir)
    state = {
        "scenario": scenario, "task_id": task_dir.name,
        "started_at": started_at,
        "model_type": "powerful", "model": C.get_model_config("powerful", "model"),
        "agent_runtime": "smolagents",
        "model_adapter": "configured",
        "max_steps": {"supervisor": 10, "worker": 8},
        "initial_timeout_seconds": 180 if scenario == "main" else 240,
        "resume_timeout_seconds": 360,
        "definition_sha256": {str(path): hashlib.sha256(path.read_bytes()).hexdigest()
                              for path in Path(YAML_PATH).parent.rglob("*.yaml")},
        "session_root": str(SESSION_ROOT), "task_dir": str(task_dir),
        "context_ref": context_ref, "probe_path": str(probe_path),
        "old_run_ids": sorted(old_run_ids), "before_counts": before,
        "before_worker_step": before_worker.get("step_count") if before_worker else None,
        "interrupt_returncode": returncode,
        "framework_tree": subprocess.check_output(["git", "rev-parse", "HEAD:src"], cwd=ROOT, text=True).strip(),
        "workflow": YAML_PATH,
        "source_revision": _source_revision(),
        "runtime_contract": _current_runtime_contract(),
        "prepared_at": datetime.now(UTC).isoformat(),
    }
    if scenario == "completed":
        before_path = SESSION_ROOT / "worker_output_before.txt"
        output = before_path.read_text()
        if _worker_final_output(before_worker) != _worker_handoff_output(output):
            raise AssertionError("Observed initial Worker return differs from committed final ActionStep")
        state["completed_worker"] = {
            "output": output, "usage": _worker_usage(before_worker),
            "checkpoint": before_worker,
            "call_count": len(_worker_calls(task_dir)),
            "side_effect_counts": dict(Counter(SIDE_EFFECT_LOG.read_text().splitlines())),
        }
    (SESSION_ROOT / "resume_state.json").write_text(json.dumps(state, indent=2) + "\n")
    return state


def resume_prepared(state: dict) -> dict:
    _require_same_runtime_contract(state)
    if Path(state["workflow"]).resolve() != Path(YAML_PATH).resolve():
        raise ValueError(
            "prepared checkpoint workflow does not match this same-version session"
        )
    task_dir = Path(state["task_dir"])
    if state["scenario"] == "completed":
        (SESSION_ROOT / "handoff_release").touch(exist_ok=False)
    resume_text = _resume(state["task_id"], SESSION_ROOT / "logs" / "resume.log")
    after = _event_counts(task_dir)
    new_ids = _run_ids_for_task(state["task_id"])
    old_ids = set(state["old_run_ids"])
    if len(old_ids) != 1 or len(new_ids) != 2 or not old_ids < new_ids:
        raise AssertionError(f"resume must preserve task and create new run: {old_ids} -> {new_ids}")
    _verify_resume_probes(task_dir, state["context_ref"], Path(state["probe_path"]))
    _assert_final_files()
    tree, calls = _assert_completed_resume(
        task_dir,
        resume_text,
        after,
        scenario=state["scenario"],
    )
    if state["scenario"] == "worker":
        if _worker_ckpt(task_dir).get("step_count", 0) <= state["before_worker_step"]:
            raise AssertionError("worker did not advance saved memory")
    if state["scenario"] == "completed":
        before = state["completed_worker"]
        after_worker = _worker_ckpt(task_dir)
        after_output = (SESSION_ROOT / "worker_output_after.txt").read_text()
        if _worker_handoff_output(after_output) != _worker_handoff_output(
            before["output"]
        ):
            raise AssertionError("Cached Worker output differs from its real pre-interruption return")
        if (
            len(calls) != before["call_count"]
            or after.get("worker_call_started") != 1
            or after.get("worker_call_finished") != 1
            or after.get("worker_call_cached_result_claimed", 0) != 0
        ):
            raise AssertionError(
                "Completed Worker was called again instead of replaying committed runtime state"
            )
        if after_worker != before["checkpoint"] or _worker_usage(after_worker) != before["usage"]:
            raise AssertionError("Completed Worker checkpoint or model usage changed on replay")
        if sum(before["usage"].values()) <= 0:
            raise AssertionError("Completed Worker has no real model usage")
    manifests = [json.loads(path.read_text()) for path in (RUNTIME_ROOT / "runs").glob("**/manifest.json")]
    if not any(item.get("status") == "completed" for item in manifests):
        raise AssertionError("resume has no completed canonical Run manifest")
    return {**state, "status": "passed", "new_run_ids": sorted(new_ids), "after_counts": after,
            "resumed_revision": _source_revision(),
            "ended_at": datetime.now(UTC).isoformat(),
            "assertions": ["task preserved", "new run", "one Worker call", "side effects exactly once",
                           "final artifact oracle", "old ContextRef retrieved", "file history restored"]
                          + (["completed Worker not called again", "exact replayed output", "Worker usage unchanged"]
                             if state["scenario"] == "completed" else [])}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--scenario", choices=["main", "worker", "completed", "all"], default="all")
    parser.add_argument("--workspace", type=Path, help="New evidence directory; existing directories are never deleted")
    parser.add_argument("--prepare-only", action="store_true", help="Leave interrupted material for same-version resume")
    parser.add_argument("--resume-state", type=Path, help="Resume state prepared by this exact checkout/runtime version")
    args = parser.parse_args()
    reports = []
    if args.resume_state:
        state = json.loads(args.resume_state.read_text())
        _configure_session(Path(state["session_root"]), existing=True)
        reports.append(resume_prepared(state))
    else:
        root = args.workspace or Path(tempfile.mkdtemp(prefix="agentloom-checkpoint-")) / "cases"
        for scenario in (["main", "worker", "completed"] if args.scenario == "all" else [args.scenario]):
            _configure_session(root / scenario)
            if scenario == "completed":
                _configure_completed_worker_probe()
            try:
                state = prepare(scenario)
                reports.append(state if args.prepare_only else resume_prepared(state))
            except BaseException as exc:
                (SESSION_ROOT / "failure.json").write_text(json.dumps({"scenario": scenario, "error": str(exc),
                    "type": type(exc).__name__, "status": "failed"}, indent=2) + "\n")
                raise
    for report in reports:
        (Path(report["session_root"]) / "report.json").write_text(json.dumps(report, indent=2) + "\n")
    display = []
    for report in reports:
        compact = dict(report)
        if "completed_worker" in compact:
            compact["completed_worker"] = {key: value for key, value in compact["completed_worker"].items() if key != "checkpoint"}
        display.append(compact)
    print(json.dumps(display, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
