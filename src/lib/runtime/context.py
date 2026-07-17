"""Canonical runtime storage paths and run-scoped context.

This module is the only place that knows the on-disk layout under the
AgentLoom runtime home.  Callers receive concrete paths from a
``RuntimeContext`` instead of deriving paths from the current directory,
timestamps, logger state, or agent names.
"""

from __future__ import annotations

import contextvars
import fcntl
import hashlib
import json
import os
import re
import stat
import threading
import uuid
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Any, TextIO

_SAFE_PART = re.compile(r"[^A-Za-z0-9._-]+")
_CURRENT_RUN_CONTEXT: contextvars.ContextVar[RuntimeContext | None] = contextvars.ContextVar(
    "agentloom_run_context", default=None
)
_DIRECTORY_OPEN_FLAGS = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
_FILE_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)
RUNTIME_ROOT_ENV = "AGENTLOOM_RUNTIME_ROOT"


def _absolute_path_without_resolving_symlinks(path: str | Path) -> Path:
    return Path(os.path.abspath(os.fspath(Path(path).expanduser())))


def _safe_part(value: str, *, field: str) -> str:
    raw = str(value).strip()
    if not raw or raw in {".", ".."}:
        raise ValueError(f"{field} must be a non-empty safe path component")
    if "/" in raw or "\\" in raw:
        raise ValueError(f"{field} must not contain path separators")
    cleaned = _SAFE_PART.sub("_", raw).strip("._")
    if not cleaned or cleaned != raw or len(raw) > 160:
        raise ValueError(f"{field} must already be a safe path component")
    return cleaned


def validate_runtime_id(value: str, *, field: str = "runtime_id") -> str:
    """Validate an opaque task/run id without changing its identity."""

    return _safe_part(value, field=field)


def validate_runtime_owned_path(path: Path, *, root: Path) -> Path:
    """Reject symlinks inside a runtime-owned path before creating files."""

    root = Path(root).expanduser().absolute()
    path = Path(path).expanduser().absolute()
    if root.is_symlink():
        raise RuntimeError(f"runtime root is a symlink: {root}")
    try:
        relative = path.relative_to(root)
    except ValueError as exc:
        raise RuntimeError(f"runtime path escapes root: {path}") from exc
    current = root
    for part in relative.parts:
        current = current / part
        if current.is_symlink():
            raise RuntimeError(f"runtime path contains a symlink: {current}")
    return path


def _open_runtime_directory(
    directory: Path,
    *,
    root: Path,
    create: bool = False,
) -> int:
    """Open a runtime-owned directory without following any child symlink.

    Path-level validation alone has a check/use race.  This helper anchors the
    traversal at an opened runtime-root descriptor and opens every component
    with ``O_NOFOLLOW``.  The returned descriptor therefore keeps referring to
    the validated directory even if a pathname is renamed afterwards.
    """

    root = _absolute_path_without_resolving_symlinks(root)
    directory = _absolute_path_without_resolving_symlinks(directory)
    try:
        relative = directory.relative_to(root)
    except ValueError as exc:
        raise RuntimeError(f"runtime path escapes root: {directory}") from exc

    if create:
        root.mkdir(parents=True, exist_ok=True)
    if root.is_symlink():
        raise RuntimeError(f"runtime root is a symlink: {root}")
    try:
        current_fd = os.open(root, _DIRECTORY_OPEN_FLAGS)
    except OSError as exc:
        raise RuntimeError(f"cannot open runtime root safely: {root}") from exc

    try:
        for part in relative.parts:
            try:
                next_fd = os.open(part, _DIRECTORY_OPEN_FLAGS, dir_fd=current_fd)
            except FileNotFoundError:
                if not create:
                    raise
                try:
                    os.mkdir(part, mode=0o700, dir_fd=current_fd)
                except FileExistsError:
                    pass
                next_fd = os.open(part, _DIRECTORY_OPEN_FLAGS, dir_fd=current_fd)
            except OSError as exc:
                raise RuntimeError(f"runtime path component is not a safe directory: {directory}") from exc
            os.close(current_fd)
            current_fd = next_fd
        return current_fd
    except BaseException:
        os.close(current_fd)
        raise


