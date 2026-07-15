"""Run the simplified memory contract through real ``loom run`` Applications.

The runner is intentionally a black-box client.  It does not import the
self-learning implementation: configuration, CLI behavior, SQLite state, and
the model-visible result are observed exactly as an external operator would.
"""

from __future__ import annotations

import argparse
import ast
import base64
import hashlib
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
import unicodedata
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import closing
from datetime import datetime
from pathlib import Path
from typing import Any

import yaml

_BOOTSTRAP_ROOT = Path(__file__).resolve().parents[3]
if str(_BOOTSTRAP_ROOT) not in sys.path:
    # Keep stdlib and installed dependencies ahead of the historical subject
    # tree; only the AgentLoom namespace itself should resolve from this root.
    sys.path.append(str(_BOOTSTRAP_ROOT))

from applications.memory_feature_validation.scripts.campaign_identity import (  # noqa: E402
    default_campaign_id as _default_campaign_id,
)
from applications.memory_feature_validation.scripts.memory_review_campaign_capsule import (  # noqa: E402
    CAMPAIGN_LLM_CONFIG_FD_ENV,
    CAPSULE_TOKEN_ENV,
    _git_env,
    _require_isolated_git_metadata,
    _trusted_git,
    active_capsule_bootstrap_issues,
    build_capsule_descriptor,
    capsule_descriptor_issues,
    capsule_is_active,
    guarded_runtime_command,
    provision_capsule,
    trusted_control_plane_matches,
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
    release_git_source_state,
    select_runs,
)

_LOG_RECORD_RE = re.compile(r"^\s*(?:\[[^\]\r\n]*\])+\s*")
_INPUT_TOKEN_RE = re.compile(r"Input tokens:\s*[0-9,]+\s*\(\+([0-9,]+)\)")
_OUTPUT_TOKEN_RE = re.compile(r"Output tokens:\s*[0-9,]+\s*\(\+([0-9,]+)\)")
_MODEL_ID_RE = re.compile(r"Resolved smolagents model:\s*summary\s*->\s*([^\s]+)", re.IGNORECASE)
_ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]")
_SENSITIVE_ASSIGNMENT_RE = re.compile(
    r"(?<![\w.-])"
    r"(?P<key>[\w.-]*(?:"
    r"api[_-]?key|access[_-]?key|access[_-]?token|password|passwd|pwd|"
    r"client[_-]?secret|authorization|refresh[_-]?token|private[_-]?key|"
    r"cookie|credentials?|secret[_-]?key|signing[_-]?key|token|secret"
    r"))"
    r"(?P<separator>[\"']?\s*[:=：＝]\s*)"
    r"(?P<value>\"[^\"\r\n]*\"|'[^'\r\n]*'|[^,}\]\r\n]+)",
    re.IGNORECASE,
)
_BEARER_TOKEN_RE = re.compile(
    r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]+"
)
_SENSITIVE_NORMALIZED_KEYS = (
    "accesstoken",
    "accesskey",
    "apikey",
    "authorization",
    "clientsecret",
    "cookie",
    "credential",
    "credentials",
    "password",
    "passwd",
    "privatekey",
    "pwd",
    "refreshtoken",
    "secret",
    "secretkey",
    "signingkey",
    "token",
    "awssecretaccesskey",
)
_COMPLETION_MARKER = "Execution completed successfully."
_ACTIVE_CAPSULE_DESCRIPTOR: dict[str, Any] | None = None
_ACTIVE_MODEL_CONFIG_BYTES: bytes | None = None
_ACTIVE_MODEL_PRIVACY_MARKERS: tuple[tuple[str, bytes], ...] = ()
_MAX_MODEL_CONFIG_BYTES = 1024 * 1024
_REPRODUCTION_STABLE_CAPSULE_FIELDS = (
    "git_commit",
    "source_manifest_hash",
    "dataset_manifest_hash",
    "model_contract_hash",
    "uv_lock_hash",
    "uv_binary_hash",
    "uv_version",
    "git_binary_hash",
    "git_version",
    "python_version",
    "python_cache_tag",
    "python_binary_hash",
    "loom_hash",
    "loom_shebang_target_hash",
    "venv_manifest_hash",
    "stdlib_manifest_hash",
    "distribution_set_hash",
    "checkout_manifest_hash",
    "runtime_env_contract_hash",
    "write_guard_binary_hash",
)


def _now() -> str:
    return datetime.now().astimezone().isoformat()


def _write_text(path: Path, value: str) -> None:
    leaked = _real_marker_findings_bytes(
        str(value).encode("utf-8"),
        location=str(path),
    )
    if leaked:
        raise RuntimeError("refusing to persist model configuration material")
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
    candidate = REPO_ROOT / ".venv" / "bin" / "loom"
    if candidate.is_file():
        return str(candidate.resolve())
    raise FileNotFoundError("repository .venv/bin/loom executable not found")


def _active_capsule_id() -> str:
    return str((_ACTIVE_CAPSULE_DESCRIPTOR or {}).get("capsule_id") or "")


def _normalized_config_key(value: Any) -> str:
    return re.sub(
        r"[^a-z0-9]",
        "",
        unicodedata.normalize("NFKC", str(value)).casefold(),
    )


def _is_sensitive_key(value: Any) -> bool:
    normalized = _normalized_config_key(value)
    return normalized in _SENSITIVE_NORMALIZED_KEYS or normalized.endswith(
        _SENSITIVE_NORMALIZED_KEYS
    )


