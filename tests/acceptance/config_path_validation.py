"""Opt-in real-provider Applications exercising configuration declaration roots.

Every case owns its project, configuration, MCP process and runtime. Model
credentials are copied into private ignored directories, never into reports.
"""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
import json
import os
from pathlib import Path
import shlex
import signal
import subprocess
import sys
import time
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
CASES = (
    "prompt_root", "prompt_cross_app", "prompt_project", "prompt_absolute", "prompt_no_fallback",
    "worker_nested", "worker_cross_app", "skill_root", "skill_cross_app", "skill_global",
    "mcp_agent", "mcp_application", "mcp_global_inherit", "mcp_global_override",
    "mcp_list", "mcp_paths", "hook_application", "hook_global",
    "access_application", "access_global", "template_application", "template_global",
)


def write(path: Path, value: str | dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(value if isinstance(value, str) else yaml.safe_dump(value, allow_unicode=True))


def report(path: Path, value: dict) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, default=str) + "\n")


def read_task(path: Path, runtime: str) -> str:
    tool = "read" if runtime == "pi" else "read_file"
    argument = "path" if runtime == "pi" else "file_path"
    return f"Call {tool} with {argument}={str(path)!r}. Return the verification token from its actual contents."


def prepare(case: str, workspace: Path, runtime: str, profile: str, models: Path) -> dict:
    workspace.mkdir(parents=True, mode=0o700, exist_ok=False)
    app = workspace / "applications/path_check"
    shared = workspace / "applications/shared"
    workflow = app / "workflows/nested/supervisor.yaml"
    model_data = yaml.safe_load(models.read_text())
    model_file = workspace / "config/model.yaml"
    write(model_file, {"model": {profile: model_data["model"][profile]}})
    model_file.chmod(0o600)
    system: dict[str, Any] = {
        "runtime": {"root_dir": str(workspace / "runtime")},
        "self_learning": {"enabled": False}, "default_toolsets": [],
        "checkpoint": {"enabled": False}, "logging": {"console_enabled": False},
        "redaction": {"enabled": True},
    }
    app_system: dict = {}
    token = f"CONFIG-PATH-{case.upper().replace('_', '-')}-{runtime.upper()}"
    definition: dict[str, Any] = {
        "name": "path_check", "agent_runtime": runtime, "model_type": profile,
        "description": "Verify configured resources using their actual content.",
        "task": "Follow your system instructions.", "tools": [], "toolsets": [],
    }
    if runtime == "smolagents":
        definition["runtime_options"] = {"max_steps": 8, "smart_summary": False, "todo_mode": "off"}
    proof: dict = {
        "workflow": workflow, "expected": [token], "required_tools": set(),
        "global": system, "application": app_system, "definition": definition,
    }
    native = "read" if runtime == "pi" else "read_file"
    if case.startswith("prompt_"):
        prompt = app / "config/prompts/role.md"
        value = "config/prompts/role.md"
        if case == "prompt_cross_app":
            prompt, value = shared / "config/prompts/role.md", "../shared/config/prompts/role.md"
        elif case == "prompt_project":
            prompt, value = workspace / "config/prompts/role.md", "../../config/prompts/role.md"
        elif case == "prompt_absolute":
            value = str(prompt)
        write(prompt, f"Reply with exactly {token}. Do not add anything else.")
        definition["system_prompt"] = {"path": value}
        if case == "prompt_absolute":
            # Prompt files retain the existing relative-only admission rule.
            proof["reject_before_call"] = True
            proof["correct_prompt"] = "config/prompts/role.md"
        if case == "prompt_no_fallback":
            # The old YAML-relative spelling must fail even when a file exists
            # at that location. The case then executes the corrected declaration.
            definition["system_prompt"]["path"] = "missing.md"
            write(workflow.parent / "missing.md", "OLD-PATH-MUST-NOT-BE-USED")
            proof["reject_before_call"] = True
            proof["correct_prompt"] = value
    elif case.startswith("worker_"):
        worker_app = shared if case == "worker_cross_app" else app
        worker_path = worker_app / "workflows/worker_agents/deep/worker.yaml"
        write(worker_app / "config/prompts/worker.md", f"Reply with exactly {token}.")
        worker = deepcopy(definition)
        worker.update(
            name="path_worker", description="Return the token from this worker's instructions.",
            task="Follow the system instructions; return the token.",
            system_prompt={"path": "config/prompts/worker.md"},
            input_schema={"type": "object", "properties": {"query": {"type": "string"}},
                          "required": ["query"], "additionalProperties": False},
        )
        write(worker_path, worker)
        worker_reference = ("../shared/" if worker_app == shared else "") + "workflows/worker_agents/deep/worker.yaml"
        definition["worker_agents"] = [{"path": worker_reference}]
        definition["task"] = "Call path_worker(query='verify declared root'). Return the token from its actual result."
        proof["required_tools"] = {"path_worker"}
    elif case.startswith("skill_"):
        owner = workspace if case == "skill_global" else shared if case == "skill_cross_app" else app
        skill = owner / "extra-skills/path-checker"
        write(skill / "SKILL.md", f"---\nname: path-checker\ndescription: Read the private verification instruction.\n---\nReply with exactly {token} after loading this Skill.\n")
        skill_reference = "../shared/extra-skills/path-checker" if owner == shared else "extra-skills/path-checker"
        layer = system if owner == workspace else definition
        layer["skills"] = {"paths": [skill_reference]}
        definition["tools"] = [{"name": "read" if runtime == "pi" else "skill"}]
        definition["task"] = "Load the path-checker Skill and return the verification token from its actual contents."
        proof["required_tools"] = {"read" if runtime == "pi" else "skill"}
    elif case.startswith("mcp_"):
        config = {
            "mcpServers": {"facts": {"command": sys.executable,
                "args": [str(ROOT / "tests/mcp_test/fixtures/stdio_server.py"),
                         str(workspace / "mcp-events.jsonl")]}}
        }
        owner = workspace if case in {"mcp_global_inherit", "mcp_global_override"} else app
        write(owner / "config/mcp.json", json.dumps(config))
        mcp_reference: str | list | dict = {"path": "config/mcp.json"}
        if case == "mcp_agent":
            mcp_reference = "config/mcp.json"
        elif case == "mcp_list":
            mcp_reference = ["config/mcp.json"]
        elif case == "mcp_paths":
            mcp_reference = {"paths": ["config/mcp.json"]}
        layer = app_system if case == "mcp_application" else system if owner == workspace else definition
        layer["mcp_servers"] = mcp_reference
        if case == "mcp_global_inherit":
            app_system["mcp_servers"] = {"tool_timeout": 30}
        elif case == "mcp_global_override":
            write(app / "config/mcp.json", json.dumps(config))
            app_system["mcp_servers"] = {"path": "config/mcp.json"}
            # A bad global source makes an accidental inheritance observable.
            write(owner / "config/mcp.json", json.dumps({"mcpServers": {"wrong": {
                "command": sys.executable, "args": ["-c", "raise RuntimeError('wrong root')"]}}}))
        definition["task"] = f"Call mcp__facts__lookup(query={token!r}) exactly once. Report its query and answer."
        proof["required_tools"] = {"mcp__facts__lookup"}
        proof["mcp"] = True
    elif case.startswith(("hook_", "access_")):
        owner = workspace if case.endswith("_global") else app
        source = owner / "data/note.txt"
        write(source, token)
        definition["tools"] = [{"name": native}]
        definition["task"] = read_task(source, runtime)
        proof["required_tools"] = {native}
        if case.startswith("access_"):
            # Workspace contents are allowed by default. Read outside it so
            # success actually depends on the configured include path.
            included = workspace.parent / f"allowed-{workspace.name}"
            source = included / "note.txt"
            write(source, token)
            definition["task"] = read_task(source, runtime)
            layer = system if owner == workspace else app_system
            layer["tool_access_control"] = {"path_validation": [
                {"tools": [native], "include_paths": [os.path.relpath(included, owner)],
                 "exclude_paths": ["data/private"]}]}
            excluded = owner / "data/private/note.txt"
            hidden_token = token + "-EXCLUDED-CONTENT"
            write(excluded, hidden_token)
            definition["task"] += (f" Then call {native} with "
                f"{'path' if runtime == 'pi' else 'file_path'}={str(excluded)!r} once. "
                "Report whether that actual read was blocked. Do not infer its contents.")
            proof["excluded_path"] = excluded
            proof["excluded_token"] = hidden_token
        else:
            bundle = owner / "hooks/path-audit"
            hook_token = token + "-HOOK"
            write(bundle / "emit.py",
                  "import json\nfrom pathlib import Path\n"
                  f"Path('executed.txt').write_text({hook_token!r})\n"
                  f"print(json.dumps({{'agent_context': {hook_token!r}}}))\n")
            write(bundle / "HOOK.yaml", {"name": "path-audit", "hooks": {"PostToolUse": [
                {"id": "root-check", "matcher": native,
                 "command": f"{shlex.quote(sys.executable)} emit.py"}]}})
            layer = system if owner == workspace else app_system
            layer["hooks"] = {"bundles": {"path-audit": {"path": "hooks/path-audit"}}}
            definition["task"] += " Include the verification token subsequently supplied by the Hook."
            proof["hook_token"] = hook_token
            proof["hook_marker"] = bundle / "executed.txt"
    elif case.startswith("template_"):
        from agentloom.runtimes.smolagents.prompts.prompt_builder import load_base_prompt_templates
        from agentloom.execution.logging import NullLoggerBackend
        template = load_base_prompt_templates(prompt_template_path=None, model_id=None,
                                               agent_root=workspace, logger=NullLoggerBackend())
        assert template is not None
        template["system_prompt"] += f"\nWhen asked for the verification token, reply with exactly {token}.\n"
        owner = workspace if case == "template_global" else app
        write(owner / "config/template.yaml", template)
        layer = system if owner == workspace else app_system
        layer["runtime_options"] = {"prompt_template_path": "config/template.yaml"}
        definition["task"] = "Return the verification token from your system instructions."
    else:
        raise ValueError(case)
    write(workspace / "config/system.yaml", system)
    if app_system:
        write(app / "config/system.yaml", app_system)
    write(workflow, definition)
    return proof


