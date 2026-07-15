"""Read-only semantic and privacy audit for the real memory-review campaign."""

from __future__ import annotations

import argparse
import json
import math
import sys
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any

import yaml

_BOOTSTRAP_ROOT = Path(__file__).resolve().parents[3]
if str(_BOOTSTRAP_ROOT) not in sys.path:
    sys.path.insert(0, str(_BOOTSTRAP_ROOT))

from applications.memory_feature_validation.scripts.memory_review_campaign_capsule import (  # noqa: E402
    canonical_json_hash,
    capsule_descriptor_issues,
)
from applications.memory_feature_validation.scripts.memory_review_campaign_common import (  # noqa: E402
    GLOBAL_SUMMARY_FIXTURE_ROOT,
    ORACLE_PATH,
    REPO_ROOT,
    RunSpec,
    dataset_manifest,
    find_privacy_markers,
    indexed_rows,
    normalize,
    release_source_manifest_at_commit,
    select_runs,
    terms_match,
)

_HARD_CODES = {
    "application_scope_leak",
    "approval_transition",
    "database_integrity",
    "duplicate_result",
    "exact_evidence_mismatch",
    "pending_recalled",
    "privacy_marker",
    "progress_persisted",
    "review_evidence_missing",
    "review_action_mismatch",
    "review_atomicity",
    "review_call_limit_exceeded",
    "review_off_called",
    "review_on_not_called",
    "retry_contract",
    "run_identity",
    "scope_mismatch",
    "security_persisted",
    "unverified_claim_persisted",
    "unexpected_memory_write",
}
_PROVIDER_TEMPFAIL_RETURN_CODE = 75
_SUBPROCESS_TIMEOUT_RETURN_CODE = 124


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


