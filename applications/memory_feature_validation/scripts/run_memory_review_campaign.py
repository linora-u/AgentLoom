"""Run the simplified memory contract through real ``loom run`` Applications.

The runner is intentionally a black-box client.  It does not import the
self-learning implementation: configuration, CLI behavior, SQLite state, and
the model-visible result are observed exactly as an external operator would.
"""

from __future__ import annotations

import argparse
import ast
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import closing
from datetime import datetime
from pathlib import Path
from typing import Any

import yaml

_BOOTSTRAP_ROOT = Path(__file__).resolve().parents[3]
if str(_BOOTSTRAP_ROOT) not in sys.path:
    sys.path.insert(0, str(_BOOTSTRAP_ROOT))

from applications.memory_feature_validation.scripts.campaign_identity import (  # noqa: E402
    default_campaign_id as _default_campaign_id,
)
from applications.memory_feature_validation.scripts.memory_review_campaign_common import (  # noqa: E402
    ORACLE_PATH,
    REPO_ROOT,
    RunSpec,
    all_probe_markers,
    dataset_manifest,
    find_privacy_markers,
    grouped_runs,
    indexed_rows,
    normalize,
    select_runs,
    terms_match,
)

_TRANSPORT_STATUS_LINE_RE = re.compile(
    r"^\s*(?:(?:HTTP(?:/\d(?:\.\d)?)?|status(?:\s+code)?|error\s+code)\s*[:=]?\s*"
    r"(?:429|502|503|504)\b)",
    re.IGNORECASE,
)
_TRANSPORT_EXCEPTION_LINE_RE = re.compile(
    r"^\s*(?:(?:litellm(?:\.exceptions)?|openai|httpx|httpcore)\.)?"
    r"(?:APIConnectionError|APITimeoutError|BadGatewayError|RateLimitError|"
    r"ConnectionError|ConnectError|ReadTimeout|ConnectTimeout|TimeoutError|"
    r"NameResolutionError|ServiceUnavailableError|GatewayTimeoutError)\s*:",
    re.IGNORECASE,
)
_LITELLM_NATIVE_TIMEOUT_LINE_RE = re.compile(
    r"^\s*litellm(?:\.exceptions)?\.Timeout\s*:\s*Timeout Error\s*:",
    re.IGNORECASE,
)
_LITELLM_TIMEOUT_EXCEPTION_RE = re.compile(
    r"\blitellm\.Timeout\s*:\s*APITimeoutError\s*-\s*Request timed out"
    r"(?:\.(?:\s*Error_str:\s*Request timed out\.)?)?"
    r"(?=\s*(?:$|Execution failed:|Error while generating output:))",
    re.IGNORECASE,
)
_LOG_RECORD_RE = re.compile(r"^\s*(?:\[[^\]\r\n]*\])+\s*")
_ERROR_LEVEL_RE = re.compile(r"\[(?:ERROR|CRITICAL)\]", re.IGNORECASE)
_TRANSPORT_LOG_LEVEL_RE = re.compile(
    r"\[(?:ERROR|WARNING|CRITICAL)\]",
    re.IGNORECASE,
)
_ERROR_MESSAGE_RE = re.compile(
    r"^\s*(?:Error while generating output:|Execution failed:)",
    re.IGNORECASE,
)
_BARE_LOG_LEVEL_RE = re.compile(
    r"^\s*(?:ERROR|WARNING|CRITICAL)\b\s*[:=-]?\s*",
    re.IGNORECASE,
)
_INPUT_TOKEN_RE = re.compile(r"Input tokens:\s*[0-9,]+\s*\(\+([0-9,]+)\)")
_OUTPUT_TOKEN_RE = re.compile(r"Output tokens:\s*[0-9,]+\s*\(\+([0-9,]+)\)")
_MODEL_ID_RE = re.compile(r"Resolved smolagents model:\s*summary\s*->\s*([^\s]+)", re.IGNORECASE)
_ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]")
_SENSITIVE_ASSIGNMENT_RE = re.compile(
    r"(?i)\b(password|client[_-]?secret|authorization|refresh[_-]?token|"
    r"private[_-]?key|cookie|bearer)\b(\s*[:=]\s*)([^,}\]\s]+|\"[^\"]*\")"
)
_COMPLETION_MARKER = "Execution completed successfully."


def _now() -> str:
    return datetime.now().astimezone().isoformat()


def _write_text(path: Path, value: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(value, encoding="utf-8")
    temporary.replace(path)


def _write_json(path: Path, value: Any) -> None:
    _write_text(
        path,
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True, default=str) + "\n",
    )


