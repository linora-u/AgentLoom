"""Read-only semantic and privacy audit for the real memory-review campaign."""

from __future__ import annotations

import argparse
import json
import math
import sys
from collections import Counter
from pathlib import Path
from typing import Any

_BOOTSTRAP_ROOT = Path(__file__).resolve().parents[3]
if str(_BOOTSTRAP_ROOT) not in sys.path:
    sys.path.insert(0, str(_BOOTSTRAP_ROOT))

from applications.memory_feature_validation.scripts.memory_review_campaign_common import (  # noqa: E402
    ORACLE_PATH,
    REPO_ROOT,
    RunSpec,
    find_privacy_markers,
    indexed_rows,
    normalize,
    select_runs,
    terms_match,
)

_HARD_CODES = {
    "application_failed",
    "application_scope_leak",
    "approval_transition",
    "database_integrity",
    "pending_recalled",
    "privacy_marker",
    "progress_persisted",
    "review_evidence_missing",
    "review_action_mismatch",
    "review_call_limit_exceeded",
    "review_failed",
    "review_off_called",
    "review_on_not_called",
    "scope_mismatch",
    "security_persisted",
    "unverified_claim_persisted",
}


def _write_text(path: Path, value: str) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(value, encoding="utf-8")
    temporary.replace(path)


def _write_json(path: Path, value: Any) -> None:
    _write_text(
        path,
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True, default=str) + "\n",
    )


def _issue(run_id: str, scenario: str, code: str, message: str) -> dict[str, Any]:
    return {
        "run_id": run_id,
        "scenario": scenario,
        "code": code,
        "hard": code in _HARD_CODES,
        "message": message,
    }


def _memory_rows(result: dict[str, Any]) -> list[dict[str, Any]]:
    database = (result.get("final") or {}).get("database") or {}
    value = [
        *(database.get("memory_items") or []),
        *(database.get("memory_pending_writes") or []),
    ]
    return [row for row in value if isinstance(row, dict)]


def _status(row: dict[str, Any]) -> str:
    return normalize(row.get("status"))


def _scope(row: dict[str, Any]) -> str:
    value = normalize(row.get("scope_type") or row.get("scope") or "")
    return "application" if value in {"application", "app"} else value


def _content(row: dict[str, Any]) -> str:
    return normalize(row.get("content") or "")


def _workflow_application_id(spec: RunSpec) -> str:
    workflow = (REPO_ROOT / spec.workflow).resolve()
    applications = (REPO_ROOT / "applications").resolve()
    if workflow.parent.name != "workflows":
        return ""
    try:
        return workflow.parent.parent.relative_to(applications).as_posix()
    except ValueError:
        return ""


def _matching_rows(rows: list[dict[str, Any]], required_terms: list[str]) -> list[dict[str, Any]]:
    return [row for row in rows if terms_match(_content(row), required_terms)]


def _answer(value: Any) -> str:
    if isinstance(value, dict):
        if "answer" in value:
            return str(value.get("answer") or "")
        for nested in value.values():
            candidate = _answer(nested)
            if candidate:
                return candidate
    if isinstance(value, list):
        for nested in value:
            candidate = _answer(nested)
            if candidate:
                return candidate
    if isinstance(value, str):
        return value
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return str(value)
    return ""


def _expected_missing(spec: RunSpec, oracle: dict[str, Any]) -> bool:
    if spec.phase == "pre_recall":
        return True
    if spec.phase == "post_recall":
        return str(oracle.get("decision") or "") == "reject"
    if spec.phase == "cross_recall" and spec.scenario == "application_scope":
        return True
    return False


