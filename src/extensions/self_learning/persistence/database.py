"""SQLite connection and transaction ownership for self-learning state."""

from __future__ import annotations

import sqlite3
import threading
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path

_BUSY_TIMEOUT_SECONDS = 5.0
_DATABASE_WRITER_LOCKS: dict[str, threading.Lock] = {}
_DATABASE_WRITER_LOCKS_GUARD = threading.Lock()


def _database_writer_lock(db_path: str | Path) -> threading.Lock:
    key = str(Path(db_path).resolve())
    with _DATABASE_WRITER_LOCKS_GUARD:
        lock = _DATABASE_WRITER_LOCKS.get(key)
        if lock is None:
            lock = threading.Lock()
            _DATABASE_WRITER_LOCKS[key] = lock
        return lock


@contextmanager
def serialized_database_writer(db_path: str | Path) -> Iterator[None]:
    """Serialize one process-local SQLite writer for a database."""
    with _database_writer_lock(db_path):
        yield


@contextmanager
def serialized_write_transaction(
    db_path: str | Path,
    connect: Callable[[], sqlite3.Connection],
) -> Iterator[sqlite3.Connection]:
    """Run one short write transaction behind the per-database writer gate."""
    with serialized_database_writer(db_path):
        conn = connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            yield conn
            conn.commit()
        except BaseException:
            if conn.in_transaction:
                conn.rollback()
            raise
        finally:
            conn.close()


class SelfLearningDatabase:
    """Own construction of every SQLite connection to self-learning state."""

    def __init__(self, db_path: str | Path) -> None:
        self.path = Path(db_path).expanduser().resolve()

    def connect(
        self,
        *,
        foreign_keys: bool = False,
        read_only: bool = False,
    ) -> sqlite3.Connection:
        if read_only:
            conn = sqlite3.connect(
                f"{self.path.as_uri()}?mode=ro",
                uri=True,
                timeout=_BUSY_TIMEOUT_SECONDS,
            )
        else:
            conn = sqlite3.connect(
                str(self.path),
                timeout=_BUSY_TIMEOUT_SECONDS,
            )
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout=5000")
        if foreign_keys:
            conn.execute("PRAGMA foreign_keys=ON")
        if read_only:
            conn.execute("PRAGMA query_only=ON")
        return conn


__all__ = [
    "SelfLearningDatabase",
    "serialized_database_writer",
    "serialized_write_transaction",
]
