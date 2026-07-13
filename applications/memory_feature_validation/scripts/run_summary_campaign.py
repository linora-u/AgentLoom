"""Run the isolated 100-case real-summary memory validation campaign.

Dry-run verifies the complete plan without a provider call. A real run invokes
the configured ``summary`` provider through ``loom run`` and may take hours.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import time
from collections import Counter
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[3]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from applications.memory_feature_validation.scripts.campaign_common import (  # noqa: E402,I001
    CaseSpec,
    INJECTION_PROBES,
    SCENARIO_ORDER,
    SECRET_PROBES,
    group_cases,
    probe_present,
    retryable_scan_error_attempt,
    secret_probe_present,
    select_cases,
    validated_artifact_scan_errors,
)
from src.extensions.self_learning.redaction import (  # noqa: E402
    BLOCKED_TEXT,
    redact_text,
    sanitize_value_fragments,
    scan_injection_patterns,
)

_TERMINAL_JOB_STATUSES = {"succeeded", "dead"}
_DETACHED_CLAIM_GRACE_SECONDS = 10.0
_INFRA_ERROR_PATTERNS = (
    ("http_429", re.compile(r"(?:http|status(?:\s+code)?)\s*[:=]?\s*429\b", re.IGNORECASE)),
    ("http_502", re.compile(r"(?:http|status(?:\s+code)?)\s*[:=]?\s*502\b", re.IGNORECASE)),
    ("http_503", re.compile(r"(?:http|status(?:\s+code)?)\s*[:=]?\s*503\b", re.IGNORECASE)),
    ("http_504", re.compile(r"(?:http|status(?:\s+code)?)\s*[:=]?\s*504\b", re.IGNORECASE)),
    ("rate_limit", re.compile(r"\brate[ -]?limit(?:ed|ing)?\b", re.IGNORECASE)),
    ("connection_refused", re.compile(r"\bconnection refused\b", re.IGNORECASE)),
    ("connection_reset", re.compile(r"\bconnection reset\b", re.IGNORECASE)),
    ("connection_aborted", re.compile(r"\bconnection aborted\b", re.IGNORECASE)),
    (
        "name_resolution",
        re.compile(r"\b(?:temporary failure in )?name resolution\b", re.IGNORECASE),
    ),
    ("service_unavailable", re.compile(r"\bservice unavailable\b", re.IGNORECASE)),
    ("gateway_timeout", re.compile(r"\bgateway timeout\b", re.IGNORECASE)),
    ("read_timeout", re.compile(r"\bread timed out\b", re.IGNORECASE)),
    ("connect_timeout", re.compile(r"\bconnect timeout\b", re.IGNORECASE)),
)
_SEMANTIC_EXCEPTION_RE = re.compile(
    r"(?:^|\s)(?:AssertionError|ValueError|TypeError|KeyError|LookupError|"
    r"ValidationError|NotImplementedError)\s*:",
    re.IGNORECASE,
)
_TRANSPORT_STATUS_LINE_RE = re.compile(
    r"^\s*(?:(?:HTTP(?:/\d(?:\.\d)?)?|status(?:\s+code)?|error\s+code)\s*[:=]?\s*"
    r"(?:429|502|503|504)\b)",
    re.IGNORECASE,
)
_TRANSPORT_EXCEPTION_LINE_RE = re.compile(
    r"(?:^|[\s.])(?:APIConnectionError|APITimeoutError|RateLimitError|"
    r"ConnectionError|ConnectError|ReadTimeout|ConnectTimeout|TimeoutError|"
    r"NameResolutionError|ServiceUnavailableError|GatewayTimeoutError)\s*:",
    re.IGNORECASE,
)
_TRANSPORT_LOG_LINE_RE = re.compile(
    r"^\s*(?:\[[^\]]*\b(?:ERROR|WARNING|CRITICAL)\b[^\]]*\]|"
    r"(?:ERROR|WARNING|CRITICAL)\b)",
    re.IGNORECASE,
)
_BARE_TRANSPORT_PREFIXES = (
    "connection refused",
    "connection reset",
    "connection aborted",
    "temporary failure in name resolution",
    "name resolution",
    "service unavailable",
    "gateway timeout",
    "read timed out",
    "connect timeout",
    "rate limited",
    "rate limit exceeded",
)
_SUMMARY_REQUEST_RE = re.compile(
    r"Requested model type ['\"]summary['\"], resolved to:\s*summary",
    re.IGNORECASE,
)
_SUMMARY_MODEL_RE = re.compile(
    r"Resolved smolagents model:\s*summary\s*->\s*([^\s]+)",
    re.IGNORECASE,
)
_SESSION_END_LATENCY_RE = re.compile(
    r"Hook executed:\s*func=session_finalize_hook\s+success=(?:True|False)\s+duration=([0-9.]+)s",
    re.IGNORECASE,
)
_MAX_CAMPAIGN_SECONDS = 8 * 60 * 60


def _now() -> str:
    return datetime.now().astimezone().isoformat()


def _default_campaign_id(dry_run: bool) -> str:
    prefix = "dry-run" if dry_run else "summary"
    return f"{prefix}-{datetime.now().astimezone().strftime('%Y%m%dT%H%M%S%z')}"


def _create_campaign_dir(output_root: Path, campaign_id: str) -> Path:
    """Create one new campaign directory and refuse every form of reuse."""
    campaign_id = str(campaign_id or "").strip()
    if not campaign_id:
        raise ValueError("campaign id must not be empty")
    if Path(campaign_id).name != campaign_id or campaign_id in {".", ".."}:
        raise ValueError("campaign id must be one path component")
    output_root = output_root.expanduser().resolve()
    output_root.mkdir(parents=True, exist_ok=True)
    campaign_dir = output_root / campaign_id
    try:
        campaign_dir.mkdir(exist_ok=False)
    except FileExistsError as exc:
        raise FileExistsError(
            f"campaign directory already exists; choose a fresh --campaign-id: {campaign_dir}"
        ) from exc
    return campaign_dir


def _write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True, default=str) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def _atomic_write_text(path: Path, value: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(value, encoding="utf-8")
    temporary.replace(path)


def _progress_payload(
    cases: list[CaseSpec],
    result_by_id: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    """Return an interruption-safe checkpoint with enums and counters only."""
    results: list[dict[str, Any]] = []
    for case in cases:
        result = result_by_id.get(case.case_id)
        if result is None:
            continue
        attempts: list[dict[str, Any]] = []
        for attempt in result.get("attempts") or []:
            wait = attempt.get("job_wait") or {}
            artifact_findings = [
                {
                    key: finding[key]
                    for key in (
                        "path",
                        "kind",
                        "scope",
                        "error_type",
                        "probe_label",
                        "probe_sha256_prefix",
                        "storage",
                        "table",
                        "column",
                    )
                    if key in finding
                }
                for finding in (attempt.get("artifact_scan") or {}).get("probe_hits") or []
                if isinstance(finding, dict)
            ]
            attempts.append(
                {
                    "attempt": int(attempt.get("attempt") or 0),
                    "returncode": int(attempt.get("returncode") or 0),
                    "timed_out": bool(attempt.get("timed_out")),
                    "deadline_exceeded": bool(attempt.get("deadline_exceeded")),
                    "failure_kind": str(attempt.get("failure_kind") or ""),
                    "artifact_finding_count": int(
                        (attempt.get("artifact_scan") or {}).get("finding_count") or 0
                    ),
                    "artifact_findings": artifact_findings,
                    "live_db_unchanged": (attempt.get("isolation_evidence") or {}).get(
                        "live_db_unchanged"
                    ),
                    "jobs_terminal": bool(wait.get("terminal")),
                    "job_read_error_count": len(wait.get("read_errors") or []),
                    "jobs": [
                        {
                            "id": int(job.get("id") or 0),
                            "kind": str(job.get("kind") or ""),
                            "status": str(job.get("status") or ""),
                            "attempts": int(job.get("attempts") or 0),
                            "error_kind": str(job.get("error_kind") or ""),
                            "artifact_delivery": str(job.get("artifact_delivery") or ""),
                            "distilled_by": str(job.get("distilled_by") or ""),
                        }
                        for job in wait.get("jobs") or []
                    ],
                }
            )
        results.append(
            {
                "case_id": case.case_id,
                "scenario": case.scenario,
                "status": str(result.get("status") or ""),
                "attempts": attempts,
            }
        )
    return sanitize_value_fragments(
        {
            "updated_at": _now(),
            "completed_cases": len(results),
            "results": results,
        }
    )


def _write_progress(
    campaign_dir: Path,
    cases: list[CaseSpec],
    result_by_id: dict[str, dict[str, Any]],
) -> None:
    _write_json(
        campaign_dir / "attempt_progress.json",
        _progress_payload(cases, result_by_id),
    )


def _write_case_progress(
    campaign_dir: Path,
    case: CaseSpec,
    result: dict[str, Any],
) -> None:
    _write_json(
        campaign_dir / "attempt_progress" / f"{case.case_id}.json",
        _progress_payload([case], {case.case_id: result}),
    )


def _git_head() -> str:
    completed = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=REPO_ROOT,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        check=False,
    )
    return completed.stdout.strip() if completed.returncode == 0 else "unknown"


def _loom_command(workflow: str) -> list[str]:
    """Return the production CLI command; never bypass Click with run_app()."""
    executable = shutil.which("loom")
    if not executable:
        raise FileNotFoundError(
            "loom executable was not found on PATH; run the campaign via `uv run python ...`"
        )
    return [executable, "run", workflow, "--log-to-file"]


def _worker_command(db_path: Path, max_wait_seconds: float) -> list[str]:
    executable = shutil.which("loom")
    if not executable:
        raise FileNotFoundError("loom executable was not found on PATH")
    return [
        executable,
        "_memory-worker",
        "--db",
        str(db_path),
        "--max-wait",
        str(max(1.0, float(max_wait_seconds))),
    ]


def _assert_isolated_state_root(state_root: Path) -> Path:
    state_root = state_root.expanduser().resolve()
    db_path = state_root / "self_learning.db"
    live_db = (REPO_ROOT / ".agentloom" / "self_learning.db").resolve()
    if db_path.resolve() == live_db:
        raise RuntimeError("campaign refused to use the live .agentloom/self_learning.db")
    state_root.mkdir(parents=True, exist_ok=True)
    return db_path


def _resolved_model_contract() -> dict[str, str]:
    """Resolve the exact global config that detached distillers will read."""
    from src.extensions.self_learning.paths import memory_config
    from src.lib.smolagents.models.model_types import ModelTypeManager

    application_type = "summary"
    distill_type = str(memory_config().get("distill_model") or "").strip()

    def resolve(model_type: str) -> tuple[str, str]:
        resolved = ModelTypeManager.resolve_model_type(model_type)
        model = ModelTypeManager.get_llm_config(resolved)
        return resolved.value, str(model.model_id)

    app_resolved, app_model = resolve(application_type)
    distill_resolved, distill_model = resolve(distill_type)
    return {
        "application_requested_type": application_type,
        "application_resolved_type": app_resolved,
        "application_model_id": app_model,
        "distiller_requested_type": distill_type,
        "distiller_resolved_type": distill_resolved,
        "distiller_model_id": distill_model,
    }


def _probe_hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:16]


def _file_fingerprint(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {"exists": False, "size": 0, "sha256": ""}
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return {
        "exists": True,
        "size": path.stat().st_size,
        "sha256": digest.hexdigest(),
    }


def _database_fingerprint(db_path: Path) -> dict[str, Any]:
    """Fingerprint SQLite main/WAL/SHM so an uncheckpointed write is visible."""
    return {
        label: _file_fingerprint(Path(str(db_path) + suffix))
        for label, suffix in (("db", ""), ("wal", "-wal"), ("shm", "-shm"))
    }


@contextmanager
def _hold_wal_for_precheckpoint_privacy(db_path: Path):
    """Pin WAL frames so an insert-delete-checkpoint leak remains auditable.

    The validation database is initialized before the child starts, then a
    read transaction is kept open until the caller has scanned the state tree.
    SQLite cannot discard WAL frames visible to that reader, so a value that
    was briefly persisted cannot disappear before the privacy oracle observes
    it.
    """
    from src.extensions.self_learning.ledger import SelfLearningLedger

    SelfLearningLedger(db_path)
    keeper = sqlite3.connect(db_path)
    try:
        keeper.execute("PRAGMA journal_mode=WAL")
        keeper.execute("PRAGMA wal_autocheckpoint=0")
        keeper.execute("BEGIN")
        keeper.execute("SELECT COUNT(*) FROM schema_version").fetchone()
        yield keeper
    finally:
        try:
            keeper.rollback()
        finally:
            # Never reset/truncate a live campaign WAL here. A detached
            # learning worker can retain the WAL/SHM mapping briefly after its
            # job commits; resetting that generation on macOS can hide or lose
            # the next cohort phase's committed frames. The privacy scan has
            # already inspected the pinned frames before this reader closes.
            # One-shot forensic copies own any later physical cleanup.
            keeper.close()


def _probe_findings(
    data: bytes,
    *,
    path: str = "",
    binary_container: bool | None = None,
) -> list[dict[str, Any]]:
    findings: list[dict[str, Any]] = []
    if binary_container is None:
        binary_container = Path(path).name.endswith((".db", "-wal", "-shm"))
    for kind, probes, injection in (
        ("secret", SECRET_PROBES, False),
        ("injection", INJECTION_PROBES, True),
    ):
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
            if present:
                findings.append(
                    {
                        "path": path,
                        "kind": kind,
                        "probe_label": label,
                        "probe_sha256_prefix": _probe_hash(probe),
                    }
                )
    return findings


def _scan_error_finding(path: str, exc: BaseException, *, scope: str) -> dict[str, Any]:
    """Describe an unreadable artifact without persisting its exception text."""
    return {
        "path": path,
        "kind": "scan_error",
        "scope": scope,
        "error_type": type(exc).__name__,
    }


def _quote_sqlite_identifier(value: str) -> str:
    return '"' + str(value).replace('"', '""') + '"'


def _inspect_sqlite_logical_text(
    db_path: Path,
    *,
    display_path: str,
) -> list[dict[str, Any]]:
    """Scan logical SQLite values so short probes are not lost in binary noise.

    Raw page scans deliberately require key context for the three-byte password
    fixture because it can occur by chance in a large DB/WAL. Once SQLite has
    decoded a persisted TEXT/BLOB cell, that collision concern no longer
    applies: exact and residual probes must be checked in full, including FTS
    virtual-table content.
    """
    for attempt in range(3):
        findings: list[dict[str, Any]] = []
        seen: set[tuple[str, str, str, str]] = set()
        try:
            uri = db_path.resolve().as_uri() + "?mode=ro"
            with sqlite3.connect(uri, uri=True) as conn:
                conn.row_factory = sqlite3.Row
                conn.execute("PRAGMA query_only=ON")
                table_rows = conn.execute(
                    "SELECT name FROM sqlite_schema "
                    "WHERE type = 'table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
                ).fetchall()
                for table_row in table_rows:
                    table = str(table_row["name"])
                    quoted_table = _quote_sqlite_identifier(table)
                    column_rows = conn.execute(
                        f"PRAGMA table_xinfo({quoted_table})"
                    ).fetchall()
                    columns = [
                        str(column["name"])
                        for column in column_rows
                        if int(column["hidden"] or 0) == 0
                    ]
                    if not columns:
                        continue
                    projection = ", ".join(
                        _quote_sqlite_identifier(column) for column in columns
                    )
                    for row in conn.execute(f"SELECT {projection} FROM {quoted_table}"):
                        for column, value in zip(columns, row, strict=True):
                            if isinstance(value, str):
                                encoded = value.encode("utf-8", errors="replace")
                            else:
                                # BLOB cells (notably FTS shadow blocks) are
                                # binary containers. Their short-probe
                                # collision policy belongs to the raw scan.
                                continue
                            for finding in _probe_findings(
                                encoded,
                                path=display_path,
                                binary_container=False,
                            ):
                                key = (
                                    table,
                                    column,
                                    str(finding.get("kind") or ""),
                                    str(finding.get("probe_label") or ""),
                                )
                                if key in seen:
                                    continue
                                seen.add(key)
                                findings.append(
                                    {
                                        **finding,
                                        "storage": "sqlite_logical",
                                        "table": table,
                                        "column": column,
                                    }
                                )
            return findings
        except (OSError, sqlite3.Error) as exc:
            if attempt < 2:
                time.sleep((0.02, 0.05)[attempt])
                continue
            return [
                _scan_error_finding(
                    display_path,
                    exc,
                    scope="sqlite_logical",
                )
            ]
    raise AssertionError("unreachable SQLite scan retry state")


def _inspect_artifact_tree(root: Path) -> dict[str, Any]:
    """Read-only raw-probe inspection; this function never repairs artifacts."""
    root = Path(root)
    if not root.exists():
        return {"ok": True, "finding_count": 0, "probe_hits": []}
    paths = [root] if root.is_file() else sorted(path for path in root.rglob("*") if path.is_file())
    findings: list[dict[str, Any]] = []
    for path in paths:
        try:
            relative = str(path.relative_to(root)) if root.is_dir() else path.name
        except ValueError:
            relative = str(path)
        try:
            data = path.read_bytes()
        except OSError as exc:
            findings.append(
                _scan_error_finding(relative, exc, scope="artifact_bytes")
            )
            continue
        findings.extend(_probe_findings(data, path=relative))
        if path.suffix.casefold() == ".db":
            findings.extend(
                _inspect_sqlite_logical_text(path, display_path=relative)
            )
    return {
        "ok": not findings,
        "finding_count": len(findings),
        "probe_hits": findings,
    }


def _sanitize_captured_output(value: str) -> tuple[str, dict[str, Any]]:
    """Sanitize an in-memory child stream before its first filesystem write."""
    raw = str(value or "")
    source_findings = _probe_findings(raw.encode("utf-8", errors="replace"))
    safe = redact_text(raw)
    # Known synthetic support-set values may appear without their structured
    # key in terminal rendering. Exact replacement is defense in depth after
    # the generic credential scanner.
    for label, probe in SECRET_PROBES.items():
        safe = safe.replace(probe, f"[REDACTED:{label}]")
    injection_ids = scan_injection_patterns(safe)
    if injection_ids:
        safe = BLOCKED_TEXT
    if _probe_findings(safe.encode("utf-8", errors="replace")):
        safe = BLOCKED_TEXT
    if not safe.endswith("\n"):
        safe += "\n"
    return safe, {
        "prewrite_sanitized": True,
        "source_probe_hit_count": len(source_findings),
        "source_probe_hits": source_findings,
        "injection_pattern_ids": sorted(set(injection_ids)),
        "written_bytes": len(safe.encode("utf-8")),
    }


def _write_captured_log(path: Path, value: str) -> dict[str, Any]:
    """Cross the campaign log boundary once: sanitize in memory, then write."""
    safe, report = _sanitize_captured_output(value)
    _atomic_write_text(path, safe)
    return report


def _seed_state(case: CaseSpec, state_root: Path) -> dict[str, Any]:
    """Create deterministic preconditions without changing global process env."""
    state_root.mkdir(parents=True, exist_ok=True)
    if not case.seed:
        return {"seed": "none"}
    from src.extensions.self_learning.memory_store import MemoryStore

    store = MemoryStore(state_root / "self_learning.db")
    if case.seed == "recall":
        project = store.add(
            "project",
            "The internal data lake nickname is Orchid",
            proposal=False,
            source="campaign_seed",
        )
        application = store.add(
            "app",
            "Uploads for memory validation must use region ap-southeast-1",
            proposal=False,
            source="campaign_seed",
            scope_id="memory_feature_validation",
        )
        foreign_application = store.add(
            "app",
            "Uploads for memory validation must use region eu-west-1",
            proposal=False,
            source="campaign_seed",
            scope_id="foreign_validation_app",
        )
        return {
            "seed": "recall",
            "project_item_id": project.get("id"),
            "application_item_id": application.get("id"),
            "foreign_application_item_id": foreign_application.get("id"),
        }
    if case.seed == "revision":
        old = store.add(
            "project",
            "The export endpoint uses legacy XML envelopes",
            proposal=False,
            source="campaign_seed",
        )
        old_id = int(old["id"])
        late_old_run = f"late-old-{case.case_id}"
        store.record_injections(late_old_run, [old_id])
        revision = store.replace(
            "project",
            str(old_id),
            "The export endpoint always returns text/csv rather than JSON",
            proposal=False,
            source="campaign_seed",
        )
        new_id = int(revision["new_id"])
        before_late = next(item for item in store.list(include_pending=True) if int(item["id"]) == new_id)
        # These arrive *after* replacement and are deliberately attributed to
        # the old injected revision. They must never mutate generation 2.
        store.record_run_outcome(late_old_run, True)
        try:
            old_feedback = store.feedback(str(old_id), helpful=False, restrict_to_run=late_old_run)
        except (KeyError, ValueError) as exc:
            old_feedback = {"ok": False, "error": type(exc).__name__}
        after_late = next(item for item in store.list(include_pending=True) if int(item["id"]) == new_id)
        return {
            "seed": "revision",
            "old_item_id": old_id,
            "new_item_id": new_id,
            "generation": revision.get("generation"),
            "late_old_run_id": late_old_run,
            "old_feedback_ok": bool(old_feedback.get("ok")),
            "new_before_late": {
                "trust_score": before_late.get("trust_score"),
                "helpful_count": before_late.get("helpful_count"),
                "unhelpful_count": before_late.get("unhelpful_count"),
            },
            "new_after_late": {
                "trust_score": after_late.get("trust_score"),
                "helpful_count": after_late.get("helpful_count"),
                "unhelpful_count": after_late.get("unhelpful_count"),
            },
        }
    raise ValueError(f"Unknown campaign seed: {case.seed}")


def _max_job_id(db_path: Path) -> int:
    if not db_path.exists():
        return 0
    observer = Path(__file__).with_name("observe_learning_jobs.py")
    try:
        completed = subprocess.run(
            [
                sys.executable,
                str(observer),
                "--db",
                str(db_path),
                "--max-id-only",
            ],
            cwd=REPO_ROOT,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            check=False,
            timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise _JobObservationError(type(exc).__name__) from None
    try:
        payload = json.loads(str(completed.stdout or ""))
    except (TypeError, json.JSONDecodeError):
        raise _JobObservationError("InvalidObserverOutput") from None
    if (
        completed.returncode != 0
        or not isinstance(payload, dict)
        or payload.get("ok") is not True
    ):
        error_type = (
            str(payload.get("error_type") or "ObserverProcessError")
            if isinstance(payload, dict)
            else "ObserverProcessError"
        )
        raise _JobObservationError(error_type)
    try:
        return max(0, int(payload["max_id"]))
    except (KeyError, TypeError, ValueError):
        raise _JobObservationError("InvalidObserverOutput") from None


def _error_kind(value: str) -> str:
    return str(_transport_evidence(value).get("kind") or "")


def _transport_evidence(value: str) -> dict[str, str]:
    """Return only an allowlisted transport enum; never retain raw text."""
    text_value = str(value or "")
    if not text_value:
        return {"kind": "", "signal": ""}
    lines = [line.strip() for line in text_value.splitlines() if line.strip()]
    # A semantic exception is authoritative for this attempt. Its message can
    # quote HTTP statuses or transport phrases as test data; scanning that body
    # would incorrectly grant an infrastructure retry.
    if any(_SEMANTIC_EXCEPTION_RE.search(line) for line in lines):
        return {"kind": "semantic_or_code", "signal": ""}
    for line in lines:
        structured = bool(
            _TRANSPORT_STATUS_LINE_RE.search(line)
            or _TRANSPORT_EXCEPTION_LINE_RE.search(line)
            or _TRANSPORT_LOG_LINE_RE.search(line)
            or line.casefold().startswith(_BARE_TRANSPORT_PREFIXES)
        )
        if not structured:
            continue
        for signal, pattern in _INFRA_ERROR_PATTERNS:
            if pattern.search(line):
                return {"kind": "infrastructure", "signal": signal}
    return {"kind": "", "signal": ""}


def _read_log(log_path: str | Path, *, max_chars: int = 200_000) -> str:
    path = Path(log_path)
    if not path.exists():
        return ""
    text_value = path.read_text(encoding="utf-8", errors="replace")
    return text_value[-max_chars:]


def _model_log_evidence(
    log_path: Path,
    runtime_root: Path | None = None,
    *,
    captured_text: str = "",
) -> dict[str, Any]:
    parts = [captured_text if captured_text else _read_log(log_path)]
    if runtime_root and runtime_root.exists():
        for path in runtime_root.rglob("*"):
            if path.is_file() and path.suffix.casefold() not in {".db", ".sqlite", ".wal", ".shm"}:
                parts.append(_read_log(path, max_chars=100_000))
    log_text = "\n".join(parts)
    resolved_ids = sorted(set(_SUMMARY_MODEL_RE.findall(log_text)))
    hook_latencies = [float(value) for value in _SESSION_END_LATENCY_RE.findall(log_text)]
    return {
        "summary_requested_and_resolved": bool(_SUMMARY_REQUEST_RE.search(log_text)),
        "summary_model_ids": resolved_ids,
        "session_finalize_hook_latencies_seconds": hook_latencies,
    }


class _JobObservationError(RuntimeError):
    """Sanitized SQLite observation failure used by campaign retry logic."""

    def __init__(self, error_type: str):
        self.error_type = str(error_type or "SQLiteError")
        super().__init__(self.error_type)


def _new_jobs(db_path: Path, after_id: int) -> list[dict[str, Any]]:
    observer = Path(__file__).with_name("observe_learning_jobs.py")
    try:
        completed = subprocess.run(
            [
                sys.executable,
                str(observer),
                "--db",
                str(db_path),
                "--after-id",
                str(int(after_id)),
            ],
            cwd=REPO_ROOT,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            check=False,
            timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise _JobObservationError(type(exc).__name__) from None
    try:
        payload = json.loads(str(completed.stdout or ""))
    except (TypeError, json.JSONDecodeError):
        raise _JobObservationError("InvalidObserverOutput") from None
    if completed.returncode != 0 or not isinstance(payload, dict) or payload.get("ok") is not True:
        error_type = str(payload.get("error_type") or "ObserverProcessError") if isinstance(payload, dict) else "ObserverProcessError"
        raise _JobObservationError(error_type)
    jobs = payload.get("jobs")
    if not isinstance(jobs, list) or any(not isinstance(job, dict) for job in jobs):
        raise _JobObservationError("InvalidObserverOutput")
    return jobs


def _wait_for_jobs(
    db_path: Path,
    after_id: int,
    timeout_seconds: float,
    *,
    deadline_monotonic: float | None = None,
    initial_read_errors: list[dict[str, str]] | None = None,
    recover_when_unleased: Callable[[], None] | None = None,
) -> dict[str, Any]:
    deadline = time.monotonic() + max(0.1, float(timeout_seconds))
    if deadline_monotonic is not None:
        deadline = min(deadline, deadline_monotonic)
    last_jobs: list[dict[str, Any]] = []
    read_errors = list(initial_read_errors or [])
    recovery_attempted = False
    while time.monotonic() < deadline:
        try:
            last_jobs = _new_jobs(db_path, after_id)
        except _JobObservationError as exc:
            finding = {"kind": "sqlite_observation", "error_type": exc.error_type}
            if finding not in read_errors:
                read_errors.append(finding)
            time.sleep(0.5)
            continue
        has_review = any(job["kind"] == "session_review" for job in last_jobs)
        database_terminal = all(
            job["status"] in _TERMINAL_JOB_STATUSES for job in last_jobs
        )
        artifacts_settled = all(
            job.get("artifact_delivery") in {"delivered", "failed", "not_required"}
            for job in last_jobs
        )
        if has_review and database_terminal and artifacts_settled:
            return {
                "terminal": True,
                "timed_out": False,
                "jobs": last_jobs,
                "read_errors": read_errors,
            }
        active_claim = any(
            job.get("status") == "running" and job.get("lease_active")
            for job in last_jobs
        )
        if (
            last_jobs
            and not database_terminal
            and not active_claim
            and recover_when_unleased is not None
            and not recovery_attempted
        ):
            recovery_attempted = True
            recover_when_unleased()
        time.sleep(0.5)
    return {
        "terminal": False,
        "timed_out": True,
        "jobs": last_jobs,
        "read_errors": read_errors,
    }


def _decode_final_answer(value: Any) -> Any:
    if value in (None, ""):
        return None
    if not isinstance(value, str):
        return value
    try:
        decoded = json.loads(value)
    except json.JSONDecodeError:
        return value
    if isinstance(decoded, str):
        try:
            return json.loads(decoded)
        except json.JSONDecodeError:
            return decoded
    return decoded


def _final_answer_from_db(db_path: Path, root_run_id: str) -> Any:
    if not db_path.exists() or not root_run_id:
        return None
    try:
        with sqlite3.connect(f"file:{db_path}?mode=ro", uri=True) as conn:
            row = conn.execute(
                "SELECT final_answer FROM runs WHERE root_run_id = ? "
                "AND final_answer != '' ORDER BY COALESCE(ended_at, indexed_at) DESC LIMIT 1",
                (root_run_id,),
            ).fetchone()
    except sqlite3.Error:
        return None
    return _decode_final_answer(row[0]) if row else None


def _final_answer_from_text(value: str) -> Any:
    """Recover Click's final ``loom run`` echo from an in-memory stream."""
    for line in reversed(str(value or "").splitlines()):
        candidate = line.strip()
        if not candidate or candidate[:1] not in {"{", "[", '"'}:
            continue
        decoded = _decode_final_answer(candidate)
        if isinstance(decoded, (dict, list)):
            return decoded
    return None


