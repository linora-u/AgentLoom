"""SQLite event ledger for AgentLoom self-learning state."""

from __future__ import annotations

import json
import re
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Any

from src.lib.logging import get_logger

from .event_schema import CanonicalSessionEvent
from .paths import self_learning_db
from .redaction import redact_text

logger = get_logger(__name__)


def _json_dumps(value: Any) -> str:
    return json.dumps(value if value is not None else {}, ensure_ascii=False, sort_keys=True, default=str)


def _now_iso() -> str:
    return datetime.now().astimezone().isoformat()


class SelfLearningLedger:
    """DB-first source of truth for sessions, memory, proposals, and reviews."""

    def __init__(self, db_path: str | Path | None = None):
        self.db_path = Path(db_path).resolve() if db_path else self_learning_db()
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.db_path))
        conn.row_factory = sqlite3.Row
        return conn

    def _init_db(self) -> None:
        with self._connect() as conn:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.executescript(_SCHEMA_SQL)
            try:
                conn.executescript(_FTS_SQL)
                conn.executescript(_FTS_TRIGRAM_SQL)
            except sqlite3.OperationalError as exc:
                logger.warning("Self-learning FTS unavailable: %s", exc)

    @staticmethod
    def _row_to_event_dict(row: sqlite3.Row) -> dict[str, Any]:
        result = dict(row)
        if "content_text" in result:
            result["content"] = redact_text(result.get("content_text") or "")
        for source_key, result_key in (
            ("input_json", "input_data"),
            ("output_json", "output_data"),
            ("metadata_json", "metadata"),
        ):
            raw = result.pop(source_key, None)
            if raw:
                try:
                    result[result_key] = json.loads(raw)
                except json.JSONDecodeError:
                    result[result_key] = {}
        return result

    @staticmethod
    def _run_metadata(event: CanonicalSessionEvent) -> dict[str, Any]:
        record = event.to_record()
        metadata = record.get("metadata") or {}
        content_payload = {}
        try:
            parsed = json.loads(record.get("content_text") or record.get("content") or "{}")
            if isinstance(parsed, dict):
                content_payload = parsed
        except Exception:
            content_payload = {}

        status = "indexed"
        if record.get("event_type") in {"run_failed", "task_failed"}:
            status = "failed"
        elif record.get("event_type") in {"run_completed", "task_completed"}:
            status = "completed"

        return {
            "run_id": record["run_id"],
            "task_id": record.get("task_id") or "",
            "agent_name": record.get("agent_name") or "",
            "application_id": record.get("application_id") or str(metadata.get("application_id") or metadata.get("app") or ""),
            "application_name": record.get("application_name") or str(metadata.get("application_name") or metadata.get("app") or ""),
            "application_path": record.get("application_path") or str(metadata.get("application_path") or ""),
            "workflow_path": record.get("workflow_path") or str(metadata.get("workflow_path") or metadata.get("yaml_path") or ""),
            "yaml_path": record.get("workflow_path") or str(metadata.get("yaml_path") or ""),
            "run_dir": str(metadata.get("run_dir") or record.get("source_path") or ""),
            "status": status,
            "started_at": record.get("created_at") or "",
            "ended_at": record.get("created_at") or "",
            "task_text": redact_text(content_payload.get("task_text") or content_payload.get("task") or ""),
            "final_answer": redact_text(content_payload.get("result") or content_payload.get("final_answer") or ""),
            "indexed_at": _now_iso(),
            "metadata_json": _json_dumps(metadata),
        }

    def append_event(self, event: CanonicalSessionEvent) -> dict[str, Any]:
        """Append one redacted canonical event to the ledger."""
        record = event.to_record()
        run_meta = self._run_metadata(event)
        with self._connect() as conn:
            existing = conn.execute("SELECT * FROM runs WHERE run_id = ?", (record["run_id"],)).fetchone()
            if existing:
                merged = dict(existing)
                for key in (
                    "task_id",
                    "agent_name",
                    "application_id",
                    "application_name",
                    "application_path",
                    "workflow_path",
                    "yaml_path",
                    "run_dir",
                    "task_text",
                ):
                    merged[key] = merged.get(key) or run_meta[key]
                merged["ended_at"] = run_meta["ended_at"] or merged.get("ended_at")
                merged["final_answer"] = run_meta["final_answer"] or merged.get("final_answer")
                if merged.get("status") == "failed" or run_meta["status"] == "failed":
                    merged["status"] = "failed"
                elif run_meta["status"] != "indexed":
                    merged["status"] = run_meta["status"]
                merged["indexed_at"] = run_meta["indexed_at"]
                merged["metadata_json"] = run_meta["metadata_json"] or merged.get("metadata_json")
                conn.execute(
                    """
                    UPDATE runs SET task_id=:task_id, agent_name=:agent_name,
                        application_id=:application_id, application_name=:application_name,
                        application_path=:application_path, workflow_path=:workflow_path,
                        yaml_path=:yaml_path, run_dir=:run_dir, status=:status,
                        started_at=:started_at, ended_at=:ended_at, task_text=:task_text,
                        final_answer=:final_answer, indexed_at=:indexed_at,
                        metadata_json=:metadata_json
                    WHERE run_id=:run_id
                    """,
                    merged,
                )
            else:
                conn.execute(
                    """
                    INSERT INTO runs (
                        run_id, task_id, agent_name, application_id, application_name,
                        application_path, workflow_path, yaml_path, run_dir, status,
                        started_at, ended_at, task_text, final_answer, indexed_at, metadata_json
                    ) VALUES (
                        :run_id, :task_id, :agent_name, :application_id, :application_name,
                        :application_path, :workflow_path, :yaml_path, :run_dir, :status,
                        :started_at, :ended_at, :task_text, :final_answer, :indexed_at, :metadata_json
                    )
                    """,
                    run_meta,
                )

            ordinal = conn.execute(
                "SELECT COALESCE(MAX(ordinal), -1) + 1 FROM events WHERE run_id = ?",
                (record["run_id"],),
            ).fetchone()[0]
            cursor = conn.execute(
                """
                INSERT OR IGNORE INTO events (
                    event_id, run_id, task_id, parent_task_id, parent_event_id,
                    application_id, application_name, application_path, workflow_path,
                    agent_name, worker_name, tool_name, event_type, phase, source, role,
                    status, step_number, input_json, output_json, content_text, content_ref,
                    source_path, created_at, ordinal, metadata_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    record["event_id"],
                    record["run_id"],
                    record.get("task_id") or "",
                    record.get("parent_task_id") or "",
                    record.get("parent_event_id") or "",
                    record.get("application_id") or run_meta["application_id"],
                    record.get("application_name") or run_meta["application_name"],
                    record.get("application_path") or run_meta["application_path"],
                    record.get("workflow_path") or run_meta["workflow_path"],
                    record.get("agent_name") or "",
                    record.get("worker_name") or "",
                    record.get("tool_name") or "",
                    record.get("event_type") or "",
                    record.get("phase") or "",
                    record.get("source") or "",
                    record.get("role") or "",
                    record.get("status") or "",
                    record.get("step_number"),
                    _json_dumps(record.get("input_data") or {}),
                    _json_dumps(record.get("output_data") or {}),
                    record.get("content_text") or record.get("content") or "",
                    record.get("content_ref") or "",
                    record.get("source_path") or "",
                    record.get("created_at") or "",
                    int(ordinal),
                    _json_dumps(record.get("metadata") or {}),
                ),
            )
            row_id = int(cursor.lastrowid) if cursor.rowcount else None
        return {
            "run_id": record["run_id"],
            "event_id": record["event_id"],
            "indexed": row_id is not None,
            "db_path": str(self.db_path),
        }

    @staticmethod
    def _fts_query(query: str) -> str:
        tokens = re.findall(r"[\w./:-]+", query, flags=re.UNICODE)
        if not tokens:
            return ""
        return " AND ".join(f'"{token.replace(chr(34), chr(34) + chr(34))}"' for token in tokens[:12])

    @staticmethod
    def _contains_cjk(text: str) -> bool:
        return any(
            0x3400 <= ord(ch) <= 0x9FFF
            or 0x3040 <= ord(ch) <= 0x30FF
            or 0xAC00 <= ord(ch) <= 0xD7AF
            for ch in text
        )

    def search_events(
        self,
        query: str,
        *,
        limit: int = 10,
        agent: str | None = None,
        app: str | None = None,
        since: str | None = None,
        exclude_run_id: str | None = None,
        scope: str = "all",
    ) -> list[dict[str, Any]]:
        limit = max(1, min(int(limit or 10), 50))
        params: list[Any] = []
        filters: list[str] = []
        if exclude_run_id:
            filters.append("e.run_id != ?")
            params.append(exclude_run_id)
        if agent:
            filters.append("e.agent_name = ?")
            params.append(agent)
        if app:
            filters.append("(e.application_id = ? OR r.application_id = ? OR r.application_name = ?)")
            params.extend([app, app, app])
        if since:
            filters.append("(e.created_at >= ? OR r.started_at >= ?)")
            params.extend([since, since])
        if scope == "current_app" and app:
            filters.append("(e.application_id = ? OR r.application_id = ?)")
            params.extend([app, app])
        where = (" AND " + " AND ".join(filters)) if filters else ""

        with self._connect() as conn:
            fts_query = self._fts_query(query)
            if fts_query:
                fts_table = "events_fts_trigram" if self._contains_cjk(query) else "events_fts"
                sql = f"""
                    SELECT e.*, r.status AS run_status, r.workflow_path AS run_workflow_path,
                        r.run_dir, r.application_id AS run_application_id,
                        r.application_name AS run_application_name
                    FROM {fts_table} f
                    JOIN events e ON e.id = f.rowid
                    LEFT JOIN runs r ON r.run_id = e.run_id
                    WHERE {fts_table} MATCH ? {where}
                    ORDER BY bm25({fts_table}), e.id DESC
                    LIMIT ?
                """
                try:
                    rows = conn.execute(sql, [fts_query, *params, limit]).fetchall()
                    return [self._row_to_event_dict(row) for row in rows]
                except sqlite3.OperationalError:
                    logger.debug("FTS query failed, falling back to LIKE", exc_info=True)

            like = f"%{query}%"
            sql = f"""
                SELECT e.*, r.status AS run_status, r.workflow_path AS run_workflow_path,
                    r.run_dir, r.application_id AS run_application_id,
                    r.application_name AS run_application_name
                FROM events e
                LEFT JOIN runs r ON r.run_id = e.run_id
                WHERE e.content_text LIKE ? {where}
                ORDER BY e.id DESC
                LIMIT ?
            """
            rows = conn.execute(sql, [like, *params, limit]).fetchall()
            return [self._row_to_event_dict(row) for row in rows]

    def scroll_events(self, run_id: str, event_id: int, *, direction: str = "after", window: int = 5) -> list[dict[str, Any]]:
        window = max(1, min(int(window or 5), 100))
        direction = (direction or "after").strip().lower()
        comparator = ">" if direction == "after" else "<"
        order = "ASC" if direction == "after" else "DESC"
        with self._connect() as conn:
            rows = conn.execute(
                f"""
                SELECT * FROM events
                WHERE run_id = ? AND id {comparator} ?
                ORDER BY id {order}
                LIMIT ?
                """,
                (run_id, int(event_id), window),
            ).fetchall()
        values = [self._row_to_event_dict(row) for row in rows]
        if direction != "after":
            values.reverse()
        return values

    def count_events(self) -> dict[str, Any]:
        with self._connect() as conn:
            runs = conn.execute("SELECT COUNT(*) FROM runs").fetchone()[0]
            events = conn.execute("SELECT COUNT(*) FROM events").fetchone()[0]
        return {"runs_indexed": int(runs), "events_indexed": int(events), "db_path": str(self.db_path)}

    def delete_run(self, run_id: str) -> None:
        with self._connect() as conn:
            conn.execute("DELETE FROM events WHERE run_id = ?", (run_id,))
            conn.execute("DELETE FROM runs WHERE run_id = ?", (run_id,))

    def record_review(
        self,
        *,
        source_run_id: str,
        hook_event: str,
        application_id: str = "",
        trigger_event_id: str = "",
        output: dict[str, Any] | None = None,
        status: str = "proposal",
    ) -> int:
        with self._connect() as conn:
            cursor = conn.execute(
                """
                INSERT INTO review_runs (
                    source_run_id, trigger_event_id, hook_event, application_id,
                    status, output_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    source_run_id,
                    trigger_event_id,
                    hook_event,
                    application_id,
                    status,
                    _json_dumps(output or {}),
                    _now_iso(),
                ),
            )
            return int(cursor.lastrowid)

    def upsert_skill_proposal(
        self,
        *,
        proposal_id: str,
        name: str,
        action: str,
        status: str,
        proposal_path: str,
        application_id: str = "",
        source_run_id: str = "",
        source_event_id: str = "",
        manifest: dict[str, Any] | None = None,
    ) -> None:
        now = _now_iso()
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO skill_proposals (
                    proposal_id, name, action, status, proposal_path, application_id,
                    source_run_id, source_event_id, manifest_json, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(proposal_id) DO UPDATE SET
                    name=excluded.name,
                    action=excluded.action,
                    status=excluded.status,
                    proposal_path=excluded.proposal_path,
                    application_id=excluded.application_id,
                    source_run_id=excluded.source_run_id,
                    source_event_id=excluded.source_event_id,
                    manifest_json=excluded.manifest_json,
                    updated_at=excluded.updated_at
                """,
                (
                    proposal_id,
                    name,
                    action,
                    status,
                    proposal_path,
                    application_id,
                    source_run_id,
                    source_event_id,
                    _json_dumps(manifest or {}),
                    now,
                    now,
                ),
            )

    def update_skill_proposal_status(self, proposal_id: str, status: str) -> None:
        column = "promoted_at" if status == "promoted" else "archived_at" if status == "archived" else ""
        now = _now_iso()
        with self._connect() as conn:
            if column:
                conn.execute(
                    f"UPDATE skill_proposals SET status = ?, updated_at = ?, {column} = ? WHERE proposal_id = ?",
                    (status, now, now, proposal_id),
                )
            else:
                conn.execute(
                    "UPDATE skill_proposals SET status = ?, updated_at = ? WHERE proposal_id = ?",
                    (status, now, proposal_id),
                )


_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS schema_version (
    version INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS runs (
    run_id TEXT PRIMARY KEY,
    task_id TEXT,
    agent_name TEXT,
    application_id TEXT,
    application_name TEXT,
    application_path TEXT,
    workflow_path TEXT,
    yaml_path TEXT,
    run_dir TEXT,
    status TEXT,
    started_at TEXT,
    ended_at TEXT,
    task_text TEXT,
    final_answer TEXT,
    indexed_at TEXT NOT NULL,
    metadata_json TEXT
);

CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id TEXT NOT NULL UNIQUE,
    run_id TEXT NOT NULL,
    task_id TEXT,
    parent_task_id TEXT,
    parent_event_id TEXT,
    application_id TEXT,
    application_name TEXT,
    application_path TEXT,
    workflow_path TEXT,
    agent_name TEXT,
    worker_name TEXT,
    tool_name TEXT,
    event_type TEXT,
    phase TEXT,
    source TEXT,
    role TEXT,
    status TEXT,
    step_number INTEGER,
    input_json TEXT,
    output_json TEXT,
    content_text TEXT NOT NULL,
    content_ref TEXT,
    source_path TEXT,
    created_at TEXT,
    ordinal INTEGER NOT NULL DEFAULT 0,
    metadata_json TEXT
);

CREATE TABLE IF NOT EXISTS memory_items (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    scope_type TEXT NOT NULL,
    scope_id TEXT NOT NULL DEFAULT '',
    content TEXT NOT NULL,
    status TEXT NOT NULL,
    action TEXT NOT NULL,
    target TEXT,
    source TEXT,
    source_run_id TEXT,
    source_event_id TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    applied_at TEXT
);

CREATE TABLE IF NOT EXISTS skill_proposals (
    proposal_id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    action TEXT NOT NULL,
    status TEXT NOT NULL,
    proposal_path TEXT NOT NULL,
    application_id TEXT,
    source_run_id TEXT,
    source_event_id TEXT,
    manifest_json TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    promoted_at TEXT,
    archived_at TEXT
);

CREATE TABLE IF NOT EXISTS review_runs (
    review_id INTEGER PRIMARY KEY AUTOINCREMENT,
    source_run_id TEXT NOT NULL,
    trigger_event_id TEXT,
    hook_event TEXT,
    application_id TEXT,
    status TEXT,
    output_json TEXT,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS artifacts (
    artifact_id INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id TEXT,
    run_id TEXT,
    kind TEXT,
    uri TEXT,
    sha256 TEXT,
    metadata_json TEXT,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_runs_application ON runs(application_id);
CREATE INDEX IF NOT EXISTS idx_runs_started ON runs(started_at);
CREATE INDEX IF NOT EXISTS idx_events_run_id ON events(run_id);
CREATE INDEX IF NOT EXISTS idx_events_task_id ON events(task_id);
CREATE INDEX IF NOT EXISTS idx_events_agent ON events(agent_name);
CREATE INDEX IF NOT EXISTS idx_events_application ON events(application_id);
CREATE INDEX IF NOT EXISTS idx_events_created ON events(created_at);
CREATE INDEX IF NOT EXISTS idx_memory_scope ON memory_items(scope_type, scope_id);
CREATE INDEX IF NOT EXISTS idx_memory_status ON memory_items(status);
CREATE INDEX IF NOT EXISTS idx_skill_proposals_status ON skill_proposals(status);
CREATE INDEX IF NOT EXISTS idx_review_runs_source ON review_runs(source_run_id);
"""

_FTS_SQL = """
CREATE VIRTUAL TABLE IF NOT EXISTS events_fts USING fts5(
    content_text,
    tool_name,
    agent_name,
    worker_name,
    event_type,
    source,
    role,
    status,
    application_id UNINDEXED,
    run_id UNINDEXED
);

CREATE TRIGGER IF NOT EXISTS events_fts_insert AFTER INSERT ON events BEGIN
    INSERT INTO events_fts(
        rowid, content_text, tool_name, agent_name, worker_name,
        event_type, source, role, status, application_id, run_id
    ) VALUES (
        new.id,
        COALESCE(new.content_text, '') || ' ' || COALESCE(new.tool_name, '') || ' ' || COALESCE(new.input_json, '') || ' ' || COALESCE(new.output_json, ''),
        COALESCE(new.tool_name, ''),
        COALESCE(new.agent_name, ''),
        COALESCE(new.worker_name, ''),
        COALESCE(new.event_type, ''),
        COALESCE(new.source, ''),
        COALESCE(new.role, ''),
        COALESCE(new.status, ''),
        COALESCE(new.application_id, ''),
        COALESCE(new.run_id, '')
    );
END;

CREATE TRIGGER IF NOT EXISTS events_fts_delete AFTER DELETE ON events BEGIN
    DELETE FROM events_fts WHERE rowid = old.id;
END;

CREATE TRIGGER IF NOT EXISTS events_fts_update AFTER UPDATE ON events BEGIN
    DELETE FROM events_fts WHERE rowid = old.id;
    INSERT INTO events_fts(
        rowid, content_text, tool_name, agent_name, worker_name,
        event_type, source, role, status, application_id, run_id
    ) VALUES (
        new.id,
        COALESCE(new.content_text, '') || ' ' || COALESCE(new.tool_name, '') || ' ' || COALESCE(new.input_json, '') || ' ' || COALESCE(new.output_json, ''),
        COALESCE(new.tool_name, ''),
        COALESCE(new.agent_name, ''),
        COALESCE(new.worker_name, ''),
        COALESCE(new.event_type, ''),
        COALESCE(new.source, ''),
        COALESCE(new.role, ''),
        COALESCE(new.status, ''),
        COALESCE(new.application_id, ''),
        COALESCE(new.run_id, '')
    );
END;
"""

_FTS_TRIGRAM_SQL = """
CREATE VIRTUAL TABLE IF NOT EXISTS events_fts_trigram USING fts5(
    content_text,
    tokenize='trigram'
);

CREATE TRIGGER IF NOT EXISTS events_fts_trigram_insert AFTER INSERT ON events BEGIN
    INSERT INTO events_fts_trigram(rowid, content_text) VALUES (
        new.id,
        COALESCE(new.content_text, '') || ' ' || COALESCE(new.tool_name, '') || ' ' || COALESCE(new.input_json, '') || ' ' || COALESCE(new.output_json, '')
    );
END;

CREATE TRIGGER IF NOT EXISTS events_fts_trigram_delete AFTER DELETE ON events BEGIN
    DELETE FROM events_fts_trigram WHERE rowid = old.id;
END;

CREATE TRIGGER IF NOT EXISTS events_fts_trigram_update AFTER UPDATE ON events BEGIN
    DELETE FROM events_fts_trigram WHERE rowid = old.id;
    INSERT INTO events_fts_trigram(rowid, content_text) VALUES (
        new.id,
        COALESCE(new.content_text, '') || ' ' || COALESCE(new.tool_name, '') || ' ' || COALESCE(new.input_json, '') || ' ' || COALESCE(new.output_json, '')
    );
END;
"""