def _ensure_runtime_directory(directory: Path, *, root: Path) -> Path:
    fd = _open_runtime_directory(directory, root=root, create=True)
    os.close(fd)
    return _absolute_path_without_resolving_symlinks(directory)


def _safe_named_part(value: str, *, fallback: str, include_hash_on_change: bool) -> str:
    """Sanitize a human-readable name without losing stable identity."""

    raw = str(value).strip()
    cleaned = _SAFE_PART.sub("_", raw).strip("._")[:160]
    digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:10]
    if not cleaned:
        return f"{fallback}-{digest}"
    if include_hash_on_change and (cleaned != raw or len(raw) > 160):
        return f"{cleaned[:149]}-{digest}"
    return cleaned


def portable_runtime_component(value: str, *, fallback: str = "item") -> str:
    """Map a display name to one deterministic portable storage component."""

    raw = str(value).strip()
    if not raw:
        raise ValueError("runtime storage name must not be empty")
    return _safe_named_part(raw, fallback=fallback, include_hash_on_change=True)


def safe_application_id(value: str) -> str:
    """Return a safe, possibly nested application identifier.

    Application ids may contain ``/`` so nested applications retain their
    stable identity.  Traversal and absolute paths are rejected rather than
    normalized away.
    """

    raw = str(value).strip().replace("\\", "/")
    path = PurePosixPath(raw)
    if not raw or path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        raise ValueError("application_id must be a safe relative path")
    return "/".join(_safe_named_part(part, fallback="app", include_hash_on_change=True) for part in path.parts)


def fallback_application_id(
    workflow_path: str | Path | None,
    *,
    name_hint: str | None = None,
) -> str:
    """Build a stable id for YAML files outside ``applications/``.

    The readable stem is not unique by itself, so a short hash of the resolved
    path is always included.
    """

    if workflow_path:
        path = Path(workflow_path).expanduser()
        try:
            canonical = str(path.resolve())
        except OSError:
            canonical = str(path.absolute())
        readable = name_hint or path.stem or "external"
    else:
        canonical = str(name_hint or "external")
        readable = name_hint or "external"
    safe_name = _safe_named_part(
        readable,
        fallback="external",
        include_hash_on_change=False,
    )
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:10]
    return f"{safe_name}-{digest}"


def generate_runtime_id(kind: str) -> str:
    prefix = _safe_part(kind, field="id kind")
    timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
    return f"{prefix}_{timestamp}_{uuid.uuid4().hex[:12]}"


class RuntimeRunLease:
    """Cross-process lease proving that a run attempt is still active."""

    def __init__(self, run_dir: Path) -> None:
        self.run_dir = Path(run_dir)
        self._fd: int | None = None

    def acquire(self) -> None:
        if self._fd is not None:
            return
        if self.run_dir.is_symlink() or not self.run_dir.is_dir():
            raise RuntimeError(f"run directory is unavailable: {self.run_dir}")
        fd = os.open(self.run_dir, os.O_RDONLY)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BaseException:
            os.close(fd)
            raise
        self._fd = fd

    def is_held(self) -> bool:
        """Return whether a writer owns this run without excluding other readers."""

        if self.run_dir.is_symlink() or not self.run_dir.is_dir():
            raise RuntimeError(f"run directory is unavailable: {self.run_dir}")
        fd = os.open(self.run_dir, _DIRECTORY_OPEN_FLAGS)
        shared = False
        try:
            try:
                fcntl.flock(fd, fcntl.LOCK_SH | fcntl.LOCK_NB)
                shared = True
            except BlockingIOError:
                return True
            return False
        finally:
            if shared:
                fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)

    def release(self) -> None:
        if self._fd is None:
            return
        fd, self._fd = self._fd, None
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)

    def __enter__(self) -> RuntimeRunLease:
        self.acquire()
        return self

    def __exit__(self, exc_type, exc, traceback) -> None:
        self.release()