def run_case(case: str, workspace: Path, runtime: str, profile: str, models: Path) -> None:
    from agentloom.app.runner import execute_app
    from agentloom.config.config import bind_config, load_project_config
    from agentloom.execution.observability import inspect_run
    proof = prepare(case, workspace, runtime, profile, models)
    with bind_config(load_project_config(workspace)):
        if proof.get("reject_before_call"):
            try:
                execute_app(proof["workflow"], file_logging=True)
            except (ValueError, FileNotFoundError):
                pass
            else:
                raise AssertionError("YAML-relative fallback unexpectedly ran")
            proof["definition"]["system_prompt"]["path"] = proof["correct_prompt"]
            write(proof["workflow"], proof["definition"])
        result = execute_app(proof["workflow"], file_logging=True)
    with inspect_run(result.run) as trace:
        events = list(trace.events())
    requests = sum(event["kind"] == "model_request" for event in events)
    turns = {event["model_turn_id"] for event in events
             if event["kind"] == "model_request" and event.get("model_turn_id")}
    assert requests > 0, "Application produced no model request evidence"
    assert all(token in str(result.output) for token in proof["expected"]), "Required resource content absent"
    records = [event for event in events if event["kind"] == "tool"]
    completed = {record["tool_name"] for record in records if record["status"] == "completed"}
    assert proof["required_tools"] <= completed, f"Required tools did not complete: {proof['required_tools'] - completed}"
    if proof.get("excluded_path"):
        with inspect_run(result.run) as trace:
            excluded_reads = [record for record in records if record.get("input_ref")
                and record["tool_name"] in proof["required_tools"]
                and json.loads(trace.read_text(record["input_ref"])).get(
                    "path" if runtime == "pi" else "file_path") == str(proof["excluded_path"])]
            assert excluded_reads and all(record["status"] == "blocked" for record in excluded_reads), \
                "Excluded file was not actually denied by the declared policy"
        assert proof["excluded_token"] not in str(result.output), "Excluded content leaked"
    if proof.get("mcp"):
        import psutil
        calls = [json.loads(line) for line in (workspace / "mcp-events.jsonl").read_text().splitlines()]
        assert any(item["event"] == "lookup" and item["query"] == proof["expected"][0] for item in calls)
        assert not any(psutil.pid_exists(item["pid"]) for item in calls), "MCP server was not released"
    if proof.get("hook_marker"):
        assert proof["hook_marker"].read_text() == proof["hook_token"]
        with inspect_run(result.run) as trace:
            assert any(proof["hook_token"] in trace.read_text(event["request_ref"])
                       for event in events if event["kind"] == "model_request" and event.get("request_ref")), \
                "Executed Hook context did not reach a real model request"
    value = {"case": case, "runtime": runtime, "profile": profile, "provider": "real",
             "passed": True, "model_request_events": requests, "logical_model_requests": len(turns),
             "completed_tools": sorted(completed),
             "blocked_tools": sorted({record["tool_name"] for record in records if record["status"] == "blocked"}),
             "output": result.output, "run_id": result.run.run_id, "run_dir": str(result.run.run_dir),
             "manifest": str(result.run.manifest_path)}
    report(workspace / "report.json", value)
    print(json.dumps(value, ensure_ascii=False), flush=True)


