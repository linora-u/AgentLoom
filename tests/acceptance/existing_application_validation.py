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
import importlib.util
import json
import os
import shutil
import signal
import sqlite3
import subprocess
import sys
import tempfile
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
CASES = ("unit", "repo", "context_text", "context_json", "context_multi", "core", "markdown", "goal_bounded", "goal_parallel")
TIMEOUTS = {case: 900 for case in CASES} | {"goal_bounded": 1500, "goal_parallel": 1200}
WORKERS = ("function_intake", "scenario_planner", "pytest_generator", "test_refiner", "delivery_reporter")
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
    # CodeAct keeps ToolCallRecord in HookRun memory and projects real tool
    # completions to the durable session recorder. Read that observation seam,
    # never infer execution from model code or a final answer.
    database = runtime / "self_learning.db"
    if database.is_file():
        with sqlite3.connect(f"file:{database}?mode=ro", uri=True) as connection:
            connection.row_factory = sqlite3.Row
            for row in connection.execute(
                "SELECT event_id, tool_name, status, input_json, output_json, root_run_id, run_id "
                "FROM events WHERE event_type = 'tool_result'"
            ):
                found[row["event_id"]] = {"call_id": row["event_id"], "tool_name": row["tool_name"],
                    "status": row["status"], "input": json.loads(row["input_json"]),
                    "output": json.loads(row["output_json"]), "root_run_id": row["root_run_id"],
                    "run_id": row["run_id"], "evidence_source": "session_recorder"}
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
    from agentloom.configuration import C
    import yaml
    cfg = yaml.safe_load(workflow.read_text())
    model_type = cfg.get("model_type", C.default_model_type)
    return {"revision": subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip(),
            "workflow": str(workflow), "workflow_sha256": hashlib.sha256(workflow.read_bytes()).hexdigest(),
            "model_type": model_type, "model": C.get_model_config(model_type, "model"),
            "agent_runtime": cfg.get("agent_runtime"),
            "max_steps": cfg.get("max_steps", 80), "goal": cfg.get("goal"),
            "definition_files": {str(path): hashlib.sha256(path.read_bytes()).hexdigest()
                                 for path in workflow.parent.rglob("*") if path.suffix in {".yaml", ".yml", ".md"}},
            "interpreter": sys.executable}


def execute(workflow: Path, workspace: Path, *, task: str | None = None, resume: str | None = None, attempt="run") -> dict:
    from agentloom.application.runner import execute_app
    from agentloom.application.run import ApplicationRunBudgetLimited
    lifecycle = []
    meta = metadata(workflow)
    meta["started_at"] = datetime.now(timezone.utc).isoformat()
    def observe(event):
        from dataclasses import asdict
        lifecycle.append(asdict(event))
    try:
        result = execute_app(workflow, task_override=task, resume_task_id=resume, file_logging=True, event_sink=observe)
        meta.update(status="completed", run_id=result.run.run_id, task_id=result.run.task_id,
                    manifest=str(result.run.manifest_path), output=result.output, goal=dict(result.goal) if result.goal else None)
    except ApplicationRunBudgetLimited as exc:
        meta.update(status="budget_limited", run_id=exc.run.run_id, task_id=exc.run.task_id,
                    manifest=str(exc.run.manifest_path), goal=dict(exc.goal))
    finally:
        meta["ended_at"] = datetime.now(timezone.utc).isoformat()
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