def _raw_content(row: dict[str, Any]) -> str:
    return str(row.get("content") or "")


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
    *,
    require_audit: bool = True,
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
        # An opted-out Application never enters the reviewer, so the correct
        # observable is no reviewer telemetry and no review audit.  Do not
        # require a synthetic zero-call record from code that must stay inert.
        calls_are_absent = logged_calls is None
        if not calls_are_absent and (
            isinstance(logged_calls, bool) or logged_calls != 0
        ):
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
        memory_effect_count = evidence.get("memory_effect_count")
        completed_run_count_delta = evidence.get("completed_run_count_delta")
        no_review_calls = logged_calls is None or (
            isinstance(logged_calls, int)
            and not isinstance(logged_calls, bool)
            and logged_calls == 0
        )
        if not require_audit and no_review_calls and not audits:
            valid_effect = (
                isinstance(memory_effect_count, int)
                and not isinstance(memory_effect_count, bool)
                and memory_effect_count >= 0
            )
            valid_completed_delta = (
                isinstance(completed_run_count_delta, int)
                and not isinstance(completed_run_count_delta, bool)
                and completed_run_count_delta >= 0
            )
            if valid_effect and memory_effect_count > 0:
                return [
                    _issue(
                        spec.run_id,
                        spec.scenario,
                        "review_atomicity",
                        "failed configured run changed memory without a review audit",
                    )
                ]
            if (
                valid_effect
                and memory_effect_count == 0
                and valid_completed_delta
                and completed_run_count_delta == 0
            ):
                return []
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
    memory_effect_count = evidence.get("memory_effect_count")
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
        or isinstance(memory_effect_count, bool)
        or not isinstance(memory_effect_count, int)
        or memory_effect_count < 0
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
        if actions != 0 or memory_effect_count != 0:
            issues.append(
                _issue(
                    spec.run_id,
                    spec.scenario,
                    "review_atomicity",
                    "failed reviewer committed or reported a memory effect",
                )
            )
        return issues
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
    if actions != memory_effect_count:
        issues.append(
            _issue(
                spec.run_id,
                spec.scenario,
                "review_atomicity",
                "completed review audit actions did not equal durable memory effects",
            )
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
    if not must_not_mutate and actions < 1:
        issues.append(
            _issue(
                spec.run_id,
                spec.scenario,
                "review_action_mismatch",
                "eligible reviewer writer did not record a memory mutation",
            )
        )
    return issues


def _attempt_contract_issues(result: dict[str, Any]) -> list[str]:
    """Validate the one optional retry allowed by the release contract."""
    attempts = result.get("attempts")
    if not isinstance(attempts, list) or not attempts or not all(
        isinstance(attempt, dict) for attempt in attempts
    ):
        return ["Application attempts were not recorded as a non-empty list"]
    if len(attempts) > 2:
        return ["Application used more than one infrastructure retry"]
    if result.get("final") != attempts[-1]:
        return ["final Application evidence did not match the last attempt"]
    for index, attempt in enumerate(attempts, 1):
        returncode = attempt.get("returncode")
        if isinstance(returncode, bool) or not isinstance(returncode, int):
            return [f"Application attempt {index} return code was not an integer"]
        if not isinstance(attempt.get("completion_marker_seen"), bool):
            return [f"Application attempt {index} completion marker evidence was missing"]
        if not isinstance(attempt.get("timed_out"), bool):
            return [f"Application attempt {index} timeout evidence was missing"]
        if not isinstance(attempt.get("retryable_transport"), bool):
            return [f"Application attempt {index} retry evidence was missing"]
        if "transport_failure_reason" not in attempt:
            return [f"Application attempt {index} transport reason was missing"]
        reason = attempt.get("transport_failure_reason")
        timed_out = attempt["timed_out"]
        if timed_out is True and returncode == _SUBPROCESS_TIMEOUT_RETURN_CODE:
            expected_reason = "subprocess_timeout"
        elif returncode == _PROVIDER_TEMPFAIL_RETURN_CODE:
            expected_reason = "provider_tempfail" if timed_out is False else None
        else:
            expected_reason = None
        if (
            (
                timed_out is True
                and returncode != _SUBPROCESS_TIMEOUT_RETURN_CODE
            )
            or attempt["retryable_transport"] is not (expected_reason is not None)
            or reason != expected_reason
        ):
            return [
                f"Application attempt {index} transport evidence contradicted process status"
            ]
    final = attempts[-1]
    returncode = final["returncode"]
    completed = returncode == 0 and final["completion_marker_seen"] is True
    expected_status = "completed" if completed else "failed"
    if result.get("status") != expected_status:
        return ["Application status contradicted final process evidence"]
    if len(attempts) == 2:
        first = attempts[0]
        first_returncode = first["returncode"]
        if first_returncode == 0:
            return ["a retry was used after a successful Application attempt"]
        if (
            first.get("retryable_transport") is not True
            or first.get("transport_failure_reason")
            not in {"subprocess_timeout", "provider_tempfail"}
        ):
            return ["a retry was used after a non-transport failure"]
    return []


def _agent_root_attempt_issues(
    spec: RunSpec,
    result: dict[str, Any],
) -> list[str]:
    if spec.agent_root == ".":
        return []
    attempts = result.get("attempts")
    if not isinstance(attempts, list) or not attempts:
        return []
    if any(
        not isinstance(attempt, dict)
        or attempt.get("agent_root") != spec.agent_root
        for attempt in attempts
    ):
        return ["Application attempt did not use its canonical agent root"]
    return []


def _run_identity_contract_issues(result: dict[str, Any]) -> list[str]:
    issues: list[str] = []
    for attempt in result.get("attempts") or []:
        if not isinstance(attempt, dict):
            continue
        evidence = attempt.get("model_evidence")
        evidence = evidence if isinstance(evidence, dict) else {}
        identity_required = evidence.get("root_identity_required", True)
        if not isinstance(identity_required, bool):
            issues.append("root-run identity requirement evidence was malformed")
            continue
        if identity_required and evidence.get("root_identity_valid") is not True:
            issues.append("Application database lacked explicit root-run identity")
            continue
        root_ids = evidence.get("completed_root_run_ids")
        count = evidence.get("completed_run_count_delta")
        valid_ids = (
            isinstance(root_ids, list)
            and all(isinstance(value, str) and value for value in root_ids)
            and len(root_ids) == len(set(root_ids))
        )
        if (
            not valid_ids
            or isinstance(count, bool)
            or not isinstance(count, int)
            or count != len(root_ids)
        ):
            issues.append("completed root-run identity evidence was malformed")
            continue
        audits = evidence.get("review_audit_delta")
        audits = audits if isinstance(audits, list) else []
        completed = (
            attempt.get("returncode") == 0
            and attempt.get("completion_marker_seen") is True
        )
        expected_count = 1 if completed or audits else 0
        if count != expected_count:
            issues.append(
                "Application did not persist exactly one unique completed root run"
            )
            continue
        if audits and count == 1:
            audit_root_ids = {
                str(audit.get("root_run_id") or "")
                for audit in audits
                if isinstance(audit, dict)
            }
            if audit_root_ids != {root_ids[0]}:
                issues.append(
                    "review audit root did not match the completed Application root"
                )
    return issues


def _failed_writer_snapshot_issues(
    spec: RunSpec,
    attempt: dict[str, Any],
    oracle_row: dict[str, Any],
    *,
    seen_codes: set[str],
) -> list[dict[str, Any]]:
    """Audit durable residue from one writer attempt that did not complete."""
    rows = _memory_rows({"final": attempt})
    live_rows = [row for row in rows if _status(row) in {"active", "pending"}]
    if not live_rows:
        return []

    run_id = spec.run_id
    scenario = spec.scenario
    issues: list[dict[str, Any]] = []
    expected_status = normalize(oracle_row.get("expected_writer_status"))
    required_terms = [str(value) for value in oracle_row.get("required_terms") or []]
    forbidden_terms = [normalize(value) for value in oracle_row.get("forbidden_terms") or []]
    if expected_status == "absent":
        code = (
            "progress_persisted"
            if scenario == "review_on_progress"
            else "security_persisted"
            if scenario == "review_on_security"
            else "unverified_claim_persisted"
            if scenario == "review_on_unverified_claim"
            else "unexpected_memory_write"
        )
        if code not in seen_codes:
            issues.append(
                _issue(
                    run_id,
                    scenario,
                    code,
                    "failed writer persisted an unexpected memory effect",
                )
            )
            seen_codes.add(code)
    else:
        term_matches = [
            row
            for row in _matching_rows(rows, required_terms)
            if _status(row) == expected_status
        ]
        expected_content = str(oracle_row.get("expected_content") or "")
        matches = [
            row for row in term_matches if _raw_content(row) == expected_content
        ]
        if not matches and "exact_evidence_mismatch" not in seen_codes:
            issues.append(
                _issue(
                    run_id,
                    scenario,
                    "exact_evidence_mismatch",
                    "failed writer persisted bytes that did not equal the independent evidence",
                )
            )
            seen_codes.add("exact_evidence_mismatch")
        if (
            (len(live_rows) != 1 or len(matches) != 1)
            and "unexpected_memory_write" not in seen_codes
        ):
            issues.append(
                _issue(
                    run_id,
                    scenario,
                    "unexpected_memory_write",
                    "failed writer persisted extra or unrelated memory",
                )
            )
            seen_codes.add("unexpected_memory_write")

    live_content = "\n".join(_content(row) for row in live_rows)
    if any(
        term and terms_match(live_content, [term])
        for term in forbidden_terms
    ):
        code = (
            "progress_persisted"
            if scenario in {"review_on_progress", "review_on_mixed_noise"}
            else "security_persisted"
        )
        if code not in seen_codes:
            issues.append(
                _issue(
                    run_id,
                    scenario,
                    code,
                    "failed writer persisted forbidden content",
                )
            )
            seen_codes.add(code)

    scope_candidates = [
        row
        for row in _matching_rows(rows, required_terms)
        if expected_status == "absent" or _status(row) == expected_status
    ]
    expected_scope = normalize(oracle_row.get("expected_scope"))
    if (
        scope_candidates
        and expected_scope
        and not all(_scope(row) == expected_scope for row in scope_candidates)
        and "scope_mismatch" not in seen_codes
    ):
        issues.append(
            _issue(
                run_id,
                scenario,
                "scope_mismatch",
                "failed writer persisted the fact in the wrong scope",
            )
        )
        seen_codes.add("scope_mismatch")
    if (
        scope_candidates
        and expected_scope == "application"
        and not all(
            str(row.get("scope_id") or "") == _workflow_application_id(spec)
            for row in scope_candidates
        )
        and "scope_mismatch" not in seen_codes
    ):
        issues.append(
            _issue(
                run_id,
                scenario,
                "scope_mismatch",
                "failed writer used the wrong Application scope id",
            )
        )
        seen_codes.add("scope_mismatch")
    return issues


def _approval_transition_contract(
    spec: RunSpec,
    result: dict[str, Any],
    oracle_row: dict[str, Any],
) -> list[dict[str, Any]]:
    """Validate the complete isolated state produced by one approval decision."""
    if spec.phase != "post_recall":
        return []

    run_id = spec.run_id
    scenario = spec.scenario
    final = result.get("final") if isinstance(result.get("final"), dict) else {}
    database = final.get("database") if isinstance(final.get("database"), dict) else {}
    transition = (
        result.get("approval_transition")
        if isinstance(result.get("approval_transition"), dict)
        else {}
    )
    decision = str(oracle_row.get("decision") or "")
    target = str(transition.get("target") or "")
    expected_status = "approved" if decision == "approve" else "rejected"
    expected_content = str(oracle_row.get("expected_content") or "")
    expected_scope = normalize(oracle_row.get("expected_scope"))

    pending_rows = [
        row
        for row in database.get("memory_pending_writes") or []
        if isinstance(row, dict)
    ]
    target_rows = [row for row in pending_rows if str(row.get("id") or "") == target]
    target_row = target_rows[0] if len(target_rows) == 1 else {}
    persisted_transition = bool(
        len(pending_rows) == 1
        and len(target_rows) == 1
        and normalize(target_row.get("status")) == expected_status
        and target_row.get("resolved_at")
    )

    active_rows = [
        row
        for row in database.get("memory_items") or []
        if isinstance(row, dict)
    ]
    exact_active = [
        row for row in active_rows if _raw_content(row) == expected_content
    ]
    scope_active = [
        row
        for row in exact_active
        if not expected_scope or _scope(row) == expected_scope
    ]
    expected_application_id = (
        _workflow_application_id(spec) if expected_scope == "application" else ""
    )
    application_active = [
        row
        for row in scope_active
        if not expected_application_id
        or str(row.get("scope_id") or "") == expected_application_id
    ]

    issues: list[dict[str, Any]] = []
    seen_codes: set[str] = set()

    def add(code: str, message: str) -> None:
        if code not in seen_codes:
            issues.append(_issue(run_id, scenario, code, message))
            seen_codes.add(code)

    target_has_content = _raw_content(target_row) == expected_content
    if target_row and not target_has_content:
        add(
            "exact_evidence_mismatch",
            "resolved pending memory did not preserve the exact evidence bytes",
        )
    if (
        target_row
        and expected_scope
        and _scope(target_row) != expected_scope
    ):
        add("scope_mismatch", "resolved pending memory used the wrong scope")
    if (
        target_row
        and expected_application_id
        and str(target_row.get("scope_id") or "") != expected_application_id
    ):
        add(
            "scope_mismatch",
            "resolved pending memory used the wrong Application scope id",
        )

    if decision == "approve":
        if active_rows and len(exact_active) != len(active_rows):
            add(
                "exact_evidence_mismatch",
                "approved memory did not preserve the exact evidence bytes",
            )
        if expected_scope and active_rows and not all(
            _scope(row) == expected_scope for row in active_rows
        ):
            add("scope_mismatch", "approved memory landed in the wrong scope")
        if expected_application_id and active_rows and not all(
            str(row.get("scope_id") or "") == expected_application_id
            for row in active_rows
        ):
            add(
                "scope_mismatch",
                "approved memory used the wrong Application scope id",
            )
        if len(active_rows) != 1:
            add(
                "unexpected_memory_write",
                "approval did not produce exactly one isolated active memory",
            )
        active_transition = len(application_active) == len(active_rows) == 1
    else:
        if active_rows:
            add(
                "unexpected_memory_write",
                "rejection left active memory in the isolated cohort",
            )
        active_transition = not active_rows

    transition_valid = bool(
        transition.get("ok") is True
        and transition.get("readback_ok") is True
        and str(transition.get("decision") or "") == decision
        and persisted_transition
        and target_has_content
        and active_transition
    )
    if not transition_valid or issues:
        add(
            "approval_transition",
            "pending item was not approved or rejected through exactly one target CLI effect",
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
    attempts = [
        attempt
        for attempt in (result.get("attempts") or [final])
        if isinstance(attempt, dict)
    ]
    if any(
        (attempt.get("database") or {}).get("exists") is not False
        and (attempt.get("database") or {}).get("integrity") != "ok"
        for attempt in attempts
    ):
        issues.append(_issue(run_id, scenario, "database_integrity", "SQLite integrity_check was not ok"))
    if any(attempt.get("privacy_findings") for attempt in attempts):
        issues.append(_issue(run_id, scenario, "privacy_marker", "raw security marker reached runtime or state"))

    final_returncode = final.get("returncode")
    application_failed = (
        result.get("status") != "completed"
        or isinstance(final_returncode, bool)
        or not isinstance(final_returncode, int)
        or final_returncode != 0
        or (
            "completion_marker_seen" in final
            and final.get("completion_marker_seen") is not True
        )
    )
    for index, attempt in enumerate(attempts):
        memory_effect_count = (attempt.get("model_evidence") or {}).get(
            "memory_effect_count"
        )
        if not spec.review_expected and spec.phase != "writer":
            if (
                isinstance(memory_effect_count, bool)
                or not isinstance(memory_effect_count, int)
                or memory_effect_count < 0
            ):
                issues.append(
                    _issue(
                        run_id,
                        scenario,
                        "review_evidence_missing",
                        "non-writer run did not report its memory effect count",
                    )
                )
            elif memory_effect_count != 0:
                issues.append(
                    _issue(
                        run_id,
                        scenario,
                        "unexpected_memory_write",
                        "non-writer run changed durable memory state",
                    )
                )
        issues.extend(
            _review_audit_contract(
                spec,
                {"final": attempt},
                oracle_row,
                require_audit=(not application_failed and index == len(attempts) - 1),
            )
        )
    if spec.phase == "writer":
        seen_codes = {str(issue.get("code") or "") for issue in issues}
        failed_attempts = attempts if application_failed else attempts[:-1]
        for attempt in failed_attempts:
            issues.extend(
                _failed_writer_snapshot_issues(
                    spec,
                    attempt,
                    oracle_row,
                    seen_codes=seen_codes,
                )
            )
    issues.extend(_approval_transition_contract(spec, result, oracle_row))
    if application_failed:
        issues.append(
            _issue(
                run_id,
                scenario,
                "application_failed",
                "real loom run did not complete",
            )
        )
        return issues

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
            expected_content = str(oracle_row.get("expected_content") or "")
            term_matches = [
                row
                for row in _matching_rows(rows, required_terms)
                if _status(row) == expected_status
            ]
            matches = [
                row
                for row in term_matches
                if _raw_content(row) == expected_content
            ]
            if not matches:
                issues.append(
                    _issue(
                        run_id,
                        scenario,
                        (
                            "exact_evidence_mismatch"
                            if term_matches or not expected_content
                            else "expected_memory_missing"
                        ),
                        (
                            "writer memory did not equal the independently authored evidence bytes"
                            if term_matches or not expected_content
                            else f"no {expected_status} memory contained the independently required fact terms"
                        ),
                    )
                )
            if live_rows and (len(live_rows) != 1 or len(matches) != 1):
                issues.append(
                    _issue(
                        run_id,
                        scenario,
                        "unexpected_memory_write",
                        "fresh writer did not persist exactly one expected memory",
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

    return issues


def evaluate_results(
    specs: list[RunSpec],
    results: list[dict[str, Any]],
    oracle: dict[str, dict[str, Any]],
    *,
    require_complete: bool,
    require_attempt_contract: bool = False,
) -> dict[str, Any]:
    result_ids = [str(result.get("run_id") or "") for result in results]
    by_id = {str(result.get("run_id") or ""): result for result in results}
    specs_by_id = {spec.run_id: spec for spec in specs}
    issues: list[dict[str, Any]] = []
    for run_id, count in Counter(result_ids).items():
        if count <= 1:
            continue
        spec = specs_by_id.get(run_id)
        issues.append(
            _issue(
                run_id,
                spec.scenario if spec is not None else "result_identity",
                "duplicate_result",
                "campaign contained more than one result for the same run id",
            )
        )
    outcomes: list[dict[str, Any]] = []
    for spec in specs:
        result = by_id.get(spec.run_id)
        if result is None:
            run_issues = [_issue(spec.run_id, spec.scenario, "missing_result", "planned Application has no result")]
        elif result.get("status") == "planned":
            run_issues = []
        else:
            run_issues = _evaluate_one(spec, result, oracle[spec.case_id])
            if require_attempt_contract:
                run_issues = [
                    *[
                        _issue(
                            spec.run_id,
                            spec.scenario,
                            "retry_contract",
                            message,
                        )
                        for message in _attempt_contract_issues(result)
                    ],
                    *[
                        _issue(
                            spec.run_id,
                            spec.scenario,
                            "run_identity",
                            message,
                        )
                        for message in _agent_root_attempt_issues(spec, result)
                    ],
                    *[
                        _issue(
                            spec.run_id,
                            spec.scenario,
                            "run_identity",
                            message,
                        )
                        for message in _run_identity_contract_issues(result)
                    ],
                    *run_issues,
                ]
        issues.extend(run_issues)
        outcomes.append(
            {
                "run_id": spec.run_id,
                "scenario": spec.scenario,
                "ok": not run_issues,
                "hard_failure": any(issue["hard"] for issue in run_issues),
            }
        )
    complete = (
        len(results) == len(by_id) == len(specs)
        and all(spec.run_id in by_id for spec in specs)
    )
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


def _provenance_issues(
    plan: dict[str, Any],
    environment: dict[str, Any],
    completed_environment: dict[str, Any],
) -> list[str]:
    """Verify that one campaign used one committed source and model contract."""
    issues: list[str] = []
    source = environment.get("source")
    completed_source = completed_environment.get("source")
    source = source if isinstance(source, dict) else {}
    completed_source = completed_source if isinstance(completed_source, dict) else {}
    source_valid = (
        source.get("available") is True
        and source.get("dirty") is False
        and isinstance(source.get("files"), list)
        and bool(source.get("files"))
    )
    if not source_valid:
        issues.append("bound campaign sources did not match the recorded commit")
    elif (
        completed_source.get("available") is not True
        or completed_source.get("dirty") is not False
        or completed_source.get("commit") != source.get("commit")
        or completed_source.get("files") != source.get("files")
    ):
        issues.append("bound campaign sources changed while the campaign was running")

    model_contract = environment.get("model_contract")
    model_contract = model_contract if isinstance(model_contract, dict) else {}
    model_contract_valid = (
        model_contract.get("configured") is True
        and model_contract.get("requested_type") == "summary"
        and bool(str(model_contract.get("model_id") or ""))
        and isinstance(model_contract.get("config_hash"), str)
        and len(model_contract["config_hash"]) == 64
        and isinstance(model_contract.get("endpoint_hash"), str)
        and len(model_contract["endpoint_hash"]) == 64
        and model_contract.get("num_retries") == 0
        and not isinstance(model_contract.get("num_retries"), bool)
        and model_contract.get("parallel_tool_calls") is False
        and normalize(model_contract.get("thinking_type")) != "disabled"
    )
    if not model_contract_valid:
        issues.append("recorded summary model contract was incomplete or unsafe")
    elif completed_environment.get("model_contract") != model_contract:
        issues.append("summary model contract changed while the campaign was running")
    if completed_environment.get("dataset") != plan.get("dataset"):
        issues.append("dataset changed while the campaign was running")

    capsule = environment.get("capsule")
    completed_capsule = completed_environment.get("capsule")
    capsule = capsule if isinstance(capsule, dict) else {}
    completed_capsule = (
        completed_capsule if isinstance(completed_capsule, dict) else {}
    )
    issues.extend(capsule_descriptor_issues(capsule))
    if completed_capsule != capsule:
        issues.append("capsule runtime changed while the campaign was running")
    if capsule.get("git_commit") != source.get("commit"):
        issues.append("capsule commit did not match the recorded source")
    if capsule.get("source_manifest_hash") != canonical_json_hash(
        source.get("files") or []
    ):
        issues.append("capsule source hash did not match the recorded manifest")
    if capsule.get("dataset_manifest_hash") != canonical_json_hash(
        plan.get("dataset")
    ):
        issues.append("capsule dataset hash did not match the campaign plan")
    if capsule.get("model_contract_hash") != canonical_json_hash(model_contract):
        issues.append("capsule model hash did not match the recorded contract")
    uv_lock_rows = [
        row
        for row in (source.get("files") or [])
        if isinstance(row, dict) and row.get("path") == "uv.lock"
    ]
    if (
        len(uv_lock_rows) != 1
        or capsule.get("uv_lock_hash") != uv_lock_rows[0].get("sha256")
    ):
        issues.append("capsule lock hash did not match committed uv.lock")
    return issues


def _plan_issues(plan: dict[str, Any]) -> list[str]:
    issues: list[str] = []
    try:
        requested = int(plan.get("requested_runs"))
        canonical = [spec.to_dict() for spec in select_runs(requested)]
    except Exception as exc:
        return [f"invalid requested_runs: {type(exc).__name__}"]
    if plan.get("runs") != canonical:
        issues.append("plan does not match the canonical committed dataset plan")
    if plan.get("dataset") != dataset_manifest():
        issues.append("dataset manifest does not match the canonical campaign dataset")
    if plan.get("cli_contract") != "loom run <workflow> --log-to-file":
        issues.append("campaign bypassed the production loom run CLI")
    if plan.get("memory_cli_contract") != ["list", "pending", "approve", "reject"]:
        issues.append("memory CLI contract is not list/pending/approve/reject")
    if requested == 100 and int(plan.get("max_concurrency") or 0) > 2:
        issues.append("release concurrency exceeded 2")
    issues.extend(_global_opt_out_plan_issues(canonical))
    return issues


def _global_opt_out_plan_issues(
    canonical_rows: list[dict[str, Any]],
) -> list[str]:
    """Independently verify the real global-summary/Application-empty cohort."""
    try:
        specs = [RunSpec(**row) for row in canonical_rows]
        off_specs = [
            spec for spec in specs if spec.scenario == "review_off_durable"
        ]
        if (
            not off_specs
            or (len(specs) == 100 and len(off_specs) != 10)
            or {spec.agent_root for spec in off_specs}
            != {GLOBAL_SUMMARY_FIXTURE_ROOT}
        ):
            raise ValueError
        oracle = indexed_rows(ORACLE_PATH)
        for spec in off_specs:
            if (
                oracle.get(spec.case_id, {}).get("config_layering")
                != "global_summary_application_opt_out"
            ):
                raise ValueError
            agent_root = (REPO_ROOT / spec.agent_root).resolve()
            workflow = (REPO_ROOT / spec.workflow).resolve()
            workflow.relative_to(agent_root / "applications")
            global_payload = yaml.safe_load(
                (agent_root / "config" / "system.yaml").read_text(
                    encoding="utf-8"
                )
            ) or {}
            app_payload = yaml.safe_load(
                (workflow.parent.parent / "config" / "system.yaml").read_text(
                    encoding="utf-8"
                )
            ) or {}
            global_review = (
                global_payload.get("self_learning", {})
                .get("memory", {})
                .get("review_model")
            )
            app_memory = app_payload.get("self_learning", {}).get("memory", {})
            if global_review != "summary" or (
                "review_model" not in app_memory
                or app_memory.get("review_model") != ""
            ):
                raise ValueError
    except (AttributeError, OSError, TypeError, ValueError, yaml.YAMLError):
        return [
            "review-off cohort did not use global summary with an explicit "
            "Application opt-out"
        ]
    return []


def _aware_timestamp(value: Any) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(str(value or ""))
    except ValueError:
        return None
    return parsed if parsed.utcoffset() is not None else None


def _campaign_timing_issues(
    plan: dict[str, Any],
    environment: dict[str, Any],
    completed_environment: dict[str, Any],
    results: list[dict[str, Any]],
) -> list[str]:
    """Audit wall-clock evidence without trusting one aggregate duration."""
    plan_started_raw = plan.get("campaign_started_at")
    environment_started_raw = environment.get("campaign_started_at")
    finished_raw = completed_environment.get("campaign_finished_at")
    started = _aware_timestamp(plan_started_raw)
    environment_started = _aware_timestamp(environment_started_raw)
    finished = _aware_timestamp(finished_raw)
    if (
        started is None
        or environment_started is None
        or finished is None
        or str(plan_started_raw) != str(environment_started_raw)
        or finished < started
    ):
        return ["campaign timing evidence was incomplete or invalid"]

    attempt_windows: list[tuple[datetime, datetime]] = []
    for result in results:
        attempts = result.get("attempts") if isinstance(result, dict) else None
        if not isinstance(attempts, list) or not attempts:
            continue
        for attempt in attempts:
            if not isinstance(attempt, dict):
                return ["Application attempt timestamps were incomplete or invalid"]
            attempt_started = _aware_timestamp(attempt.get("started_at"))
            attempt_finished = _aware_timestamp(attempt.get("finished_at"))
            if (
                attempt_started is None
                or attempt_finished is None
                or attempt_finished < attempt_started
            ):
                return ["Application attempt timestamps were incomplete or invalid"]
            attempt_windows.append((attempt_started, attempt_finished))

    if results and not attempt_windows:
        return ["Application attempt timestamps were incomplete or invalid"]
    if any(
        attempt_started < started or attempt_finished > finished
        for attempt_started, attempt_finished in attempt_windows
    ):
        return ["Application attempt timestamps escaped the campaign envelope"]
    if (
        int(plan.get("requested_runs") or 0) == 100
        and (finished - started).total_seconds() > 8 * 60 * 60
    ):
        return ["100-run campaign exceeded the eight-hour release limit"]
    return []


def _campaign_duration_seconds(
    plan: dict[str, Any],
    completed_environment: dict[str, Any],
) -> float | None:
    started = _aware_timestamp(plan.get("campaign_started_at"))
    finished = _aware_timestamp(completed_environment.get("campaign_finished_at"))
    if started is None or finished is None or finished < started:
        return None
    return (finished - started).total_seconds()


def _application_completed(result: dict[str, Any]) -> bool:
    final = result.get("final")
    if not isinstance(final, dict):
        return False
    return (
        result.get("status") == "completed"
        and isinstance(final.get("returncode"), int)
        and not isinstance(final.get("returncode"), bool)
        and final.get("returncode") == 0
        and final.get("completion_marker_seen") is True
        and final.get("timed_out") is not True
    )


def _first_attempt_gate(
    results: list[dict[str, Any]],
    *,
    selected_runs: int,
) -> bool:
    required = math.ceil(max(0, int(selected_runs)) * 0.95)
    completed = sum(
        1
        for result in results
        if _application_completed(result)
        and len(result.get("attempts") or []) == 1
    )
    return completed >= required


def _provider_protocol_empty_count(results: list[dict[str, Any]]) -> int:
    """Count recoverable required-tool protocol misses across every attempt."""
    total = 0
    for result in results:
        attempts = result.get("attempts") or [result.get("final") or {}]
        total += sum(
            int(attempt.get("provider_protocol_empty_responses") or 0)
            for attempt in attempts
            if isinstance(attempt, dict)
        )
    return total


def _provider_protocol_empty_run_ids(
    results: list[dict[str, Any]],
) -> list[str]:
    affected: list[str] = []
    for result in results:
        attempts = result.get("attempts") or [result.get("final") or {}]
        if any(
            int(attempt.get("provider_protocol_empty_responses") or 0) > 0
            for attempt in attempts
            if isinstance(attempt, dict)
        ):
            affected.append(str(result.get("run_id") or ""))
    return sorted(run_id for run_id in affected if run_id)


def _review_completion_counts(
    specs: list[RunSpec],
    results: list[dict[str, Any]],
) -> tuple[int, int]:
    eligible = 0
    completed = 0
    by_id = {
        str(result.get("run_id") or ""): result
        for result in results
        if isinstance(result, dict)
    }
    for spec in specs:
        result = by_id.get(spec.run_id)
        if not spec.review_expected or result is None or not _application_completed(result):
            continue
        eligible += 1
        audits = (
            ((result.get("final") or {}).get("model_evidence") or {}).get(
                "review_audit_delta"
            )
            or []
        )
        if (
            len(audits) == 1
            and isinstance(audits[0], dict)
            and normalize(
                (
                    audits[0].get("result")
                    if isinstance(audits[0].get("result"), dict)
                    else audits[0]
                ).get("status")
            )
            == "completed"
        ):
            completed += 1
    return eligible, completed


def _review_completion_gate(
    specs: list[RunSpec],
    results: list[dict[str, Any]],
) -> bool:
    eligible, completed = _review_completion_counts(specs, results)
    return eligible == 0 or completed >= math.ceil(eligible * 0.95)


def _result_privacy_findings(
    results: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    findings: list[dict[str, Any]] = []
    for result in results:
        attempts = result.get("attempts")
        if not isinstance(attempts, list) or not attempts:
            attempts = [result.get("final") or {}]
        for attempt in attempts:
            if not isinstance(attempt, dict):
                continue
            findings.extend(
                finding
                for finding in (attempt.get("privacy_findings") or [])
                if isinstance(finding, dict)
            )
    return findings


def _capsule_attempt_issues(
    results: list[dict[str, Any]],
    *,
    capsule_id: str,
) -> list[str]:
    issues: list[str] = []
    for result in results:
        attempts = result.get("attempts") if isinstance(result, dict) else None
        attempts = attempts if isinstance(attempts, list) else []
        for attempt in attempts:
            if not isinstance(attempt, dict):
                continue
            if (
                attempt.get("capsule_id") != capsule_id
                or attempt.get("execution_root") != capsule_id
            ):
                issues.append(
                    "Application attempt did not execute in the recorded capsule: "
                    f"{result.get('run_id')}"
                )
    return issues


def audit_campaign(campaign_dir: Path) -> dict[str, Any]:
    campaign_dir = campaign_dir.expanduser().resolve()
    plan = json.loads((campaign_dir / "plan.json").read_text(encoding="utf-8"))
    results_payload = json.loads((campaign_dir / "results.json").read_text(encoding="utf-8"))
    results = results_payload.get("results") if isinstance(results_payload, dict) else None
    if not isinstance(results, list):
        raise ValueError("results.json must contain a results list")
    plan_issues = _plan_issues(plan)
    campaign_duration_seconds: float | None = None
    if not plan.get("dry_run"):
        environment_path = campaign_dir / "environment.json"
        completed_environment_path = campaign_dir / "environment_completed.json"
        environment = (
            json.loads(environment_path.read_text(encoding="utf-8"))
            if environment_path.is_file()
            else {}
        )
        completed_environment = (
            json.loads(completed_environment_path.read_text(encoding="utf-8"))
            if completed_environment_path.is_file()
            else {}
        )
        environment = environment if isinstance(environment, dict) else {}
        completed_environment = (
            completed_environment
            if isinstance(completed_environment, dict)
            else {}
        )
        plan_issues.extend(
            _provenance_issues(plan, environment, completed_environment)
        )
        plan_issues.extend(
            _campaign_timing_issues(
                plan,
                environment,
                completed_environment,
                results,
            )
        )
        campaign_duration_seconds = _campaign_duration_seconds(
            plan,
            completed_environment,
        )
        source = environment.get("source")
        source = source if isinstance(source, dict) else {}
        recorded_files = source.get("files")
        committed_files = release_source_manifest_at_commit(
            str(source.get("commit") or "")
        )
        if (
            isinstance(recorded_files, list)
            and recorded_files
            and committed_files != recorded_files
        ):
            plan_issues.append(
                "recorded source manifest did not match the recorded commit"
            )
        capsule = environment.get("capsule")
        capsule = capsule if isinstance(capsule, dict) else {}
        capsule_id = str(capsule.get("capsule_id") or "")
        plan_issues.extend(
            _capsule_attempt_issues(results, capsule_id=capsule_id)
        )
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
        evaluated = evaluate_results(
            specs,
            results,
            oracle,
            require_complete=True,
            require_attempt_contract=True,
        )
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
            if _application_completed(result)
            and len(result.get("attempts") or []) == 1
        )
        first_attempt_required = math.ceil(len(specs) * 0.95)
        first_attempt_gate = _first_attempt_gate(
            results,
            selected_runs=len(specs),
        )
        review_eligible, review_completed = _review_completion_counts(
            specs, results
        )
        review_completion_gate = _review_completion_gate(specs, results)
        provider_protocol_empty_responses = _provider_protocol_empty_count(results)
        provider_protocol_empty_run_ids = _provider_protocol_empty_run_ids(results)
        completion_gate = sum(
            _application_completed(result) for result in results
        ) >= math.ceil(len(specs) * 0.99)
        ok = bool(
            not plan_issues
            and evaluated["complete"]
            and not hard_issues
            and passed_count >= required_total
            and all(scenario_gates.values())
            and first_attempt_gate
            and review_completion_gate
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
            "campaign_duration_seconds": campaign_duration_seconds,
            "passed_runs": passed_count,
            "required_passed_runs": required_total,
            "first_attempt_completed": first_attempt,
            "first_attempt_required": first_attempt_required,
            "first_attempt_gate": first_attempt_gate,
            "review_eligible": review_eligible,
            "review_completed": review_completed,
            "review_completion_gate": review_completion_gate,
            "provider_protocol_empty_responses": provider_protocol_empty_responses,
            "provider_protocol_empty_run_ids": provider_protocol_empty_run_ids,
            "final_completed": sum(
                _application_completed(result) for result in results
            ),
            "scenario_passed": dict(passed),
            "scenario_total": dict(total),
            "scenario_gates": scenario_gates,
            "hard_issue_count": len(hard_issues),
            "issues": evaluated["issues"],
        }

    result_privacy_findings = _result_privacy_findings(results)
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
        if spec is None:
            continue
        original_result = results_by_id.get(run_id) or {}
        snapshot_record = original_result.get("reproduction_snapshot")
        snapshot_record = (
            snapshot_record if isinstance(snapshot_record, dict) else {}
        )
        reproduction_commands.append(
            {
                "run_id": run_id,
                "cwd": str(REPO_ROOT),
                "state_snapshot": str(
                    campaign_dir / str(snapshot_record.get("path") or "missing")
                ),
                "state_snapshot_sha256": str(
                    snapshot_record.get("sha256") or ""
                ),
                "command": [
                    "uv",
                    "run",
                    "python",
                    "applications/memory_feature_validation/scripts/run_memory_review_campaign.py",
                    "--reproduce-campaign",
                    str(campaign_dir),
                    "--run-id",
                    run_id,
                ],
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
                f"- campaign_duration_seconds: {audit['campaign_duration_seconds']}",
                f"- first_attempt_completed: {audit['first_attempt_completed']}/{audit['selected_runs']} (required {audit['first_attempt_required']})",
                f"- first_attempt_gate: {audit['first_attempt_gate']}",
                f"- reviewer_completed: {audit['review_completed']}/{audit['review_eligible']}",
                f"- reviewer_completion_gate: {audit['review_completion_gate']}",
                f"- provider_protocol_empty_responses: {audit['provider_protocol_empty_responses']}",
                f"- provider_protocol_empty_runs: {len(audit['provider_protocol_empty_run_ids'])}",
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
