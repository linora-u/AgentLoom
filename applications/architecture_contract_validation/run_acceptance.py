"""Bounded F1/F2/F9 entry point. Every attempt gets a new retained directory.

Example (from any cwd, with the candidate installed):
    python -m applications.architecture_contract_validation.run_acceptance \
      --project /path/to/AgentLoom --output /private/evidence/architecture --case all
"""

from __future__ import annotations

import argparse
import dataclasses
import hashlib
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import time
import uuid
from datetime import UTC, datetime
from pathlib import Path

import yaml

from .validation import APP_ROOT, reset_fixture, sha256, validate_artifacts, validate_trace

CASES = ("native", "codeact", "nested-native", "nested-codeact", "repeat-native", "repeat-codeact", "rejections", "policy")
ALL_CASES = ("repeat-native", "repeat-codeact", "nested-native", "nested-codeact", "rejections", "policy")


def _json_default(value):
    if dataclasses.is_dataclass(value):
        return {field.name: getattr(value, field.name) for field in dataclasses.fields(value)}
    if isinstance(value, (Path, datetime)):
        return str(value)
    raise TypeError(type(value).__name__)


def _write(path: Path, value) -> None:
    path.write_text(json.dumps(value, indent=2, default=_json_default), encoding="utf-8")


def _revision(project: Path) -> str:
    return subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=project, text=True).strip()


def _relocate_tool_namespaces(application: Path, application_id: str) -> list[dict[str, str]]:
    """Adapt absolute Python names only in the copied nested Application.

    Unlike Worker paths, an applications.* module is project-root-qualified.
    Moving its owner changes that namespace; preserve the loader's project
    isolation and record the explicit migration rather than using a fallback.
    """
    original = "applications.architecture_contract_validation."
    relocated = "applications." + application_id.replace("/", ".") + "."
    if original == relocated:
        return []
    changes = []
    for source in sorted((application / "workflows").rglob("*")):
        if source.suffix not in {".yaml", ".yml", ".md"}:
            continue
        content = source.read_text(encoding="utf-8")
        block = re.search(r"```yaml\s*\n(.*?)\n```", content, re.DOTALL) if source.suffix == ".md" else None
        if source.suffix == ".md" and block is None:
            raise ValueError(f"Markdown definition has no YAML block: {source}")
        config = yaml.safe_load(block.group(1) if block else content)
        changed = False
        for index, tool in enumerate(config.get("tools", [])):
            module = tool.get("module")
            if isinstance(module, str) and module.startswith(original):
                tool["module"] = relocated + module.removeprefix(original)
                changes.append({"definition": source.relative_to(application).as_posix(),
                                "field": f"tools[{index}].module", "from": module, "to": tool["module"]})
                changed = True
        if changed:
            rendered = yaml.safe_dump(config, sort_keys=False)
            if block:
                rendered = content[:block.start(1)] + rendered.rstrip("\n") + content[block.end(1):]
            source.write_text(rendered, encoding="utf-8")
    return changes