def _final_answer_from_log(log_path: Path) -> Any:
    """Recover Click's final ``loom run`` echo from an already-safe log."""
    return _final_answer_from_text(_read_log(log_path))


def _attempt_failure_kind(attempt: dict[str, Any]) -> str:
    """Classify only explicit transient transport failures as retryable."""
    if (attempt.get("isolation_evidence") or {}).get("live_db_unchanged") is False:
        return "semantic_or_code"
    artifact_scan = attempt.get("artifact_scan") or {}
    artifact_scan_errors = validated_artifact_scan_errors(artifact_scan)
    # A real secret/injection hit is never retryable. Missing or malformed
    # finding details also fail closed; only explicit read/SQLite scan errors
    # can become infrastructure after the Application and jobs prove success.
    if artifact_scan_errors is None:
        return "semantic_or_code"
    if attempt.get("deadline_exceeded"):
        return "deadline"
    wait = attempt.get("job_wait") or {}
    jobs = wait.get("jobs", [])
    log_kind = _error_kind(_read_log(str(attempt.get("log_path") or "")))
    prewrite_kind = str((attempt.get("transport_evidence") or {}).get("kind") or "")
    # The in-memory pre-write classification sees the original child stream;
    # an artifact log may legitimately be the single token ``[BLOCKED]``. Use
    # the log only when no pre-write enum exists.
    execution_kind = prewrite_kind or log_kind
    explicit_semantic = execution_kind == "semantic_or_code"
    explicit_infrastructure = execution_kind == "infrastructure" or any(
        job.get("error_kind") == "infrastructure" for job in jobs
    )
    # A failed Application with no explicit transport signal is a semantic/code
    # failure even when the independent SQLite observer happened to report a
    # transient read error.  Letting observer telemetry override that result
    # would grant the semantic execution a clean-state retry and could mask it.
    if attempt.get("timed_out"):
        if explicit_semantic:
            return "semantic_or_code"
        return "infrastructure" if explicit_infrastructure else "semantic_or_code"
    if int(attempt.get("returncode") or 0) != 0:
        if explicit_semantic:
            return "semantic_or_code"
        return "infrastructure" if explicit_infrastructure else "semantic_or_code"
    if any(job.get("artifact_delivery") == "failed" for job in jobs):
        return "semantic_or_code"
    review_jobs = [job for job in jobs if job.get("kind") == "session_review"]
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
    if not wait.get("terminal"):
        if explicit_infrastructure or wait.get("read_errors"):
            return "infrastructure"
        return "semantic_or_code"
    if not review_jobs:
        return "semantic_or_code"
    # Observation errors are infrastructure only after the Application and its
    # semantic job outcome have both been shown successful.
    if wait.get("read_errors"):
        return "infrastructure"
    if artifact_scan_errors:
        if retryable_scan_error_attempt(attempt):
            return "infrastructure"
        return "semantic_or_code"
    return ""


