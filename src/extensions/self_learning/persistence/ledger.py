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
from src.lib.trusted_memory_evidence import (
    TRUSTED_MEMORY_EVIDENCE_KIND,
    TRUSTED_MEMORY_EVIDENCE_RESPONSE_KEY,
)

from ..application_scope import resolve_legacy_application_id
from ..event_schema import (
    CanonicalSessionEvent,
    safe_run_id,
)
from ..paths import self_learning_db, session_events_dir
from ..redaction import (
    BLOCKED_TEXT,
    redact_text,
    require_safe_identity,
    safe_storage_identity,
    sanitize_text_fragment,
    sanitize_text_fragment_with_taint,
    sanitize_value_fragments,
    sanitize_value_fragments_with_taint,
)
from ..review_types import payload_hash
from .database import (
    SelfLearningDatabase,
    serialized_database_writer,
    serialized_write_transaction,
)

logger = get_logger(__name__)

_SCHEMA_VERSION = 6
_BUSY_TIMEOUT_MS = 5000
_SAFETY_TAINT_KEY = "_safety_tainted"
_V4_PHYSICAL_CLEANUP_KEY = "schema_v4_physical_cleanup"
_V4_CLEANUP_PENDING = "pending"
_V4_CLEANUP_COMPLETE = "complete"
_V4_SANITIZER_REVISION_KEY = "schema_v4_sanitizer_revision"
_V4_SANITIZER_REVISION = "5"
_V5_MIGRATION_REPORT_KEY = "schema_v5_simplified_memory_migration"
_V5_SANITIZER_REVISION_KEY = "schema_v5_sanitizer_revision"
_V5_SANITIZER_REVISION = "4"
_V5_PENDING_ADD_HASH_REVISION_KEY = "schema_v5_pending_add_hash_revision"
_V5_PENDING_ADD_HASH_REVISION = "2"
_V5_REVIEW_KEY_REVISION_KEY = "schema_v5_review_key_revision"
_V5_REVIEW_KEY_REVISION = "1"
_V6_MIGRATION_REPORT_KEY = "schema_v6_typed_review_migration"
_V5_PENDING_HASH_TRIGGERS = (
    "trg_memory_pending_add_require_hash_insert",
    "trg_memory_pending_add_require_hash_update",
)
_V5_REMOVED_TABLES = (
    "learning_job_effects",
    "learning_jobs",
    "memory_evidence",
    "memory_injections",
    "artifacts",
)
_V6_REMOVED_TABLES = (*_V5_REMOVED_TABLES, "memory_pending_writes", "review_runs")
_V6_REQUIRED_TABLES = (
    "memory_items",
    "review_batches",
    "review_batch_runs",
    "review_candidates",
    "review_mutations",
    "run_feedback",
)
_V5_REMOVED_MAINTENANCE_KEYS = (
    "learning_worker_lease",
    "learning_worker_kick_lease",
)
_FTS_TRIGGER_NAMES = (
    "events_fts_insert",
    "events_fts_delete",
    "events_fts_update",
    "events_fts_trigram_insert",
    "events_fts_trigram_delete",
    "events_fts_trigram_update",
)
_V4_PRIVACY_BASE_TABLES = (
    "runs",
    "events",
    "memory_items",
    "memory_evidence",
    "memory_injections",
    "maintenance",
    "skill_proposals",
    "review_runs",
    "learning_jobs",
    "learning_job_effects",
    "artifacts",
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


def _v5_memory_tables_sql(
    items_table: str,
    pending_table: str,
    *,
    if_not_exists: bool = False,
) -> str:
    """Build the canonical v5 curated-memory tables under internal names."""
    for table_name in (items_table, pending_table):
        if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", table_name) is None:
            raise ValueError(f"invalid internal table name: {table_name!r}")
    create_modifier = " IF NOT EXISTS" if if_not_exists else ""
    return f"""
CREATE TABLE{create_modifier} {items_table} (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    scope_type TEXT NOT NULL
        CHECK (scope_type IN ('project', 'application')),
    scope_id TEXT NOT NULL,
    content TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE (scope_type, scope_id, content_hash)
);

CREATE TABLE{create_modifier} {pending_table} (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    status TEXT NOT NULL
        CHECK (status IN ('pending', 'approved', 'rejected', 'stale')),
    action TEXT NOT NULL
        CHECK (action IN ('add', 'replace', 'remove')),
    scope_type TEXT NOT NULL
        CHECK (scope_type IN ('project', 'application')),
    scope_id TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    content_hash TEXT,
    source_run_id TEXT NOT NULL,
    created_at TEXT NOT NULL,
    resolved_at TEXT
);
"""


def _trusted_review_evidence_table_sql(
    table_name: str,
    *,
    if_not_exists: bool = False,
) -> str:
    """Build the canonical non-importable reviewer-evidence table."""
    if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", table_name) is None:
        raise ValueError(f"invalid internal table name: {table_name!r}")
    create_modifier = " IF NOT EXISTS" if if_not_exists else ""
    return f"""
CREATE TABLE{create_modifier} {table_name} (
    event_id TEXT NOT NULL,
    root_run_id TEXT NOT NULL,
    tool_name TEXT NOT NULL,
    kind TEXT NOT NULL CHECK (kind = 'durable_fact'),
    scope_type TEXT NOT NULL
        CHECK (scope_type IN ('project', 'application')),
    scope_id TEXT NOT NULL,
    source TEXT NOT NULL,
    text TEXT NOT NULL,
    created_at TEXT NOT NULL,
    CHECK (
        (scope_type = 'project' AND scope_id = 'project')
        OR (scope_type = 'application' AND length(scope_id) > 0)
    ),
    PRIMARY KEY (event_id, kind, scope_type, scope_id, source, text)
);
"""


def _v6_review_tables_sql(
    *,
    memory_items: str = "memory_items",
    review_batches: str = "review_batches",
    review_batch_runs: str = "review_batch_runs",
    review_candidates: str = "review_candidates",
    review_mutations: str = "review_mutations",
    run_feedback: str = "run_feedback",
    if_not_exists: bool = False,
) -> str:
    """Build the canonical typed-memory and review tables."""
    table_names = (
        memory_items,
        review_batches,
        review_batch_runs,
        review_candidates,
        review_mutations,
        run_feedback,
    )
    for table_name in table_names:
        if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", table_name) is None:
            raise ValueError(f"invalid internal table name: {table_name!r}")
    create_modifier = " IF NOT EXISTS" if if_not_exists else ""
    return f"""
CREATE TABLE{create_modifier} {memory_items} (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    scope_type TEXT NOT NULL CHECK (scope_type IN ('project', 'application')),
    scope_id TEXT NOT NULL,
    kind TEXT NOT NULL CHECK (kind IN ('fact', 'experience')),
    memory_key TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    payload_hash TEXT NOT NULL,
    state TEXT NOT NULL CHECK (
        state IN ('active_unreviewed', 'active_confirmed', 'retracted', 'shadowed')
    ),
    activation_source TEXT NOT NULL CHECK (
        activation_source IN ('auto', 'manual', 'migration', 'admin')
    ),
    provenance_json TEXT NOT NULL DEFAULT '[]',
    revision INTEGER NOT NULL DEFAULT 1 CHECK (revision > 0),
    source_review_id TEXT,
    supersedes_id INTEGER,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    CHECK (
        (scope_type = 'project' AND scope_id = 'project')
        OR (scope_type = 'application' AND length(scope_id) > 0)
    ),
    UNIQUE (scope_type, scope_id, kind, memory_key, revision)
);

CREATE TABLE{create_modifier} {review_batches} (
    review_id TEXT PRIMARY KEY,
    scope_type TEXT NOT NULL CHECK (scope_type IN ('project', 'application')),
    scope_id TEXT NOT NULL,
    status TEXT NOT NULL CHECK (
        status IN ('completed', 'failed', 'rolled_back', 'dry_run')
    ),
    dry_run INTEGER NOT NULL DEFAULT 0 CHECK (dry_run IN (0, 1)),
    result_json TEXT NOT NULL DEFAULT '{{}}',
    created_at TEXT NOT NULL,
    finished_at TEXT NOT NULL,
    CHECK (
        (scope_type = 'project' AND scope_id = 'project')
        OR (scope_type = 'application' AND length(scope_id) > 0)
    )
);

CREATE TABLE{create_modifier} {review_batch_runs} (
    review_id TEXT NOT NULL,
    root_run_id TEXT NOT NULL,
    application_id TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (review_id, root_run_id, application_id)
);

CREATE TABLE{create_modifier} {review_candidates} (
    candidate_id TEXT PRIMARY KEY,
    review_id TEXT NOT NULL,
    scope_type TEXT NOT NULL CHECK (scope_type IN ('project', 'application')),
    scope_id TEXT NOT NULL,
    kind TEXT NOT NULL CHECK (kind IN ('fact', 'experience')),
    memory_key TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    payload_hash TEXT NOT NULL,
    proposed_action TEXT NOT NULL CHECK (
        proposed_action IN ('add', 'replace', 'remove', 'promote_project')
    ),
    approval TEXT NOT NULL CHECK (approval IN ('auto', 'manual')),
    state TEXT NOT NULL CHECK (
        state IN (
            'quarantined', 'pending_pre_review', 'active_unreviewed',
            'active_confirmed', 'rejected', 'retracted', 'dry_run'
        )
    ),
    outcome TEXT NOT NULL,
    revision INTEGER NOT NULL DEFAULT 1 CHECK (revision > 0),
    target_item_id INTEGER,
    provenance_json TEXT NOT NULL DEFAULT '[]',
    source_run_ids_json TEXT NOT NULL DEFAULT '[]',
    gate_reasons_json TEXT NOT NULL DEFAULT '[]',
    reason TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    resolved_at TEXT
);

CREATE TABLE{create_modifier} {review_mutations} (
    mutation_id TEXT PRIMARY KEY,
    review_id TEXT NOT NULL,
    candidate_id TEXT,
    memory_item_id INTEGER NOT NULL,
    operation TEXT NOT NULL CHECK (operation IN ('insert', 'state', 'provenance')),
    before_json TEXT,
    after_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    rolled_back_at TEXT
);

CREATE TABLE{create_modifier} {run_feedback} (
    feedback_id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL,
    verdict TEXT NOT NULL CHECK (verdict IN ('accepted', 'rejected', 'corrected')),
    item_id INTEGER,
    application_id TEXT NOT NULL DEFAULT '',
    correction_json TEXT,
    created_at TEXT NOT NULL
);
"""


class SelfLearningLedger:
    """DB-first source of truth for history, curated memory, and reviews."""

    # DDL + migrations run once per (process, db_path); the recorder constructs
    # a ledger on every hook event, so re-running schema bootstrap there would
    # put schema churn on the synchronous tool path.
    _initialized_paths: set[str] = set()
    _init_lock = threading.Lock()

    def __init__(self, db_path: str | Path | None = None):
        self.db_path = Path(db_path).resolve() if db_path else self_learning_db()
        self._database = SelfLearningDatabase(self.db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        key = str(self.db_path)
        if key not in self._initialized_paths:
            with self._init_lock:
                if key not in self._initialized_paths:
                    self._init_db()
                    self._initialized_paths.add(key)
        else:
            # Process-local initialization cannot prove that a stale v4
            # process did not recreate removed state afterwards.  The hot path
            # performs one narrow read and only re-enters the locked migration
            # when the latest schema invariant was actually violated.
            self._remove_legacy_memory_artifacts()
            if self._has_removed_v5_state():
                with self._init_lock:
                    if self._has_removed_v5_state():
                        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        return self._database.connect()

    @staticmethod
    def _read_event_file(path: Path) -> list[CanonicalSessionEvent]:
        events: list[CanonicalSessionEvent] = []
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except OSError:
            return events
        for line in lines:
            raw = line.strip()
            if not raw:
                continue
            try:
                data = json.loads(raw)
            except json.JSONDecodeError:
                logger.warning(
                    "Skipping malformed self-learning event export line in %s",
                    path,
                )
                continue
            if isinstance(data, dict):
                events.append(CanonicalSessionEvent.from_record(data))
        return [event for event in events if event.run_id]

    @staticmethod
    def _event_file_for_run(run_id: str) -> Path:
        safe_name = "".join(
            ch if ch.isalnum() or ch in "._-" else "_" for ch in str(run_id)
        )
        return session_events_dir() / f"{safe_name}.jsonl"

    @classmethod
    def _event_files(cls, target: str | Path | None = None) -> list[Path]:
        if target is None:
            root = session_events_dir()
            return sorted(root.glob("*.jsonl")) if root.exists() else []
        path = Path(target).expanduser()
        if not path.is_absolute():
            path = Path.cwd() / path
        path = path.resolve()
        if path.is_file():
            return [path]
        if path.is_dir():
            return sorted(path.glob("*.jsonl"))
        run_file = cls._event_file_for_run(str(target))
        return [run_file] if run_file.exists() else []

    def index_run(self, target: str | Path) -> dict[str, Any]:
        """Import one canonical JSONL event export into this ledger."""
        files = self._event_files(target)
        if len(files) != 1:
            raise FileNotFoundError(
                "Expected one canonical event export file, "
                f"found {len(files)} for {target}"
            )
        event_file = files[0]
        events = self._read_event_file(event_file)
        if not events:
            return {
                "run_id": "",
                "events_indexed": 0,
                "db_path": str(self.db_path),
                "event_file": str(event_file),
            }
        run_id = events[0].run_id
        self.delete_run(run_id)
        inserted = sum(
            1 for event in events if self.append_event(event).get("indexed")
        )
        return {
            "run_id": run_id,
            "events_indexed": inserted,
            "db_path": str(self.db_path),
            "event_file": str(event_file),
        }

    def index_all(self, events_root: str | Path | None = None) -> dict[str, Any]:
        """Import JSONL exports, or report current persisted event counts."""
        if events_root is None:
            return self.count_events()
        runs = 0
        events = 0
        for path in self._event_files(events_root):
            stats = self.index_run(path)
            if stats.get("run_id"):
                runs += 1
            events += int(stats.get("events_indexed") or 0)
        return {
            "runs_indexed": runs,
            "events_indexed": events,
            "db_path": str(self.db_path),
        }

    def _init_db(self) -> None:
        with serialized_database_writer(self.db_path):
            with self._connect() as conn:
                self._enable_wal_mode(conn)
                self._run_migrations(conn)
        self._remove_legacy_memory_artifacts()

    def _remove_legacy_memory_artifacts(self) -> None:
        """Delete pre-v5 memory mirrors after the database is authoritative.

        Keeping a second mutable copy creates stale-fact, cross-process, and
        secret-retention races.  ``loom memory export`` is the supported human
        view in v5, so the known generated mirror filenames are obsolete.
        """
        memory_root = self.db_path.parent / "memory"
        if not memory_root.exists() or memory_root.is_symlink():
            return
        candidates = [memory_root / "MEMORY.md"]
        applications_root = memory_root / "applications"
        if applications_root.is_dir() and not applications_root.is_symlink():
            candidates.extend(applications_root.glob("*.md"))
        candidates.extend(
            memory_root / name
            for name in ("memory.db", "memory.db-wal", "memory.db-shm")
        )
        for path in candidates:
            # Never follow a link out of the state directory.  These exact
            # filenames are the only mirrors generated by pre-v5 code.
            if path.is_file() and not path.is_symlink():
                path.unlink()

    def _has_removed_v5_state(self) -> bool:
        """Return whether cached initialization has been invalidated."""
        if not self.db_path.exists():
            return True
        try:
            with self._connect() as conn:
                current = int(
                    conn.execute(
                        "SELECT COALESCE(MAX(version), 0) FROM schema_version"
                    ).fetchone()[0]
                )
                if current != _SCHEMA_VERSION:
                    return True
                existing_tables = {
                    str(row[0])
                    for row in conn.execute(
                        "SELECT name FROM sqlite_master WHERE type='table'"
                    )
                }
                if not set(_V6_REQUIRED_TABLES).issubset(existing_tables):
                    return True
                if not self._trusted_review_evidence_schema_is_canonical(conn):
                    return True
                forbidden_tables = conn.execute(
                    "SELECT 1 FROM sqlite_master "
                    "WHERE type='table' AND name IN (?, ?, ?, ?, ?, ?, ?) LIMIT 1",
                    _V6_REMOVED_TABLES,
                ).fetchone()
                if forbidden_tables is not None:
                    return True
                maintenance_exists = conn.execute(
                    "SELECT 1 FROM sqlite_master "
                    "WHERE type='table' AND name='maintenance'"
                ).fetchone()
                if maintenance_exists is None:
                    return True
                sanitizer_revision = conn.execute(
                    "SELECT value FROM maintenance WHERE key = ?",
                    (_V5_SANITIZER_REVISION_KEY,),
                ).fetchone()
                if (
                    sanitizer_revision is None
                    or str(sanitizer_revision["value"] or "")
                    != _V5_SANITIZER_REVISION
                ):
                    return True
                placeholders = ", ".join(
                    "?" for _ in _V5_REMOVED_MAINTENANCE_KEYS
                )
                return conn.execute(
                    f"SELECT 1 FROM maintenance WHERE key IN ({placeholders}) LIMIT 1",
                    _V5_REMOVED_MAINTENANCE_KEYS,
                ).fetchone() is not None
        except sqlite3.Error:
            return True

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

    @staticmethod
    def _trusted_review_evidence_schema_is_canonical(
        conn: sqlite3.Connection,
    ) -> bool:
        expected_columns = [
            ("event_id", "TEXT", 1, None, 1),
            ("root_run_id", "TEXT", 1, None, 0),
            ("tool_name", "TEXT", 1, None, 0),
            ("kind", "TEXT", 1, None, 2),
            ("scope_type", "TEXT", 1, None, 3),
            ("scope_id", "TEXT", 1, None, 4),
            ("source", "TEXT", 1, None, 5),
            ("text", "TEXT", 1, None, 6),
            ("created_at", "TEXT", 1, None, 0),
        ]
        actual_columns = [
            (str(row[1]), str(row[2]).upper(), int(row[3]), row[4], int(row[5]))
            for row in conn.execute(
                "PRAGMA table_info(trusted_review_evidence)"
            )
        ]
        if actual_columns != expected_columns:
            return False
        schema_row = conn.execute(
            "SELECT sql FROM sqlite_master "
            "WHERE type = 'table' AND name = 'trusted_review_evidence'"
        ).fetchone()
        if schema_row is None:
            return False
        kind_constraint = re.search(
            r"CHECK\s*\(\s*kind\s*=\s*'durable_fact'\s*\)",
            str(schema_row[0] or ""),
            flags=re.IGNORECASE,
        ) is not None
        scope_constraint = re.search(
            r"scope_type\s+IN\s*\(\s*'project'\s*,\s*'application'\s*\)",
            str(schema_row[0] or ""),
            flags=re.IGNORECASE,
        ) is not None
        project_binding = re.search(
            r"scope_type\s*=\s*'project'\s+AND\s+scope_id\s*=\s*'project'",
            str(schema_row[0] or ""),
            flags=re.IGNORECASE,
        ) is not None
        return kind_constraint and scope_constraint and project_binding

    @classmethod
    def _rebuild_trusted_review_evidence_schema(
        cls,
        conn: sqlite3.Connection,
    ) -> None:
        """Replace a development-era table without trusting its rows."""
        cls._replace_trusted_review_evidence_table(conn, ())

    @classmethod
    def _replace_trusted_review_evidence_table(
        cls,
        conn: sqlite3.Connection,
        rows: tuple[
            tuple[str, str, str, str, str, str, str, str, str], ...
        ],
    ) -> None:
        """Install the canonical evidence table from already-validated rows."""
        replacement = "trusted_review_evidence_v5_canonical"
        conn.execute(f"DROP TABLE IF EXISTS {replacement}")
        cls._execute_script_in_transaction(
            conn,
            _trusted_review_evidence_table_sql(replacement),
        )
        conn.executemany(
            f"""
            INSERT OR IGNORE INTO {replacement} (
                event_id, root_run_id, tool_name, kind, scope_type, scope_id,
                source, text, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            rows,
        )
        conn.execute("DROP TABLE trusted_review_evidence")
        conn.execute(
            f"ALTER TABLE {replacement} RENAME TO trusted_review_evidence"
        )
        conn.execute(
            "CREATE INDEX idx_trusted_review_evidence_root "
            "ON trusted_review_evidence(root_run_id, event_id)"
        )

    def _run_migrations(self, conn: sqlite3.Connection) -> None:
        # Schema upgrades are process-safe as well as thread-safe.  In
        # particular, two short-lived ``loom run`` processes may discover the
        # same old database concurrently; only one is allowed to inspect and
        # mutate its shape at a time.
        # Secure deletion must be enabled before legacy rows/FTS segments are
        # rewritten.  The post-commit truncate below then removes superseded
        # WAL frames instead of leaving raw historical credentials on disk.
        conn.execute("PRAGMA secure_delete=ON")
        secure_delete = int(conn.execute("PRAGMA secure_delete").fetchone()[0])
        if secure_delete != 1:
            raise sqlite3.OperationalError(
                "SQLite refused the secure_delete privacy boundary"
            )
        # SQLite WAL frames contain complete *new* page images.  With cache
        # spill enabled, a long migration can flush a page after only one of
        # its rows/columns was cleaned, transiently copying neighbouring
        # legacy secrets into WAL. Hold every dirty page until the transaction
        # reaches its fully sanitized final state.
        conn.execute("PRAGMA cache_spill=OFF")
        cache_spill = conn.execute("PRAGMA cache_spill").fetchone()
        if cache_spill is None or int(cache_spill[0]) != 0:
            raise sqlite3.OperationalError(
                "SQLite refused the cache_spill privacy boundary"
            )
        conn.execute("BEGIN IMMEDIATE")
        migrated_v4 = False
        migrated_v5 = False
        migrated_v6 = False
        refreshed_v4_sanitizer = False
        refreshed_v5_sanitizer = False
        rebuilt_trusted_evidence_schema = False
        requires_v4_physical_cleanup = False
        try:
            existing_schema_objects = {
                str(row[0])
                for row in conn.execute("SELECT name FROM sqlite_master")
            }
            if "schema_version" in existing_schema_objects:
                current = int(
                    conn.execute(
                        "SELECT COALESCE(MAX(version), 0) FROM schema_version"
                    ).fetchone()[0]
                )
            else:
                current = 0
            if current > _SCHEMA_VERSION:
                raise RuntimeError(
                    "unsupported self-learning schema version "
                    f"{current}; this binary supports up to {_SCHEMA_VERSION}"
                )
            # ``sqlite3.Connection.executescript`` commits an open transaction
            # before executing. Run every bootstrap/repair statement through
            # ``execute`` so fresh-schema DDL, FTS objects, and versioned data
            # migration are all fenced by the same process-wide write lock.
            self._execute_script_in_transaction(
                conn,
                (
                    _SCHEMA_V6_SQL
                    if current >= 6
                    else _SCHEMA_V5_SQL
                    if current >= 5
                    else _SCHEMA_SQL
                ),
            )
            if current >= 5 and not self._trusted_review_evidence_schema_is_canonical(
                conn
            ):
                # Development-era rows without the canonical kind constraint
                # cannot prove that tool code classified them as durable.
                self._rebuild_trusted_review_evidence_schema(conn)
                rebuilt_trusted_evidence_schema = True
            available_fts_scripts: list[str] = []
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
            cleanup_row = conn.execute(
                "SELECT value FROM maintenance WHERE key = ?",
                (_V4_PHYSICAL_CLEANUP_KEY,),
            ).fetchone()
            cleanup_state = str(cleanup_row["value"] or "") if cleanup_row else ""
            v5_sanitizer_row = conn.execute(
                "SELECT value FROM maintenance WHERE key = ?",
                (_V5_SANITIZER_REVISION_KEY,),
            ).fetchone()
            v5_sanitizer_revision = (
                str(v5_sanitizer_row["value"] or "")
                if v5_sanitizer_row
                else ""
            )
            # A legacy sanitizer marker is mutable input, not a privacy proof.
            # Every v4 -> v5 upgrade therefore re-sanitizes retained rows and
            # rebuilds FTS even when the old database claims the latest marker.
            sanitizing_legacy = current < 5
            requires_v5_event_sanitizer = (
                current < 5
                or v5_sanitizer_revision != _V5_SANITIZER_REVISION
            )
            requires_v5_memory_sanitizer = (
                current < 6 and requires_v5_event_sanitizer
            )
            sanitizing_storage = sanitizing_legacy or requires_v5_event_sanitizer
            if sanitizing_storage:
                # UPDATE triggers would mirror intermediate event rows into
                # FTS. Rebuild the indexes from final sanitized base rows and
                # only then restore trigger maintenance.
                for trigger_name in _FTS_TRIGGER_NAMES:
                    conn.execute(
                        f'DROP TRIGGER IF EXISTS main."{trigger_name}"'
                    )
                for trigger_name in _V5_PENDING_HASH_TRIGGERS:
                    conn.execute(
                        f'DROP TRIGGER IF EXISTS main."{trigger_name}"'
                    )
                self._assert_no_v4_migration_triggers(conn)
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
            if current < 5:
                # Do not trust the legacy marker checked above.  This second
                # boundary is intentionally unconditional after the v4 shape
                # exists, including for databases just migrated from v1-v3.
                self._sanitize_v4_identities(conn)
                self._sanitize_v4_rows(conn)
                refreshed_v4_sanitizer = True
            if sanitizing_legacy:
                # Updating visible rows is insufficient: a legacy page can
                # retain bytes from rows deleted while secure_delete was OFF.
                # Rewrite every privacy-bearing base table from its final
                # sanitized values, then rebuild all ordinary indexes. With
                # cache spill fenced off, only zeroed or sanitized final page
                # images can reach the migration WAL at commit.
                self._rewrite_v4_base_tables(conn)
                conn.execute("REINDEX")
            if current < 5:
                self._migrate_v5_simplified_memory(conn)
                conn.execute("INSERT INTO schema_version (version) VALUES (5)")
                migrated_v5 = True
            if requires_v5_event_sanitizer:
                self._sanitize_v5_event_sequences(conn)
                self._sanitize_v5_trusted_review_evidence(conn)
                refreshed_v5_sanitizer = True
            if requires_v5_memory_sanitizer:
                self._sanitize_v5_memory_state(conn)
            if current < 6 and self._v5_pending_add_hash_repair_required(conn):
                self._repair_v5_pending_add_hashes(conn)
                conn.execute(
                    """
                    INSERT INTO maintenance (key, value) VALUES (?, ?)
                    ON CONFLICT(key) DO UPDATE SET value = excluded.value
                    """,
                    (
                        _V5_PENDING_ADD_HASH_REVISION_KEY,
                        _V5_PENDING_ADD_HASH_REVISION,
                    ),
                )
            if sanitizing_storage:
                # These scripts succeeded before trigger removal. A restore
                # failure is therefore corruption of an available index path,
                # not an optional-capability miss: let the outer transaction
                # roll back both the trigger drops and sanitizer marker.
                for fts_script in available_fts_scripts:
                    self._execute_script_in_transaction(conn, fts_script)
            removed_v5_legacy_state = self._drop_v5_removed_state(
                conn,
                include_v6_removed=current >= 6,
            )
            # Pre-release v5 builds persisted a ``running`` claim before the
            # model call. A process crash made that row permanent. Reviews now
            # use an OS lock and persist only terminal audits, so no durable
            # non-terminal state may survive migration or initialization.
            if current < 6:
                conn.execute(
                    "UPDATE review_runs SET status = 'skipped' "
                    "WHERE model_type = 'legacy' AND status = 'legacy'"
                )
                conn.execute(
                    "DELETE FROM review_runs "
                    "WHERE status NOT IN ('completed', 'failed', 'skipped')"
                )
                if self._v5_review_key_repair_required(conn):
                    self._repair_v5_review_keys(conn)
                    conn.execute(
                        """
                        INSERT INTO maintenance (key, value) VALUES (?, ?)
                        ON CONFLICT(key) DO UPDATE SET value = excluded.value
                        """,
                        (_V5_REVIEW_KEY_REVISION_KEY, _V5_REVIEW_KEY_REVISION),
                    )
            conn.execute(
                """
                INSERT INTO maintenance (key, value) VALUES (?, ?)
                ON CONFLICT(key) DO UPDATE SET value = excluded.value
                """,
                (_V4_SANITIZER_REVISION_KEY, _V4_SANITIZER_REVISION),
            )
            conn.execute(
                """
                INSERT INTO maintenance (key, value) VALUES (?, ?)
                ON CONFLICT(key) DO UPDATE SET value = excluded.value
                """,
                (_V5_SANITIZER_REVISION_KEY, _V5_SANITIZER_REVISION),
            )
            if current < 6:
                self._migrate_v6_typed_review(conn)
                conn.execute("INSERT INTO schema_version (version) VALUES (6)")
                migrated_v6 = True
            # ``schema_version=4`` proves only that logical redaction committed.
            # A checkpoint can still fail afterwards while an older reader pins
            # pre-redaction WAL frames. Persist the physical-cleanup obligation
            # in the same transaction so every later process retries it.
            requires_v4_physical_cleanup = (
                migrated_v4
                or migrated_v5
                or migrated_v6
                or refreshed_v4_sanitizer
                or refreshed_v5_sanitizer
                or rebuilt_trusted_evidence_schema
                or removed_v5_legacy_state
                or cleanup_state != _V4_CLEANUP_COMPLETE
            )
            if requires_v4_physical_cleanup:
                self._set_v4_cleanup_state(conn, _V4_CLEANUP_PENDING)
            # Root-scoped review/history is the real query path; no event query
            # is keyed by task_id. Replace the obsolete write-amplifying index
            # on existing databases as well as fresh schemas.
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
    def _drop_v5_removed_state(
        conn: sqlite3.Connection,
        *,
        include_v6_removed: bool = False,
    ) -> bool:
        """Keep deleted outbox state absent even after a stale v4 writer.

        Mixed-version processes can recreate an old table after the v5
        transition.  Enforce the v5 shape on every initialization and report
        whether physical cleanup is required for the discarded pages.
        """
        changed = False
        existing = {
            str(row[0])
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
        removed_tables = _V6_REMOVED_TABLES if include_v6_removed else _V5_REMOVED_TABLES
        for table in removed_tables:
            if table in existing:
                conn.execute(f'DROP TABLE "{table}"')
                changed = True
        placeholders = ", ".join("?" for _ in _V5_REMOVED_MAINTENANCE_KEYS)
        cursor = conn.execute(
            f"DELETE FROM maintenance WHERE key IN ({placeholders})",
            _V5_REMOVED_MAINTENANCE_KEYS,
        )
        return changed or cursor.rowcount > 0

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
    def _v5_pending_add_hash_repair_required(conn: sqlite3.Connection) -> bool:
        revision = conn.execute(
            "SELECT value FROM maintenance WHERE key = ?",
            (_V5_PENDING_ADD_HASH_REVISION_KEY,),
        ).fetchone()
        if (
            revision is None
            or str(revision["value"] or "") != _V5_PENDING_ADD_HASH_REVISION
        ):
            return True
        columns = {
            str(row[1])
            for row in conn.execute("PRAGMA table_info(memory_pending_writes)")
        }
        if "content_hash" not in columns:
            return True
        index = conn.execute(
            "SELECT 1 FROM sqlite_master "
            "WHERE type='index' AND name='idx_memory_pending_add_dedup'",
        ).fetchone()
        if index is None:
            return True
        placeholders = ", ".join("?" for _ in _V5_PENDING_HASH_TRIGGERS)
        triggers = {
            str(row["name"])
            for row in conn.execute(
                f"SELECT name FROM sqlite_master "
                f"WHERE type='trigger' AND name IN ({placeholders})",
                _V5_PENDING_HASH_TRIGGERS,
            )
        }
        return triggers != set(_V5_PENDING_HASH_TRIGGERS)

    @staticmethod
    def _v5_review_key_repair_required(conn: sqlite3.Connection) -> bool:
        revision = conn.execute(
            "SELECT value FROM maintenance WHERE key = ?",
            (_V5_REVIEW_KEY_REVISION_KEY,),
        ).fetchone()
        return (
            revision is None
            or str(revision["value"] or "") != _V5_REVIEW_KEY_REVISION
        )

    @staticmethod
    def _repair_v5_review_keys(conn: sqlite3.Connection) -> None:
        """Quarantine terminal root keys that were bound by old v5 callers."""
        occupied = {
            str(row["review_key"]): int(row["review_id"])
            for row in conn.execute(
                "SELECT review_id, review_key FROM review_runs"
            )
        }
        rows = conn.execute(
            """
            SELECT review_id, review_key, root_run_id
            FROM review_runs
            WHERE status IN ('completed', 'failed', 'skipped')
              AND review_key GLOB 'root:*'
            ORDER BY review_id
            """
        ).fetchall()
        for row in rows:
            review_id = int(row["review_id"])
            old_key = str(row["review_key"])
            if old_key == f"root:{str(row['root_run_id'])}":
                continue
            base = f"legacy-mismatch:{review_id}"
            candidate = base
            suffix = 0
            while (
                candidate in occupied
                and occupied[candidate] != review_id
            ):
                suffix += 1
                candidate = f"{base}:{suffix}"
            conn.execute(
                "UPDATE review_runs SET review_key = ? WHERE review_id = ?",
                (candidate, review_id),
            )
            if occupied.get(old_key) == review_id:
                del occupied[old_key]
            occupied[candidate] = review_id

    @classmethod
    def _repair_v5_pending_add_hashes(cls, conn: sqlite3.Connection) -> None:
        """Make approval-add deduplication indexed and deterministic."""
        conn.execute("DROP INDEX IF EXISTS idx_memory_pending_add_dedup")
        cls._add_column_if_missing(
            conn,
            "memory_pending_writes",
            "content_hash",
            "content_hash TEXT",
        )
        active_hashes = {
            (str(row["scope_type"]), str(row["scope_id"]), str(row["content_hash"]))
            for row in conn.execute(
                "SELECT scope_type, scope_id, content_hash FROM memory_items"
            )
        }
        seen_pending: set[tuple[str, str, str]] = set()
        now = _now_iso()
        rows = conn.execute(
            """
            SELECT id, status, action, scope_type, scope_id, payload_json
            FROM memory_pending_writes
            ORDER BY id
            """
        ).fetchall()
        for row in rows:
            if str(row["action"]) != "add":
                conn.execute(
                    "UPDATE memory_pending_writes SET content_hash=NULL WHERE id=?",
                    (int(row["id"]),),
                )
                continue
            try:
                payload = json.loads(str(row["payload_json"] or "{}"))
            except (TypeError, ValueError, json.JSONDecodeError):
                payload = {}
            content = str(payload.get("content") or "") if isinstance(payload, dict) else ""
            content_hash = memory_content_hash(content) if content else None
            status = str(row["status"])
            scope_key = (
                str(row["scope_type"]),
                str(row["scope_id"]),
                str(content_hash or ""),
            )
            if status == "pending" and (
                content_hash is None
                or scope_key in active_hashes
                or scope_key in seen_pending
            ):
                conn.execute(
                    """
                    UPDATE memory_pending_writes
                    SET status='stale', content_hash=?, resolved_at=?
                    WHERE id=?
                    """,
                    (content_hash, now, int(row["id"])),
                )
                continue
            conn.execute(
                "UPDATE memory_pending_writes SET content_hash=? WHERE id=?",
                (content_hash, int(row["id"])),
            )
            if status == "pending":
                seen_pending.add(scope_key)
        conn.execute(
            """
            CREATE UNIQUE INDEX IF NOT EXISTS idx_memory_pending_add_dedup
            ON memory_pending_writes(scope_type, scope_id, content_hash)
            WHERE status='pending' AND action='add' AND content_hash IS NOT NULL
            """
        )
        conn.execute(
            """
            CREATE TRIGGER IF NOT EXISTS trg_memory_pending_add_require_hash_insert
            BEFORE INSERT ON memory_pending_writes
            WHEN NEW.status='pending' AND NEW.action='add'
                 AND NEW.content_hash IS NULL
            BEGIN
                SELECT RAISE(ABORT, 'pending add content hash required');
            END
            """
        )
        conn.execute(
            """
            CREATE TRIGGER IF NOT EXISTS trg_memory_pending_add_require_hash_update
            BEFORE UPDATE OF status, action, content_hash ON memory_pending_writes
            WHEN NEW.status='pending' AND NEW.action='add'
                 AND NEW.content_hash IS NULL
            BEGIN
                SELECT RAISE(ABORT, 'pending add content hash required');
            END
            """
        )

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

    @staticmethod
    def _quote_identifier(value: str) -> str:
        return '"' + str(value).replace('"', '""') + '"'

    @staticmethod
    def _assert_no_v4_migration_triggers(conn: sqlite3.Connection) -> None:
        trigger_count = int(
            conn.execute(
                "SELECT ("
                "SELECT COUNT(*) FROM main.sqlite_master WHERE type = 'trigger'"
                ") + ("
                "SELECT COUNT(*) FROM temp.sqlite_master WHERE type = 'trigger'"
                ")"
            ).fetchone()[0]
        )
        if trigger_count:
            raise sqlite3.OperationalError(
                "unexpected trigger remained during v4 table rewrite "
                f"(count={trigger_count})"
            )

    @classmethod
    def _rewrite_v4_base_tables(cls, conn: sqlite3.Connection) -> None:
        """Physically rewrite sanitized rows without changing public identity.

        ``secure_delete`` cannot retroactively clean free space created by a
        legacy connection. Each table is therefore copied only after logical
        sanitization, cleared under secure deletion, and restored with its
        original rowid. The temporary copy contains sanitized values only.
        """
        privacy_tables = set(_V4_PRIVACY_BASE_TABLES)
        cls._assert_no_v4_migration_triggers(conn)

        existing_tables = {
            str(row[0])
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
        missing = privacy_tables - existing_tables
        if missing:
            raise sqlite3.OperationalError(
                "v4 privacy table missing during rewrite: "
                + ", ".join(sorted(missing))
            )

        for table in _V4_PRIVACY_BASE_TABLES:
            quoted_table = cls._quote_identifier(table)
            column_rows = conn.execute(
                f"PRAGMA table_xinfo({quoted_table})"
            ).fetchall()
            writable_columns = [
                str(row[1]) for row in column_rows if int(row[6] or 0) == 0
            ]
            if not writable_columns:
                raise sqlite3.OperationalError(
                    f"v4 privacy table has no writable columns: {table}"
                )
            primary_key_columns = [row for row in column_rows if int(row[5] or 0) > 0]
            has_rowid_alias = (
                len(primary_key_columns) == 1
                and str(primary_key_columns[0][2] or "").strip().upper()
                == "INTEGER"
            )
            quoted_columns = [
                cls._quote_identifier(column) for column in writable_columns
            ]
            temp_name = f"v4_rewrite_{table}"
            quoted_temp = cls._quote_identifier(temp_name)
            projection = ", ".join(quoted_columns)
            target_columns = list(quoted_columns)
            if not has_rowid_alias:
                projection = f"rowid AS __v4_rowid, {projection}"
                target_columns.insert(0, "rowid")
            before = int(
                conn.execute(f"SELECT COUNT(*) FROM {quoted_table}").fetchone()[0]
            )
            conn.execute(f"DROP TABLE IF EXISTS temp.{quoted_temp}")
            try:
                conn.execute(
                    f"CREATE TEMP TABLE {quoted_temp} AS "
                    f"SELECT {projection} FROM main.{quoted_table}"
                )
                conn.execute(f"DELETE FROM main.{quoted_table}")
                source_columns = list(quoted_columns)
                if not has_rowid_alias:
                    source_columns.insert(0, "__v4_rowid")
                conn.execute(
                    f"INSERT INTO main.{quoted_table} "
                    f"({', '.join(target_columns)}) "
                    f"SELECT {', '.join(source_columns)} FROM temp.{quoted_temp}"
                )
                after = int(
                    conn.execute(
                        f"SELECT COUNT(*) FROM main.{quoted_table}"
                    ).fetchone()[0]
                )
                if after != before:
                    raise RuntimeError(
                        f"v4 table rewrite changed {table} row count: "
                        f"{before} -> {after}"
                    )
            finally:
                conn.execute(f"DROP TABLE IF EXISTS temp.{quoted_temp}")

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

    @classmethod
    def _migrate_v5_simplified_memory(cls, conn: sqlite3.Connection) -> None:
        """Replace the learning state machine with curated active memory.

        Session history remains in ``runs``/``events``.  Only explicitly active
        project/application facts enter ``memory_items``; every legacy pending
        operation remains non-active in ``memory_pending_writes``.  No model is
        invoked during migration.
        """
        now = _now_iso()
        conn.execute("DROP TABLE IF EXISTS memory_items_v5")
        conn.execute("DROP TABLE IF EXISTS memory_pending_writes_v5")
        conn.execute("DROP TABLE IF EXISTS review_runs_v5")
        conn.execute("DROP TABLE IF EXISTS runs_v5")
        cls._execute_script_in_transaction(
            conn,
            _v5_memory_tables_sql(
                "memory_items_v5",
                "memory_pending_writes_v5",
            )
            + """
            CREATE TABLE review_runs_v5 (
                review_id INTEGER PRIMARY KEY AUTOINCREMENT,
                review_key TEXT NOT NULL UNIQUE,
                root_run_id TEXT NOT NULL,
                application_id TEXT,
                model_type TEXT NOT NULL,
                status TEXT NOT NULL,
                result_json TEXT NOT NULL DEFAULT '{}',
                created_at TEXT NOT NULL,
                finished_at TEXT
            );
            CREATE TABLE runs_v5 (
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
                metadata_json TEXT
            );
            """,
        )

        run_columns = (
            "run_id, root_run_id, task_id, agent_name, application_id, "
            "application_name, application_path, workflow_path, yaml_path, "
            "run_dir, status, started_at, ended_at, task_text, final_answer, "
            "indexed_at, metadata_json"
        )
        conn.execute(
            f"INSERT INTO runs_v5 ({run_columns}) SELECT {run_columns} FROM runs"
        )

        legacy_rows = conn.execute(
            "SELECT * FROM memory_items ORDER BY id"
        ).fetchall()
        skipped_session = 0
        active_copied = 0
        pending_copied = 0
        stale_copied = 0

        for row in legacy_rows:
            scope_type = str(row["scope_type"] or "").strip().casefold()
            if scope_type == "app":
                scope_type = "application"
            if scope_type not in {"project", "application"}:
                skipped_session += 1
                continue
            status = str(row["status"] or "").strip().casefold()
            applied_by = str(row["applied_by"] or "").strip().casefold()
            if status != "active" or applied_by == "auto":
                continue
            content = sanitize_text_fragment(str(row["content"] or "")).strip()
            if not content or content == BLOCKED_TEXT:
                continue
            item_id = int(row["id"])
            content_hash = memory_content_hash(content)
            cursor = conn.execute(
                """
                INSERT OR IGNORE INTO memory_items_v5 (
                    id, scope_type, scope_id, content, content_hash,
                    created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    item_id,
                    scope_type,
                    str(row["scope_id"] or ""),
                    content,
                    content_hash,
                    str(row["created_at"] or now),
                    str(row["updated_at"] or now),
                ),
            )
            if cursor.rowcount:
                active_copied += 1

        for row in legacy_rows:
            scope_type = str(row["scope_type"] or "").strip().casefold()
            if scope_type == "app":
                scope_type = "application"
            if scope_type not in {"project", "application"}:
                continue
            old_status = str(row["status"] or "").strip().casefold()
            applied_by = str(row["applied_by"] or "").strip().casefold()
            if old_status != "pending" and not (
                old_status == "active" and applied_by == "auto"
            ):
                continue

            action = (
                "add"
                if old_status == "active"
                else str(row["action"] or "add").strip().casefold()
            )
            pending_status = "pending"
            resolved_at: str | None = None
            payload: dict[str, Any]
            content = sanitize_text_fragment(str(row["content"] or "")).strip()
            if action == "add" and content and content != BLOCKED_TEXT:
                payload = {"content": content}
            elif action in {"replace", "remove"}:
                target_id = int(row["target_item_id"] or 0)
                target = conn.execute(
                    """
                    SELECT scope_type, scope_id, content_hash
                    FROM memory_items_v5 WHERE id = ?
                    """,
                    (target_id,),
                ).fetchone()
                target_valid = (
                    target is not None
                    and str(target["scope_type"] or "") == scope_type
                    and str(target["scope_id"] or "") == str(row["scope_id"] or "")
                )
                if target_valid:
                    payload = {
                        "target_id": target_id,
                        "target_content_hash": str(target["content_hash"]),
                    }
                    if action == "replace" and content and content != BLOCKED_TEXT:
                        payload["content"] = content
                    elif action == "replace":
                        target_valid = False
                else:
                    payload = {}
                if not target_valid:
                    pending_status = "stale"
                    resolved_at = now
            else:
                action = "add"
                payload = (
                    {"content": content}
                    if content and content != BLOCKED_TEXT
                    else {}
                )
                pending_status = "stale"
                resolved_at = now

            conn.execute(
                """
                INSERT INTO memory_pending_writes_v5 (
                    id, status, action, scope_type, scope_id, payload_json,
                    source_run_id, created_at, resolved_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    int(row["id"]),
                    pending_status,
                    action,
                    scope_type,
                    str(row["scope_id"] or ""),
                    _json_dumps(payload),
                    str(row["source_run_id"] or ""),
                    str(row["created_at"] or now),
                    resolved_at,
                ),
            )
            if pending_status == "pending":
                pending_copied += 1
            else:
                stale_copied += 1

        for row in conn.execute("SELECT * FROM review_runs ORDER BY review_id"):
            created_at = str(row["created_at"] or now)
            conn.execute(
                """
                INSERT INTO review_runs_v5 (
                    review_id, review_key, root_run_id, application_id,
                    model_type, status, result_json, created_at, finished_at
                ) VALUES (?, ?, ?, ?, 'legacy', ?, ?, ?, ?)
                """,
                (
                    int(row["review_id"]),
                    f"legacy:{int(row['review_id'])}",
                    str(row["source_run_id"] or ""),
                    str(row["application_id"] or ""),
                    "legacy",
                    "{}",
                    created_at,
                    created_at,
                ),
            )

        for table in _V5_REMOVED_TABLES:
            conn.execute(f'DROP TABLE IF EXISTS "{table}"')
        conn.execute("DROP TABLE memory_items")
        conn.execute("DROP TABLE review_runs")
        conn.execute("DROP TABLE runs")
        conn.execute("ALTER TABLE memory_items_v5 RENAME TO memory_items")
        conn.execute(
            "ALTER TABLE memory_pending_writes_v5 RENAME TO memory_pending_writes"
        )
        conn.execute("ALTER TABLE review_runs_v5 RENAME TO review_runs")
        conn.execute("ALTER TABLE runs_v5 RENAME TO runs")
        cls._execute_script_in_transaction(conn, _SCHEMA_V5_SQL)
        conn.execute(
            """
            INSERT INTO maintenance (key, value) VALUES (?, ?)
            ON CONFLICT(key) DO UPDATE SET value = excluded.value
            """,
            (
                _V5_MIGRATION_REPORT_KEY,
                _json_dumps(
                    {
                        "active_copied": active_copied,
                        "pending_copied": pending_copied,
                        "stale_copied": stale_copied,
                        "session_rows_skipped": skipped_session,
                    }
                ),
            ),
        )

    @classmethod
    def _migrate_v6_typed_review(cls, conn: sqlite3.Connection) -> None:
        """Migrate curated v5 facts and pending writes without invoking a model.

        Active v5 rows are confirmed migration facts. Pending operations become
        manual review candidates, never active memory. The entire replacement
        runs inside the outer schema transaction.
        """
        now = _now_iso()
        internal_tables = {
            "memory_items": "memory_items_v6",
            "review_batches": "review_batches_v6",
            "review_batch_runs": "review_batch_runs_v6",
            "review_candidates": "review_candidates_v6",
            "review_mutations": "review_mutations_v6",
            "run_feedback": "run_feedback_v6",
        }
        for table_name in internal_tables.values():
            conn.execute(f'DROP TABLE IF EXISTS "{table_name}"')
        cls._execute_script_in_transaction(
            conn,
            _v6_review_tables_sql(**internal_tables),
        )

        identity_paths: dict[str, dict[str, set[str]]] = {}

        def remember_identity(
            application_id: Any,
            *,
            application_path: Any = "",
            workflow_path: Any = "",
            yaml_path: Any = "",
        ) -> None:
            legacy_id = str(application_id or "").strip()
            if not legacy_id:
                return
            bucket = identity_paths.setdefault(
                legacy_id,
                {"application_paths": set(), "workflow_paths": set()},
            )
            if str(application_path or "").strip():
                bucket["application_paths"].add(str(application_path).strip())
            for path in (workflow_path, yaml_path):
                if str(path or "").strip():
                    bucket["workflow_paths"].add(str(path).strip())

        for table in ("runs", "events"):
            if not cls._has_column(conn, table, "application_id"):
                continue
            columns = {
                str(row[1])
                for row in conn.execute(f'PRAGMA table_info("{table}")').fetchall()
            }
            if "application_id" not in columns:
                continue
            selected = ["application_id"]
            for column in ("application_path", "workflow_path", "yaml_path"):
                selected.append(column if column in columns else f"'' AS {column}")
            for identity_row in conn.execute(
                f'SELECT {", ".join(selected)} FROM "{table}"'
            ).fetchall():
                remember_identity(
                    identity_row["application_id"],
                    application_path=identity_row["application_path"],
                    workflow_path=identity_row["workflow_path"],
                    yaml_path=identity_row["yaml_path"],
                )

        for row in conn.execute(
            "SELECT scope_type,scope_id FROM memory_items "
            "UNION SELECT scope_type,scope_id FROM memory_pending_writes"
        ).fetchall():
            if str(row["scope_type"] or "") == "application":
                remember_identity(row["scope_id"])
        for row in conn.execute(
            "SELECT application_id FROM review_runs WHERE application_id IS NOT NULL"
        ).fetchall():
            remember_identity(row["application_id"])
        if cls._has_column(conn, "skill_proposals", "application_id"):
            for row in conn.execute(
                "SELECT DISTINCT application_id FROM skill_proposals "
                "WHERE application_id IS NOT NULL"
            ).fetchall():
                remember_identity(row["application_id"])
        if cls._has_column(conn, "trusted_review_evidence", "scope_id"):
            for row in conn.execute(
                "SELECT DISTINCT scope_id FROM trusted_review_evidence "
                "WHERE scope_type='application'"
            ).fetchall():
                remember_identity(row["scope_id"])

        resolutions = {
            legacy_id: resolve_legacy_application_id(
                legacy_id,
                application_paths=tuple(sorted(paths["application_paths"])),
                workflow_paths=tuple(sorted(paths["workflow_paths"])),
            )
            for legacy_id, paths in identity_paths.items()
        }

        def application_resolution(legacy_id: Any) -> tuple[str, bool, str]:
            raw = str(legacy_id or "").strip()
            resolution = resolutions.get(raw)
            if resolution is None:
                resolution = resolve_legacy_application_id(raw)
                resolutions[raw] = resolution
            if resolution.canonical_id:
                return resolution.canonical_id, True, resolution.reason
            return resolution.quarantine_id, False, resolution.reason

        def legacy_scope_resolution(
            legacy_scope_type: Any,
            legacy_scope_id: Any,
        ) -> tuple[str, str, bool, str]:
            scope_type = str(legacy_scope_type or "").strip().casefold()
            scope_id = str(legacy_scope_id or "").strip()
            if scope_type == "project" and scope_id == "project":
                return "project", "project", True, "project_scope"
            if scope_type == "application":
                target_id, resolved, reason = application_resolution(scope_id)
                return "application", target_id, resolved, reason
            digest = hashlib.sha256(
                f"{scope_type}:{scope_id}".encode("utf-8", errors="surrogatepass")
            ).hexdigest()[:24]
            reason = (
                "invalid_project_scope_binding"
                if scope_type == "project"
                else "invalid_legacy_scope_type"
            )
            return (
                "application",
                f"migration-unresolved/{digest}",
                False,
                reason,
            )

        # Historical rows retain their provenance but use either the one proven
        # canonical identity or an isolated migration namespace. They can never
        # accidentally bind to a different live Application after the upgrade.
        for legacy_id, resolution in resolutions.items():
            target_id = resolution.canonical_id or resolution.quarantine_id
            if not target_id or target_id == legacy_id:
                continue
            for table in ("runs", "events", "skill_proposals"):
                if cls._has_column(conn, table, "application_id"):
                    conn.execute(
                        f'UPDATE "{table}" SET application_id=? WHERE application_id=?',
                        (target_id, legacy_id),
                    )
            if cls._has_column(conn, "trusted_review_evidence", "scope_id"):
                # Avoid uniqueness collisions when two proven historical aliases
                # refer to the same canonical Application.
                conn.execute(
                    """
                    INSERT OR IGNORE INTO trusted_review_evidence(
                        event_id,root_run_id,tool_name,kind,scope_type,scope_id,
                        source,text,created_at
                    )
                    SELECT event_id,root_run_id,tool_name,kind,scope_type,?,
                           source,text,created_at
                    FROM trusted_review_evidence
                    WHERE scope_type='application' AND scope_id=?
                    """,
                    (target_id, legacy_id),
                )
                conn.execute(
                    "DELETE FROM trusted_review_evidence "
                    "WHERE scope_type='application' AND scope_id=?",
                    (legacy_id,),
                )

        migration_batches: set[tuple[str, str]] = set()

        def ensure_migration_batch(
            scope_type: str,
            scope_id: str,
            *,
            created_at: str,
        ) -> str:
            scope_pair = (scope_type, scope_id)
            scope_digest = hashlib.sha256(
                f"{scope_type}:{scope_id}".encode()
            ).hexdigest()[:16]
            review_id = f"migration_v5_{scope_digest}"
            if scope_pair not in migration_batches:
                conn.execute(
                    """
                    INSERT INTO review_batches_v6(
                        review_id, scope_type, scope_id, status, dry_run,
                        result_json, created_at, finished_at
                    ) VALUES (?, ?, ?, 'completed', 0, ?, ?, ?)
                    """,
                    (
                        review_id,
                        scope_type,
                        scope_id,
                        _json_dumps({"migration_schema": 5}),
                        created_at,
                        now,
                    ),
                )
                migration_batches.add(scope_pair)
            return review_id

        pending_count = 0
        quarantined_count = 0

        active_rows = conn.execute(
            "SELECT * FROM memory_items ORDER BY id"
        ).fetchall()
        active_payloads: dict[int, dict[str, str]] = {}
        active_slots: set[tuple[str, str, str, str]] = set()
        for row in active_rows:
            content = sanitize_text_fragment(str(row["content"] or "")).strip()
            if not content or content == BLOCKED_TEXT:
                continue
            item_id = int(row["id"])
            content_digest = str(row["content_hash"] or memory_content_hash(content))
            typed_payload = {"text": content}
            memory_key = f"legacy:{content_digest}"
            legacy_scope_type = str(row["scope_type"] or "")
            legacy_scope_id = str(row["scope_id"] or "")
            scope_type, scope_id, scope_resolved, scope_reason = (
                legacy_scope_resolution(legacy_scope_type, legacy_scope_id)
            )
            provenance = [
                {
                    "migration_schema": 5,
                    "legacy_item_id": item_id,
                    "legacy_scope_type": legacy_scope_type,
                    "legacy_scope_id": legacy_scope_id,
                    "canonical_scope_type": scope_type,
                    "canonical_scope_id": scope_id,
                    "scope_resolution": scope_reason,
                }
            ]
            slot = (scope_type, scope_id, "fact", memory_key)
            collision = scope_resolved and slot in active_slots
            if not scope_resolved or collision:
                reason = (
                    "legacy_application_scope_collision"
                    if collision
                    else "legacy_application_scope_unresolved"
                )
                gate_reasons = [reason]
                if scope_reason and scope_reason != reason:
                    gate_reasons.append(scope_reason)
                review_id = ensure_migration_batch(
                    scope_type,
                    scope_id,
                    created_at=str(row["created_at"] or now),
                )
                conn.execute(
                    """
                    INSERT INTO review_candidates_v6(
                        candidate_id, review_id, scope_type, scope_id, kind,
                        memory_key, payload_json, payload_hash, proposed_action,
                        approval, state, outcome, revision, target_item_id,
                        provenance_json, source_run_ids_json, gate_reasons_json,
                        reason, created_at, resolved_at
                    ) VALUES (?, ?, ?, ?, 'fact', ?, ?, ?, 'add', 'manual',
                        'quarantined', 'quarantined', 1, NULL, ?, '[]', ?, ?, ?, ?)
                    """,
                    (
                        f"migration_v5_active_{item_id}",
                        review_id,
                        scope_type,
                        scope_id,
                        memory_key,
                        _json_dumps(typed_payload),
                        payload_hash(typed_payload),
                        _json_dumps(provenance),
                        _json_dumps(gate_reasons),
                        reason,
                        str(row["created_at"] or now),
                        now,
                    ),
                )
                quarantined_count += 1
                continue
            conn.execute(
                """
                INSERT INTO memory_items_v6(
                    id, scope_type, scope_id, kind, memory_key, payload_json,
                    payload_hash, state, activation_source, provenance_json,
                    revision, created_at, updated_at
                ) VALUES (?, ?, ?, 'fact', ?, ?, ?, 'active_confirmed',
                    'migration', ?, 1, ?, ?)
                """,
                (
                    item_id,
                    scope_type,
                    scope_id,
                    memory_key,
                    _json_dumps(typed_payload),
                    payload_hash(typed_payload),
                    _json_dumps(provenance),
                    str(row["created_at"] or now),
                    str(row["updated_at"] or now),
                ),
            )
            active_slots.add(slot)
            active_payloads[item_id] = typed_payload

        for row in conn.execute(
            "SELECT * FROM memory_pending_writes ORDER BY id"
        ).fetchall():
            pending_id = int(row["id"])
            legacy_scope_type = str(row["scope_type"] or "")
            legacy_scope_id = str(row["scope_id"] or "")
            scope_type, scope_id, scope_resolved, scope_reason = (
                legacy_scope_resolution(legacy_scope_type, legacy_scope_id)
            )
            review_id = ensure_migration_batch(
                scope_type,
                scope_id,
                created_at=str(row["created_at"] or now),
            )

            try:
                legacy_payload_value = json.loads(str(row["payload_json"] or "{}"))
            except (TypeError, json.JSONDecodeError):
                legacy_payload_value = {}
            legacy_payload = (
                legacy_payload_value
                if isinstance(legacy_payload_value, dict)
                else {}
            )
            action = str(row["action"] or "add").strip().casefold()
            action_valid = action in {"add", "replace", "remove"}
            if not action_valid:
                action = "add"
            target_id = 0
            try:
                target_id = int(legacy_payload.get("target_id") or 0)
            except (TypeError, ValueError):
                target_id = 0
            content = sanitize_text_fragment(
                str(legacy_payload.get("content") or "")
            ).strip()
            if not content and target_id in active_payloads:
                content = active_payloads[target_id]["text"]
            reconstructable = bool(content) and content != BLOCKED_TEXT
            if not reconstructable:
                content = "Legacy pending operation could not be reconstructed safely."
            typed_payload = {"text": content}
            typed_payload_hash = payload_hash(typed_payload)
            content_digest = memory_content_hash(content)
            memory_key = (
                f"legacy:{str(legacy_payload.get('target_content_hash') or content_digest)}"
            )
            source_run_id = str(row["source_run_id"] or "")
            provenance = [
                {
                    "migration_schema": 5,
                    "migration_evidence": "v5_pending_write",
                    "legacy_pending_id": pending_id,
                    "legacy_scope_type": legacy_scope_type,
                    "legacy_scope_id": legacy_scope_id,
                    "canonical_scope_type": scope_type,
                    "canonical_scope_id": scope_id,
                    "scope_resolution": scope_reason,
                    "proposed_action": action,
                    "memory_key": memory_key,
                    "payload_hash": typed_payload_hash,
                    "source_run_id": source_run_id,
                    "root_run_id": source_run_id,
                    "target_item_id": target_id or None,
                    "legacy_payload": sanitize_value_fragments(legacy_payload),
                }
            ]
            old_status = str(row["status"] or "stale")
            pending = (
                old_status == "pending"
                and reconstructable
                and scope_resolved
                and action_valid
            )
            state = "pending_pre_review" if pending else "quarantined"
            outcome = "pending" if pending else "quarantined"
            gate_reasons = ["migrated_v5_pending"]
            if not scope_resolved:
                gate_reasons.append("legacy_application_scope_unresolved")
                if scope_reason:
                    gate_reasons.append(scope_reason)
            if not reconstructable:
                gate_reasons.append("legacy_payload_unreconstructable")
            if not action_valid:
                gate_reasons.append("legacy_action_invalid")
            reason = (
                "migrated_from_v5_without_model"
                if pending
                else (
                    "legacy_application_scope_unresolved"
                    if not scope_resolved
                    else "migrated_v5_pending_quarantined"
                )
            )
            conn.execute(
                """
                INSERT INTO review_candidates_v6(
                    candidate_id, review_id, scope_type, scope_id, kind,
                    memory_key, payload_json, payload_hash, proposed_action,
                    approval, state, outcome, revision, target_item_id,
                    provenance_json, source_run_ids_json, gate_reasons_json,
                    reason, created_at, resolved_at
                ) VALUES (?, ?, ?, ?, 'fact', ?, ?, ?, ?, 'manual', ?, ?, 1,
                    ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    f"migration_v5_pending_{pending_id}",
                    review_id,
                    scope_type,
                    scope_id,
                    memory_key,
                    _json_dumps(typed_payload),
                    typed_payload_hash,
                    action,
                    state,
                    outcome,
                    target_id or None,
                    _json_dumps(provenance),
                    _json_dumps([source_run_id] if source_run_id else []),
                    _json_dumps(gate_reasons),
                    reason,
                    str(row["created_at"] or now),
                    None if pending else str(row["resolved_at"] or now),
                ),
            )
            if source_run_id:
                conn.execute(
                    """
                    INSERT OR IGNORE INTO review_batch_runs_v6(
                        review_id, root_run_id, application_id
                    ) VALUES (?, ?, ?)
                    """,
                    (
                        review_id,
                        source_run_id,
                        scope_id if scope_type == "application" else "",
                    ),
                )
            if pending:
                pending_count += 1
            else:
                quarantined_count += 1

        legacy_review_count = 0
        for row in conn.execute("SELECT * FROM review_runs ORDER BY review_id"):
            legacy_id = int(row["review_id"])
            review_id = f"legacy_review_{legacy_id}"
            application_id = str(row["application_id"] or "")
            scope_resolved = True
            scope_reason = "project_scope"
            if application_id:
                scope_type = "application"
                scope_id, scope_resolved, scope_reason = application_resolution(
                    application_id
                )
            else:
                scope_type = "project"
                scope_id = "project"
            status = str(row["status"] or "completed")
            if status not in {"completed", "failed"}:
                status = "completed"
            result_json = str(row["result_json"] or "{}")
            if not scope_resolved:
                status = "failed"
                try:
                    legacy_result = json.loads(result_json)
                except (TypeError, json.JSONDecodeError):
                    legacy_result = {}
                result_json = _json_dumps(
                    {
                        "migration_schema": 5,
                        "migration_quarantined": True,
                        "reason": "legacy_application_scope_unresolved",
                        "scope_resolution": scope_reason,
                        "legacy_application_id": application_id,
                        "legacy_review_key": str(row["review_key"] or ""),
                        "legacy_result": sanitize_value_fragments(legacy_result),
                    }
                )
            created_at = str(row["created_at"] or now)
            conn.execute(
                """
                INSERT OR IGNORE INTO review_batches_v6(
                    review_id, scope_type, scope_id, status, dry_run,
                    result_json, created_at, finished_at
                ) VALUES (?, ?, ?, ?, 0, ?, ?, ?)
                """,
                (
                    review_id,
                    scope_type,
                    scope_id,
                    status,
                    result_json,
                    created_at,
                    str(row["finished_at"] or created_at),
                ),
            )
            root_run_id = str(row["root_run_id"] or "")
            if root_run_id:
                conn.execute(
                    """
                    INSERT OR IGNORE INTO review_batch_runs_v6(
                        review_id, root_run_id, application_id
                    ) VALUES (?, ?, ?)
                    """,
                    (
                        review_id,
                        root_run_id,
                        scope_id if scope_type == "application" else "",
                    ),
                )
            legacy_review_count += 1

        conn.execute("DROP TABLE memory_items")
        conn.execute("DROP TABLE memory_pending_writes")
        conn.execute("DROP TABLE review_runs")
        for canonical, internal in internal_tables.items():
            conn.execute(f'ALTER TABLE "{internal}" RENAME TO "{canonical}"')
        cls._execute_script_in_transaction(conn, _SCHEMA_V6_SQL)
        conn.execute(
            """
            INSERT INTO maintenance(key, value) VALUES (?, ?)
            ON CONFLICT(key) DO UPDATE SET value=excluded.value
            """,
            (
                _V6_MIGRATION_REPORT_KEY,
                _json_dumps(
                    {
                        "active_facts": len(active_payloads),
                        "pending_candidates": pending_count,
                        "quarantined_candidates": quarantined_count,
                        "legacy_review_batches": legacy_review_count,
                    }
                ),
            ),
        )

    @staticmethod
    def _sanitize_json_cell_with_taint(
        value: Any,
    ) -> tuple[Any, str, bool]:
        raw = "" if value is None else str(value)
        if not raw:
            return {}, "{}", False
        try:
            parsed = json.loads(raw)
        except (TypeError, json.JSONDecodeError):
            sanitized, tainted = sanitize_text_fragment_with_taint(raw)
            return sanitized, sanitized, tainted
        sanitized, tainted = sanitize_value_fragments_with_taint(
            parsed,
            legacy_redaction_provenance=True,
        )
        return sanitized, _json_dumps(sanitized), tainted

    @classmethod
    def _sanitize_v5_event_sequences(cls, conn: sqlite3.Connection) -> None:
        """Apply the current sanitizer while preserving root event order.

        A later event can contain an arbitrary secret echo that has no remaining
        key shape. The preceding event's content-free taint flag is therefore
        the only safe evidence needed to block every subsequent event.
        """
        before_runs = int(conn.execute("SELECT COUNT(*) FROM runs").fetchone()[0])
        before_events = int(
            conn.execute("SELECT COUNT(*) FROM events").fetchone()[0]
        )
        tainted_roots: set[str] = set()
        rows = conn.execute(
            """
            SELECT id, run_id, root_run_id, event_type, input_json,
                   output_json, content_text, metadata_json
            FROM events
            ORDER BY COALESCE(NULLIF(root_run_id, ''), run_id), id
            """
        ).fetchall()
        for row in rows:
            root_run_id = str(row["root_run_id"] or row["run_id"] or "")
            content_text, content_tainted = sanitize_text_fragment_with_taint(
                row["content_text"] or ""
            )
            _input_value, input_json, input_tainted = (
                cls._sanitize_json_cell_with_taint(row["input_json"])
            )
            output_value, output_json, output_tainted = (
                cls._sanitize_json_cell_with_taint(row["output_json"])
            )
            if isinstance(output_value, dict):
                output_value.pop(TRUSTED_MEMORY_EVIDENCE_RESPONSE_KEY, None)
                output_json = _json_dumps(output_value)
            metadata_value, metadata_json, metadata_tainted = (
                cls._sanitize_json_cell_with_taint(row["metadata_json"])
            )
            persisted_taint = (
                isinstance(metadata_value, dict)
                and metadata_value.get(_SAFETY_TAINT_KEY) is True
            )
            root_was_tainted = root_run_id in tainted_roots
            event_tainted = (
                content_tainted
                or input_tainted
                or output_tainted
                or metadata_tainted
                or persisted_taint
            )
            if root_was_tainted:
                content_text = BLOCKED_TEXT
                input_json = _json_dumps({"input": BLOCKED_TEXT})
                output_json = _json_dumps({"result": BLOCKED_TEXT})
                metadata_value = {}
                metadata_json = "{}"
                event_tainted = True
            if event_tainted:
                metadata = (
                    dict(metadata_value)
                    if isinstance(metadata_value, dict)
                    else {"value": metadata_value}
                )
                metadata[_SAFETY_TAINT_KEY] = True
                metadata_json = _json_dumps(metadata)
                tainted_roots.add(root_run_id)
            conn.execute(
                """
                UPDATE events
                SET input_json = ?, output_json = ?, content_text = ?,
                    metadata_json = ?
                WHERE id = ?
                """,
                (
                    input_json,
                    output_json,
                    content_text,
                    metadata_json,
                    int(row["id"]),
                ),
            )

        for row in conn.execute(
            "SELECT run_id, root_run_id, task_text, final_answer, metadata_json "
            "FROM runs"
        ).fetchall():
            task_text, _task_tainted = sanitize_text_fragment_with_taint(
                row["task_text"] or ""
            )
            final_answer, _final_tainted = sanitize_text_fragment_with_taint(
                row["final_answer"] or ""
            )
            metadata_value, metadata_json, _metadata_tainted = (
                cls._sanitize_json_cell_with_taint(row["metadata_json"])
            )
            run_root_id = str(row["root_run_id"] or row["run_id"] or "")
            if run_root_id in tainted_roots:
                final_answer = BLOCKED_TEXT
                metadata = (
                    dict(metadata_value)
                    if isinstance(metadata_value, dict)
                    else {"value": metadata_value}
                )
                metadata[_SAFETY_TAINT_KEY] = True
                metadata_json = _json_dumps(metadata)
            conn.execute(
                """
                UPDATE runs
                SET task_text = ?, final_answer = ?, metadata_json = ?
                WHERE run_id = ?
                """,
                (
                    task_text,
                    final_answer,
                    metadata_json,
                    str(row["run_id"]),
                ),
            )

        cls._rebuild_v5_event_fts(conn)
        after_runs = int(conn.execute("SELECT COUNT(*) FROM runs").fetchone()[0])
        after_events = int(
            conn.execute("SELECT COUNT(*) FROM events").fetchone()[0]
        )
        if (after_runs, after_events) != (before_runs, before_events):
            raise RuntimeError("v5 sanitizer changed run/event row counts")

    @classmethod
    def _sanitize_v5_trusted_review_evidence(
        cls,
        conn: sqlite3.Connection,
    ) -> None:
        """Keep only losslessly safe evidence from its original live event."""

        safe_rows: list[
            tuple[str, str, str, str, str, str, str, str, str]
        ] = []
        rows = conn.execute(
            """
            SELECT evidence.event_id, evidence.root_run_id, evidence.tool_name,
                   evidence.kind, evidence.scope_type, evidence.scope_id,
                   evidence.source, evidence.text, evidence.created_at,
                   events.run_id AS event_run_id,
                   events.root_run_id AS event_root_run_id,
                   events.tool_name AS event_tool_name,
                   events.application_id AS event_application_id,
                   events.event_type AS event_type,
                   events.status AS event_status,
                   events.created_at AS event_created_at,
                   events.metadata_json AS event_metadata_json
            FROM trusted_review_evidence AS evidence
            LEFT JOIN events ON events.event_id = evidence.event_id
            ORDER BY evidence.event_id, evidence.kind, evidence.scope_type,
                     evidence.scope_id, evidence.source, evidence.text
            """
        ).fetchall()
        for row in rows:
            if str(row["kind"] or "") != TRUSTED_MEMORY_EVIDENCE_KIND:
                continue
            if (
                str(row["event_type"] or "") != "tool_result"
                or str(row["event_status"] or "") != "completed"
            ):
                continue
            try:
                event_metadata = json.loads(
                    str(row["event_metadata_json"] or "{}")
                )
            except (TypeError, ValueError, json.JSONDecodeError):
                continue
            if not isinstance(event_metadata, dict) or event_metadata.get(
                _SAFETY_TAINT_KEY
            ) is True:
                continue

            values = {
                "event_id": str(row["event_id"] or ""),
                "root_run_id": str(row["root_run_id"] or ""),
                "tool_name": str(row["tool_name"] or ""),
                "scope_type": str(row["scope_type"] or ""),
                "scope_id": str(row["scope_id"] or ""),
                "source": str(row["source"] or ""),
                "text": str(row["text"] or ""),
                "created_at": str(row["created_at"] or ""),
            }
            sanitized_values: dict[str, str] = {}
            changed_or_tainted = False
            for name, value in values.items():
                sanitized, tainted = sanitize_text_fragment_with_taint(value)
                sanitized_values[name] = sanitized
                changed_or_tainted = changed_or_tainted or tainted or sanitized != value
            if changed_or_tainted or any(
                not value or value == BLOCKED_TEXT
                for value in sanitized_values.values()
            ):
                continue
            if values["source"] != values["source"].strip():
                continue

            expected_scope_id = (
                "project"
                if values["scope_type"] == "project"
                else str(row["event_application_id"] or "")
                if values["scope_type"] == "application"
                else ""
            )
            if not expected_scope_id or values["scope_id"] != expected_scope_id:
                continue

            event_root_run_id = str(
                row["event_root_run_id"] or row["event_run_id"] or ""
            )
            if (
                values["root_run_id"] != event_root_run_id
                or values["tool_name"] != str(row["event_tool_name"] or "")
                or values["created_at"] != str(row["event_created_at"] or "")
            ):
                continue
            safe_rows.append(
                (
                    values["event_id"],
                    values["root_run_id"],
                    values["tool_name"],
                    TRUSTED_MEMORY_EVIDENCE_KIND,
                    values["scope_type"],
                    values["scope_id"],
                    values["source"],
                    values["text"],
                    values["created_at"],
                )
            )

        cls._replace_trusted_review_evidence_table(conn, tuple(safe_rows))

    @classmethod
    def _sanitize_v5_memory_state(cls, conn: sqlite3.Connection) -> None:
        """Rebuild curated memory from sanitized, internally consistent rows.

        Redaction can collapse distinct legacy content hashes. Rebuilding under
        the final v5 uniqueness constraint makes that collision explicit and
        records an id map so pending replace/remove operations still target the
        retained row. Pending rows are never silently executed during repair.
        """
        before_items = int(
            conn.execute("SELECT COUNT(*) FROM memory_items").fetchone()[0]
        )
        before_pending = int(
            conn.execute(
                "SELECT COUNT(*) FROM memory_pending_writes"
            ).fetchone()[0]
        )
        now = _now_iso()
        conn.execute("DROP TABLE IF EXISTS memory_items_v5_sanitized")
        conn.execute("DROP TABLE IF EXISTS memory_pending_writes_v5_sanitized")
        cls._execute_script_in_transaction(
            conn,
            _v5_memory_tables_sql(
                "memory_items_v5_sanitized",
                "memory_pending_writes_v5_sanitized",
            ),
        )

        def safe_scope_id(scope_type: str, value: Any) -> str:
            if scope_type == "project":
                return "project"
            return safe_storage_identity(
                value,
                namespace="application",
                allow_empty=True,
            )

        item_id_map: dict[int, int] = {}
        for row in conn.execute(
            "SELECT * FROM memory_items ORDER BY id"
        ).fetchall():
            old_id = int(row["id"])
            scope_type = str(row["scope_type"])
            scope_id = safe_scope_id(scope_type, row["scope_id"])
            content = sanitize_text_fragment(row["content"] or "").strip()
            if not content or content == BLOCKED_TEXT:
                continue
            content_hash = memory_content_hash(content)
            created_at = sanitize_text_fragment(row["created_at"] or "").strip()
            updated_at = sanitize_text_fragment(row["updated_at"] or "").strip()
            if not created_at or created_at == BLOCKED_TEXT:
                created_at = now
            if not updated_at or updated_at == BLOCKED_TEXT:
                updated_at = now
            cursor = conn.execute(
                """
                INSERT OR IGNORE INTO memory_items_v5_sanitized(
                    id, scope_type, scope_id, content, content_hash,
                    created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    old_id,
                    scope_type,
                    scope_id,
                    content,
                    content_hash,
                    created_at,
                    updated_at,
                ),
            )
            if cursor.rowcount:
                item_id_map[old_id] = old_id
                continue
            retained = conn.execute(
                """
                SELECT id FROM memory_items_v5_sanitized
                WHERE scope_type = ? AND scope_id = ? AND content_hash = ?
                """,
                (scope_type, scope_id, content_hash),
            ).fetchone()
            if retained is None:
                raise RuntimeError("v5 memory sanitizer lost a safe memory row")
            item_id_map[old_id] = int(retained["id"])

        seen_pending: set[tuple[str, str, str, str]] = set()
        for row in conn.execute(
            "SELECT * FROM memory_pending_writes ORDER BY id"
        ).fetchall():
            pending_id = int(row["id"])
            status = str(row["status"])
            action = str(row["action"])
            scope_type = str(row["scope_type"])
            scope_id = safe_scope_id(scope_type, row["scope_id"])
            raw_payload = str(row["payload_json"] or "{}")
            parse_failed = False
            try:
                payload_value = json.loads(raw_payload)
            except (TypeError, json.JSONDecodeError):
                payload_value = {}
                parse_failed = True
            payload_value = sanitize_value_fragments(payload_value)
            payload = payload_value if isinstance(payload_value, dict) else {}
            valid = not parse_failed and isinstance(payload_value, dict)
            canonical_payload: dict[str, Any] = {}

            if action == "add":
                content = sanitize_text_fragment(payload.get("content") or "").strip()
                valid = valid and bool(content) and content != BLOCKED_TEXT
                if valid:
                    canonical_payload = {"content": content}
            else:
                try:
                    old_target_id = int(payload.get("target_id"))
                except (TypeError, ValueError):
                    old_target_id = 0
                target_id = item_id_map.get(old_target_id, 0)
                target = (
                    conn.execute(
                        "SELECT * FROM memory_items_v5_sanitized WHERE id = ?",
                        (target_id,),
                    ).fetchone()
                    if target_id
                    else None
                )
                valid = (
                    valid
                    and target is not None
                    and str(target["scope_type"]) == scope_type
                    and str(target["scope_id"]) == scope_id
                )
                if valid and target is not None:
                    canonical_payload = {
                        "target_id": int(target["id"]),
                        "target_content_hash": str(target["content_hash"]),
                    }
                    if action == "replace":
                        content = sanitize_text_fragment(
                            payload.get("content") or ""
                        ).strip()
                        valid = bool(content) and content != BLOCKED_TEXT
                        if valid:
                            canonical_payload["content"] = content
                        else:
                            canonical_payload = {}

            payload_json = _json_dumps(canonical_payload)
            resolved_at_raw = row["resolved_at"]
            resolved_at = (
                sanitize_text_fragment(resolved_at_raw).strip()
                if resolved_at_raw is not None
                else None
            )
            if resolved_at == BLOCKED_TEXT:
                resolved_at = now
            if status == "pending":
                dedupe_key = (action, scope_type, scope_id, payload_json)
                if not valid or dedupe_key in seen_pending:
                    status = "stale"
                    resolved_at = now
                else:
                    seen_pending.add(dedupe_key)
            source_run_id = safe_storage_identity(
                row["source_run_id"],
                namespace="run",
                allow_empty=True,
            )
            created_at = sanitize_text_fragment(row["created_at"] or "").strip()
            if not created_at or created_at == BLOCKED_TEXT:
                created_at = now
            conn.execute(
                """
                INSERT INTO memory_pending_writes_v5_sanitized(
                    id, status, action, scope_type, scope_id, payload_json,
                    source_run_id, created_at, resolved_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    pending_id,
                    status,
                    action,
                    scope_type,
                    scope_id,
                    payload_json,
                    source_run_id,
                    created_at,
                    resolved_at,
                ),
            )

        conn.execute("DROP TABLE memory_items")
        conn.execute("DROP TABLE memory_pending_writes")
        conn.execute(
            "ALTER TABLE memory_items_v5_sanitized RENAME TO memory_items"
        )
        conn.execute(
            "ALTER TABLE memory_pending_writes_v5_sanitized "
            "RENAME TO memory_pending_writes"
        )
        cls._execute_script_in_transaction(conn, _SCHEMA_V5_SQL)

        after_items = int(
            conn.execute("SELECT COUNT(*) FROM memory_items").fetchone()[0]
        )
        after_pending = int(
            conn.execute(
                "SELECT COUNT(*) FROM memory_pending_writes"
            ).fetchone()[0]
        )
        if after_items != len(set(item_id_map.values())):
            raise RuntimeError("v5 sanitizer changed safe memory row identity")
        if after_items > before_items or after_pending != before_pending:
            raise RuntimeError("v5 sanitizer changed pending memory row counts")

    @staticmethod
    def _rebuild_v5_event_fts(conn: sqlite3.Connection) -> None:
        existing_fts_tables = {
            str(row[0])
            for row in conn.execute(
                "SELECT name FROM sqlite_master "
                "WHERE type = 'table' "
                "AND name IN ('events_fts', 'events_fts_trigram')"
            )
        }
        if "events_fts" in existing_fts_tables:
            conn.execute(
                "INSERT INTO events_fts(events_fts, rank) "
                "VALUES('secure-delete', 1)"
            )
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
            conn.execute("INSERT INTO events_fts(events_fts) VALUES('optimize')")

        if "events_fts_trigram" in existing_fts_tables:
            conn.execute(
                "INSERT INTO events_fts_trigram(events_fts_trigram, rank) "
                "VALUES('secure-delete', 1)"
            )
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
            conn.execute(
                "INSERT INTO events_fts_trigram(events_fts_trigram) "
                "VALUES('optimize')"
            )

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
            conn.execute(
                "INSERT INTO events_fts(events_fts, rank) "
                "VALUES('secure-delete', 1)"
            )
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
            conn.execute(
                "INSERT INTO events_fts_trigram(events_fts_trigram, rank) "
                "VALUES('secure-delete', 1)"
            )
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
        with serialized_write_transaction(self.db_path, self._connect) as conn:
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
        with serialized_write_transaction(self.db_path, self._connect) as conn:
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

    @staticmethod
    def _root_has_safety_taint(
        conn: sqlite3.Connection,
        root_run_id: str,
    ) -> bool:
        return (
            conn.execute(
                """
                SELECT 1
                FROM events
                WHERE COALESCE(NULLIF(root_run_id, ''), run_id) = ?
                  AND instr(metadata_json, '"_safety_tainted": true') > 0
                LIMIT 1
                """,
                (root_run_id,),
            ).fetchone()
            is not None
        )

    @staticmethod
    def _mark_record_safety_taint(record: dict[str, Any]) -> None:
        metadata = dict(record.get("metadata") or {})
        metadata[_SAFETY_TAINT_KEY] = True
        record["metadata"] = metadata

    @classmethod
    def _block_event_after_safety_taint(
        cls,
        record: dict[str, Any],
    ) -> None:
        record["content"] = BLOCKED_TEXT
        record["content_text"] = BLOCKED_TEXT
        record["input_data"] = {"input": BLOCKED_TEXT}
        record["output_data"] = {"result": BLOCKED_TEXT}
        record["metadata"] = {_SAFETY_TAINT_KEY: True}

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
        record, safety_tainted = event.to_record_with_safety()
        persisted_taint = (record.get("metadata") or {}).get(
            _SAFETY_TAINT_KEY
        ) is True
        if safety_tainted or persisted_taint:
            self._mark_record_safety_taint(record)
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
        if self._root_has_safety_taint(conn, run_meta["root_run_id"]):
            self._block_event_after_safety_taint(record)
            run_meta = self._run_metadata(record, root_run_id=root_run_id)
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
        with serialized_write_transaction(self.db_path, self._connect) as conn:
            # Serialize the idempotency read with the run/event writes. Without
            # this boundary two processes could both miss the same event before
            # either writer creates it.
            return self._append_event_in_conn(conn, event, root_run_id=root_run_id)

    def append_runtime_event(
        self,
        event: CanonicalSessionEvent,
        *,
        trusted_evidence: tuple[dict[str, str], ...] = (),
    ) -> dict[str, Any]:
        """Atomically append a live event and non-importable review evidence."""

        with serialized_write_transaction(self.db_path, self._connect) as conn:
            result = self._append_event_in_conn(conn, event)
            result["trusted_evidence_indexed"] = 0
            if not result.get("indexed") or not trusted_evidence:
                return result

            row = conn.execute(
                """
                SELECT event_id, root_run_id, tool_name, application_id,
                       event_type, status, created_at, metadata_json
                FROM events WHERE event_id = ?
                """,
                (result["event_id"],),
            ).fetchone()
            if (
                row is None
                or str(row["event_type"] or "") != "tool_result"
                or str(row["status"] or "") != "completed"
            ):
                return result
            try:
                metadata = json.loads(str(row["metadata_json"] or "{}"))
            except (TypeError, ValueError, json.JSONDecodeError):
                return result
            if isinstance(metadata, dict) and metadata.get(_SAFETY_TAINT_KEY) is True:
                return result

            normalized: list[tuple[str, str, str, str]] = []
            for entry in trusted_evidence:
                if not isinstance(entry, dict):
                    return result
                if entry.get("kind") != TRUSTED_MEMORY_EVIDENCE_KIND:
                    return result
                scope_type = entry.get("scope")
                if scope_type not in {"project", "application"}:
                    return result
                scope_id = (
                    "project"
                    if scope_type == "project"
                    else str(row["application_id"] or "")
                )
                safe_scope_id, scope_id_tainted = sanitize_text_fragment_with_taint(
                    scope_id
                )
                source, source_tainted = sanitize_text_fragment_with_taint(
                    entry.get("source") or ""
                )
                text, text_tainted = sanitize_text_fragment_with_taint(
                    entry.get("text") or ""
                )
                if (
                    scope_id_tainted
                    or safe_scope_id != scope_id
                    or not safe_scope_id
                    or source_tainted
                    or text_tainted
                    or not source.strip()
                    or not text
                    or source == BLOCKED_TEXT
                    or text == BLOCKED_TEXT
                ):
                    return result
                normalized.append(
                    (scope_type, safe_scope_id, source.strip(), text)
                )

            inserted = 0
            for scope_type, scope_id, source, text in normalized:
                inserted += conn.execute(
                    """
                    INSERT OR IGNORE INTO trusted_review_evidence (
                        event_id, root_run_id, tool_name, kind, scope_type,
                        scope_id, source, text, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        str(row["event_id"]),
                        str(row["root_run_id"] or result["root_run_id"]),
                        str(row["tool_name"] or ""),
                        TRUSTED_MEMORY_EVIDENCE_KIND,
                        scope_type,
                        scope_id,
                        source,
                        text,
                        str(row["created_at"] or _now_iso()),
                    ),
                ).rowcount
            result["trusted_evidence_indexed"] = inserted
            return result

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

    def review_application_id(self, root_run_id: str) -> str:
        """Return the Application identity bound to a persisted root run."""
        root_run_id = safe_run_id(root_run_id)
        if not root_run_id:
            return ""
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT application_id FROM runs
                WHERE run_id = ?
                LIMIT 1
                """,
                (root_run_id,),
            ).fetchone()
        return str(row["application_id"] or "") if row is not None else ""

    @staticmethod
    def completed_root_run_in_transaction(
        conn: sqlite3.Connection,
        root_run_id: str,
    ) -> sqlite3.Row | None:
        """Return the root row only when its own SessionEnd is persisted."""
        run_row = conn.execute(
            """
            SELECT status, final_answer, application_id FROM runs
            WHERE run_id = ?
            LIMIT 1
            """,
            (root_run_id,),
        ).fetchone()
        completion_row = conn.execute(
            """
            SELECT 1 FROM events
            WHERE run_id = ?
              AND COALESCE(NULLIF(root_run_id, ''), run_id) = ?
              AND event_type = 'run_completed'
              AND status = 'completed'
            LIMIT 1
            """,
            (root_run_id, root_run_id),
        ).fetchone()
        if (
            run_row is None
            or completion_row is None
            or str(run_row["status"] or "").casefold() != "completed"
        ):
            return None
        return run_row

    def completed_review_context(
        self,
        root_run_id: str,
        *,
        tool_result_limit: int,
    ) -> dict[str, Any] | None:
        """Read the bounded persisted facts eligible for completed-run review."""
        root_run_id = safe_run_id(root_run_id)
        if not root_run_id:
            return None
        limit = max(0, min(int(tool_result_limit), 100))
        with self._connect() as conn:
            run_row = self.completed_root_run_in_transaction(conn, root_run_id)
            if run_row is None:
                return None
            root_application_id = str(run_row["application_id"] or "")
            event_rows = conn.execute(
                """
                SELECT event_id, tool_name, event_type, status, input_json,
                       output_json, metadata_json
                FROM events
                WHERE COALESCE(NULLIF(root_run_id, ''), run_id) = ?
                    AND event_type = 'tool_result'
                    AND status = 'completed'
                ORDER BY id DESC
                LIMIT ?
                """,
                (root_run_id, limit),
            ).fetchall()
            event_records = [dict(row) for row in event_rows]
            for event_record in event_records:
                try:
                    metadata = json.loads(
                        str(event_record.get("metadata_json") or "{}")
                    )
                except (TypeError, ValueError, json.JSONDecodeError):
                    metadata = {}
                event_record["tool_call_id"] = (
                    str(metadata.get("tool_call_id") or "")
                    if isinstance(metadata, dict)
                    else ""
                )
            evidence_by_event: dict[str, list[dict[str, str]]] = {}
            event_ids = [str(row["event_id"]) for row in event_records]
            if event_ids:
                placeholders = ", ".join("?" for _ in event_ids)
                evidence_rows = conn.execute(
                    f"""
                    SELECT event_id, kind, scope_type, scope_id, source, text
                    FROM trusted_review_evidence
                    WHERE root_run_id = ?
                      AND kind = ?
                      AND event_id IN ({placeholders})
                    ORDER BY event_id, kind, source, text
                    """,
                    (root_run_id, TRUSTED_MEMORY_EVIDENCE_KIND, *event_ids),
                ).fetchall()
                for evidence_row in evidence_rows:
                    evidence_scope_type = str(
                        evidence_row["scope_type"] or ""
                    )
                    evidence_scope_id = str(evidence_row["scope_id"] or "")
                    if evidence_scope_type == "application" and (
                        not root_application_id
                        or evidence_scope_id != root_application_id
                    ):
                        continue
                    evidence_by_event.setdefault(
                        str(evidence_row["event_id"]),
                        [],
                    ).append(
                        {
                            "kind": str(evidence_row["kind"] or ""),
                            "scope_type": evidence_scope_type,
                            "scope_id": evidence_scope_id,
                            "source": str(evidence_row["source"] or ""),
                            "text": str(evidence_row["text"] or ""),
                        }
                    )
            for event_record in event_records:
                event_record["trusted_evidence"] = evidence_by_event.get(
                    str(event_record["event_id"]),
                    [],
                )
        return {
            "final_answer": str(run_row["final_answer"] or ""),
            "tool_results": event_records,
        }

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
        with serialized_write_transaction(self.db_path, self._connect) as conn:
            conn.execute(
                "DELETE FROM trusted_review_evidence WHERE event_id IN "
                "(SELECT event_id FROM events WHERE run_id = ?)",
                (run_id,),
            )
            conn.execute("DELETE FROM events WHERE run_id = ?", (run_id,))
            conn.execute("DELETE FROM runs WHERE run_id = ?", (run_id,))

    _PRUNE_CHUNK_ROWS = 2000

    def prune_events(self, *, retention_days: int = 90) -> dict[str, Any]:
        """Delete runs (and their events) whose last activity is older than the cutoff.

        Deletes commit in small chunks: each removed events row fires the FTS
        triggers, so one big transaction over a large backlog would block live
        writers for the entire cleanup.
        """
        if retention_days < 0:
            raise ValueError("retention_days must be non-negative")
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
            first_chunk = True
            while True:
                with serialized_write_transaction(self.db_path, self._connect) as conn:
                    if first_chunk:
                        conn.execute(
                            "DELETE FROM trusted_review_evidence WHERE event_id IN "
                            "(SELECT event_id FROM events WHERE run_id = ?)",
                            (run_id,),
                        )
                        first_chunk = False
                    deleted = conn.execute(
                        "DELETE FROM events WHERE id IN "
                        "(SELECT id FROM events WHERE run_id = ? LIMIT ?)",
                        (run_id, self._PRUNE_CHUNK_ROWS),
                    ).rowcount
                    events_deleted += deleted
                    finished = deleted < self._PRUNE_CHUNK_ROWS
                    if finished:
                        conn.execute("DELETE FROM runs WHERE run_id = ?", (run_id,))
                if finished:
                    break
        # v6 review batches are immutable decision audit, not disposable
        # session transcripts.  A batch can still own pending candidates or
        # provenance for active memory after its source run is pruned, so the
        # session-retention command must never delete it.
        reviews_deleted = 0
        return {
            "ok": True,
            "runs_pruned": len(stale_runs),
            "events_pruned": int(events_deleted),
            "reviews_pruned": int(reviews_deleted),
            "cutoff": cutoff,
        }

    def review_status(self, *, review_key: str, root_run_id: str) -> str | None:
        """Return one root's terminal review status without claiming it."""
        review_key = require_safe_identity(review_key, field="review key")
        root_run_id = safe_run_id(
            require_safe_identity(root_run_id, field="review root run id")
        )
        if review_key != f"root:{root_run_id}":
            raise ValueError("review key must match the root run id")
        with self._connect() as conn:
            row = conn.execute(
                "SELECT root_run_id, status FROM review_runs WHERE review_key = ?",
                (review_key,),
            ).fetchone()
        if row is None:
            return None
        if str(row["root_run_id"]) != root_run_id:
            raise ValueError("review key is already bound to another root run")
        return str(row["status"] or "")

    @staticmethod
    def record_review_in_transaction(
        conn: sqlite3.Connection,
        *,
        review_key: str,
        root_run_id: str,
        application_id: str = "",
        model_type: str = "",
        status: str,
        result: dict[str, Any] | None = None,
        created_at: str | None = None,
        finished_at: str | None = None,
    ) -> tuple[int | None, bool]:
        """Insert one terminal audit using the caller's active transaction."""
        review_key = require_safe_identity(review_key, field="review key")
        root_run_id = safe_run_id(
            require_safe_identity(root_run_id, field="review root run id")
        )
        if review_key != f"root:{root_run_id}":
            raise ValueError("review key must match the root run id")
        application_id = require_safe_identity(
            application_id,
            field="review application id",
            allow_empty=True,
        )
        model_type = require_safe_identity(
            model_type,
            field="review model type",
            allow_empty=True,
        )
        status = require_safe_identity(status, field="review status")
        if status not in {"completed", "failed", "skipped"}:
            raise ValueError("review status must be terminal")
        created = require_safe_identity(
            created_at or _now_iso(),
            field="review created timestamp",
        )
        finished = require_safe_identity(
            finished_at or _now_iso(),
            field="review finished timestamp",
        )
        result_json = _json_dumps(sanitize_value_fragments(result or {}))

        if (
            SelfLearningLedger.completed_root_run_in_transaction(
                conn,
                root_run_id,
            )
            is None
        ):
            return None, False

        existing = conn.execute(
            "SELECT review_id, root_run_id FROM review_runs WHERE review_key = ?",
            (review_key,),
        ).fetchone()
        if existing is not None:
            if str(existing["root_run_id"]) != root_run_id:
                raise ValueError("review key is already bound to another root run")
            return int(existing["review_id"]), False
        cursor = conn.execute(
            """
            INSERT INTO review_runs (
                review_key, root_run_id, application_id, model_type,
                status, result_json, created_at, finished_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                review_key,
                root_run_id,
                application_id,
                model_type,
                status,
                result_json,
                created,
                finished,
            ),
        )
        return int(cursor.lastrowid), True

    def record_review(
        self,
        *,
        review_key: str,
        root_run_id: str,
        application_id: str = "",
        model_type: str = "",
        status: str,
        result: dict[str, Any] | None = None,
        created_at: str | None = None,
        finished_at: str | None = None,
    ) -> int | None:
        """Insert one terminal audit; an existing review is immutable."""
        with serialized_write_transaction(self.db_path, self._connect) as conn:
            review_id, _inserted = self.record_review_in_transaction(
                conn,
                review_key=review_key,
                root_run_id=root_run_id,
                application_id=application_id,
                model_type=model_type,
                status=status,
                result=result,
                created_at=created_at,
                finished_at=finished_at,
            )
            return review_id

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
        with serialized_write_transaction(self.db_path, self._connect) as conn:
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
        with serialized_write_transaction(self.db_path, self._connect) as conn:
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


_SCHEMA_V6_SQL = (
    """
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
    metadata_json TEXT
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
"""
    + _v6_review_tables_sql(if_not_exists=True)
    + """
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
"""
    + _trusted_review_evidence_table_sql(
        "trusted_review_evidence",
        if_not_exists=True,
    )
    + """
CREATE INDEX IF NOT EXISTS idx_runs_application ON runs(application_id);
CREATE INDEX IF NOT EXISTS idx_runs_started ON runs(started_at);
CREATE INDEX IF NOT EXISTS idx_events_run_id ON events(run_id);
CREATE INDEX IF NOT EXISTS idx_events_agent ON events(agent_name);
CREATE INDEX IF NOT EXISTS idx_events_application ON events(application_id);
CREATE INDEX IF NOT EXISTS idx_events_created ON events(created_at);
CREATE INDEX IF NOT EXISTS idx_memory_scope_state
    ON memory_items(scope_type, scope_id, state, kind, memory_key);
CREATE UNIQUE INDEX IF NOT EXISTS idx_memory_active_key
    ON memory_items(scope_type, scope_id, kind, memory_key)
    WHERE state IN ('active_unreviewed', 'active_confirmed');
CREATE INDEX IF NOT EXISTS idx_review_batches_scope
    ON review_batches(scope_type, scope_id, created_at);
CREATE INDEX IF NOT EXISTS idx_review_candidates_batch
    ON review_candidates(review_id, state, candidate_id);
CREATE INDEX IF NOT EXISTS idx_review_candidates_scope
    ON review_candidates(scope_type, scope_id, state, candidate_id);
CREATE INDEX IF NOT EXISTS idx_review_mutations_batch
    ON review_mutations(review_id, mutation_id);
CREATE INDEX IF NOT EXISTS idx_run_feedback_run ON run_feedback(run_id, created_at);
CREATE INDEX IF NOT EXISTS idx_skill_proposals_status ON skill_proposals(status);
CREATE INDEX IF NOT EXISTS idx_trusted_review_evidence_root
    ON trusted_review_evidence(root_run_id, event_id);
"""
)


_SCHEMA_V5_SQL = (
    """
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
    metadata_json TEXT
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
"""
    + _v5_memory_tables_sql(
        "memory_items",
        "memory_pending_writes",
        if_not_exists=True,
    )
    + """
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
    review_key TEXT NOT NULL UNIQUE,
    root_run_id TEXT NOT NULL,
    application_id TEXT,
    model_type TEXT NOT NULL,
    status TEXT NOT NULL,
    result_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    finished_at TEXT
);
"""
    + _trusted_review_evidence_table_sql(
        "trusted_review_evidence",
        if_not_exists=True,
    )
    + """
CREATE INDEX IF NOT EXISTS idx_runs_application ON runs(application_id);
CREATE INDEX IF NOT EXISTS idx_runs_started ON runs(started_at);
CREATE INDEX IF NOT EXISTS idx_events_run_id ON events(run_id);
CREATE INDEX IF NOT EXISTS idx_events_agent ON events(agent_name);
CREATE INDEX IF NOT EXISTS idx_events_application ON events(application_id);
CREATE INDEX IF NOT EXISTS idx_events_created ON events(created_at);
CREATE INDEX IF NOT EXISTS idx_memory_scope ON memory_items(scope_type, scope_id);
CREATE INDEX IF NOT EXISTS idx_memory_updated ON memory_items(updated_at);
CREATE INDEX IF NOT EXISTS idx_memory_pending_status
    ON memory_pending_writes(status, created_at, id);
CREATE INDEX IF NOT EXISTS idx_memory_pending_scope
    ON memory_pending_writes(scope_type, scope_id, status, id);
CREATE INDEX IF NOT EXISTS idx_skill_proposals_status ON skill_proposals(status);
CREATE INDEX IF NOT EXISTS idx_review_runs_root ON review_runs(root_run_id);
CREATE INDEX IF NOT EXISTS idx_trusted_review_evidence_root
    ON trusted_review_evidence(root_run_id, event_id);
"""
)


# Legacy bootstrap shape used only while upgrading databases older than v5.
# A v5 database never executes this script, so removed learning tables cannot
# silently reappear during repeated initialization.
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
