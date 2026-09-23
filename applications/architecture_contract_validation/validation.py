"""Independent artifact, pytest, negative-control and runtime evidence checks."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
from datetime import datetime
from pathlib import Path

from .agent_tools.workspace_tools import junit_summary

APP_ROOT = Path(__file__).resolve().parent
WORKERS = ("repository_investigator", "change_planner", "repair_implementer", "independent_verifier")
REQUIRED_CASES = ("threshold_equal", "discount_before_shipping", "override_precedence",
                  "invalid_quantity", "invalid_price", "invalid_config", "empty_cart", "zero_price", "multiple_lines")


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
        if not isinstance(report.get("test_report"), str):
            raise ValueError("test_report must be a relative-path string from run_workspace_tests.report, not a report object")
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


def _event_time(value) -> datetime:
    if not isinstance(value, str):
        raise ValueError("missing call or tool event timestamp")
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        raise ValueError("call and tool timestamps must include their timezone")
    return parsed


def _runtime_memory_steps(checkpoint: dict) -> list[dict]:
    envelope = checkpoint.get("runtime_checkpoint")
    if not isinstance(envelope, dict):
        raise ValueError("checkpoint lacks a runtime checkpoint envelope")
    if envelope.get("runtime_id") != "smolagents":
        raise ValueError(
            f"checkpoint runtime is not smolagents: {envelope.get('runtime_id')!r}"
        )
    if envelope.get("state_schema_version") != 2:
        raise ValueError(
            "checkpoint has unsupported smolagents state schema: "
            f"{envelope.get('state_schema_version')!r}"
        )
    if not isinstance(envelope.get("runtime_version"), str) or not envelope["runtime_version"]:
        raise ValueError("smolagents checkpoint lacks runtime_version")
    payload = envelope.get("payload")
    if not isinstance(payload, dict):
        raise ValueError("smolagents checkpoint payload is not a mapping")
    steps = payload.get("memory_steps")
    if not isinstance(steps, list):
        raise ValueError("smolagents checkpoint payload lacks memory_steps")
    if not isinstance(payload.get("canonical_model_items"), list):
        raise ValueError(
            "smolagents checkpoint payload lacks canonical_model_items"
        )
    return steps


def _completed_worker_calls(task_root, run_id, task_id, nonce, workspace, ledger):
    """Read only this receipt's calls; bind each to real start/finish and tool events."""
    if task_root is None:
        return [], [], []
    calls = sorted(task_root.glob("workers/*/calls/*/checkpoint.json"))
    records, errors = [], []
    try:
        events = [json.loads(line) for line in (task_root / "task_events.jsonl").read_text().splitlines()]
    except (OSError, ValueError):
        return calls, records, ["missing canonical Worker lifecycle events for receipt Application/task"]
    for path in calls:
        name = path.parents[2].name
        if name not in WORKERS:
            continue
        try:
            checkpoint = json.loads(path.read_text())
            if checkpoint.get("status") != "completed":
                continue  # Failed attempts cannot supply outputs; later repairs are legitimate.
            index = int(path.parent.name)
            if (checkpoint.get("task_id") != task_id or checkpoint.get("run_id") != run_id
                    or checkpoint.get("agent_name") != name or checkpoint.get("call_index") != index):
                raise ValueError("checkpoint task/run/Worker/call identity does not match its receipt path")
            starts = [(i, row) for i, row in enumerate(events) if row.get("type") == "worker_call_started"
                      and row.get("agent_name") == name and row.get("call_index") == index]
            finishes = [(i, row) for i, row in enumerate(events) if row.get("type") == "worker_call_finished"
                        and row.get("agent_name") == name and row.get("call_index") == index]
            if len(starts) != 1 or len(finishes) != 1:
                raise ValueError("completed call must have one canonical start and finish")
            start_order, start = starts[0]
            finish_order, finish = finishes[0]
            actual_hash = hashlib.sha256(str(checkpoint["task_input"]).encode()).hexdigest()[:16]
            if (start.get("run_id") != run_id or finish.get("status") != "completed"
                    or checkpoint.get("input_hash") != actual_hash
                    or start.get("input_hash") != checkpoint["input_hash"]
                    or finish.get("input_hash") != checkpoint["input_hash"] or start_order >= finish_order):
                raise ValueError("canonical call lifecycle identity or order mismatch")
            started_at, finished_at = _event_time(start.get("started_at")), _event_time(finish.get("finished_at"))
            if started_at >= finished_at:
                raise ValueError("call finish must follow its start")
            tool_events = [row for row in ledger if row.get("agent_name") == name
                           and row.get("task_id") == task_id and row.get("root_run_id") == run_id
                           and row.get("case_nonce") == nonce and row.get("workspace") == str(workspace)
                           and started_at <= _event_time(row.get("at")) <= finished_at]
            local_ids = {row.get("local_run_id") for row in tool_events}
            if len(local_ids) != 1 or not next(iter(local_ids)) or run_id in local_ids:
                raise ValueError("call must correlate to one distinct Worker local run's tool events")
            if any(record["local_run_id"] in local_ids for record in records):
                raise ValueError("distinct Worker calls must not reuse one local run identity")
            steps = _runtime_memory_steps(checkpoint)
            if not any(sum((step.get("token_usage") or {}).get(key, 0) or 0
                           for key in ("input_tokens", "output_tokens")) > 0 for step in steps):
                raise ValueError("no actual model usage in completed Worker memory")
            finals = [row for step in steps for row in (step.get("tool_results") or [])
                      if row.get("tool_name") == "final_answer" and row.get("status") == "completed"]
            output = _structured(finals[-1]["output"])
            if not isinstance(output, dict) or not _same_json(output, _structured(checkpoint["result"])):
                raise ValueError("completed result differs from the actual final-answer Tool output")
            identity = {"workspace": str(workspace), "case_nonce": nonce}
            query = _checkpoint_query(checkpoint["task_input"])
            if any(output.get(key) != value for key, value in identity.items()) or not _contains_exact_json(query, identity):
                raise ValueError("Worker input/output workspace or nonce differs from receipt")
            records.append({"worker": name, "call_index": index, "checkpoint": str(path),
                            "query": query, "output": output, "start_order": start_order, "finish_order": finish_order,
                            "started_at": started_at, "finished_at": finished_at,
                            "local_run_id": next(iter(local_ids)), "events": tool_events})
        except (OSError, ValueError, KeyError, IndexError, TypeError) as exc:
            errors.append(f"invalid {name} call evidence at {path}: {exc}")
    return calls, records, errors


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
    application_id = receipt.get("run", {}).get("application_id")
    supervisor_path = (attempt / "runtime/checkpoints" / application_id / task_id / "checkpoint.json"
                       if application_id and task_id else None)
    calls, records, call_errors = _completed_worker_calls(
        supervisor_path.parent if supervisor_path else None, root_run_id, task_id, nonce,
        attempt / "workspace", ledger,
    )
    errors.extend(call_errors)
    for worker in WORKERS:
        if not any(row["worker"] == worker for row in records):
            errors.append(f"no completed framework checkpoint with model memory and call identity for {worker}")
    if supervisor_path is None or not supervisor_path.is_file():
        errors.append("missing Supervisor checkpoint for receipt Application/task identity")
        checkpoint = {}
    else:
        checkpoint = json.loads(supervisor_path.read_text())
        if checkpoint.get("task_id") != task_id or checkpoint.get("run_id") != root_run_id:
            errors.append("Supervisor checkpoint identity does not match receipt")
            checkpoint = {}
        else:
            try:
                _runtime_memory_steps(checkpoint)
            except ValueError as exc:
                errors.append(f"invalid Supervisor runtime checkpoint: {exc}")
                checkpoint = {}
    # Both modes use the same canonical per-call input, result and lifecycle
    # evidence. Never pick a first call by filesystem or tool-result ordering.
    reachable = []
    for record in sorted(records, key=lambda row: row["start_order"]):
        name = record["worker"]
        if name == WORKERS[0]:
            reachable.append({**record, "transfers": {}, "corrective_transfers": []})
            continue
        allowed = {WORKERS[WORKERS.index(name) - 1]}
        if name == "repair_implementer":
            allowed.add("independent_verifier")
        for previous in sorted(reachable, key=lambda row: row["finish_order"], reverse=True):
            if (previous["worker"] not in allowed or previous["finish_order"] >= record["start_order"]
                    or previous["finished_at"] > record["started_at"]):
                continue
            query_match = _exact_json_match(record["query"], previous["output"])
            if query_match is None:
                continue
            transfer = {
                "from": previous["worker"], "to": name, "query_path": query_match["path"],
                "query_json_spans": query_match["json_spans"],
                "from_call_index": previous["call_index"], "to_call_index": record["call_index"],
                "from_checkpoint": previous["checkpoint"], "to_checkpoint": record["checkpoint"],
                "from_local_run_id": previous["local_run_id"], "to_local_run_id": record["local_run_id"],
                "source_finished_at": previous["finished_at"].isoformat(),
                "target_started_at": record["started_at"].isoformat(),
                "original_output_sha256": hashlib.sha256(json.dumps(previous["output"], sort_keys=True,
                                                                     ensure_ascii=False).encode()).hexdigest(),
            }
            transfers = dict(previous["transfers"])
            corrective = list(previous["corrective_transfers"])
            if previous["worker"] == "independent_verifier":
                corrective.append(transfer)
            else:
                transfers[(previous["worker"], name)] = transfer
            reachable.append({**record, "transfers": transfers, "corrective_transfers": corrective})
            break
    verifiers = [row for row in reachable if row["worker"] == "independent_verifier"]
    selected = verifiers[-1] if verifiers else None
    try:
        report = json.loads((attempt / "workspace/reports/final.json").read_text())
        reported_verifiers = [record for record in verifiers[-1:]
                              if isinstance(report.get("test_report"), str)
                              and report.get("verified") is True and record["output"].get("verified") is True
                              and record["output"].get("test_report") == report["test_report"]
                              and any(row.get("operation") == "pytest" and row.get("exit_code") == 0
                                      and row.get("report") == report["test_report"] for row in record["events"])]
        if reported_verifiers:
            selected = reported_verifiers[-1]
        else:
            errors.append("final report does not match the final independent verifier's actual test run and verdict")
    except (OSError, ValueError, AttributeError):
        errors.append("missing structured final report for trace correlation")
    transfers = list(selected["transfers"].values()) if selected else []
    for previous, following in zip(WORKERS, WORKERS[1:], strict=False):
        if not any(row["from"] == previous and row["to"] == following for row in transfers):
            errors.append(f"Worker result was not passed intact from an earlier completed call: {previous} -> {following}")
    manifest_path = receipt.get("run", {}).get("manifest_path")
    manifest = json.loads(Path(manifest_path).read_text()) if manifest_path else {}
    if manifest.get("status") != "completed":
        errors.append(f"Run manifest status is {manifest.get('status')!r}")
    return {"passed": not errors, "errors": errors, "workers": list(WORKERS),
            "local_run_ids": sorted(str(item) for item in local_ids), "tool_events": len(ledger),
            "checkpoint_files": [str(p) for p in calls], "transfers": transfers,
            "corrective_transfers": selected["corrective_transfers"] if selected else [],
            "supervisor_checkpoint": str(supervisor_path) if supervisor_path else None}


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
    match = _exact_json_match(value, expected)
    return match["path"] if match is not None else None


