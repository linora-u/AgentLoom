"""Opt-in real smol Application controls for the shared write/Shell policies."""
import argparse
import json
import os
from pathlib import Path
import shutil
from concurrent.futures import ThreadPoolExecutor, as_completed
from uuid import uuid4
import yaml
from agentloom.application.runner import execute_app
from agentloom.configuration.config import bind_config, load_project_config


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--profiles", default="powerful,powerful2")
    args = parser.parse_args()
    root = args.out.resolve()
    root.mkdir(parents=True, mode=0o700, exist_ok=False)
    config = root / "config"
    config.mkdir(mode=0o700)
    shutil.copyfile("config/llm.yaml", config / "llm.yaml")
    os.chmod(config / "llm.yaml", 0o600)
    settings = yaml.safe_load((config / "llm.yaml").read_text())
    for profile in args.profiles.split(","):
        settings["model"][profile].update(timeout=60, num_retries=0, max_tokens=2048, max_output_tokens=2048)
    (config / "llm.yaml").write_text(yaml.safe_dump(settings))
    (config / "system.yaml").write_text(yaml.safe_dump({"runtime": {"root_dir": str(root / "runtime")}, "checkpoint": {"enabled": False}, "self_learning": {"enabled": False}, "default_toolsets": [], "skills": {"paths": []}, "lsp_servers": {"enabled": False}, "logging": {"console_enabled": False, "file_enabled": False}}))
    cases = []
    for profile in args.profiles.split(","):
        for scenario in ("create", "overwrite", "command_denied", "search_excluded"):
            name = f"smol_{profile}_{scenario}"
            app = root / "applications" / name
            files = app / "files"
            files.mkdir(parents=True)
            target = files / "target.txt"
            marker = "SMOL_" + uuid4().hex
            if scenario == "overwrite":
                target.write_text("ORIGINAL")
            (files / "secrets").mkdir()
            (files / "secrets" / "hidden.txt").write_text("SECRET_PRIVATE")
            (files / "public.txt").write_text("PUBLIC_" + marker)
            task = {
                "create": f"Call write_file exactly once to create {target} with exact content {marker}. Then answer DONE.",
                "overwrite": f"First read_file {target}, then write_file the same path with exact content {marker}. Then answer DONE.",
                "command_denied": f"Call shell_tool exactly once with command 'touch {target}'. It will be blocked; report BLOCKED and never retry or use another tool.",
                "search_excluded": f"Call grep_search exactly once with pattern PUBLIC|SECRET, path {files}, include *.txt. Return the matching line and never use another tool.",
            }[scenario]
            names = {"create": ["write_file"], "overwrite": ["read_file", "write_file"], "command_denied": ["shell_tool"], "search_excluded": ["grep_search"]}[scenario]
            workflow = app / "workflows" / "root.yaml"
            workflow.parent.mkdir()
            workflow.write_text(yaml.safe_dump({"name": name, "description": "Real smol shared-policy control", "workflow": task, "agent_runtime": "smolagents", "model_type": profile, "max_steps": 5, "smart_summary": False, "todo": {"mode": "off"}, "toolsets": [], "tools": [{"name": name} for name in names], "shell_settings": {"allowed_commands": ["printf"], "sandbox": {"enabled": False}}, "tool_access_control": {"path_validation": [{"tools": ["grep_search"], "exclude_paths": [str(files / "secrets")]}]}}))
            cases.append((name, workflow, target, marker, scenario))
    configuration = load_project_config(root)
    def run(case):
        name, workflow, target, marker, scenario = case
        row = {"application": name, "scenario": scenario, "status": "FAIL"}
        try:
            with bind_config(configuration):
                result = execute_app(workflow, file_logging=False)
            row.update(run_id=result.run.run_id, run_dir=str(result.run.run_dir), output=result.output)
            if scenario in {"create", "overwrite"}:
                assert target.read_text() == marker
            elif scenario == "command_denied":
                assert not target.exists() and "BLOCKED" in result.output.upper()
            else:
                assert marker in result.output and "SECRET_PRIVATE" not in result.output
            row["status"] = "PASS"
        except Exception as exc:
            row["error_type"] = type(exc).__name__
            (root / f"{name}.error.txt").write_text(str(exc))
        return row
    results = []
    with ThreadPoolExecutor(max_workers=2) as pool:
        for future in as_completed([pool.submit(run, case) for case in cases]):
            row = future.result()
            results.append(row)
            (root / "results.json").write_text(json.dumps(results, indent=2, ensure_ascii=False))
            print(json.dumps({key: row[key] for key in ("application", "status")}), flush=True)
    raise SystemExit(0 if all(row["status"] == "PASS" for row in results) else 1)

if __name__ == "__main__":
    main()