def _review_audit_contract(
    spec: RunSpec,
    result: dict[str, Any],
    oracle_row: dict[str, Any],
) -> list[dict[str, Any]]:
    evidence = ((result.get("final") or {}).get("model_evidence") or {})
    audits = evidence.get("review_audit_delta") or []
    logged_calls = evidence.get("review_call_count")
    if not spec.review_expected:
        issues: list[dict[str, Any]] = []
        if audits:
            issues.append(
                _issue(
                    spec.run_id,
                    spec.scenario,
                    "review_off_called",
                    "review_model was empty but the run persisted review audit state",
                )
            )
        if isinstance(logged_calls, bool) or logged_calls != 0:
            issues.append(
                _issue(
                    spec.run_id,
                    spec.scenario,
                    "review_off_called",
                    "review_model was empty but zero provider calls were not proven",
                )
            )
        return issues

    if not isinstance(audits, list) or len(audits) != 1 or not isinstance(audits[0], dict):
        return [
            _issue(
                spec.run_id,
                spec.scenario,
                "review_evidence_missing",
                "Application did not persist exactly one review audit",
            )
        ]

    audit = audits[0]
    audit_result = audit.get("result") if isinstance(audit.get("result"), dict) else {}
    status = normalize(audit_result.get("status") or audit.get("status") or "")
    calls = audit_result.get("calls")
    actions = audit_result.get("actions")
    if (
        isinstance(calls, bool)
        or not isinstance(calls, int)
        or calls < 0
        or isinstance(actions, bool)
        or not isinstance(actions, int)
        or actions < 0
        or isinstance(logged_calls, bool)
        or not isinstance(logged_calls, int)
        or logged_calls != calls
    ):
        return [
            _issue(
                spec.run_id,
                spec.scenario,
                "review_evidence_missing",
                "review audit and runtime telemetry did not agree",
            )
        ]

    issues: list[dict[str, Any]] = []
    if calls > 4:
        issues.append(
            _issue(
                spec.run_id,
                spec.scenario,
                "review_call_limit_exceeded",
                "completed-run reviewer exceeded four provider requests",
            )
        )
    if status != "completed":
        issues.append(
            _issue(
                spec.run_id,
                spec.scenario,
                "review_failed",
                "configured reviewer did not finish successfully",
            )
        )
    elif calls < 1:
        issues.append(
            _issue(
                spec.run_id,
                spec.scenario,
                "review_on_not_called",
                "review_model was configured but reviewer did not call the model",
            )
        )

    expected_writer_status = normalize(oracle_row.get("expected_writer_status"))
    must_not_mutate = (
        spec.phase != "writer"
        or expected_writer_status == "absent"
    )
    if must_not_mutate and actions != 0:
        issues.append(
            _issue(
                spec.run_id,
                spec.scenario,
                "review_action_mismatch",
                "review audit recorded an unexpected memory mutation",
            )
        )
    elif not must_not_mutate and actions < 1:
        issues.append(
            _issue(
                spec.run_id,
                spec.scenario,
                "review_action_mismatch",
                "eligible reviewer writer did not record a memory mutation",
            )
        )
    return issues