def _attempt_is_infrastructure_failure(attempt: dict[str, Any]) -> bool:
    return _attempt_failure_kind(attempt) == "infrastructure"


def _run_attempt_body(
    case: CaseSpec,
    *,
    campaign_dir: Path,
    state_root: Path,
    attempt_number: int,
    timeout_seconds: int,
    job_timeout_seconds: int,
    deadline_monotonic: float,
) -> dict[str, Any]:
    runtime_root = campaign_dir / "runtime" / case.case_id / f"attempt-{attempt_number}"
    runtime_root.mkdir(parents=True, exist_ok=True)
    log_path = campaign_dir / "logs" / case.case_id / f"attempt-{attempt_number}.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    db_path = _assert_isolated_state_root(state_root)
    live_db_path = (REPO_ROOT / ".agentloom" / "self_learning.db").resolve()
    live_db_before = _database_fingerprint(live_db_path)
    before_job_id = _max_job_id(db_path)

    env = os.environ.copy()
    env.update(case.env)
    env.update(
        {
            "AGENT_LOOM_RUNTIME_ROOT": str(runtime_root),
            "AGENTLOOM_SELF_LEARNING_ROOT": str(state_root),
            "AGENTLOOM_MEMORY_CAMPAIGN_CASE_ID": case.case_id,
            "AGENTLOOM_MEMORY_CAMPAIGN_SAFE_ARTIFACTS": "1",
            "PYTHONUNBUFFERED": "1",
        }
    )
    command = _loom_command(case.workflow)
    started_at = _now()
    timed_out = False
    deadline_exceeded = False
    remaining = deadline_monotonic - time.monotonic()
    if remaining <= 0:
        return {
            "attempt": attempt_number,
            "started_at": started_at,
            "finished_at": _now(),
            "returncode": 124,
            "timed_out": False,
            "deadline_exceeded": True,
            "runtime_root": str(runtime_root),
            "self_learning_root": str(state_root),
            "log_path": str(log_path),
            "command": command,
            "before_job_id": before_job_id,
            "job_wait": {"terminal": False, "timed_out": True, "jobs": []},
            "final_answer": None,
            "model_evidence": {},
            "capture_boundary": {
                "prewrite_sanitized": True,
                "source_probe_hit_count": 0,
                "source_probe_hits": [],
                "injection_pattern_ids": [],
                "written_bytes": 0,
            },
            "artifact_scan": {"ok": True, "finding_count": 0, "probe_hits": []},
            "failure_kind": "deadline",
        }
    process_timeout = max(1.0, min(float(timeout_seconds), remaining))
    captured_output = ""
    try:
        completed = subprocess.run(
            command,
            cwd=REPO_ROOT,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            check=False,
            timeout=process_timeout,
        )
        captured_output = str(completed.stdout or "")
        returncode = int(completed.returncode)
    except subprocess.TimeoutExpired as exc:
        timed_out = True
        returncode = 124
        deadline_exceeded = time.monotonic() >= deadline_monotonic
        raw_timeout_output = exc.stdout or ""
        if isinstance(raw_timeout_output, bytes):
            captured_output = raw_timeout_output.decode("utf-8", errors="replace")
        else:
            captured_output = str(raw_timeout_output)
        timeout_kind = "CAMPAIGN_DEADLINE" if deadline_exceeded else "CAMPAIGN_TIMEOUT"
        captured_output += f"\n{timeout_kind} after {process_timeout:.1f}s\n"

    evidence = _model_log_evidence(
        log_path,
        captured_text=captured_output,
    )
    final_answer_from_capture = _final_answer_from_text(captured_output)
    capture_boundary = _write_captured_log(log_path, captured_output)

    remaining = max(0.0, deadline_monotonic - time.monotonic())
    # SessionEnd is synchronous only through the outbox commit, so even a failed
    # Application can have a valid review job. Once observed, always wait the
    # full configured terminal budget; process status must not truncate it.
    observation_errors: list[dict[str, str]] = []
    try:
        observed_jobs = _new_jobs(db_path, before_job_id)
    except _JobObservationError as exc:
        observed_jobs = []
        observation_errors.append(
            {"kind": "sqlite_observation", "error_type": exc.error_type}
        )
    # A just-committed WAL generation can become visible to an independent
    # reader only after the prior detached worker releases its mapping. Never
    # infer "no SessionEnd job" from the first read or collapse the explicit
    # post-loom terminal wait to five seconds.
    wait_budget = min(float(job_timeout_seconds), remaining)
    initial_wait_budget = min(
        _DETACHED_CLAIM_GRACE_SECONDS,
        max(0.1, wait_budget),
    )
    initial_wait = _wait_for_jobs(
        db_path,
        before_job_id,
        initial_wait_budget,
        deadline_monotonic=deadline_monotonic,
        initial_read_errors=observation_errors,
    )
    worker_recovery: dict[str, Any] = {"required": False, "command": [], "log_path": ""}
    initial_jobs = initial_wait.get("jobs", [])
    worker_has_claim = any(
        job.get("status") == "running" and job.get("lease_active")
        for job in initial_jobs
    )
    database_terminal = bool(initial_jobs) and all(
        job.get("status") in _TERMINAL_JOB_STATUSES for job in initial_jobs
    )
    should_recover = (
        bool(observed_jobs)
        and not initial_wait.get("terminal")
        and not database_terminal
        and not worker_has_claim
        and time.monotonic() < deadline_monotonic
    )
    def start_recovery_worker() -> None:
        nonlocal worker_recovery
        if worker_recovery.get("required") or time.monotonic() >= deadline_monotonic:
            return
        recovery_command = _worker_command(
            db_path,
            min(15.0, wait_budget, deadline_monotonic - time.monotonic()),
        )
        process = subprocess.Popen(
            recovery_command,
            cwd=REPO_ROOT,
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
            close_fds=True,
        )
        worker_recovery = {
            "required": True,
            "command": recovery_command,
            "log_path": "",
            "pid": int(process.pid),
        }

    if should_recover:
        start_recovery_worker()
    if initial_wait.get("terminal"):
        job_wait = initial_wait
    else:
        remaining_wait = max(
            0.1,
            min(
                wait_budget - initial_wait_budget,
                deadline_monotonic - time.monotonic(),
            ),
        )
        job_wait = _wait_for_jobs(
            db_path,
            before_job_id,
            remaining_wait,
            deadline_monotonic=deadline_monotonic,
            initial_read_errors=initial_wait.get("read_errors") or [],
            recover_when_unleased=(
                None if worker_recovery.get("required") else start_recovery_worker
            ),
        )
    review_jobs = [job for job in job_wait.get("jobs", []) if job.get("kind") == "session_review"]
    root_run_id = str(review_jobs[-1].get("root_run_id") or "") if review_jobs else ""
    runtime_scan = _inspect_artifact_tree(runtime_root)
    log_scan = _inspect_artifact_tree(log_path)
    artifact_hits = [*runtime_scan["probe_hits"], *log_scan["probe_hits"]]
    artifact_scan = {
        "ok": not artifact_hits,
        "finding_count": len(artifact_hits),
        "probe_hits": artifact_hits,
    }
    live_db_after = _database_fingerprint(live_db_path)
    final_answer = _final_answer_from_db(db_path, root_run_id) or final_answer_from_capture
    final_answer = sanitize_value_fragments(final_answer) if final_answer is not None else None
    result = {
        "attempt": attempt_number,
        "started_at": started_at,
        "finished_at": _now(),
        "returncode": returncode,
        "timed_out": timed_out,
        "deadline_exceeded": deadline_exceeded,
        "runtime_root": str(runtime_root),
        "self_learning_root": str(state_root),
        "log_path": str(log_path),
        "command": command,
        "before_job_id": before_job_id,
        "root_run_id": root_run_id,
        "job_wait": job_wait,
        "final_answer": final_answer,
        "model_evidence": evidence,
        "transport_evidence": _transport_evidence(captured_output),
        "capture_boundary": capture_boundary,
        "artifact_scan": artifact_scan,
        "worker_recovery": worker_recovery,
        "isolation_evidence": {
            "configured_self_learning_root": str(state_root.resolve()),
            "campaign_db_path": str(db_path.resolve()),
            "live_db_path": str(live_db_path),
            "live_db_sha256_before": live_db_before["db"].get("sha256", ""),
            "live_db_sha256_after": live_db_after["db"].get("sha256", ""),
            "live_db_bundle_before": live_db_before,
            "live_db_bundle_after": live_db_after,
            "live_db_unchanged": live_db_before == live_db_after,
        },
    }
    result["failure_kind"] = _attempt_failure_kind(result)
    return result


