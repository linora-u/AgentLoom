"""Opt-in live acceptance. Creates isolated real YAML Applications using private llm.yaml.

Run with this worktree's Python. Reports contain receipts and answers, never model settings.
Each case has a process deadline and its own project/config/runtime directory.
"""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import time

import yaml

PROFILES = ["powerful", "powerful2", "powerful3", "powerful4",
            "responses_powerful", "responses_powerful2", "responses_powerful3", "responses_powerful4"]
TASKS = [
    ("literal", "Reply with exactly AGENTLOOM_PI_OK. No other text.", "AGENTLOOM_PI_OK"),
    ("arithmetic", "Compute 17 + 25. Reply with exactly the integer and no other text.", "42"),
    ("chinese", "请只回复：验证通过。不要加标点、解释或引号。", "验证通过"),
    ("json", 'Return exactly this JSON object, with no code fences or other text: {"ok":true,"runtime":"pi"}', None),
]


def child(root: Path, output: Path):
    from agentloom.app.runner import execute_app
    from agentloom.config.config import bind_config, load_project_config
    with bind_config(load_project_config(root)):
        result = execute_app(root / "applications/live/workflows/root.yaml", file_logging=False)
    output.write_text(json.dumps({"output": result.output, "application_id": result.run.application_id,
        "task_id": result.run.task_id, "run_id": result.run.run_id, "run_dir": str(result.run.run_dir),
        "manifest_path": str(result.run.manifest_path)}, ensure_ascii=False, indent=2))


def campaign(destination: Path, source: Path, limit: int):
    destination.mkdir(parents=True, exist_ok=True)
    jobs = [(profile, task) for profile in PROFILES for task in TASKS][:limit]
    python = sys.executable
    script = str(Path(__file__).resolve())

    def run(job):
        profile, (kind, task, expected) = job
        case = f"{profile}-{kind}"
        root = destination / case
        (root / "config").mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, root / "config/llm.yaml")
        (root / "config/llm.yaml").chmod(0o600)
        (root / "config/system.yaml").write_text("lsp_servers: {enabled: false}\ncheckpoint: {enabled: false}\nself_learning: {enabled: false}\ndefault_toolsets: []\n")
        app = root / "applications/live/workflows/root.yaml"
        app.parent.mkdir(parents=True, exist_ok=True)
        app.write_text(yaml.safe_dump({"name": case, "agent_runtime": "pi", "model_type": profile,
            "description": task, "workflow": task, "tools": [], "toolsets": []}, allow_unicode=True))
        receipt = root / "result.json"
        started = time.monotonic()
        with (root / "execution.log").open("w") as log:
            process = subprocess.Popen([python, script, "--child", str(root), "--output", str(receipt)],
                stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
            timed_out = False
            try:
                process.wait(timeout=150)
            except subprocess.TimeoutExpired:
                timed_out = True
                # SIGINT exercises Application interruption and adapter cleanup.
                os.kill(process.pid, signal.SIGINT)
                try:
                    process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    os.killpg(process.pid, signal.SIGKILL)
                    process.wait()
        record = {"case": case, "profile": profile, "seconds": round(time.monotonic() - started, 2),
                  "exit_code": process.returncode, "timed_out": timed_out, "passed": False}
        if process.returncode == 0 and receipt.is_file():
            data = json.loads(receipt.read_text())
            answer = data["output"].strip()
            try:
                passed = answer == expected if expected else json.loads(answer) == {"ok": True, "runtime": "pi"}
            except ValueError:
                passed = False
            record.update(data, passed=passed)
        print(json.dumps({k: record[k] for k in ("case", "passed", "seconds", "exit_code")}), flush=True)
        return record

    with ThreadPoolExecutor(max_workers=4) as pool:
        records = list(pool.map(run, jobs))
    report = {"sdk": "0.79.4", "entry": "execute_app with real YAML and builtin registry",
              "profile_overrides": {}, "cases": records, "passed": sum(r["passed"] for r in records), "total": len(records)}
    (destination / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2))
    return all(r["passed"] for r in records)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--child", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--llm", type=Path, default=Path("config/llm.yaml"))
    parser.add_argument("--limit", type=int, default=32)
    args = parser.parse_args()
    if args.child:
        child(args.child, args.output)
    else:
        sys.exit(0 if campaign(args.output.resolve(), args.llm.resolve(), args.limit) else 1)
