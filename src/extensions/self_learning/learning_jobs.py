"""Durable SQLite outbox and fenced worker for self-learning maintenance."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import tempfile
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path, PurePosixPath
from threading import Event, Thread
from typing import Any

from src.lib.logging import get_logger

from .ledger import LEGACY_SANITIZER_DEAD_ERROR, SelfLearningLedger
from .paths import project_root
from .redaction import (
    redact_text,
    require_safe_identity,
    sanitize_text_fragment,
    sanitize_value_fragments,
    scan_injection_patterns,
)

logger = get_logger(__name__)

_JOB_STATUSES = ("pending", "running", "retry", "succeeded", "dead")
_WORKER_LEASE_KEY = "learning_worker_lease"
_WORKER_KICK_LEASE_KEY = "learning_worker_kick_lease"
_DEFAULT_LEASE_SECONDS = 180
_KICK_LEASE_SECONDS = 30
_DETACHED_MAX_WAIT_SECONDS = 15.0
_MAX_ATTEMPTS = 3
_RETRY_DELAYS_SECONDS = (2, 10)
_HEARTBEAT_SECONDS = 30.0
_ARTIFACT_DELIVERY_PAYLOAD_KEY = "_artifact_delivery"
_ARTIFACT_DELIVERY_VERSION = 1


class JobLeaseFencedError(RuntimeError):
    """The worker no longer owns a live lease for this job."""


def _now_iso(now: str | datetime | None = None) -> str:
    if isinstance(now, str):
        return datetime.fromisoformat(now).isoformat()
    value = now or datetime.now().astimezone()
    if value.tzinfo is None:
        value = value.astimezone()
    return value.isoformat()


def _after(iso_value: str, seconds: int) -> str:
    return (datetime.fromisoformat(iso_value) + timedelta(seconds=seconds)).isoformat()


def _json_dumps(value: Any) -> str:
    return json.dumps(value if value is not None else {}, ensure_ascii=False, sort_keys=True, default=str)


def _decode_json(value: Any) -> Any:
    if not value:
        return {}
    try:
        return json.loads(str(value))
    except (TypeError, json.JSONDecodeError):
        return {}


@dataclass
class JobExecution:
    """A semantic result plus optional durable artifact-delivery manifest."""

    result: dict[str, Any]
    artifact_delivery: dict[str, Any] | None = None


def _artifact_relative_path(value: Any) -> str:
    raw = str(value or "").replace("\\", "/").strip()
    path = PurePosixPath(raw)
    if not raw or path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        raise ValueError("artifact relative path must stay inside its delivery root")
    return path.as_posix()


def _artifact_sha256(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def _require_safe_artifact_content(content: str) -> str:
    """Accept exact artifact bytes only when no further mutation is needed.

    ``[BLOCKED]`` is an intentional safe marker inside JSON/Markdown artifacts,
    not an instruction. Other injection findings and any credential redaction
    candidate reject the manifest before its hash is frozen.
    """
    if redact_text(content) != content:
        raise ValueError("artifact content contains sensitive text")
    findings = [
        finding
        for finding in scan_injection_patterns(content)
        if finding != "blocked-fragment"
    ]
    if findings:
        raise ValueError("artifact content contains blocked instructions")
    return content


def build_artifact_delivery(
    *,
    job_id: int,
    kind: str,
    root_dir: str | Path,
    files: dict[str, str],
) -> dict[str, Any]:
    """Build a serializable, self-verifying artifact-delivery manifest.

    The manifest is persisted before any file is written.  A reclaimed worker
    can therefore finish these exact bytes without re-running the semantic
    handler (and, for session-review jobs, without invoking the model again).
    """
    if type(job_id) is not int or job_id < 1:
        raise TypeError("artifact job_id must be a positive integer")
    kind = require_safe_identity(kind, field="artifact job kind")
    root = Path(root_dir).expanduser().resolve()
    if not isinstance(files, dict) or not files:
        raise ValueError("artifact delivery requires at least one file")
    entries: list[dict[str, Any]] = []
    for relative_path, raw_content in sorted(files.items()):
        path = _artifact_relative_path(relative_path)
        require_safe_identity(path, field="artifact relative path")
        if not isinstance(raw_content, str) or not raw_content:
            raise ValueError("artifact content must be a non-empty string")
        content = _require_safe_artifact_content(raw_content)
        entries.append(
            {
                "relative_path": path,
                "content": content,
                "sha256": _artifact_sha256(content),
            }
        )
    return {
        "version": _ARTIFACT_DELIVERY_VERSION,
        "job_id": job_id,
        "kind": kind,
        "root_dir": str(root),
        "attempts": 0,
        "files": entries,
    }


def _artifact_stage(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, dict):
        return None
    stage = value.get(_ARTIFACT_DELIVERY_PAYLOAD_KEY)
    return stage if isinstance(stage, dict) else None


def _validated_artifact_stage(
    value: Any,
    *,
    job_id: int,
    kind: str,
    result: dict[str, Any],
    state_root: Path,
    expected_root: Path | None = None,
) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != {
        "version",
        "job_id",
        "kind",
        "root_dir",
        "attempts",
        "files",
    }:
        raise ValueError("artifact delivery manifest has an invalid schema")
    if value.get("version") != _ARTIFACT_DELIVERY_VERSION:
        raise ValueError("artifact delivery manifest version is unsupported")
    if type(value.get("job_id")) is not int or int(value["job_id"]) != int(job_id):
        raise ValueError("artifact delivery job id does not match its claim")
    if str(value.get("kind") or "") != str(kind):
        raise ValueError("artifact delivery kind does not match its job")
    if type(value.get("attempts")) is not int or int(value["attempts"]) < 0:
        raise ValueError("artifact delivery attempts must be a non-negative integer")
    root = Path(str(value.get("root_dir") or "")).expanduser().resolve()
    if expected_root is not None:
        if root != expected_root.expanduser().resolve():
            raise ValueError("artifact delivery root does not match the job run directory")
    else:
        try:
            root.relative_to(state_root.resolve())
        except ValueError as exc:
            raise ValueError("artifact delivery root is outside self-learning state") from exc
    raw_files = value.get("files")
    if not isinstance(raw_files, list) or not raw_files:
        raise ValueError("artifact delivery manifest has no files")
    files: list[dict[str, str]] = []
    seen_paths: set[str] = set()
    for entry in raw_files:
        if not isinstance(entry, dict) or set(entry) != {
            "relative_path",
            "content",
            "sha256",
        }:
            raise ValueError("artifact delivery file has an invalid schema")
        relative_path = _artifact_relative_path(entry.get("relative_path"))
        content = entry.get("content")
        digest = entry.get("sha256")
        if relative_path in seen_paths:
            raise ValueError("artifact delivery contains a duplicate file path")
        if not isinstance(content, str) or not content:
            raise ValueError("artifact delivery file content is empty")
        _require_safe_artifact_content(content)
        if not isinstance(digest, str) or digest != _artifact_sha256(content):
            raise ValueError("artifact delivery file hash does not match its content")
        seen_paths.add(relative_path)
        files.append(
            {
                "relative_path": relative_path,
                "content": content,
                "sha256": digest,
            }
        )

    required_json = f"learning_jobs/{int(job_id)}.json"
    required_markdown = f"learning_jobs/{int(job_id)}.md"
    by_path = {entry["relative_path"]: entry for entry in files}
    if required_json not in by_path or required_markdown not in by_path:
        raise ValueError("artifact delivery must include job JSON and Markdown files")
    try:
        stored_json = json.loads(by_path[required_json]["content"])
    except (TypeError, json.JSONDecodeError) as exc:
        raise ValueError("artifact delivery JSON is invalid") from exc
    safe_result = sanitize_value_fragments(result)
    if stored_json != safe_result:
        raise ValueError("artifact delivery JSON does not match the staged job result")
    markdown = by_path[required_markdown]["content"]
    if f"Learning Job {int(job_id)}" not in markdown or f"kind: {kind}" not in markdown:
        raise ValueError("artifact delivery Markdown does not identify its job and kind")
    return {
        "version": _ARTIFACT_DELIVERY_VERSION,
        "job_id": int(job_id),
        "kind": str(kind),
        "root_dir": str(root),
        "attempts": int(value["attempts"]),
        "files": files,
    }


def _atomic_write_artifact(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(
        dir=str(path.parent),
        prefix=f".{path.name}.",
        suffix=".tmp",
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(content)
        os.replace(temporary, path)
    except BaseException:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise


def deliver_artifact_stage(
    stage: dict[str, Any],
    *,
    job_id: int,
    kind: str,
    result: dict[str, Any],
    state_root: Path,
    expected_root: Path | None = None,
) -> None:
    """Idempotently write one already-persisted artifact manifest."""
    validated = _validated_artifact_stage(
        stage,
        job_id=job_id,
        kind=kind,
        result=result,
        state_root=state_root,
        expected_root=expected_root,
    )
    root = Path(validated["root_dir"])
    for entry in validated["files"]:
        _atomic_write_artifact(
            root / entry["relative_path"],
            entry["content"],
        )


class LearningJobQueue:
    """Public job-store seam used by SessionEnd, CLI, and the worker process."""

    def __init__(self, db_path: str | Path | None = None):
        self.ledger = SelfLearningLedger(db_path)
        self.db_path = self.ledger.db_path

    @staticmethod
    def _row(row: Any) -> dict[str, Any]:
        result = dict(row)
        result["payload"] = _decode_json(result.pop("payload_json", ""))
        result["result"] = _decode_json(result.pop("result_json", ""))
        return result

    def enqueue(
        self,
        kind: str,
        dedupe_key: str,
        root_run_id: str,
        payload: dict[str, Any] | None = None,
        *,
        now: str | datetime | None = None,
    ) -> dict[str, Any]:
        kind = require_safe_identity(kind, field="job kind")
        dedupe_key = require_safe_identity(dedupe_key, field="job dedupe key")
        root_run_id = require_safe_identity(root_run_id, field="job root run id")
        timestamp = _now_iso(now)
        with self.ledger._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            cursor = conn.execute(
                """
                INSERT OR IGNORE INTO learning_jobs (
                    kind, dedupe_key, root_run_id, payload_json, status, attempts,
                    available_at, created_at, updated_at
                ) VALUES (?, ?, ?, ?, 'pending', 0, ?, ?, ?)
                """,
                (
                    kind,
                    dedupe_key,
                    root_run_id,
                    _json_dumps(sanitize_value_fragments(payload or {})),
                    timestamp,
                    timestamp,
                    timestamp,
                ),
            )
            row = conn.execute(
                "SELECT * FROM learning_jobs WHERE kind = ? AND dedupe_key = ?",
                (kind, dedupe_key),
            ).fetchone()
        result = self._row(row)
        result["created"] = bool(cursor.rowcount)
        return result

    def get_job(self, job_id: int) -> dict[str, Any]:
        with self.ledger._connect() as conn:
            row = conn.execute("SELECT * FROM learning_jobs WHERE id = ?", (int(job_id),)).fetchone()
        if row is None:
            raise KeyError(f"Learning job not found: {job_id}")
        return self._row(row)

    def list_jobs(self, *, status: str | None = None, limit: int = 100) -> list[dict[str, Any]]:
        limit = max(1, min(int(limit or 100), 1000))
        params: list[Any] = []
        where = ""
        if status:
            if status not in _JOB_STATUSES:
                raise ValueError(f"Unsupported job status: {status}")
            where = "WHERE status = ?"
            params.append(status)
        with self.ledger._connect() as conn:
            rows = conn.execute(
                f"SELECT * FROM learning_jobs {where} ORDER BY id ASC LIMIT ?",
                [*params, limit],
            ).fetchall()
        return [self._row(row) for row in rows]

    def stats(self) -> dict[str, Any]:
        counts = {status: 0 for status in _JOB_STATUSES}
        with self.ledger._connect() as conn:
            for row in conn.execute("SELECT status, COUNT(*) AS count FROM learning_jobs GROUP BY status"):
                counts[str(row["status"])] = int(row["count"])
        return {"by_status": counts, "total": sum(counts.values())}

    def claim_worker_kick_slot(
        self,
        *,
        now: str | datetime | None = None,
        lease_seconds: int = _KICK_LEASE_SECONDS,
    ) -> str | None:
        """Fence detached-worker creation across concurrent SessionEnd hooks.

        The outbox is the durability boundary.  This slot only prevents a
        process storm in the gap before the spawned worker acquires its global
        worker lease.  A crashed launcher is recoverable after the short TTL.
        """
        with self.ledger._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            timestamp = _now_iso(now)
            lease_until = _after(timestamp, max(1, int(lease_seconds)))
            ready = conn.execute(
                """
                SELECT 1 FROM learning_jobs
                WHERE (status IN ('pending', 'retry') AND available_at <= ?)
                   OR (status = 'running' AND COALESCE(lease_until, '') <= ?)
                LIMIT 1
                """,
                (timestamp, timestamp),
            ).fetchone()
            if ready is None:
                return None
            worker_row = conn.execute(
                "SELECT value FROM maintenance WHERE key = ?",
                (_WORKER_LEASE_KEY,),
            ).fetchone()
            worker = _decode_json(worker_row["value"] if worker_row else "")
            if worker and str(worker.get("lease_until") or "") > timestamp:
                return None
            kick_row = conn.execute(
                "SELECT value FROM maintenance WHERE key = ?",
                (_WORKER_KICK_LEASE_KEY,),
            ).fetchone()
            kick = _decode_json(kick_row["value"] if kick_row else "")
            if kick and str(kick.get("lease_until") or "") > timestamp:
                return None
            token = uuid.uuid4().hex
            conn.execute(
                """
                INSERT INTO maintenance (key, value) VALUES (?, ?)
                ON CONFLICT(key) DO UPDATE SET value = excluded.value
                """,
                (
                    _WORKER_KICK_LEASE_KEY,
                    _json_dumps({"token": token, "lease_until": lease_until}),
                ),
            )
        return token

    def release_worker_kick_slot(self, token: str) -> bool:
        with self.ledger._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT value FROM maintenance WHERE key = ?",
                (_WORKER_KICK_LEASE_KEY,),
            ).fetchone()
            current = _decode_json(row["value"] if row else "")
            if current.get("token") != str(token or ""):
                return False
            conn.execute(
                "DELETE FROM maintenance WHERE key = ?",
                (_WORKER_KICK_LEASE_KEY,),
            )
        return True

    def continue_worker_kick_slot(
        self,
        token: str,
        *,
        now: str | datetime | None = None,
        lease_seconds: int = _KICK_LEASE_SECONDS,
    ) -> bool:
        """Atomically retain a launch fence only for work that is ready now.

        ``LearningJobWorker.run_until_idle`` releases the global maintenance
        lease at every bounded batch boundary.  The detached entry calls this
        method immediately afterwards.  If work was committed while a
        concurrent kick was coalesced, the same process renews its kick slot
        and drains another batch.  Otherwise the slot is deleted in the same
        transaction as the readiness check, so a later enqueue can launch a
        new worker without falling into a shutdown race.

        A different live worker or a replaced kick token is a fenced handoff:
        this entry must exit and let the new owner drain the outbox.
        Future retry jobs deliberately do not retain the slot here; the normal
        retry wait inside the current batch owns that bounded delay.
        """
        with self.ledger._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            timestamp = _now_iso(now)
            row = conn.execute(
                "SELECT value FROM maintenance WHERE key = ?",
                (_WORKER_KICK_LEASE_KEY,),
            ).fetchone()
            current = _decode_json(row["value"] if row else "")
            if current.get("token") != str(token or ""):
                return False

            worker_row = conn.execute(
                "SELECT value FROM maintenance WHERE key = ?",
                (_WORKER_LEASE_KEY,),
            ).fetchone()
            worker = _decode_json(worker_row["value"] if worker_row else "")
            worker_is_live = bool(
                worker and str(worker.get("lease_until") or "") > timestamp
            )
            ready = conn.execute(
                """
                SELECT 1 FROM learning_jobs
                WHERE (status IN ('pending', 'retry') AND available_at <= ?)
                   OR (status = 'running' AND COALESCE(lease_until, '') <= ?)
                LIMIT 1
                """,
                (timestamp, timestamp),
            ).fetchone()
            if ready is not None and not worker_is_live:
                current["lease_until"] = _after(
                    timestamp,
                    max(1, int(lease_seconds)),
                )
                conn.execute(
                    "UPDATE maintenance SET value = ? WHERE key = ?",
                    (_json_dumps(current), _WORKER_KICK_LEASE_KEY),
                )
                return True

            conn.execute(
                "DELETE FROM maintenance WHERE key = ?",
                (_WORKER_KICK_LEASE_KEY,),
            )
        return False

    def acquire_worker_lease(
        self,
        owner: str,
        *,
        now: str | datetime | None = None,
        lease_seconds: int = _DEFAULT_LEASE_SECONDS,
    ) -> str | None:
        owner = require_safe_identity(owner, field="worker owner")
        with self.ledger._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            timestamp = _now_iso(now)
            lease_until = _after(timestamp, max(1, int(lease_seconds)))
            row = conn.execute("SELECT value FROM maintenance WHERE key = ?", (_WORKER_LEASE_KEY,)).fetchone()
            current = _decode_json(row["value"] if row else "")
            if current and str(current.get("lease_until") or "") > timestamp:
                if current.get("owner") == owner:
                    token = str(current.get("token") or "")
                    if not token:
                        return None
                    current["lease_until"] = max(
                        str(current.get("lease_until") or ""),
                        lease_until,
                    )
                    conn.execute(
                        "UPDATE maintenance SET value = ? WHERE key = ?",
                        (_json_dumps(current), _WORKER_LEASE_KEY),
                    )
                    return token
                return None
            token = uuid.uuid4().hex
            value = _json_dumps({"owner": owner, "token": token, "lease_until": lease_until})
            conn.execute(
                """
                INSERT INTO maintenance (key, value) VALUES (?, ?)
                ON CONFLICT(key) DO UPDATE SET value = excluded.value
                """,
                (_WORKER_LEASE_KEY, value),
            )
        return token

    def renew_worker_lease(
        self,
        owner: str,
        token: str,
        *,
        now: str | datetime | None = None,
        lease_seconds: int = _DEFAULT_LEASE_SECONDS,
    ) -> bool:
        owner = require_safe_identity(owner, field="worker owner")
        with self.ledger._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            timestamp = _now_iso(now)
            row = conn.execute("SELECT value FROM maintenance WHERE key = ?", (_WORKER_LEASE_KEY,)).fetchone()
            current = _decode_json(row["value"] if row else "")
            if (
                current.get("owner") != owner
                or current.get("token") != token
                or str(current.get("lease_until") or "") <= timestamp
            ):
                return False
            current["lease_until"] = _after(timestamp, max(1, int(lease_seconds)))
            conn.execute(
                "UPDATE maintenance SET value = ? WHERE key = ?",
                (_json_dumps(current), _WORKER_LEASE_KEY),
            )
        return True

    def release_worker_lease(self, owner: str, token: str) -> bool:
        owner = require_safe_identity(owner, field="worker owner")
        with self.ledger._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT value FROM maintenance WHERE key = ?", (_WORKER_LEASE_KEY,)).fetchone()
            current = _decode_json(row["value"] if row else "")
            if current.get("owner") != owner or current.get("token") != token:
                return False
            conn.execute("DELETE FROM maintenance WHERE key = ?", (_WORKER_LEASE_KEY,))
        return True

    def _has_worker_lease(self, conn: Any, owner: str, token: str, timestamp: str) -> bool:
        row = conn.execute("SELECT value FROM maintenance WHERE key = ?", (_WORKER_LEASE_KEY,)).fetchone()
        current = _decode_json(row["value"] if row else "")
        return bool(
            current.get("owner") == owner
            and current.get("token") == token
            and str(current.get("lease_until") or "") > timestamp
        )

    def claim(
        self,
        owner: str,
        worker_token: str,
        *,
        now: str | datetime | None = None,
        lease_seconds: int = _DEFAULT_LEASE_SECONDS,
    ) -> dict[str, Any] | None:
        owner = require_safe_identity(owner, field="worker owner")
        with self.ledger._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            timestamp = _now_iso(now)
            if not self._has_worker_lease(conn, owner, worker_token, timestamp):
                return None
            # A process death never reaches either failure path. Retire an
            # exhausted expired semantic claim, or an exhausted artifact-only
            # claim, before selecting work so crash-only jobs cannot be
            # reclaimed forever. Artifact delivery owns a separate attempt
            # counter because reclaiming it must never re-run the model.
            exhausted = conn.execute(
                """
                SELECT id, attempts, payload_json FROM learning_jobs
                WHERE status = 'running' AND COALESCE(lease_until, '') <= ?
                """,
                (timestamp,),
            ).fetchall()
            for exhausted_row in exhausted:
                payload = _decode_json(exhausted_row["payload_json"])
                stage = _artifact_stage(payload)
                if stage is not None:
                    exhausted_attempts = int(stage.get("attempts") or 0)
                    exhausted_error = (
                        f"artifact delivery lease expired after {_MAX_ATTEMPTS} attempts"
                    )
                else:
                    exhausted_attempts = int(exhausted_row["attempts"] or 0)
                    exhausted_error = f"job lease expired after {_MAX_ATTEMPTS} attempts"
                if exhausted_attempts < _MAX_ATTEMPTS:
                    continue
                conn.execute(
                    """
                    UPDATE learning_jobs
                    SET status = 'dead', available_at = ?,
                        last_error = ?, lease_owner = NULL, lease_token = NULL,
                        lease_until = NULL, updated_at = ?, finished_at = ?
                    WHERE id = ? AND status = 'running'
                        AND COALESCE(lease_until, '') <= ?
                    """,
                    (
                        timestamp,
                        exhausted_error,
                        timestamp,
                        timestamp,
                        int(exhausted_row["id"]),
                        timestamp,
                    ),
                )
            row = conn.execute(
                """
                SELECT * FROM learning_jobs
                WHERE (
                    status IN ('pending', 'retry') AND available_at <= ?
                ) OR (
                    status = 'running' AND COALESCE(lease_until, '') <= ?
                )
                ORDER BY id ASC
                LIMIT 1
                """,
                (timestamp, timestamp),
            ).fetchone()
            if row is None:
                return None
            payload = _decode_json(row["payload_json"])
            semantic_attempt_increment = 0 if _artifact_stage(payload) is not None else 1
            lease_token = uuid.uuid4().hex
            lease_until = _after(timestamp, max(1, int(lease_seconds)))
            updated = conn.execute(
                """
                UPDATE learning_jobs
                SET status = 'running', attempts = attempts + ?,
                    lease_owner = ?, lease_token = ?, lease_until = ?,
                    updated_at = ?
                WHERE id = ? AND (
                    (status IN ('pending', 'retry') AND available_at <= ?)
                    OR (status = 'running' AND COALESCE(lease_until, '') <= ?)
                )
                """,
                (
                    semantic_attempt_increment,
                    owner,
                    lease_token,
                    lease_until,
                    timestamp,
                    int(row["id"]),
                    timestamp,
                    timestamp,
                ),
            ).rowcount
            if not updated:
                return None
            claimed = conn.execute("SELECT * FROM learning_jobs WHERE id = ?", (int(row["id"]),)).fetchone()
        return self._row(claimed)

    def stage_artifact_delivery(
        self,
        job_id: int,
        lease_token: str,
        result: dict[str, Any],
        delivery: dict[str, Any],
        *,
        now: str | datetime | None = None,
    ) -> dict[str, Any]:
        """Persist a semantic result and exact artifact bytes under a live claim."""
        timestamp = _now_iso(now)
        safe_result = sanitize_value_fragments(result)
        if not isinstance(safe_result, dict):
            safe_result = {"value": safe_result}
        with self.ledger._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT kind, payload_json FROM learning_jobs "
                "WHERE id = ? AND status = 'running' AND lease_token = ? "
                "AND COALESCE(lease_until, '') > ?",
                (int(job_id), lease_token, timestamp),
            ).fetchone()
            if row is None:
                raise JobLeaseFencedError(f"Learning job lease was fenced: {job_id}")
            payload = _decode_json(row["payload_json"])
            if not isinstance(payload, dict):
                payload = {}
            expected_root = (
                Path(str(payload["run_dir"]))
                if str(payload.get("run_dir") or "").strip()
                else None
            )
            validated = _validated_artifact_stage(
                delivery,
                job_id=int(job_id),
                kind=str(row["kind"]),
                result=safe_result,
                state_root=self.db_path.parent,
                expected_root=expected_root,
            )
            existing = _artifact_stage(payload)
            if existing is not None and existing != validated:
                raise JobLeaseFencedError(
                    f"Learning job artifact manifest is already frozen: {job_id}"
                )
            payload[_ARTIFACT_DELIVERY_PAYLOAD_KEY] = validated
            boundary_timestamp = _now_iso(now)
            updated = conn.execute(
                "UPDATE learning_jobs SET payload_json = ?, result_json = ?, "
                "updated_at = ? WHERE id = ? AND status = 'running' "
                "AND lease_token = ? AND COALESCE(lease_until, '') > ?",
                (
                    _json_dumps(payload),
                    _json_dumps(safe_result),
                    boundary_timestamp,
                    int(job_id),
                    lease_token,
                    boundary_timestamp,
                ),
            ).rowcount
            if not updated:
                raise JobLeaseFencedError(f"Learning job lease was fenced: {job_id}")
        return validated

    def begin_artifact_delivery(
        self,
        job_id: int,
        lease_token: str,
        *,
        now: str | datetime | None = None,
    ) -> dict[str, Any]:
        """Persist one artifact-delivery attempt before touching the filesystem."""
        timestamp = _now_iso(now)
        with self.ledger._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT kind, payload_json, result_json FROM learning_jobs "
                "WHERE id = ? AND status = 'running' AND lease_token = ? "
                "AND COALESCE(lease_until, '') > ?",
                (int(job_id), lease_token, timestamp),
            ).fetchone()
            if row is None:
                raise JobLeaseFencedError(f"Learning job lease was fenced: {job_id}")
            payload = _decode_json(row["payload_json"])
            result = _decode_json(row["result_json"])
            stage = _artifact_stage(payload)
            if stage is None or not isinstance(result, dict):
                raise ValueError("artifact delivery was not durably staged")
            validated = _validated_artifact_stage(
                stage,
                job_id=int(job_id),
                kind=str(row["kind"]),
                result=result,
                state_root=self.db_path.parent,
                expected_root=(
                    Path(str(payload["run_dir"]))
                    if str(payload.get("run_dir") or "").strip()
                    else None
                ),
            )
            attempts = int(validated["attempts"])
            if attempts >= _MAX_ATTEMPTS:
                raise RuntimeError("artifact delivery attempt budget is exhausted")
            validated["attempts"] = attempts + 1
            payload[_ARTIFACT_DELIVERY_PAYLOAD_KEY] = validated
            boundary_timestamp = _now_iso(now)
            updated = conn.execute(
                "UPDATE learning_jobs SET payload_json = ?, updated_at = ? "
                "WHERE id = ? AND status = 'running' AND lease_token = ? "
                "AND COALESCE(lease_until, '') > ?",
                (
                    _json_dumps(payload),
                    boundary_timestamp,
                    int(job_id),
                    lease_token,
                    boundary_timestamp,
                ),
            ).rowcount
            if not updated:
                raise JobLeaseFencedError(f"Learning job lease was fenced: {job_id}")
        return validated

    def complete_artifact_delivery(
        self,
        job_id: int,
        lease_token: str,
        *,
        now: str | datetime | None = None,
    ) -> bool:
        """Mark a staged job succeeded only after every manifest file verifies."""
        timestamp = _now_iso(now)
        with self.ledger._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT kind, payload_json, result_json FROM learning_jobs "
                "WHERE id = ? AND status = 'running' AND lease_token = ? "
                "AND COALESCE(lease_until, '') > ?",
                (int(job_id), lease_token, timestamp),
            ).fetchone()
            if row is None:
                return False
            payload = _decode_json(row["payload_json"])
            result = _decode_json(row["result_json"])
            stage = _artifact_stage(payload)
            if stage is None or not isinstance(result, dict):
                raise ValueError("artifact delivery was not durably staged")
            validated = _validated_artifact_stage(
                stage,
                job_id=int(job_id),
                kind=str(row["kind"]),
                result=result,
                state_root=self.db_path.parent,
                expected_root=(
                    Path(str(payload["run_dir"]))
                    if str(payload.get("run_dir") or "").strip()
                    else None
                ),
            )
            root = Path(validated["root_dir"])
            for entry in validated["files"]:
                path = root / entry["relative_path"]
                if not path.is_file() or not path.stat().st_size:
                    raise OSError("artifact delivery file is missing or empty")
                if hashlib.sha256(path.read_bytes()).hexdigest() != entry["sha256"]:
                    raise OSError("artifact delivery file hash mismatch")
            boundary_timestamp = _now_iso(now)
            cursor = conn.execute(
                """
                UPDATE learning_jobs
                SET status = 'succeeded', last_error = NULL,
                    lease_owner = NULL, lease_token = NULL, lease_until = NULL,
                    updated_at = ?, finished_at = ?
                WHERE id = ? AND status = 'running' AND lease_token = ?
                    AND COALESCE(lease_until, '') > ?
                """,
                (
                    boundary_timestamp,
                    boundary_timestamp,
                    int(job_id),
                    lease_token,
                    boundary_timestamp,
                ),
            )
        return bool(cursor.rowcount)

    def fail_artifact_delivery(
        self,
        job_id: int,
        lease_token: str,
        error: str,
        *,
        now: str | datetime | None = None,
    ) -> str | None:
        """Retry only persisted artifact bytes; semantic attempts stay frozen."""
        safe_error = sanitize_text_fragment(str(error), max_chars=4000)
        with self.ledger._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            timestamp = _now_iso(now)
            row = conn.execute(
                "SELECT payload_json FROM learning_jobs "
                "WHERE id = ? AND status = 'running' AND lease_token = ? "
                "AND COALESCE(lease_until, '') > ?",
                (int(job_id), lease_token, timestamp),
            ).fetchone()
            if row is None:
                return None
            payload = _decode_json(row["payload_json"])
            stage = _artifact_stage(payload)
            if stage is None:
                raise ValueError("artifact delivery failure has no durable stage")
            delivery_attempts = int(stage.get("attempts") or 0)
            if delivery_attempts < 1:
                raise ValueError("artifact delivery attempt was not started")
            if delivery_attempts < _MAX_ATTEMPTS:
                delay = _RETRY_DELAYS_SECONDS[
                    min(delivery_attempts - 1, len(_RETRY_DELAYS_SECONDS) - 1)
                ]
                status = "retry"
                available_at = _after(timestamp, delay)
                finished_at = None
            else:
                status = "dead"
                available_at = timestamp
                finished_at = timestamp
            updated = conn.execute(
                """
                UPDATE learning_jobs
                SET payload_json = ?, status = ?, available_at = ?, last_error = ?,
                    lease_owner = NULL, lease_token = NULL, lease_until = NULL,
                    updated_at = ?, finished_at = ?
                WHERE id = ? AND status = 'running' AND lease_token = ?
                    AND COALESCE(lease_until, '') > ?
                """,
                (
                    _json_dumps(payload),
                    status,
                    available_at,
                    safe_error,
                    timestamp,
                    finished_at,
                    int(job_id),
                    lease_token,
                    timestamp,
                ),
            ).rowcount
        return status if updated else None

    def complete(
        self,
        job_id: int,
        lease_token: str,
        result: dict[str, Any] | None,
        *,
        now: str | datetime | None = None,
    ) -> bool:
        # Result safety work may be arbitrarily slow for a large/nested value.
        # Finish it before taking the write lock, then compare the lease against
        # a fresh clock while the UPDATE is the next operation to commit.
        result_json = _json_dumps(sanitize_value_fragments(result or {}))
        with self.ledger._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            timestamp = _now_iso(now)
            cursor = conn.execute(
                """
                UPDATE learning_jobs
                SET status = 'succeeded', result_json = ?, last_error = NULL,
                    lease_owner = NULL, lease_token = NULL, lease_until = NULL,
                    updated_at = ?, finished_at = ?
                WHERE id = ? AND status = 'running' AND lease_token = ?
                    AND COALESCE(lease_until, '') > ?
                """,
                (
                    result_json,
                    timestamp,
                    timestamp,
                    int(job_id),
                    lease_token,
                    timestamp,
                ),
            )
        return bool(cursor.rowcount)

    def fail(
        self,
        job_id: int,
        lease_token: str,
        error: str,
        *,
        now: str | datetime | None = None,
        retryable: bool = True,
    ) -> str | None:
        # Redaction can scan a large provider error. It must not consume any of
        # the claim's remaining lease after the timestamp used by the fence.
        safe_error = sanitize_text_fragment(str(error), max_chars=4000)
        with self.ledger._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            timestamp = _now_iso(now)
            row = conn.execute(
                "SELECT attempts FROM learning_jobs "
                "WHERE id = ? AND status = 'running' AND lease_token = ? "
                "AND COALESCE(lease_until, '') > ?",
                (int(job_id), lease_token, timestamp),
            ).fetchone()
            if row is None:
                return None
            attempts = int(row["attempts"])
            if retryable and attempts < _MAX_ATTEMPTS:
                delay = _RETRY_DELAYS_SECONDS[min(attempts - 1, len(_RETRY_DELAYS_SECONDS) - 1)]
                status = "retry"
                available_at = _after(timestamp, delay)
                finished_at = None
            else:
                status = "dead"
                available_at = timestamp
                finished_at = timestamp
            updated = conn.execute(
                """
                UPDATE learning_jobs
                SET status = ?, available_at = ?, last_error = ?,
                    lease_owner = NULL, lease_token = NULL, lease_until = NULL,
                    updated_at = ?, finished_at = ?
                WHERE id = ? AND status = 'running' AND lease_token = ?
                    AND COALESCE(lease_until, '') > ?
                """,
                (
                    status,
                    available_at,
                    safe_error,
                    timestamp,
                    finished_at,
                    int(job_id),
                    lease_token,
                    timestamp,
                ),
            ).rowcount
        return status if updated else None

    def require_active_claim(
        self,
        job_id: int,
        lease_token: str,
        *,
        now: str | datetime | None = None,
    ) -> None:
        timestamp = _now_iso(now)
        with self.ledger._connect() as conn:
            row = conn.execute(
                """
                SELECT 1 FROM learning_jobs
                WHERE id = ? AND status = 'running' AND lease_token = ?
                    AND COALESCE(lease_until, '') > ?
                """,
                (int(job_id), lease_token, timestamp),
            ).fetchone()
        if row is None:
            raise JobLeaseFencedError(f"Learning job lease was fenced: {job_id}")

    def renew_claim(
        self,
        job_id: int,
        lease_token: str,
        *,
        owner: str,
        worker_token: str,
        now: str | datetime | None = None,
        lease_seconds: int = _DEFAULT_LEASE_SECONDS,
    ) -> bool:
        """Atomically renew the global worker lease and its current job claim."""
        owner = require_safe_identity(owner, field="worker owner")
        with self.ledger._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            timestamp = _now_iso(now)
            lease_until = _after(timestamp, max(1, int(lease_seconds)))
            if not self._has_worker_lease(conn, owner, worker_token, timestamp):
                return False
            job = conn.execute(
                """
                SELECT 1 FROM learning_jobs
                WHERE id = ? AND status = 'running' AND lease_owner = ?
                    AND lease_token = ? AND COALESCE(lease_until, '') > ?
                """,
                (int(job_id), owner, lease_token, timestamp),
            ).fetchone()
            if job is None:
                return False
            worker_row = conn.execute("SELECT value FROM maintenance WHERE key = ?", (_WORKER_LEASE_KEY,)).fetchone()
            worker_value = _decode_json(worker_row["value"] if worker_row else "")
            worker_value["lease_until"] = lease_until
            conn.execute(
                "UPDATE maintenance SET value = ? WHERE key = ?",
                (_json_dumps(worker_value), _WORKER_LEASE_KEY),
            )
            updated = conn.execute(
                """
                UPDATE learning_jobs SET lease_until = ?, updated_at = ?
                WHERE id = ? AND status = 'running' AND lease_owner = ?
                    AND lease_token = ?
                """,
                (lease_until, timestamp, int(job_id), owner, lease_token),
            ).rowcount
        return bool(updated)

    def mark_dead(self, job_id: int, error: str, *, now: str | datetime | None = None) -> dict[str, Any]:
        timestamp = _now_iso(now)
        with self.ledger._connect() as conn:
            cursor = conn.execute(
                """
                UPDATE learning_jobs
                SET status = 'dead', last_error = ?, finished_at = ?, updated_at = ?,
                    lease_owner = NULL, lease_token = NULL, lease_until = NULL
                WHERE id = ? AND status != 'succeeded'
                """,
                (
                    sanitize_text_fragment(str(error), max_chars=4000),
                    timestamp,
                    timestamp,
                    int(job_id),
                ),
            )
        if not cursor.rowcount:
            raise ValueError(f"Learning job {job_id} is already succeeded or missing")
        return self.get_job(job_id)

    def retry_job(self, job_id: int, *, now: str | datetime | None = None) -> dict[str, Any]:
        timestamp = _now_iso(now)
        with self.ledger._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT status, last_error, payload_json FROM learning_jobs WHERE id = ?",
                (int(job_id),),
            ).fetchone()
            if row is not None and str(row["last_error"] or "") == LEGACY_SANITIZER_DEAD_ERROR:
                raise ValueError(
                    "Legacy sanitizer dead-letter jobs cannot be retried; "
                    "run an explicit fresh distillation for the migrated root"
                )
            payload = _decode_json(row["payload_json"] if row is not None else "")
            stage = _artifact_stage(payload)
            if stage is not None:
                # A manual retry of an artifact dead letter must preserve the
                # frozen semantic result and exact bytes. Only its independent
                # delivery budget is reset, so no model handler can run again.
                stage["attempts"] = 0
                payload[_ARTIFACT_DELIVERY_PAYLOAD_KEY] = stage
                cursor = conn.execute(
                    """
                    UPDATE learning_jobs
                    SET status = 'pending', payload_json = ?, available_at = ?,
                        lease_owner = NULL, lease_token = NULL, lease_until = NULL,
                        last_error = NULL, finished_at = NULL, updated_at = ?
                    WHERE id = ? AND status = 'dead'
                    """,
                    (_json_dumps(payload), timestamp, timestamp, int(job_id)),
                )
            else:
                cursor = conn.execute(
                    """
                    UPDATE learning_jobs
                    SET status = 'pending', attempts = 0, available_at = ?,
                        lease_owner = NULL, lease_token = NULL, lease_until = NULL,
                        result_json = NULL, last_error = NULL, finished_at = NULL,
                        updated_at = ?
                    WHERE id = ? AND status = 'dead'
                    """,
                    (timestamp, timestamp, int(job_id)),
                )
        if not cursor.rowcount:
            raise ValueError(f"Only dead jobs can be retried: {job_id}")
        return self.get_job(job_id)

    def note_artifact_error(self, job_id: int, error: str) -> None:
        """Record delivery failure without making the completed model job retry."""
        with self.ledger._connect() as conn:
            row = conn.execute(
                "SELECT result_json FROM learning_jobs WHERE id = ? AND status = 'succeeded'",
                (int(job_id),),
            ).fetchone()
            if row is None:
                return
            result = _decode_json(row["result_json"])
            if not isinstance(result, dict):
                result = {"value": result}
            result["artifact_error"] = sanitize_text_fragment(
                str(error),
                max_chars=1000,
            )
            conn.execute(
                "UPDATE learning_jobs SET result_json = ?, updated_at = ? WHERE id = ? AND status = 'succeeded'",
                (_json_dumps(result), _now_iso(), int(job_id)),
            )

    def record_review_fenced(
        self,
        job_id: int,
        lease_token: str,
        *,
        source_run_id: str,
        hook_event: str,
        application_id: str,
        output: dict[str, Any],
        status: str,
        now: str | datetime | None = None,
    ) -> int:
        """Insert the job's idempotent audit row only while its lease is live."""
        source_run_id = require_safe_identity(
            source_run_id,
            field="review source run id",
        )
        hook_event = require_safe_identity(hook_event, field="review hook event")
        application_id = require_safe_identity(
            application_id,
            field="review application id",
            allow_empty=True,
        )
        status = require_safe_identity(status, field="review status")
        with self.ledger._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            timestamp = _now_iso(now)
            claim = conn.execute(
                "SELECT 1 FROM learning_jobs WHERE id = ? AND status = 'running' "
                "AND lease_token = ? AND COALESCE(lease_until, '') > ?",
                (int(job_id), lease_token, timestamp),
            ).fetchone()
            if claim is None:
                raise JobLeaseFencedError(f"Learning job lease was fenced: {job_id}")
            cursor = conn.execute(
                """
                INSERT OR IGNORE INTO review_runs (
                    source_run_id, trigger_event_id, hook_event, application_id,
                    status, output_json, created_at, learning_job_id
                ) VALUES (?, '', ?, ?, ?, ?, ?, ?)
                """,
                (
                    source_run_id,
                    hook_event,
                    application_id,
                    status,
                    _json_dumps(sanitize_value_fragments(output)),
                    timestamp,
                    int(job_id),
                ),
            )
            if cursor.rowcount:
                review_id = int(cursor.lastrowid)
            else:
                row = conn.execute(
                    "SELECT review_id FROM review_runs WHERE learning_job_id = ?",
                    (int(job_id),),
                ).fetchone()
                if row is None:
                    raise RuntimeError("Review insert lost its idempotency row")
                review_id = int(row["review_id"])

            # Serialization and insertion happen while this transaction is
            # open. Recheck at its boundary so a worker whose lease expired
            # during either step cannot commit an audit row. Explicit ``now``
            # remains a frozen clock for deterministic tests.
            boundary_timestamp = _now_iso(now)
            claim = conn.execute(
                "SELECT 1 FROM learning_jobs WHERE id = ? AND status = 'running' "
                "AND lease_token = ? AND COALESCE(lease_until, '') > ?",
                (int(job_id), lease_token, boundary_timestamp),
            ).fetchone()
            if claim is None:
                raise JobLeaseFencedError(f"Learning job lease was fenced: {job_id}")
            return review_id

    def persist_payload_fields(
        self,
        job_id: int,
        lease_token: str,
        fields: dict[str, Any],
        *,
        now: str | datetime | None = None,
    ) -> dict[str, Any]:
        """Fenced write-once preparation data into a running job payload."""
        with self.ledger._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            timestamp = _now_iso(now)
            row = conn.execute(
                "SELECT payload_json FROM learning_jobs "
                "WHERE id = ? AND status = 'running' AND lease_token = ? "
                "AND COALESCE(lease_until, '') > ?",
                (int(job_id), lease_token, timestamp),
            ).fetchone()
            if row is None:
                raise JobLeaseFencedError(f"Learning job lease was fenced: {job_id}")
            payload = _decode_json(row["payload_json"])
            if not isinstance(payload, dict):
                payload = {}
            # Prepared input and the validated semantic plan are immutable once
            # written. A retry may resume effects, but can never ask a
            # non-deterministic provider to replace an already-frozen plan.
            for key, value in fields.items():
                safe_key = require_safe_identity(key, field="job payload field")
                if safe_key == "prepared_digest" and value is not None:
                    from .distiller import _load_prepared_digest, _prepared_payload

                    validated_digest = _load_prepared_digest(value)
                    if validated_digest is None:
                        raise ValueError("prepared digest failed integrity validation")
                    # Never pass the caller-owned mapping through to storage.
                    # The validator parses the exact schema, enforces safe
                    # identities, and returns a canonical digest representation.
                    canonical_value = _prepared_payload(validated_digest)
                elif safe_key == "semantic_plan":
                    from .distiller import load_semantic_plan

                    validated_plan = load_semantic_plan(
                        value,
                        prepared_digest=payload.get("prepared_digest"),
                        application_id=str(
                            payload.get("application_id") or "default"
                        ),
                    )
                    if validated_plan is None:
                        raise ValueError("semantic plan failed integrity validation")
                    canonical_value = validated_plan
                else:
                    # Sanitize the pair, not only the value: a sensitive key
                    # owns its complete value even when this late field is not
                    # one of the two frozen semantic structures.
                    canonical_pair = sanitize_value_fragments({safe_key: value})
                    if not isinstance(canonical_pair, dict) or set(
                        canonical_pair
                    ) != {safe_key}:
                        raise ValueError("job payload field failed safety validation")
                    canonical_value = canonical_pair[safe_key]
                if safe_key in {"prepared_digest", "semantic_plan"} and safe_key in payload:
                    if payload[safe_key] != canonical_value:
                        raise JobLeaseFencedError(
                            f"Learning job {safe_key} is already frozen: {job_id}"
                        )
                    continue
                payload[safe_key] = canonical_value
            # Validation/sanitization above can outlive the claim. Re-read the
            # wall clock at the transaction boundary; explicit ``now`` remains
            # deliberately frozen for deterministic tests.
            payload_json = _json_dumps(payload)
            boundary_timestamp = _now_iso(now)
            updated = conn.execute(
                "UPDATE learning_jobs SET payload_json = ?, updated_at = ? "
                "WHERE id = ? AND status = 'running' AND lease_token = ? "
                "AND COALESCE(lease_until, '') > ?",
                (
                    payload_json,
                    boundary_timestamp,
                    int(job_id),
                    lease_token,
                    boundary_timestamp,
                ),
            ).rowcount
            if not updated:
                raise JobLeaseFencedError(f"Learning job lease was fenced: {job_id}")
        return payload