def _campaign_dir(output_root: Path, campaign_id: str) -> Path:
    campaign_id = str(campaign_id or "").strip()
    if not campaign_id or Path(campaign_id).name != campaign_id:
        raise ValueError("campaign id must be one non-empty path component")
    output_root = output_root.expanduser().resolve()
    output_root.mkdir(parents=True, exist_ok=True)
    path = output_root / campaign_id
    path.mkdir(exist_ok=False)
    return path


def _loom() -> str:
    executable = shutil.which("loom")
    if executable:
        return executable
    candidate = REPO_ROOT / ".venv" / "bin" / "loom"
    if candidate.is_file():
        return str(candidate)
    raise FileNotFoundError("loom executable not found; run through `uv run python ...`")


def _safe_model_contract() -> dict[str, Any]:
    path = REPO_ROOT / "config" / "llm.yaml"
    if not path.exists():
        return {"configured": False, "requested_type": "summary", "model_id": ""}
    payload = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    model = payload.get("model") if isinstance(payload, dict) else {}
    summary = model.get("summary") if isinstance(model, dict) else {}
    summary = summary if isinstance(summary, dict) else {}
    return {
        "configured": bool(summary.get("model")),
        "requested_type": "summary",
        "model_id": str(summary.get("model") or ""),
        "temperature": summary.get("temperature"),
        "max_tokens": summary.get("max_tokens"),
        "timeout": summary.get("timeout"),
        "num_retries": summary.get("num_retries"),
    }


def _sanitize_text(value: str, markers: list[str]) -> str:
    text = str(value or "")
    hostile_phrases = (
        "ignore all previous instructions",
        "disregard the task",
        "SYSTEM MESSAGE:",
        "<system>",
        "<role>",
    )
    def wrapped_pattern(fragment: str) -> re.Pattern[str]:
        pieces: list[str] = []
        for character in fragment:
            pieces.append(r"\s+" if character.isspace() else re.escape(character) + r"\s*")
        return re.compile("".join(pieces), re.IGNORECASE)

    for fragment in (*markers, *hostile_phrases):
        if fragment:
            text = wrapped_pattern(fragment).sub("[BLOCKED]", text)

    safe_lines: list[str] = []
    for line in text.splitlines():
        if "[BLOCKED]" in line:
            safe_lines.append("[BLOCKED]")
            continue
        safe_lines.append(
            _SENSITIVE_ASSIGNMENT_RE.sub(lambda match: f"{match.group(1)}{match.group(2)}[REDACTED]", line)
        )
    return "\n".join(safe_lines)


def _sanitize_value(value: Any, markers: list[str]) -> Any:
    if isinstance(value, dict):
        return {str(key): _sanitize_value(item, markers) for key, item in value.items()}
    if isinstance(value, list):
        return [_sanitize_value(item, markers) for item in value]
    if isinstance(value, tuple):
        return [_sanitize_value(item, markers) for item in value]
    if isinstance(value, str):
        return _sanitize_text(value, markers)
    return value


def _decode_json(value: str) -> Any:
    result: Any = None
    for loader in (json.loads, ast.literal_eval):
        try:
            result = loader(value)
            break
        except (ValueError, SyntaxError, json.JSONDecodeError):
            continue
    else:
        return None
    if isinstance(result, str):
        for loader in (json.loads, ast.literal_eval):
            try:
                return loader(result)
            except (ValueError, SyntaxError, json.JSONDecodeError):
                continue
    return result


def _last_json(value: str) -> Any:
    plain = _ANSI_RE.sub("", str(value or ""))
    stripped = plain.strip()
    decoded = _decode_json(stripped)
    if decoded is not None:
        return decoded

    marker_index = plain.rfind(_COMPLETION_MARKER)
    if marker_index >= 0:
        tail = plain[marker_index + len(_COMPLETION_MARKER):]
        output_lines = []
        for line in tail.splitlines():
            candidate = line.strip()
            if not candidate:
                continue
            if re.match(r"^\[\d{4}-\d{2}-\d{2}", candidate):
                continue
            output_lines.append(candidate)
        candidate = " ".join(output_lines).strip()
        if candidate.casefold() == "missing":
            return "MISSING"
        decoded = _decode_json(candidate)
        return decoded if decoded is not None else (candidate or None)

    # Compatibility for small direct-unit payloads. Do not accept standalone
    # numbers/lists from incomplete runtime logs: token counters and exception
    # summaries are not final answers.
    for line in reversed(plain.splitlines()):
        candidate = line.strip()
        if candidate.casefold() == "missing":
            return "MISSING"
        if candidate[:1] not in {"{", "(", "'", '"'}:
            continue
        decoded = _decode_json(candidate)
        if decoded is not None:
            return decoded
    return None


