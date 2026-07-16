"""Descriptor-anchored storage primitives for runtime-owned state."""

from __future__ import annotations

import fcntl
import json
import os
import stat
import threading
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path, PurePosixPath
from typing import Any

_DIRECTORY_FLAGS = (
    os.O_RDONLY
    | getattr(os, "O_DIRECTORY", 0)
    | getattr(os, "O_NOFOLLOW", 0)
)
_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)
_COPY_CHUNK_BYTES = 1024 * 1024


def _write_all(fd: int, payload: bytes) -> None:
    view = memoryview(payload)
    while view:
        try:
            written = os.write(fd, view)
        except InterruptedError:
            continue
        if written <= 0:
            raise OSError("short write while copying runtime state")
        view = view[written:]


def _copy_fd(source_fd: int, destination_fd: int) -> None:
    while True:
        try:
            chunk = os.read(source_fd, _COPY_CHUNK_BYTES)
        except InterruptedError:
            continue
        if not chunk:
            return
        _write_all(destination_fd, chunk)


def _apply_copied_metadata(fd: int, source_stat: os.stat_result) -> None:
    """Preserve the copy2 metadata required by file-history rewind."""

    os.fchmod(fd, stat.S_IMODE(source_stat.st_mode))
    os.utime(fd, ns=(source_stat.st_atime_ns, source_stat.st_mtime_ns))


def _read_up_to(fd: int, limit: int) -> bytes:
    chunks: list[bytes] = []
    remaining = limit
    while remaining:
        try:
            chunk = os.read(fd, remaining)
        except InterruptedError:
            continue
        if not chunk:
            break
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