def prepare_attempt(project: Path, output: Path, case: str, *, baseline_project_relative: bool = False) -> tuple[Path, dict]:
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S")
    nonce = uuid.uuid4().hex
    attempt = output / f"{stamp}-{case}-{nonce[:8]}"
    attempt.mkdir(parents=True, exist_ok=False, mode=0o700)
    workspace = reset_fixture(attempt / "workspace", nonce)
    isolated = attempt / "project"
    (isolated / "config").mkdir(parents=True)
    (isolated / "pyproject.toml").write_text('[project]\nname = "AgentLoom"\nversion = "0.0.0"\n')
    system = yaml.safe_load((project / "config/system.yaml").read_text())
    system.update({"lsp_servers": {"enabled": False}, "mcp_servers": None,
                   "checkpoint": {**system.get("checkpoint", {}), "enabled": True, "cleanup_on_success": False},
                   "runtime": {**system.get("runtime", {}), "root_dir": str(attempt / "runtime")},
                   "self_learning": {"enabled": False}, "todo": {"mode": "off"}})
    (isolated / "config/system.yaml").write_text(yaml.safe_dump(system, sort_keys=False))
    llm = project / "config/llm.yaml"
    if not llm.is_file():
        raise FileNotFoundError("real acceptance requires project config/llm.yaml; no mock fallback")
    shutil.copy2(llm, isolated / "config/llm.yaml")
    (isolated / "config/llm.yaml").chmod(0o600)
    application_id = "nested/suite/architecture_contract_validation" if case.startswith("nested-") else "architecture_contract_validation"
    application = isolated / "applications" / application_id
    shutil.copytree(APP_ROOT, application, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    namespace_adaptations = _relocate_tool_namespaces(application, application_id)
    if baseline_project_relative:
        for supervisor in (application / "workflows").glob("*.yaml"):
            config = yaml.safe_load(supervisor.read_text())
            for worker in config["worker_agents"]:
                worker["path"] = f"applications/{application_id}/workflows/{worker['path']}"
            supervisor.write_text(yaml.safe_dump(config, sort_keys=False))
    mode = "codeact" if case.endswith("codeact") else "native"
    definition = application / "workflows" / f"{mode}.yaml"
    identity = hashlib.sha256()
    for path in sorted(APP_ROOT.rglob("*")):
        if path.is_file() and "__pycache__" not in path.parts:
            identity.update(path.relative_to(APP_ROOT).as_posix().encode() + path.read_bytes())
    request = {
        "case": case, "case_nonce": nonce, "workspace": str(workspace), "candidate_revision": _revision(project),
        "application_content_sha256": identity.hexdigest(), "definition_sha256": sha256(definition),
        "project": str(isolated), "candidate_project": str(project), "application_id": application_id,
        "definition": str(definition), "mode": mode, "started_at": datetime.now(UTC).isoformat(),
        "baseline_project_relative_adaptation": baseline_project_relative,
        "namespace_adaptations": namespace_adaptations,
        "configuration": {"lsp_servers": False, "mcp_servers": None, "checkpoint_cleanup_on_success": False,
                          "model_profile": "powerful", "runtime_root": str(attempt / "runtime")},
    }
    if case == "policy":
        _policy_definition(attempt, request)
        request["definition_sha256"] = sha256(definition)
    request["prepared_definition_sha256"] = {
        source.relative_to(application).as_posix(): sha256(source)
        for source in sorted((application / "workflows").rglob("*"))
        if source.is_file() and source.suffix in {".yaml", ".yml", ".md"}
    }
    _write(attempt / "request.json", request)
    return attempt, request


def _run_child(attempt: Path, request: dict) -> int:
    from agentloom.application.runner import execute_app
    from agentloom.configuration import C

    receipt = {**request, "status": "failed"}
    events = []

    def observe(event):
        events.append(event)
        with (attempt / "lifecycle.jsonl").open("a") as stream:
            stream.write(json.dumps(event, default=_json_default) + "\n")

    try:
        receipt["model"] = C.llm.for_type("powerful").model
        if request["case"] == "policy":
            definition = _policy_definition(attempt, request)
            task = "Attempt the requested write once, observe its blocked result, and report the actual policy decision. ACCEPTANCE_PAYLOAD=" + json.dumps({"workspace": request["workspace"], "case_nonce": request["case_nonce"]})
        else:
            definition = Path(request["definition"])
            task = "Investigate and repair the repository through every Worker, then verify and persist reports/final.json. ACCEPTANCE_PAYLOAD=" + json.dumps({"workspace": request["workspace"], "case_nonce": request["case_nonce"]})
        result = execute_app(definition, task_override=task, file_logging=True, event_sink=observe)
        receipt.update({"status": "completed", "run": _json_default(result.run), "output": result.output})
    except BaseException as exc:
        receipt["error"] = {"type": type(exc).__name__, "message": str(exc)}
        if getattr(exc, "run", None):
            receipt["run"] = _json_default(exc.run)
    finally:
        receipt["ended_at"] = datetime.now(UTC).isoformat()
        _write(attempt / "receipt.json", receipt)
    if request["case"].startswith("repeat-") and not request.get("repetition"):
        repeated = attempt / "repeated"
        repeated.mkdir()
        nonce = uuid.uuid4().hex
        workspace = reset_fixture(repeated / "workspace", nonce)
        next_request = {**request, "repetition": 2, "case_nonce": nonce, "workspace": str(workspace),
                        "configuration": {**request["configuration"], "runtime_root": str(repeated / "runtime")},
                        "started_at": datetime.now(UTC).isoformat()}
        _write(repeated / "request.json", next_request)
        os.environ["AGENTLOOM_ARCHITECTURE_WORKSPACE"] = str(workspace)
        os.environ["AGENTLOOM_RUNTIME_ROOT"] = str(repeated / "runtime")
        C.raw["runtime"]["root_dir"] = str(repeated / "runtime")
        second_exit = _run_child(repeated, next_request)
        if second_exit:
            return second_exit
    return 0 if receipt["status"] == "completed" else 1


def _policy_definition(attempt: Path, request: dict) -> Path:
    definition = Path(request["definition"])
    payload = yaml.safe_load(definition.read_text())
    payload.update({"name": "architecture_policy_probe", "worker_agents": [], "max_steps": 4,
                    "workflow": "Call write_workspace_file exactly once with the payload workspace, relative_path='reports/blocked-sentinel.json', content='{}'. This is a real policy test: attempt the tool even though it will be blocked. Do not bypass, retry, or write by another method. Return the actual observed blocked reason through final_answer.",
                    "hooks": {"PreToolUse": [{"id": "architecture.deny-write", "matcher": "write_workspace_file", "command": "printf '%s\\n' '{\"decision\":\"block\",\"reason\":\"ARCHITECTURE_POLICY_DENY\"}'"}]}})
    definition.write_text(yaml.safe_dump(payload, sort_keys=False))
    return definition


def _rejection_child(attempt: Path, request: dict) -> int:
    """Negative case: audit network attempts and require run-less preflight rejection."""
    from agentloom.application.runner import execute_app
    from agentloom.tui_bridge.domain_cli import _dispatch

    project = Path(request["project"])
    base = Path(request["definition"])
    original = base.read_text()
    variants = ("duplicate-key", "invalid-worker", "missing-reference")
    results = []
    for variant in variants:
        application_id = f"invalid_{variant.replace('-', '_')}"
        app = project / "applications" / application_id
        shutil.copytree(base.parent.parent, app)
        definition = app / "workflows/native.yaml"
        if variant == "duplicate-key":
            definition.write_text(original + "\nname: duplicated_name\n")
        elif variant == "invalid-worker":
            worker = app / "workflows/worker_agents/repository_investigator.yaml"
            invalid = yaml.safe_load(worker.read_text())
            invalid["agent_function_schema"]["inputs"]["query"]["required"] = "yes"
            worker.write_text(yaml.safe_dump(invalid, sort_keys=False))
        else:
            invalid = yaml.safe_load(original)
            invalid["worker_agents"][0]["path"] = "worker_agents/does_not_exist.yaml"
            definition.write_text(yaml.safe_dump(invalid, sort_keys=False))
        # Keep only the tested supervisor: a second valid definition must not mask errors.
        (app / "workflows/codeact.yaml").unlink()
        studio = _dispatch(project, "application.validate", {"application_id": application_id})
        events = []
        error = None
        network = []

        def audit(event, args, network=network):
            if event == "socket.connect":
                network.append(str(args[1]))
                raise AssertionError("static rejection attempted a network connection")

        sys.addaudithook(audit)
        try:
            execute_app(definition, task_override="Invalid definitions must reject without running.", event_sink=events.append)
        except Exception as exc:
            error = {"type": type(exc).__name__, "message": str(exc)}
        diagnostic = {"duplicate-key": "duplicate", "invalid-worker": "required", "missing-reference": "does_not_exist"}[variant]
        same_cause = (diagnostic in json.dumps(studio.get("errors", [])).lower()
                      and diagnostic in str((error or {}).get("message", "")).lower())
        passed = (studio.get("valid") is False and same_cause and error is not None
                  and [event.event for event in events] == ["run.rejected"] and not network
                  and not list((attempt / "runtime").glob("**/manifest.json"))
                  and not (attempt / "tool-ledger.jsonl").exists())
        results.append({"variant": variant, "passed": passed, "studio": studio, "definition_sha256": sha256(definition),
                        "runtime_error": error, "events": events, "network_attempts": network})
    result = {**request, "passed": all(row["passed"] for row in results), "variants": results,
              "ended_at": datetime.now(UTC).isoformat()}
    _write(attempt / "validation.json", result)
    return 0 if result["passed"] else 1


def _walk(value):
    if isinstance(value, dict):
        yield value
        for item in value.values():
            yield from _walk(item)
    elif isinstance(value, list):
        for item in value:
            yield from _walk(item)


def validate_policy(attempt: Path, receipt: dict) -> dict:
    records = []
    for checkpoint in (attempt / "runtime").glob("**/checkpoint.json"):
        for row in _walk(json.loads(checkpoint.read_text())):
            if row.get("tool_name") == "write_workspace_file" and row.get("status") == "blocked":
                records.append(row)
    errors = []
    if not records or not any("ARCHITECTURE_POLICY_DENY" in json.dumps(row) for row in records):
        errors.append("missing actual persisted blocked ToolCallRecord with policy reason")
    if (attempt / "workspace/reports/blocked-sentinel.json").exists() or (attempt / "tool-ledger.jsonl").exists():
        errors.append("policy-blocked side effect was executed")
    if receipt.get("status") != "completed":
        errors.append("policy probe did not complete after observing the block")
    return {"passed": not errors, "errors": errors, "blocked_records": records}


def run_attempt(project: Path, output: Path, case: str, timeout: int, *, baseline_project_relative: bool = False) -> dict:
    attempt, request = prepare_attempt(project, output, case, baseline_project_relative=baseline_project_relative)
    env = {**os.environ, "PYTHONPATH": str(project), "AGENTLOOM_RUNTIME_ROOT": str(attempt / "runtime"),
           "AGENTLOOM_ARCHITECTURE_WORKSPACE": request["workspace"], "PYTHONDONTWRITEBYTECODE": "1"}
    command = [sys.executable, "-m", "applications.architecture_contract_validation.run_acceptance", "--child", str(attempt)]
    started = time.monotonic()
    with (attempt / "process.log").open("w") as log:
        process = subprocess.Popen(command, cwd=request["project"], env=env, stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
        timed_out = False
        try:
            exit_code = process.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            timed_out = True
            os.killpg(process.pid, signal.SIGINT)
            try:
                exit_code = process.wait(timeout=15)
            except subprocess.TimeoutExpired:
                os.killpg(process.pid, signal.SIGKILL)
                exit_code = process.wait()
    if case == "rejections":
        validation_path = attempt / "validation.json"
        validation = json.loads(validation_path.read_text()) if validation_path.exists() else {"passed": False, "errors": ["rejection child failed before report"]}
    else:
        receipt_path = attempt / "receipt.json"
        receipt = json.loads(receipt_path.read_text()) if receipt_path.exists() else {**request, "status": "failed"}
        try:
            if case == "policy":
                validation = validate_policy(attempt, receipt)
            else:
                artifacts = validate_artifacts(attempt / "workspace", attempt / "host-validation", request["case_nonce"])
                trace = validate_trace(attempt, receipt)
                validation = {"passed": artifacts["passed"] and trace["passed"], "artifacts": artifacts, "trace": trace}
                if case.startswith("repeat-"):
                    repeated = attempt / "repeated"
                    second_receipt = json.loads((repeated / "receipt.json").read_text())
                    second_artifacts = validate_artifacts(repeated / "workspace", repeated / "host-validation", second_receipt["case_nonce"])
                    second_trace = validate_trace(repeated, second_receipt)
                    distinct = all(receipt["run"][key] != second_receipt["run"][key] for key in ("task_id", "run_id"))
                    validation["repeated"] = {"artifacts": second_artifacts, "trace": second_trace, "distinct_identities": distinct}
                    validation["passed"] = validation["passed"] and second_artifacts["passed"] and second_trace["passed"] and distinct
        except Exception as exc:
            validation = {"passed": False, "errors": [f"independent validation raised {type(exc).__name__}: {exc}"]}
        _write(attempt / "validation.json", validation)
    summary = {**request, "attempt": str(attempt), "exit_code": exit_code, "timed_out": timed_out,
               "elapsed_seconds": round(time.monotonic() - started, 3), "ended_at": datetime.now(UTC).isoformat(),
               "passed": exit_code == 0 and not timed_out and validation["passed"], "validation": str(attempt / "validation.json")}
    _write(attempt / "summary.json", summary)
    with (output / "attempts.jsonl").open("a") as stream:
        stream.write(json.dumps(summary) + "\n")
    return summary


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project", type=Path, default=APP_ROOT.parents[1])
    parser.add_argument("--output", type=Path)
    parser.add_argument("--case", choices=("all", *CASES), default="all")
    parser.add_argument("--repeats", type=int, default=2, help="consecutive fresh native/CodeAct runs; default 2")
    parser.add_argument("--timeout", type=int, default=1200, help="per-attempt wall-clock seconds")
    parser.add_argument("--baseline-project-relative", action="store_true", help="record an explicit baseline-only Worker-path adaptation; final acceptance must omit")
    parser.add_argument("--child", type=Path, help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.child:
        request = json.loads((args.child / "request.json").read_text())
        return _rejection_child(args.child, request) if request["case"] == "rejections" else _run_child(args.child, request)
    if not args.output or args.repeats < 1 or not 1 <= args.timeout <= 3600:
        parser.error("--output is required, repeats >= 1, and timeout must be 1..3600 seconds")
    output = args.output.expanduser().resolve()
    if output == args.project.resolve() or args.project.resolve() in output.parents:
        parser.error("--output must be outside the candidate checkout; attempts retain private model configuration")
    output.mkdir(parents=True, exist_ok=True, mode=0o700)
    cases = ALL_CASES if args.case == "all" else (args.case,)
    summaries = []
    for case in cases:
        for _ in range(args.repeats if case in {"native", "codeact"} else 1):
            try:
                summary = run_attempt(args.project.resolve(), output, case, args.timeout,
                                      baseline_project_relative=args.baseline_project_relative)
            except Exception as exc:
                # Even configuration/preparation failures receive a retained,
                # append-only receipt. Partial attempt directories stay intact.
                failed = output / f"preparation-failed-{case}-{uuid.uuid4().hex[:8]}"
                failed.mkdir(mode=0o700)
                summary = {"case": case, "passed": False, "attempt": str(failed), "elapsed_seconds": 0,
                           "ended_at": datetime.now(UTC).isoformat(), "exit_code": 1,
                           "error": {"type": type(exc).__name__, "message": str(exc)}}
                _write(failed / "summary.json", summary)
                with (output / "attempts.jsonl").open("a") as stream:
                    stream.write(json.dumps(summary) + "\n")
            summaries.append(summary)
            print(json.dumps({key: summary[key] for key in ("case", "passed", "attempt", "elapsed_seconds")}), flush=True)
    return 0 if all(summary["passed"] for summary in summaries) else 1


if __name__ == "__main__":
    raise SystemExit(main())