def _run_attempt(
    case: CaseSpec,
    *,
    campaign_dir: Path,
    state_root: Path,
    attempt_number: int,
    timeout_seconds: int,
    job_timeout_seconds: int,
    deadline_monotonic: float,
) -> dict[str, Any]:
    """Run one child, wait for outbox completion, then scan physical state.

    A long-lived read snapshot cannot be held on the live campaign database:
    cohort phase 2 and the detached worker must both observe/append the next
    WAL generation. Transient insert-delete coverage is exercised separately
    against one-shot forensic databases; this path scans the live DB/WAL/SHM
    immediately after the corresponding job reaches a terminal state.
    """
    _assert_isolated_state_root(state_root)
    result = _run_attempt_body(
        case,
        campaign_dir=campaign_dir,
        state_root=state_root,
        attempt_number=attempt_number,
        timeout_seconds=timeout_seconds,
        job_timeout_seconds=job_timeout_seconds,
        deadline_monotonic=deadline_monotonic,
    )
    state_scan = _inspect_artifact_tree(state_root)
    existing_scan = result.get("artifact_scan") or {}
    findings = [
        *(existing_scan.get("probe_hits") or []),
        *(state_scan.get("probe_hits") or []),
    ]
    result["precheckpoint_privacy_scan"] = state_scan
    result["artifact_scan"] = {
        "ok": not findings,
        "finding_count": len(findings),
        "probe_hits": findings,
    }
    result["failure_kind"] = _attempt_failure_kind(result)
    return result