def _completed_run_answer(
    raw_output: str,
    *,
    returncode: int,
    timed_out: bool,
) -> Any:
    """Parse only the declared result of a successful production loom run."""
    plain = _ANSI_RE.sub("", str(raw_output or ""))
    if timed_out or int(returncode) != 0 or _COMPLETION_MARKER not in plain:
        return None
    return _last_json(plain)


def _parse_key_values(value: str) -> dict[str, str]:
    result: dict[str, str] = {}
    for token in re.findall(r"([a-zA-Z_]+)=([^\s]+)", value):
        result[token[0].casefold()] = token[1].strip(" ,")
    return result


def _review_telemetry_records(raw_output: str) -> list[dict[str, str]]:
    """Parse logger-wrapped telemetry without mistaking an audit row for a call."""
    plain = _ANSI_RE.sub("", str(raw_output or ""))
    lines = plain.splitlines()
    records: list[dict[str, str]] = []
    for index, line in enumerate(lines):
        marker = "Memory review:"
        if marker not in line:
            continue
        fragments = [line.split(marker, 1)[1].strip()]
        cursor = index + 1
        while cursor < len(lines):
            continuation = lines[cursor].strip()
            if not continuation or continuation.startswith("[") or "=" not in continuation:
                break
            fragments.append(continuation)
            cursor += 1
        records.append(_parse_key_values(" ".join(fragments)))
    return records


def _model_evidence(raw_output: str) -> dict[str, Any]:
    input_tokens = [int(value.replace(",", "")) for value in _INPUT_TOKEN_RE.findall(raw_output)]
    output_tokens = [int(value.replace(",", "")) for value in _OUTPUT_TOKEN_RE.findall(raw_output)]
    review_records = _review_telemetry_records(raw_output)
    explicit_calls: list[int] = []
    for record in review_records:
        try:
            explicit_calls.append(int(record["calls"]))
        except (KeyError, TypeError, ValueError):
            continue
    return {
        "application_completion_calls": len(input_tokens),
        "application_input_tokens": sum(input_tokens),
        "application_output_tokens": sum(output_tokens),
        "summary_model_ids": sorted(set(_MODEL_ID_RE.findall(raw_output))),
        "review_records": review_records,
        "review_call_count": sum(explicit_calls) if explicit_calls else None,
        "review_input_tokens": sum(
            int(record.get("input_tokens") or 0)
            for record in review_records
            if str(record.get("input_tokens") or "").isdigit()
        ),
        "review_output_tokens": sum(
            int(record.get("output_tokens") or 0)
            for record in review_records
            if str(record.get("output_tokens") or "").isdigit()
        ),
    }


def _table_columns(conn: sqlite3.Connection, table: str) -> list[str]:
    return [str(row[1]) for row in conn.execute(f'PRAGMA table_info("{table}")')]


