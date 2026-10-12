"""Bounded real-model F3–F8 acceptance through the public execute_app seam.

Run with the configured project interpreter, e.g.:
    python tests/acceptance/existing_application_validation.py --case all --workspace /new/evidence

Each attempt creates a new directory and retains logs, receipts, copied inputs,
pytest XML, tool records and failures. No historical output is removed. F6 has
its separate interrupt/resume runner in tests/agent_test/real_checkpoint_validation.py.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from datetime import UTC, datetime
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
CASES = ("repo", "context_text", "context_json", "context_multi", "core", "markdown", "goal_contract", "goal_parallel")
TIMEOUTS = {case: 900 for case in CASES} | {"goal_contract": 1500, "goal_parallel": 1200}
CONTEXT = {
    "text": [
        (
            "make_context_engine_text_payload",
            "loom_retrieve_context",
            "text",
            "TARGET_RECORD case=text",
            "TEXT-CTX-7319",
        )
    ],
    "json": [
        (
            "make_context_engine_json_payload",
            "loom_retrieve_context",
            "json",
            "verification_value",
            "JSON-CTX-4927",
        )
    ],
    "multi": [
        (
            "make_context_engine_log_payload",
            "retrieve_log_context",
            "log",
            "LOG_TARGET_RECORD",
            "LOG-CTX-8842",
        ),
        (
            "make_context_engine_search_payload",
            "retrieve_search_context",
            "search",
            "SEARCH_TARGET_RECORD",
            "SEARCH-CTX-6194",
        ),
    ],
}


def dump(path: Path, value: object) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, default=str) + "\n", encoding="utf-8")


def walk(value):
    if isinstance(value, dict):
        yield value
        for item in value.values():
            yield from walk(item)
    elif isinstance(value, list):
        for item in value:
            yield from walk(item)


def records(runtime: Path) -> list[dict]:
    found = {}
    for path in runtime.rglob("*.json"):
        for value in walk(json.loads(path.read_text())):
            if {"tool_name", "status", "call_id", "input"} <= value.keys():
                found[value["call_id"]] = value
    return list(found.values())


def events(runtime: Path) -> list[dict]:
    return [json.loads(line) for path in (runtime / "checkpoints").rglob("task_events.jsonl")
            for line in path.read_text().splitlines() if line.strip()]


def assert_tools(runtime: Path, names: set[str]) -> list[dict]:
    found = records(runtime)
    completed = {item["tool_name"] for item in found if item["status"] == "completed"}
    missing = names - completed
    if missing:
        raise AssertionError(f"Missing successful persisted actual tool completion evidence: {sorted(missing)}; got {sorted(completed)}")
    return found


def assert_workers(runtime: Path, required: set[str], count: int | None = None) -> list[dict]:
    starts = [item for item in events(runtime) if item.get("type") == "worker_call_started"]
    observed = {item.get("agent_name") for item in starts}
    missing = sorted(required - observed)
    if missing or (count is not None and len(starts) != count):
        raise AssertionError(f"Worker calls missing {missing}; expected count {count}, got {len(starts)}")
    return starts


def metadata(workflow: Path) -> dict:
    import yaml
    from agentloom.config import C
    cfg = yaml.safe_load(workflow.read_text())
    model_type = cfg.get("model_type", C.default_model_type)
    return {"revision": subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip(),
            "workflow": str(workflow), "workflow_sha256": hashlib.sha256(workflow.read_bytes()).hexdigest(),
            "model_type": model_type, "model": C.get_model_config(model_type, "model"),
            "agent_runtime": cfg.get("agent_runtime"),
            "runtime_options": cfg.get("runtime_options", {}), "goal": cfg.get("goal"),
            "definition_files": {str(path): hashlib.sha256(path.read_bytes()).hexdigest()
                                 for path in workflow.parent.rglob("*") if path.suffix in {".yaml", ".yml", ".md"}},
            "interpreter": sys.executable}


def execute(workflow: Path, workspace: Path, *, task: str | None = None, resume: str | None = None, attempt="run") -> dict:
    from agentloom.app.runner import execute_app
    lifecycle = []
    if task is not None:
        configured = yaml.safe_load(workflow.read_text(encoding="utf-8"))
        configured["task"] = task
        workflow.write_text(yaml.safe_dump(configured, allow_unicode=True, sort_keys=False), encoding="utf-8")
    meta = metadata(workflow)
    meta["started_at"] = datetime.now(UTC).isoformat()
    def observe(event):
        from dataclasses import asdict
        lifecycle.append(asdict(event))
    try:
        result = execute_app(workflow, resume_task_id=resume, file_logging=True, event_sink=observe)
        meta.update(status="completed", run_id=result.run.run_id, task_id=result.run.task_id,
                    manifest=str(result.run.manifest_path), output=result.output, goal=dict(result.goal) if result.goal else None)
    finally:
        meta["ended_at"] = datetime.now(UTC).isoformat()
        dump(workspace / f"{attempt}_receipt.json", meta)
        dump(workspace / f"{attempt}_lifecycle.json", lifecycle)
    manifest = json.loads(Path(meta["manifest"]).read_text())
    if manifest["status"] != meta["status"]:
        raise AssertionError(f"Run receipt/manifest state mismatch: {meta['status']} vs {manifest['status']}")
    return meta


def copied_workflow(name: str, filename: str, workspace: Path, replacements: dict[str, str]) -> Path:
    source = ROOT / "applications" / name
    dest = ROOT / "applications" / ("architecture_acceptance_" + hashlib.sha256(str(workspace).encode()).hexdigest()[:12])
    shutil.copytree(source / "workflows", dest / "workflows")
    if (source / "config").is_dir():
        shutil.copytree(source / "config", dest / "config")
    for path in (dest / "workflows").rglob("*.yaml"):
        content = path.read_text()
        content = content.replace(f"applications/{name}/workflows/", str(dest / "workflows") + "/")
        for before, after in replacements.items():
            content = content.replace(before, after)
        path.write_text(content)
    return dest / "workflows" / filename


def verify_context(case: str, workspace: Path) -> dict:
    runtime = workspace / "runtime"
    entries = [json.loads(path.read_text()) for path in runtime.glob("**/context_store/entries/*.json")]
    retrieves = [json.loads(line) for path in runtime.glob("**/context_store/events.jsonl")
                for line in path.read_text().splitlines() if line.strip() and json.loads(line).get("type") == "retrieved"]
    retrieval_tools = {item[1] for item in CONTEXT[case]}
    tool_records = assert_tools(runtime, retrieval_tools)
    matched_refs = []
    for tool, retrieval_tool, kind, marker, hidden in CONTEXT[case]:
        matches = [entry for entry in entries if entry.get("source") == f"tool_result:{tool}" and entry.get("kind") == kind
                   and marker in entry.get("original", "") and hidden in entry.get("original", "")]
        if not matches:
            raise AssertionError(f"Missing independently verified context origin {tool}/{kind}/{hidden}")
        refs = {entry["ref"] for entry in matches}
        retrieval = [event for event in retrieves if event["ref"] in refs and event["retrieved_chars"] > 0]
        actual = [record for record in tool_records if record["tool_name"] == retrieval_tool and record["status"] == "completed"
                  and hidden in str(record.get("output")) and record["input"].get("ref") in refs
                  and any(event["ref"] == record["input"].get("ref")
                          and event["query"] == marker
                          and event["offset"] == 0
                          and event["limit"] in {20, 30} for event in retrieval)]
        if not retrieval or not actual:
            raise AssertionError(f"No correlated real retrieval + returned hidden value for {tool}")
        matched_refs.append(refs)
    if len(matched_refs) == 2 and matched_refs[0] & matched_refs[1]:
        raise AssertionError("Different worker payloads share ContextRef identity")
    assert_workers(runtime, set(), count=2 if case == "multi" else 1)
    return {"origin_refs": [sorted(refs) for refs in matched_refs], "retrieval_events": len(retrieves)}


def repo_fixture(target: Path) -> None:
    sources = {
        "main.py": "from package.service.handlers.checkout import checkout\n\ndef main():\n    return checkout(3)\n",
        "package/service/handlers/checkout.py": "from package.domain.pricing import total\n\ndef checkout(quantity):\n    return total(quantity, 7)\n",
        "package/domain/pricing.py": "def total(quantity, price):\n    if quantity < 0:\n        raise ValueError('negative quantity')\n    return quantity * price\n",
    }
    for relative, source in sources.items():
        path = target / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(source)


def verify_repo(workspace: Path) -> dict:
    out = workspace / "repo_output"
    progress = json.loads((out / "data/analysis_progress.json").read_text())
    if not progress or any(entry["status"] != "completed" for entry in progress.values()):
        raise AssertionError(f"Repo Map LLM directory analysis incomplete: {progress}")
    tags = json.loads((out / "data/tags.json").read_text())
    known = {"main", "checkout", "total"}
    actual = {item.get("name") for item in walk(tags) if item.get("kind") == "def"}
    if not known <= actual:
        raise AssertionError(f"Known symbols missing from parsed tags: {known - actual}")
    for tag in walk(tags):
        if tag.get("kind") not in {"def", "ref"} or not tag.get("rel_fname"):
            continue
        source = workspace / "repository" / tag["rel_fname"]
        if not source.is_file() or tag["name"] not in source.read_text():
            raise AssertionError(f"Unresolvable symbol/reference: {tag}")
    ranked = json.loads((out / "data/ranked.json").read_text())
    if not ranked:
        raise AssertionError("No ranking produced")
    skills = list(out.glob("*/SKILL.md"))
    if len(skills) != 1 or skills[0].stat().st_size < 100:
        raise AssertionError("Missing complete Skill artifact")
    package = skills[0].parent
    dependency = (package / "references/repo_map/dependencies.md").read_text()
    if not all(name in dependency for name in ("main.py", "checkout.py", "pricing.py")):
        raise AssertionError("Known cross-directory dependencies absent")
    routes = [json.loads(line) for line in (package / "references/manifest.jsonl").read_text().splitlines() if line]
    for route in routes:
        for key in ("index_path", "analysis_path"):
            path = package / route[key]
            if not path.is_file() or not path.read_text().strip():
                raise AssertionError(f"Empty/unresolvable Skill route: {path}")
    assert_workers(workspace / "runtime", {"dir_architecture_analysis"})
    assert_tools(workspace / "runtime", {"run_analysis_loop", "prepare_repo_map_skill_workspace", "write_repo_map_skill_files", "validate_repo_map_skill"})
    return {"directories": len(progress), "known_symbols": sorted(known), "skill": str(skills[0]), "routes": len(routes)}


def validate_goal(workspace: Path, receipt: dict, *, contract_case: bool) -> dict:
    goal = receipt.get("goal") or {}
    if receipt["status"] != "completed" or goal.get("status") != "complete" or not goal.get("evidence"):
        raise AssertionError(f"Goal did not explicitly complete with evidence: {goal}")
    runtime = workspace / "runtime"
    starts = assert_workers(runtime, set())
    finished = [event for event in events(runtime) if event.get("type") == "worker_call_finished"]
    if len(finished) != len(starts) or any(event.get("status") != "completed" for event in finished):
        raise AssertionError("Required audit Workers did not all complete")
    if len(starts) < (4 if contract_case else 6):
        raise AssertionError(f"Missing real audit Workers: {len(starts)}")
    name = "contract_validation" if contract_case else "parallel"
    report = workspace / "goal_reports" / f"{name}.md"
    content = report.read_text()
    markers = ("# Goal Mode Validation", "## Configuration Contract", "## Verdict") if contract_case else (
        "# Parallel Goal Validation", "## Batch Results", "## Goal State", "## Resume Instructions", f"goal_id={goal['goal_id']}")
    if any(marker not in content for marker in markers):
        raise AssertionError("Persisted Goal evidence is incomplete")
    assert_tools(runtime, {"run_goal_audit_batch", "update_goal"} if contract_case else {"inspect_parallel_goal_report", "update_goal"})
    return {"goal": goal, "worker_calls": len(starts), "report_sha256": hashlib.sha256(report.read_bytes()).hexdigest()}


def child(case: str, workspace: Path) -> dict:
    from agentloom.config import C
    C.raw.setdefault("runtime", {})["root_dir"] = str(workspace / "runtime")
    C.raw.setdefault("checkpoint", {})["cleanup_on_success"] = False
    os.environ["AGENTLOOM_GOAL_VALIDATION_OUTPUT_ROOT"] = str(workspace / "goal_reports")
    if case.startswith("context_"):
        kind = case.removeprefix("context_")
        name = f"context_engine_{'multi_worker' if kind == 'multi' else kind + '_retrieve'}_validation"
        execute(ROOT / f"applications/{name}/workflows/{name}_agent.yaml", workspace)
        return verify_context(kind, workspace)
    if case in {"core", "markdown"}:
        name = f"tool_registry_{case}_validation"
        workflow = copied_workflow(
            name,
            "core_tools_agent.yaml" if case == "core" else "markdown_report_agent.yaml",
            workspace,
            {f"/tmp/agentloom_{name}": str(workspace / "artifacts")},
        )
        execute(workflow, workspace)
        if case == "core":
            content = (workspace / "artifacts/result.txt").read_text()
            if content.splitlines() != ["ALPHA one", "beta two", "GAMMA three"]:
                raise AssertionError(f"Wrong real file result: {content!r}")
            expected = {"shell_tool", "write_file", "edit_file", "read_file", "glob_search", "grep_search", "list_directory"}
        else:
            content = (workspace / "artifacts/report.md").read_text()
            if not all(item in content for item in (
                "# Core File Markdown Validation",
                "## Summary",
                "## Result",
                "MARKDOWN_WRITE_MIGRATION_VALIDATION: PASS",
            )):
                raise AssertionError("Markdown content oracle failed")
            expected = {"write_file", "read_file"}
        return {"tools": sorted(expected), "tool_records": len(assert_tools(workspace / "runtime", expected)), "artifact_oracle": True}
    if case == "repo":
        from applications.repo_map.agent_tools.markdown_tool import generate_markdown_map
        from applications.repo_map.agent_tools.scan_rank_tool import scan_and_rank
        repo_fixture(workspace / "repository")
        scan_and_rank(str(workspace / "repository"), str(workspace / "repo_output"), incremental=False)
        generate_markdown_map(str(workspace / "repo_output"))
        repo_workflow = copied_workflow("repo_map", "repo_map_agent.yaml", workspace, {})
        execute(repo_workflow, workspace,
                task=f"Complete all Repo Map architecture analysis and Skill steps. output_dir={workspace / 'repo_output'}")
        return verify_repo(workspace)
    contract_case = case == "goal_contract"
    workflow = copied_workflow("goal_mode_validation", "goal_contract_agent.yaml" if contract_case else "goal_parallel_agent.yaml", workspace, {})
    first = execute(workflow, workspace)
    if contract_case:
        return validate_goal(workspace, first, contract_case=True)
    return validate_goal(workspace, first, contract_case=False)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--case", choices=[*CASES, "all"], default="all")
    parser.add_argument("--workspace", type=Path)
    parser.add_argument("--child", choices=CASES, help=argparse.SUPPRESS)
    parser.add_argument("--verify-existing", action="store_true", help="Recheck existing evidence; never rewrites old reports")
    args = parser.parse_args()
    if args.verify_existing:
        if not args.workspace or args.case == "all":
            parser.error("--verify-existing requires one --case and its existing scenario --workspace")
        output = args.workspace / ("verification_" + datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ") + ".json")
        try:
            if args.case == "repo":
                value = verify_repo(args.workspace)
            elif args.case.startswith("context_"):
                value = verify_context(args.case.removeprefix("context_"), args.workspace)
            elif args.case.startswith("goal_"):
                receipt = args.workspace / "run_receipt.json"
                value = validate_goal(args.workspace, json.loads(receipt.read_text()), contract_case=args.case == "goal_contract")
            else:
                parser.error("Tool scenarios are checked by a new full run")
        except BaseException as exc:
            dump(output, {"status": "failed", "type": type(exc).__name__, "error": str(exc)})
            raise
        dump(output, {"status": "passed", "rechecked_at": datetime.now(UTC).isoformat(), **value})
        print(output)
        return 0

    if args.child:
        try:
            result = child(args.child, args.workspace)
            dump(args.workspace / "assertions.json", {"status": "passed", **result})
            return 0
        except BaseException as exc:
            dump(args.workspace / "assertions.json", {"status": "failed", "type": type(exc).__name__, "error": str(exc)})
            raise
    root = args.workspace or Path(tempfile.mkdtemp(prefix="agentloom-existing-")) / "cases"
    root.mkdir(parents=True, exist_ok=False)
    results = []
    for case in CASES if args.case == "all" else [args.case]:
        workspace = root / case
        workspace.mkdir()
        start = time.monotonic()
        env = os.environ.copy()
        env["PYTHONPATH"] = str(ROOT)
        with (workspace / "process.log").open("w") as log:
            proc = subprocess.Popen([sys.executable, str(Path(__file__).resolve()), "--child", case, "--workspace", str(workspace)],
                                    cwd=ROOT, env=env, stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
            timed_out = False
            try:
                code = proc.wait(timeout=TIMEOUTS[case])
            except subprocess.TimeoutExpired:
                timed_out = True
                os.killpg(proc.pid, signal.SIGINT)
                try:
                    code = proc.wait(timeout=15)
                except subprocess.TimeoutExpired:
                    os.killpg(proc.pid, signal.SIGKILL)
                    code = proc.wait()
        result = {"case": case, "status": "passed" if code == 0 and not timed_out else "failed", "exit_code": code,
                  "timed_out": timed_out, "timeout_seconds": TIMEOUTS[case], "elapsed_seconds": round(time.monotonic() - start, 2),
                  "workspace": str(workspace)}
        results.append(result)
        dump(root / "summary.json", results)
        print(json.dumps(result), flush=True)
    return 1 if any(item["status"] != "passed" for item in results) else 0


if __name__ == "__main__":
    raise SystemExit(main())
