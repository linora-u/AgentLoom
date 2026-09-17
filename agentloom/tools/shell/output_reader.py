"""Descriptor-anchored reads for shell output artifacts.

Runtime artifact paths are useful labels, but they are not stable capabilities:
an ancestor directory can be renamed or replaced after a process starts.  This
module opens the output inode once and performs every subsequent size/tail read
through that descriptor, so another run can never be observed through a stale
pathname.
"""

from __future__ import annotations

import os
import stat
import threading
from pathlib import Path

_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)


def _open_regular_file_no_follow(path: str | os.PathLike[str]) -> int:
    """Open a regular final path component without following that component.

    Runtime callers should pass the already-open writer FD, which gives a
    stronger inode identity than any pathname can.  This fallback exists for
    legacy standalone callers and anchors the file once; later reads never
    resolve the path again.
    """

    absolute = Path(os.path.abspath(os.fspath(path)))
    if not absolute.name:
        raise OSError(f"output path is not a regular file: {absolute}")
    fd = os.open(absolute, os.O_RDONLY | _NOFOLLOW)
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise OSError(f"output path is not a regular file: {absolute}")
        os.set_inheritable(fd, False)
        return fd
    except BaseException:
        os.close(fd)
        raise


class AnchoredOutputReader:
    """Own a read descriptor for one immutable output-file identity."""

    def __init__(self, fd: int):
        file_stat = os.fstat(fd)
        if not stat.S_ISREG(file_stat.st_mode):
            raise OSError("shell output descriptor is not a regular file")
        self._fd = fd
        self._identity = (file_stat.st_dev, file_stat.st_ino)
        self._lock = threading.Lock()

    @classmethod
    def from_path(cls, path: str | os.PathLike[str]) -> AnchoredOutputReader:
        """Securely anchor an existing regular file by pathname."""

        return cls(_open_regular_file_no_follow(path))

    @classmethod
    def from_fd(
        cls,
        fd: int,
        *,
        path: str | os.PathLike[str] | None = None,
    ) -> AnchoredOutputReader:
        """Anchor *fd*, reopening and inode-checking write-only descriptors."""

        expected = os.fstat(fd)
        if not stat.S_ISREG(expected.st_mode):
            raise OSError("shell output descriptor is not a regular file")

        duplicate = os.dup(fd)
        os.set_inheritable(duplicate, False)
        try:
            # A zero-byte positional read checks access mode without changing
            # the shared file offset.
            os.pread(duplicate, 0, 0)
        except OSError as exc:
            os.close(duplicate)
            if path is None:
                raise OSError("a write-only output descriptor requires its original path") from exc
            duplicate = _open_regular_file_no_follow(path)
            actual = os.fstat(duplicate)
            if (actual.st_dev, actual.st_ino) != (expected.st_dev, expected.st_ino):
                os.close(duplicate)
                raise RuntimeError("shell output path no longer identifies its writer inode") from None

        return cls(duplicate)

    @property
    def closed(self) -> bool:
        with self._lock:
            return self._fd < 0

    @property
    def identity(self) -> tuple[int, int]:
        return self._identity

    def size(self) -> int:
        """Return the current inode size, or zero once unavailable."""

        with self._lock:
            if self._fd < 0:
                return 0
            try:
                return os.fstat(self._fd).st_size
            except OSError:
                return 0

    def read_tail(self, max_bytes: int) -> bytes:
        """Read at most *max_bytes* from the current inode tail."""

        if max_bytes <= 0:
            return b""
        with self._lock:
            if self._fd < 0:
                return b""
            try:
                size = os.fstat(self._fd).st_size
                offset = max(0, size - max_bytes)
                return os.pread(self._fd, min(max_bytes, size), offset)
            except OSError:
                return b""

    def close(self) -> None:
        """Close the owned descriptor exactly once."""

        with self._lock:
            if self._fd < 0:
                return
            fd, self._fd = self._fd, -1
            os.close(fd)

    def __del__(self) -> None:
        # Keep direct BackgroundTaskState construction from leaking a file
        # descriptor.  Registry-owned readers are still closed deterministically.
        try:
            self.close()
        except (AttributeError, OSError):
            pass