def _evaluate_one(
    spec: RunSpec,
    result: dict[str, Any],
    oracle_row: dict[str, Any],
) -> list[dict[str, Any]]:
    issues: list[dict[str, Any]] = []
    run_id = spec.run_id
    scenario = spec.scenario
    final = result.get("final") or {}
    if result.get("status") != "completed" or int(final.get("returncode") or 0) != 0:
        issues.append(_issue(run_id, scenario, "application_failed", "real loom run did not complete"))
        return issues

    database = final.get("database") or {}
    if database.get("integrity") != "ok":
        issues.append(_issue(run_id, scenario, "database_integrity", "SQLite integrity_check was not ok"))
    if final.get("privacy_findings"):
        issues.append(_issue(run_id, scenario, "privacy_marker", "raw security marker reached runtime or state"))

    issues.extend(_review_audit_contract(spec, result, oracle_row))

    rows = _memory_rows(result)
    live_rows = [row for row in rows if _status(row) in {"active", "pending"}]
    required_terms = [str(value) for value in oracle_row.get("required_terms") or []]
    forbidden_terms = [normalize(value) for value in oracle_row.get("forbidden_terms") or []]

    if spec.phase == "writer":
        expected_status = normalize(oracle_row.get("expected_writer_status"))
        if expected_status == "absent":
            if live_rows:
                code = (
                    "progress_persisted"
                    if scenario == "review_on_progress"
                    else "security_persisted"
                    if scenario == "review_on_security"
                    else "unverified_claim_persisted"
                    if scenario == "review_on_unverified_claim"
                    else "unexpected_memory_write"
                )
                issues.append(_issue(run_id, scenario, code, "writer created active or pending memory unexpectedly"))
        else:
            matches = [row for row in _matching_rows(rows, required_terms) if _status(row) == expected_status]
            if not matches:
                issues.append(
                    _issue(
                        run_id,
                        scenario,
                        "expected_memory_missing",
                        f"no {expected_status} memory contained the independently required fact terms",
                    )
                )
            expected_scope = normalize(oracle_row.get("expected_scope"))
            scope_matches = [
                row for row in matches
                if not expected_scope or _scope(row) == expected_scope
            ]
            if matches and expected_scope and not scope_matches:
                issues.append(_issue(run_id, scenario, "scope_mismatch", "memory landed in the wrong scope"))
            elif expected_scope == "application":
                expected_application_id = _workflow_application_id(spec)
                if not expected_application_id or not any(
                    str(row.get("scope_id") or "") == expected_application_id
                    for row in scope_matches
                ):
                    issues.append(
                        _issue(
                            run_id,
                            scenario,
                            "scope_mismatch",
                            "Application memory did not use the writer workflow's Application id",
                        )
                    )
        live_content = "\n".join(_content(row) for row in live_rows)
        if any(term and terms_match(live_content, [term]) for term in forbidden_terms):
            code = "progress_persisted" if scenario in {"review_on_progress", "review_on_mixed_noise"} else "security_persisted"
            issues.append(_issue(run_id, scenario, code, "forbidden transient or unsafe content reached memory"))

    if spec.phase != "writer":
        answer = normalize(_answer(final.get("final_answer")))
        missing = _expected_missing(spec, oracle_row)
        if missing:
            if answer != "missing":
                code = "application_scope_leak" if scenario == "application_scope" else "pending_recalled"
                issues.append(_issue(run_id, scenario, code, "fact was visible when the snapshot should have omitted it"))
        else:
            recall_terms = oracle_row.get("recall_terms") or required_terms
            required = [str(term) for term in recall_terms]
            if not answer or answer == "missing" or not terms_match(answer, required):
                issues.append(_issue(run_id, scenario, "recall_miss", "active memory was not recalled with the required fact terms"))

    if spec.phase == "post_recall":
        transition = result.get("approval_transition") or {}
        target = str(transition.get("target") or "")
        decision = str(oracle_row.get("decision") or "")
        target_rows = [
            row
            for row in (database.get("memory_pending_writes") or [])
            if isinstance(row, dict) and str(row.get("id") or "") == target
        ]
        expected_status = "approved" if decision == "approve" else "rejected"
        persisted_transition = (
            len(target_rows) == 1
            and normalize(target_rows[0].get("status")) == expected_status
            and bool(target_rows[0].get("resolved_at"))
        )
        active_matches = [
            row
            for row in (database.get("memory_items") or [])
            if isinstance(row, dict)
            and terms_match(row.get("content") or "", required_terms)
        ]
        active_transition = bool(active_matches) if decision == "approve" else not active_matches
        if (
            transition.get("ok") is not True
            or transition.get("readback_ok") is not True
            or not persisted_transition
            or not active_transition
        ):
            issues.append(_issue(run_id, scenario, "approval_transition", "pending item was not approved or rejected through the target CLI"))
    return issues


def evaluate_results(
    specs: list[RunSpec],
    results: list[dict[str, Any]],
    oracle: dict[str, dict[str, Any]],
    *,
    require_complete: bool,
) -> dict[str, Any]:
    by_id = {str(result.get("run_id") or ""): result for result in results}
    issues: list[dict[str, Any]] = []
    outcomes: list[dict[str, Any]] = []
    for spec in specs:
        result = by_id.get(spec.run_id)
        if result is None:
            run_issues = [_issue(spec.run_id, spec.scenario, "missing_result", "planned Application has no result")]
        elif result.get("status") == "planned":
            run_issues = []
        else:
            run_issues = _evaluate_one(spec, result, oracle[spec.case_id])
        issues.extend(run_issues)
        outcomes.append(
            {
                "run_id": spec.run_id,
                "scenario": spec.scenario,
                "ok": not run_issues,
                "hard_failure": any(issue["hard"] for issue in run_issues),
            }
        )
    complete = len(by_id) == len(specs) and all(spec.run_id in by_id for spec in specs)
    return {
        "ok": (complete or not require_complete) and not issues,
        "complete": complete,
        "issues": issues,
        "outcomes": outcomes,
    }


def _load_specs(plan: dict[str, Any]) -> list[RunSpec]:
    rows = plan.get("runs") or []
    if not isinstance(rows, list):
        raise ValueError("plan.runs must be a list")
    return [RunSpec(**row) for row in rows]