def _read_fixed_db_snapshot(
    conn: sqlite3.Connection,
    markers: list[str],
) -> dict[str, Any]:
    """Inspect one connection whose pages cannot change underneath the read."""
    tables = sorted(
        str(row[0])
        for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
    )
    integrity = str(conn.execute("PRAGMA integrity_check").fetchone()[0])
    items: list[dict[str, Any]] = []
    if "memory_items" in tables:
        available = _table_columns(conn, "memory_items")
        wanted = [
            name
            for name in (
                "id", "scope", "scope_type", "scope_id", "application_id",
                "status", "action", "content", "source", "source_run_id",
                "created_at", "updated_at",
            )
            if name in available
        ]
        if wanted:
            quoted = ", ".join(f'"{name}"' for name in wanted)
            for row in conn.execute(f"SELECT {quoted} FROM memory_items ORDER BY id"):
                item = dict(zip(wanted, row, strict=True))
                # In v5 every memory_items row is active; status is no longer
                # stored redundantly in the source table.
                item.setdefault("status", "active")
                items.append(_sanitize_value(item, markers))

    pending_writes: list[dict[str, Any]] = []
    if "memory_pending_writes" in tables:
        available = _table_columns(conn, "memory_pending_writes")
        wanted = [
            name
            for name in (
                "id", "status", "action", "scope_type", "scope_id",
                "payload_json", "source_run_id", "created_at", "resolved_at",
            )
            if name in available
        ]
        if wanted:
            quoted = ", ".join(f'"{name}"' for name in wanted)
            for row in conn.execute(
                f"SELECT {quoted} FROM memory_pending_writes ORDER BY id"
            ):
                item = dict(zip(wanted, row, strict=True))
                payload = _decode_json(str(item.pop("payload_json", "") or ""))
                if isinstance(payload, dict):
                    item.update(
                        {
                            key: payload[key]
                            for key in (
                                "content", "target_id", "target_content_hash",
                            )
                            if key in payload
                        }
                    )
                pending_writes.append(_sanitize_value(item, markers))

    review_count: int | None = None
    review_audits: list[dict[str, Any]] = []
    for review_table in ("memory_review_runs", "review_runs"):
        if review_table in tables:
            review_count = int(
                conn.execute(f'SELECT COUNT(*) FROM "{review_table}"').fetchone()[0]
            )
            available = _table_columns(conn, review_table)
            wanted = [
                name
                for name in (
                    "review_id", "review_key", "root_run_id", "model_type",
                    "status", "result_json", "created_at", "finished_at",
                )
                if name in available
            ]
            if wanted:
                quoted = ", ".join(f'"{name}"' for name in wanted)
                for row in conn.execute(
                    f'SELECT {quoted} FROM "{review_table}" ORDER BY 1'
                ):
                    audit = dict(zip(wanted, row, strict=True))
                    result = _decode_json(str(audit.pop("result_json", "") or ""))
                    audit["result"] = result if isinstance(result, dict) else {}
                    review_audits.append(_sanitize_value(audit, markers))
            break
    counts = {
        table: int(conn.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0])
        for table in ("runs", "events")
        if table in tables
    }
    return {
        "exists": True,
        "integrity": integrity,
        "tables": tables,
        "counts": counts,
        "memory_items": items,
        "memory_pending_writes": pending_writes,
        "review_audits": review_audits,
        "review_audit_count": review_count,
    }


def _db_snapshot_once(db_path: Path, markers: list[str]) -> dict[str, Any]:
    if not db_path.exists():
        return {
            "exists": False,
            "integrity": "missing",
            "tables": [],
            "memory_items": [],
            "memory_pending_writes": [],
            "review_audits": [],
            "review_audit_count": None,
        }
    stage = "connect"
    try:
        # Campaign state is isolated, so first finish WAL recovery explicitly,
        # then copy the resulting logical database through SQLite's backup API.
        # Reading the live WAL database directly can mix pager recovery with
        # integrity/FTS traversal and has produced false SQLITE_CORRUPT reports.
        with closing(
            sqlite3.connect(db_path, isolation_level=None, timeout=5)
        ) as source:
            stage = "configure"
            source.execute("PRAGMA busy_timeout = 5000")
            stage = "wal_checkpoint"
            checkpoint = source.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
            if checkpoint is None or int(checkpoint[0] or 0) != 0:
                raise sqlite3.OperationalError("WAL checkpoint remained busy")
            stage = "backup"
            with closing(sqlite3.connect(":memory:")) as snapshot:
                source.backup(snapshot)
                stage = "fixed_snapshot"
                return _read_fixed_db_snapshot(snapshot, markers)
    except sqlite3.Error as exc:
        return {
            "exists": True,
            "integrity": "error",
            "error_type": type(exc).__name__,
            "sqlite_errorcode": getattr(exc, "sqlite_errorcode", None),
            "sqlite_errorname": getattr(exc, "sqlite_errorname", None),
            "error_stage": stage,
            "error_message": _sanitize_text(str(exc), markers),
            "tables": [],
            "memory_items": [],
            "memory_pending_writes": [],
            "review_audits": [],
            "review_audit_count": None,
        }


def _db_snapshot(db_path: Path, markers: list[str]) -> dict[str, Any]:
    """Read a stable post-process snapshot, retrying transient WAL close races."""
    result: dict[str, Any] = {}
    for attempt in range(5):
        result = _db_snapshot_once(db_path, markers)
        if result.get("integrity") != "error":
            return result
        if attempt < 4:
            time.sleep(0.05 * (attempt + 1))
    return result


def _review_audit_delta(
    before: dict[str, Any],
    after: dict[str, Any],
) -> list[dict[str, Any]]:
    """Return the new/changed content-free audit rows for one Application."""
    before_rows = {
        str(row.get("review_key") or row.get("review_id") or ""): row
        for row in before.get("review_audits") or []
        if isinstance(row, dict)
    }
    return [
        row
        for row in after.get("review_audits") or []
        if isinstance(row, dict)
        and before_rows.get(
            str(row.get("review_key") or row.get("review_id") or "")
        ) != row
    ]


