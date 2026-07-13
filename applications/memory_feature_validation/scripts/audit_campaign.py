"""Audit a memory summary campaign without invoking any model."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import sqlite3
import sys
import time
from collections import Counter, defaultdict
from collections.abc import Iterable
from datetime import datetime
from pathlib import Path
from typing import Any

import yaml

REPO_ROOT = Path(__file__).resolve().parents[3]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from applications.memory_feature_validation.scripts.campaign_common import (  # noqa: E402
    APP_ROOT,
    HIGH_OVERLAP_FACTS,
    HIGH_OVERLAP_PAIRS,
    INJECTION_PROBES,
    SCENARIO_ORDER,
    SECRET_PROBES,
    probe_present,
    retryable_scan_error_attempt,
    secret_probe_present,
    validated_artifact_scan_errors,
)

_TERMINAL = {"succeeded", "dead"}
_CORROBORATION_FACT = (
    "The nightly export job must start after 02:00 UTC because upstream data only lands at 01:45 UTC"
)
_SAME_RUN_FACT = "The staging cluster only accepts deploys tagged with a release ticket id"
_FINAL_ONLY_FACT = (
    "The validation export endpoint requires UTF-8 CSV files with a header row"
)
_SESSION_NOTE_FACTS = (
    "learned: the report generator emits a UTF-8 BOM that breaks the downstream CSV parser; "
    "strip the BOM before upload",
    "learned: upstream 502 errors from the metrics API are transient; three retries with "
    "exponential backoff always succeeded",
    "progress: finished step 3 of 5 of today's pipeline debugging, resuming tomorrow",
)
_MAX_CAMPAIGN_SECONDS = 8 * 60 * 60
_PUBLIC_STATUSES = (
    "DRY_RUN_PASS",
    "DRY_RUN_FAIL",
    "RELEASE_PASS",
    "RELEASE_FAIL",
    "CANARY_PASS",
    "CANARY_FAIL",
)
_PUBLIC_ISSUE_CODES = (
    "hard_zero_failure",
    "count_mismatch",
    "release_shape",
    "case_identity",
    "concurrency",
    "retry_policy",
    "deadline",
    "cli_contract",
    "model_contract",
    "canary_order",
    "scenario_quota",
    "workflow_missing",
    "model_type",
    "state_isolation",
    "conflict_shapes",
    "worker_model_type",
    "distill_model",
    "case_failure",
    "first_attempt_rate",
    "final_completion_rate",
    "semantic_pass_rate",
    "scenario_pass_rate",
    "stuck_jobs",
    "sqlite_integrity",
    "llm_distillation_rate",
    "session_end_measurement",
    "session_end_p95",
    "session_end_p99",
    "privacy",
    "canary_gate",
    "dry_run_execution",
    "campaign_deadline",
)


def _hard_violation_code(scenario: str, issue: str) -> str:
    """Map only an explicit release hard-gate violation to a stable code."""
    scenario = str(scenario or "")
    issue = str(issue or "")
    common_rules = (
        ("live .agentloom/self_learning.db changed", "state_isolation"),
        ("runtime root reused", "state_isolation"),
        ("root_run_id was reused across cases", "session_cross_run"),
        ("same learning job was counted for multiple runs", "duplicate_job_effect"),
        ("attempt artifact privacy scan failed", "privacy_boundary"),
        ("pre-checkpoint state privacy scan", "privacy_boundary"),
        ("result metadata does not match canonical plan", "case_identity"),
        ("retry was not authorized", "retry_policy"),
        ("attempt sequence is invalid", "retry_policy"),
        ("attempt failure_kind does not match", "retry_policy"),
    )
    for marker, code in common_rules:
        if marker in issue:
            return code
    scenario_rules = {
        "recall_isolation": (
            ("foreign application memory leaked", "scope_isolation"),
        ),
        "exact_corroboration": (
            ("evidence roots did not match selected cohort roots", "session_cross_run"),
            ("exact proposal was not pending", "same_run_false_evidence"),
        ),
        "feedback_revision": (
            ("late old-revision outcome/feedback changed", "revision_identity"),
            ("replace did not create an immutable", "revision_identity"),
        ),
        "capacity_atomic_batch": (
            ("failed batch did not roll back", "batch_atomicity"),
            ("failed over-capacity batch partially committed", "batch_atomicity"),
        ),
        "root_run_attribution": (
            ("not attributed to one root run", "session_cross_run"),
            ("foreign or missing root_run_id", "session_cross_run"),
            ("distinct local run_id", "session_cross_run"),
            ("owner/root event identity", "session_cross_run"),
            ("not emitted by the root owner", "session_cross_run"),
            ("final SessionEnd events instead of one", "session_cross_run"),
        ),
        "injection_boundary": (
            ("fragment was not blocked", "injection_boundary"),
        ),
        "recursive_redaction": (
            ("contains no evidence that nested secrets were redacted", "redaction_boundary"),
        ),
        "same_run_fake_corroboration": (
            ("incorrectly corroborated or applied", "same_run_false_evidence"),
            ("same run was auto-applied", "same_run_false_evidence"),
        ),
        "high_overlap_conflict": (
            ("did not remain separate", "conflicting_fact_auto_apply"),
            ("corroborated or auto-applied", "conflicting_fact_auto_apply"),
        ),
    }
    for marker, code in scenario_rules.get(scenario, ()):
        if marker in issue:
            return code
    return ""


def _hard_violation_codes(scenario: str, issues: Iterable[Any]) -> list[str]:
    return sorted(
        {
            code
            for issue in issues
            for code in [_hard_violation_code(scenario, str(issue))]
            if code
        }
    )


def _read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _issue(issues: list[dict[str, str]], code: str, message: str) -> None:
    issues.append({"code": code, "message": message})


def _public_status(value: Any) -> str:
    for status in _PUBLIC_STATUSES:
        if value == status:
            return status
    return "INVALID"


def _public_issue_codes(issues: Any) -> list[str]:
    codes: list[str] = []
    for issue in issues if isinstance(issues, list) else []:
        raw_code = issue.get("code") if isinstance(issue, dict) else None
        public_code = next(
            (code for code in _PUBLIC_ISSUE_CODES if raw_code == code),
            "unknown_issue",
        )
        if public_code not in codes:
            codes.append(public_code)
    return codes


def _public_report_view(report: dict[str, Any]) -> dict[str, Any]:
    issue_codes = _public_issue_codes(report.get("issues"))
    return {
        "status": _public_status(report.get("status")),
        "ok": True if report.get("ok") is True else False,
        "release_eligible": True
        if report.get("release_eligible") is True
        else False,
        "issue_count": len(report.get("issues") or []),
        "issue_codes": issue_codes,
    }


def _hard_zero_issues(failures: list[dict[str, Any]]) -> list[dict[str, str]]:
    hard: list[tuple[dict[str, Any], list[str]]] = []
    for failure in failures:
        codes = [str(code) for code in failure.get("violation_codes") or [] if str(code)]
        if not codes:
            codes = _hard_violation_codes(
                str(failure.get("scenario") or ""),
                failure.get("issues") or [],
            )
        if codes:
            hard.append((failure, codes))
    if not hard:
        return []
    return [
        {
            "code": "hard_zero_failure",
            "message": (
                f"{len(hard)} zero-tolerance case(s) failed: "
                + ", ".join(
                    f"{item.get('case_id')}({','.join(codes)})"
                    for item, codes in hard[:10]
                )
            ),
        }
    ]


def _campaign_status(*, dry_run: bool, selected_runs: int, ok: bool) -> tuple[str, bool]:
    """Return an unambiguous campaign status and release eligibility."""
    release_gate_run = not dry_run and int(selected_runs) == 100
    if dry_run:
        status = "DRY_RUN_PASS" if ok else "DRY_RUN_FAIL"
    elif release_gate_run:
        status = "RELEASE_PASS" if ok else "RELEASE_FAIL"
    else:
        status = "CANARY_PASS" if ok else "CANARY_FAIL"
    return status, bool(release_gate_run and ok)


def _finalize_campaign_timing(
    campaign_dir: Path,
    *,
    campaign_started_monotonic: float | None,
) -> float:
    """Freeze elapsed time after the expensive final audit has completed.

    A standalone re-audit reuses the persisted duration instead of measuring
    wall time since the original campaign. The in-process runner supplies its
    monotonic start so privacy, integrity, semantic, and artifact audits are
    included in the eight-hour gate.
    """
    timing_path = campaign_dir / "campaign_timing.json"
    timing = _read_json(timing_path) if timing_path.exists() else {}
    if campaign_started_monotonic is None:
        return float(timing.get("elapsed_seconds") or math.inf)

    elapsed_seconds = max(0.0, time.monotonic() - campaign_started_monotonic)
    timing.update(
        {
            "finished_at": datetime.now().astimezone().isoformat(),
            "elapsed_seconds": elapsed_seconds,
            "within_eight_hours": elapsed_seconds <= _MAX_CAMPAIGN_SECONDS,
        }
    )
    temporary = timing_path.with_name(f".{timing_path.name}.tmp")
    temporary.write_text(
        json.dumps(timing, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    temporary.replace(timing_path)
    return elapsed_seconds


def _selected_attempt(result: dict[str, Any]) -> dict[str, Any]:
    attempts = result.get("attempts") or []
    return attempts[-1] if attempts else {}


def _selected_value(result: dict[str, Any], key: str) -> Any:
    """Read a duplicated result field from the selected attempt first."""
    selected = _selected_attempt(result)
    return selected[key] if key in selected else result.get(key)


def _normalized_result_path(value: Any) -> str:
    text = str(value or "").strip()
    return str(Path(text).expanduser().resolve()) if text else ""


def _selected_attempt_consistency_issues(result: dict[str, Any]) -> list[str]:
    """Reject a stale top-level view after a clean-state infrastructure retry."""
    selected = _selected_attempt(result)
    if not selected:
        return []
    issues: list[str] = []
    if (
        "selected_attempt" in result
        and int(result.get("selected_attempt") or 0)
        != int(selected.get("attempt") or 0)
    ):
        issues.append("top-level selected_attempt does not match the selected attempt")
    for key in ("self_learning_root", "runtime_root"):
        if key in result and _normalized_result_path(
            result.get(key)
        ) != _normalized_result_path(selected.get(key)):
            issues.append(f"top-level {key} does not match the selected attempt")
    for key in ("job_wait", "final_answer"):
        if key in result and result.get(key) != selected.get(key):
            issues.append(f"top-level {key} does not match the selected attempt")
    return issues


_CANONICAL_RESULT_FIELDS = (
    "case_id",
    "scenario",
    "ordinal",
    "workflow",
    "state_key",
    "cohort_id",
    "phase",
    "env",
    "canary_rank",
    "seed",
)


def _canonical_result_issues(
    case: dict[str, Any],
    result: dict[str, Any],
) -> list[str]:
    mismatched = [
        key
        for key in _CANONICAL_RESULT_FIELDS
        if result.get(key) != case.get(key)
    ]
    if not mismatched:
        return []
    return [
        "result metadata does not match canonical plan: "
        + ", ".join(mismatched)
    ]


def _audit_attempt_failure_kind(attempt: dict[str, Any]) -> str:
    """Independently classify one persisted attempt without trusting its enum."""
    if (attempt.get("isolation_evidence") or {}).get("live_db_unchanged") is False:
        return "semantic_or_code"
    scan_errors = validated_artifact_scan_errors(attempt.get("artifact_scan"))
    if scan_errors is None:
        return "semantic_or_code"
    if attempt.get("deadline_exceeded"):
        return "deadline"
    wait = attempt.get("job_wait")
    if not isinstance(wait, dict):
        return "semantic_or_code"
    jobs = wait.get("jobs")
    if not isinstance(jobs, list):
        return "semantic_or_code"
    execution_kind = str(
        (attempt.get("transport_evidence") or {}).get("kind") or ""
    )
    explicit_semantic = execution_kind == "semantic_or_code"
    explicit_infrastructure = execution_kind == "infrastructure" or any(
        isinstance(job, dict) and job.get("error_kind") == "infrastructure"
        for job in jobs
    )
    if attempt.get("timed_out"):
        if explicit_semantic:
            return "semantic_or_code"
        return "infrastructure" if explicit_infrastructure else "semantic_or_code"
    if type(attempt.get("returncode")) is not int or attempt.get("returncode") != 0:
        if explicit_semantic:
            return "semantic_or_code"
        return "infrastructure" if explicit_infrastructure else "semantic_or_code"
    if any(
        isinstance(job, dict) and job.get("artifact_delivery") == "failed"
        for job in jobs
    ):
        return "semantic_or_code"
    review_jobs = [
        job
        for job in jobs
        if isinstance(job, dict) and job.get("kind") == "session_review"
    ]
    if any(
        job.get("status") == "dead" and job.get("error_kind") != "infrastructure"
        for job in review_jobs
    ):
        return "semantic_or_code"
    if any(
        job.get("status") == "dead" and job.get("error_kind") == "infrastructure"
        for job in review_jobs
    ):
        return "infrastructure"
    if wait.get("terminal") is not True:
        if explicit_infrastructure or wait.get("read_errors"):
            return "infrastructure"
        return "semantic_or_code"
    if not review_jobs:
        return "semantic_or_code"
    if wait.get("read_errors"):
        return "infrastructure"
    if scan_errors:
        return (
            "infrastructure"
            if retryable_scan_error_attempt(attempt)
            else "semantic_or_code"
        )
    return ""


def _first_attempt_completed(attempt: dict[str, Any]) -> bool:
    """Count only a clean first attempt proven from persisted evidence."""
    jobs = (attempt.get("job_wait") or {}).get("jobs") or []
    review_jobs = [
        job
        for job in jobs
        if isinstance(job, dict) and job.get("kind") == "session_review"
    ]
    return bool(
        _audit_attempt_failure_kind(attempt) == ""
        and str(attempt.get("failure_kind") or "") == ""
        and type(attempt.get("returncode")) is int
        and attempt.get("returncode") == 0
        and (attempt.get("job_wait") or {}).get("terminal") is True
        and len(review_jobs) == 1
        and review_jobs[0].get("status") == "succeeded"
        and review_jobs[0].get("artifact_delivery") == "delivered"
        and not bool((attempt.get("worker_recovery") or {}).get("required"))
    )


def _logged_final_answer(result: dict[str, Any]) -> Any:
    value = _selected_value(result, "final_answer")
    if value is not None:
        return value
    log_path = Path(str(_selected_attempt(result).get("log_path") or ""))
    if not log_path.exists():
        return None
    for line in reversed(log_path.read_text(encoding="utf-8", errors="replace").splitlines()):
        candidate = line.strip()
        if not candidate or candidate[:1] not in {"{", "[", '"'}:
            continue
        try:
            decoded = json.loads(candidate)
        except json.JSONDecodeError:
            continue
        if isinstance(decoded, str):
            try:
                decoded = json.loads(decoded)
            except json.JSONDecodeError:
                pass
        if isinstance(decoded, (dict, list)):
            return decoded
    return None


def _percentile(values: list[float], percentile: float) -> float:
    if not values:
        return math.inf
    ordered = sorted(values)
    rank = max(0, math.ceil(percentile * len(ordered)) - 1)
    return float(ordered[rank])


def _scan_files(
    paths: Iterable[Path],
    probes: dict[str, str],
    *,
    injection: bool = False,
) -> list[dict[str, Any]]:
    findings: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for path in paths:
        if not path.is_file():
            continue
        try:
            data = path.read_bytes()
        except OSError as exc:
            findings.append(
                {
                    "path": str(path),
                    "kind": "scan_error",
                    "scope": "artifact_bytes",
                    "error_type": type(exc).__name__,
                }
            )
            continue
        binary_container = path.name.endswith((".db", "-wal", "-shm"))
        for label, probe in probes.items():
            present = (
                probe_present(data, probe, injection=True)
                if injection
                else secret_probe_present(
                    data,
                    label,
                    probe,
                    structured_only=binary_container,
                )
            )
            if not present:
                continue
            key = (str(path), label)
            if key in seen:
                continue
            seen.add(key)
            findings.append(
                {
                    "path": str(path),
                    "probe_label": label,
                }
            )
        if path.suffix.casefold() == ".db" and not injection:
            # Reuse the attempt-time logical scanner. Raw SQLite pages use a
            # structural-only rule for the collision-prone three-byte fixture;
            # decoded TEXT/FTS values must still be scanned exactly on re-audit.
            from applications.memory_feature_validation.scripts.run_summary_campaign import (
                _inspect_sqlite_logical_text,
            )

            findings.extend(
                finding
                for finding in _inspect_sqlite_logical_text(
                    path,
                    display_path=str(path),
                )
                if finding.get("kind") in {"secret", "scan_error"}
                and (
                    finding.get("kind") == "scan_error"
                    or str(finding.get("probe_label") or "") in probes
                )
            )
    return findings


def _posthoc_rewrite_findings(results: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Reject legacy campaigns that became green only after mutating artifacts."""
    findings: list[dict[str, Any]] = []
    for result in results:
        for attempt in result.get("attempts") or []:
            legacy = attempt.get("log_redaction")
            if not isinstance(legacy, dict):
                continue
            replacements = int(legacy.get("replacement_count") or 0)
            files_changed = int(legacy.get("files_changed") or 0)
            if replacements or files_changed:
                findings.append(
                    {
                        "case_id": str(result.get("case_id") or ""),
                        "attempt": int(attempt.get("attempt") or 0),
                        "files_changed": files_changed,
                        "replacement_count": replacements,
                    }
                )
    return findings