def _clone_state(source: Path, destination: Path) -> None:
    if destination.exists():
        shutil.rmtree(destination)
    if source.exists():
        # ``copytree`` cannot snapshot a WAL database: the db, WAL, and SHM
        # files can belong to different generations when a detached worker is
        # still releasing its connection. Copy non-database artifacts first,
        # then use SQLite's online backup API for one coherent generation.
        shutil.copytree(
            source,
            destination,
            ignore=shutil.ignore_patterns(
                "self_learning.db",
                "self_learning.db-wal",
                "self_learning.db-shm",
            ),
        )
        source_db = source / "self_learning.db"
        if source_db.exists():
            destination_db = destination / "self_learning.db"
            snapshot_db = destination / ".self_learning.snapshot.db"
            with sqlite3.connect(
                f"file:{source_db}?mode=ro",
                uri=True,
                timeout=30,
            ) as source_conn, sqlite3.connect(snapshot_db, timeout=30) as snapshot_conn:
                source_conn.backup(snapshot_conn)
            os.replace(snapshot_db, destination_db)
            with sqlite3.connect(destination_db, timeout=30) as conn:
                has_maintenance = conn.execute(
                    "SELECT 1 FROM sqlite_master "
                    "WHERE type = 'table' AND name = 'maintenance'"
                ).fetchone()
                if has_maintenance:
                    # A cloned namespace has no process corresponding to a
                    # source worker/kick lease. Keeping those rows can block
                    # the clean retry until a 180-second TTL expires.
                    conn.execute(
                        "DELETE FROM maintenance WHERE key IN (?, ?)",
                        ("learning_worker_lease", "learning_worker_kick_lease"),
                    )
                integrity = str(conn.execute("PRAGMA integrity_check").fetchone()[0])
                if integrity != "ok":
                    raise RuntimeError(f"Cloned validation state is corrupt: {integrity}")
    else:
        destination.mkdir(parents=True, exist_ok=True)