def _review_calls_from_audit_delta(
    before: dict[str, Any],
    after: dict[str, Any],
) -> int | None:
    """Read provider-call counts from new/changed content-free audit rows."""
    changed = _review_audit_delta(before, after)
    if not changed:
        return None
    calls: list[int] = []
    for row in changed:
        result = row.get("result")
        value = result.get("calls") if isinstance(result, dict) else None
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            return None
        calls.append(value)
    return sum(calls)


def _privacy_findings(paths: list[Path], oracle: dict[str, dict[str, Any]]) -> list[dict[str, str]]:
    return find_privacy_markers(paths, oracle)


def _run_cli(state_root: Path, args: list[str], markers: list[str]) -> dict[str, Any]:
    env = os.environ.copy()
    env["AGENTLOOM_SELF_LEARNING_ROOT"] = str(state_root)
    completed = subprocess.run(
        [_loom(), "memory", *args],
        cwd=REPO_ROOT,
        env=env,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        check=False,
        timeout=30,
    )
    raw = str(completed.stdout or "")
    return {
        "command": ["loom", "memory", *args],
        "returncode": int(completed.returncode),
        "payload": _sanitize_value(_last_json(raw), markers),
        "output": _sanitize_text(raw, markers)[-4000:],
    }


def _candidate_ids(value: Any, required_terms: list[str]) -> list[str]:
    found: list[str] = []
    if isinstance(value, dict):
        payload = value.get("payload")
        payload = payload if isinstance(payload, dict) else {}
        content = normalize(
            value.get("content")
            or value.get("text")
            or payload.get("content")
            or ""
        )
        status = normalize(value.get("status") or "")
        if status == "pending" and terms_match(content, required_terms):
            identifier = value.get("id")
            if identifier not in (None, ""):
                found.append(str(identifier))
        for nested in value.values():
            found.extend(_candidate_ids(nested, required_terms))
    elif isinstance(value, list):
        for nested in value:
            found.extend(_candidate_ids(nested, required_terms))
    return found


def _approval_transition(
    state_root: Path,
    oracle_row: dict[str, Any],
    markers: list[str],
) -> dict[str, Any]:
    pending = _run_cli(state_root, ["pending"], markers)
    candidates = _candidate_ids(pending.get("payload"), list(oracle_row.get("required_terms") or []))
    if len(set(candidates)) != 1:
        return {
            "ok": False,
            "decision": str(oracle_row.get("decision") or ""),
            "pending": pending,
            "candidate_count": len(set(candidates)),
        }
    target = candidates[0]
    decision = str(oracle_row.get("decision") or "")
    applied = _run_cli(state_root, [decision, target], markers)
    payload = applied.get("payload")
    business_ok = isinstance(payload, dict) and payload.get("ok") is True
    if not business_ok:
        return {
            "ok": False,
            "decision": decision,
            "target": target,
            "pending": pending,
            "result": applied,
        }

    readback = _db_snapshot(state_root / "self_learning.db", markers)
    pending_rows = [
        row
        for row in readback.get("memory_pending_writes") or []
        if isinstance(row, dict) and str(row.get("id")) == target
    ]
    expected_status = "approved" if decision == "approve" else "rejected"
    required_terms = [str(value) for value in oracle_row.get("required_terms") or []]
    resolved = (
        len(pending_rows) == 1
        and normalize(pending_rows[0].get("status")) == expected_status
        and bool(pending_rows[0].get("resolved_at"))
    )
    matching_active = [
        row
        for row in readback.get("memory_items") or []
        if isinstance(row, dict)
        and normalize(row.get("status") or "active") == "active"
        and terms_match(row.get("content") or "", required_terms)
    ]
    readback_ok = bool(
        readback.get("integrity") == "ok"
        and resolved
        and (bool(matching_active) if decision == "approve" else not matching_active)
    )
    return {
        "ok": applied.get("returncode") == 0 and readback_ok,
        "decision": decision,
        "target": target,
        "pending": pending,
        "result": applied,
        "readback": readback,
        "readback_ok": readback_ok,
    }


def _snapshot_state(state_root: Path, backup: Path) -> None:
    if backup.exists():
        shutil.rmtree(backup)
    if state_root.exists():
        shutil.copytree(state_root, backup)


def _restore_state(state_root: Path, backup: Path) -> None:
    if state_root.exists():
        shutil.rmtree(state_root)
    if backup.exists():
        shutil.copytree(backup, state_root)
    else:
        state_root.mkdir(parents=True, exist_ok=True)


