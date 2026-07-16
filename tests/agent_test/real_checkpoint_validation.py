"""Manual real-LLM checkpoint validation.

This script intentionally calls the configured LLM. It is not a pytest test.

Usage:
    PYTHONPATH=/Users/bytedance/code/data_clear/AgentLoom-checkpoint \
    /Users/bytedance/code/data_clear/AgentLoom/.venv/bin/python \
      tests/agent_test/real_checkpoint_validation.py --scenario all
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import signal
import subprocess
import time
from collections import Counter
from pathlib import Path
from textwrap import dedent

ROOT = Path(__file__).resolve().parents[2]
PYTHON = Path("/Users/bytedance/code/data_clear/AgentLoom/.venv/bin/python")
YAML_PATH = "applications/test_demo/workflows/test_checkpoint_complex_supervisor.yaml"
APP_NAME = "test_checkpoint_complex_supervisor"
WORK_DIR = Path("/tmp/agentloom_ckpt_complex")
RUNTIME_ROOT = Path("/tmp/agentloom_real_checkpoint_runtime/.agentloom")
PROBE_STEP = 9001
CONTEXT_NEEDLE = "old context survives the real resume"
SIDE_EFFECT_LOG = Path("/tmp/agentloom_ckpt_side_effects.log")
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
        from src.lib.config import C
        C.raw.setdefault("runtime", {{}})["root_dir"] = {str(RUNTIME_ROOT)!r}
        C.raw.setdefault("checkpoint", {{}})["cleanup_on_success"] = False
        C.raw.setdefault("lsp_servers", {{}})["enabled"] = False
        C.raw["skills"] = []
        from src.runner import run_app
        result = run_app({YAML_PATH!r}{resume_arg})
        print("RESULT_PREFIX=" + str(result)[:200].replace("\\n", " "))
        """
    )


def _clean_runtime() -> None:
    shutil.rmtree(RUNTIME_ROOT.parent, ignore_errors=True)
    shutil.rmtree(WORK_DIR, ignore_errors=True)
    SIDE_EFFECT_LOG.unlink(missing_ok=True)


def _latest_task_dir() -> Path | None:
    candidates = sorted((_checkpoint_root() / "test_demo").glob("task_*"))
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

    from src.lib.checkpoint import CheckpointManager
    from src.lib.checkpoint.file_history import FileHistoryManager
    from src.lib.context_engine.engine import ContextEngine

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
    from src.lib.checkpoint import CheckpointManager
    from src.lib.checkpoint.file_history import FileHistoryManager
    from src.lib.context_engine.store import ContextStore

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
) -> tuple[dict, list[dict]]:
    if "CHECKPOINT COMPLEX COMPLETE" not in resume_text:
        raise AssertionError("resume result did not contain the required final answer")
    if counts.get("run_started") != 1 or counts.get("run_resumed") != 1:
        raise AssertionError(f"resume attempt events are not one-to-one: {counts}")
    if counts.get("worker_call_started") != 1 or counts.get("worker_call_finished") != 1:
        raise AssertionError(f"worker call was duplicated or left unfinished: {counts}")
    tree = json.loads((task_dir / "task_tree.json").read_text(encoding="utf-8"))
    if tree.get("status") != "completed":
        raise AssertionError(f"task tree did not complete: {tree.get('status')}")
    calls = _worker_calls(task_dir)
    if len(calls) != 1 or calls[0].get("status") != "completed":
        raise AssertionError(f"expected exactly one completed worker call: {calls}")
    _assert_side_effects_executed_once()
    return tree, calls


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
        if task_dir is not None:
            counts = _event_counts(task_dir)
            supervisor_ckpt = task_dir / "checkpoint.json"
            step_count = 0
            if supervisor_ckpt.exists():
                step_count = json.loads(supervisor_ckpt.read_text(encoding="utf-8")).get("step_count", 0)
            if step_count >= 1 and counts.get("worker_call_started", 0) == 0:
                return task_dir
        time.sleep(0.2)
    raise TimeoutError("timed out waiting for main interrupt point")


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


