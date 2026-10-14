"""Atomic OS job evidence; these files do not decide queue admission or retry."""
import hashlib
import json
import os
import tempfile
from pathlib import Path


def atomic_json(path: Path, value) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, pending = tempfile.mkstemp(prefix=".pending-", dir=path.parent)
    try:
        with os.fdopen(fd, "w") as stream:
            json.dump(value, stream, ensure_ascii=False, allow_nan=False)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(pending, path)
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if os.path.exists(pending):
            os.unlink(pending)


def read_json(path: Path):
    return json.loads(path.read_text())


def initialize_namespace(root: Path, database_url: str) -> None:
    from dbos import DBOS
    from filelock import FileLock
    from sqlalchemy import make_url

    url = make_url(database_url)
    # libpq query parameters can override URL host/port/dbname (including
    # service-file indirection). Keep this local-machine replacement Interface
    # explicit: target belongs in the URL, connection-only TLS settings may
    # rotate. Never persist credentials to calculate this marker.
    connection_options = {"sslmode", "sslrootcert", "sslcert", "sslkey", "connect_timeout", "application_name"}
    allowed = connection_options if url.get_backend_name() == "postgresql" else set()
    if set(url.query) - allowed:
        raise ValueError("Storage target must use the URL host/port/database; target query overrides are unsupported")
    database = url.database
    if url.get_backend_name() == "sqlite":
        import sqlite3
        import sys

        # SQLite 3.51.2 fixes the Unix WAL close lock-order inversion present
        # in 3.51.1. Refuse the known unsafe library before DBOS opens a queue.
        # https://www.sqlite.org/releaselog/3_51_2.html
        if sys.platform in {"darwin", "linux"} and sqlite3.sqlite_version_info == (3, 51, 1):
            raise RuntimeError("SQLite 3.51.1 has a known Unix WAL close deadlock; "
                               "use an environment with SQLite 3.51.2 or another verified library")
        if not database or database == ":memory:":
            raise ValueError("durable SQLite storage requires a persistent file")
        database = str(Path(database).expanduser().resolve())
    # Authentication may rotate; the namespace must still name the same DB.
    # Hash only the physical target, never persist URL credentials or tokens.
    target = json.dumps([url.get_backend_name(), url.host, url.port, database], separators=(",", ":"))
    expected = {"task_schema_version": 1, "engine": "dbos", "engine_version": "3.2.0",
                "storage_target": hashlib.sha256(target.encode()).hexdigest()}
    with FileLock(root / "schema.lock"):
        marker = root / "namespace.json"
        if marker.exists():
            if read_json(marker) != expected:
                raise ValueError("Incompatible durable namespace; migrate with the old executor stopped")
            return
        if any((root / "jobs").rglob("description.json")):
            raise RuntimeError("Namespace metadata is missing while old OS jobs exist; recovery is required")
        # Public upstream migration API, without launching an executor.
        DBOS.migrate(database_url)
        atomic_json(marker, expected)