def _safe_config_fingerprint_value(value: Any, *, key: str = "") -> Any:
    normalized_key = _normalized_config_key(key)
    if _is_sensitive_key(key):
        return "[REDACTED]"
    if normalized_key in {"headers", "extraheaders", "defaultheaders"} and isinstance(
        value, dict
    ):
        return {
            str(child_key): _safe_config_fingerprint_value(
                child_value,
                key=str(child_key),
            )
            for child_key, child_value in value.items()
        }
    if normalized_key in {"baseurl", "endpoint", "endpointurl"}:
        return hashlib.sha256(str(value or "").encode()).hexdigest()
    if isinstance(value, dict):
        return {
            str(child_key): _safe_config_fingerprint_value(
                child_value,
                key=str(child_key),
            )
            for child_key, child_value in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [
            _safe_config_fingerprint_value(item)
            for item in value
        ]
    return value


def _sensitive_scalar_values(value: Any) -> list[str]:
    if isinstance(value, dict):
        return [
            item
            for child in value.values()
            for item in _sensitive_scalar_values(child)
        ]
    if isinstance(value, (list, tuple, set)):
        return [
            item
            for child in value
            for item in _sensitive_scalar_values(child)
        ]
    if value is None:
        return []
    return [str(value)]


def _model_config_privacy_markers(
    config_bytes: bytes,
) -> tuple[tuple[str, bytes], ...]:
    """Build credential markers without ever persisting their plaintext."""
    labels: list[tuple[str, bytes]] = []
    if config_bytes:
        standard_blob = base64.b64encode(config_bytes)
        urlsafe_blob = base64.urlsafe_b64encode(config_bytes)
        labels.extend(
            (
                ("model_config_blob", config_bytes),
                ("model_config_base64", standard_blob),
                ("model_config_base64_unpadded", standard_blob.rstrip(b"=")),
                ("model_config_urlsafe_base64", urlsafe_blob),
                (
                    "model_config_urlsafe_base64_unpadded",
                    urlsafe_blob.rstrip(b"="),
                ),
            )
        )
    try:
        payload = yaml.safe_load(config_bytes.decode("utf-8")) or {}
    except (UnicodeDecodeError, yaml.YAMLError):
        payload = {}

    def append_sensitive_scalar(item: str) -> None:
        raw = item.encode("utf-8")
        standard = base64.b64encode(raw)
        urlsafe = base64.urlsafe_b64encode(raw)
        labels.extend(
            (
                ("model_config_secret", raw),
                ("model_config_secret_base64", standard),
                ("model_config_secret_base64_unpadded", standard.rstrip(b"=")),
                ("model_config_secret_urlsafe_base64", urlsafe),
                (
                    "model_config_secret_urlsafe_base64_unpadded",
                    urlsafe.rstrip(b"="),
                ),
            )
        )

    def collect(value: Any) -> None:
        if isinstance(value, dict):
            for key, child in value.items():
                if _is_sensitive_key(key):
                    sensitive_values = [
                        item
                        for item in _sensitive_scalar_values(child)
                        if item
                    ]
                    for item in sensitive_values:
                        append_sensitive_scalar(item)
                    if _normalized_config_key(key).endswith("authorization"):
                        for item in sensitive_values:
                            bearer = re.fullmatch(
                                r"\s*Bearer\s+(.+?)\s*",
                                item,
                                flags=re.IGNORECASE,
                            )
                            if bearer and bearer.group(1):
                                append_sensitive_scalar(bearer.group(1))
                else:
                    collect(child)
        elif isinstance(value, (list, tuple, set)):
            for child in value:
                collect(child)

    collect(payload)
    unique: dict[bytes, str] = {}
    for kind, marker in labels:
        if marker:
            unique.setdefault(marker, kind)
    return tuple((kind, marker) for marker, kind in unique.items())


def _consume_active_model_config() -> None:
    """Consume and close the one-shot parent pipe before spawning children."""
    global _ACTIVE_MODEL_CONFIG_BYTES, _ACTIVE_MODEL_PRIVACY_MARKERS

    if _ACTIVE_MODEL_CONFIG_BYTES is not None:
        return
    fd_value = str(os.environ.pop(CAMPAIGN_LLM_CONFIG_FD_ENV, "") or "")
    if not fd_value:
        raise RuntimeError("capsule model configuration pipe was missing")
    try:
        fd = int(fd_value)
        if fd < 0:
            raise ValueError
    except ValueError as exc:
        raise RuntimeError("capsule model configuration pipe was invalid") from exc
    try:
        with os.fdopen(fd, "rb", closefd=True) as stream:
            config_bytes = stream.read(_MAX_MODEL_CONFIG_BYTES + 1)
    except OSError as exc:
        raise RuntimeError("capsule model configuration pipe was invalid") from exc
    if not config_bytes or len(config_bytes) > _MAX_MODEL_CONFIG_BYTES:
        raise RuntimeError("capsule model configuration payload was invalid")
    _ACTIVE_MODEL_CONFIG_BYTES = config_bytes
    _ACTIVE_MODEL_PRIVACY_MARKERS = _model_config_privacy_markers(config_bytes)


def _safe_model_contract(
    path: Path | None = None,
    *,
    config_bytes: bytes | None = None,
) -> dict[str, Any]:
    path = path or (REPO_ROOT / "config" / "llm.yaml")
    if config_bytes is None and capsule_is_active():
        config_bytes = _ACTIVE_MODEL_CONFIG_BYTES
    if not path.exists() and config_bytes is None:
        safe = {
            "configured": False,
            "requested_type": "summary",
            "model_id": "",
            "endpoint_hash": "",
            "temperature": None,
            "max_tokens": None,
            "timeout": None,
            "num_retries": None,
            "tool_choice": None,
            "parallel_tool_calls": None,
            "thinking_type": None,
        }
        safe["config_hash"] = hashlib.sha256(
            json.dumps(safe, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        return safe
    try:
        config_text = (
            config_bytes.decode("utf-8")
            if config_bytes is not None
            else path.read_text(encoding="utf-8")
        )
        payload = yaml.safe_load(config_text) or {}
    except Exception:
        payload = {}
    model = payload.get("model") if isinstance(payload, dict) else {}
    summary = model.get("summary") if isinstance(model, dict) else {}
    summary = summary if isinstance(summary, dict) else {}
    extra_body = summary.get("extra_body")
    extra_body = extra_body if isinstance(extra_body, dict) else {}
    thinking = extra_body.get("thinking")
    thinking = thinking if isinstance(thinking, dict) else {}
    safe = {
        "configured": bool(summary.get("model")),
        "requested_type": "summary",
        "model_id": str(summary.get("model") or ""),
        "endpoint_hash": hashlib.sha256(
            str(summary.get("base_url") or "").encode()
        ).hexdigest(),
        "temperature": summary.get("temperature"),
        "max_tokens": summary.get("max_tokens"),
        "timeout": summary.get("timeout"),
        "num_retries": summary.get("num_retries"),
        "tool_choice": summary.get("tool_choice"),
        "parallel_tool_calls": summary.get("parallel_tool_calls"),
        "thinking_type": thinking.get("type"),
    }
    safe["config_hash"] = hashlib.sha256(
        json.dumps(
            _safe_config_fingerprint_value(summary),
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        ).encode()
    ).hexdigest()
    return safe


def _model_contract_issues(contract: dict[str, Any]) -> list[str]:
    """Fail fast when the real campaign cannot prove its provider contract."""
    issues: list[str] = []
    if not contract.get("configured"):
        return ["summary model is not configured"]
    if isinstance(contract.get("num_retries"), bool) or contract.get("num_retries") != 0:
        issues.append("summary model must set num_retries: 0")
    if contract.get("parallel_tool_calls") is not False:
        issues.append("summary model must set parallel_tool_calls: false")
    if str(contract.get("thinking_type") or "").strip().casefold() == "disabled":
        issues.append("summary model must not disable thinking")
    return issues


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
            text = text.replace(fragment, "[BLOCKED]")
            if len(fragment) <= 4096:
                text = wrapped_pattern(fragment).sub("[BLOCKED]", text)

    safe_lines: list[str] = []
    for line in text.splitlines():
        if "[BLOCKED]" in line:
            safe_lines.append("[BLOCKED]")
            continue
        line = unicodedata.normalize("NFKC", line)

        def redact_assignment(match: re.Match[str]) -> str:
            if not _is_sensitive_key(match.group("key")):
                return match.group(0)
            return (
                f"{match.group('key')}"
                f"{match.group('separator')}"
                "[REDACTED]"
            )

        safe_lines.append(
            _BEARER_TOKEN_RE.sub(
                "Bearer [REDACTED]",
                _SENSITIVE_ASSIGNMENT_RE.sub(
                    redact_assignment,
                    line,
                ),
            )
        )
    return "\n".join(safe_lines)


def _sanitize_value(value: Any, markers: list[str]) -> Any:
    if isinstance(value, dict):
        return {
            str(key): (
                "[REDACTED]"
                if _is_sensitive_key(key)
                else _sanitize_value(item, markers)
            )
            for key, item in value.items()
        }
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
    run_status_counts = (
        {
            str(row[0] or ""): int(row[1])
            for row in conn.execute(
                "SELECT status, COUNT(*) FROM runs GROUP BY status"
            )
        }
        if "runs" in tables and "status" in _table_columns(conn, "runs")
        else {}
    )
    completed_root_run_ids: list[str] = []
    root_identity_valid = "runs" in tables
    if "runs" in tables:
        run_columns = _table_columns(conn, "runs")
        required_run_columns = {"status", "run_id", "root_run_id"}
        root_identity_valid = required_run_columns <= set(run_columns)
        if root_identity_valid:
            completed_rows = conn.execute(
                "SELECT run_id, root_run_id FROM runs WHERE status='completed'"
            ).fetchall()
            root_identity_valid = all(
                isinstance(row[1], str) and bool(row[1])
                for row in completed_rows
            )
            completed_root_run_ids = sorted(
                {
                    str(row[1])
                    for row in completed_rows
                    if row[1]
                }
            )
            completed_owner_ids = {
                str(row[0])
                for row in completed_rows
                if row[0] and row[0] == row[1]
            }
            root_identity_valid = bool(
                root_identity_valid
                and set(completed_root_run_ids) <= completed_owner_ids
            )
    return {
        "exists": True,
        "integrity": integrity,
        "tables": tables,
        "counts": counts,
        "run_status_counts": run_status_counts,
        "root_identity_valid": root_identity_valid,
        "completed_root_run_ids": completed_root_run_ids,
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
            "run_status_counts": {},
            "root_identity_valid": None,
            "completed_root_run_ids": [],
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
            "run_status_counts": {},
            "root_identity_valid": False,
            "completed_root_run_ids": [],
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


def _memory_effect_count(
    before: dict[str, Any],
    after: dict[str, Any],
) -> int:
    """Count added, removed, or changed durable/pending memory rows."""

    def indexed(snapshot: dict[str, Any]) -> dict[tuple[str, str], dict[str, Any]]:
        rows: dict[tuple[str, str], dict[str, Any]] = {}
        for table in ("memory_items", "memory_pending_writes"):
            for index, row in enumerate(snapshot.get(table) or []):
                if not isinstance(row, dict):
                    continue
                identity = str(row.get("id") or f"row:{index}")
                rows[(table, identity)] = row
        return rows

    old = indexed(before)
    new = indexed(after)
    keys = set(old) | set(new)
    return sum(old.get(key) != new.get(key) for key in keys)


def _completed_root_run_ids_delta(
    before: dict[str, Any],
    after: dict[str, Any],
) -> list[str]:
    before_ids = {
        str(value) for value in before.get("completed_root_run_ids") or []
    }
    after_ids = {
        str(value) for value in after.get("completed_root_run_ids") or []
    }
    return sorted(after_ids - before_ids)


def _active_marker_texts() -> list[str]:
    return [
        marker.decode("utf-8")
        for _kind, marker in _ACTIVE_MODEL_PRIVACY_MARKERS
        if marker
    ]


def _real_marker_findings_bytes(
    content: bytes,
    *,
    location: str,
) -> list[dict[str, str]]:
    whitespace_folded = re.sub(rb"\s+", b"", content)
    findings: list[dict[str, str]] = []
    seen_kinds: set[str] = set()
    for kind, marker in _ACTIVE_MODEL_PRIVACY_MARKERS:
        folded_marker = re.sub(rb"\s+", b"", marker)
        if marker not in content and folded_marker not in whitespace_folded:
            continue
        if kind not in seen_kinds:
            findings.append({"path": location, "kind": kind})
            seen_kinds.add(kind)
    return findings


def _real_marker_findings(paths: list[Path]) -> list[dict[str, str]]:
    findings: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for root in paths:
        candidates = (
            [root]
            if root.is_file()
            else sorted(root.rglob("*"))
            if root.exists()
            else []
        )
        for path in candidates:
            if not path.is_file():
                continue
            try:
                content = path.read_bytes()
            except OSError:
                finding = {
                    "path": str(path),
                    "kind": "read_error",
                }
                key = (finding["path"], finding["kind"])
                if key not in seen:
                    findings.append(finding)
                    seen.add(key)
                continue
            for finding in _real_marker_findings_bytes(
                content,
                location=str(path),
            ):
                key = (
                    finding["path"],
                    finding["kind"],
                )
                if key not in seen:
                    findings.append(finding)
                    seen.add(key)
    return findings


def _privacy_findings(
    paths: list[Path],
    oracle: dict[str, dict[str, Any]],
) -> list[dict[str, str]]:
    return [*find_privacy_markers(paths, oracle), *_real_marker_findings(paths)]


def _private_finding_paths(
    findings: list[dict[str, str]],
    execution_root: Path,
) -> list[dict[str, str]]:
    """Make private scratch evidence useful without persisting its host path."""

    normalized: list[dict[str, str]] = []
    for finding in findings:
        rendered = dict(finding)
        candidate = Path(str(rendered.get("path") or ""))
        if candidate.is_absolute():
            try:
                relative = candidate.relative_to(execution_root).as_posix()
            except ValueError:
                relative = "outside-execution-root"
            rendered["path"] = f"private-scratch/{relative}"
        normalized.append(rendered)
    return normalized


def _run_cli(state_root: Path, args: list[str], markers: list[str]) -> dict[str, Any]:
    env = os.environ.copy()
    env.pop(CAPSULE_TOKEN_ENV, None)
    env.pop(CAMPAIGN_LLM_CONFIG_FD_ENV, None)
    env["AGENTLOOM_SELF_LEARNING_ROOT"] = str(state_root)
    command = [_loom(), "memory", *args]
    if capsule_is_active():
        command = guarded_runtime_command(command, repo_root=REPO_ROOT)
    completed = subprocess.run(
        command,
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


def _candidate_ids(value: Any, expected_content: str) -> list[str]:
    found: list[str] = []
    if isinstance(value, dict):
        payload = value.get("payload")
        payload = payload if isinstance(payload, dict) else {}
        content = str(
            value.get("content")
            or value.get("text")
            or payload.get("content")
            or ""
        )
        status = normalize(value.get("status") or "")
        if status == "pending" and content == expected_content:
            identifier = value.get("id")
            if identifier not in (None, ""):
                found.append(str(identifier))
        for nested in value.values():
            found.extend(_candidate_ids(nested, expected_content))
    elif isinstance(value, list):
        for nested in value:
            found.extend(_candidate_ids(nested, expected_content))
    return found


def _approval_transition(
    state_root: Path,
    oracle_row: dict[str, Any],
    markers: list[str],
) -> dict[str, Any]:
    pending = _run_cli(state_root, ["pending"], markers)
    expected_content = str(oracle_row.get("expected_content") or "")
    candidates = _candidate_ids(pending.get("payload"), expected_content)
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
    expected_scope = normalize(oracle_row.get("expected_scope"))
    resolved = (
        len(readback.get("memory_pending_writes") or []) == 1
        and len(pending_rows) == 1
        and normalize(pending_rows[0].get("status")) == expected_status
        and bool(pending_rows[0].get("resolved_at"))
        and str(pending_rows[0].get("content") or "") == expected_content
        and (
            not expected_scope
            or normalize(
                pending_rows[0].get("scope_type")
                or pending_rows[0].get("scope")
                or ""
            )
            in (
                {"application", "app"}
                if expected_scope == "application"
                else {expected_scope}
            )
        )
    )
    active_rows = [
        row
        for row in readback.get("memory_items") or []
        if isinstance(row, dict)
    ]
    matching_active = [
        row
        for row in active_rows
        if normalize(row.get("status") or "active") == "active"
        and str(row.get("content") or "") == expected_content
        and (
            not expected_scope
            or normalize(row.get("scope_type") or row.get("scope") or "")
            in ({"application", "app"} if expected_scope == "application" else {expected_scope})
        )
    ]
    readback_ok = bool(
        readback.get("integrity") == "ok"
        and resolved
        and (
            len(active_rows) == len(matching_active) == 1
            if decision == "approve"
            else not active_rows
        )
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


def _directory_hash(path: Path) -> str:
    digest = hashlib.sha256()
    for item in sorted(candidate for candidate in path.rglob("*") if candidate.is_file()):
        relative = item.relative_to(path).as_posix()
        digest.update(relative.encode())
        digest.update(b"\0")
        with item.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
        digest.update(b"\0")
    return digest.hexdigest()


def _provider_protocol_empty_response_count(raw_output: str) -> int:
    """Count exact parser failures, including Rich wraps and reviewer output."""

    plain = _ANSI_RE.sub("", str(raw_output or ""))
    expected = re.compile(
        r"Error\s+while\s+parsing\s+tool\s+call\s+from\s+model\s+output:\s*"
        r"Empty\s+or\s+whitespace-only\s+model\s+output\.",
        re.IGNORECASE,
    )
    levels = re.compile(r"\[(ERROR|CRITICAL|WARNING|INFO|DEBUG)\]", re.IGNORECASE)
    count = 0
    for match in expected.finditer(plain):
        line_start = plain.rfind("\n", 0, match.start()) + 1
        prefix = plain[line_start : match.start()]
        # The isolated reviewer emits this exact parser failure without a log
        # prefix. Rich otherwise wraps the prefix, so use the nearest level in
        # the bounded preceding block rather than requiring one physical line.
        if not prefix.strip():
            count += 1
            continue
        if re.match(r"^\s*(?:ERROR|CRITICAL)\b", prefix, re.IGNORECASE):
            count += 1
            continue
        if re.match(
            r"^\s*(?:Execution failed|Error while generating output)\s*:\s*$",
            prefix,
            re.IGNORECASE,
        ):
            count += 1
            continue
        preceding = plain[max(0, match.start() - 320) : match.start()]
        found_levels = list(levels.finditer(preceding))
        if found_levels and found_levels[-1].group(1).upper() in {"ERROR", "CRITICAL"}:
            count += 1
    return count


def _run_attempt(
    spec: RunSpec,
    *,
    campaign_dir: Path,
    state_root: Path,
    attempt_number: int,
    timeout_seconds: int,
    oracle: dict[str, dict[str, Any]],
    markers: list[str],
    execution_root: Path | None = None,
) -> dict[str, Any]:
    execution_root = execution_root or campaign_dir
    agent_root = (REPO_ROOT / spec.agent_root).resolve()
    workflow_path = (REPO_ROOT / spec.workflow).resolve()
    try:
        agent_root.relative_to(REPO_ROOT)
        workflow_path.relative_to(agent_root / "applications")
    except ValueError as exc:
        raise RuntimeError("campaign Application escaped its committed agent root") from exc
    if not (agent_root / "pyproject.toml").is_file() or not workflow_path.is_file():
        raise RuntimeError("campaign Application agent root was incomplete")
    runtime_root = (
        execution_root / "runtime" / spec.run_id / f"attempt-{attempt_number}"
    )
    runtime_root.mkdir(parents=True, exist_ok=True)
    db_path = state_root / "self_learning.db"
    before = _db_snapshot(db_path, markers)
    env = os.environ.copy()
    env.pop(CAPSULE_TOKEN_ENV, None)
    env.pop(CAMPAIGN_LLM_CONFIG_FD_ENV, None)
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
    command = [_loom(), "run", str(workflow_path), "--log-to-file"]
    if capsule_is_active():
        if _ACTIVE_MODEL_CONFIG_BYTES is None:
            raise RuntimeError("capsule model configuration was not initialized")
        env[CAMPAIGN_LLM_CONFIG_FD_ENV] = "0"
        command = guarded_runtime_command(command, repo_root=REPO_ROOT)
    started_at = _now()
    started = time.monotonic()
    try:
        run_kwargs: dict[str, Any] = {
            "cwd": agent_root,
            "env": env,
            "stdout": subprocess.PIPE,
            "stderr": subprocess.STDOUT,
            "check": False,
            "timeout": timeout_seconds,
        }
        if capsule_is_active():
            run_kwargs["input"] = _ACTIVE_MODEL_CONFIG_BYTES
        else:
            run_kwargs["stdin"] = subprocess.DEVNULL
            run_kwargs["text"] = True
        completed = subprocess.run(command, **run_kwargs)
        returncode = int(completed.returncode)
        raw = completed.stdout or ""
        raw_output = raw.decode(errors="replace") if isinstance(raw, bytes) else str(raw)
        timed_out = False
    except subprocess.TimeoutExpired as exc:
        raw = exc.stdout or ""
        raw_output = raw.decode(errors="replace") if isinstance(raw, bytes) else str(raw)
        returncode = 124
        timed_out = True
    elapsed = time.monotonic() - started
    finished_at = _now()

    model = _model_evidence(raw_output)
    after = _db_snapshot(db_path, markers)
    model["memory_effect_count"] = _memory_effect_count(before, after)
    completed_root_run_ids = _completed_root_run_ids_delta(before, after)
    model["completed_root_run_ids"] = completed_root_run_ids
    model["completed_run_count_delta"] = len(completed_root_run_ids)
    model["root_identity_required"] = after.get("exists") is True
    model["root_identity_valid"] = after.get("root_identity_valid") is True
    review_audit_delta = _review_audit_delta(before, after)
    model["review_audit_delta"] = review_audit_delta
    if model["review_call_count"] is None:
        audit_calls = _review_calls_from_audit_delta(before, after)
        if audit_calls is not None:
            model["review_call_count"] = audit_calls
            model["review_evidence_source"] = "sqlite_review_audit"

    private_findings = _privacy_findings([state_root, runtime_root], oracle)
    if execution_root != campaign_dir:
        private_findings = _private_finding_paths(
            private_findings,
            execution_root,
        )
    privacy = [
        *private_findings,
        *_real_marker_findings_bytes(
            raw_output.encode("utf-8"),
            location=f"stdout:{spec.run_id}:attempt-{attempt_number}",
        ),
    ]
    safe_output = _sanitize_text(raw_output, markers)
    log_path = campaign_dir / "logs" / spec.run_id / f"attempt-{attempt_number}.log"
    _write_text(log_path, safe_output + ("\n" if safe_output else ""))
    cli = {
        "list": _run_cli(state_root, ["list"], markers),
        "pending": _run_cli(state_root, ["pending"], markers),
    }
    return {
        "attempt": attempt_number,
        "capsule_id": _active_capsule_id(),
        "execution_root": _active_capsule_id(),
        "returncode": returncode,
        "timed_out": timed_out,
        "started_at": started_at,
        "finished_at": finished_at,
        "duration_seconds": elapsed,
        "command": ["loom", "run", spec.workflow, "--log-to-file"],
        "agent_root": spec.agent_root,
        "runtime_root": runtime_root.relative_to(execution_root).as_posix(),
        "state_root": state_root.relative_to(execution_root).as_posix(),
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
        "provider_protocol_empty_responses": (
            _provider_protocol_empty_response_count(raw_output)
        ),
        # Only the subprocess timeout is a typed infrastructure signal. Log
        # text is model-visible and therefore cannot authorize a retry.
        "retryable_transport": timed_out,
    }


def _run_spec(
    spec: RunSpec,
    *,
    campaign_dir: Path,
    state_root: Path,
    timeout_seconds: int,
    oracle: dict[str, dict[str, Any]],
    markers: list[str],
    execution_root: Path | None = None,
) -> dict[str, Any]:
    execution_root = execution_root or campaign_dir
    backup = execution_root / "retry_state" / spec.run_id
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
            execution_root=execution_root,
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
                execution_root=execution_root,
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
    execution_root: Path | None = None,
) -> list[dict[str, Any]]:
    execution_root = execution_root or campaign_dir
    state_root = execution_root / "state" / specs[0].state_key
    state_root.mkdir(parents=True, exist_ok=True)
    results: list[dict[str, Any]] = []
    transition: dict[str, Any] | None = None
    for spec in specs:
        reproduction_snapshot: Path | None = None
        if spec.phase == "post_recall":
            reproduction_snapshot = (
                execution_root / "reproduction_state" / spec.run_id
            )
            _snapshot_state(state_root, reproduction_snapshot)
            transition = _approval_transition(state_root, oracle[spec.case_id], markers)
        result = _run_spec(
            spec,
            campaign_dir=campaign_dir,
            state_root=state_root,
            timeout_seconds=timeout_seconds,
            oracle=oracle,
            markers=markers,
            execution_root=execution_root,
        )
        if transition is not None and spec.phase == "post_recall":
            result["approval_transition"] = transition
        if reproduction_snapshot is None:
            reproduction_snapshot = execution_root / "retry_state" / spec.run_id
        result["reproduction_snapshot"] = {
            "path": reproduction_snapshot.relative_to(execution_root).as_posix(),
            "sha256": _directory_hash(reproduction_snapshot),
        }
        results.append(result)
    return results


def _run_groups(
    groups: list[list[RunSpec]],
    *,
    campaign_dir: Path,
    timeout_seconds: int,
    max_workers: int,
    oracle: dict[str, dict[str, Any]],
    markers: list[str],
    execution_root: Path | None = None,
) -> list[dict[str, Any]]:
    execution_root = execution_root or campaign_dir
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
                execution_root=execution_root,
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
                execution_root=execution_root,
            ): group[0].state_key
            for group in groups
        }
        for future in as_completed(futures):
            results.extend(future.result())
    return results


def _append_privacy_findings(
    results: list[dict[str, Any]],
    findings: list[dict[str, str]],
) -> None:
    """Attach content-free campaign findings to one audited attempt."""

    if not findings:
        return
    for result in results:
        if not isinstance(result, dict):
            continue
        attempts = result.get("attempts")
        target = next(
            (
                attempt
                for attempt in attempts or []
                if isinstance(attempt, dict)
            ),
            None,
        )
        if target is None and isinstance(result.get("final"), dict):
            target = result["final"]
        if target is None:
            target = {}
            result["final"] = target
        existing = target.get("privacy_findings")
        existing = existing if isinstance(existing, list) else []
        known = {
            json.dumps(item, sort_keys=True)
            for item in existing
            if isinstance(item, dict)
        }
        for finding in findings:
            key = json.dumps(finding, sort_keys=True)
            if key not in known:
                existing.append(finding)
                known.add(key)
        target["privacy_findings"] = existing
        return


def _publish_clean_snapshots(
    execution_root: Path,
    campaign_dir: Path,
    oracle: dict[str, dict[str, Any]],
) -> list[dict[str, str]]:
    """Publish only scanned retry/reproduction evidence via atomic renames."""

    snapshot_kinds = ("retry_state", "reproduction_state")
    available = [
        kind for kind in snapshot_kinds if (execution_root / kind).is_dir()
    ]
    if not available:
        return []
    for kind in available:
        if (campaign_dir / kind).exists():
            raise FileExistsError(f"snapshot destination already exists: {kind}")

    campaign_dir.parent.mkdir(parents=True, exist_ok=True)
    staging_root = Path(
        tempfile.mkdtemp(
            prefix=f".{campaign_dir.name}-snapshot-publish-",
            dir=campaign_dir.parent,
        )
    )
    os.chmod(staging_root, 0o700)
    published: list[Path] = []
    try:
        for kind in available:
            shutil.copytree(execution_root / kind, staging_root / kind)
        findings = _privacy_findings([staging_root], oracle)
        if findings:
            return _private_finding_paths(findings, staging_root)
        try:
            for kind in available:
                destination = campaign_dir / kind
                os.replace(staging_root / kind, destination)
                published.append(destination)
        except OSError:
            for destination in published:
                shutil.rmtree(destination, ignore_errors=True)
            raise
        return []
    finally:
        shutil.rmtree(staging_root, ignore_errors=True)


def _run_real_campaign(
    specs: list[RunSpec],
    *,
    campaign_dir: Path,
    requested_runs: int,
    timeout_seconds: int,
    max_workers: int,
    oracle: dict[str, dict[str, Any]],
    markers: list[str],
) -> list[dict[str, Any]]:
    """Execute all untrusted state/runtime writes behind a private boundary."""

    with tempfile.TemporaryDirectory(prefix="agentloom-memory-review-") as raw_root:
        execution_root = Path(raw_root)
        os.chmod(execution_root, 0o700)
        canary_specs = [spec for spec in specs if spec.canary_rank]
        results = _run_groups(
            grouped_runs(canary_specs),
            campaign_dir=campaign_dir,
            timeout_seconds=timeout_seconds,
            max_workers=1,
            oracle=oracle,
            markers=markers,
            execution_root=execution_root,
        )
        from applications.memory_feature_validation.scripts.audit_memory_review_campaign import (
            evaluate_results,
        )

        canary_audit = evaluate_results(
            canary_specs,
            results,
            oracle,
            require_complete=True,
            require_attempt_contract=True,
        )
        canary_audit["continuation_ok"] = _canary_can_continue(
            canary_audit,
            results=results,
        )
        _write_json(campaign_dir / "canary_audit.json", canary_audit)
        if canary_audit["continuation_ok"] and requested_runs == 100:
            canary_ids = {spec.run_id for spec in canary_specs}
            remainder = [spec for spec in specs if spec.run_id not in canary_ids]
            results.extend(
                _run_groups(
                    grouped_runs(remainder),
                    campaign_dir=campaign_dir,
                    timeout_seconds=timeout_seconds,
                    max_workers=max_workers,
                    oracle=oracle,
                    markers=markers,
                    execution_root=execution_root,
                )
            )

        private_findings = _private_finding_paths(
            _privacy_findings([execution_root], oracle),
            execution_root,
        )
        if not private_findings:
            private_findings = _publish_clean_snapshots(
                execution_root,
                campaign_dir,
                oracle,
            )
        _append_privacy_findings(results, private_findings)
        return results


def _run_reproduction_in_private_scratch(
    spec: RunSpec,
    *,
    snapshot: Path,
    reproduction_dir: Path,
    timeout_seconds: int,
    oracle: dict[str, dict[str, Any]],
    markers: list[str],
) -> dict[str, Any]:
    """Reproduce from published evidence without publishing new raw state."""

    with tempfile.TemporaryDirectory(prefix="agentloom-memory-reproduce-") as raw_root:
        execution_root = Path(raw_root)
        os.chmod(execution_root, 0o700)
        state_root = execution_root / "state"
        _restore_state(state_root, snapshot)
        transition: dict[str, Any] | None = None
        if spec.phase == "post_recall":
            transition = _approval_transition(
                state_root,
                oracle[spec.case_id],
                markers,
            )
        result = _run_spec(
            spec,
            campaign_dir=reproduction_dir,
            state_root=state_root,
            timeout_seconds=timeout_seconds,
            oracle=oracle,
            markers=markers,
            execution_root=execution_root,
        )
        if transition is not None:
            result["approval_transition"] = transition
        private_findings = _private_finding_paths(
            _privacy_findings([execution_root], oracle),
            execution_root,
        )
        _append_privacy_findings([result], private_findings)
        return result


def _usage(results: list[dict[str, Any]]) -> dict[str, int]:
    totals = Counter()
    for result in results:
        if result.get("status") == "planned":
            continue
        attempts = result.get("attempts") or [result.get("final") or {}]
        for attempt in attempts:
            if not isinstance(attempt, dict):
                totals["unverifiable_review_attempts"] += 1
                continue
            evidence = attempt.get("model_evidence") or {}
            for key in (
                "application_completion_calls",
                "application_input_tokens",
                "application_output_tokens",
                "review_input_tokens",
                "review_output_tokens",
            ):
                totals[key] += int(evidence.get(key) or 0)
            calls = evidence.get("review_call_count")
            if isinstance(calls, int) and not isinstance(calls, bool):
                totals["review_completion_calls"] += calls
            else:
                totals["unverifiable_review_attempts"] += 1
    return dict(totals)


def _canary_can_continue(
    audit: dict[str, Any],
    *,
    results: list[dict[str, Any]] | None = None,
) -> bool:
    """Stop only for infrastructure or hard-safety failures.

    A five-run sample is too small to decide the 95% semantic release gate.
    Soft semantic misses remain recorded and are never retried; the full
    campaign must measure their actual rate.
    """
    issues = [
        issue
        for issue in audit.get("issues") or []
        if isinstance(issue, dict)
    ]
    application_failed = any(
        str(issue.get("code") or "") == "application_failed"
        for issue in issues
    )
    timed_out = any(
        attempt.get("timed_out") is True
        for result in results or []
        if isinstance(result, dict)
        for attempt in result.get("attempts") or []
        if isinstance(attempt, dict)
    )
    return bool(
        audit.get("complete")
        and not application_failed
        and not timed_out
        and not any(bool(issue.get("hard")) for issue in issues)
    )


def _git_head() -> str:
    completed = subprocess.run(
        [str(_trusted_git()), "--no-replace-objects", "rev-parse", "HEAD"],
        cwd=REPO_ROOT,
        env=_git_env(),
        text=True,
        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, check=False,
    )
    return completed.stdout.strip() if completed.returncode == 0 else "unknown"


def _initialize_active_capsule() -> list[str]:
    """Attest the inner process before it may execute one real Application."""
    global _ACTIVE_CAPSULE_DESCRIPTOR

    try:
        _consume_active_model_config()
    except Exception:
        return ["model_config_invalid"]

    issue_codes: list[str] = []
    try:
        bootstrap_issues = active_capsule_bootstrap_issues(REPO_ROOT)
    except Exception:
        bootstrap_issues = ["invalid"]
    if bootstrap_issues:
        issue_codes.append("bootstrap_invalid")

    try:
        source = release_git_source_state()
    except Exception:
        source = {}
    if source.get("available") is not True or source.get("dirty") is not False:
        issue_codes.append("source_invalid")

    try:
        model_contract = _safe_model_contract(
            config_bytes=_ACTIVE_MODEL_CONFIG_BYTES
        )
        model_contract_issues = _model_contract_issues(model_contract)
    except Exception:
        model_contract = {}
        model_contract_issues = ["invalid"]
    if model_contract_issues:
        issue_codes.append("model_contract_invalid")

    try:
        dataset = dataset_manifest()
    except Exception:
        dataset = {}
        issue_codes.append("dataset_invalid")

    if issue_codes:
        return list(dict.fromkeys(issue_codes))
    try:
        descriptor = build_capsule_descriptor(
            repo_root=REPO_ROOT,
            runner_file=Path(__file__),
            source=source,
            dataset=dataset,
            model_contract=model_contract,
            model_config_memory_only=_ACTIVE_MODEL_CONFIG_BYTES is not None,
        )
        if descriptor.get("lock_sync_ok") is not True:
            return ["dependency_environment_invalid"]
        descriptor_issues = capsule_descriptor_issues(descriptor)
    except Exception:
        return ["capsule_descriptor_invalid"]
    if descriptor_issues:
        return ["capsule_descriptor_invalid"]
    _ACTIVE_CAPSULE_DESCRIPTOR = descriptor
    return []


def _print_active_capsule_preflight_failure(issue_codes: list[str]) -> None:
    """Expose stable failure classes without echoing sensitive diagnostics."""
    print(
        json.dumps(
            {
                "status": "CAPSULE_PREFLIGHT_FAIL",
                "issue_codes": list(dict.fromkeys(issue_codes)),
            },
            sort_keys=True,
        )
    )


def _run_in_capsule(
    args: argparse.Namespace,
    *,
    campaign_id: str,
    expected_commit: str,
) -> int:
    """Re-exec the campaign from a detached checkout and private locked venv."""
    campaign_started_at = _now()
    try:
        _require_isolated_git_metadata(REPO_ROOT)
        if not trusted_control_plane_matches(REPO_ROOT, expected_commit):
            raise RuntimeError("historical runner did not match trusted control plane")
        with provision_capsule(
            REPO_ROOT,
            expected_commit=expected_commit,
        ) as capsule:
            command = [
                str(capsule.python),
                "-I",
                "-P",
                "-B",
                str(capsule.runner),
            ]
            if args.reproduce_campaign:
                command.extend(
                    [
                        "--reproduce-campaign",
                        str(args.reproduce_campaign.expanduser().resolve()),
                        "--run-id",
                        str(args.run_id).strip(),
                        "--timeout-seconds",
                        str(args.timeout_seconds),
                    ]
                )
            else:
                command.extend(
                    [
                        "--runs",
                        str(args.runs),
                        "--campaign-id",
                        campaign_id,
                        "--max-workers",
                        str(args.max_workers),
                        "--timeout-seconds",
                        str(args.timeout_seconds),
                        "--output-root",
                        str(args.output_root.expanduser().resolve()),
                        "--campaign-started-at",
                        campaign_started_at,
                    ]
                )
            completed = subprocess.run(
                command,
                cwd=capsule.root,
                env={
                    **capsule.env,
                    CAMPAIGN_LLM_CONFIG_FD_ENV: "0",
                },
                input=capsule.model_config_bytes,
                check=False,
            )
            return int(completed.returncode)
    except (OSError, RuntimeError, subprocess.SubprocessError):
        print("status=CAPSULE_PREFLIGHT_FAIL")
        return 1


def _reproduce_run(
    campaign_dir: Path,
    run_id: str,
    *,
    timeout_seconds: int,
) -> int:
    campaign_dir = campaign_dir.expanduser().resolve()
    plan = json.loads((campaign_dir / "plan.json").read_text(encoding="utf-8"))
    environment = json.loads(
        (campaign_dir / "environment.json").read_text(encoding="utf-8")
    )
    results_payload = json.loads(
        (campaign_dir / "results.json").read_text(encoding="utf-8")
    )

    from applications.memory_feature_validation.scripts.audit_memory_review_campaign import (
        _plan_issues,
        evaluate_results,
    )

    plan_issues = _plan_issues(plan)
    if plan_issues:
        print(
            json.dumps(
                {
                    "status": "REPRODUCTION_PREFLIGHT_FAIL",
                    "issues": plan_issues,
                }
            )
        )
        return 1
    specs = [RunSpec(**row) for row in plan.get("runs") or []]
    matches = [spec for spec in specs if spec.run_id == run_id]
    if len(matches) != 1:
        raise ValueError("run id must identify exactly one planned Application")
    spec = matches[0]
    original_results = (
        results_payload.get("results")
        if isinstance(results_payload, dict)
        else None
    )
    original_results = original_results if isinstance(original_results, list) else []
    original_matches = [
        result
        for result in original_results
        if isinstance(result, dict) and result.get("run_id") == run_id
    ]
    if len(original_matches) != 1:
        raise ValueError("campaign must contain exactly one original run result")
    original_result = original_matches[0]

    recorded_source = environment.get("source") or {}
    recorded_capsule = environment.get("capsule") or {}
    recorded_capsule = recorded_capsule if isinstance(recorded_capsule, dict) else {}
    current_source = release_git_source_state()
    current_model = _safe_model_contract()
    current_capsule = _ACTIVE_CAPSULE_DESCRIPTOR or {}
    preflight_issues: list[str] = []
    if (
        current_source.get("dirty") is not False
        or current_source.get("commit") != recorded_source.get("commit")
        or current_source.get("files") != recorded_source.get("files")
    ):
        preflight_issues.append("current bound sources do not match the campaign")
    if current_model != environment.get("model_contract"):
        preflight_issues.append("current summary model contract does not match the campaign")
    if dataset_manifest() != plan.get("dataset"):
        preflight_issues.append("current dataset does not match the campaign")
    if capsule_descriptor_issues(recorded_capsule):
        preflight_issues.append("recorded campaign capsule was invalid")
    if _reproduction_capsule_contract_changed(
        current_capsule,
        recorded_capsule,
    ):
        preflight_issues.append("current capsule contract does not match the campaign")
    if preflight_issues:
        print(json.dumps({"status": "REPRODUCTION_PREFLIGHT_FAIL", "issues": preflight_issues}))
        return 1

    snapshot_kind = "reproduction_state" if spec.phase == "post_recall" else "retry_state"
    snapshot = campaign_dir / snapshot_kind / run_id
    if not snapshot.is_dir():
        raise FileNotFoundError(f"pre-run state snapshot is missing: {snapshot}")
    snapshot_record = original_result.get("reproduction_snapshot")
    expected_relative = snapshot.relative_to(campaign_dir).as_posix()
    if (
        not isinstance(snapshot_record, dict)
        or snapshot_record.get("path") != expected_relative
        or snapshot_record.get("sha256") != _directory_hash(snapshot)
    ):
        raise ValueError("pre-run state snapshot does not match campaign evidence")
    reproduction_dir = (
        campaign_dir
        / "reproductions"
        / _default_campaign_id(f"reproduce-{run_id}")
    )
    reproduction_dir.mkdir(parents=True, exist_ok=False)
    oracle = indexed_rows(ORACLE_PATH)
    markers = [*all_probe_markers(oracle), *_active_marker_texts()]
    result = _run_reproduction_in_private_scratch(
        spec,
        snapshot=snapshot,
        reproduction_dir=reproduction_dir,
        timeout_seconds=timeout_seconds,
        oracle=oracle,
        markers=markers,
    )
    _write_json(reproduction_dir / "result.json", result)

    audit = evaluate_results(
        [spec],
        [result],
        oracle,
        require_complete=True,
        require_attempt_contract=True,
    )
    _write_json(reproduction_dir / "audit.json", audit)
    print(f"reproduction={reproduction_dir}")
    print("status=REPRODUCED_PASS" if audit["ok"] else "status=REPRODUCED_FAIL")
    return 0 if audit["ok"] else 1


def _recorded_reproduction_commit(campaign_dir: Path) -> str:
    try:
        environment = json.loads(
            (
                campaign_dir.expanduser().resolve() / "environment.json"
            ).read_text(encoding="utf-8")
        )
    except (OSError, ValueError, json.JSONDecodeError):
        return ""
    source = environment.get("source") if isinstance(environment, dict) else None
    commit = str(source.get("commit") or "") if isinstance(source, dict) else ""
    return commit if re.fullmatch(r"[0-9a-f]{40}", commit) else ""


def _reproduction_capsule_contract_changed(
    current: dict[str, Any],
    recorded: dict[str, Any],
) -> bool:
    return any(
        current.get(field) != recorded.get(field)
        for field in _REPRODUCTION_STABLE_CAPSULE_FIELDS
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runs", type=int, choices=(1, 5, 100), default=100)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--campaign-id", default="")
    parser.add_argument("--max-workers", type=int, choices=(1, 2), default=2)
    parser.add_argument("--timeout-seconds", type=int, default=600)
    parser.add_argument("--campaign-started-at", default="", help=argparse.SUPPRESS)
    parser.add_argument("--reproduce-campaign", type=Path)
    parser.add_argument("--run-id", default="")
    parser.add_argument(
        "--output-root",
        type=Path,
        default=REPO_ROOT / ".agentloom" / "validation" / "memory_feature_validation",
    )
    args = parser.parse_args()

    if bool(args.reproduce_campaign) != bool(str(args.run_id).strip()):
        parser.error("--reproduce-campaign and --run-id must be provided together")
    try:
        _require_isolated_git_metadata(REPO_ROOT)
    except (OSError, RuntimeError):
        if capsule_is_active():
            _print_active_capsule_preflight_failure(["git_metadata_invalid"])
        else:
            print("status=CAPSULE_PREFLIGHT_FAIL")
        return 1
    if not args.dry_run:
        if capsule_is_active():
            try:
                capsule_issues = _initialize_active_capsule()
            except Exception:
                capsule_issues = ["capsule_preflight_internal_error"]
            if capsule_issues:
                _print_active_capsule_preflight_failure(capsule_issues)
                return 1
        else:
            if args.reproduce_campaign:
                recorded_commit = _recorded_reproduction_commit(
                    args.reproduce_campaign
                )
                if not recorded_commit:
                    print("status=CAPSULE_PREFLIGHT_FAIL")
                    return 1
                return _run_in_capsule(
                    args,
                    campaign_id="",
                    expected_commit=recorded_commit,
                )
            parent_source = release_git_source_state()
            parent_model = _safe_model_contract()
            parent_ready = bool(
                parent_source.get("available") is True
                and parent_source.get("dirty") is False
                and not _model_contract_issues(parent_model)
            )
            if parent_ready:
                child_campaign_id = args.campaign_id or _default_campaign_id(
                    "memory-review"
                )
                return _run_in_capsule(
                    args,
                    campaign_id=child_campaign_id,
                    expected_commit=str(parent_source["commit"]),
                )
            print("status=CAPSULE_PREFLIGHT_FAIL")
            return 1
    if args.reproduce_campaign:
        if args.dry_run:
            parser.error("--dry-run cannot be combined with reproduction")
        return _reproduce_run(
            args.reproduce_campaign,
            str(args.run_id).strip(),
            timeout_seconds=args.timeout_seconds,
        )

    campaign_started_at = (
        str(args.campaign_started_at or "").strip()
        if capsule_is_active()
        else _now()
    )
    campaign_id = args.campaign_id or _default_campaign_id(
        "memory-review-dry" if args.dry_run else "memory-review"
    )
    campaign_dir = _campaign_dir(args.output_root, campaign_id)
    specs = select_runs(args.runs)
    oracle = indexed_rows(ORACLE_PATH)
    markers = [*all_probe_markers(oracle), *_active_marker_texts()]
    plan = {
        "schema_version": 1,
        "campaign_id": campaign_id,
        "created_at": _now(),
        "campaign_started_at": campaign_started_at,
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
    model_contract = _safe_model_contract()
    source = release_git_source_state()
    model_contract_issues = [] if args.dry_run else _model_contract_issues(model_contract)
    source_issues: list[str] = []
    if not args.dry_run:
        if not source.get("available"):
            source_issues.append("bound campaign source commit is unavailable")
        elif source.get("dirty"):
            source_issues.append("bound campaign sources must match HEAD")
    preflight_issues = [*model_contract_issues, *source_issues]
    _write_json(
        campaign_dir / "environment.json",
        {
            "created_at": _now(),
            "campaign_started_at": campaign_started_at,
            "python": sys.version,
            "platform": sys.platform,
            "git_head": _git_head(),
            "source": source,
            "model_contract": model_contract,
            "capsule": _ACTIVE_CAPSULE_DESCRIPTOR,
            "model_contract_issues": model_contract_issues,
            "source_issues": source_issues,
        },
    )
    if preflight_issues:
        _write_json(
            campaign_dir / "preflight_audit.json",
            {
                "ok": False,
                "status": "PREFLIGHT_FAIL",
                "issues": preflight_issues,
            },
        )
        print(f"campaign={campaign_dir}")
        print("status=PREFLIGHT_FAIL")
        return 1

    if args.dry_run:
        results = [{**spec.to_dict(), "status": "planned"} for spec in specs]
    else:
        results = _run_real_campaign(
            specs,
            campaign_dir=campaign_dir,
            requested_runs=args.runs,
            timeout_seconds=args.timeout_seconds,
            max_workers=args.max_workers,
            oracle=oracle,
            markers=markers,
        )

    order = {spec.run_id: index for index, spec in enumerate(specs)}
    results.sort(key=lambda result: order.get(str(result.get("run_id") or ""), 10**9))
    _write_json(campaign_dir / "results.json", {"schema_version": 1, "results": results})
    _write_json(campaign_dir / "usage.json", _usage(results))
    completed_source = release_git_source_state()
    completed_model_contract = _safe_model_contract()
    completed_dataset = dataset_manifest()
    completed_capsule = (
        build_capsule_descriptor(
            repo_root=REPO_ROOT,
            runner_file=Path(__file__),
            source=completed_source,
            dataset=completed_dataset,
            model_contract=completed_model_contract,
            model_config_memory_only=_ACTIVE_MODEL_CONFIG_BYTES is not None,
        )
        if not args.dry_run
        else None
    )
    campaign_finished_at = _now()
    _write_json(
        campaign_dir / "environment_completed.json",
        {
            "completed_at": _now(),
            "campaign_finished_at": campaign_finished_at,
            "source": completed_source,
            "model_contract": completed_model_contract,
            "dataset": completed_dataset,
            "capsule": completed_capsule,
        },
    )

    from applications.memory_feature_validation.scripts.audit_memory_review_campaign import (
        audit_campaign,
    )

    audit = audit_campaign(campaign_dir)
    final_model_config_findings = _real_marker_findings([campaign_dir])
    _write_json(
        campaign_dir / "model_config_privacy_audit.json",
        {
            "ok": not final_model_config_findings,
            "finding_count": len(final_model_config_findings),
            "findings": final_model_config_findings,
        },
    )
    if final_model_config_findings:
        audit["ok"] = False
        audit["status"] = (
            "RELEASE_FAIL" if args.runs == 100 else "CANARY_FAIL"
        )
    print(f"campaign={campaign_dir}")
    print(f"status={audit['status']}")
    return 0 if audit["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