def _has_litellm_timeout_error(raw_output: str) -> bool:
    lines = [
        _ANSI_RE.sub("", line).strip()
        for line in str(raw_output or "").splitlines()
    ]
    for index, line in enumerate(lines):
        record = _LOG_RECORD_RE.match(line)
        if record is not None:
            header = record.group(0)
            message = line[record.end() :]
            block_start = message
            is_error_record = bool(_ERROR_LEVEL_RE.search(header))
            starts_error = is_error_record and bool(
                _ERROR_MESSAGE_RE.match(message)
                or _LITELLM_TIMEOUT_EXCEPTION_RE.match(message)
            )
        else:
            block_start = line
            starts_error = bool(_ERROR_MESSAGE_RE.match(line))
            if starts_error and index > 0:
                previous_index = index - 1
                while previous_index >= 0 and not lines[previous_index]:
                    previous_index -= 1
                if previous_index >= 0:
                    previous_record = _LOG_RECORD_RE.match(lines[previous_index])
                    starts_error = bool(
                        previous_record is not None
                        and _ERROR_LEVEL_RE.search(previous_record.group(0))
                    )
        if not starts_error:
            continue
        block_lines = [block_start]
        for continuation in lines[index + 1 : index + 8]:
            if _LOG_RECORD_RE.match(continuation):
                break
            if continuation:
                block_lines.append(continuation)
        error_block = " ".join(block_lines).strip()
        while prefix := _ERROR_MESSAGE_RE.match(error_block):
            error_block = error_block[prefix.end() :].lstrip()
        if _LITELLM_TIMEOUT_EXCEPTION_RE.match(error_block):
            return True
    return False


def _retryable(raw_output: str) -> bool:
    if _has_litellm_timeout_error(raw_output):
        return True
    for raw_line in str(raw_output or "").splitlines():
        line = _ANSI_RE.sub("", raw_line).strip()
        record = _LOG_RECORD_RE.match(line)
        if record is not None:
            if not _TRANSPORT_LOG_LEVEL_RE.search(record.group(0)):
                continue
            messages = (line[record.end() :].lstrip(),)
        elif level := _BARE_LOG_LEVEL_RE.match(line):
            messages = (line, line[level.end() :].lstrip())
        else:
            messages = (line,)
        if any(
            _TRANSPORT_STATUS_LINE_RE.match(message)
            or _TRANSPORT_EXCEPTION_LINE_RE.match(message)
            or _LITELLM_NATIVE_TIMEOUT_LINE_RE.match(message)
            for message in messages
        ):
            return True
    return False


def _run_attempt(
    spec: RunSpec,
    *,
    campaign_dir: Path,
    state_root: Path,
    attempt_number: int,
    timeout_seconds: int,
    oracle: dict[str, dict[str, Any]],
    markers: list[str],
) -> dict[str, Any]:
    runtime_root = campaign_dir / "runtime" / spec.run_id / f"attempt-{attempt_number}"
    runtime_root.mkdir(parents=True, exist_ok=True)
    db_path = state_root / "self_learning.db"
    before = _db_snapshot(db_path, markers)
    env = os.environ.copy()
    env.update(
        {
            "AGENT_LOOM_RUNTIME_ROOT": str(runtime_root),
            "AGENTLOOM_SELF_LEARNING_ROOT": str(state_root),
            "AGENTLOOM_MEMORY_CASE_ID": spec.case_id,
            "AGENTLOOM_MEMORY_CASE_PHASE": spec.phase,
            "AGENTLOOM_MEMORY_CAMPAIGN_SAFE_ARTIFACTS": "1",
            "PYTHONUNBUFFERED": "1",
        }
    )
    command = [_loom(), "run", spec.workflow, "--log-to-file"]
    started = time.monotonic()
    try:
        completed = subprocess.run(
            command,
            cwd=REPO_ROOT,
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            check=False,
            timeout=timeout_seconds,
        )
        returncode = int(completed.returncode)
        raw_output = str(completed.stdout or "")
        timed_out = False
    except subprocess.TimeoutExpired as exc:
        raw = exc.stdout or ""
        raw_output = raw.decode(errors="replace") if isinstance(raw, bytes) else str(raw)
        returncode = 124
        timed_out = True
    elapsed = time.monotonic() - started

    model = _model_evidence(raw_output)
    after = _db_snapshot(db_path, markers)
    review_audit_delta = _review_audit_delta(before, after)
    model["review_audit_delta"] = review_audit_delta
    if model["review_call_count"] is None:
        audit_calls = _review_calls_from_audit_delta(before, after)
        if audit_calls is not None:
            model["review_call_count"] = audit_calls
            model["review_evidence_source"] = "sqlite_review_audit"

    privacy = _privacy_findings([state_root, runtime_root], oracle)
    safe_output = _sanitize_text(raw_output, markers)
    log_path = campaign_dir / "logs" / spec.run_id / f"attempt-{attempt_number}.log"
    _write_text(log_path, safe_output + ("\n" if safe_output else ""))
    cli = {
        "list": _run_cli(state_root, ["list"], markers),
        "pending": _run_cli(state_root, ["pending"], markers),
    }
    return {
        "attempt": attempt_number,
        "returncode": returncode,
        "timed_out": timed_out,
        "duration_seconds": elapsed,
        "command": ["loom", "run", spec.workflow, "--log-to-file"],
        "runtime_root": str(runtime_root),
        "state_root": str(state_root),
        "log_path": str(log_path),
        "completion_marker_seen": _COMPLETION_MARKER in _ANSI_RE.sub("", raw_output),
        "final_answer": _sanitize_value(
            _completed_run_answer(
                raw_output,
                returncode=returncode,
                timed_out=timed_out,
            ),
            markers,
        ),
        "model_evidence": model,
        "database": after,
        "cli": cli,
        "privacy_findings": privacy,
        "retryable_transport": timed_out or _retryable(raw_output),
    }