def run_main_interrupt() -> dict:
    _clean_runtime()
    log_dir = Path("/tmp/agentloom_real_checkpoint_validation")
    proc = _start_run(log_dir / "main_interrupt_initial.log")
    task_dir = _wait_for_main_interrupt_point(proc)
    task_id = task_dir.name
    returncode = _interrupt(proc)
    before = _event_counts(task_dir)
    _assert_interrupted_attempt(task_dir, returncode, before)
    old_run_ids = _run_ids_for_task(task_id)
    context_ref, probe_path = _seed_resume_probes(task_dir)
    resume_text = _resume(task_id, log_dir / "main_interrupt_resume.log")
    if "Restored file history:" not in resume_text:
        raise AssertionError("runner did not restore persisted file-history during resume")
    after = _event_counts(task_dir)
    new_run_ids = _run_ids_for_task(task_id)
    if len(old_run_ids) != 1 or len(new_run_ids) != 2 or not old_run_ids < new_run_ids:
        raise AssertionError(
            f"resume must keep task_id and create one new run_id: {old_run_ids} -> {new_run_ids}"
        )
    _verify_resume_probes(task_dir, context_ref, probe_path)
    _assert_final_files()
    tree, calls = _assert_completed_resume(task_dir, resume_text, after)
    return {
        "scenario": "main_interrupt",
        "task_id": task_id,
        "interrupt_returncode": returncode,
        "before_counts": before,
        "after_counts": after,
        "tree_status": tree.get("status"),
        "worker_calls": len(calls),
        "final_files_ok": True,
        "resume_result_seen": "CHECKPOINT COMPLEX COMPLETE" in resume_text,
        "new_run_on_same_task": True,
        "old_context_ref_retrieved": True,
        "file_history_rewound": True,
    }


def run_worker_interrupt() -> dict:
    _clean_runtime()
    log_dir = Path("/tmp/agentloom_real_checkpoint_validation")
    proc = _start_run(log_dir / "worker_interrupt_initial.log")
    task_dir = _wait_for_worker_interrupt_point(proc)
    task_id = task_dir.name
    before_worker = _worker_ckpt(task_dir)
    returncode = _interrupt(proc)
    before = _event_counts(task_dir)
    _assert_interrupted_attempt(task_dir, returncode, before)
    old_run_ids = _run_ids_for_task(task_id)
    context_ref, probe_path = _seed_resume_probes(task_dir)
    resume_text = _resume(task_id, log_dir / "worker_interrupt_resume.log")
    if "Restored file history:" not in resume_text:
        raise AssertionError("runner did not restore persisted file-history during resume")
    after = _event_counts(task_dir)
    new_run_ids = _run_ids_for_task(task_id)
    if len(old_run_ids) != 1 or len(new_run_ids) != 2 or not old_run_ids < new_run_ids:
        raise AssertionError(
            f"resume must keep task_id and create one new run_id: {old_run_ids} -> {new_run_ids}"
        )
    _verify_resume_probes(task_dir, context_ref, probe_path)
    _assert_final_files()
    tree, calls = _assert_completed_resume(task_dir, resume_text, after)
    final_worker = _worker_ckpt(task_dir)
    if before_worker.get("step_count", 0) < 1:
        raise AssertionError(f"worker was not checkpointed before interrupt: {before_worker}")
    if final_worker.get("step_count", 0) <= before_worker.get("step_count", 0):
        raise AssertionError("resumed worker did not advance beyond its saved memory")
    if "Restored worker artifact_worker #0" not in resume_text:
        raise AssertionError("worker restore log not found")
    return {
        "scenario": "worker_interrupt",
        "task_id": task_id,
        "interrupt_returncode": returncode,
        "before_counts": before,
        "after_counts": after,
        "tree_status": tree.get("status"),
        "worker_calls": len(calls),
        "worker_status": calls[0].get("status"),
        "worker_step_count_before": before_worker.get("step_count"),
        "worker_step_count_after": final_worker.get("step_count"),
        "worker_memory_restored": "Restored worker artifact_worker #0" in resume_text,
        "final_files_ok": True,
        "new_run_on_same_task": True,
        "old_context_ref_retrieved": True,
        "file_history_rewound": True,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--scenario",
        choices=["main", "worker", "all"],
        default="all",
    )
    args = parser.parse_args()

    results = []
    if args.scenario in {"main", "all"}:
        results.append(run_main_interrupt())
    if args.scenario in {"worker", "all"}:
        results.append(run_worker_interrupt())

    print(json.dumps(results, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