def _all_files(root: Path) -> list[Path]:
    return sorted(path for path in root.rglob("*") if path.is_file())


def _state_files(root: Path) -> list[Path]:
    state_dirs = ("state", "retry_state", "pre_attempt_state")
    return sorted(
        path
        for name in state_dirs
        for base in [root / name]
        if base.exists()
        for path in base.rglob("*")
        if path.is_file()
    )


def _db_rows(db_path: Path, query: str, params: tuple[Any, ...] = ()) -> list[sqlite3.Row]:
    if not db_path.exists():
        return []
    try:
        with sqlite3.connect(f"file:{db_path}?mode=ro", uri=True) as conn:
            conn.row_factory = sqlite3.Row
            return conn.execute(query, params).fetchall()
    except sqlite3.Error:
        return []


def _sqlite_integrity_audit(state_roots: Iterable[Path]) -> dict[str, Any]:
    """Fail closed on integrity or foreign-key damage in every used state DB."""
    findings: list[dict[str, Any]] = []
    roots = sorted({Path(root).expanduser().resolve() for root in state_roots})
    for root in roots:
        db_path = root / "self_learning.db"
        state_id = hashlib.sha256(str(root).encode("utf-8")).hexdigest()[:16]
        if not db_path.is_file():
            findings.append({"state_id": state_id, "kind": "missing_database"})
            continue
        try:
            uri = db_path.as_uri() + "?mode=ro&cache=private"
            with sqlite3.connect(uri, uri=True) as conn:
                conn.execute("PRAGMA query_only=ON")
                integrity_rows = conn.execute("PRAGMA integrity_check").fetchall()
                foreign_key_rows = conn.execute("PRAGMA foreign_key_check").fetchall()
            integrity_ok = (
                len(integrity_rows) == 1
                and str(integrity_rows[0][0] or "").casefold() == "ok"
            )
            if not integrity_ok:
                findings.append(
                    {
                        "state_id": state_id,
                        "kind": "integrity_violation",
                        "result_rows": len(integrity_rows),
                    }
                )
            if foreign_key_rows:
                findings.append(
                    {
                        "state_id": state_id,
                        "kind": "foreign_key_violation",
                        "result_rows": len(foreign_key_rows),
                    }
                )
        except (OSError, sqlite3.Error) as exc:
            findings.append(
                {
                    "state_id": state_id,
                    "kind": "sqlite_error",
                    "error_type": type(exc).__name__,
                }
            )
    return {
        "passed": not findings,
        "checked": len(roots),
        "finding_count": len(findings),
        "findings": findings,
    }