def _run_spec(
    spec: RunSpec,
    *,
    campaign_dir: Path,
    state_root: Path,
    timeout_seconds: int,
    oracle: dict[str, dict[str, Any]],
    markers: list[str],
) -> dict[str, Any]:
    backup = campaign_dir / "retry_state" / spec.run_id
    _snapshot_state(state_root, backup)
    attempts = [
        _run_attempt(
            spec,
            campaign_dir=campaign_dir,
            state_root=state_root,
            attempt_number=1,
            timeout_seconds=timeout_seconds,
            oracle=oracle,
            markers=markers,
        )
    ]
    if attempts[0]["returncode"] != 0 and attempts[0]["retryable_transport"]:
        _restore_state(state_root, backup)
        attempts.append(
            _run_attempt(
                spec,
                campaign_dir=campaign_dir,
                state_root=state_root,
                attempt_number=2,
                timeout_seconds=timeout_seconds,
                oracle=oracle,
                markers=markers,
            )
        )
    final = attempts[-1]
    return {
        **spec.to_dict(),
        "status": (
            "completed"
            if final["returncode"] == 0 and final.get("completion_marker_seen") is True
            else "failed"
        ),
        "attempts": attempts,
        "final": final,
    }


def _run_group(
    specs: list[RunSpec],
    *,
    campaign_dir: Path,
    timeout_seconds: int,
    oracle: dict[str, dict[str, Any]],
    markers: list[str],
) -> list[dict[str, Any]]:
    state_root = campaign_dir / "state" / specs[0].state_key
    state_root.mkdir(parents=True, exist_ok=True)
    results: list[dict[str, Any]] = []
    transition: dict[str, Any] | None = None
    for spec in specs:
        if spec.phase == "post_recall":
            transition = _approval_transition(state_root, oracle[spec.case_id], markers)
        result = _run_spec(
            spec,
            campaign_dir=campaign_dir,
            state_root=state_root,
            timeout_seconds=timeout_seconds,
            oracle=oracle,
            markers=markers,
        )
        if transition is not None and spec.phase == "post_recall":
            result["approval_transition"] = transition
        results.append(result)
        if result["status"] != "completed":
            break
    return results


def _run_groups(
    groups: list[list[RunSpec]],
    *,
    campaign_dir: Path,
    timeout_seconds: int,
    max_workers: int,
    oracle: dict[str, dict[str, Any]],
    markers: list[str],
) -> list[dict[str, Any]]:
    if max_workers == 1:
        return [
            result
            for group in groups
            for result in _run_group(
                group,
                campaign_dir=campaign_dir,
                timeout_seconds=timeout_seconds,
                oracle=oracle,
                markers=markers,
            )
        ]
    results: list[dict[str, Any]] = []
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = {
            executor.submit(
                _run_group,
                group,
                campaign_dir=campaign_dir,
                timeout_seconds=timeout_seconds,
                oracle=oracle,
                markers=markers,
            ): group[0].state_key
            for group in groups
        }
        for future in as_completed(futures):
            results.extend(future.result())
    return results