def _run_case(
    case: CaseSpec,
    *,
    campaign_dir: Path,
    state_root: Path,
    timeout_seconds: int,
    job_timeout_seconds: int,
    deadline_monotonic: float,
) -> tuple[dict[str, Any], Path]:
    seed = _seed_state(case, state_root)
    snapshot = campaign_dir / "pre_attempt_state" / case.case_id
    _clone_state(state_root, snapshot)
    attempts: list[dict[str, Any]] = []
    current_root = state_root
    for attempt_number in (1, 2):
        if attempt_number == 2:
            current_root = campaign_dir / "retry_state" / case.case_id
            _clone_state(snapshot, current_root)
        attempt = _run_attempt(
            case,
            campaign_dir=campaign_dir,
            state_root=current_root,
            attempt_number=attempt_number,
            timeout_seconds=timeout_seconds,
            job_timeout_seconds=job_timeout_seconds,
            deadline_monotonic=deadline_monotonic,
        )
        attempts.append(attempt)
        if not _attempt_is_infrastructure_failure(attempt):
            break
    failure_kind = _attempt_failure_kind(attempts[-1])
    if not failure_kind:
        status = "completed"
    elif failure_kind == "infrastructure":
        status = "infrastructure_failed"
    elif failure_kind == "deadline":
        status = "deadline_exceeded"
    else:
        status = "semantic_or_code_failed"
    return (
        {
            **case.to_dict(),
            "status": status,
            "seed_result": seed,
            "attempts": attempts,
            "selected_attempt": attempts[-1]["attempt"],
            "runtime_root": attempts[-1]["runtime_root"],
            "self_learning_root": attempts[-1]["self_learning_root"],
            "final_answer": attempts[-1]["final_answer"],
            "job_wait": attempts[-1]["job_wait"],
        },
        current_root,
    )


