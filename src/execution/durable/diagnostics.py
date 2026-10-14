"""Safe persistence diagnostics; observations never decide task admission."""
from __future__ import annotations

import logging
import os
import re
import sqlite3
import threading
import time
from contextlib import closing
from pathlib import Path

from .files import atomic_json, read_json


def exception_chain(error):
    """Retain original driver codes without SQL, parameters or credentials."""
    chain, pending, seen = [], [(error, None, None)], set()
    while pending:
        cause, parent, relation = pending.pop()
        if id(cause) in seen:
            continue
        seen.add(id(cause))
        index = len(chain)
        detail = {"error_type": type(cause).__name__, "module": type(cause).__module__}
        if parent is not None:
            detail.update(parent=parent, relation=relation)
        for name in ("errno", "sqlite_errorcode", "sqlite_errorname", "sqlstate", "pgcode"):
            value = getattr(cause, name, None)
            if isinstance(value, (int, str)):
                detail[name] = value
        chain.append(detail)
        for name in ("__context__", "__cause__", "orig"):
            nested = getattr(cause, name, None)
            if isinstance(nested, BaseException):
                pending.append((nested, index, name))
    return chain


def error_detail(error, phase, *, database=None):
    chain = exception_chain(error)
    driver = next((item for item in chain if "sqlite_errorcode" in item or "errno" in item), chain[-1])
    database_error = any(item["module"].split(".")[0] in {"sqlite3", "sqlalchemy", "psycopg", "psycopg2"}
                         for item in chain)
    # SQLAlchemy's str(error) embeds the statement and bound parameters. Driver
    # codes and typed chains preserve the diagnosis without serializing either.
    message = (f"Persistence failed ({driver.get('sqlite_errorname', driver['error_type'])})"
               if database_error else str(error))
    detail = {"error_type": type(error).__name__, "message": message, "phase": phase,
              "pid": os.getpid(), "thread": threading.get_ident(), "exception_chain": chain,
              **{name: driver[name] for name in ("errno", "sqlite_errorcode", "sqlite_errorname") if name in driver}}
    if database is not None:
        detail["database"] = database
    return detail


def record_cleanup_error(job, error, phase, *, database=None):
    try:
        path = job / "cleanup-failed.json"
        detail = error_detail(error, phase, database=database)
        if path.exists():
            detail = {**read_json(path), "latest_error": detail}
        atomic_json(path, detail)
    except (OSError, ValueError):
        pass  # Diagnostic failure must never replace the original cause.


def storage_metadata(root: Path, database_url: str):
    from sqlalchemy import make_url

    url = make_url(database_url)
    detail = {"backend": url.get_backend_name(), "storage_target": read_json(root / "namespace.json")["storage_target"]}
    if url.get_backend_name() == "sqlite":
        detail["sqlite_version"] = sqlite3.sqlite_version
        database = Path(url.database).expanduser().resolve()
        try:
            # A read-only connection observes the real mode; it never creates
            # the file or silently switches the application's journal policy.
            with closing(sqlite3.connect(database.as_uri() + "?mode=ro", uri=True, timeout=0)) as connection:
                detail["journal_mode"] = connection.execute("PRAGMA journal_mode").fetchone()[0]
        except sqlite3.Error as error:
            detail["metadata_error"] = exception_chain(error)
    return detail


class PersistenceTrace(logging.Handler):
    """Observe DBOS background exceptions through Python's public logging API."""
    def __init__(self, root, database):
        super().__init__(level=logging.ERROR)
        self.root, self.database = root, database

    def emit(self, record):
        if not record.exc_info or record.exc_info[1] is None:
            return
        error = record.exc_info[1]
        chain = exception_chain(error)
        if not any(item["module"].split(".")[0] in {"sqlite3", "sqlalchemy", "psycopg", "psycopg2"}
                   for item in chain):
            return
        from dbos import DBOS

        workflow_id = DBOS.workflow_id
        if not isinstance(workflow_id, str) or re.fullmatch(r"loom-[0-9a-f]{64}", workflow_id) is None:
            workflow_id = None
        detail = {**error_detail(error, "dbos_background", database=self.database), "observed_at": time.time(),
                  "workflow_id": workflow_id}
        try:
            path = self.root / "jobs" / workflow_id / "persistence-error.json" if workflow_id else None
            if path is not None and path.exists():
                previous = read_json(path)
                detail["first_error"] = previous.get("first_error", previous)
            atomic_json(self.root / "latest-persistence-error.json", detail)
            if path is not None:
                atomic_json(path, detail)
        except (OSError, ValueError):
            pass  # Diagnostics cannot mask DBOS's original exception.


def attach_persistence_trace(root, database_url):
    from dbos import DBOS

    trace = PersistenceTrace(root, storage_metadata(root, database_url))
    DBOS.logger.addHandler(trace)
    return trace


def detach_persistence_trace(trace):
    from dbos import DBOS

    DBOS.logger.removeHandler(trace)
    trace.close()
