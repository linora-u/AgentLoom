"""SQLite event ledger for AgentLoom self-learning state."""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from src.lib.logging import get_logger

from .event_schema import (
    CanonicalSessionEvent,
    require_optional_strict_int,
    safe_run_id,
)
from .paths import self_learning_db
from .redaction import (
    redact_text,
    require_safe_identity,
    sanitize_text_fragment,
    sanitize_value_fragments,
)

logger = get_logger(__name__)

_SCHEMA_VERSION = 4
_BUSY_TIMEOUT_MS = 5000
_V4_PHYSICAL_CLEANUP_KEY = "schema_v4_physical_cleanup"
_V4_CLEANUP_PENDING = "pending"
_V4_CLEANUP_COMPLETE = "complete"
_V4_SANITIZER_REVISION_KEY = "schema_v4_sanitizer_revision"
_V4_SANITIZER_REVISION = "4"
_FTS_TRIGGER_NAMES = (
    "events_fts_insert",
    "events_fts_delete",
    "events_fts_update",
    "events_fts_trigram_insert",
    "events_fts_trigram_delete",
    "events_fts_trigram_update",
)
LEGACY_SANITIZER_DEAD_ERROR = (
    "legacy_v4_identity_sanitizer_changed_frozen_job_input"
)


def memory_content_hash(content: str) -> str:
    """Stable dedup hash: whitespace-collapsed, case-folded content."""
    normalized = " ".join(str(content).split()).casefold()
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def _json_dumps(value: Any) -> str:
    return json.dumps(value if value is not None else {}, ensure_ascii=False, sort_keys=True, default=str)


def _now_iso() -> str:
    return datetime.now().astimezone().isoformat()