def _run_group(
    cases: list[CaseSpec],
    *,
    campaign_dir: Path,
    timeout_seconds: int,
    job_timeout_seconds: int,
    deadline_monotonic: float,
    on_result: Callable[[CaseSpec, dict[str, Any]], None] | None = None,
) -> list[dict[str, Any]]:
    state_root = campaign_dir / "state" / cases[0].state_key
    state_root.mkdir(parents=True, exist_ok=True)
    results: list[dict[str, Any]] = []
    for case in cases:
        if results and results[-1]["status"] != "completed":
            result = {
                **case.to_dict(),
                "status": "dependency_failed",
                "attempts": [],
                "self_learning_root": str(state_root),
                "reason": f"prior cohort phase failed: {results[-1]['case_id']}",
            }
            results.append(result)
            if on_result is not None:
                on_result(case, result)
            continue
        result, state_root = _run_case(
            case,
            campaign_dir=campaign_dir,
            state_root=state_root,
            timeout_seconds=timeout_seconds,
            job_timeout_seconds=job_timeout_seconds,
            deadline_monotonic=deadline_monotonic,
        )
        results.append(result)
        if on_result is not None:
            on_result(case, result)
    return results


def _planned_results(cases: list[CaseSpec], campaign_dir: Path) -> list[dict[str, Any]]:
    return [
        {
            **case.to_dict(),
            "status": "planned",
            "runtime_root": str(campaign_dir / "runtime" / case.case_id / "attempt-1"),
            "self_learning_root": str(campaign_dir / "state" / case.state_key),
            "max_infrastructure_attempts": 2,
        }
        for case in cases
    ]


def _write_reproduction_commands(campaign_dir: Path, runs: int) -> None:
    script = "applications/memory_feature_validation/scripts/run_summary_campaign.py"
    commands = [
        "# Full reproduction uses a fresh campaign id and real summary-model calls.",
        f"uv run python {script} --runs {runs}",
        "",
        "# Safe plan-only smoke checks:",
        f"uv run python {script} --runs 1 --dry-run",
        f"uv run python {script} --runs 5 --dry-run",
        "",
        "# Re-audit this campaign without invoking a model:",
        "uv run python applications/memory_feature_validation/scripts/audit_campaign.py "
        f"{campaign_dir}",
    ]
    (campaign_dir / "reproduce_commands.txt").write_text("\n".join(commands) + "\n", encoding="utf-8")