@dataclass(frozen=True, slots=True)
class RuntimeContext:
    """All canonical paths and identities for one execution attempt."""

    root_dir: Path
    application_id: str
    task_id: str
    run_id: str

    def __post_init__(self) -> None:
        canonical_root = _absolute_path_without_resolving_symlinks(self.root_dir)
        object.__setattr__(self, "root_dir", canonical_root)
        object.__setattr__(self, "application_id", safe_application_id(self.application_id))
        object.__setattr__(self, "task_id", validate_runtime_id(self.task_id, field="task_id"))
        object.__setattr__(self, "run_id", validate_runtime_id(self.run_id, field="run_id"))

    @property
    def runtime_key(self) -> tuple[str, str, str, str]:
        """Stable process-local identity used to partition run-scoped state."""

        return (str(self.root_dir), self.application_id, self.task_id, self.run_id)

    def run_lease(self) -> RuntimeRunLease:
        return RuntimeRunLease(self.run_dir)

    @property
    def run_dir(self) -> Path:
        return self.root_dir / "runs" / Path(*self.application_id.split("/")) / self.run_id

    @property
    def manifest_path(self) -> Path:
        return self.run_dir / "manifest.json"

    @property
    def logs_dir(self) -> Path:
        return self.run_dir / "logs"

    @property
    def log_path(self) -> Path:
        return self.logs_dir / "runtime.log"

    @property
    def audit_dir(self) -> Path:
        return self.run_dir / "audit"

    @property
    def shell_audit_path(self) -> Path:
        return self.audit_dir / "shell.jsonl"

    @property
    def artifacts_dir(self) -> Path:
        return self.run_dir / "artifacts"

    @property
    def shell_artifacts_dir(self) -> Path:
        return self.artifacts_dir / "shell"

    @property
    def background_artifacts_dir(self) -> Path:
        return self.artifacts_dir / "background"

    @property
    def skill_artifacts_dir(self) -> Path:
        return self.artifacts_dir / "skills"

    def skill_workspace_dir(self, skill_name: str) -> Path:
        """Return the canonical workspace for one skill in this run."""

        component = _safe_named_part(
            skill_name,
            fallback="skill",
            include_hash_on_change=True,
        )
        return self.skill_artifacts_dir / component

    def new_skill_execution_dir(self, skill_name: str) -> Path:
        """Atomically allocate a unique audit directory for a skill execution."""

        executions_dir = self.skill_workspace_dir(skill_name) / "runs"
        executions_fd = _open_runtime_directory(
            executions_dir,
            root=self.root_dir,
            create=True,
        )
        try:
            while True:
                name = generate_runtime_id("execution")
                try:
                    os.mkdir(name, mode=0o700, dir_fd=executions_fd)
                except FileExistsError:
                    continue
                return executions_dir / name
        finally:
            os.close(executions_fd)

    def prepare_skill_workspace(self, skill_name: str) -> Path:
        """Create and return a skill workspace through the trusted path layer."""

        return _ensure_runtime_directory(
            self.skill_workspace_dir(skill_name),
            root=self.root_dir,
        )

    def allocate_artifact(
        self,
        kind: str,
        *,
        prefix: str,
        suffix: str,
    ) -> tuple[int, Path]:
        """Atomically allocate a run artifact and return its open descriptor."""

        directories = {
            "shell": self.shell_artifacts_dir,
            "background": self.background_artifacts_dir,
            "skills": self.skill_artifacts_dir,
        }
        try:
            directory = directories[kind]
        except KeyError as exc:
            raise ValueError(f"unknown run artifact kind: {kind}") from exc
        if any(separator in prefix or separator in suffix for separator in ("/", "\\", "\x00")):
            raise ValueError("artifact prefix and suffix must be filename fragments")

        directory_fd = _open_runtime_directory(
            directory,
            root=self.root_dir,
            create=True,
        )
        try:
            while True:
                name = f"{prefix}{uuid.uuid4().hex}{suffix}"
                try:
                    fd = os.open(
                        name,
                        os.O_RDWR | os.O_CREAT | os.O_EXCL | _FILE_NOFOLLOW,
                        0o600,
                        dir_fd=directory_fd,
                    )
                except FileExistsError:
                    continue
                return fd, directory / name
        finally:
            os.close(directory_fd)

    def create_run_file(self, path: Path) -> int:
        """Create one exclusive regular file below this run and return its fd.

        This is intended for subprocess stdout/stderr redirection: the child
        writes directly to the runtime-owned file, so arbitrarily large output
        never has to be buffered in the AgentLoom process.
        """

        target = _absolute_path_without_resolving_symlinks(path)
        try:
            target.relative_to(self.run_dir)
        except ValueError as exc:
            raise RuntimeError(f"run file escapes run directory: {target}") from exc
        parent_fd = _open_runtime_directory(
            target.parent,
            root=self.root_dir,
            create=False,
        )
        try:
            fd = os.open(
                target.name,
                os.O_RDWR | os.O_CREAT | os.O_EXCL | _FILE_NOFOLLOW,
                0o600,
                dir_fd=parent_fd,
            )
            if not stat.S_ISREG(os.fstat(fd).st_mode):
                os.close(fd)
                raise RuntimeError(f"run file is not regular: {target}")
            return fd
        finally:
            os.close(parent_fd)

    def atomic_write_run_file(
        self,
        path: Path,
        content: str | bytes,
        *,
        encoding: str = "utf-8",
    ) -> None:
        """Atomically replace one file below this run without following links."""

        target = _absolute_path_without_resolving_symlinks(path)
        try:
            target.relative_to(self.run_dir)
        except ValueError as exc:
            raise RuntimeError(f"run artifact escapes run directory: {target}") from exc
        parent_fd = _open_runtime_directory(
            target.parent,
            root=self.root_dir,
            create=False,
        )
        temporary = f".{target.name}.{uuid.uuid4().hex}.tmp"
        fd = -1
        try:
            try:
                existing = os.stat(target.name, dir_fd=parent_fd, follow_symlinks=False)
            except FileNotFoundError:
                existing = None
            if existing is not None and not stat.S_ISREG(existing.st_mode):
                raise RuntimeError(f"run file is not a regular file: {target}")
            fd = os.open(
                temporary,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | _FILE_NOFOLLOW,
                0o600,
                dir_fd=parent_fd,
            )
            payload = content.encode(encoding) if isinstance(content, str) else content
            with os.fdopen(fd, "wb", closefd=True) as stream:
                fd = -1
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(
                temporary,
                target.name,
                src_dir_fd=parent_fd,
                dst_dir_fd=parent_fd,
            )
        finally:
            if fd >= 0:
                os.close(fd)
            try:
                os.unlink(temporary, dir_fd=parent_fd)
            except FileNotFoundError:
                pass
            os.close(parent_fd)

    def atomic_write_run_file_chunks(
        self,
        path: Path,
        chunks: Iterable[str | bytes],
        *,
        encoding: str = "utf-8",
    ) -> int:
        """Atomically replace a run file while streaming bounded chunks."""

        target = _absolute_path_without_resolving_symlinks(path)
        try:
            target.relative_to(self.run_dir)
        except ValueError as exc:
            raise RuntimeError(f"run artifact escapes run directory: {target}") from exc
        parent_fd = _open_runtime_directory(
            target.parent,
            root=self.root_dir,
            create=False,
        )
        temporary = f".{target.name}.{uuid.uuid4().hex}.tmp"
        fd = -1
        bytes_written = 0
        try:
            try:
                existing = os.stat(target.name, dir_fd=parent_fd, follow_symlinks=False)
            except FileNotFoundError:
                existing = None
            if existing is not None and not stat.S_ISREG(existing.st_mode):
                raise RuntimeError(f"run file is not a regular file: {target}")
            fd = os.open(
                temporary,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | _FILE_NOFOLLOW,
                0o600,
                dir_fd=parent_fd,
            )
            with os.fdopen(fd, "wb", closefd=True) as stream:
                fd = -1
                for chunk in chunks:
                    payload = chunk.encode(encoding) if isinstance(chunk, str) else chunk
                    stream.write(payload)
                    bytes_written += len(payload)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(
                temporary,
                target.name,
                src_dir_fd=parent_fd,
                dst_dir_fd=parent_fd,
            )
            return bytes_written
        finally:
            if fd >= 0:
                os.close(fd)
            try:
                os.unlink(temporary, dir_fd=parent_fd)
            except FileNotFoundError:
                pass
            os.close(parent_fd)

    def read_run_file(self, path: Path, *, encoding: str = "utf-8") -> str:
        """Read one regular file below this run through a trusted directory fd."""

        target = _absolute_path_without_resolving_symlinks(path)
        try:
            target.relative_to(self.run_dir)
        except ValueError as exc:
            raise RuntimeError(f"run file escapes run directory: {target}") from exc
        parent_fd = _open_runtime_directory(target.parent, root=self.root_dir)
        try:
            fd = os.open(target.name, os.O_RDONLY | _FILE_NOFOLLOW, dir_fd=parent_fd)
            try:
                if not stat.S_ISREG(os.fstat(fd).st_mode):
                    raise RuntimeError(f"run file is not regular: {target}")
                with os.fdopen(fd, "r", encoding=encoding, closefd=True) as stream:
                    fd = -1
                    return stream.read()
            finally:
                if fd >= 0:
                    os.close(fd)
        finally:
            os.close(parent_fd)

    def remove_run_file(self, path: Path) -> None:
        """Unlink one run file without following a replaced parent directory."""

        target = _absolute_path_without_resolving_symlinks(path)
        try:
            target.relative_to(self.run_dir)
        except ValueError as exc:
            raise RuntimeError(f"run file escapes run directory: {target}") from exc
        parent_fd = _open_runtime_directory(target.parent, root=self.root_dir)
        try:
            os.unlink(target.name, dir_fd=parent_fd)
        finally:
            os.close(parent_fd)

    @property
    def checkpoint_dir(self) -> Path:
        return self.root_dir / "checkpoints" / Path(*self.application_id.split("/")) / self.task_id

    @property
    def task_events_path(self) -> Path:
        return self.checkpoint_dir / "task_events.jsonl"

    @property
    def task_tree_path(self) -> Path:
        return self.checkpoint_dir / "task_tree.json"

    @property
    def checkpoint_path(self) -> Path:
        return self.checkpoint_dir / "checkpoint.json"

    @property
    def heartbeat_path(self) -> Path:
        return self.checkpoint_dir / "heartbeat.json"

    @property
    def workers_dir(self) -> Path:
        return self.checkpoint_dir / "workers"

    @property
    def context_store_dir(self) -> Path:
        return self.checkpoint_dir / "context_store"

    @property
    def file_history_dir(self) -> Path:
        return self.checkpoint_dir / "file-history"

    def prepare_run(self) -> None:
        _ensure_runtime_directory(self.run_dir, root=self.root_dir)
        self.atomic_write_run_file(
            self.run_dir / ".run-starting.json",
            json.dumps(
                {
                    "application_id": self.application_id,
                    "task_id": self.task_id,
                    "run_id": self.run_id,
                },
                ensure_ascii=False,
                sort_keys=True,
            )
            + "\n",
        )
        _ensure_runtime_directory(self.logs_dir, root=self.root_dir)
        _ensure_runtime_directory(self.audit_dir, root=self.root_dir)
        for path in (
            self.shell_artifacts_dir,
            self.background_artifacts_dir,
            self.skill_artifacts_dir,
        ):
            _ensure_runtime_directory(path, root=self.root_dir)

    def prepare_checkpoint(self) -> None:
        self.validate_checkpoint_path()
        _ensure_runtime_directory(self.checkpoint_dir, root=self.root_dir)

    def validate_checkpoint_path(self, *, require_exists: bool = False) -> Path:
        """Validate the full canonical checkpoint path without following links."""

        path = validate_runtime_owned_path(self.checkpoint_dir, root=self.root_dir)
        if require_exists and not path.is_dir():
            raise FileNotFoundError(f"checkpoint directory does not exist: {self.checkpoint_dir}")
        return path

    def write_manifest(self, **extra: Any) -> None:
        payload: dict[str, Any] = {
            "schema_version": 1,
            "application_id": self.application_id,
            "task_id": self.task_id,
            "run_id": self.run_id,
            "started_at": datetime.now(UTC).isoformat(),
            "status": "running",
        }
        payload.update(extra)
        self.atomic_write_run_file(
            self.manifest_path,
            json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        )
        try:
            self.remove_run_file(self.run_dir / ".run-starting.json")
        except FileNotFoundError:
            pass

    def update_manifest(self, **updates: Any) -> None:
        payload: dict[str, Any] = {}
        if self.manifest_path.exists():
            try:
                loaded = json.loads(self.read_run_file(self.manifest_path))
                if isinstance(loaded, dict):
                    payload = loaded
            except (OSError, json.JSONDecodeError):
                payload = {}
        payload.update(updates)
        self.atomic_write_run_file(
            self.manifest_path,
            json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        )

    def as_dict(self) -> dict[str, str]:
        data = asdict(self)
        data["root_dir"] = str(self.root_dir)
        return data