def _job_distilled_by(result_json: str) -> str:
    try:
        value = json.loads(result_json or "{}")
    except json.JSONDecodeError:
        return ""

    def find(current: Any) -> str:
        if isinstance(current, dict):
            if current.get("distilled_by"):
                return str(current["distilled_by"])
            for child in current.values():
                found = find(child)
                if found:
                    return found
        elif isinstance(current, list):
            for child in current:
                found = find(child)
                if found:
                    return found
        return ""

    return find(value)


def _static_audit(plan: dict[str, Any], results: list[dict[str, Any]]) -> list[dict[str, str]]:
    issues: list[dict[str, str]] = []
    cases = plan.get("cases") or []
    requested_runs = int(plan.get("requested_runs") or 0)
    selected_runs = int(plan.get("selected_runs") or 0)
    if selected_runs != len(cases) or len(results) != len(cases):
        _issue(issues, "count_mismatch", "plan, selected_runs, and results must have identical counts")
    release_shape = (requested_runs, selected_runs, len(cases), len(results))
    if 100 in release_shape and release_shape != (100, 100, 100, 100):
        _issue(
            issues,
            "release_shape",
            "release requires requested_runs, selected_runs, plan cases, and results to all equal 100",
        )
    case_ids = [str(case.get("case_id") or "") for case in cases]
    result_ids = [str(result.get("case_id") or "") for result in results]
    if (
        not all(case_ids)
        or not all(result_ids)
        or any(count != 1 for count in Counter(case_ids).values())
        or any(count != 1 for count in Counter(result_ids).values())
        or Counter(case_ids) != Counter(result_ids)
    ):
        _issue(
            issues,
            "case_identity",
            "plan and results must contain the same non-empty case ids exactly once",
        )
    canonical_cases = {
        str(case.get("case_id") or ""): case
        for case in cases
        if str(case.get("case_id") or "")
    }
    canonical_mismatches = [
        (str(result.get("case_id") or ""), mismatch)
        for result in results
        if (case := canonical_cases.get(str(result.get("case_id") or ""))) is not None
        for mismatch in _canonical_result_issues(case, result)
    ]
    if canonical_mismatches:
        _issue(
            issues,
            "case_identity",
            "; ".join(
                f"{case_id}: {mismatch}"
                for case_id, mismatch in canonical_mismatches[:10]
            ),
        )
    if int(plan.get("max_concurrency") or 0) > 2:
        _issue(issues, "concurrency", "campaign concurrency must not exceed 2")
    if int(plan.get("infrastructure_retries") or -1) != 1:
        _issue(issues, "retry_policy", "campaign must allow exactly one infrastructure retry")
    if int(plan.get("deadline_seconds") or 0) <= 0 or int(plan.get("deadline_seconds") or 0) > _MAX_CAMPAIGN_SECONDS:
        _issue(issues, "deadline", "campaign deadline must be positive and no more than eight hours")
    if plan.get("cli_contract") != "loom run <workflow> --log-to-file":
        _issue(issues, "cli_contract", "real executions must use the loom run CLI")
    model_contract = plan.get("model_contract") or {}
    for key in (
        "application_requested_type",
        "application_resolved_type",
        "distiller_requested_type",
        "distiller_resolved_type",
    ):
        if model_contract.get(key) != "summary":
            _issue(issues, "model_contract", f"{key} is not summary")
    if not model_contract.get("application_model_id") or not model_contract.get("distiller_model_id"):
        _issue(issues, "model_contract", "resolved summary model ids are missing")

    canary_ids = list(plan.get("canary_case_ids") or [])
    expected_canaries = [case.get("case_id") for case in cases[: min(5, len(cases))]]
    if canary_ids != expected_canaries:
        _issue(issues, "canary_order", "the first five selected executions must be the declared canaries")

    quotas = Counter(str(case.get("scenario")) for case in cases)
    if requested_runs == 100:
        expected = {scenario: 10 for scenario in SCENARIO_ORDER}
        if dict(quotas) != expected:
            _issue(issues, "scenario_quota", f"expected ten runs per scenario, got {dict(quotas)}")

    state_keys: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for case in cases:
        state_keys[str(case.get("state_key"))].append(case)
        workflow = REPO_ROOT / str(case.get("workflow") or "")
        if not workflow.exists():
            _issue(issues, "workflow_missing", f"missing workflow: {workflow}")
            continue
        parsed = yaml.safe_load(workflow.read_text(encoding="utf-8")) or {}
        if parsed.get("model_type") != "summary":
            _issue(issues, "model_type", f"workflow is not summary: {workflow}")
    for state_key, members in state_keys.items():
        if len(members) == 1:
            continue
        cohort_ids = {str(member.get("cohort_id") or "") for member in members}
        phases = {int(member.get("phase") or 0) for member in members}
        scenarios = {str(member.get("scenario") or "") for member in members}
        if (
            len(members) != 2
            or "" in cohort_ids
            or len(cohort_ids) != 1
            or phases != {1, 2}
            or not scenarios <= {"exact_corroboration", "high_overlap_conflict"}
        ):
            _issue(issues, "state_isolation", f"non-pair cases share state key {state_key}")

    if requested_runs == 100:
        variants = {
            str((case.get("env") or {}).get("AGENTLOOM_MEMORY_VALIDATION_VARIANT") or "")
            for case in cases
            if case.get("scenario") == "high_overlap_conflict"
        }
        if variants != set(HIGH_OVERLAP_FACTS):
            _issue(issues, "conflict_shapes", f"high-overlap variants incomplete: {sorted(variants)}")

    worker = APP_ROOT / "workflows" / "worker_agents" / "note_taker.yaml"
    if (yaml.safe_load(worker.read_text(encoding="utf-8")) or {}).get("model_type") != "summary":
        _issue(issues, "worker_model_type", "note_taker worker must use summary")
    app_config = yaml.safe_load((APP_ROOT / "config" / "system.yaml").read_text(encoding="utf-8")) or {}
    distill_model = (
        app_config.get("self_learning", {}).get("memory", {}).get("distill_model")
    )
    if distill_model != "summary":
        _issue(issues, "distill_model", "application distill_model must be summary")
    return issues


def _memory_rows(root: Path, content: str) -> list[sqlite3.Row]:
    return _db_rows(
        root / "self_learning.db",
        "SELECT * FROM memory_items WHERE content = ? ORDER BY id",
        (content,),
    )


def _evidence_count(root: Path, item_id: int) -> int:
    rows = _db_rows(
        root / "self_learning.db",
        "SELECT COUNT(DISTINCT root_run_id) AS n FROM memory_evidence WHERE item_id = ?",
        (int(item_id),),
    )
    return int(rows[0]["n"] or 0) if rows else 0


def _evidence_root_runs(root: Path, item_id: int) -> set[str]:
    rows = _db_rows(
        root / "self_learning.db",
        "SELECT DISTINCT root_run_id FROM memory_evidence "
        "WHERE item_id = ? ORDER BY root_run_id",
        (int(item_id),),
    )
    return {str(row["root_run_id"]) for row in rows}


def _cohort_phase_root_runs(
    results: Iterable[dict[str, Any]],
) -> dict[str, dict[int, str]]:
    phase_roots: dict[str, dict[int, str]] = defaultdict(dict)
    for result in results:
        cohort_id = str(result.get("cohort_id") or "")
        phase = int(result.get("phase") or 0)
        root_run_id = str(_selected_attempt(result).get("root_run_id") or "")
        if cohort_id and phase in {1, 2} and root_run_id:
            phase_roots[cohort_id][phase] = root_run_id
    return dict(phase_roots)


def _has_llm_session_review(root: Path) -> bool:
    rows = _db_rows(
        root / "self_learning.db",
        "SELECT status, result_json FROM learning_jobs WHERE kind = 'session_review' ORDER BY id DESC",
    )
    return any(
        row["status"] == "succeeded"
        and _job_distilled_by(str(row["result_json"] or "")) == "llm"
        for row in rows
    )