def campaign(args) -> bool:
    destination = args.output.resolve()
    destination.mkdir(parents=True, mode=0o700, exist_ok=False)
    jobs = [(runtime, case) for runtime in args.runtimes for case in args.cases
            if runtime == "smolagents" or not case.startswith("template_")]
    revision = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip()
    dirty = bool(subprocess.check_output(["git", "status", "--porcelain"], cwd=ROOT, text=True).strip())

    def run(job):
        runtime, case = job
        profile = args.pi_profile if runtime == "pi" else args.smol_profile
        workspace = destination / f"{runtime}-{case}"
        started = time.monotonic()
        with (destination / f"{runtime}-{case}.log").open("w") as log:
            process = subprocess.Popen(
                [sys.executable, str(Path(__file__).resolve()), "--child", case,
                 "--runtimes", runtime, "--pi-profile", profile, "--smol-profile", profile,
                 "--model-config", str(args.model_config.resolve()), "--output", str(workspace)],
                stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
            timed_out = False
            try:
                process.wait(timeout=args.deadline)
            except subprocess.TimeoutExpired:
                timed_out = True
                os.kill(process.pid, signal.SIGINT)
                try:
                    process.wait(timeout=12)
                except subprocess.TimeoutExpired:
                    os.killpg(process.pid, signal.SIGKILL)
                    process.wait()
        value = {"case": case, "runtime": runtime, "profile": profile,
                 "passed": process.returncode == 0 and (workspace / "report.json").is_file(),
                 "exit_code": process.returncode, "timed_out": timed_out,
                 "seconds": round(time.monotonic() - started, 2), "workspace": str(workspace)}
        print(json.dumps(value), flush=True)
        return value

    with ThreadPoolExecutor(max_workers=args.concurrency) as pool:
        results = list(pool.map(run, jobs))
    report(destination / "campaign.json", {"revision": revision, "dirty_worktree": dirty,
                                          "provider": "real", "cases": results,
                                          "passed": sum(value["passed"] for value in results),
                                          "total": len(results)})
    return all(value["passed"] for value in results)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--child", choices=CASES)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--model-config", type=Path, default=ROOT / "applications/news_agent/config/model.yaml")
    parser.add_argument("--pi-profile", default="codex_luna_news_initial")
    parser.add_argument("--smol-profile", default="deepseek_v4_1")
    parser.add_argument("--runtimes", nargs="+", choices=["pi", "smolagents"], default=["pi", "smolagents"])
    parser.add_argument("--cases", nargs="+", choices=CASES, default=list(CASES))
    parser.add_argument("--deadline", type=int, default=360)
    parser.add_argument("--concurrency", type=int, default=3)
    args = parser.parse_args()
    if args.child:
        profile = args.pi_profile if args.runtimes[0] == "pi" else args.smol_profile
        run_case(args.child, args.output.resolve(), args.runtimes[0], profile, args.model_config.resolve())
        return 0
    return 0 if campaign(args) else 1


if __name__ == "__main__":
    raise SystemExit(main())