class RuntimeHome:
    """Factory for canonical run and task contexts under one root."""

    def __init__(self, root_dir: str | Path) -> None:
        self.root_dir = _absolute_path_without_resolving_symlinks(root_dir)

    def validate_root(self) -> Path:
        """Reject a runtime root that redirects storage through a symlink."""

        return validate_runtime_owned_path(self.root_dir, root=self.root_dir)

    @property
    def checkpoints_root(self) -> Path:
        self.validate_root()
        return self.root_dir / "checkpoints"

    @property
    def runs_root(self) -> Path:
        self.validate_root()
        return self.root_dir / "runs"

    def context(
        self,
        *,
        application_id: str,
        task_id: str,
        run_id: str,
    ) -> RuntimeContext:
        return RuntimeContext(
            root_dir=self.root_dir,
            application_id=safe_application_id(application_id),
            task_id=validate_runtime_id(task_id, field="task_id"),
            run_id=validate_runtime_id(run_id, field="run_id"),
        )

    def new_context(
        self,
        *,
        application_id: str,
        task_id: str | None = None,
        run_id: str | None = None,
    ) -> RuntimeContext:
        return self.context(
            application_id=application_id,
            task_id=task_id or generate_runtime_id("task"),
            run_id=run_id or generate_runtime_id("run"),
        )


