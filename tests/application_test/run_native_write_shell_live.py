"""Opt-in remote-provider Application campaign. Never part of pytest/CI.

Run with PYTHONPATH=. python -m tests.application_test.run_native_write_shell_live
--out /private/new/directory --profiles powerful,powerful2 --repetitions 2.
Copies private credentials only into the requested private evidence directory.
"""

import argparse
import json
import os
import shutil
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import yaml
from agentloom.integrations.litellm.model_binding import ModelProfileOverlay, resolve_litellm_model_turn_binding
from agentloom.application.runner import execute_app
from agentloom.configuration.config import bind_config, load_project_config

from tests.application_test.native_write_shell_support import (
    SCENARIOS,
    external_write_runtime,
    verify_native,
    write_application,
)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--profiles", default="powerful")
    parser.add_argument("--repetitions", type=int, default=1)
    parser.add_argument("--scenarios", default=",".join(SCENARIOS))
    parser.add_argument("--smol", type=int, default=4, help="Real smol control Applications per profile")
    args = parser.parse_args()
    root = args.out.resolve()
    root.mkdir(parents=True, mode=0o700, exist_ok=False)
    config = root / "config"
    config.mkdir(mode=0o700)
    shutil.copyfile("config/llm.yaml", config / "llm.yaml")
    os.chmod(config / "llm.yaml", 0o600)
    private = yaml.safe_load((config / "llm.yaml").read_text())
    profiles = args.profiles.split(",")
    for profile in profiles:
        private["model"][profile].update(timeout=60, num_retries=0, max_tokens=2048, max_output_tokens=2048)
    (config / "llm.yaml").write_text(yaml.safe_dump(private))
    (config / "system.yaml").write_text(
        yaml.safe_dump(
            {
                "runtime": {"root_dir": str(root / "runtime")},
                "checkpoint": {"enabled": False},
                "self_learning": {"enabled": False},
                "lsp_servers": {"enabled": False},
                "default_toolsets": [],
                "logging": {"console_enabled": False, "file_enabled": False},
                "skills": {"paths": []},
            }
        )
    )
    cases = []
    for profile in profiles:
        for repetition in range(args.repetitions):
            for scenario in args.scenarios.split(","):
                name = f"native_{profile}_{scenario}_{repetition}"
                workflow, marker = write_application(root, name, profile, scenario)
                cases.append((name, profile, scenario, workflow, marker, False))
        for repetition in range(args.smol):
            name = f"smol_{profile}_{repetition}"
            workflow, marker = write_application(root, name, profile, "allowed", smol=True)
            cases.append((name, profile, "allowed", workflow, marker, True))
    observations = {}
    configuration = load_project_config(root)

    def factory(definition):
        return resolve_litellm_model_turn_binding(
            definition.model_selection.model_type,
            profile_overlay=ModelProfileOverlay(timeout=60, num_retries=0, max_tokens=2048, max_output_tokens=2048),
        )

    def run(case):
        name, profile, scenario, workflow, marker, smol = case
        started = time.monotonic()
        row = {
            "application": name,
            "profile": profile,
            "scenario": scenario,
            "runtime": "smolagents" if smol else "external-executor-fixture",
            "status": "FAIL",
        }
        try:
            with bind_config(configuration):
                result = execute_app(workflow, file_logging=False)
            row.update(run_id=result.run.run_id, run_dir=str(result.run.run_dir), output=result.output)
            if smol:
                assert marker in result.output, "Smol answer did not contain the independent random receipt"
            else:
                verify_native(scenario, observations[result.run.run_id], marker)
                row["executions"] = observations[result.run.run_id]["executions"]
            row["status"] = "PASS"
        except Exception as exc:
            # Keep diagnostics private; the published report contains only type.
            row["error_type"] = type(exc).__name__
            (root / f"{name}.error.txt").write_text(str(exc))
        row["seconds"] = round(time.monotonic() - started, 3)
        return row

    results = []
    with external_write_runtime(factory, observations), ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(run, case) for case in cases]
        for future in as_completed(futures):
            row = future.result()
            results.append(row)
            (root / "results.json").write_text(json.dumps(results, indent=2, ensure_ascii=False))
            print(json.dumps({key: row[key] for key in ("application", "status", "seconds")}), flush=True)
    (root / "revision.txt").write_text(subprocess.check_output(["git", "rev-parse", "HEAD"], text=True))
    raise SystemExit(0 if all(row["status"] == "PASS" for row in results) else 1)


if __name__ == "__main__":
    main()
