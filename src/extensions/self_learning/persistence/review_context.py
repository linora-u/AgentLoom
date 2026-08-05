"""Persisted read model for bounded self-learning review context."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

from ..application_scope import safe_application_id
from ..redaction import sanitize_text_fragment
from .database import SelfLearningDatabase
from .ledger import SelfLearningLedger


def _table_exists(conn: sqlite3.Connection, name: str) -> bool:
    return (
        conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
            (name,),
        ).fetchone()
        is not None
    )


class ReviewContextStore:
    """Own the cross-table read projection consumed by review orchestration."""

    def __init__(self, db_path: str | Path) -> None:
        self.db_path = Path(db_path).expanduser().resolve()
        self._database = SelfLearningDatabase(self.db_path)

    def _connect(self) -> sqlite3.Connection:
        return self._database.connect(foreign_keys=True)

    def unreviewed_application_ids(self) -> list[str]:
        """Return Applications with completed roots not consumed by review."""
        if not self.db_path.exists():
            return []
        try:
            with self._connect() as conn:
                if not _table_exists(conn, "runs"):
                    return []
                reviewed = _table_exists(
                    conn, "review_batch_runs"
                ) and _table_exists(conn, "review_batches")
                exclusion = ""
                if reviewed:
                    exclusion = """
                        AND NOT EXISTS (
                            SELECT 1
                            FROM review_batch_runs rbr
                            JOIN review_batches rb ON rb.review_id=rbr.review_id
                            WHERE rbr.root_run_id=COALESCE(NULLIF(r.root_run_id,''),r.run_id)
                              AND rb.scope_type='application'
                              AND rb.scope_id=r.application_id
                              AND rb.status='completed'
                        )
                    """
                rows = conn.execute(
                    f"""
                    SELECT DISTINCT application_id FROM runs r
                    WHERE status='completed'
                      AND COALESCE(NULLIF(root_run_id,''),run_id)=run_id
                      AND application_id != ''
                      {exclusion}
                    ORDER BY application_id
                    """
                ).fetchall()
        except sqlite3.Error:
            return []
        return [
            safe_application_id(str(row[0]))
            for row in rows
            if str(row[0] or "")
        ]

    def collect_application(self, application_id: str) -> dict[str, Any]:
        roots = self._unreviewed_roots(application_id)
        if not roots:
            return {"source_runs": [], "allowed_provenance": [], "context": []}
        ledger = SelfLearningLedger(self.db_path)
        context: list[dict[str, Any]] = []
        allowed: list[dict[str, Any]] = []
        for root_run_id, _app_id in roots:
            completed = ledger.completed_review_context(
                root_run_id,
                tool_result_limit=100,
            )
            if completed is None:
                continue
            for row in completed.get("tool_results") or ():
                if not isinstance(row, dict):
                    continue
                evidence = [
                    entry
                    for entry in row.get("trusted_evidence") or ()
                    if isinstance(entry, dict)
                    and entry.get("scope_type") == "application"
                    and entry.get("scope_id") == application_id
                ]
                provenance = {
                    "root_run_id": root_run_id,
                    "application_id": application_id,
                    "event_id": str(row.get("event_id") or ""),
                    "tool_call_id": str(row.get("tool_call_id") or ""),
                }
                provenance = {
                    key: value for key, value in provenance.items() if value
                }
                if provenance.get("event_id"):
                    allowed.append(provenance)
                context.append(
                    {
                        "provenance": provenance,
                        "tool_name": str(row.get("tool_name") or ""),
                        "status": str(row.get("status") or "completed"),
                        "trusted_evidence": evidence,
                        "observed_result": sanitize_text_fragment(
                            str(row.get("output_json") or ""),
                            max_chars=4_000,
                        ),
                    }
                )
        return {
            "source_runs": [
                {"root_run_id": root_run_id, "application_id": app_id}
                for root_run_id, app_id in roots
            ],
            "allowed_provenance": allowed,
            "context": context,
        }

    def _unreviewed_roots(self, application_id: str) -> list[tuple[str, str]]:
        if not self.db_path.exists():
            return []
        try:
            with self._connect() as conn:
                if not _table_exists(conn, "runs"):
                    return []
                reviewed = _table_exists(
                    conn, "review_batch_runs"
                ) and _table_exists(conn, "review_batches")
                exclusion = ""
                if reviewed:
                    exclusion = """
                        AND NOT EXISTS (
                            SELECT 1
                            FROM review_batch_runs rbr
                            JOIN review_batches rb ON rb.review_id=rbr.review_id
                            WHERE rbr.root_run_id=COALESCE(NULLIF(r.root_run_id,''),r.run_id)
                              AND rb.scope_type='application'
                              AND rb.scope_id=?
                              AND rb.status='completed'
                        )
                    """
                    params: tuple[Any, ...] = (application_id, application_id)
                else:
                    params = (application_id,)
                rows = conn.execute(
                    f"""
                    SELECT run_id, application_id FROM runs r
                    WHERE status='completed'
                      AND COALESCE(NULLIF(root_run_id,''),run_id)=run_id
                      AND application_id=?
                      {exclusion}
                    ORDER BY ended_at, run_id
                    """,
                    params,
                ).fetchall()
        except sqlite3.Error:
            return []
        return [
            (str(row["run_id"]), str(row["application_id"])) for row in rows
        ]

    def collect_project(self) -> dict[str, Any]:
        source_runs: list[dict[str, str]] = []
        allowed: list[dict[str, Any]] = []
        context: list[dict[str, Any]] = []
        if not self.db_path.exists():
            return {
                "source_runs": source_runs,
                "allowed_provenance": allowed,
                "context": context,
            }
        try:
            with self._connect() as conn:
                if _table_exists(conn, "trusted_review_evidence") and _table_exists(
                    conn, "events"
                ):
                    columns = {
                        str(row[1])
                        for row in conn.execute("PRAGMA table_info(events)")
                    }
                    tool_call_expr = (
                        "e.tool_call_id" if "tool_call_id" in columns else "''"
                    )
                    rows = conn.execute(
                        f"""
                        SELECT tre.root_run_id, e.application_id, tre.event_id,
                               {tool_call_expr} AS tool_call_id, tre.text, tre.source
                        FROM trusted_review_evidence tre
                        JOIN events e ON e.event_id=tre.event_id
                        WHERE tre.scope_type='project'
                          AND e.status='completed'
                          AND NOT EXISTS (
                              SELECT 1
                              FROM review_batch_runs rbr
                              JOIN review_batches rb ON rb.review_id=rbr.review_id
                              WHERE rbr.root_run_id=tre.root_run_id
                                AND rb.scope_type='project'
                                AND rb.scope_id='project'
                                AND rb.status='completed'
                          )
                        ORDER BY tre.root_run_id, tre.event_id, tre.text
                        """
                    ).fetchall()
                    for row in rows:
                        provenance = {
                            "root_run_id": str(row["root_run_id"] or ""),
                            "application_id": str(row["application_id"] or ""),
                            "event_id": str(row["event_id"] or ""),
                            "tool_call_id": str(row["tool_call_id"] or ""),
                        }
                        provenance = {
                            key: value
                            for key, value in provenance.items()
                            if value
                        }
                        allowed.append(provenance)
                        source_runs.append(
                            {
                                "root_run_id": provenance.get("root_run_id", ""),
                                "application_id": provenance.get(
                                    "application_id", ""
                                ),
                            }
                        )
                        context.append(
                            {
                                "kind": "code_marked_project_fact",
                                "text": str(row["text"] or ""),
                                "source": str(row["source"] or ""),
                                "provenance": provenance,
                            }
                        )
                if _table_exists(conn, "memory_items"):
                    memory_columns = {
                        str(row[1])
                        for row in conn.execute("PRAGMA table_info(memory_items)")
                    }
                    required = {
                        "kind",
                        "memory_key",
                        "payload_json",
                        "payload_hash",
                        "state",
                    }
                    if required <= memory_columns:
                        groups = conn.execute(
                            """
                            SELECT kind,memory_key,payload_json,payload_hash,
                                   GROUP_CONCAT(DISTINCT scope_id) AS applications,
                                   COUNT(DISTINCT scope_id) AS application_count
                            FROM memory_items
                            WHERE scope_type='application'
                              AND state IN ('active_confirmed','active_unreviewed')
                            GROUP BY kind,memory_key,payload_hash
                            HAVING COUNT(DISTINCT scope_id) >= 2
                            ORDER BY kind,memory_key,payload_hash
                            """
                        ).fetchall()
                        for group in groups:
                            already_proposed = conn.execute(
                                """
                                SELECT 1 FROM review_candidates
                                WHERE scope_type='project'
                                  AND scope_id='project'
                                  AND kind=?
                                  AND memory_key=?
                                  AND payload_hash=?
                                  AND state IN (
                                      'pending_pre_review','active_unreviewed',
                                      'active_confirmed','quarantined'
                                  )
                                LIMIT 1
                                """,
                                (
                                    str(group["kind"]),
                                    str(group["memory_key"]),
                                    str(group["payload_hash"]),
                                ),
                            ).fetchone()
                            if already_proposed is not None:
                                continue
                            try:
                                payload = json.loads(str(group["payload_json"]))
                            except (
                                TypeError,
                                ValueError,
                                json.JSONDecodeError,
                            ):
                                continue
                            context.append(
                                {
                                    "kind": "cross_application_corroboration",
                                    "memory_kind": str(group["kind"]),
                                    "memory_key": str(group["memory_key"]),
                                    "payload": payload,
                                    "applications": sorted(
                                        item
                                        for item in str(
                                            group["applications"] or ""
                                        ).split(",")
                                        if item
                                    ),
                                }
                            )
        except sqlite3.Error:
            pass
        deduped_runs = list(
            {
                (
                    item.get("root_run_id", ""),
                    item.get("application_id", ""),
                ): item
                for item in source_runs
                if item.get("root_run_id")
            }.values()
        )
        return {
            "source_runs": deduped_runs,
            "allowed_provenance": allowed,
            "context": context,
        }


__all__ = ["ReviewContextStore"]