def resolve_runtime_home(
    effective_config: dict[str, Any] | None,
    *,
    agent_root: str | Path,
) -> RuntimeHome:
    section = effective_config.get("runtime", {}) if isinstance(effective_config, dict) else {}
    if not isinstance(section, dict):
        section = {}
    # Subprocess harnesses may override the one canonical home, but the
    # override applies to every runtime consumer.  There is deliberately no
    # self-learning-only environment variable.
    root_override = os.environ.get(RUNTIME_ROOT_ENV, "").strip()
    configured = Path(root_override or str(section.get("root_dir", ".agentloom"))).expanduser()
    if not configured.is_absolute():
        configured = Path(agent_root).expanduser() / configured
    return RuntimeHome(configured)


def resolve_application_id(
    agent_config: dict[str, Any] | None,
    workflow_path: str | Path,
    *,
    agent_root: str | Path | None = None,
) -> str:
    """Resolve a canonical Application id, with a hashed external fallback."""

    from src.extensions.self_learning.application_scope import resolve_application_scope

    scope = resolve_application_scope(agent_config, workflow_path=workflow_path)
    if scope.application_id:
        return safe_application_id(scope.application_id)

    path = Path(workflow_path).expanduser()
    if agent_root is not None:
        applications_root = Path(agent_root).expanduser().resolve() / "applications"
        try:
            relative = path.resolve().relative_to(applications_root)
            parts = relative.parts
            if "workflows" in parts:
                workflow_index = parts.index("workflows")
                if workflow_index > 0:
                    return safe_application_id("/".join(parts[:workflow_index]))
        except (OSError, ValueError):
            pass
    hint = str((agent_config or {}).get("name") or path.stem or "external")
    return fallback_application_id(path, name_hint=hint)