def _exact_json_match(value, expected) -> dict | None:
    """Locate an intact result, preserving replayable paths and decoded spans.

    Sibling context fields may supplement the result object, but every original
    field retains its complete exact nested value, type, and list order. A real
    Worker returns JSON text, so a Supervisor may preserve that text inside an
    envelope or free text. A malformed outer envelope does not invalidate an
    intact JSON object inside it. raw_decode only reads complete containers; it
    never repairs text or assembles fields from separate fragments.

    At most three JSON decodes per path, 32 levels and 10,000 nodes are examined.
    Strings are at most one million characters; fallback scanning also limits
    candidates to 10,000 and cumulative consumed/error spans to four million.
    ::json_at[start:end] slices Unicode characters in the current string before
    decoding, and json_spans records the same raw_decode boundaries for replay.
    """
    decoder = json.JSONDecoder()
    remaining_attempts, remaining_characters = 10_000, 4_000_000
    pending = [(value, "$", 0, 3, [])]
    for _ in range(10_000):
        if not pending:
            break
        current, path, depth, decodes, spans = pending.pop()
        if depth > 32:
            continue
        if _same_json(current, expected):
            return {"path": path, "json_spans": spans}
        if isinstance(current, dict) and isinstance(expected, dict):
            if expected.keys() <= current.keys() and all(_same_json(current[key], item) for key, item in expected.items()):
                return {"path": path, "json_spans": spans}
        if isinstance(current, str) and decodes and len(current) <= 1_000_000:
            try:
                decoded = json.loads(current)
            except RecursionError:
                continue
            except ValueError:
                fragments = []
                offset = 0
                while offset < len(current) and remaining_attempts and remaining_characters > 0:
                    if current[offset] not in "{[":
                        offset += 1
                        continue
                    remaining_attempts -= 1
                    try:
                        decoded, end = decoder.raw_decode(current, offset)
                    except json.JSONDecodeError as exc:
                        remaining_characters -= max(1, exc.pos - offset)
                        offset += 1
                        continue
                    except RecursionError:
                        break
                    remaining_characters -= end - offset
                    if remaining_characters < 0:
                        break
                    span = {"input_path": path, "offset": offset, "end": end, "characters": end - offset}
                    fragments.append((decoded, f"{path}::json_at[{offset}:{end}]", depth + 1,
                                      decodes - 1, [*spans, span]))
                    offset = end  # Nested values are visited through this decoded container.
                pending.extend(reversed(fragments))
            else:
                pending.append((decoded, path + "::json", depth + 1, decodes - 1, spans))
        elif isinstance(current, dict):
            pending.extend((item, f"{path}[{json.dumps(key)}]", depth + 1, decodes, spans)
                           for key, item in reversed(list(current.items())))
        elif isinstance(current, list):
            pending.extend((current[index], f"{path}[{index}]", depth + 1, decodes, spans)
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
    """Return the persisted single query input."""
    return task_input.strip()