def _deadline_seconds(value: str) -> int:
    seconds = int(value)
    if seconds <= 0 or seconds > _MAX_CAMPAIGN_SECONDS:
        raise argparse.ArgumentTypeError(
            f"deadline must be between 1 and {_MAX_CAMPAIGN_SECONDS} seconds"
        )
    return seconds


def main() -> int:
    campaign_started_monotonic = time.monotonic()
    campaign_started_at = _now()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runs", type=int, choices=(1, 5, 100), default=100)
    parser.add_argument("--dry-run", action="store_true", help="Write and audit the plan without model calls.")
    parser.add_argument("--campaign-id", default="")
    parser.add_argument(
        "--output-root",
        type=Path,
        default=REPO_ROOT / ".agentloom" / "validation" / "memory_feature_validation",
    )
    parser.add_argument("--timeout-seconds", type=int, default=600)
    parser.add_argument("--job-timeout-seconds", type=int, default=240)
    parser.add_argument("--max-workers", type=int, choices=(1, 2), default=2)
    parser.add_argument(
        "--deadline-seconds",
        type=_deadline_seconds,
        default=_MAX_CAMPAIGN_SECONDS,
        help="Hard campaign deadline; cannot exceed eight hours.",
    )
    args = parser.parse_args()

    campaign_id = args.campaign_id or _default_campaign_id(args.dry_run)
    campaign_dir = _create_campaign_dir(args.output_root, campaign_id)
    cases = select_cases(args.runs)
    model_contract = _resolved_model_contract()
    deadline_monotonic = campaign_started_monotonic + args.deadline_seconds
    canaries = [case for case in cases if case.canary_rank]
    quotas = Counter(case.scenario for case in cases)
    plan = {
        "schema_version": 1,
        "campaign_id": campaign_id,
        "created_at": _now(),
        "started_at": campaign_started_at,
        "dry_run": bool(args.dry_run),
        "requested_runs": args.runs,
        "selected_runs": len(cases),
        "scenario_order": list(SCENARIO_ORDER),
        "scenario_quotas": dict(quotas),
        "canary_count": min(5, len(cases)),
        "canary_case_ids": [case.case_id for case in sorted(canaries, key=lambda item: item.canary_rank)],
        "max_concurrency": args.max_workers,
        "infrastructure_retries": 1,
        "deadline_seconds": args.deadline_seconds,
        "cli_contract": "loom run <workflow> --log-to-file",
        "model_contract": model_contract,
        "job_terminal_statuses": sorted(_TERMINAL_JOB_STATUSES),
        "isolation": {
            "runtime": "unique per case attempt via AGENT_LOOM_RUNTIME_ROOT",
            "self_learning": "unique per case; only two-phase cohorts share a state checkpoint",
        },
        "artifact_safety": {
            "process_file_sink": "disabled by internal campaign env; terminal retained",
            "captured_stream": "sanitized in memory before atomic write",
            "command_hooks": "sanitized before temp/env/stdin serialization",
            "checkpoint": "disabled by application effective config",
            "wal": "live DB/WAL/SHM scanned after terminal job; never reset by campaign",
            "transient_forensics": "one-shot isolated databases in offline and contract suites",
            "audit": "read-only; no post-hoc repair",
        },
        "cases": [case.to_dict() for case in cases],
    }
    _write_json(campaign_dir / "plan.json", plan)
    _write_json(
        campaign_dir / "environment.json",
        {
            "created_at": _now(),
            "python": sys.version,
            "platform": sys.platform,
            "repo_root": str(REPO_ROOT),
            "git_head": _git_head(),
            "summary_model_configured": (REPO_ROOT / "config" / "llm.yaml").exists(),
            "model_contract": model_contract,
        },
    )
    _write_reproduction_commands(campaign_dir, args.runs)

    if args.dry_run:
        results = _planned_results(cases, campaign_dir)
    else:
        result_by_id: dict[str, dict[str, Any]] = {}
        canary_ids = set(plan["canary_case_ids"])
        canary_cases = [case for case in cases if case.case_id in canary_ids]
        for group in group_cases(canary_cases):
            for result in _run_group(
                group,
                campaign_dir=campaign_dir,
                timeout_seconds=args.timeout_seconds,
                job_timeout_seconds=args.job_timeout_seconds,
                deadline_monotonic=deadline_monotonic,
                on_result=lambda case, result: _write_case_progress(
                    campaign_dir, case, result
                ),
            ):
                result_by_id[result["case_id"]] = result
                _write_progress(campaign_dir, cases, result_by_id)
                print(f"CANARY {result['status']} {result['case_id']}", flush=True)
            if any(result_by_id[case.case_id]["status"] != "completed" for case in group):
                break

        from applications.memory_feature_validation.scripts.audit_campaign import (
            audit_canary_results,
        )

        ordered_canary_results = [
            result_by_id[case.case_id] for case in canary_cases if case.case_id in result_by_id
        ]
        canary_failures = audit_canary_results(campaign_dir, ordered_canary_results)
        _write_json(
            campaign_dir / "canary_audit.json",
            {"ok": not canary_failures, "failures": canary_failures},
        )
        if len(result_by_id) == len(canary_cases) and not canary_failures:
            remainder = [case for case in cases if case.case_id not in canary_ids]
            with ThreadPoolExecutor(max_workers=args.max_workers) as executor:
                futures = {
                    executor.submit(
                        _run_group,
                        group,
                        campaign_dir=campaign_dir,
                        timeout_seconds=args.timeout_seconds,
                        job_timeout_seconds=args.job_timeout_seconds,
                        deadline_monotonic=deadline_monotonic,
                        on_result=lambda case, result: _write_case_progress(
                            campaign_dir, case, result
                        ),
                    ): group
                    for group in group_cases(remainder)
                }
                for future in as_completed(futures):
                    for result in future.result():
                        result_by_id[result["case_id"]] = result
                        _write_progress(campaign_dir, cases, result_by_id)
                        print(f"RUN {result['status']} {result['case_id']}", flush=True)
        else:
            for case in cases:
                if case.case_id not in result_by_id:
                    result_by_id[case.case_id] = {
                        **case.to_dict(),
                        "status": "blocked_by_canary",
                        "attempts": [],
                    }
            _write_progress(campaign_dir, cases, result_by_id)
        results = [result_by_id[case.case_id] for case in cases]

    _write_json(campaign_dir / "results.json", results)
    elapsed_seconds = time.monotonic() - campaign_started_monotonic
    _write_json(
        campaign_dir / "campaign_timing.json",
        {
            "started_at": campaign_started_at,
            "finished_at": _now(),
            "elapsed_seconds": elapsed_seconds,
            "deadline_seconds": args.deadline_seconds,
            "within_eight_hours": elapsed_seconds <= _MAX_CAMPAIGN_SECONDS,
        },
    )

    from applications.memory_feature_validation.scripts.audit_campaign import audit_campaign

    report = audit_campaign(
        campaign_dir,
        campaign_started_monotonic=campaign_started_monotonic,
    )
    print(f"campaign_dir={campaign_dir}")
    print(f"audit={report['status']}")
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
