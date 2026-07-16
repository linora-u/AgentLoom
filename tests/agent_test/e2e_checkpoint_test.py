#!/usr/bin/env python3
"""
End-to-end checkpoint/resume manual verification script.

Usage:
    # Phase 1: Run and self-interrupt after 60s
    .venv/bin/python tests/e2e_checkpoint_test.py interrupt

    # Phase 2: Check saved checkpoints
    .venv/bin/python tests/e2e_checkpoint_test.py check

    # Phase 3: Resume from checkpoint
    .venv/bin/python tests/e2e_checkpoint_test.py resume <task_id>
"""

from __future__ import annotations

import os
import signal
import sys
import threading
import time

project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, project_root)
os.chdir(project_root)


YAML_PATH = "applications/test_demo/workflows/test_default_logger_fallback_multi_supervisor.yaml"
INTERRUPT_DELAY = 60  # seconds


def phase_interrupt():
    """Run agent, auto-SIGINT after INTERRUPT_DELAY seconds."""
    print(f"[E2E] Phase 1: Run + self-interrupt after {INTERRUPT_DELAY}s")
    print(f"[E2E] YAML: {YAML_PATH}")

    def _auto_sigint():
        time.sleep(INTERRUPT_DELAY)
        print(f"\n[E2E] >>> Sending SIGINT to self (PID {os.getpid()}) <<<\n", flush=True)
        os.kill(os.getpid(), signal.SIGINT)

    t = threading.Thread(target=_auto_sigint, daemon=True)
    t.start()

    from src.runner import run_app
    try:
        result = run_app(YAML_PATH)
        print(f"[E2E] Completed normally (task finished before interrupt): {str(result)[:200]}")
    except (KeyboardInterrupt, SystemExit):
        print("\n[E2E] >>> INTERRUPTED — checkpoint should be saved <<<", flush=True)
    except RuntimeError as e:
        if "KeyboardInterrupt" in str(e):
            print(f"\n[E2E] >>> INTERRUPTED (RuntimeError wrapper) <<<", flush=True)
        else:
            print(f"[E2E] RuntimeError: {e}")

    print("[E2E] Phase 1 done. Now run: .venv/bin/python tests/e2e_checkpoint_test.py check")


def phase_check():
    """List all saved checkpoints."""
    print("[E2E] Phase 2: Checking saved checkpoints...")
    from src.lib.config import C
    from src.lib.checkpoint.checkpoint_manager import list_all_tasks
    from src.lib.runtime import resolve_runtime_home

    checkpoints_root = resolve_runtime_home(C.raw, agent_root=C.agent_root).root_dir / "checkpoints"
    tasks = list_all_tasks(checkpoints_root=checkpoints_root)
    if not tasks:
        print("[E2E] No checkpoints found!")
        return

    print(f"[E2E] Found {len(tasks)} checkpoint(s):")
    for t in tasks:
        icon = {"interrupted": "⏸", "failed": "❌", "running": "🔄", "completed": "✅"}.get(t["status"], "?")
        print(f"  {icon}  {t['task_id']}  [{t['agent_name']}]  {t['status']}")

    # Also check checkpoint.json and heartbeat
    import json
    from pathlib import Path
    for t in tasks:
        base = Path(t["checkpoint_dir"])
        ckpt = base / "checkpoint.json"
        hb = base / "heartbeat.json"
        if ckpt.exists():
            data = json.loads(ckpt.read_text())
            print(f"  [checkpoint] steps={data.get('step_count', '?')} status={data.get('status', '?')}")
        else:
            print(f"  [checkpoint] NOT FOUND (agent was killed too fast?)")
        if hb.exists():
            data = json.loads(hb.read_text())
            print(f"  [heartbeat]  pid={data.get('pid')} status={data.get('status')}")

    print(f"\n[E2E] To resume: .venv/bin/python tests/e2e_checkpoint_test.py resume {tasks[0]['task_id']}")


def phase_resume(task_id: str):
    """Resume from a checkpoint."""
    print(f"[E2E] Phase 3: Resuming task {task_id}")
    from src.runner import run_app
    try:
        result = run_app(YAML_PATH, resume_task_id=task_id)
        print(f"[E2E] >>> RESUMED and completed successfully <<<")
        print(f"[E2E] Result: {str(result)[:500]}")
    except Exception as e:
        print(f"[E2E] Resume failed: {type(e).__name__}: {e}")


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)

    phase = sys.argv[1]
    if phase == "interrupt":
        phase_interrupt()
    elif phase == "check":
        phase_check()
    elif phase == "resume":
        if len(sys.argv) < 3:
            print("Usage: ... resume <task_id>")
            sys.exit(1)
        phase_resume(sys.argv[2])
    else:
        print(f"Unknown phase: {phase}")
        sys.exit(1)


if __name__ == "__main__":
    main()