class SelfLearningLedger:
    """DB-first source of truth for sessions, memory, proposals, and reviews."""

    # DDL + migrations run once per (process, db_path); the recorder constructs
    # a ledger on every hook event, so re-running schema bootstrap there would
    # put schema churn on the synchronous tool path.
    _initialized_paths: set[str] = set()
    _init_lock = threading.Lock()

    def __init__(self, db_path: str | Path | None = None):
        self.db_path = Path(db_path).resolve() if db_path else self_learning_db()
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        key = str(self.db_path)
        if key not in self._initialized_paths:
            with self._init_lock:
                if key not in self._initialized_paths:
                    self._init_db()
                    self._initialized_paths.add(key)

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.db_path), timeout=_BUSY_TIMEOUT_MS / 1000)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_db(self) -> None:
        with self._connect() as conn:
            self._enable_wal_mode(conn)
            self._run_migrations(conn)

    @staticmethod
    def _execute_script_in_transaction(
        conn: sqlite3.Connection,
        script: str,
    ) -> None:
        """Execute a SQL script without ``executescript``'s implicit commit."""
        statement_lines: list[str] = []
        for line in script.splitlines(keepends=True):
            statement_lines.append(line)
            statement = "".join(statement_lines)
            if not sqlite3.complete_statement(statement):
                continue
            if statement.strip():
                conn.execute(statement)
            statement_lines.clear()
        remainder = "".join(statement_lines)
        if remainder.strip():
            raise sqlite3.OperationalError("incomplete SQL statement in schema script")

    @staticmethod
    def _enable_wal_mode(
        conn: sqlite3.Connection,
        *,
        timeout_seconds: float = _BUSY_TIMEOUT_MS / 1000,
        monotonic_fn: Any = None,
        sleep_fn: Any = None,
    ) -> None:
        """Switch to WAL, retrying only SQLite's transient lock error."""
        monotonic = monotonic_fn or time.monotonic
        sleep = sleep_fn or time.sleep
        deadline = monotonic() + max(0.0, float(timeout_seconds))
        while True:
            try:
                conn.execute("PRAGMA journal_mode=WAL")
                return
            except sqlite3.OperationalError as exc:
                if "locked" not in str(exc).casefold() or monotonic() >= deadline:
                    raise
                sleep(0.01)

    def _run_migrations(self, conn: sqlite3.Connection) -> None:
        # Schema upgrades are process-safe as well as thread-safe.  In
        # particular, two short-lived ``loom run`` processes may discover the
        # same old database concurrently; only one is allowed to inspect and
        # mutate its shape at a time.
        # Secure deletion must be enabled before legacy rows/FTS segments are
        # rewritten.  The post-commit truncate below then removes superseded
        # WAL frames instead of leaving raw historical credentials on disk.
        conn.execute("PRAGMA secure_delete=ON")
        # SQLite WAL frames contain complete *new* page images.  With cache
        # spill enabled, a long migration can flush a page after only one of
        # its rows/columns was cleaned, transiently copying neighbouring
        # legacy secrets into WAL. Hold every dirty page until the transaction
        # reaches its fully sanitized final state.
        conn.execute("PRAGMA cache_spill=OFF")
        conn.execute("BEGIN IMMEDIATE")
        migrated_v4 = False
        refreshed_v4_sanitizer = False
        requires_v4_physical_cleanup = False
        try:
            # ``sqlite3.Connection.executescript`` commits an open transaction
            # before executing. Run every bootstrap/repair statement through
            # ``execute`` so fresh-schema DDL, FTS objects, and versioned data
            # migration are all fenced by the same process-wide write lock.
            self._execute_script_in_transaction(conn, _SCHEMA_SQL)
            available_fts_scripts: list[str] = []
            existing_schema_objects = {
                str(row[0])
                for row in conn.execute("SELECT name FROM sqlite_master")
            }
            for group_index, (fts_script, fts_label, fts_table) in enumerate(
                (
                    (_FTS_SQL, "fts5", "events_fts"),
                    (_FTS_TRIGRAM_SQL, "fts5 trigram", "events_fts_trigram"),
                )
            ):
                if fts_table == "events_fts":
                    had_existing_objects = any(
                        (
                            name == "events_fts"
                            or name.startswith("events_fts_")
                        )
                        and name != "events_fts_trigram"
                        and not name.startswith("events_fts_trigram_")
                        for name in existing_schema_objects
                    )
                else:
                    had_existing_objects = any(
                        name == fts_table or name.startswith(f"{fts_table}_")
                        for name in existing_schema_objects
                    )
                savepoint = f"optional_fts_bootstrap_{group_index}"
                conn.execute(f"SAVEPOINT {savepoint}")
                try:
                    self._execute_script_in_transaction(conn, fts_script)
                    conn.execute(f"RELEASE SAVEPOINT {savepoint}")
                    available_fts_scripts.append(fts_script)
                except sqlite3.OperationalError as exc:
                    # A multi-statement script may create the virtual table and
                    # fail while creating a later trigger. Roll back the whole
                    # capability group so an optional fresh miss cannot leave a
                    # half-live index. If any object in this group existed before
                    # bootstrap, the index was already a supported data path and
                    # losing its maintenance is corruption, not an optional miss.
                    conn.execute(f"ROLLBACK TO SAVEPOINT {savepoint}")
                    conn.execute(f"RELEASE SAVEPOINT {savepoint}")
                    if had_existing_objects:
                        raise
                    logger.warning(
                        "Self-learning %s unavailable: %s",
                        fts_label,
                        sanitize_text_fragment(str(exc), max_chars=1000),
                    )
            current = int(conn.execute("SELECT COALESCE(MAX(version), 0) FROM schema_version").fetchone()[0])
            cleanup_row = conn.execute(
                "SELECT value FROM maintenance WHERE key = ?",
                (_V4_PHYSICAL_CLEANUP_KEY,),
            ).fetchone()
            cleanup_state = str(cleanup_row["value"] or "") if cleanup_row else ""
            sanitizer_row = conn.execute(
                "SELECT value FROM maintenance WHERE key = ?",
                (_V4_SANITIZER_REVISION_KEY,),
            ).fetchone()
            sanitizer_revision = (
                str(sanitizer_row["value"] or "") if sanitizer_row else ""
            )
            sanitizing_legacy = (
                current < 4 or sanitizer_revision != _V4_SANITIZER_REVISION
            )
            if sanitizing_legacy:
                # UPDATE triggers would mirror intermediate event rows into
                # FTS. Rebuild the indexes from final sanitized base rows and
                # only then restore trigger maintenance.
                for trigger_name in _FTS_TRIGGER_NAMES:
                    conn.execute(f'DROP TRIGGER IF EXISTS "{trigger_name}"')
            if current < 1:
                conn.execute("INSERT INTO schema_version (version) VALUES (1)")
            if current < 2:
                self._migrate_v2_memory_hash(conn)
                conn.execute("INSERT INTO schema_version (version) VALUES (2)")
            if current < 3:
                self._migrate_v3_memory_value(conn)
                conn.execute("INSERT INTO schema_version (version) VALUES (3)")
            if current < 4:
                self._migrate_v4_identity_and_jobs(conn)
                conn.execute("INSERT INTO schema_version (version) VALUES (4)")
                migrated_v4 = True
            elif sanitizer_revision != _V4_SANITIZER_REVISION:
                # Development builds may already carry a v4 marker from before
                # identity rekeying joined the privacy boundary. Keep the public
                # schema version stable while making that cleanup resumable and
                # forcing the same post-commit physical rewrite.
                self._sanitize_v4_identities(conn)
                self._sanitize_v4_rows(conn)
                refreshed_v4_sanitizer = True
            if sanitizing_legacy:
                # These scripts succeeded before trigger removal. A restore
                # failure is therefore corruption of an available index path,
                # not an optional-capability miss: let the outer transaction
                # roll back both the trigger drops and sanitizer marker.
                for fts_script in available_fts_scripts:
                    self._execute_script_in_transaction(conn, fts_script)
            conn.execute(
                """
                INSERT INTO maintenance (key, value) VALUES (?, ?)
                ON CONFLICT(key) DO UPDATE SET value = excluded.value
                """,
                (_V4_SANITIZER_REVISION_KEY, _V4_SANITIZER_REVISION),
            )
            # ``schema_version=4`` proves only that logical redaction committed.
            # A checkpoint can still fail afterwards while an older reader pins
            # pre-redaction WAL frames. Persist the physical-cleanup obligation
            # in the same transaction so every later process retries it.
            requires_v4_physical_cleanup = (
                migrated_v4
                or refreshed_v4_sanitizer
                or cleanup_state != _V4_CLEANUP_COMPLETE
            )
            if requires_v4_physical_cleanup:
                self._set_v4_cleanup_state(conn, _V4_CLEANUP_PENDING)
            # Root-scoped distillation is a real query path; no event query is
            # keyed by task_id. Replace the obsolete write-amplifying index on
            # existing v4 databases as well as fresh schemas.
            conn.execute("DROP INDEX IF EXISTS idx_events_task_id")
            conn.execute("DROP INDEX IF EXISTS idx_events_root_run")
            conn.execute(
                "CREATE INDEX idx_events_root_run ON events(root_run_id)"
            )
            # This idempotency constraint is part of the latest physical DDL,
            # not only the v3 -> v4 transition. Development builds may already
            # have stamped v4 before the constraint landed, so repair that
            # shape on every initialization as well.
            review_columns = {
                row[1] for row in conn.execute("PRAGMA table_info(review_runs)")
            }
            if "learning_job_id" in review_columns:
                conn.execute(
                    "DELETE FROM review_runs WHERE learning_job_id IS NOT NULL "
                    "AND review_id NOT IN (SELECT MIN(review_id) FROM review_runs "
                    "WHERE learning_job_id IS NOT NULL GROUP BY learning_job_id)"
                )
                conn.execute(
                    "CREATE UNIQUE INDEX IF NOT EXISTS idx_review_runs_learning_job "
                    "ON review_runs(learning_job_id) "
                    "WHERE learning_job_id IS NOT NULL"
                )
            # Older migration code did not constrain this ledger.  Collapse
            # any historical duplicate version markers before making future
            # concurrent upgrades structurally idempotent.
            conn.execute(
                "DELETE FROM schema_version WHERE rowid NOT IN (SELECT MIN(rowid) FROM schema_version GROUP BY version)"
            )
            conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_schema_version_unique ON schema_version(version)")
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            # The connection is short-lived, but restoring the default keeps
            # direct migration tests and future connection reuse predictable.
            conn.execute("PRAGMA cache_spill=ON")
        if requires_v4_physical_cleanup:
            self._complete_v4_physical_cleanup(conn)

    @staticmethod
    def _set_v4_cleanup_state(conn: sqlite3.Connection, state: str) -> None:
        conn.execute(
            """
            INSERT INTO maintenance (key, value) VALUES (?, ?)
            ON CONFLICT(key) DO UPDATE SET value = excluded.value
            """,
            (_V4_PHYSICAL_CLEANUP_KEY, state),
        )

    def _complete_v4_physical_cleanup(self, conn: sqlite3.Connection) -> None:
        """Rewrite sanitized storage, then durably close the cleanup marker.

        The first truncate is the privacy boundary: until it succeeds, the
        durable marker remains ``pending``. VACUUM is also required because a
        legacy database may have deleted secrets while ``secure_delete`` was
        off; those bytes live in main-DB freelist pages and no WAL checkpoint
        can remove them. The following truncates checkpoint the compacted DB
        and the ``complete`` marker itself. If the final checkpoint is blocked,
        reset the durable state and make the next constructor retry.
        """
        self._truncate_migration_wal(conn)
        conn.execute("VACUUM")
        self._truncate_migration_wal(conn)

        conn.execute("BEGIN IMMEDIATE")
        try:
            self._set_v4_cleanup_state(conn, _V4_CLEANUP_COMPLETE)
            conn.commit()
        except Exception:
            conn.rollback()
            raise

        try:
            self._truncate_migration_wal(conn)
        except Exception:
            try:
                conn.execute("BEGIN IMMEDIATE")
                self._set_v4_cleanup_state(conn, _V4_CLEANUP_PENDING)
                conn.commit()
            except Exception:
                conn.rollback()
            raise

    @staticmethod
    def _truncate_migration_wal(
        conn: sqlite3.Connection,
        *,
        timeout_seconds: float = _BUSY_TIMEOUT_MS / 1000,
    ) -> None:
        """Remove pre-redaction WAL frames before declaring v4 initialized."""
        deadline = time.monotonic() + max(0.0, float(timeout_seconds))
        while True:
            busy, _log_frames, _checkpointed = conn.execute(
                "PRAGMA wal_checkpoint(TRUNCATE)"
            ).fetchone()
            if int(busy or 0) == 0:
                return
            if time.monotonic() >= deadline:
                raise sqlite3.OperationalError(
                    "timed out truncating pre-redaction migration WAL"
                )
            time.sleep(0.01)

    @staticmethod
    def _add_column_if_missing(conn: sqlite3.Connection, table: str, name: str, ddl: str) -> None:
        columns = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
        if name not in columns:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {ddl}")

    @staticmethod
    def _migrate_v2_memory_hash(conn: sqlite3.Connection) -> None:
        """Backfill memory_items.content_hash and enforce active/pending uniqueness."""
        columns = {row[1] for row in conn.execute("PRAGMA table_info(memory_items)")}
        if "content_hash" not in columns:
            conn.execute("ALTER TABLE memory_items ADD COLUMN content_hash TEXT")
        rows = conn.execute(
            "SELECT id, content FROM memory_items WHERE content_hash IS NULL OR content_hash = ''"
        ).fetchall()
        for row in rows:
            conn.execute(
                "UPDATE memory_items SET content_hash = ? WHERE id = ?",
                (memory_content_hash(row["content"]), row["id"]),
            )
        # Collapse pre-existing duplicates before the unique index lands.
        # Keep the preferred row per bucket: active beats pending, then lowest id.
        duplicates = conn.execute(
            """
            SELECT scope_type, scope_id, content_hash FROM memory_items
            WHERE status IN ('active', 'pending')
            GROUP BY scope_type, scope_id, content_hash
            HAVING COUNT(*) > 1
            """
        ).fetchall()
        now = _now_iso()
        for bucket in duplicates:
            members = conn.execute(
                """
                SELECT id, status FROM memory_items
                WHERE scope_type = ? AND scope_id = ? AND content_hash = ?
                    AND status IN ('active', 'pending')
                ORDER BY CASE status WHEN 'active' THEN 0 ELSE 1 END, id
                """,
                (bucket["scope_type"], bucket["scope_id"], bucket["content_hash"]),
            ).fetchall()
            for extra in members[1:]:
                conn.execute(
                    "UPDATE memory_items SET status = 'removed', updated_at = ? WHERE id = ?",
                    (now, extra["id"]),
                )
        conn.execute(
            """
            CREATE UNIQUE INDEX IF NOT EXISTS idx_memory_dedup
            ON memory_items(scope_type, scope_id, content_hash)
            WHERE status IN ('active', 'pending')
            """
        )
        conn.execute("CREATE INDEX IF NOT EXISTS idx_memory_updated ON memory_items(updated_at)")

    @staticmethod
    def _migrate_v3_memory_value(conn: sqlite3.Connection) -> None:
        """Add memory value-tracking columns, injection log, and maintenance state."""
        columns = {row[1] for row in conn.execute("PRAGMA table_info(memory_items)")}
        for name, ddl in (
            ("trust_score", "trust_score REAL NOT NULL DEFAULT 0.5"),
            ("injected_count", "injected_count INTEGER NOT NULL DEFAULT 0"),
            ("last_injected_at", "last_injected_at TEXT"),
            ("helpful_count", "helpful_count INTEGER NOT NULL DEFAULT 0"),
            ("unhelpful_count", "unhelpful_count INTEGER NOT NULL DEFAULT 0"),
            ("applied_by", "applied_by TEXT DEFAULT ''"),
            ("conflicts_json", "conflicts_json TEXT DEFAULT ''"),
            ("corroboration_runs_json", "corroboration_runs_json TEXT DEFAULT ''"),
        ):
            if name not in columns:
                try:
                    conn.execute(f"ALTER TABLE memory_items ADD COLUMN {ddl}")
                except sqlite3.OperationalError as exc:
                    # Another process migrated between our PRAGMA read and this
                    # ALTER; the column existing is the desired end state.
                    if "duplicate column" not in str(exc).lower():
                        raise
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS memory_injections (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                run_id TEXT NOT NULL,
                item_id INTEGER NOT NULL,
                injected_at TEXT NOT NULL
            )
            """
        )
        conn.execute("CREATE INDEX IF NOT EXISTS idx_injections_run ON memory_injections(run_id)")
        conn.execute("CREATE TABLE IF NOT EXISTS maintenance (key TEXT PRIMARY KEY, value TEXT)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_memory_trust ON memory_items(trust_score)")

    @staticmethod
    def _has_column(
        conn: sqlite3.Connection,
        table: str,
        column: str,
    ) -> bool:
        return column in {
            str(row[1]) for row in conn.execute(f'PRAGMA table_info("{table}")')
        }

    @classmethod
    def _rekey_legacy_identity_domain(
        cls,
        conn: sqlite3.Connection,
        *,
        namespace: str,
        references: tuple[tuple[str, str, str], ...],
    ) -> dict[str, str]:
        """Replace unsafe legacy identities while preserving every reference.

        Identity columns cannot be redacted independently: doing so can either
        collide two primary keys or detach events/evidence from their run.  A
        full SHA-256-derived identifier is deterministic across all references,
        contains none of the original text, and is collision-checked against
        both safe historical ids and earlier replacements in this transaction.
        """
        available = [
            (table, column, predicate)
            for table, column, predicate in references
            if cls._has_column(conn, table, column)
        ]
        values: set[str] = set()
        for table, column, predicate in available:
            where = f" WHERE {predicate}" if predicate else ""
            values.update(
                str(row[0])
                for row in conn.execute(
                    f'SELECT DISTINCT "{column}" FROM "{table}"{where}'
                )
                if row[0] is not None and str(row[0])
            )

        unsafe = {
            value
            for value in values
            if sanitize_text_fragment(value) != value
        }
        occupied = values - unsafe
        replacements: dict[str, str] = {}
        for value in sorted(unsafe):
            digest = hashlib.sha256(
                value.encode("utf-8", errors="surrogatepass")
            ).hexdigest()
            base = f"redacted-{namespace}-{digest}"
            replacement = base
            suffix = 0
            while replacement in occupied:
                suffix += 1
                replacement = f"{base}-{suffix}"
            replacements[value] = replacement
            occupied.add(replacement)

        for original, replacement in replacements.items():
            for table, column, predicate in available:
                where = f" AND ({predicate})" if predicate else ""
                conn.execute(
                    f'UPDATE "{table}" SET "{column}" = ? '
                    f'WHERE "{column}" = ?{where}',
                    (replacement, original),
                )
        return replacements

    @classmethod
    def _sanitize_v4_identities(cls, conn: sqlite3.Connection) -> None:
        """Rekey every cross-table identity domain before content cleanup."""
        changed_job_ids: set[int] = set()
        if cls._has_column(conn, "learning_jobs", "kind"):
            for row in conn.execute(
                "SELECT id, kind, dedupe_key, root_run_id FROM learning_jobs"
            ).fetchall():
                if any(
                    sanitize_text_fragment(str(row[column] or ""))
                    != str(row[column] or "")
                    for column in ("kind", "dedupe_key", "root_run_id")
                ):
                    changed_job_ids.add(int(row["id"]))
        run_replacements = cls._rekey_legacy_identity_domain(
            conn,
            namespace="run",
            references=(
                ("runs", "run_id", ""),
                ("runs", "root_run_id", ""),
                ("events", "run_id", ""),
                ("events", "root_run_id", ""),
                ("memory_items", "source_run_id", ""),
                ("memory_items", "scope_id", "scope_type = 'session'"),
                ("memory_evidence", "root_run_id", ""),
                ("memory_injections", "run_id", ""),
                ("skill_proposals", "source_run_id", ""),
                ("review_runs", "source_run_id", ""),
                ("learning_jobs", "root_run_id", ""),
                # A session-review dedupe key is its root id. Updating equal
                # values here keeps the outbox identity internally coherent.
                ("learning_jobs", "dedupe_key", "kind = 'session_review'"),
                ("artifacts", "run_id", ""),
            ),
        )
        event_replacements = cls._rekey_legacy_identity_domain(
            conn,
            namespace="event",
            references=(
                ("events", "event_id", ""),
                ("events", "parent_event_id", ""),
                ("memory_items", "source_event_id", ""),
                ("skill_proposals", "source_event_id", ""),
                ("review_runs", "trigger_event_id", ""),
                ("artifacts", "event_id", ""),
            ),
        )
        task_replacements = cls._rekey_legacy_identity_domain(
            conn,
            namespace="task",
            references=(
                ("runs", "task_id", ""),
                ("events", "task_id", ""),
                ("events", "parent_task_id", ""),
            ),
        )
        application_replacements = cls._rekey_legacy_identity_domain(
            conn,
            namespace="application",
            references=(
                ("runs", "application_id", ""),
                ("events", "application_id", ""),
                ("memory_items", "scope_id", "scope_type = 'application'"),
                ("skill_proposals", "application_id", ""),
                ("review_runs", "application_id", ""),
            ),
        )
        cls._rekey_legacy_identity_domain(
            conn,
            namespace="proposal",
            references=(("skill_proposals", "proposal_id", ""),),
        )
        cls._rekey_legacy_identity_domain(
            conn,
            namespace="job-kind",
            references=(("learning_jobs", "kind", ""),),
        )
        # Non-run job keys (for example a future maintenance kind) still need
        # a collision-safe identity boundary.
        cls._rekey_legacy_identity_domain(
            conn,
            namespace="job-key",
            references=(("learning_jobs", "dedupe_key", ""),),
        )
        cls._rekey_legacy_identity_domain(
            conn,
            namespace="effect-key",
            references=(("learning_job_effects", "effect_key", ""),),
        )
        cls._rekey_legacy_identity_domain(
            conn,
            namespace="effect-hash",
            references=(("learning_job_effects", "effect_hash", ""),),
        )
        cls._rekey_legacy_identity_domain(
            conn,
            namespace="maintenance",
            references=(("maintenance", "key", ""),),
        )
        cls._rewrite_v4_job_payload_identities(
            conn,
            run_replacements=run_replacements,
            event_replacements=event_replacements,
            task_replacements=task_replacements,
            application_replacements=application_replacements,
            changed_job_ids=changed_job_ids,
        )

    @staticmethod
    def _rewrite_v4_job_payload_identities(
        conn: sqlite3.Connection,
        *,
        run_replacements: dict[str, str],
        event_replacements: dict[str, str],
        task_replacements: dict[str, str],
        application_replacements: dict[str, str],
        changed_job_ids: set[int],
    ) -> None:
        """Sanitize legacy jobs without silently executing a changed frozen plan.

        Any non-terminal job whose identity/payload changes is dead-lettered.
        Its digest/semantic plan is removed, so an explicit ``retry-job`` can
        safely prepare a fresh, internally consistent input instead of burning
        attempts on stale hashes or triggering an unrequested model call during
        migration.
        """
        domains = {
            "run_id": run_replacements,
            "root_run_id": run_replacements,
            "session_id": run_replacements,
            "source_run_id": run_replacements,
            "event_id": event_replacements,
            "parent_event_id": event_replacements,
            "source_event_id": event_replacements,
            "trigger_event_id": event_replacements,
            "task_id": task_replacements,
            "parent_task_id": task_replacements,
            "application_id": application_replacements,
        }
        all_replacements: dict[str, str] = {}
        for replacements in (
            run_replacements,
            event_replacements,
            task_replacements,
            application_replacements,
        ):
            for original, replacement in replacements.items():
                all_replacements.setdefault(original, replacement)

        def rewrite(value: Any, field: str = "") -> Any:
            if isinstance(value, dict):
                return {
                    key: rewrite(
                        item,
                        re.sub(
                            r"[^0-9a-z]+",
                            "_",
                            str(key).casefold(),
                        ).strip("_"),
                    )
                    for key, item in value.items()
                }
            if isinstance(value, list):
                return [rewrite(item, field) for item in value]
            if isinstance(value, str):
                rewritten = domains.get(field, {}).get(value, value)
                for original, replacement in all_replacements.items():
                    if original in rewritten:
                        rewritten = rewritten.replace(original, replacement)
                return rewritten
            return value

        if not SelfLearningLedger._has_column(conn, "learning_jobs", "payload_json"):
            return
        for row in conn.execute(
            "SELECT id, status, payload_json FROM learning_jobs"
        ).fetchall():
            raw_payload = str(row["payload_json"] or "{}")
            parse_failed = False
            try:
                payload = json.loads(raw_payload)
            except (TypeError, json.JSONDecodeError):
                payload = {}
                parse_failed = True
            rewritten = rewrite(payload)
            sanitized = sanitize_value_fragments(rewritten)
            if not isinstance(sanitized, dict):
                sanitized = {}
            changed = (
                int(row["id"]) in changed_job_ids
                or parse_failed
                or rewritten != payload
                or sanitized != rewritten
            )
            if not changed:
                continue
            sanitized.pop("prepared_digest", None)
            sanitized.pop("semantic_plan", None)
            if str(row["status"] or "") != "succeeded":
                timestamp = _now_iso()
                conn.execute(
                    """
                    UPDATE learning_jobs
                    SET payload_json = ?, status = 'dead', attempts = 3,
                        available_at = ?, lease_owner = NULL, lease_token = NULL,
                        lease_until = NULL, result_json = NULL, last_error = ?,
                        updated_at = ?, finished_at = ?
                    WHERE id = ?
                    """,
                    (
                        _json_dumps(sanitized),
                        timestamp,
                        LEGACY_SANITIZER_DEAD_ERROR,
                        timestamp,
                        timestamp,
                        int(row["id"]),
                    ),
                )
            else:
                conn.execute(
                    "UPDATE learning_jobs SET payload_json = ? WHERE id = ?",
                    (_json_dumps(sanitized), int(row["id"])),
                )

    @classmethod
    def _migrate_v4_identity_and_jobs(cls, conn: sqlite3.Connection) -> None:
        """Add immutable memory identity and the durable learning outbox.

        Deliberately do not read ``corroboration_runs_json``.  Earlier code
        populated it using fuzzy overlap, so it is not trustworthy evidence.
        The only legacy evidence we can prove is the row's originating run.
        """
        cls._add_column_if_missing(conn, "runs", "root_run_id", "root_run_id TEXT")
        cls._add_column_if_missing(
            conn,
            "runs",
            "memory_outcome_recorded_at",
            "memory_outcome_recorded_at TEXT",
        )
        cls._add_column_if_missing(conn, "events", "root_run_id", "root_run_id TEXT")
        cls._add_column_if_missing(
            conn,
            "memory_items",
            "generation",
            "generation INTEGER NOT NULL DEFAULT 1",
        )
        cls._add_column_if_missing(conn, "memory_items", "supersedes_id", "supersedes_id INTEGER")
        cls._add_column_if_missing(conn, "memory_items", "target_item_id", "target_item_id INTEGER")
        cls._add_column_if_missing(conn, "review_runs", "learning_job_id", "learning_job_id INTEGER")

        conn.execute("UPDATE runs SET root_run_id = run_id WHERE root_run_id IS NULL OR root_run_id = ''")
        conn.execute("UPDATE events SET root_run_id = run_id WHERE root_run_id IS NULL OR root_run_id = ''")
        conn.execute("UPDATE memory_items SET generation = 1 WHERE generation IS NULL OR generation < 1")

        # Do not use ``executescript`` here: Python's sqlite wrapper commits
        # before executescript(), which would punch a hole in the migration's
        # BEGIN IMMEDIATE atomicity.
        for ddl in (
            """CREATE TABLE IF NOT EXISTS memory_evidence (
                item_id INTEGER NOT NULL,
                root_run_id TEXT NOT NULL,
                source TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL,
                PRIMARY KEY (item_id, root_run_id)
            )""",
            """CREATE TABLE IF NOT EXISTS learning_jobs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                kind TEXT NOT NULL,
                dedupe_key TEXT NOT NULL,
                root_run_id TEXT NOT NULL,
                payload_json TEXT NOT NULL DEFAULT '{}',
                status TEXT NOT NULL DEFAULT 'pending',
                attempts INTEGER NOT NULL DEFAULT 0,
                available_at TEXT NOT NULL,
                lease_owner TEXT,
                lease_token TEXT,
                lease_until TEXT,
                result_json TEXT,
                last_error TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                finished_at TEXT,
                UNIQUE (kind, dedupe_key)
            )""",
            """CREATE TABLE IF NOT EXISTS learning_job_effects (
                job_id INTEGER NOT NULL,
                effect_key TEXT NOT NULL,
                effect_hash TEXT NOT NULL,
                effect_type TEXT NOT NULL,
                result_json TEXT NOT NULL DEFAULT '{}',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                PRIMARY KEY (job_id, effect_key)
            )""",
            """CREATE INDEX IF NOT EXISTS idx_memory_evidence_run
                ON memory_evidence(root_run_id)""",
            """CREATE INDEX IF NOT EXISTS idx_learning_jobs_ready
                ON learning_jobs(status, available_at, id)""",
            """CREATE INDEX IF NOT EXISTS idx_learning_jobs_root_run
                ON learning_jobs(root_run_id, id)""",
            """CREATE INDEX IF NOT EXISTS idx_learning_job_effects_job
                ON learning_job_effects(job_id, effect_key)""",
        ):
            conn.execute(ddl)

        cls._sanitize_v4_identities(conn)

        # Origin-only backfill.  Fuzzy corroboration history is intentionally
        # discarded; a future independent run must re-earn the second vote.
        legacy_origins = conn.execute(
            """
            SELECT id, source_run_id, COALESCE(source, '') AS source, created_at
            FROM memory_items
            WHERE source_run_id IS NOT NULL
            """
        ).fetchall()
        for origin in legacy_origins:
            # Use the same canonical root id as the runtime evidence path. In
            # particular, a padded legacy id must not become a second vote
            # when the same root later supplies the canonical spelling.
            root_run_id = safe_run_id(str(origin["source_run_id"] or ""))
            if not root_run_id:
                continue
            conn.execute(
                """
                INSERT OR IGNORE INTO memory_evidence (
                    item_id, root_run_id, source, created_at
                ) VALUES (?, ?, ?, ?)
                """,
                (
                    int(origin["id"]),
                    root_run_id,
                    sanitize_text_fragment(origin["source"] or ""),
                    origin["created_at"],
                ),
            )

        # Auto-applied legacy rows may have crossed the old fuzzy evidence
        # gate.  Keep them for review but require fresh exact evidence before
        # activation again.
        conn.execute(
            """
            UPDATE memory_items
            SET status = 'pending', applied_at = NULL, updated_at = ?
            WHERE status = 'active' AND applied_by = 'auto'
            """,
            (_now_iso(),),
        )

        # Freeze replace/remove proposals onto a concrete active revision.
        # Ambiguous or missing legacy targets are not guessed.
        proposals = conn.execute(
            """
            SELECT id, scope_type, scope_id, target
            FROM memory_items
            WHERE status = 'pending' AND action IN ('replace', 'remove')
                AND target_item_id IS NULL
            """
        ).fetchall()
        now = _now_iso()
        for proposal in proposals:
            target = str(proposal["target"] or "").strip()
            if not target:
                conn.execute(
                    "UPDATE memory_items SET status = 'stale', updated_at = ? WHERE id = ?",
                    (now, int(proposal["id"])),
                )
                continue
            if target.isdigit():
                candidates = conn.execute(
                    """
                    SELECT id FROM memory_items
                    WHERE id = ? AND scope_type = ? AND scope_id = ?
                        AND status = 'active'
                    """,
                    (
                        int(target),
                        proposal["scope_type"],
                        proposal["scope_id"],
                    ),
                ).fetchall()
            else:
                candidates = conn.execute(
                    """
                    SELECT id FROM memory_items
                    WHERE scope_type = ? AND scope_id = ? AND status = 'active'
                        AND content LIKE ?
                    LIMIT 2
                    """,
                    (
                        proposal["scope_type"],
                        proposal["scope_id"],
                        f"%{target}%",
                    ),
                ).fetchall()
            if len(candidates) == 1:
                conn.execute(
                    "UPDATE memory_items SET target_item_id = ?, updated_at = ? WHERE id = ?",
                    (int(candidates[0]["id"]), now, int(proposal["id"])),
                )
            else:
                conn.execute(
                    "UPDATE memory_items SET status = 'stale', updated_at = ? WHERE id = ?",
                    (now, int(proposal["id"])),
                )

        cls._sanitize_v4_rows(conn)

    @staticmethod
    def _redact_legacy_cell(value: Any, *, json_value: bool) -> str | None:
        if value is None:
            return None
        raw = str(value)
        if not json_value:
            return sanitize_text_fragment(raw)
        try:
            parsed = json.loads(raw)
        except (TypeError, json.JSONDecodeError):
            return sanitize_text_fragment(raw)
        return _json_dumps(sanitize_value_fragments(parsed))

    @classmethod
    def _sanitize_v4_rows(cls, conn: sqlite3.Connection) -> None:
        """Redact legacy content in-place, then rebuild both FTS indexes."""
        before_runs = int(conn.execute("SELECT COUNT(*) FROM runs").fetchone()[0])
        before_events = int(conn.execute("SELECT COUNT(*) FROM events").fetchone()[0])

        # SQLite's type affinity accepts TEXT in INTEGER/REAL columns.  Clean
        # invalid historical values as part of the same no-spill transaction;
        # otherwise a secret can hide outside the TEXT/JSON column inventory.
        numeric_repairs = (
            "UPDATE events SET step_number = NULL "
            "WHERE step_number IS NOT NULL AND typeof(step_number) != 'integer'",
            "UPDATE events SET ordinal = 0 WHERE typeof(ordinal) != 'integer'",
            "UPDATE memory_items SET trust_score = 0.5 "
            "WHERE typeof(trust_score) NOT IN ('integer', 'real')",
            "UPDATE memory_items SET injected_count = 0 "
            "WHERE typeof(injected_count) != 'integer'",
            "UPDATE memory_items SET helpful_count = 0 "
            "WHERE typeof(helpful_count) != 'integer'",
            "UPDATE memory_items SET unhelpful_count = 0 "
            "WHERE typeof(unhelpful_count) != 'integer'",
            "UPDATE memory_items SET generation = 1 "
            "WHERE typeof(generation) != 'integer' OR generation < 1",
            "UPDATE memory_items SET supersedes_id = NULL "
            "WHERE supersedes_id IS NOT NULL AND typeof(supersedes_id) != 'integer'",
            "UPDATE memory_items SET target_item_id = NULL "
            "WHERE target_item_id IS NOT NULL AND typeof(target_item_id) != 'integer'",
            "UPDATE review_runs SET learning_job_id = NULL "
            "WHERE learning_job_id IS NOT NULL AND typeof(learning_job_id) != 'integer'",
            "UPDATE learning_jobs SET attempts = 0 "
            "WHERE typeof(attempts) != 'integer'",
            "DELETE FROM memory_evidence WHERE typeof(item_id) != 'integer'",
            "DELETE FROM memory_injections WHERE typeof(item_id) != 'integer'",
            "DELETE FROM learning_job_effects WHERE typeof(job_id) != 'integer'",
        )
        for statement in numeric_repairs:
            try:
                conn.execute(statement)
            except sqlite3.OperationalError as exc:
                # v3 databases legitimately lack some v4 tables/columns until
                # the surrounding migration creates them. A missing shape is
                # skipped; every present latest-schema surface is repaired.
                if "no such table" not in str(exc).casefold() and "no such column" not in str(exc).casefold():
                    raise

        # (table, primary-key, [(column, is_json), ...]). Cross-table identity
        # fields were rekeyed coherently above; every remaining TEXT/JSON field
        # crosses the same privacy boundary here.
        targets: tuple[tuple[str, str, tuple[tuple[str, bool], ...]], ...] = (
            (
                "runs",
                "run_id",
                (
                    ("task_id", False),
                    ("agent_name", False),
                    ("application_id", False),
                    ("application_name", False),
                    ("application_path", False),
                    ("workflow_path", False),
                    ("yaml_path", False),
                    ("run_dir", False),
                    ("status", False),
                    ("started_at", False),
                    ("ended_at", False),
                    ("task_text", False),
                    ("final_answer", False),
                    ("indexed_at", False),
                    ("metadata_json", True),
                    ("memory_outcome_recorded_at", False),
                ),
            ),
            (
                "events",
                "id",
                (
                    ("task_id", False),
                    ("parent_task_id", False),
                    ("parent_event_id", False),
                    ("application_id", False),
                    ("application_name", False),
                    ("application_path", False),
                    ("workflow_path", False),
                    ("agent_name", False),
                    ("worker_name", False),
                    ("tool_name", False),
                    ("event_type", False),
                    ("phase", False),
                    ("source", False),
                    ("role", False),
                    ("status", False),
                    ("input_json", True),
                    ("output_json", True),
                    ("content_text", False),
                    ("content_ref", False),
                    ("source_path", False),
                    ("created_at", False),
                    ("metadata_json", True),
                ),
            ),
            (
                "memory_items",
                "id",
                (
                    ("scope_type", False),
                    ("scope_id", False),
                    ("content", False),
                    ("content_hash", False),
                    ("status", False),
                    ("action", False),
                    ("target", False),
                    ("source", False),
                    ("created_at", False),
                    ("updated_at", False),
                    ("applied_at", False),
                    ("last_injected_at", False),
                    ("applied_by", False),
                    ("conflicts_json", True),
                    ("corroboration_runs_json", True),
                ),
            ),
            (
                "skill_proposals",
                "proposal_id",
                (
                    ("name", False),
                    ("action", False),
                    ("status", False),
                    ("proposal_path", False),
                    ("application_id", False),
                    ("manifest_json", True),
                    ("created_at", False),
                    ("updated_at", False),
                    ("promoted_at", False),
                    ("archived_at", False),
                ),
            ),
            (
                "review_runs",
                "review_id",
                (
                    ("hook_event", False),
                    ("application_id", False),
                    ("status", False),
                    ("output_json", True),
                    ("created_at", False),
                ),
            ),
            (
                "artifacts",
                "artifact_id",
                (
                    ("kind", False),
                    ("uri", False),
                    ("sha256", False),
                    ("metadata_json", True),
                    ("created_at", False),
                ),
            ),
            (
                "learning_jobs",
                "id",
                (
                    ("payload_json", True),
                    ("result_json", True),
                    ("last_error", False),
                    ("status", False),
                    ("available_at", False),
                    ("lease_owner", False),
                    ("lease_token", False),
                    ("lease_until", False),
                    ("created_at", False),
                    ("updated_at", False),
                    ("finished_at", False),
                ),
            ),
            (
                "memory_evidence",
                "rowid",
                (("source", False), ("created_at", False)),
            ),
            (
                "memory_injections",
                "rowid",
                (("injected_at", False),),
            ),
            (
                "learning_job_effects",
                "rowid",
                (
                    ("effect_type", False),
                    ("result_json", True),
                    ("created_at", False),
                    ("updated_at", False),
                ),
            ),
            ("maintenance", "key", (("value", True),)),
        )
        conn.execute("DROP INDEX IF EXISTS idx_memory_dedup")
        for table, primary_key, columns in targets:
            existing_columns = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
            selected = [column for column, _ in columns if column in existing_columns]
            if not selected or (primary_key != "rowid" and primary_key not in existing_columns):
                continue
            key_select = "rowid AS migration_rowid" if primary_key == "rowid" else primary_key
            rows = conn.execute(f"SELECT {key_select}, {', '.join(selected)} FROM {table}").fetchall()
            column_modes = dict(columns)
            for row in rows:
                values = {
                    column: cls._redact_legacy_cell(row[column], json_value=column_modes[column]) for column in selected
                }
                if table == "memory_items" and "content" in values:
                    values["content_hash"] = memory_content_hash(values["content"] or "")
                assignments = ", ".join(f"{column} = ?" for column in values)
                row_key = row["migration_rowid"] if primary_key == "rowid" else row[primary_key]
                conn.execute(
                    f"UPDATE {table} SET {assignments} WHERE {primary_key} = ?",
                    [*values.values(), row_key],
                )

        # Redaction can intentionally collapse two secret-only memories to the
        # same content.  Preserve one reviewable row per active/pending bucket.
        duplicates = conn.execute(
            """
            SELECT scope_type, scope_id, content_hash FROM memory_items
            WHERE status IN ('active', 'pending')
            GROUP BY scope_type, scope_id, content_hash
            HAVING COUNT(*) > 1
            """
        ).fetchall()
        now = _now_iso()
        for bucket in duplicates:
            rows = conn.execute(
                """
                SELECT id FROM memory_items
                WHERE scope_type = ? AND scope_id = ? AND content_hash = ?
                    AND status IN ('active', 'pending')
                ORDER BY CASE status WHEN 'active' THEN 0 ELSE 1 END, id
                """,
                (bucket["scope_type"], bucket["scope_id"], bucket["content_hash"]),
            ).fetchall()
            for extra in rows[1:]:
                conn.execute(
                    "UPDATE memory_items SET status = 'removed', updated_at = ? WHERE id = ?",
                    (now, int(extra["id"])),
                )
        conn.execute(
            """
            CREATE UNIQUE INDEX IF NOT EXISTS idx_memory_dedup
            ON memory_items(scope_type, scope_id, content_hash)
            WHERE status IN ('active', 'pending')
            """
        )

        # UPDATE triggers already keep FTS current; an explicit rebuild also
        # removes any orphan rows left by databases predating those triggers.
        # FTS is optional only when its virtual table could not be created.
        # Once an index exists, a failed rebuild is part of the privacy
        # boundary and must abort the surrounding sanitizer transaction.
        existing_fts_tables = {
            str(row[0])
            for row in conn.execute(
                "SELECT name FROM sqlite_master "
                "WHERE type = 'table' "
                "AND name IN ('events_fts', 'events_fts_trigram')"
            )
        }
        if "events_fts" in existing_fts_tables:
            conn.execute("DELETE FROM events_fts")
            conn.execute(
                """
                INSERT INTO events_fts (
                    rowid, content_text, tool_name, agent_name, worker_name,
                    event_type, source, role, status, application_id, run_id
                )
                SELECT id,
                    COALESCE(content_text, '') || ' ' || COALESCE(tool_name, '') ||
                    ' ' || COALESCE(input_json, '') || ' ' || COALESCE(output_json, ''),
                    COALESCE(tool_name, ''), COALESCE(agent_name, ''),
                    COALESCE(worker_name, ''), COALESCE(event_type, ''),
                    COALESCE(source, ''), COALESCE(role, ''), COALESCE(status, ''),
                    COALESCE(application_id, ''), COALESCE(run_id, '')
                FROM events
                """
            )
            # DELETE + reinsert leaves tombstoned terms in FTS5 segment
            # tables. Compact immediately so legacy secrets are not still
            # logically readable from the index's shadow tables.
            conn.execute("INSERT INTO events_fts(events_fts) VALUES('optimize')")

        if "events_fts_trigram" in existing_fts_tables:
            conn.execute("DELETE FROM events_fts_trigram")
            conn.execute(
                """
                INSERT INTO events_fts_trigram (rowid, content_text)
                SELECT id,
                    COALESCE(content_text, '') || ' ' || COALESCE(tool_name, '') ||
                    ' ' || COALESCE(input_json, '') || ' ' || COALESCE(output_json, '')
                FROM events
                """
            )
            # DELETE + reinsert leaves tombstoned terms in FTS5 segment
            # tables. Compact immediately so legacy secrets are not still
            # logically readable from the index's shadow tables.
            conn.execute("INSERT INTO events_fts_trigram(events_fts_trigram) VALUES('optimize')")

        after_runs = int(conn.execute("SELECT COUNT(*) FROM runs").fetchone()[0])
        after_events = int(conn.execute("SELECT COUNT(*) FROM events").fetchone()[0])
        if (after_runs, after_events) != (before_runs, before_events):
            raise RuntimeError("v4 migration changed run/event row counts")

    def get_maintenance(self, key: str) -> str:
        key = require_safe_identity(key, field="maintenance key")
        with self._connect() as conn:
            row = conn.execute("SELECT value FROM maintenance WHERE key = ?", (key,)).fetchone()
        return str(row["value"]) if row else ""

    def set_maintenance(self, key: str, value: str) -> None:
        key = require_safe_identity(key, field="maintenance key")
        value = require_safe_identity(
            value,
            field="maintenance value",
            allow_empty=True,
        )
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO maintenance (key, value) VALUES (?, ?)
                ON CONFLICT(key) DO UPDATE SET value = excluded.value
                """,
                (key, value),
            )

    def claim_maintenance_slot(self, key: str, expected: str, new_value: str) -> bool:
        """Atomically claim a maintenance slot (compare-and-swap on the value).

        Returns True only for the one caller that swaps ``expected`` for
        ``new_value``; concurrent sessions that read the same stale marker
        lose the swap and skip the work.
        """
        key = require_safe_identity(key, field="maintenance key")
        expected = require_safe_identity(
            expected,
            field="expected maintenance value",
            allow_empty=True,
        )
        new_value = require_safe_identity(
            new_value,
            field="new maintenance value",
            allow_empty=True,
        )
        with self._connect() as conn:
            if expected:
                claimed = conn.execute(
                    "UPDATE maintenance SET value = ? WHERE key = ? AND value = ?",
                    (new_value, key, expected),
                ).rowcount
            else:
                claimed = conn.execute(
                    "INSERT OR IGNORE INTO maintenance (key, value) VALUES (?, ?)",
                    (key, new_value),
                ).rowcount
        return bool(claimed)

    @staticmethod
    def _row_to_event_dict(row: sqlite3.Row) -> dict[str, Any]:
        result = dict(row)
        if "content_text" in result:
            # The write/migration boundary owns sanitization before bytes reach
            # either events or FTS. Re-scanning trusted stored content on each
            # search adds payload-sized latency and would make reads the real
            # (inconsistent) security boundary while input/output JSON already
            # relies on the persisted invariant.
            result["content"] = result.get("content_text") or ""
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
    def _run_metadata(record: dict[str, Any], *, root_run_id: str = "") -> dict[str, Any]:
        metadata = record.get("metadata") or {}
        content_payload = {}
        try:
            parsed = json.loads(record.get("content_text") or record.get("content") or "{}")
            if isinstance(parsed, dict):
                content_payload = parsed
        except Exception:
            content_payload = {}
        input_payload = record.get("input_data") or {}
        if not isinstance(input_payload, dict):
            input_payload = {}
        output_payload = record.get("output_data") or {}
        if not isinstance(output_payload, dict):
            output_payload = {}

        def first_present(*values: Any) -> Any:
            return next((value for value in values if value is not None and value != ""), "")

        # Structured input/output has already crossed CanonicalSessionEvent's
        # recursive safety boundary and is less lossy than the compact text
        # projection.  In particular, one injection-bearing task field blocks
        # the complete ``content_text`` fragment while an independent safe
        # final result must remain available to run-level consumers.
        task_text = first_present(
            input_payload.get("task_text"),
            input_payload.get("task"),
            content_payload.get("task_text"),
            content_payload.get("task"),
        )
        final_answer = ""
        if record.get("event_type") in {"task_completed", "run_completed"}:
            final_answer = first_present(
                output_payload.get("result"),
                output_payload.get("final_answer"),
                content_payload.get("result"),
                content_payload.get("final_answer"),
            )

        status = "indexed"
        if record.get("event_type") in {"run_failed", "task_failed"}:
            status = "failed"
        elif record.get("event_type") in {"run_completed", "task_completed"}:
            status = "completed"

        return {
            "run_id": record["run_id"],
            "root_run_id": root_run_id
            or str(record.get("root_run_id") or "")
            or str(metadata.get("root_run_id") or "")
            or record["run_id"],
            "task_id": record.get("task_id") or "",
            "agent_name": record.get("agent_name") or "",
            "application_id": record.get("application_id")
            or str(metadata.get("application_id") or metadata.get("app") or ""),
            "application_name": record.get("application_name")
            or str(metadata.get("application_name") or metadata.get("app") or ""),
            "application_path": record.get("application_path") or str(metadata.get("application_path") or ""),
            "workflow_path": record.get("workflow_path")
            or str(metadata.get("workflow_path") or metadata.get("yaml_path") or ""),
            "yaml_path": record.get("workflow_path") or str(metadata.get("yaml_path") or ""),
            "run_dir": str(metadata.get("run_dir") or record.get("source_path") or ""),
            "status": status,
            "started_at": record.get("created_at") or "",
            "ended_at": record.get("created_at") or "",
            "task_text": redact_text(task_text),
            "final_answer": redact_text(final_answer),
            "indexed_at": _now_iso(),
            "metadata_json": _json_dumps(metadata),
        }

    def _append_event_in_conn(
        self,
        conn: sqlite3.Connection,
        event: CanonicalSessionEvent,
        *,
        root_run_id: str = "",
    ) -> dict[str, Any]:
        if root_run_id:
            root_run_id = require_safe_identity(
                root_run_id,
                field="event root run id",
            )
        record = event.to_record()
        # ``to_record`` owns the event safety boundary. Reusing that immutable
        # snapshot avoids redacting and injection-scanning every field twice on
        # the synchronous append path.
        run_meta = self._run_metadata(record, root_run_id=root_run_id)
        # Event identity is the idempotency boundary for both the event and its
        # run projection.  A repeated SessionEnd may carry a different late
        # answer/status; returning before touching ``runs`` keeps the immutable
        # event, run projection, and deduplicated review payload consistent.
        existing_event = conn.execute(
            "SELECT run_id, root_run_id FROM events WHERE event_id = ?",
            (record["event_id"],),
        ).fetchone()
        if existing_event is not None:
            return {
                "run_id": str(existing_event["run_id"]),
                "root_run_id": str(existing_event["root_run_id"]),
                "event_id": record["event_id"],
                "indexed": False,
                "db_path": str(self.db_path),
            }
        # INSERT first, merge on miss: SELECT-then-INSERT races when two
        # writers hit a brand-new run_id concurrently.
        created = conn.execute(
            """
                INSERT OR IGNORE INTO runs (
                    run_id, root_run_id, task_id, agent_name, application_id, application_name,
                    application_path, workflow_path, yaml_path, run_dir, status,
                    started_at, ended_at, task_text, final_answer, indexed_at, metadata_json
                ) VALUES (
                    :run_id, :root_run_id, :task_id, :agent_name, :application_id, :application_name,
                    :application_path, :workflow_path, :yaml_path, :run_dir, :status,
                    :started_at, :ended_at, :task_text, :final_answer, :indexed_at, :metadata_json
                )
                """,
            run_meta,
        ).rowcount
        existing = None
        if not created:
            existing = conn.execute("SELECT * FROM runs WHERE run_id = ?", (record["run_id"],)).fetchone()
        if existing:
            merged = dict(existing)
            for key in (
                "root_run_id",
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
                    UPDATE runs SET root_run_id=:root_run_id,
                        task_id=:task_id, agent_name=:agent_name,
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

        # Ordinal is assigned inside the INSERT so two writers (threads or
        # processes) cannot race a separate SELECT MAX and collide.
        cursor = conn.execute(
            """
                INSERT OR IGNORE INTO events (
                    event_id, run_id, root_run_id, task_id, parent_task_id, parent_event_id,
                    application_id, application_name, application_path, workflow_path,
                    agent_name, worker_name, tool_name, event_type, phase, source, role,
                    status, step_number, input_json, output_json, content_text, content_ref,
                    source_path, created_at, ordinal, metadata_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                    (SELECT COALESCE(MAX(ordinal), -1) + 1 FROM events WHERE run_id = ?), ?)
                """,
            (
                record["event_id"],
                record["run_id"],
                run_meta["root_run_id"],
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
                record["run_id"],
                _json_dumps(record.get("metadata") or {}),
            ),
        )
        row_id = int(cursor.lastrowid) if cursor.rowcount else None
        return {
            "run_id": record["run_id"],
            "root_run_id": run_meta["root_run_id"],
            "event_id": record["event_id"],
            "indexed": row_id is not None,
            "db_path": str(self.db_path),
        }

    def append_event(self, event: CanonicalSessionEvent, *, root_run_id: str = "") -> dict[str, Any]:
        """Append one redacted canonical event to the ledger."""
        with self._connect() as conn:
            # Serialize the idempotency read with the run/event writes. Without
            # this boundary two processes could both miss the same event before
            # either writer creates it.
            conn.execute("BEGIN IMMEDIATE")
            return self._append_event_in_conn(conn, event, root_run_id=root_run_id)

    @staticmethod
    def _enqueue_job_in_conn(
        conn: sqlite3.Connection,
        *,
        kind: str,
        dedupe_key: str,
        root_run_id: str,
        payload: dict[str, Any],
        now: str,
    ) -> bool:
        kind = require_safe_identity(kind, field="job kind")
        dedupe_key = require_safe_identity(dedupe_key, field="job dedupe key")
        root_run_id = require_safe_identity(root_run_id, field="job root run id")
        return bool(
            conn.execute(
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
                    _json_dumps(sanitize_value_fragments(payload)),
                    now,
                    now,
                    now,
                ),
            ).rowcount
        )

    def finalize_session(
        self,
        event: CanonicalSessionEvent,
        *,
        root_run_id: str,
        succeeded: bool,
        review_payload: dict[str, Any],
        enqueue_review: bool = True,
        retention_dedupe_key: str = "",
    ) -> dict[str, Any]:
        """Atomically record SessionEnd, score memory once, and enqueue work."""
        root_run_id = require_safe_identity(
            root_run_id,
            field="SessionEnd root run id",
        )
        now = _now_iso()
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            event_result = self._append_event_in_conn(conn, event, root_run_id=root_run_id)
            # Root and leaf run ids are normally identical for the owner.  The
            # extra row makes the CAS fail-closed even if a malformed leaf
            # SessionEnd arrives before its root's first event.
            conn.execute(
                """
                INSERT OR IGNORE INTO runs (
                    run_id, root_run_id, status, indexed_at, metadata_json
                ) VALUES (?, ?, 'indexed', ?, '{}')
                """,
                (root_run_id, root_run_id, now),
            )
            outcome_claimed = bool(
                conn.execute(
                    """
                    UPDATE runs SET memory_outcome_recorded_at = ?
                    WHERE run_id = ? AND memory_outcome_recorded_at IS NULL
                    """,
                    (now, root_run_id),
                ).rowcount
            )
            trust_bumped = 0
            if outcome_claimed and succeeded:
                trust_bumped = int(
                    conn.execute(
                        """
                        UPDATE memory_items
                        SET trust_score = MIN(1.0, trust_score + 0.02),
                            updated_at = ?
                        WHERE id IN (
                            SELECT item_id FROM memory_injections WHERE run_id = ?
                        )
                        """,
                        (now, root_run_id),
                    ).rowcount
                )

            jobs_enqueued: list[str] = []
            if enqueue_review and self._enqueue_job_in_conn(
                conn,
                kind="session_review",
                dedupe_key=root_run_id,
                root_run_id=root_run_id,
                payload=review_payload,
                now=now,
            ):
                jobs_enqueued.append("session_review")
            if retention_dedupe_key and self._enqueue_job_in_conn(
                conn,
                kind="retention",
                dedupe_key=retention_dedupe_key,
                root_run_id=root_run_id,
                payload={
                    "root_run_id": root_run_id,
                    "run_dir": review_payload.get("run_dir", ""),
                    "retention_date": retention_dedupe_key,
                },
                now=now,
            ):
                jobs_enqueued.append("retention")
        return {
            **event_result,
            "outcome_recorded": outcome_claimed,
            "trust_bumped": trust_bumped,
            "jobs_enqueued": jobs_enqueued,
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
            0x3400 <= ord(ch) <= 0x9FFF or 0x3040 <= ord(ch) <= 0x30FF or 0xAC00 <= ord(ch) <= 0xD7AF for ch in text
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
            filters.append("COALESCE(NULLIF(e.root_run_id, ''), e.run_id) != ?")
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
                    if rows:
                        return [self._row_to_event_dict(row) for row in rows]
                    # Fall through to LIKE: trigram FTS silently returns nothing
                    # for queries under 3 chars (common for short CJK terms).
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

    def root_run_id_for(self, run_id: str) -> str:
        """Resolve a leaf run to its persisted root identity."""
        with self._connect() as conn:
            row = conn.execute("SELECT root_run_id FROM runs WHERE run_id = ?", (run_id,)).fetchone()
            if row is None:
                row = conn.execute(
                    "SELECT root_run_id FROM events WHERE run_id = ? ORDER BY id LIMIT 1",
                    (run_id,),
                ).fetchone()
        if row is None:
            return ""
        return str(row["root_run_id"] or run_id)

    def scroll_events(
        self, run_id: str, event_id: int, *, direction: str = "after", window: int = 5
    ) -> list[dict[str, Any]]:
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

    _PRUNE_CHUNK_ROWS = 2000

    def prune_events(self, *, retention_days: int = 90) -> dict[str, Any]:
        """Delete runs (and their events) whose last activity is older than the cutoff.

        Deletes commit in small chunks: each removed events row fires the FTS
        triggers, so one big transaction over a large backlog would hold the
        writer lock past every concurrent writer's busy_timeout.
        """
        cutoff = (datetime.now().astimezone() - timedelta(days=retention_days)).isoformat()
        with self._connect() as conn:
            stale_runs = [
                str(row["run_id"])
                for row in conn.execute(
                    "SELECT run_id FROM runs WHERE COALESCE(ended_at, started_at, indexed_at) < ?",
                    (cutoff,),
                )
            ]
            events_deleted = 0
            for run_id in stale_runs:
                while True:
                    deleted = conn.execute(
                        "DELETE FROM events WHERE id IN (SELECT id FROM events WHERE run_id = ? LIMIT ?)",
                        (run_id, self._PRUNE_CHUNK_ROWS),
                    ).rowcount
                    events_deleted += deleted
                    conn.commit()
                    if deleted < self._PRUNE_CHUNK_ROWS:
                        break
                conn.execute("DELETE FROM runs WHERE run_id = ?", (run_id,))
                conn.commit()
            reviews_deleted = conn.execute("DELETE FROM review_runs WHERE created_at < ?", (cutoff,)).rowcount
        return {
            "ok": True,
            "runs_pruned": len(stale_runs),
            "events_pruned": int(events_deleted),
            "reviews_pruned": int(reviews_deleted),
            "cutoff": cutoff,
        }

    def record_review(
        self,
        *,
        source_run_id: str,
        hook_event: str,
        application_id: str = "",
        trigger_event_id: str = "",
        output: dict[str, Any] | None = None,
        status: str = "proposal",
        learning_job_id: int | None = None,
    ) -> int:
        learning_job_id = require_optional_strict_int(
            learning_job_id,
            field="review learning_job_id",
        )
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
        trigger_event_id = require_safe_identity(
            trigger_event_id,
            field="review trigger event id",
            allow_empty=True,
        )
        status = require_safe_identity(status, field="review status")
        with self._connect() as conn:
            cursor = conn.execute(
                """
                INSERT OR IGNORE INTO review_runs (
                    source_run_id, trigger_event_id, hook_event, application_id,
                    status, output_json, created_at, learning_job_id
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    source_run_id,
                    trigger_event_id,
                    hook_event,
                    application_id,
                    status,
                    _json_dumps(sanitize_value_fragments(output or {})),
                    _now_iso(),
                    learning_job_id,
                ),
            )
            if cursor.rowcount:
                return int(cursor.lastrowid)
            if learning_job_id is None:
                raise RuntimeError("Review insert was ignored without a job id")
            row = conn.execute(
                "SELECT review_id FROM review_runs WHERE learning_job_id = ?",
                (learning_job_id,),
            ).fetchone()
            if row is None:
                raise RuntimeError("Review insert lost its idempotency row")
            return int(row["review_id"])

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
        proposal_id = require_safe_identity(
            proposal_id,
            field="skill proposal id",
        )
        application_id = require_safe_identity(
            application_id,
            field="skill proposal application id",
            allow_empty=True,
        )
        source_run_id = require_safe_identity(
            source_run_id,
            field="skill proposal source run id",
            allow_empty=True,
        )
        source_event_id = require_safe_identity(
            source_event_id,
            field="skill proposal source event id",
            allow_empty=True,
        )
        name = sanitize_text_fragment(name)
        action = sanitize_text_fragment(action)
        status = sanitize_text_fragment(status)
        proposal_path = sanitize_text_fragment(proposal_path)
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
                    _json_dumps(sanitize_value_fragments(manifest or {})),
                    now,
                    now,
                ),
            )

    def update_skill_proposal_status(self, proposal_id: str, status: str) -> None:
        proposal_id = require_safe_identity(
            proposal_id,
            field="skill proposal id",
        )
        status = require_safe_identity(status, field="skill proposal status")
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
    root_run_id TEXT,
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
    metadata_json TEXT,
    memory_outcome_recorded_at TEXT
);

CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id TEXT NOT NULL UNIQUE,
    run_id TEXT NOT NULL,
    root_run_id TEXT,
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
    content_hash TEXT,
    status TEXT NOT NULL,
    action TEXT NOT NULL,
    target TEXT,
    source TEXT,
    source_run_id TEXT,
    source_event_id TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    applied_at TEXT,
    trust_score REAL NOT NULL DEFAULT 0.5,
    injected_count INTEGER NOT NULL DEFAULT 0,
    last_injected_at TEXT,
    helpful_count INTEGER NOT NULL DEFAULT 0,
    unhelpful_count INTEGER NOT NULL DEFAULT 0,
    applied_by TEXT DEFAULT '',
    conflicts_json TEXT DEFAULT '',
    corroboration_runs_json TEXT DEFAULT '',
    generation INTEGER NOT NULL DEFAULT 1,
    supersedes_id INTEGER,
    target_item_id INTEGER
);

CREATE TABLE IF NOT EXISTS memory_evidence (
    item_id INTEGER NOT NULL,
    root_run_id TEXT NOT NULL,
    source TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    PRIMARY KEY (item_id, root_run_id)
);

CREATE TABLE IF NOT EXISTS memory_injections (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT NOT NULL,
    item_id INTEGER NOT NULL,
    injected_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS maintenance (
    key TEXT PRIMARY KEY,
    value TEXT
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
    created_at TEXT NOT NULL,
    learning_job_id INTEGER
);

CREATE TABLE IF NOT EXISTS learning_jobs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    kind TEXT NOT NULL,
    dedupe_key TEXT NOT NULL,
    root_run_id TEXT NOT NULL,
    payload_json TEXT NOT NULL DEFAULT '{}',
    status TEXT NOT NULL DEFAULT 'pending',
    attempts INTEGER NOT NULL DEFAULT 0,
    available_at TEXT NOT NULL,
    lease_owner TEXT,
    lease_token TEXT,
    lease_until TEXT,
    result_json TEXT,
    last_error TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    finished_at TEXT,
    UNIQUE (kind, dedupe_key)
);

CREATE TABLE IF NOT EXISTS learning_job_effects (
    job_id INTEGER NOT NULL,
    effect_key TEXT NOT NULL,
    effect_hash TEXT NOT NULL,
    effect_type TEXT NOT NULL,
    result_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (job_id, effect_key)
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
CREATE INDEX IF NOT EXISTS idx_events_agent ON events(agent_name);
CREATE INDEX IF NOT EXISTS idx_events_application ON events(application_id);
CREATE INDEX IF NOT EXISTS idx_events_created ON events(created_at);
CREATE INDEX IF NOT EXISTS idx_memory_scope ON memory_items(scope_type, scope_id);
CREATE INDEX IF NOT EXISTS idx_memory_status ON memory_items(status);
CREATE INDEX IF NOT EXISTS idx_memory_evidence_run ON memory_evidence(root_run_id);
CREATE INDEX IF NOT EXISTS idx_injections_run ON memory_injections(run_id);
CREATE INDEX IF NOT EXISTS idx_skill_proposals_status ON skill_proposals(status);
CREATE INDEX IF NOT EXISTS idx_review_runs_source ON review_runs(source_run_id);
CREATE INDEX IF NOT EXISTS idx_learning_jobs_ready ON learning_jobs(status, available_at, id);
CREATE INDEX IF NOT EXISTS idx_learning_jobs_root_run ON learning_jobs(root_run_id, id);
CREATE INDEX IF NOT EXISTS idx_learning_job_effects_job ON learning_job_effects(job_id, effect_key);
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
