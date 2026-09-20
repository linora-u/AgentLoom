"""Run the existing on/auto/off Todo Applications against the configured real model.

No canned model responses. Each case has a fresh process, runtime and retained
checkpoint evidence. Run: python tests/acceptance/smol_todo_validation.py
--workspace /new/private/evidence. Process logs may contain private model data.
"""
from __future__ import annotations

import argparse
import json
import os
import signal
import subprocess
import sys
from pathlib import Path

from existing_application_validation import ROOT, dump, execute

CASES = {
    "on": ("test_todo_on_agent.yaml", "ON_MODE_OK project=AgentLoom python=>=3.12 checkpoint=true"),
    "auto": ("test_todo_auto_trivial_agent.yaml", "TODO_AUTO_TRIVIAL_OK"),
    "off": ("test_todo_off_agent.yaml", "TODO_OFF_OK MOOL-4"),
}


def run_case(case: str, workspace: Path) -> dict:
    from agentloom.configuration import C

    C.raw.setdefault("runtime", {})["root_dir"] = str(workspace / "runtime")
    C.raw.setdefault("checkpoint", {})["cleanup_on_success"] = False
    C.raw.setdefault("lsp_servers", {})["enabled"] = False
    filename, expected = CASES[case]
    receipt = execute(ROOT / "applications/test_demo/workflows" / filename, workspace)
    assert str(receipt["output"]).strip() == expected, "Final output oracle failed"
    envelopes = [
        data["runtime_checkpoint"]
        for path in (workspace / "runtime").rglob("checkpoint.json")
        if isinstance(data := json.loads(path.read_text()), dict)
        and isinstance(data.get("runtime_checkpoint"), dict)
        and data["runtime_checkpoint"].get("task_id") == receipt["task_id"]
        and data["runtime_checkpoint"].get("run_id") == receipt["run_id"]
    ]
    assert len(envelopes) == 1, "Expected one persisted root runtime checkpoint"
    envelope = envelopes[0]
    assert envelope["runtime_id"] == "smolagents" and envelope["state_schema_version"] == 2
    steps = envelope["payload"]["memory_steps"]
    records = [record for step in steps for record in step.get("tool_results") or []]
    completed = [record for record in records if record["status"] == "completed"]
    todos = [record for record in completed if record["tool_name"] == "todo_write"]
    assert any(record["tool_name"] == "final_answer" for record in completed)
    if case == "on":
        assert completed[0]["tool_name"] == "todo_write", "On mode must establish Todo before work"
        assert len(todos) >= 2, "Expected initial and final Todo updates"
        paths = {Path(record["input"]["file_path"]).name for record in completed if record["tool_name"] == "read_file"}
        assert paths >= {"README.md", "pyproject.toml", "system.yaml"}, "Source reads missing"
        persisted = list((workspace / "runtime").rglob("todos.json"))
        assert len(persisted) == 1
        document = json.loads(persisted[0].read_text())
        assert document["task_id"] == receipt["task_id"]
        snapshots = list(document["agents"].values())
        assert len(snapshots) == 1 and snapshots[0]["revision"] >= 2
        assert all(item["status"] == "completed" for item in snapshots[0]["items"])
    else:
        assert not any(record["tool_name"] == "todo_write" for record in records)
    return {"case": case, "status": "passed", "revision": receipt["revision"],
            "task_id": receipt["task_id"], "run_id": receipt["run_id"],
            "completed_tools": [record["tool_name"] for record in completed], "todo_updates": len(todos)}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace", type=Path, required=True)
    parser.add_argument("--child", choices=CASES, help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.child:
        try:
            report = run_case(args.child, args.workspace)
        except BaseException as exc:
            dump(args.workspace / "assertions.json", {"status": "failed", "type": type(exc).__name__, "error": str(exc)})
            raise
        dump(args.workspace / "assertions.json", report)
        return 0
    args.workspace.mkdir(parents=True, exist_ok=False)
    reports = []
    for case in CASES:
        workspace = args.workspace / case
        workspace.mkdir()
        with (workspace / "process.log").open("w") as log:
            process = subprocess.Popen([sys.executable, __file__, "--child", case, "--workspace", str(workspace)],
                                       cwd=ROOT, stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
            timed_out = False
            try:
                code = process.wait(timeout=300)
            except subprocess.TimeoutExpired:
                timed_out = True
                os.killpg(process.pid, signal.SIGINT)
                try:
                    code = process.wait(timeout=15)
                except subprocess.TimeoutExpired:
                    os.killpg(process.pid, signal.SIGKILL)
                    code = process.wait()
        report = {"case": case, "status": "passed" if code == 0 and not timed_out else "failed",
                  "exit_code": code, "timed_out": timed_out, "workspace": str(workspace)}
        reports.append(report)
        dump(args.workspace / "summary.json", reports)
        print(json.dumps(report), flush=True)
    return int(any(report["status"] != "passed" for report in reports))


if __name__ == "__main__":
    raise SystemExit(main())
