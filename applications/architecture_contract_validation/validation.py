"""Independent artifact, pytest, negative-control and runtime evidence checks."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

from .agent_tools.workspace_tools import junit_summary

APP_ROOT = Path(__file__).resolve().parent
WORKERS = ("repository_investigator", "change_planner", "repair_implementer", "independent_verifier")
REQUIRED_CASES = ("threshold_equal", "discount_before_shipping", "override_precedence",
                  "invalid_quantity", "invalid_price", "invalid_config", "empty_cart", "multiple_lines")


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def reset_fixture(destination: Path, case_nonce: str) -> Path:
    """Allocate a new fixture; refuse to erase any previous attempt or user data."""
    if destination.exists():
        raise FileExistsError(f"refusing to reset an existing workspace: {destination}")
    shutil.copytree(APP_ROOT / "fixture", destination, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    (destination / ".architecture-case.json").write_text(json.dumps({"case_nonce": case_nonce}))
    return destination


def _pytest(root: Path, evidence: Path, target: str) -> dict[str, object]:
    evidence.mkdir(parents=True, exist_ok=False)
    junit = evidence / "junit.xml"
    process = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-o", "addopts=", "-p", "no:cacheprovider", target, f"--junitxml={junit}"],
        cwd=root, env={**os.environ, "PYTHONPATH": str(root), "PYTHONDONTWRITEBYTECODE": "1", "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1"},
        text=True, capture_output=True, timeout=60,
    )
    (evidence / "pytest.log").write_text(process.stdout + process.stderr)
    return {"exit_code": process.returncode, **junit_summary(junit)}


def validate_artifacts(workspace: Path, evidence: Path, case_nonce: str) -> dict[str, object]:
    """Validate actual products without using a model or trusting a model verdict."""
    errors: list[str] = []
    evidence.mkdir(exist_ok=False)
    protected = [p for p in (APP_ROOT / "fixture").rglob("*") if p.is_file()
                 and "orderdesk" not in p.relative_to(APP_ROOT / "fixture").parts
                 and "__pycache__" not in p.parts and p.suffix != ".pyc"]
    for original in protected:
        relative = original.relative_to(APP_ROOT / "fixture")
        actual = workspace / relative
        if not actual.exists() or sha256(original) != sha256(actual):
            errors.append(f"protected fixture changed: {relative}")

    oracle_process = subprocess.run([sys.executable, str(APP_ROOT / "oracle.py"), str(workspace)],
                                    text=True, capture_output=True, timeout=30,
                                    env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"})
    (evidence / "oracle.json").write_text(oracle_process.stdout)
    (evidence / "oracle.stderr").write_text(oracle_process.stderr)
    if oracle_process.returncode != 0:
        errors.append("independent behavior oracle failed")

    generated = sorted((workspace / "tests/generated").glob("test_*.py"))
    if not generated:
        errors.append("no model-generated regression tests")
    test_result = _pytest(workspace, evidence / "pytest", "tests")
    if test_result["exit_code"] != 0 or test_result["tests"] <= 3 or any(test_result[k] for k in ("failures", "errors", "skipped")):
        errors.append("host pytest did not collect and pass all baseline/generated cases")
    for name in REQUIRED_CASES:
        if not any(name in item for item in test_result["cases"]):
            errors.append(f"required regression scenario not collected: {name}")

    negative = reset_fixture(evidence / "original-source", "negative-control")
    (negative / "tests/generated").mkdir(parents=True, exist_ok=True)
    for generated_file in generated:
        shutil.copy2(generated_file, negative / "tests/generated" / generated_file.name)
    negative_result = _pytest(negative, evidence / "negative-pytest", "tests/generated")
    if negative_result["exit_code"] != 1 or negative_result["failures"] < 5 or negative_result["errors"] or negative_result["skipped"]:
        errors.append("generated regressions failed to detect at least five original defects")

    report_path = workspace / "reports/final.json"
    try:
        report = json.loads(report_path.read_text())
        if report.get("case_nonce") != case_nonce or report.get("workspace") != str(workspace):
            errors.append("report identity differs from current workspace/case")
        if set(report.get("workers", [])) != set(WORKERS) or report.get("verified") is not True:
            errors.append("report Worker list or verifier verdict is incomplete")
        if set(report.get("generated_tests", [])) != {p.relative_to(workspace).as_posix() for p in generated}:
            errors.append("report generated_tests differs from actual files")
        referenced = (workspace / report["test_report"]).resolve()
        if workspace not in referenced.parents or not referenced.is_file():
            raise ValueError("test_report must reference an existing local report")
        test_report = json.loads(referenced.read_text())
        if test_report.get("exit_code") != 0 or test_report.get("tests", 0) <= 3:
            errors.append("report references a failed or empty test run")
        actual_summary = junit_summary(workspace / test_report["junit"])
        if any(test_report.get(key) != actual_summary[key] for key in ("tests", "failures", "errors", "skipped", "cases")):
            errors.append("referenced test report differs from actual JUnit evidence")
    except (OSError, ValueError, KeyError, TypeError) as exc:
        errors.append(f"invalid structured final report: {exc}")
    return {"passed": not errors, "errors": errors, "pytest": test_result, "negative_control": negative_result,
            "artifact_hashes": {p.relative_to(workspace).as_posix(): sha256(p) for p in workspace.rglob("*.py")
                                if "__pycache__" not in p.parts}}


def validate_trace(attempt: Path, receipt: dict[str, object]) -> dict[str, object]:
    errors: list[str] = []
    ledger_path = attempt / "tool-ledger.jsonl"
    ledger = [json.loads(line) for line in ledger_path.read_text().splitlines()] if ledger_path.exists() else []
    root_run_id = receipt.get("run", {}).get("run_id")
    task_id = receipt.get("run", {}).get("task_id")
    nonce = receipt["case_nonce"]
    local_ids = set()
    for worker in WORKERS:
        events = [row for row in ledger if row.get("agent_name") == worker]
        if not events:
            errors.append(f"no actual tool calls for Worker {worker}")
        for row in events:
            if row.get("root_run_id") != root_run_id or row.get("task_id") != task_id or row.get("case_nonce") != nonce:
                errors.append(f"cross-run identity leakage for {worker}")
            if not row.get("local_run_id") or row["local_run_id"] == root_run_id:
                errors.append(f"missing independent local run for {worker}")
            local_ids.add(row.get("local_run_id"))
    if len(local_ids) < 4:
        errors.append("fewer than four distinct Worker local runs")
    for worker in ("repair_implementer", "independent_verifier"):
        if not any(row.get("agent_name") == worker and row.get("operation") == "pytest"
                   and row.get("exit_code") == 0 for row in ledger):
            errors.append(f"no successful actual pytest execution by {worker}")
    calls = list((attempt / "runtime").glob("**/workers/*/calls/*/checkpoint.json"))
    checkpoints = [json.loads(path.read_text()) for path in calls]
    for worker in WORKERS:
        matching = [checkpoint for path, checkpoint in zip(calls, checkpoints, strict=True) if path.parents[2].name == worker]
        if not any(c.get("status") == "completed" and c.get("memory_steps") for c in matching):
            errors.append(f"no completed framework checkpoint with model memory for {worker}")
        if not any(sum((step.get("token_usage") or {}).get(key, 0) or 0
                       for step in c.get("memory_steps", []) for key in ("input_tokens", "output_tokens")) > 0 for c in matching):
            errors.append(f"no actual model usage in Worker memory for {worker}")
    supervisor_records = []
    code_actions = []
    for checkpoint_path in (attempt / "runtime").glob("checkpoints/*/*/checkpoint.json"):
        checkpoint = json.loads(checkpoint_path.read_text())
        for step in checkpoint.get("memory_steps", []):
            if step.get("code_action"):
                code_actions.append(step["code_action"])
            supervisor_records.extend(row for row in (step.get("tool_results") or [])
                                      if row.get("tool_name") in WORKERS and row.get("status") == "completed")
    if receipt.get("mode") == "codeact" and not code_actions:
        errors.append("CodeAct Supervisor has no persisted Python execution evidence")
    # Native Supervisor memory records typed calls directly. CodeAct's nested
    # calls are evidenced by the Worker's canonical input and final-answer Tool
    # record, without relying on an optional self-learning audit projection.
    by_worker = {}
    for row in supervisor_records:
        by_worker.setdefault(row["tool_name"], row)
    for path, checkpoint in sorted(zip(calls, checkpoints, strict=True), key=lambda pair: str(pair[0])):
        name = path.parents[2].name
        if name in by_worker or name not in WORKERS or checkpoint.get("status") != "completed":
            continue
        try:
            final_records = [row for step in checkpoint["memory_steps"] for row in (step.get("tool_results") or [])
                             if row.get("tool_name") == "final_answer" and row.get("status") == "completed"]
            by_worker[name] = {"input": {"query": _checkpoint_query(checkpoint["task_input"])},
                               "output": final_records[-1]["output"]}
        except (KeyError, IndexError, ValueError):
            errors.append(f"missing typed input or final Tool record in {name} checkpoint")
    transfers = []
    for previous, following in zip(WORKERS, WORKERS[1:], strict=False):
        try:
            previous_output = _structured(by_worker[previous]["output"])
            if not isinstance(previous_output, dict):
                raise ValueError("Worker result must be a JSON object")
            next_input = by_worker[following]["input"]["query"]
            matching_path = _exact_json_path(next_input, previous_output)
            if matching_path is None:
                errors.append(f"Worker result was not passed intact: {previous} -> {following}")
            else:
                transfers.append({"from": previous, "to": following, "query_path": matching_path,
                                  "original_output_sha256": hashlib.sha256(json.dumps(previous_output, sort_keys=True,
                                                                                      ensure_ascii=False).encode()).hexdigest()})
        except (KeyError, TypeError, ValueError) as exc:
            errors.append(f"missing structured Worker data transfer {previous} -> {following}: {exc}")
    try:
        report = json.loads((attempt / "workspace/reports/final.json").read_text())
        if not any(row.get("agent_name") == "independent_verifier" and row.get("operation") == "pytest"
                   and row.get("report") == report.get("test_report") for row in ledger):
            errors.append("final report does not reference the independent verifier's actual test run")
    except (OSError, ValueError):
        errors.append("missing structured final report for trace correlation")
    manifest_path = receipt.get("run", {}).get("manifest_path")
    manifest = json.loads(Path(manifest_path).read_text()) if manifest_path else {}
    if manifest.get("status") != "completed":
        errors.append(f"Run manifest status is {manifest.get('status')!r}")
    return {"passed": not errors, "errors": errors, "workers": list(WORKERS),
            "local_run_ids": sorted(str(item) for item in local_ids), "tool_events": len(ledger),
            "checkpoint_files": [str(p) for p in calls], "transfers": transfers}


def _structured(value):
    for _ in range(3):
        if not isinstance(value, str):
            return value
        text = value.strip()
        if text.startswith("```"):
            text = text.split("\n", 1)[1].rsplit("```", 1)[0]
        value = json.loads(text)
    return value


def _contains_exact_json(value, expected) -> bool:
    return _exact_json_path(value, expected) is not None


def _exact_json_path(value, expected) -> str | None:
    """Locate an intact result in a bounded JSON query/envelope traversal.

    Sibling context fields may supplement the result object, but every original
    field retains its complete exact nested value, type, and list order. A real
    Worker returns JSON text, so a Supervisor may preserve that text inside an
    envelope. At most three JSON decodes per path, 32 levels and 10,000 nodes are
    examined; malformed or excessively encoded payloads cannot count as proof.
    """
    pending = [(value, "$", 0, 3)]
    for _ in range(10_000):
        if not pending:
            break
        current, path, depth, decodes = pending.pop()
        if depth > 32:
            continue
        if _same_json(current, expected):
            return path
        if isinstance(current, dict) and isinstance(expected, dict):
            if expected.keys() <= current.keys() and all(_same_json(current[key], item) for key, item in expected.items()):
                return path
        if isinstance(current, str) and decodes and len(current) <= 1_000_000:
            try:
                decoded = json.loads(current)
            except (ValueError, RecursionError):
                continue
            pending.append((decoded, path + "::json", depth + 1, decodes - 1))
        elif isinstance(current, dict):
            pending.extend((item, f"{path}[{json.dumps(key)}]", depth + 1, decodes)
                           for key, item in reversed(list(current.items())))
        elif isinstance(current, list):
            pending.extend((current[index], f"{path}[{index}]", depth + 1, decodes)
                           for index in range(len(current) - 1, -1, -1))
    return None


def _same_json(value, expected) -> bool:
    # Python considers True == 1 and 1 == 1.0; these are not intact JSON values.
    if type(value) is not type(expected):
        return False
    if isinstance(value, dict):
        return value.keys() == expected.keys() and all(_same_json(value[key], expected[key]) for key in value)
    if isinstance(value, list):
        return len(value) == len(expected) and all(_same_json(left, right) for left, right in zip(value, expected, strict=True))
    return value == expected


def _checkpoint_query(task_input: str) -> str:
    """Extract the one declared typed query from its persisted input envelope."""
    block = task_input.split("\n<inputs>\n", 1)[1].split("\n</inputs>", 1)[0]
    # This Application owns a single query input and its description; use that
    # contract rather than scanning JSON in unrelated workflow instructions.
    description = "1. JSON result from the previous stage, with absolute workspace and unique case_nonce.: "
    return block.split(description, 1)[1].strip()