class LearningJobWorker:
    """One globally leased worker with per-job fencing and bounded retries."""

    def __init__(
        self,
        queue: LearningJobQueue | None = None,
        *,
        handlers: dict[str, Callable[[dict[str, Any]], Any]] | None = None,
        owner: str = "",
    ):
        self.queue = queue or LearningJobQueue()
        self.handlers = handlers or {}
        self.owner = owner or f"{os.getpid()}-{uuid.uuid4().hex[:12]}"
        self._worker_token = ""

    def _handler(self, kind: str) -> Callable[[dict[str, Any]], Any]:
        if kind in self.handlers:
            return self.handlers[kind]
        if kind == "session_review":
            from .reviewer import process_session_review_job

            return lambda job: process_session_review_job(job, queue=self.queue)
        if kind == "retention":
            from .reviewer import process_retention_job

            return lambda job: process_retention_job(job, queue=self.queue)
        raise KeyError(f"Unsupported learning job kind: {kind}")

    def run_once(self, *, now: str | datetime | None = None) -> str | None:
        timestamp = _now_iso(now)
        token = self.queue.acquire_worker_lease(self.owner, now=timestamp)
        if not token:
            return None
        self._worker_token = token
        job = self.queue.claim(self.owner, token, now=timestamp)
        if job is None:
            return None
        if now is not None:
            # Deterministic unit/campaign clocks must flow into builtin lease
            # guards; production workers always use wall time.
            job["_clock_now"] = timestamp
        heartbeat_stop = Event()
        heartbeat_lost = Event()
        heartbeat: Thread | None = None
        if now is None:

            def _heartbeat() -> None:
                while not heartbeat_stop.wait(_HEARTBEAT_SECONDS):
                    if not self.queue.renew_claim(
                        int(job["id"]),
                        str(job["lease_token"]),
                        owner=self.owner,
                        worker_token=token,
                    ):
                        heartbeat_lost.set()
                        return

            heartbeat = Thread(
                target=_heartbeat,
                name=f"learning-job-heartbeat-{job['id']}",
                daemon=True,
            )
            heartbeat.start()
        try:
            staged_delivery = _artifact_stage(job.get("payload"))
            if staged_delivery is not None:
                # The semantic effect and exact artifact bytes are already
                # durable. Recovery is delivery-only: never resolve or invoke
                # the job's handler (which may call the model).
                result = job.get("result")
                if not isinstance(result, dict):
                    raise ValueError("staged artifact delivery has no semantic result")
            else:
                execution = self._handler(str(job["kind"]))(job)
                if isinstance(execution, JobExecution):
                    result = execution.result
                    staged_delivery = execution.artifact_delivery
                else:
                    result = execution if isinstance(execution, dict) else {"value": execution}

            if heartbeat_lost.is_set():
                return "fenced"
            self.queue.require_active_claim(
                int(job["id"]),
                str(job["lease_token"]),
                now=timestamp if now is not None else None,
            )

            if staged_delivery is None:
                if not self.queue.complete(
                    int(job["id"]),
                    str(job["lease_token"]),
                    result,
                    now=timestamp if now is not None else None,
                ):
                    return "fenced"
                return "succeeded"

            if _artifact_stage(job.get("payload")) is None:
                staged_delivery = self.queue.stage_artifact_delivery(
                    int(job["id"]),
                    str(job["lease_token"]),
                    result,
                    staged_delivery,
                    now=timestamp if now is not None else None,
                )
            staged_delivery = self.queue.begin_artifact_delivery(
                int(job["id"]),
                str(job["lease_token"]),
                now=timestamp if now is not None else None,
            )
            try:
                deliver_artifact_stage(
                    staged_delivery,
                    job_id=int(job["id"]),
                    kind=str(job["kind"]),
                    result=result,
                    state_root=self.queue.db_path.parent,
                    expected_root=(
                        Path(str(job["payload"]["run_dir"]))
                        if isinstance(job.get("payload"), dict)
                        and str(job["payload"].get("run_dir") or "").strip()
                        else None
                    ),
                )
                if not self.queue.complete_artifact_delivery(
                    int(job["id"]),
                    str(job["lease_token"]),
                    now=timestamp if now is not None else None,
                ):
                    return "fenced"
            except JobLeaseFencedError:
                return "fenced"
            except Exception as exc:
                logger.warning(
                    "Learning job artifact delivery failed: %s",
                    sanitize_text_fragment(str(exc), max_chars=1000),
                )
                return self.queue.fail_artifact_delivery(
                    int(job["id"]),
                    str(job["lease_token"]),
                    str(exc),
                    now=timestamp if now is not None else None,
                )
            return "succeeded"
        except JobLeaseFencedError:
            return "fenced"
        except Exception as exc:
            logger.warning(
                "Learning job %s failed: %s",
                job["id"],
                sanitize_text_fragment(str(exc), max_chars=1000),
            )
            return self.queue.fail(
                int(job["id"]),
                str(job["lease_token"]),
                str(exc),
                now=timestamp if now is not None else None,
                retryable=True,
            )
        finally:
            heartbeat_stop.set()
            if heartbeat is not None:
                heartbeat.join(timeout=1.0)

    def run_until_idle(
        self,
        *,
        max_jobs: int = 1000,
        wait_for_retries: bool = True,
        max_wait_seconds: float = 15.0,
    ) -> dict[str, int]:
        counts = {"succeeded": 0, "retry": 0, "dead": 0, "fenced": 0}
        idle_deadline = time.monotonic() + max(0.0, float(max_wait_seconds))
        attempted = 0
        processed = 0
        try:
            while processed < max(1, int(max_jobs)):
                outcome = self.run_once()
                if outcome is None:
                    if not wait_for_retries or time.monotonic() >= idle_deadline:
                        break
                    time.sleep(min(0.2, max(0.0, idle_deadline - time.monotonic())))
                    continue
                attempted += 1
                idle_deadline = time.monotonic() + max(0.0, float(max_wait_seconds))
                if outcome in counts:
                    counts[outcome] += 1
                if outcome in {"succeeded", "dead"}:
                    processed += 1
                if outcome == "fenced":
                    break
        finally:
            if self._worker_token:
                self.queue.release_worker_lease(self.owner, self._worker_token)
        counts["attempted"] = attempted
        counts["processed"] = processed
        return counts


def kick_learning_worker(db_path: str | Path | None = None) -> bool:
    """Best-effort detached worker spawn; the SQLite outbox is the guarantee."""
    queue = LearningJobQueue(db_path)
    kick_token = queue.claim_worker_kick_slot()
    if not kick_token:
        return False
    try:
        subprocess.Popen(
            [
                sys.executable,
                "-m",
                "src.extensions.self_learning.worker_entry",
                "--db",
                str(queue.db_path),
                "--kick-token",
                kick_token,
                "--max-wait",
                str(_DETACHED_MAX_WAIT_SECONDS),
            ],
            cwd=str(project_root()),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
            close_fds=True,
        )
        return True
    except Exception as exc:
        queue.release_worker_kick_slot(kick_token)
        logger.warning(
            "Could not start detached learning worker: %s",
            sanitize_text_fragment(str(exc), max_chars=1000),
        )
        return False