def get_current_run_context(*, required: bool = False) -> RuntimeContext | None:
    context = _CURRENT_RUN_CONTEXT.get()
    if context is None and required:
        raise RuntimeError("no AgentLoom RuntimeContext is bound to this execution context")
    return context


@contextmanager
def bind_run_context(context: RuntimeContext) -> Iterator[RuntimeContext]:
    token = _CURRENT_RUN_CONTEXT.set(context)
    try:
        yield context
    finally:
        _CURRENT_RUN_CONTEXT.reset(token)


def copy_runtime_context() -> contextvars.Context:
    """Capture the current context for explicit propagation to a worker thread."""

    return contextvars.copy_context()


class RuntimeRotatingTextSink:
    """Bounded append-only text sink anchored to a validated run directory."""

    def __init__(
        self,
        context: RuntimeContext,
        path: Path,
        *,
        max_file_bytes: int,
        backup_count: int,
        encoding: str = "utf-8",
    ) -> None:
        target = _absolute_path_without_resolving_symlinks(path)
        try:
            target.relative_to(context.run_dir)
        except ValueError as exc:
            raise RuntimeError(f"log path escapes run directory: {target}") from exc
        self.path = target
        self.max_file_bytes = max(1, int(max_file_bytes))
        self.backup_count = max(0, int(backup_count))
        self.encoding = encoding
        self._directory_fd = _open_runtime_directory(
            target.parent,
            root=context.root_dir,
            create=True,
        )
        self._stream: TextIO | None = None
        self._lock = threading.RLock()
        self._closed = False

    @property
    def stream(self) -> TextIO | None:
        return self._stream

    def _open_unlocked(self) -> TextIO:
        if self._closed:
            raise RuntimeError("runtime text sink is closed")
        if self._stream is None:
            fd = os.open(
                self.path.name,
                os.O_WRONLY | os.O_APPEND | os.O_CREAT | _FILE_NOFOLLOW,
                0o600,
                dir_fd=self._directory_fd,
            )
            if not stat.S_ISREG(os.fstat(fd).st_mode):
                os.close(fd)
                raise RuntimeError(f"runtime log is not regular: {self.path}")
            self._stream = os.fdopen(fd, "a", encoding=self.encoding)
        return self._stream

    def _regular_exists_unlocked(self, name: str) -> bool:
        try:
            metadata = os.stat(name, dir_fd=self._directory_fd, follow_symlinks=False)
        except FileNotFoundError:
            return False
        if not stat.S_ISREG(metadata.st_mode):
            raise RuntimeError(f"runtime log generation is not regular: {self.path.parent / name}")
        return True

    def _rollover_unlocked(self) -> None:
        if self._stream is not None:
            self._stream.close()
            self._stream = None
        active = self.path.name
        if self.backup_count == 0:
            try:
                os.unlink(active, dir_fd=self._directory_fd)
            except FileNotFoundError:
                pass
            return
        oldest = f"{active}.{self.backup_count}"
        try:
            os.unlink(oldest, dir_fd=self._directory_fd)
        except FileNotFoundError:
            pass
        for index in range(self.backup_count - 1, 0, -1):
            source = f"{active}.{index}"
            if self._regular_exists_unlocked(source):
                os.replace(
                    source,
                    f"{active}.{index + 1}",
                    src_dir_fd=self._directory_fd,
                    dst_dir_fd=self._directory_fd,
                )
        if self._regular_exists_unlocked(active):
            os.replace(
                active,
                f"{active}.1",
                src_dir_fd=self._directory_fd,
                dst_dir_fd=self._directory_fd,
            )

    def write(self, text: str) -> None:
        if not text:
            return
        encoded_size = len(text.encode(self.encoding))
        with self._lock:
            stream = self._open_unlocked()
            current_size = os.fstat(stream.fileno()).st_size
            if current_size > 0 and current_size + encoded_size > self.max_file_bytes:
                self._rollover_unlocked()
                stream = self._open_unlocked()
            stream.write(text)
            stream.flush()

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
            if self._stream is not None:
                self._stream.close()
                self._stream = None
            os.close(self._directory_fd)
