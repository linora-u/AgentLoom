"""Read one learning-job snapshot in a fresh SQLite process.

macOS can retain a stale WAL/SHM generation in a long-lived campaign process
after detached workers hand the same cohort database to the next ``loom run``.
Opening the observation in a short-lived process gives every poll a new SQLite
mapping and returns only allowlisted metadata; raw payloads and errors never
cross stdout.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sqlite3
import time
from datetime import datetime
from pathlib import Path, PurePosixPath
from typing import Any

_INFRA_PATTERN = re.compile(
    r"(?:\b(?:http|status(?:\s+code)?)\s*[:=]?\s*(?:429|502|503|504)\b|"
    r"\brate[ -]?limit|\bconnection (?:refused|reset|aborted)\b|"
    r"\bname resolution\b|\bservice unavailable\b|\bgateway timeout\b|"
    r"\b(?:read|connect) timed?\s*out\b)",
    re.IGNORECASE,
)


def _lease_active(value: Any) -> bool:
    try:
        return datetime.fromisoformat(str(value or "")).timestamp() > time.time()
    except (TypeError, ValueError, OverflowError):
        return False


def _json_object(value: Any) -> dict[str, Any]:
    try:
        decoded = json.loads(str(value or "{}"))
    except (TypeError, ValueError, json.JSONDecodeError):
        return {}
    return decoded if isinstance(decoded, dict) else {}


def _error_kind(value: Any) -> str:
    text = str(value or "")
    if not text:
        return ""
    return "infrastructure" if _INFRA_PATTERN.search(text) else "semantic_or_code"


def _invalid_artifact(reason: str) -> dict[str, Any]:
    return {"ok": False, "reason": reason}


def _artifact_relative_path(value: Any) -> str | None:
    raw = str(value or "").replace("\\", "/").strip()
    path = PurePosixPath(raw)
    if not raw or path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        return None
    return path.as_posix()


def _validate_artifacts(
    *,
    job_id: int,
    kind: str,
    payload: dict[str, Any],
    result: dict[str, Any],
) -> tuple[dict[str, Any], list[str]]:
    stage = payload.get("_artifact_delivery")
    if not isinstance(stage, dict):
        return _invalid_artifact("missing_delivery_manifest"), []
    if stage.get("version") != 1:
        return _invalid_artifact("unsupported_delivery_manifest"), []
    if type(stage.get("job_id")) is not int or int(stage["job_id"]) != job_id:
        return _invalid_artifact("manifest_job_id_mismatch"), []
    if str(stage.get("kind") or "") != kind:
        return _invalid_artifact("manifest_kind_mismatch"), []

    raw_run_dir = str(payload.get("run_dir") or "")
    raw_root = str(stage.get("root_dir") or "")
    if not raw_run_dir or not raw_root:
        return _invalid_artifact("missing_artifact_root"), []
    run_dir = Path(raw_run_dir).expanduser().resolve()
    root = Path(raw_root).expanduser().resolve()
    if root != run_dir:
        return _invalid_artifact("manifest_root_mismatch"), []

    raw_files = stage.get("files")
    if not isinstance(raw_files, list) or not raw_files:
        return _invalid_artifact("empty_delivery_manifest"), []
    required_json = f"learning_jobs/{job_id}.json"
    required_markdown = f"learning_jobs/{job_id}.md"
    immutable_job_paths = {required_json, required_markdown}
    entries: dict[str, dict[str, str]] = {}
    artifact_paths: list[str] = []
    for raw_entry in raw_files:
        if not isinstance(raw_entry, dict):
            return _invalid_artifact("invalid_manifest_file"), artifact_paths
        relative_path = _artifact_relative_path(raw_entry.get("relative_path"))
        content = raw_entry.get("content")
        digest = raw_entry.get("sha256")
        if relative_path is None or relative_path in entries:
            return _invalid_artifact("invalid_manifest_path"), artifact_paths
        if not isinstance(content, str) or not content:
            return _invalid_artifact("empty_manifest_content"), artifact_paths
        expected_digest = hashlib.sha256(content.encode("utf-8")).hexdigest()
        if not isinstance(digest, str) or digest != expected_digest:
            return _invalid_artifact("manifest_content_hash_mismatch"), artifact_paths
        path = root / relative_path
        artifact_paths.append(str(path))
        if not path.is_file():
            return _invalid_artifact("artifact_file_missing"), artifact_paths
        try:
            file_bytes = path.read_bytes()
        except OSError:
            return _invalid_artifact("artifact_file_unreadable"), artifact_paths
        if not file_bytes:
            return _invalid_artifact("artifact_file_empty"), artifact_paths
        # Per-job JSON/Markdown are immutable delivery receipts. Shared
        # roll-ups (session_summary.md, memory_proposals.md) are intentionally
        # appended/replaced by later jobs, so their current bytes need only be
        # present and non-empty; the campaign privacy scan still inspects them.
        if (
            relative_path in immutable_job_paths
            and hashlib.sha256(file_bytes).hexdigest() != digest
        ):
            return _invalid_artifact("artifact_file_hash_mismatch"), artifact_paths
        entries[relative_path] = {
            "content": content,
            "sha256": digest,
        }

    if required_json not in entries or required_markdown not in entries:
        return _invalid_artifact("required_job_artifact_missing"), artifact_paths
    try:
        artifact_result = json.loads(entries[required_json]["content"])
    except (TypeError, ValueError, json.JSONDecodeError):
        return _invalid_artifact("job_json_invalid"), artifact_paths
    if artifact_result != result:
        return _invalid_artifact("job_json_result_mismatch"), artifact_paths
    markdown = entries[required_markdown]["content"]
    if f"Learning Job {job_id}" not in markdown:
        return _invalid_artifact("markdown_job_id_mismatch"), artifact_paths
    if f"kind: {kind}" not in markdown:
        return _invalid_artifact("markdown_kind_mismatch"), artifact_paths
    return {"ok": True, "reason": "verified"}, artifact_paths


def observe_jobs(db_path: Path, after_id: int) -> list[dict[str, Any]]:
    if not db_path.exists():
        return []
    uri = db_path.resolve().as_uri() + "?mode=ro&cache=private"
    with sqlite3.connect(uri, uri=True) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            """
            SELECT id, kind, dedupe_key, root_run_id, status, attempts,
                   available_at, lease_until, payload_json, result_json,
                   created_at, updated_at, finished_at, last_error
            FROM learning_jobs WHERE id > ? ORDER BY id
            """,
            (int(after_id),),
        ).fetchall()

    jobs: list[dict[str, Any]] = []
    for row in rows:
        payload = _json_object(row["payload_json"])
        result = _json_object(row["result_json"])
        job_id = int(row["id"])
        kind = str(row["kind"])
        artifact_paths: list[str] = []
        if str(row["status"]) != "succeeded":
            artifact_delivery = "not_required"
            artifact_validation = {"ok": True, "reason": "job_not_succeeded"}
        elif result.get("artifact_error"):
            artifact_delivery = "failed"
            artifact_validation = _invalid_artifact("legacy_artifact_error")
        elif kind not in {"session_review", "retention"}:
            artifact_delivery = "not_required"
            artifact_validation = {"ok": True, "reason": "job_kind_has_no_artifacts"}
        else:
            artifact_validation, artifact_paths = _validate_artifacts(
                job_id=job_id,
                kind=kind,
                payload=payload,
                result=result,
            )
            artifact_delivery = "delivered" if artifact_validation["ok"] else "invalid"
        jobs.append(
            {
                "id": job_id,
                "kind": kind,
                "dedupe_key": str(row["dedupe_key"]),
                "root_run_id": str(row["root_run_id"]),
                "status": str(row["status"]),
                "attempts": int(row["attempts"] or 0),
                "available_at": row["available_at"],
                "lease_active": _lease_active(row["lease_until"]),
                "finished_at": row["finished_at"],
                "created_at": row["created_at"],
                "updated_at": row["updated_at"],
                "error_kind": _error_kind(row["last_error"]),
                "has_error": bool(row["last_error"]),
                "artifact_delivery": artifact_delivery,
                "artifact_validation": artifact_validation,
                "artifact_paths": artifact_paths,
            }
        )
    return jobs


def observe_max_job_id(db_path: Path) -> int:
    if not db_path.exists():
        return 0
    uri = db_path.resolve().as_uri() + "?mode=ro&cache=private"
    with sqlite3.connect(uri, uri=True) as conn:
        row = conn.execute("SELECT COALESCE(MAX(id), 0) FROM learning_jobs").fetchone()
    return int(row[0] or 0) if row else 0


def main() -> int:
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--db", type=Path, required=True)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--after-id", type=int)
    mode.add_argument("--max-id-only", action="store_true")
    args = parser.parse_args()
    try:
        if args.max_id_only:
            max_id = observe_max_job_id(args.db)
            jobs: list[dict[str, Any]] = []
        else:
            jobs = observe_jobs(args.db, int(args.after_id))
            max_id = max((int(job["id"]) for job in jobs), default=int(args.after_id))
    except (OSError, sqlite3.Error) as exc:
        print(json.dumps({"ok": False, "error_type": type(exc).__name__}))
        return 1
    print(json.dumps({"ok": True, "max_id": max_id, "jobs": jobs}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