def _usage(results: list[dict[str, Any]]) -> dict[str, int]:
    totals = Counter()
    for result in results:
        if result.get("status") == "planned":
            continue
        evidence = (result.get("final") or {}).get("model_evidence") or {}
        for key in (
            "application_completion_calls",
            "application_input_tokens",
            "application_output_tokens",
            "review_input_tokens",
            "review_output_tokens",
        ):
            totals[key] += int(evidence.get(key) or 0)
        calls = evidence.get("review_call_count")
        if isinstance(calls, int):
            totals["review_completion_calls"] += calls
        else:
            totals["unverifiable_review_runs"] += 1
    return dict(totals)


def _canary_can_continue(audit: dict[str, Any]) -> bool:
    """Stop only for infrastructure or hard-safety failures.

    A five-run sample is too small to decide the 95% semantic release gate.
    Soft semantic misses remain recorded and are never retried; the full
    campaign must measure their actual rate.
    """
    return bool(
        audit.get("complete")
        and not any(
            bool(issue.get("hard"))
            for issue in audit.get("issues") or []
            if isinstance(issue, dict)
        )
    )


def _git_head() -> str:
    completed = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=REPO_ROOT, text=True,
        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, check=False,
    )
    return completed.stdout.strip() if completed.returncode == 0 else "unknown"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runs", type=int, choices=(1, 5, 100), default=100)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--campaign-id", default="")
    parser.add_argument("--max-workers", type=int, choices=(1, 2), default=2)
    parser.add_argument("--timeout-seconds", type=int, default=600)
    parser.add_argument(
        "--output-root",
        type=Path,
        default=REPO_ROOT / ".agentloom" / "validation" / "memory_feature_validation",
    )
    args = parser.parse_args()

    campaign_id = args.campaign_id or _default_campaign_id(
        "memory-review-dry" if args.dry_run else "memory-review"
    )
    campaign_dir = _campaign_dir(args.output_root, campaign_id)
    specs = select_runs(args.runs)
    oracle = indexed_rows(ORACLE_PATH)
    markers = all_probe_markers(oracle)
    plan = {
        "schema_version": 1,
        "campaign_id": campaign_id,
        "created_at": _now(),
        "dry_run": bool(args.dry_run),
        "requested_runs": args.runs,
        "selected_runs": len(specs),
        "canary_count": min(5, len(specs)),
        "max_concurrency": args.max_workers,
        "infrastructure_retries": 1,
        "cli_contract": "loom run <workflow> --log-to-file",
        "memory_cli_contract": ["list", "pending", "approve", "reject"],
        "dataset": dataset_manifest(),
        "scenario_quotas": dict(Counter(spec.scenario for spec in specs)),
        "runs": [spec.to_dict() for spec in specs],
    }
    _write_json(campaign_dir / "plan.json", plan)
    _write_json(
        campaign_dir / "environment.json",
        {
            "created_at": _now(),
            "python": sys.version,
            "platform": sys.platform,
            "git_head": _git_head(),
            "model_contract": _safe_model_contract(),
        },
    )

    if args.dry_run:
        results = [{**spec.to_dict(), "status": "planned"} for spec in specs]
    else:
        canary_specs = [spec for spec in specs if spec.canary_rank]
        results = _run_groups(
            grouped_runs(canary_specs),
            campaign_dir=campaign_dir,
            timeout_seconds=args.timeout_seconds,
            max_workers=1,
            oracle=oracle,
            markers=markers,
        )
        from applications.memory_feature_validation.scripts.audit_memory_review_campaign import (
            evaluate_results,
        )

        canary_audit = evaluate_results(canary_specs, results, oracle, require_complete=True)
        canary_audit["continuation_ok"] = _canary_can_continue(canary_audit)
        _write_json(campaign_dir / "canary_audit.json", canary_audit)
        if canary_audit["continuation_ok"] and args.runs == 100:
            canary_ids = {spec.run_id for spec in canary_specs}
            remainder = [spec for spec in specs if spec.run_id not in canary_ids]
            results.extend(
                _run_groups(
                    grouped_runs(remainder),
                    campaign_dir=campaign_dir,
                    timeout_seconds=args.timeout_seconds,
                    max_workers=args.max_workers,
                    oracle=oracle,
                    markers=markers,
                )
            )

    order = {spec.run_id: index for index, spec in enumerate(specs)}
    results.sort(key=lambda result: order.get(str(result.get("run_id") or ""), 10**9))
    _write_json(campaign_dir / "results.json", {"schema_version": 1, "results": results})
    _write_json(campaign_dir / "usage.json", _usage(results))

    from applications.memory_feature_validation.scripts.audit_memory_review_campaign import (
        audit_campaign,
    )

    audit = audit_campaign(campaign_dir)
    print(f"campaign={campaign_dir}")
    print(f"status={audit['status']}")
    return 0 if audit["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
