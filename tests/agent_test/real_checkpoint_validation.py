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
import shutil
import signal
import subprocess
import time
from pathlib import Path
from textwrap import dedent


ROOT = Path(__file__).resolve().parents[2]
PYTHON = Path("/Users/bytedance/code/data_clear/AgentLoom/.venv/bin/python")
YAML_PATH = "applications/test_demo/workflows/test_checkpoint_complex_supervisor.yaml"
APP_NAME = "test_checkpoint_complex_supervisor"
WORK_DIR = Path("/tmp/agentloom_ckpt_complex")


def _env() -> dict[str, str]:
    env = os.environ.copy()
    env["PYTHONPATH"] = str(ROOT)
    return env


def _run_code(resume_task_id: str | None = None) -> str:
    resume_arg = f", resume_task_id={resume_task_id!r}" if resume_task_id else ""
    return dedent(
        f"""
        from src.lib.config import C
        C.raw.setdefault("checkpoint", {{}})["cleanup_on_success"] = False
        C.raw.setdefault("lsp_servers", {{}})["enabled"] = False
        C.raw["skills"] = []
        from src.runner import run_app
        result = run_app({YAML_PATH!r}{resume_arg})
        print("RESULT_PREFIX=" + str(result)[:200].replace("\\n", " "))
        """
    )


def _clean_runtime() -> None:
    shutil.rmtree(ROOT / ".logs" / APP_NAME, ignore_errors=True)
    shutil.rmtree(WORK_DIR, ignore_errors=True)


def _latest_task_dir() -> Path | None:
    candidates = sorted((ROOT / ".logs" / APP_NAME).glob("*/checkpoints/task_*"))
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
                if ckpt.get("status") == "running" and ckpt.get("step_count", 0) >= 2:
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
    resume_text = _resume(task_id, log_dir / "main_interrupt_resume.log")
    after = _event_counts(task_dir)
    _assert_final_files()
    tree = json.loads((task_dir / "task_tree.json").read_text(encoding="utf-8"))
    return {
        "scenario": "main_interrupt",
        "task_id": task_id,
        "interrupt_returncode": returncode,
        "before_counts": before,
        "after_counts": after,
        "tree_status": tree.get("status"),
        "worker_calls": len(_worker_calls(task_dir)),
        "final_files_ok": True,
        "resume_result_seen": "CHECKPOINT COMPLEX COMPLETE" in resume_text,
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
    resume_text = _resume(task_id, log_dir / "worker_interrupt_resume.log")
    after = _event_counts(task_dir)
    _assert_final_files()
    tree = json.loads((task_dir / "task_tree.json").read_text(encoding="utf-8"))
    calls = _worker_calls(task_dir)
    final_worker = _worker_ckpt(task_dir)
    if len(calls) != 1:
        raise AssertionError(f"expected one worker call, got {len(calls)}")
    if calls[0].get("status") != "completed":
        raise AssertionError(f"worker call not completed after resume: {calls[0]}")
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