def verify_unit(workspace: Path) -> dict:
    target = workspace / "fixture"
    files = sorted((target / "test/generated").glob("test_*.py"))
    if len(files) != 2:
        raise AssertionError(f"Expected generated tests for both functions, got {files}")
    original = ROOT / "applications/unit_test_studio/test/fixtures/sample_project/src/text_pipeline.py"
    if (target / "src/text_pipeline.py").read_bytes() != original.read_bytes():
        raise AssertionError("generation changed the source fixture")
    suffix = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    xml = workspace / f"generated_pytest_{suffix}.xml"
    with (workspace / f"generated_pytest_{suffix}.log").open("w") as log:
        result = subprocess.run([sys.executable, "-m", "pytest", *map(str, files), "-q", f"--junitxml={xml}"],
                                cwd=target, stdout=log, stderr=subprocess.STDOUT, timeout=90)
    suites = ET.parse(xml).getroot().findall(".//testsuite")
    counts = {key: sum(int(suite.get(key, "0")) for suite in suites) for key in ("tests", "failures", "errors", "skipped")}
    if result.returncode or counts["tests"] <= 0 or any(counts[key] for key in ("failures", "errors", "skipped")):
        raise AssertionError(f"Host pytest failed: {counts}; inspect {xml}")
    # Inspect actual generated data, then compare with manually authored behavior oracle.
    all_cases = {}
    for path in files:
        spec = importlib.util.spec_from_file_location(path.stem, path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        all_cases[module.FUNCTION_NAME] = module.TEST_CASES
    required = {
        "normalize_user_message": [("text", None, "TypeError"), ("text", "", None), ("max_len", 0, "ValueError")],
        "extract_keywords": [("message", None, "TypeError"), ("message", "", None), ("limit", 0, None)],
    }
    for function, checks in required.items():
        cases = all_cases.get(function, [])
        for key, value, error in checks:
            matching = [case for case in cases if key in case["input"] and case["input"][key] == value]
            if not matching or (error and not any(case.get("raises") == error for case in matching)):
                raise AssertionError(f"Required {function} scenario {key}={value!r}, exception={error} missing")
        text_key = "text" if function == "normalize_user_message" else "message"
        expected = "hello world" if function == "normalize_user_message" else ["hello", "world"]
        if not any(case["input"] == {text_key: "  Hello, World!  "} and case.get("expected") == expected for case in cases):
            raise AssertionError(f"Independent normal-input oracle absent for {function}")
        empty = "" if function == "normalize_user_message" else ["empty"]
        if not any(case["input"] == {text_key: ""} and case.get("expected") == empty for case in cases):
            raise AssertionError(f"Independent empty-input oracle absent for {function}")
        boundary_key = "max_len" if function == "normalize_user_message" else "limit"
        if function == "normalize_user_message":
            if not any(case["input"].get(boundary_key) == 3 and case.get("expected") == "hel" for case in cases):
                raise AssertionError("Independent truncation oracle absent")
        elif not any(case["input"].get(boundary_key) == 0 and case.get("expected") == [] for case in cases):
            raise AssertionError("Independent zero-limit oracle absent")
    assert_workers(workspace / "runtime", set(WORKERS), count=5)
    assert_tools(workspace / "runtime", {"resolve_function_targets", "get_function_context", "plan_test_scenarios", "build_pytest_template", "upsert_pytest_file", "validate_and_refine_generated_tests", "collect_generation_report"})
    return {"pytest": counts, "workers": list(WORKERS), "required_behavior_oracle": True}


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


def validate_goal(workspace: Path, receipt: dict, *, bounded: bool) -> dict:
    goal = receipt.get("goal") or {}
    if receipt["status"] != "completed" or goal.get("status") != "complete" or not goal.get("evidence"):
        raise AssertionError(f"Goal did not explicitly complete with evidence: {goal}")
    if goal.get("used_tokens", 0) <= 0:
        raise AssertionError("Real Goal token usage absent")
    runtime = workspace / "runtime"
    starts = assert_workers(runtime, set())
    finished = [event for event in events(runtime) if event.get("type") == "worker_call_finished"]
    if len(finished) != len(starts) or any(event.get("status") != "completed" for event in finished):
        raise AssertionError("Required audit Workers did not all complete")
    if len(starts) < (4 if bounded else 6):
        raise AssertionError(f"Missing real audit Workers: {len(starts)}")
    name = "bounded_list" if bounded else "parallel_budget"
    report = workspace / "goal_reports" / f"{name}.md"
    content = report.read_text()
    markers = ("# Goal Mode Validation", "## Configuration Contract", "## Verdict") if bounded else (
        "# Parallel Goal Budget", "## Batch Results", "## Accounting", "## Resume Instructions", f"goal_id={goal['goal_id']}")
    if any(marker not in content for marker in markers):
        raise AssertionError("Persisted Goal evidence is incomplete")
    assert_tools(runtime, {"run_goal_audit_batch", "update_goal"} if bounded else {"inspect_parallel_goal_budget_report", "update_goal"})
    return {"goal": goal, "worker_calls": len(starts), "report_sha256": hashlib.sha256(report.read_bytes()).hexdigest()}


def child(case: str, workspace: Path) -> dict:
    from agentloom.configuration import C
    C.raw.setdefault("runtime", {})["root_dir"] = str(workspace / "runtime")
    C.raw.setdefault("checkpoint", {})["cleanup_on_success"] = False
    C.raw.setdefault("lsp_servers", {})["enabled"] = False
    os.environ["AGENTLOOM_GOAL_VALIDATION_OUTPUT_ROOT"] = str(workspace / "goal_reports")
    if case == "unit":
        target = workspace / "fixture"
        shutil.copytree(ROOT / "applications/unit_test_studio/test/fixtures/sample_project", target)
        payload = {"target_root": str(target), "targets": "src/text_pipeline.py:normalize_user_message,src/text_pipeline.py:extract_keywords", "output_dir": "test/generated"}
        execute(ROOT / "applications/unit_test_studio/workflows/unit_test_studio_agent.yaml", workspace,
                task="Generate Python pytest tests using Unit Test Studio.\nUse this JSON payload exactly:\n" + json.dumps(payload))
        return verify_unit(workspace)
    if case.startswith("context_"):
        kind = case.removeprefix("context_")
        name = f"context_engine_{'multi_worker' if kind == 'multi' else kind + '_retrieve'}_validation"
        execute(ROOT / f"applications/{name}/workflows/{name}_agent.yaml", workspace)
        return verify_context(kind, workspace)
    if case in {"core", "markdown"}:
        name = f"tool_registry_{case}_validation"
        workflow = copied_workflow(name, "core_tools_agent.yaml" if case == "core" else "markdown_report_agent.yaml", workspace,
                                   {f"/tmp/agentloom_{name}": str(workspace / "artifacts")})
        execute(workflow, workspace)
        if case == "core":
            content = (workspace / "artifacts/result.txt").read_text()
            if content.splitlines() != ["ALPHA one", "beta two", "GAMMA three"]:
                raise AssertionError(f"Wrong real file result: {content!r}")
            expected = {"shell_tool", "write_file", "edit_file", "read_file", "glob_search", "grep_search", "list_directory"}
        else:
            content = (workspace / "artifacts/report.md").read_text()
            if not all(item in content for item in ("# Built-in Tool Catalog Markdown Validation", "## Summary", "## Result", "markdown_report toolset is explicitly enabled.")):
                raise AssertionError("Markdown content oracle failed")
            expected = {"write_markdown_file", "read_file"}
        return {"tools": sorted(expected), "tool_records": len(assert_tools(workspace / "runtime", expected)), "artifact_oracle": True}
    if case == "repo":
        from applications.repo_map.agent_tools.scan_rank_tool import scan_and_rank
        from applications.repo_map.agent_tools.markdown_tool import generate_markdown_map
        repo_fixture(workspace / "repository")
        scan_and_rank(str(workspace / "repository"), str(workspace / "repo_output"), incremental=False)
        generate_markdown_map(str(workspace / "repo_output"))
        execute(ROOT / "applications/repo_map/workflows/repo_map_agent.yaml", workspace,
                task=f"Complete all Repo Map architecture analysis and Skill steps. output_dir={workspace / 'repo_output'}")
        return verify_repo(workspace)
    bounded = case == "goal_bounded"
    workflow = copied_workflow("goal_mode_validation", "goal_bounded_list_agent.yaml" if bounded else "goal_parallel_budget_agent.yaml", workspace, {})
    first = execute(workflow, workspace)
    if bounded:
        return validate_goal(workspace, first, bounded=True)
    if first["status"] != "budget_limited":
        raise AssertionError(f"Parallel Worker budget did not trigger: {first['status']}")
    report_path = workspace / "goal_reports/parallel_budget.md"
    before_report = report_path.read_bytes()
    old_goal = first["goal"]
    if f"goal_id={old_goal['goal_id']}" not in before_report.decode():
        raise AssertionError("Budget report is not bound to current Goal")
    calls_before = assert_workers(workspace / "runtime", set(), count=6)
    import yaml
    cfg = yaml.safe_load(workflow.read_text())
    cfg["goal"]["token_budget"] = old_goal["used_tokens"] + 150000
    workflow.write_text(yaml.safe_dump(cfg, allow_unicode=True, sort_keys=False))
    second = execute(workflow, workspace, resume=first["task_id"], attempt="resume")
    if second["task_id"] != first["task_id"] or second["run_id"] == first["run_id"]:
        raise AssertionError("Goal resume task/run identity violated")
    if second["goal"]["used_tokens"] < old_goal["used_tokens"] or second["goal"]["goal_id"] != old_goal["goal_id"]:
        raise AssertionError("Goal resume lost identity/cumulative usage")
    if report_path.read_bytes() != before_report or len(assert_workers(workspace / "runtime", set())) != len(calls_before):
        raise AssertionError("Goal resume reran committed Worker batch")
    return validate_goal(workspace, second, bounded=False) | {"budget_limited_then_resumed": True, "batch_not_repeated": True}


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
        output = args.workspace / ("verification_" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ") + ".json")
        try:
            if args.case == "unit":
                value = verify_unit(args.workspace)
            elif args.case == "repo":
                value = verify_repo(args.workspace)
            elif args.case.startswith("context_"):
                value = verify_context(args.case.removeprefix("context_"), args.workspace)
            elif args.case.startswith("goal_"):
                receipt = args.workspace / ("run_receipt.json" if args.case == "goal_bounded" else "resume_receipt.json")
                value = validate_goal(args.workspace, json.loads(receipt.read_text()), bounded=args.case == "goal_bounded")
            else:
                parser.error("Tool scenarios are checked by a new full run")
        except BaseException as exc:
            dump(output, {"status": "failed", "type": type(exc).__name__, "error": str(exc)})
            raise
        dump(output, {"status": "passed", "rechecked_at": datetime.now(timezone.utc).isoformat(), **value})
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