class SecureDirectory:
    """Keep storage anchored to one directory inode across pathname changes."""

    def __init__(self, path: str | Path, *, create: bool = True) -> None:
        self.path = Path(os.path.abspath(os.fspath(Path(path).expanduser())))
        self._lock = threading.RLock()
        self._fd = self._open_root(create=create)

    def _open_root(self, *, create: bool) -> int:
        parent = self.path.parent
        if create:
            parent.mkdir(parents=True, exist_ok=True)
        parent_fd = os.open(parent, _DIRECTORY_FLAGS)
        try:
            if create:
                try:
                    os.mkdir(self.path.name, mode=0o700, dir_fd=parent_fd)
                except FileExistsError:
                    pass
            return os.open(self.path.name, _DIRECTORY_FLAGS, dir_fd=parent_fd)
        finally:
            os.close(parent_fd)

    @classmethod
    def _from_fd(cls, path: Path, fd: int) -> SecureDirectory:
        instance = cls.__new__(cls)
        instance.path = path
        instance._lock = threading.RLock()
        instance._fd = fd
        return instance

    def duplicate(self) -> SecureDirectory:
        with self._lock:
            if self._fd < 0:
                raise RuntimeError(f"secure directory is closed: {self.path}")
            return self._from_fd(self.path, os.dup(self._fd))

    def child(self, relative: str | Path, *, create: bool = True) -> SecureDirectory:
        parts = self._parts(relative)
        fd = self._open_dir(parts, create=create)
        return self._from_fd(self.path.joinpath(*parts), fd)

    @property
    def closed(self) -> bool:
        return self._fd < 0

    def _parts(self, relative: str | Path) -> tuple[str, ...]:
        path = PurePosixPath(str(relative).replace("\\", "/"))
        if path.is_absolute() or not path.parts or any(
            part in {"", ".", ".."} for part in path.parts
        ):
            raise ValueError(f"unsafe relative storage path: {relative}")
        return path.parts

    def _open_dir(self, parts: tuple[str, ...], *, create: bool) -> int:
        with self._lock:
            if self._fd < 0:
                raise RuntimeError(f"secure directory is closed: {self.path}")
            current = os.dup(self._fd)
        try:
            for part in parts:
                try:
                    next_fd = os.open(part, _DIRECTORY_FLAGS, dir_fd=current)
                except FileNotFoundError:
                    if not create:
                        raise
                    try:
                        os.mkdir(part, mode=0o700, dir_fd=current)
                    except FileExistsError:
                        pass
                    next_fd = os.open(part, _DIRECTORY_FLAGS, dir_fd=current)
                os.close(current)
                current = next_fd
            return current
        except BaseException:
            os.close(current)
            raise

    def ensure_dir(self, relative: str | Path) -> None:
        fd = self._open_dir(self._parts(relative), create=True)
        os.close(fd)

    def _open_parent(self, relative: str | Path, *, create: bool) -> tuple[int, str]:
        parts = self._parts(relative)
        parent_fd = self._open_dir(parts[:-1], create=create)
        return parent_fd, parts[-1]

    def atomic_write(
        self,
        relative: str | Path,
        payload: bytes,
        *,
        mode: int = 0o600,
    ) -> None:
        parent_fd, name = self._open_parent(relative, create=True)
        temporary = f".{name}.{uuid.uuid4().hex}.tmp"
        file_fd = -1
        try:
            try:
                existing = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
            except FileNotFoundError:
                existing = None
            if existing is not None and not stat.S_ISREG(existing.st_mode):
                raise RuntimeError(f"storage target is not regular: {self.path / str(relative)}")
            file_fd = os.open(
                temporary,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | _NOFOLLOW,
                mode,
                dir_fd=parent_fd,
            )
            with os.fdopen(file_fd, "wb", closefd=True) as stream:
                file_fd = -1
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(
                temporary,
                name,
                src_dir_fd=parent_fd,
                dst_dir_fd=parent_fd,
            )
        finally:
            if file_fd >= 0:
                os.close(file_fd)
            try:
                os.unlink(temporary, dir_fd=parent_fd)
            except FileNotFoundError:
                pass
            os.close(parent_fd)

    def atomic_write_text(
        self,
        relative: str | Path,
        text: str,
        *,
        encoding: str = "utf-8",
    ) -> None:
        self.atomic_write(relative, text.encode(encoding))

    def atomic_write_json(self, relative: str | Path, payload: Any) -> None:
        self.atomic_write_text(
            relative,
            json.dumps(payload, ensure_ascii=False, indent=2, default=str) + "\n",
        )

    def append_text(
        self,
        relative: str | Path,
        text: str,
        *,
        ensure_line_boundary: bool = False,
        encoding: str = "utf-8",
    ) -> None:
        parent_fd, name = self._open_parent(relative, create=True)
        fd = -1
        try:
            fd = os.open(
                name,
                os.O_RDWR | os.O_APPEND | os.O_CREAT | _NOFOLLOW,
                0o600,
                dir_fd=parent_fd,
            )
            if not stat.S_ISREG(os.fstat(fd).st_mode):
                raise RuntimeError(f"storage target is not regular: {self.path / str(relative)}")
            payload = text.encode(encoding)
            if ensure_line_boundary and os.fstat(fd).st_size > 0:
                os.lseek(fd, -1, os.SEEK_END)
                if os.read(fd, 1) != b"\n":
                    payload = b"\n" + payload
            view = memoryview(payload)
            while view:
                try:
                    written = os.write(fd, view)
                except InterruptedError:
                    continue
                if written <= 0:
                    raise OSError("short write while appending runtime state")
                view = view[written:]
            os.fsync(fd)
        finally:
            if fd >= 0:
                os.close(fd)
            os.close(parent_fd)

    @contextmanager
    def advisory_file_lock(
        self,
        relative: str | Path,
        *,
        create: bool = False,
    ) -> Iterator[None]:
        """Hold an exclusive advisory lock on one anchored regular file."""

        parent_fd, name = self._open_parent(relative, create=create)
        fd = -1
        locked = False
        try:
            flags = os.O_RDWR | _NOFOLLOW
            if create:
                flags |= os.O_CREAT
            fd = os.open(name, flags, 0o600, dir_fd=parent_fd)
            if not stat.S_ISREG(os.fstat(fd).st_mode):
                raise RuntimeError(
                    f"storage lock target is not regular: {self.path / str(relative)}"
                )
            fcntl.flock(fd, fcntl.LOCK_EX)
            locked = True
            yield
        finally:
            if fd >= 0:
                try:
                    if locked:
                        fcntl.flock(fd, fcntl.LOCK_UN)
                finally:
                    os.close(fd)
            os.close(parent_fd)

    def read_bytes(self, relative: str | Path) -> bytes:
        parent_fd, name = self._open_parent(relative, create=False)
        fd = -1
        try:
            fd = os.open(name, os.O_RDONLY | _NOFOLLOW, dir_fd=parent_fd)
            if not stat.S_ISREG(os.fstat(fd).st_mode):
                raise RuntimeError(f"storage source is not regular: {self.path / str(relative)}")
            chunks: list[bytes] = []
            while True:
                chunk = os.read(fd, 1024 * 1024)
                if not chunk:
                    break
                chunks.append(chunk)
            return b"".join(chunks)
        finally:
            if fd >= 0:
                os.close(fd)
            os.close(parent_fd)

    def read_text(self, relative: str | Path, *, encoding: str = "utf-8") -> str:
        return self.read_bytes(relative).decode(encoding)

    def read_json(self, relative: str | Path) -> Any:
        return json.loads(self.read_text(relative))

    def regular_file_names(self, relative_dir: str | Path) -> list[str]:
        directory_fd = self._open_dir(self._parts(relative_dir), create=False)
        try:
            names: list[str] = []
            for name in os.listdir(directory_fd):
                metadata = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
                if stat.S_ISREG(metadata.st_mode):
                    names.append(name)
            return sorted(names)
        finally:
            os.close(directory_fd)

    def directory_names(self, relative_dir: str | Path) -> list[str]:
        directory_fd = self._open_dir(self._parts(relative_dir), create=False)
        try:
            names: list[str] = []
            for name in os.listdir(directory_fd):
                metadata = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
                if stat.S_ISDIR(metadata.st_mode):
                    names.append(name)
            return sorted(names)
        finally:
            os.close(directory_fd)

    def stat_file(self, relative: str | Path) -> os.stat_result:
        parent_fd, name = self._open_parent(relative, create=False)
        try:
            metadata = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
            if not stat.S_ISREG(metadata.st_mode):
                raise RuntimeError(f"storage source is not regular: {self.path / str(relative)}")
            return metadata
        finally:
            os.close(parent_fd)

    def unlink(self, relative: str | Path) -> None:
        parent_fd, name = self._open_parent(relative, create=False)
        try:
            os.unlink(name, dir_fd=parent_fd)
        finally:
            os.close(parent_fd)

    def copy_from(self, source: str | Path, relative: str | Path) -> None:
        parent_fd, name = self._open_parent(relative, create=True)
        temporary = f".{name}.{uuid.uuid4().hex}.tmp"
        source_fd = -1
        destination_fd = -1
        try:
            try:
                existing = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
            except FileNotFoundError:
                existing = None
            if existing is not None and not stat.S_ISREG(existing.st_mode):
                raise RuntimeError(f"storage target is not regular: {self.path / str(relative)}")
            source_fd = os.open(source, os.O_RDONLY)
            source_stat = os.fstat(source_fd)
            if not stat.S_ISREG(source_stat.st_mode):
                raise RuntimeError(f"copy source is not regular: {source}")
            destination_fd = os.open(
                temporary,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | _NOFOLLOW,
                0o600,
                dir_fd=parent_fd,
            )
            _copy_fd(source_fd, destination_fd)
            _apply_copied_metadata(destination_fd, source_stat)
            os.fsync(destination_fd)
            os.close(destination_fd)
            destination_fd = -1
            os.replace(
                temporary,
                name,
                src_dir_fd=parent_fd,
                dst_dir_fd=parent_fd,
            )
        finally:
            if source_fd >= 0:
                os.close(source_fd)
            if destination_fd >= 0:
                os.close(destination_fd)
            try:
                os.unlink(temporary, dir_fd=parent_fd)
            except FileNotFoundError:
                pass
            os.close(parent_fd)

    def copy_to(self, relative: str | Path, destination: str | Path) -> None:
        target = Path(destination)
        target.parent.mkdir(parents=True, exist_ok=True)
        source_parent_fd = -1
        target_parent_fd = -1
        temporary = f".{target.name}.{uuid.uuid4().hex}.tmp"
        source_fd = -1
        destination_fd = -1
        try:
            source_parent_fd, source_name = self._open_parent(relative, create=False)
            target_parent_fd = os.open(target.parent, _DIRECTORY_FLAGS)
            source_fd = os.open(
                source_name,
                os.O_RDONLY | _NOFOLLOW,
                dir_fd=source_parent_fd,
            )
            source_stat = os.fstat(source_fd)
            if not stat.S_ISREG(source_stat.st_mode):
                raise RuntimeError(f"storage source is not regular: {self.path / str(relative)}")
            try:
                existing = os.stat(
                    target.name,
                    dir_fd=target_parent_fd,
                    follow_symlinks=False,
                )
            except FileNotFoundError:
                existing = None
            if existing is not None and not stat.S_ISREG(existing.st_mode):
                raise RuntimeError(f"copy destination is not regular: {target}")
            destination_fd = os.open(
                temporary,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | _NOFOLLOW,
                stat.S_IMODE(source_stat.st_mode),
                dir_fd=target_parent_fd,
            )
            _copy_fd(source_fd, destination_fd)
            _apply_copied_metadata(destination_fd, source_stat)
            os.fsync(destination_fd)
            os.close(destination_fd)
            destination_fd = -1
            os.replace(
                temporary,
                target.name,
                src_dir_fd=target_parent_fd,
                dst_dir_fd=target_parent_fd,
            )
        finally:
            if source_fd >= 0:
                os.close(source_fd)
            if destination_fd >= 0:
                os.close(destination_fd)
            if target_parent_fd >= 0:
                try:
                    os.unlink(temporary, dir_fd=target_parent_fd)
                except FileNotFoundError:
                    pass
            if source_parent_fd >= 0:
                os.close(source_parent_fd)
            if target_parent_fd >= 0:
                os.close(target_parent_fd)

    def same_content_as(self, relative: str | Path, source: str | Path) -> bool:
        parent_fd = -1
        stored_fd = -1
        source_fd = -1
        try:
            parent_fd, name = self._open_parent(relative, create=False)
            stored_fd = os.open(name, os.O_RDONLY | _NOFOLLOW, dir_fd=parent_fd)
            source_fd = os.open(source, os.O_RDONLY)
            if not stat.S_ISREG(os.fstat(stored_fd).st_mode):
                return False
            if not stat.S_ISREG(os.fstat(source_fd).st_mode):
                return False
            while True:
                stored = _read_up_to(stored_fd, _COPY_CHUNK_BYTES)
                current = _read_up_to(source_fd, _COPY_CHUNK_BYTES)
                if stored != current:
                    return False
                if not stored:
                    return True
        except (OSError, RuntimeError):
            return False
        finally:
            if stored_fd >= 0:
                os.close(stored_fd)
            if source_fd >= 0:
                os.close(source_fd)
            if parent_fd >= 0:
                os.close(parent_fd)

    def matches_path(self) -> bool:
        if self._fd < 0 or self.path.is_symlink():
            return False
        try:
            current = self.path.stat()
            anchored = os.fstat(self._fd)
        except OSError:
            return False
        return (current.st_dev, current.st_ino) == (anchored.st_dev, anchored.st_ino)

    def close(self) -> None:
        with self._lock:
            fd, self._fd = self._fd, -1
        if fd >= 0:
            os.close(fd)

    def __enter__(self) -> SecureDirectory:
        return self

    def __exit__(self, exc_type, exc, traceback) -> None:
        self.close()

    def __del__(self) -> None:
        try:
            self.close()
        except Exception:
            pass