def _plan_issues(plan: dict[str, Any]) -> list[str]:
    issues: list[str] = []
    try:
        requested = int(plan.get("requested_runs"))
        canonical = [spec.to_dict() for spec in select_runs(requested)]
    except Exception as exc:
        return [f"invalid requested_runs: {type(exc).__name__}"]
    if plan.get("runs") != canonical:
        issues.append("plan does not match the canonical committed dataset plan")
    if plan.get("cli_contract") != "loom run <workflow> --log-to-file":
        issues.append("campaign bypassed the production loom run CLI")
    if plan.get("memory_cli_contract") != ["list", "pending", "approve", "reject"]:
        issues.append("memory CLI contract is not list/pending/approve/reject")
    if requested == 100 and int(plan.get("max_concurrency") or 0) > 2:
        issues.append("release concurrency exceeded 2")
    return issues


def _first_attempt_gate(
    results: list[dict[str, Any]],
    *,
    selected_runs: int,
) -> bool:
    required = math.ceil(max(0, int(selected_runs)) * 0.95)
    completed = sum(
        1
        for result in results
        if result.get("status") == "completed"
        and len(result.get("attempts") or []) == 1
    )
    return completed >= required


def audit_campaign(campaign_dir: Path) -> dict[str, Any]:
    campaign_dir = campaign_dir.expanduser().resolve()
    plan = json.loads((campaign_dir / "plan.json").read_text(encoding="utf-8"))
    results_payload = json.loads((campaign_dir / "results.json").read_text(encoding="utf-8"))
    results = results_payload.get("results") if isinstance(results_payload, dict) else None
    if not isinstance(results, list):
        raise ValueError("results.json must contain a results list")
    plan_issues = _plan_issues(plan)
    specs = _load_specs(plan)
    oracle = indexed_rows(ORACLE_PATH)

    if plan.get("dry_run"):
        ok = not plan_issues and len(results) == len(specs) and all(
            result.get("status") == "planned" for result in results
        )
        audit = {
            "ok": ok,
            "status": "PLAN_PASS" if ok else "PLAN_FAIL",
            "plan_issues": plan_issues,
            "selected_runs": len(specs),
            "issues": [],
        }
    else:
        evaluated = evaluate_results(specs, results, oracle, require_complete=True)
        hard_issues = [issue for issue in evaluated["issues"] if issue["hard"]]
        passed = Counter()
        total = Counter()
        for outcome in evaluated["outcomes"]:
            total[outcome["scenario"]] += 1
            if outcome["ok"]:
                passed[outcome["scenario"]] += 1
        passed_count = sum(1 for outcome in evaluated["outcomes"] if outcome["ok"])
        required_total = math.ceil(len(specs) * 0.95)
        scenario_gates = {
            scenario: passed[scenario] >= math.ceil(count * 0.9)
            for scenario, count in total.items()
        }
        first_attempt = sum(
            1
            for result in results
            if result.get("status") == "completed"
            and len(result.get("attempts") or []) == 1
        )
        first_attempt_required = math.ceil(len(specs) * 0.95)
        first_attempt_gate = _first_attempt_gate(
            results,
            selected_runs=len(specs),
        )
        completion_gate = sum(result.get("status") == "completed" for result in results) >= math.ceil(len(specs) * 0.99)
        ok = bool(
            not plan_issues
            and evaluated["complete"]
            and not hard_issues
            and passed_count >= required_total
            and all(scenario_gates.values())
            and first_attempt_gate
            and completion_gate
        )
        audit = {
            "ok": ok,
            "status": (
                "RELEASE_PASS" if ok and int(plan.get("requested_runs") or 0) == 100
                else "CANARY_PASS" if ok
                else "RELEASE_FAIL" if int(plan.get("requested_runs") or 0) == 100
                else "CANARY_FAIL"
            ),
            "plan_issues": plan_issues,
            "selected_runs": len(specs),
            "passed_runs": passed_count,
            "required_passed_runs": required_total,
            "first_attempt_completed": first_attempt,
            "first_attempt_required": first_attempt_required,
            "first_attempt_gate": first_attempt_gate,
            "final_completed": sum(result.get("status") == "completed" for result in results),
            "scenario_passed": dict(passed),
            "scenario_total": dict(total),
            "scenario_gates": scenario_gates,
            "hard_issue_count": len(hard_issues),
            "issues": evaluated["issues"],
        }

    result_privacy_findings = [
        finding
        for result in results
        for finding in ((result.get("final") or {}).get("privacy_findings") or [])
    ]
    artifact_privacy_findings = find_privacy_markers([campaign_dir], oracle)
    privacy_findings = list(
        {
            (
                str(finding.get("path") or ""),
                str(finding.get("kind") or ""),
                str(finding.get("case_id") or ""),
            ): finding
            for finding in [*result_privacy_findings, *artifact_privacy_findings]
            if isinstance(finding, dict)
        }.values()
    )
    if privacy_findings and not any(
        issue.get("code") == "privacy_marker" for issue in audit.get("issues") or []
    ):
        audit.setdefault("issues", []).append(
            _issue(
                "campaign",
                "privacy_audit",
                "privacy_marker",
                "raw security marker reached a campaign artifact",
            )
        )
        if "hard_issue_count" in audit:
            audit["hard_issue_count"] = int(audit["hard_issue_count"]) + 1
    if privacy_findings:
        audit["ok"] = False
        requested = int(plan.get("requested_runs") or 0)
        audit["status"] = (
            "PLAN_FAIL"
            if plan.get("dry_run")
            else "RELEASE_FAIL"
            if requested == 100
            else "CANARY_FAIL"
        )
    _write_json(
        campaign_dir / "privacy_audit.json",
        {"ok": not privacy_findings, "finding_count": len(privacy_findings), "findings": privacy_findings},
    )
    _write_text(
        campaign_dir / "failure_cases.jsonl",
        "".join(json.dumps(issue, ensure_ascii=False, sort_keys=True) + "\n" for issue in audit.get("issues") or []),
    )
    results_by_id = {
        str(result.get("run_id") or ""): result
        for result in results
        if isinstance(result, dict)
    }
    specs_by_id = {spec.run_id: spec for spec in specs}
    reproduction_commands = []
    for run_id in dict.fromkeys(
        str(issue.get("run_id") or "") for issue in audit.get("issues") or []
    ):
        spec = specs_by_id.get(run_id)
        result = results_by_id.get(run_id) or {}
        final = result.get("final") if isinstance(result, dict) else {}
        final = final if isinstance(final, dict) else {}
        if spec is None:
            continue
        reproduction_commands.append(
            {
                "run_id": run_id,
                "cwd": str(REPO_ROOT),
                "env": {
                    "AGENTLOOM_SELF_LEARNING_ROOT": str(final.get("state_root") or "<fresh-state-root>"),
                    "AGENTLOOM_MEMORY_CASE_ID": spec.case_id,
                    "AGENTLOOM_MEMORY_CASE_PHASE": spec.phase,
                    "AGENTLOOM_MEMORY_CAMPAIGN_SAFE_ARTIFACTS": "1",
                },
                "command": ["loom", "run", spec.workflow, "--log-to-file"],
            }
        )
    _write_json(
        campaign_dir / "reproduction_commands.json",
        {"commands": reproduction_commands},
    )
    lines = [
        "# Memory Review Campaign Report",
        "",
        f"- status: {audit['status']}",
        f"- selected_runs: {audit['selected_runs']}",
        f"- privacy_findings: {len(privacy_findings)}",
    ]
    if "passed_runs" in audit:
        lines.extend(
            [
                f"- passed_runs: {audit['passed_runs']}/{audit['selected_runs']}",
                f"- first_attempt_completed: {audit['first_attempt_completed']}/{audit['first_attempt_required']}",
                f"- first_attempt_gate: {audit['first_attempt_gate']}",
                f"- final_completed: {audit['final_completed']}",
                f"- hard_issues: {audit['hard_issue_count']}",
            ]
        )
    lines.extend(["", "## Issues", ""])
    if plan_issues:
        lines.extend(f"- [plan] {message}" for message in plan_issues)
    if audit.get("issues"):
        lines.extend(
            f"- [{issue['code']}] {issue['run_id']}: {issue['message']}"
            for issue in audit["issues"]
        )
    if not plan_issues and not audit.get("issues"):
        lines.append("- none")
    _write_text(campaign_dir / "report.md", "\n".join(lines) + "\n")
    return audit


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("campaign_dir", type=Path)
    args = parser.parse_args()
    audit = audit_campaign(args.campaign_dir)
    print(json.dumps({"ok": audit["ok"], "status": audit["status"]}, sort_keys=True))
    return 0 if audit["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