def _session_review_rows(root: Path, root_run_id: str = "") -> list[sqlite3.Row]:
    where = "WHERE kind = 'session_review'"
    params: tuple[Any, ...] = ()
    if root_run_id:
        where += " AND root_run_id = ?"
        params = (root_run_id,)
    return _db_rows(
        root / "self_learning.db",
        "SELECT * FROM learning_jobs " + where + " ORDER BY id",
        params,
    )


def _json_object(value: Any) -> dict[str, Any]:
    try:
        parsed = json.loads(str(value or "{}"))
    except (TypeError, json.JSONDecodeError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _distill_result(row: sqlite3.Row) -> dict[str, Any]:
    result = _json_object(row["result_json"])
    distill = result.get("distill")
    return distill if isinstance(distill, dict) else {}


def _prepared_evidence_refs(row: sqlite3.Row) -> set[str]:
    payload = _json_object(row["payload_json"])
    prepared = payload.get("prepared_digest")
    if not isinstance(prepared, dict) or not isinstance(prepared.get("evidence_refs"), list):
        return set()
    return {
        str(ref)
        for ref in prepared["evidence_refs"]
        if isinstance(ref, str) and ref
    }


def _prepared_fragments(row: sqlite3.Row) -> list[dict[str, Any]]:
    payload = _json_object(row["payload_json"])
    prepared = payload.get("prepared_digest")
    if not isinstance(prepared, dict):
        return []
    try:
        digest = json.loads(str(prepared.get("text") or ""))
    except json.JSONDecodeError:
        return []
    fragments = digest.get("fragments") if isinstance(digest, dict) else None
    return [item for item in (fragments or []) if isinstance(item, dict)]


def _normalized_fact(value: Any) -> str:
    return " ".join(str(value or "").casefold().split())


def _async_source_issues(
    root: Path,
    root_run_id: str,
    workflow: str,
    review_row: sqlite3.Row,
) -> list[str]:
    """Independently tie an async proposal to its frozen source fragment."""
    issues: list[str] = []
    fragments = _prepared_fragments(review_row)
    evidence_refs = _prepared_evidence_refs(review_row)
    proposal_rows = _db_rows(
        root / "self_learning.db",
        "SELECT id, content, status, source, source_run_id FROM memory_items "
        "WHERE source = 'llm_distill' AND source_run_id = ? ORDER BY id",
        (root_run_id,),
    )
    for proposal in proposal_rows:
        if str(proposal["status"]) != "pending":
            issues.append("single-run LLM proposal was not left pending")
            break
        if _evidence_count(root, int(proposal["id"])) != 1:
            issues.append("single-run LLM proposal did not retain exactly one root-run evidence row")
            break

    if workflow.endswith("mem_final_only_agent.yaml"):
        fragment = next(
            (item for item in fragments if item.get("ref") == "run.final_answer"),
            None,
        )
        if (
            not fragment
            or fragment.get("kind") != "final_answer"
            or fragment.get("blocked") is not False
            or _FINAL_ONLY_FACT not in str(fragment.get("text") or "")
            or "run.final_answer" not in evidence_refs
        ):
            issues.append("final-only fact was not preserved as an unblocked run.final_answer evidence ref")
        if len(proposal_rows) != 1 or str(proposal_rows[0]["content"]) != _FINAL_ONLY_FACT:
            issues.append("final-only LLM proposal did not exactly preserve the expected durable fact")
        return issues

    if workflow.endswith("mem_session_notes_agent.yaml"):
        note_fragments = [
            item
            for item in fragments
            if item.get("kind") == "session_note"
            and str(item.get("ref") or "").startswith("session_note:")
        ]
        note_texts = {_normalized_fact(item.get("text")) for item in note_fragments}
        expected_texts = {_normalized_fact(value) for value in _SESSION_NOTE_FACTS}
        note_refs = {str(item.get("ref")) for item in note_fragments}
        if (
            len(note_fragments) != len(_SESSION_NOTE_FACTS)
            or note_texts != expected_texts
            or any(item.get("blocked") is not False for item in note_fragments)
            or not note_refs <= evidence_refs
        ):
            issues.append("session notes were not frozen as exact unblocked session_note evidence refs")

        proposal_texts = [_normalized_fact(row["content"]) for row in proposal_rows]
        required_terms = ("utf-8 bom", "downstream csv parser", "strip the bom", "upload")
        if not any(all(term in content for term in required_terms) for content in proposal_texts):
            issues.append("session-note LLM proposals did not preserve the durable BOM remediation")
        progress_marker = _normalized_fact(_SESSION_NOTE_FACTS[2])
        if any(
            progress_marker in content
            or ("step 3 of 5" in content and "resuming tomorrow" in content)
            for content in proposal_texts
        ):
            issues.append("transient progress note was incorrectly promoted to durable memory")
        return issues

    issues.append(f"unknown async-distillation workflow: {workflow}")
    return issues


def _snapshot_probe_findings(root: Path, root_run_id: str) -> list[str]:
    """Regenerate the real model-facing snapshot and return only probe labels."""
    db_path = root / "self_learning.db"
    if not db_path.exists() or not root_run_id:
        return ["snapshot_not_generated"]
    try:
        from src.extensions.self_learning.memory_store import MemoryStore

        snapshot = MemoryStore(db_path).snapshot_for_prompt(
            application_id="memory_feature_validation",
            session_run_id=root_run_id,
            task_text="memory validation audit",
            record_usage=False,
        )
    except Exception as exc:
        return [f"snapshot_error:{type(exc).__name__}"]
    findings = [
        f"secret:{label}"
        for label, probe in SECRET_PROBES.items()
        if probe in snapshot
    ]
    findings.extend(
        f"injection:{label}"
        for label, probe in INJECTION_PROBES.items()
        if probe.casefold() in snapshot.casefold()
    )
    return findings


def _semantic_case_issues(
    result: dict[str, Any],
    cohort_final_roots: dict[str, Path],
    *,
    campaign_dir: Path,
    cohort_phase_root_runs: dict[str, dict[int, str]] | None = None,
) -> list[str]:
    scenario = str(result.get("scenario") or "")
    result_root = Path(str(_selected_value(result, "self_learning_root") or ""))
    root = result_root
    if result.get("cohort_id"):
        root = cohort_final_roots.get(str(result["cohort_id"]), root)
    final_answer = _logged_final_answer(result)
    selected = _selected_attempt(result)
    root_run_id = str(selected.get("root_run_id") or "")
    review_rows = _session_review_rows(result_root, root_run_id)
    issues: list[str] = []

    if scenario == "recall_isolation":
        if not isinstance(final_answer, dict) or final_answer.get("nickname") != "Orchid":
            issues.append("project memory nickname was not recalled")
        if not isinstance(final_answer, dict) or final_answer.get("region") != "ap-southeast-1":
            issues.append("application memory region was not recalled")
        seed = result.get("seed_result") or {}
        foreign_id = int(seed.get("foreign_application_item_id") or 0)
        foreign = _db_rows(
            root / "self_learning.db",
            "SELECT scope_id, content, status FROM memory_items WHERE id = ?",
            (foreign_id,),
        )
        if not foreign or str(foreign[0]["scope_id"]) != "foreign_validation_app":
            issues.append("foreign-application negative-control memory is missing")
        leaked = _db_rows(
            root / "self_learning.db",
            "SELECT 1 FROM memory_injections WHERE run_id = ? AND item_id = ?",
            (root_run_id, foreign_id),
        )
        if leaked:
            issues.append("foreign application memory leaked into this run's snapshot")
    elif scenario == "async_distillation":
        if len(review_rows) != 1 or str(review_rows[0]["status"]) != "succeeded":
            issues.append("session review did not succeed asynchronously")
        else:
            distill = _distill_result(review_rows[0])
            if distill.get("distilled_by") != "llm":
                issues.append("eligible session review did not use LLM distillation")
            if int(distill.get("distilled") or 0) < 1:
                issues.append("summary distiller produced no durable proposal")
            issues.extend(
                _async_source_issues(
                    result_root,
                    root_run_id,
                    str(result.get("workflow") or ""),
                    review_rows[0],
                )
            )
            run_rows = _db_rows(
                result_root / "self_learning.db",
                "SELECT ended_at FROM runs WHERE root_run_id = ? ORDER BY ended_at DESC LIMIT 1",
                (root_run_id,),
            )
            if (
                not run_rows
                or not run_rows[0]["ended_at"]
                or not review_rows[0]["finished_at"]
                or str(review_rows[0]["finished_at"]) < str(run_rows[0]["ended_at"])
            ):
                issues.append("session review was not demonstrably completed after SessionEnd")
    elif scenario == "exact_corroboration":
        phase = int(result.get("phase") or 0)
        cohort_id = str(result.get("cohort_id") or "")
        phase_roots = (cohort_phase_root_runs or {}).get(cohort_id, {})
        expected_phase_one_root = str(phase_roots.get(1) or "")
        expected_phase_two_root = str(phase_roots.get(2) or "")
        if phase == 1:
            # Phase 1 is independently successful once it leaves one pending
            # proposal with one root's evidence. Phase 2 may later fail to call
            # the tool; that must count as one failed Application, not
            # retroactively turn both members of the cohort into failures.
            phase_two_case_id = str(result.get("case_id") or "").removesuffix(
                "-p1"
            ) + "-p2"
            phase_one_root = campaign_dir / "pre_attempt_state" / phase_two_case_id
            phase_one_rows = _memory_rows(phase_one_root, _CORROBORATION_FACT)
            if len(phase_one_rows) != 1:
                issues.append(
                    "expected one exact proposal after phase 1, "
                    f"found {len(phase_one_rows)}"
                )
            else:
                if str(phase_one_rows[0]["status"]) != "pending":
                    issues.append("exact proposal was not pending after phase 1")
                evidence_roots = _evidence_root_runs(
                    phase_one_root,
                    int(phase_one_rows[0]["id"]),
                )
                if not expected_phase_one_root:
                    issues.append("exact corroboration selected cohort root mapping is incomplete")
                elif evidence_roots != {expected_phase_one_root}:
                    issues.append(
                        "phase-1 proposal evidence roots did not match selected cohort roots"
                    )
        elif phase == 2:
            pre_root = campaign_dir / "pre_attempt_state" / str(result.get("case_id") or "")
            pre_rows = _memory_rows(pre_root, _CORROBORATION_FACT)
            if len(pre_rows) != 1:
                issues.append(
                    "expected one exact proposal in phase-1 pre-attempt state, "
                    f"found {len(pre_rows)}"
                )
            else:
                if str(pre_rows[0]["status"]) != "pending":
                    issues.append("exact proposal was not pending before phase 2")
                pre_evidence_roots = _evidence_root_runs(
                    pre_root,
                    int(pre_rows[0]["id"]),
                )
                if not expected_phase_one_root:
                    issues.append("exact corroboration selected cohort root mapping is incomplete")
                elif pre_evidence_roots != {expected_phase_one_root}:
                    issues.append(
                        "phase-1 proposal evidence roots did not match selected cohort roots"
                    )
            rows = _memory_rows(root, _CORROBORATION_FACT)
            if len(rows) != 1:
                issues.append(f"expected one exact proposal, found {len(rows)}")
            else:
                final_evidence_roots = _evidence_root_runs(root, int(rows[0]["id"]))
                if not expected_phase_one_root or not expected_phase_two_root:
                    issues.append("exact corroboration selected cohort root mapping is incomplete")
                elif final_evidence_roots != {
                    expected_phase_one_root,
                    expected_phase_two_root,
                }:
                    issues.append(
                        "exact proposal evidence roots did not match selected cohort roots"
                    )
                elif str(rows[0]["status"]) != "active":
                    issues.append("two-run exact proposal was not auto-applied")
        else:
            issues.append(f"exact corroboration has invalid phase {phase}")
    elif scenario == "feedback_revision":
        seed = result.get("seed_result") or {}
        old_id, new_id = seed.get("old_item_id"), seed.get("new_item_id")
        if seed.get("new_before_late") != seed.get("new_after_late"):
            issues.append("late old-revision outcome/feedback changed the new revision")
        if seed.get("old_feedback_ok") is not True:
            issues.append("late feedback was not actually recorded against the old revision")
        rows = _db_rows(
            root / "self_learning.db",
            "SELECT id, status, generation, supersedes_id, trust_score, "
            "helpful_count, unhelpful_count FROM memory_items "
            "WHERE id IN (?, ?) ORDER BY id",
            (int(old_id or 0), int(new_id or 0)),
        )
        by_id = {int(row["id"]): row for row in rows}
        if old_id not in by_id or new_id not in by_id:
            issues.append("seeded revision lineage is missing")
        else:
            old, new = by_id[int(old_id)], by_id[int(new_id)]
            if (
                str(old["status"]) != "superseded"
                or int(new["generation"]) != 2
                or int(new["supersedes_id"] or 0) != int(old_id)
            ):
                issues.append("replace did not create an immutable generation-2 revision")
            if int(old["unhelpful_count"] or 0) < 1:
                issues.append("late old-revision feedback did not stay on the old revision")
            if int(new["unhelpful_count"] or 0) < 1:
                issues.append("contradictory injected revision was not marked unhelpful")
    elif scenario == "capacity_atomic_batch":
        if not isinstance(final_answer, dict) or final_answer.get("third_add_error") != "capacity_exceeded":
            issues.append("third capacity write did not hit capacity_exceeded")
        if not isinstance(final_answer, dict) or final_answer.get("batch_ok") is not True:
            issues.append("atomic consolidation batch did not succeed")
        if not isinstance(final_answer, dict) or final_answer.get("failed_batch_error") != "capacity_exceeded":
            issues.append("deliberately invalid batch did not fail with capacity_exceeded")
        rows = _db_rows(
            root / "self_learning.db",
            "SELECT content, status FROM memory_items WHERE scope_type = 'session' AND scope_id = ?",
            (root_run_id,),
        )
        by_prefix = {
            prefix: [row for row in rows if str(row["content"]).startswith(prefix)]
            for prefix in ("note-1:", "note-2:", "note-3-compact:", "must-not-commit:")
        }
        if not by_prefix["note-1:"] or str(by_prefix["note-1:"][0]["status"]) != "removed":
            issues.append("successful consolidation did not remove the oldest note")
        retained_statuses = {"active", "archived"}
        if (
            not by_prefix["note-2:"]
            or str(by_prefix["note-2:"][0]["status"]) not in retained_statuses
        ):
            issues.append("failed batch did not roll back removal of note-2")
        if (
            not by_prefix["note-3-compact:"]
            or str(by_prefix["note-3-compact:"][0]["status"]) not in retained_statuses
        ):
            issues.append("successful consolidation did not add the compact note")
        if by_prefix["must-not-commit:"]:
            issues.append("failed over-capacity batch partially committed its add")
    elif scenario == "root_run_attribution":
        rows = _db_rows(
            root / "self_learning.db",
            "SELECT scope_id, content FROM memory_items WHERE content LIKE 'learned:%' "
            "AND (content LIKE '%downstream parser%' OR content LIKE '%supervisor-level fact%')",
        )
        if (
            len(rows) != 2
            or {str(row["scope_id"]) for row in rows} != {root_run_id}
        ):
            issues.append("worker and supervisor notes were not attributed to one root run")
        events = _db_rows(
            root / "self_learning.db",
            "SELECT run_id, root_run_id, event_type, worker_name FROM events WHERE root_run_id = ?",
            (root_run_id,),
        )
        if not events or any(str(row["root_run_id"]) != root_run_id for row in events):
            issues.append("event tree contains a foreign or missing root_run_id")
        final_events = [row for row in events if str(row["event_type"]) in {"run_completed", "run_failed"}]
        if len(final_events) != 1:
            issues.append(f"root owner emitted {len(final_events)} final SessionEnd events instead of one")
        elif (
            str(final_events[0]["run_id"] or "") != root_run_id
            or str(final_events[0]["worker_name"] or "")
        ):
            issues.append("final SessionEnd was not emitted by the root owner")
        worker_events = [row for row in events if str(row["worker_name"] or "")]
        if not worker_events:
            issues.append("no worker event was recorded for root attribution")
        elif not any(str(row["run_id"] or "") != root_run_id for row in worker_events):
            issues.append("worker event did not retain a distinct local run_id")
        owner_events = [
            row
            for row in events
            if not str(row["worker_name"] or "")
            and str(row["run_id"] or "") == root_run_id
        ]
        if not owner_events:
            issues.append("root event tree has no owner/root event identity")
    elif scenario == "injection_boundary":
        fragments = _prepared_fragments(review_rows[0]) if len(review_rows) == 1 else []
        expected_kind = (
            "task" if str(result.get("workflow") or "").endswith("mem_poisoned_task_agent.yaml") else "repeated_failure"
        )
        if not any(
            item.get("kind") == expected_kind
            and item.get("blocked") is True
            and item.get("text") == "[BLOCKED]"
            for item in fragments
        ):
            issues.append(f"{expected_kind} fragment was not blocked as one digest fragment")
    elif scenario == "recursive_redaction":
        if (
            not isinstance(final_answer, dict)
            or final_answer.get("payload_received") is not True
            or final_answer.get("safe_fields_preserved") is not True
            or final_answer.get("note_ok") is not True
        ):
            issues.append("recursive-redaction workflow did not preserve safe negative controls")
        fragments = _prepared_fragments(review_rows[0]) if len(review_rows) == 1 else []
        if "[REDACTED]" not in json.dumps(fragments, ensure_ascii=False):
            issues.append("prepared digest contains no evidence that nested secrets were redacted")
    elif scenario == "same_run_fake_corroboration":
        rows = _memory_rows(root, _SAME_RUN_FACT)
        if len(rows) != 1:
            issues.append("same-run duplicate did not resolve to one item")
        elif _evidence_count(root, int(rows[0]["id"])) != 1 or str(rows[0]["status"]) != "pending":
            issues.append("same-run duplicate was incorrectly corroborated or applied")
        if (
            not isinstance(final_answer, dict)
            or not isinstance(final_answer.get("batch"), dict)
            or final_answer["batch"].get("ok") is not True
            or not isinstance(final_answer.get("add"), dict)
            or final_answer["add"].get("duplicate") is not True
        ):
            issues.append("batch+add attack did not execute both successful writes")
    elif scenario == "high_overlap_conflict":
        pair_index = int(str(result.get("cohort_id") or "00").rsplit("-", 1)[-1])
        _shape, left, right = HIGH_OVERLAP_PAIRS[pair_index]
        phase = int(result.get("phase") or 0)
        cohort_id = str(result.get("cohort_id") or "")
        phase_roots = (cohort_phase_root_runs or {}).get(cohort_id, {})
        expected_phase_one_root = str(phase_roots.get(1) or "")
        expected_phase_two_root = str(phase_roots.get(2) or "")
        if phase == 1:
            phase_two_case_id = str(result.get("case_id") or "").removesuffix(
                "-p1"
            ) + "-p2"
            phase_one_root = campaign_dir / "pre_attempt_state" / phase_two_case_id
            phase_one_rows = _memory_rows(phase_one_root, left[1])
            if len(phase_one_rows) != 1:
                issues.append(
                    "expected one high-overlap phase-1 fact, "
                    f"found {len(phase_one_rows)}"
                )
            elif (
                str(phase_one_rows[0]["status"]) != "pending"
                or not expected_phase_one_root
                or _evidence_root_runs(
                    phase_one_root,
                    int(phase_one_rows[0]["id"]),
                )
                != {expected_phase_one_root}
            ):
                issues.append(
                    "high-overlap phase-1 fact was corroborated or auto-applied "
                    "instead of remaining pending with its selected root"
                )
        elif phase == 2:
            for phase_number, fact, expected_root in (
                (1, left[1], expected_phase_one_root),
                (2, right[1], expected_phase_two_root),
            ):
                phase_rows = _memory_rows(root, fact)
                if len(phase_rows) != 1:
                    issues.append(
                        f"expected one high-overlap phase-{phase_number} fact, "
                        f"found {len(phase_rows)}"
                    )
                    continue
                if (
                    str(phase_rows[0]["status"]) != "pending"
                    or not expected_root
                    or _evidence_root_runs(root, int(phase_rows[0]["id"]))
                    != {expected_root}
                ):
                    issues.append(
                        f"high-overlap phase-{phase_number} fact was corroborated or "
                        "auto-applied instead of remaining pending with its selected root"
                    )
        else:
            issues.append(f"high-overlap conflict has invalid phase {phase}")
        variant = str((result.get("env") or {}).get("AGENTLOOM_MEMORY_VALIDATION_VARIANT") or "")
        if not isinstance(final_answer, dict) or final_answer.get("fact") != HIGH_OVERLAP_FACTS.get(variant):
            issues.append("workflow did not submit the configured high-overlap fact")
    return issues


def _execution_contract_issues(
    case: dict[str, Any],
    result: dict[str, Any],
    *,
    expected_model_id: str,
) -> list[str]:
    """Shared canary/release contract for one selected real execution."""
    issues: list[str] = []
    attempts = result.get("attempts") or []
    issues.extend(_canonical_result_issues(case, result))
    if result.get("status") != "completed":
        issues.append(f"execution status={result.get('status')}")
    if not attempts:
        issues.append("execution has no attempts")
        return issues
    issues.extend(_selected_attempt_consistency_issues(result))

    attempt_numbers = [attempt.get("attempt") for attempt in attempts]
    if attempt_numbers not in ([1], [1, 2]):
        issues.append(f"attempt sequence is invalid: {attempt_numbers}")
    classified_kinds = [_audit_attempt_failure_kind(attempt) for attempt in attempts]
    for attempt, classified in zip(attempts, classified_kinds, strict=True):
        if str(attempt.get("failure_kind") or "") != classified:
            issues.append(
                "attempt failure_kind does not match independent audit: "
                f"attempt={attempt.get('attempt')} stored={attempt.get('failure_kind')!r} "
                f"classified={classified!r}"
            )
    if len(attempts) == 2 and classified_kinds[0] != "infrastructure":
        issues.append(
            "retry was not authorized by an independently proven infrastructure failure"
        )
    if result.get("status") == "completed" and classified_kinds[-1] != "":
        issues.append(
            "completed result selected a failed attempt: " + classified_kinds[-1]
        )

    for attempt in attempts:
        command = attempt.get("command") or []
        if (
            not isinstance(command, list)
            or len(command) < 3
            or Path(str(command[0])).name != "loom"
            or command[1] != "run"
            or str(command[2]) != str(case.get("workflow") or "")
        ):
            issues.append("attempt did not execute the loom run CLI contract")
        if (attempt.get("isolation_evidence") or {}).get("live_db_unchanged") is not True:
            issues.append("live .agentloom/self_learning.db changed during isolated attempt")
        if (attempt.get("capture_boundary") or {}).get("prewrite_sanitized") is not True:
            issues.append("child stream did not cross the pre-write sanitization boundary")
        artifact_scan = attempt.get("artifact_scan") or {}
        retryable_artifact_scan = retryable_scan_error_attempt(attempt)
        if artifact_scan.get("ok") is not True and not retryable_artifact_scan:
            issues.append("attempt artifact privacy scan failed")
        precheckpoint_scan = attempt.get("precheckpoint_privacy_scan") or {}
        retryable_precheckpoint_scan = retryable_scan_error_attempt(
            attempt,
            scan_key="precheckpoint_privacy_scan",
        )
        if (
            precheckpoint_scan.get("ok") is not True
            and not retryable_precheckpoint_scan
        ):
            issues.append("attempt did not pass the pre-checkpoint state privacy scan")

    selected = _selected_attempt(result)
    root_run_id = str(selected.get("root_run_id") or "")
    if not root_run_id:
        issues.append("selected attempt has no explicit root_run_id")
    wait = selected.get("job_wait") or {}
    jobs = wait.get("jobs") or []
    if wait.get("terminal") is not True:
        issues.append("learning jobs did not settle before audit")
    review_jobs = [job for job in jobs if job.get("kind") == "session_review"]
    if len(review_jobs) != 1 or review_jobs[0].get("status") != "succeeded":
        issues.append("session-review job did not succeed exactly once")
    elif review_jobs[0].get("artifact_delivery") != "delivered":
        issues.append(
            "session-review artifacts were not delivered atomically by job id"
        )

    evidence = selected.get("model_evidence") or {}
    if evidence.get("summary_requested_and_resolved") is not True:
        issues.append("runtime log did not prove summary model resolution")
    logged_model_ids = set(evidence.get("summary_model_ids") or [])
    if expected_model_id and expected_model_id not in logged_model_ids:
        issues.append("runtime summary model id does not match resolved config")
    latencies = evidence.get("session_finalize_hook_latencies_seconds") or []
    if len(latencies) != 1:
        issues.append(f"expected one measured SessionEnd finalizer, observed {len(latencies)}")
    return issues


def audit_canary_results(
    campaign_dir: str | Path,
    results: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Return semantic/privacy failures that must stop the remaining 95 runs."""
    campaign_dir = Path(campaign_dir).expanduser().resolve()
    plan = _read_json(campaign_dir / "plan.json")
    expected_model_id = str(
        (plan.get("model_contract") or {}).get("application_model_id") or ""
    )
    cases_by_id = {
        str(case.get("case_id") or ""): case for case in plan.get("cases") or []
    }
    cohort_final_roots = {
        str(result["cohort_id"]): Path(
            str(_selected_value(result, "self_learning_root"))
        )
        for result in results
        if result.get("cohort_id")
        and int(result.get("phase") or 0) == 2
        and _selected_value(result, "self_learning_root")
    }
    cohort_phase_root_runs = _cohort_phase_root_runs(results)
    failures: list[dict[str, Any]] = []
    for result in results:
        case = cases_by_id.get(str(result.get("case_id") or ""), result)
        problems = _execution_contract_issues(
            case,
            result,
            expected_model_id=expected_model_id,
        )
        if result.get("status") == "completed":
            selected = _selected_attempt(result)
            evidence = selected.get("model_evidence") or {}
            latencies = evidence.get("session_finalize_hook_latencies_seconds") or []
            if len(latencies) == 1 and float(latencies[0]) >= 0.100:
                problems.append(f"SessionEnd latency failed canary gate: {latencies}")
            root = Path(str(_selected_value(result, "self_learning_root") or ""))
            root_run_id = str(selected.get("root_run_id") or "")
            review_rows = _session_review_rows(root, root_run_id)
            if len(review_rows) != 1 or str(review_rows[0]["status"]) != "succeeded":
                problems.append("canary session-review job did not succeed exactly once")
            elif _job_distilled_by(str(review_rows[0]["result_json"] or "")) != "llm":
                problems.append("canary distiller did not complete with summary LLM")
            semantic_issues = _semantic_case_issues(
                result,
                cohort_final_roots,
                campaign_dir=campaign_dir,
                cohort_phase_root_runs=cohort_phase_root_runs,
            )
            scenario = str(result.get("scenario") or "")
            problems.extend(
                f"[{code}] {issue}"
                for issue in semantic_issues
                for code in [_hard_violation_code(scenario, issue)]
                if code
            )
        if problems:
            failures.append(
                {
                    "case_id": result.get("case_id"),
                    "scenario": result.get("scenario"),
                    "issues": problems,
                }
            )
    integrity = _sqlite_integrity_audit(
        Path(str(attempt.get("self_learning_root") or ""))
        for result in results
        for attempt in result.get("attempts") or []
        if attempt.get("self_learning_root")
    )
    if not integrity["passed"]:
        failures.append(
            {
                "case_id": "sqlite_integrity",
                "scenario": "integrity",
                "issues": [
                    f"{integrity['finding_count']} state database integrity finding(s)"
                ],
                "violation_codes": ["sqlite_integrity"],
            }
        )

    all_files = _all_files(campaign_dir)
    secret_findings = _scan_files(all_files, SECRET_PROBES)
    injection_findings = _scan_files(
        all_files,
        INJECTION_PROBES,
        injection=True,
    )
    posthoc_findings = _posthoc_rewrite_findings(results)
    snapshot_findings = [
        (result.get("case_id"), labels)
        for result in results
        if result.get("status") == "completed"
        for labels in [
            _snapshot_probe_findings(
                Path(str(_selected_value(result, "self_learning_root") or "")),
                str(_selected_attempt(result).get("root_run_id") or ""),
            )
        ]
        if labels
    ]
    if secret_findings or injection_findings or snapshot_findings or posthoc_findings:
        failures.append(
            {
                "case_id": "privacy",
                "scenario": "privacy",
                "issues": [
                    "raw/snapshot probe findings after canaries: "
                    f"{len(secret_findings) + len(injection_findings) + len(snapshot_findings) + len(posthoc_findings)}"
                ],
            }
        )
    return failures


def _actual_audit(
    campaign_dir: Path,
    plan: dict[str, Any],
    results: list[dict[str, Any]],
) -> tuple[list[dict[str, str]], dict[str, Any], list[dict[str, Any]]]:
    issues: list[dict[str, str]] = []
    failures: list[dict[str, Any]] = []
    case_by_id = {str(result.get("case_id")): result for result in results}
    cohort_final_roots: dict[str, Path] = {}
    for result in results:
        cohort = str(result.get("cohort_id") or "")
        selected_root = _selected_value(result, "self_learning_root")
        if cohort and int(result.get("phase") or 0) == 2 and selected_root:
            cohort_final_roots[cohort] = Path(str(selected_root))
    cohort_phase_root_runs = _cohort_phase_root_runs(results)

    first_attempt_completed = 0
    final_completed = 0
    scenario_passes: Counter[str] = Counter()
    runtime_roots: set[str] = set()
    selected_root_runs: set[str] = set()
    eligible_root_runs: set[str] = set()
    selected_job_keys: set[tuple[str, int]] = set()
    llm_root_runs: set[str] = set()
    fallback_root_runs: set[str] = set()
    no_signal_root_runs: set[str] = set()
    dead_root_runs: set[str] = set()
    session_end_latencies: list[float] = []
    worker_recoveries = 0
    actual_state_roots: set[Path] = set()
    expected_model_id = str((plan.get("model_contract") or {}).get("application_model_id") or "")
    for case in plan.get("cases") or []:
        case_id = str(case["case_id"])
        result = case_by_id.get(case_id)
        case_errors: list[str] = []
        if result is None:
            case_errors.append("missing result")
        else:
            case_errors.extend(
                _execution_contract_issues(
                    case,
                    result,
                    expected_model_id=expected_model_id,
                )
            )
            attempts = result.get("attempts") or []
            if len(attempts) > 2:
                case_errors.append("more than one infrastructure retry was used")
            for attempt in attempts:
                runtime = str(attempt.get("runtime_root") or "")
                if runtime in runtime_roots:
                    case_errors.append(f"runtime root reused: {runtime}")
                runtime_roots.add(runtime)
                state_value = str(attempt.get("self_learning_root") or "")
                if state_value:
                    state_root = Path(state_value).resolve()
                    actual_state_roots.add(state_root)
                    if (state_root / "self_learning.db").resolve() == (
                        REPO_ROOT / ".agentloom" / "self_learning.db"
                    ).resolve():
                        case_errors.append("attempt used the live self-learning database")
                command = attempt.get("command") or []
                if (
                    not isinstance(command, list)
                    or len(command) < 3
                    or Path(str(command[0])).name != "loom"
                    or command[1] != "run"
                    or str(command[2]) != str(case.get("workflow"))
                ):
                    case_errors.append("attempt did not execute the loom run CLI contract")
                isolation = attempt.get("isolation_evidence") or {}
                if isolation.get("live_db_unchanged") is not True:
                    case_errors.append("live .agentloom/self_learning.db changed during isolated attempt")
                worker_recoveries += int(bool((attempt.get("worker_recovery") or {}).get("required")))
            if attempts:
                first = attempts[0]
                first_attempt_completed += int(_first_attempt_completed(first))
            if result.get("status") == "completed":
                final_completed += 1
            else:
                case_errors.append(f"execution status={result.get('status')}")
            jobs = (_selected_value(result, "job_wait") or {}).get("jobs", [])
            if result.get("status") == "completed" and not jobs:
                case_errors.append("no learning jobs were observed")
            if any(job.get("status") not in _TERMINAL for job in jobs):
                case_errors.append("learning job did not reach terminal state")
            if any(job.get("artifact_delivery") == "failed" for job in jobs):
                case_errors.append("learning-job artifact delivery failed")
            selected = _selected_attempt(result)
            root_run_id = str(selected.get("root_run_id") or "")
            if not root_run_id:
                case_errors.append("selected attempt has no explicit root_run_id")
            elif root_run_id in selected_root_runs:
                case_errors.append(f"root_run_id was reused across cases: {root_run_id}")
            else:
                selected_root_runs.add(root_run_id)

            if attempts:
                evidence = selected.get("model_evidence") or {}
                if evidence.get("summary_requested_and_resolved") is not True:
                    case_errors.append("runtime log does not prove summary model resolution")
                logged_model_ids = set(evidence.get("summary_model_ids") or [])
                if expected_model_id and expected_model_id not in logged_model_ids:
                    case_errors.append("runtime summary model id does not match resolved config")
                latencies = evidence.get("session_finalize_hook_latencies_seconds") or []
                if len(latencies) != 1:
                    case_errors.append(
                        f"expected one measured SessionEnd finalizer, observed {len(latencies)}"
                    )
                else:
                    session_end_latencies.append(float(latencies[0]))

            if root_run_id:
                selected_root = Path(
                    str(_selected_value(result, "self_learning_root") or "")
                )
                review_rows = _session_review_rows(selected_root, root_run_id)
                if len(review_rows) != 1:
                    case_errors.append(
                        f"expected one idempotent session-review job, found {len(review_rows)}"
                    )
                else:
                    row = review_rows[0]
                    key = (str(selected_root.resolve()), int(row["id"]))
                    if key in selected_job_keys:
                        case_errors.append("the same learning job was counted for multiple runs")
                    selected_job_keys.add(key)
                    eligible_root_runs.add(root_run_id)
                    distilled_by = _job_distilled_by(str(row["result_json"] or ""))
                    if str(row["status"]) == "dead":
                        dead_root_runs.add(root_run_id)
                    elif distilled_by == "llm":
                        llm_root_runs.add(root_run_id)
                    elif distilled_by in {
                        "fallback",
                        "deterministic",
                        "deterministic(fallback)",
                    }:
                        fallback_root_runs.add(root_run_id)
                    elif distilled_by == "no_signal":
                        no_signal_root_runs.add(root_run_id)
            if result.get("status") == "completed":
                case_errors.extend(
                    _semantic_case_issues(
                        result,
                        cohort_final_roots,
                        campaign_dir=campaign_dir,
                        cohort_phase_root_runs=cohort_phase_root_runs,
                    )
                )
        if case_errors:
            violation_codes = _hard_violation_codes(
                str(case["scenario"]),
                case_errors,
            )
            failure = {
                "case_id": case_id,
                "scenario": case["scenario"],
                "issues": case_errors,
            }
            if violation_codes:
                failure["violation_codes"] = violation_codes
            failures.append(failure)
        else:
            scenario_passes[str(case["scenario"])] += 1

    total = len(results)
    issues.extend(_hard_zero_issues(failures))
    if total < 100 and failures:
        _issue(issues, "case_failure", f"{len(failures)}/{total} canary case(s) failed")
    if total == 100:
        if first_attempt_completed < 95:
            _issue(issues, "first_attempt_rate", f"only {first_attempt_completed}/100 completed first attempt")
        if final_completed < 99:
            _issue(issues, "final_completion_rate", f"only {final_completed}/100 completed after retry")
        if total - len(failures) < 95:
            _issue(issues, "semantic_pass_rate", f"only {total - len(failures)}/100 cases passed")
        for scenario in SCENARIO_ORDER:
            if scenario_passes[scenario] < 9:
                _issue(
                    issues,
                    "scenario_pass_rate",
                    f"{scenario} passed {scenario_passes[scenario]}/10",
                )

    # Check only databases used by real attempts. Pre-attempt snapshots are not
    # jobs and selected jobs are counted exactly once above by root identity.
    stuck_jobs = 0
    for state_root in sorted(actual_state_roots):
        db_path = state_root / "self_learning.db"
        rows = _db_rows(
            db_path,
            "SELECT kind, status, result_json FROM learning_jobs",
        )
        for row in rows:
            if str(row["status"]) in {"pending", "running", "retry"}:
                stuck_jobs += 1
    if stuck_jobs:
        _issue(issues, "stuck_jobs", f"campaign has {stuck_jobs} non-terminal learning jobs")
    integrity = _sqlite_integrity_audit(actual_state_roots)
    if not integrity["passed"]:
        _issue(
            issues,
            "sqlite_integrity",
            f"{integrity['finding_count']} integrity finding(s) across "
            f"{integrity['checked']} state database(s)",
        )
    eligible = len(eligible_root_runs)
    llm = len(llm_root_runs)
    if eligible and llm / eligible < 0.95:
        _issue(
            issues,
            "llm_distillation_rate",
            f"LLM distillation was {llm}/{eligible}; fallback={len(fallback_root_runs)}, "
            f"no_signal={len(no_signal_root_runs)}, dead={len(dead_root_runs)}",
        )

    p95 = _percentile(session_end_latencies, 0.95)
    p99 = _percentile(session_end_latencies, 0.99)
    if len(session_end_latencies) != len(selected_root_runs):
        _issue(
            issues,
            "session_end_measurement",
            f"measured {len(session_end_latencies)}/{len(selected_root_runs)} finalized root hooks",
        )
    if p95 >= 0.100:
        _issue(issues, "session_end_p95", f"SessionEnd p95 was {p95:.3f}s (must be <0.100s)")
    if p99 >= 0.250:
        _issue(issues, "session_end_p99", f"SessionEnd p99 was {p99:.3f}s (must be <0.250s)")

    metrics = {
        "selected_runs": total,
        "first_attempt_completed": first_attempt_completed,
        "final_completed": final_completed,
        "case_passes": total - len(failures),
        "scenario_passes": dict(scenario_passes),
        "eligible_session_reviews": eligible,
        "llm_distilled_session_reviews": llm,
        "fallback_session_reviews": len(fallback_root_runs),
        "no_signal_session_reviews": len(no_signal_root_runs),
        "dead_session_reviews": len(dead_root_runs),
        "stuck_jobs": stuck_jobs,
        "sqlite_integrity_databases": int(integrity["checked"]),
        "sqlite_integrity_findings": int(integrity["finding_count"]),
        "worker_recoveries": worker_recoveries,
        "session_end_p95_seconds": p95,
        "session_end_p99_seconds": p99,
    }
    return issues, metrics, failures


def _write_report(
    campaign_dir: Path,
    report: dict[str, Any],
    privacy: dict[str, Any],
    failures: list[dict[str, Any]],
) -> None:
    metrics = report.get("metrics") or {}
    public_report = _public_report_view(report)
    lines = [
        "# Memory Summary Campaign Report",
        "",
        f"- status: {public_report['status']}",
        f"- release_eligible: {public_report['release_eligible']}",
        f"- dry_run: {True if report.get('dry_run') is True else False}",
        f"- selected_runs: {metrics.get('selected_runs', report.get('selected_runs', 0))}",
        f"- privacy_findings: {privacy['finding_count']}",
        f"- failed_cases: {len(failures)}",
        "",
        "## Release-gate issues",
        "",
    ]
    if public_report["issue_codes"]:
        lines.extend(f"- [{code}]" for code in public_report["issue_codes"])
    else:
        lines.append("- none")
    if metrics:
        lines.extend(
            [
                "",
                "## Metrics",
                "",
                f"- first attempt: {metrics.get('first_attempt_completed', 0)}/{metrics.get('selected_runs', 0)}",
                f"- final completion: {metrics.get('final_completed', 0)}/{metrics.get('selected_runs', 0)}",
                f"- semantic pass: {metrics.get('case_passes', 0)}/{metrics.get('selected_runs', 0)}",
                "- LLM distillation: "
                f"{metrics.get('llm_distilled_session_reviews', 0)}/"
                f"{metrics.get('eligible_session_reviews', 0)}",
                f"- deterministic fallback: {metrics.get('fallback_session_reviews', 0)}",
                f"- dead session reviews: {metrics.get('dead_session_reviews', 0)}",
                f"- stuck jobs: {metrics.get('stuck_jobs', 0)}",
                f"- explicit worker recoveries: {metrics.get('worker_recoveries', 0)}",
                "- SessionEnd p95/p99: "
                f"{metrics.get('session_end_p95_seconds', 'n/a')}s / "
                f"{metrics.get('session_end_p99_seconds', 'n/a')}s",
                f"- campaign elapsed: {metrics.get('campaign_elapsed_seconds', 'n/a')}s",
            ]
        )
    lines.extend(
        [
            "",
            "## Artifacts",
            "",
            "- plan.json",
            "- results.json",
            "- privacy_audit.json",
            "- failure_cases.jsonl",
            "- reproduce_commands.txt",
        ]
    )
    (campaign_dir / "report.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def _write_audit_artifacts(
    campaign_dir: Path,
    *,
    failures: list[dict[str, Any]],
    privacy: dict[str, Any],
) -> None:
    """Persist final audit evidence before freezing campaign elapsed time."""
    with (campaign_dir / "failure_cases.jsonl").open("w", encoding="utf-8") as handle:
        for failure in failures:
            handle.write(json.dumps(failure, ensure_ascii=False, sort_keys=True) + "\n")
    (campaign_dir / "privacy_audit.json").write_text(
        json.dumps(privacy, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def audit_campaign(
    campaign_dir: str | Path,
    *,
    campaign_started_monotonic: float | None = None,
) -> dict[str, Any]:
    campaign_dir = Path(campaign_dir).expanduser().resolve()
    plan = _read_json(campaign_dir / "plan.json")
    results = _read_json(campaign_dir / "results.json")
    issues = _static_audit(plan, results)

    # Both secret and injection source text are forbidden in every generated
    # artifact. The campaign may observe them only in memory before its write
    # boundary; the audit is strictly read-only and never repairs a finding.
    all_files = _all_files(campaign_dir)
    secret_findings = _scan_files(all_files, SECRET_PROBES)
    injection_findings = _scan_files(
        all_files,
        INJECTION_PROBES,
        injection=True,
    )
    posthoc_findings = _posthoc_rewrite_findings(results)
    snapshot_findings: list[dict[str, Any]] = []
    if not plan.get("dry_run"):
        for result in results:
            if result.get("status") != "completed":
                continue
            selected = _selected_attempt(result)
            labels = _snapshot_probe_findings(
                Path(str(_selected_value(result, "self_learning_root") or "")),
                str(selected.get("root_run_id") or ""),
            )
            if labels:
                snapshot_findings.append(
                    {
                        "case_id": result.get("case_id"),
                        "probe_labels": labels,
                    }
                )
    privacy = {
        "ok": not secret_findings
        and not injection_findings
        and not snapshot_findings
        and not posthoc_findings,
        "finding_count": len(secret_findings)
        + len(injection_findings)
        + len(snapshot_findings)
        + len(posthoc_findings),
        "secret_findings": secret_findings,
        "injection_findings": injection_findings,
        "snapshot_findings": snapshot_findings,
        "posthoc_rewrite_findings": posthoc_findings,
        "runtime_log_policy": (
            "child output is sanitized in memory before the first atomic log write; "
            "runtime, state, and learning artifacts are detected read-only and never rewritten"
        ),
        "probe_labels": {
            "secrets": sorted(SECRET_PROBES),
            "injections": sorted(INJECTION_PROBES),
        },
    }
    if not privacy["ok"]:
        _issue(issues, "privacy", f"privacy scan found {privacy['finding_count']} raw probes")

    metrics: dict[str, Any] = {"selected_runs": len(results)}
    failures: list[dict[str, Any]] = []
    if not plan.get("dry_run"):
        canary_ids = set(plan.get("canary_case_ids") or [])
        canary_results = [
            result for result in results if result.get("case_id") in canary_ids
        ]
        canary_failures = audit_canary_results(campaign_dir, canary_results)
        (campaign_dir / "canary_audit.json").write_text(
            json.dumps(
                {"ok": not canary_failures, "failures": canary_failures},
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )
        if canary_failures:
            _issue(issues, "canary_gate", f"{len(canary_failures)} canary gate(s) failed")
        actual_issues, metrics, failures = _actual_audit(campaign_dir, plan, results)
        issues.extend(actual_issues)
    elif any(result.get("status") != "planned" for result in results):
        _issue(issues, "dry_run_execution", "dry-run results must all be planned")

    _write_audit_artifacts(
        campaign_dir,
        failures=failures,
        privacy=privacy,
    )
    elapsed_seconds = _finalize_campaign_timing(
        campaign_dir,
        campaign_started_monotonic=campaign_started_monotonic,
    )
    metrics["campaign_elapsed_seconds"] = elapsed_seconds
    if elapsed_seconds > _MAX_CAMPAIGN_SECONDS:
        _issue(
            issues,
            "campaign_deadline",
            f"campaign including final audit took {elapsed_seconds:.1f}s (>8h)",
        )

    dry_run = bool(plan.get("dry_run"))
    ok = not issues
    selected_runs = (
        len(results)
        if int(plan.get("selected_runs") or 0) == len(results)
        else -1
    )
    status, release_eligible = _campaign_status(
        dry_run=dry_run,
        selected_runs=selected_runs,
        ok=ok,
    )
    report = {
        "ok": ok,
        "status": status,
        "release_eligible": release_eligible,
        "dry_run": dry_run,
        "selected_runs": len(results),
        "issues": issues,
        "metrics": metrics,
    }
    _write_report(campaign_dir, report, privacy, failures)
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("campaign_dir", type=Path)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()
    report = audit_campaign(args.campaign_dir)
    public_report = _public_report_view(report)
    if args.json:
        print(json.dumps(public_report, ensure_ascii=False, indent=2, sort_keys=True))
    else:
        print(public_report["status"])
        for code in public_report["issue_codes"]:
            print(f"[{code}]")
    return 0 if public_report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
