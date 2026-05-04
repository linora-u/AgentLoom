"""
Read-file state cache — deduplication and staleness detection.

Aligned with the ReadFileState mechanism from upstream reference:

1. **Read dedup**: If the same file+range has already been read and the file
   has not changed on disk (mtime check), return a short stub instead of
   the full content — saving LLM tokens.

2. **Staleness guard**: Before ``edit_file`` / ``write_file`` modify a file,
   verify that the file was previously read *and* has not been modified
   externally since that read.  Reject stale edits to prevent data loss.
"""

from __future__ import annotations

import os
import threading
from collections import OrderedDict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional, Union

from src.lib.logging import get_logger

logger = get_logger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
MAX_ENTRIES: int = 100
FILE_UNCHANGED_STUB: str = (
    "File unchanged since last read. The content from the earlier read_file "
    "tool result in this conversation is still current — refer to that "
    "instead of re-reading."
)


# ---------------------------------------------------------------------------
# Per-file state
# ---------------------------------------------------------------------------
@dataclass(slots=True)
class _FileEntry:
    """Cached state for a single file path."""
    content: str
    mtime_ns: int       # os.stat().st_mtime_ns for nanosecond precision
    offset: Optional[int]   # 1-based start line (None after edit/write)
    limit: Optional[int]    # lines requested       (None after edit/write)


# ---------------------------------------------------------------------------
# LRU cache with thread safety
# ---------------------------------------------------------------------------
class ReadFileState:
    """Thread-safe LRU cache tracking per-file read state.

    Public API
    ----------
    set(path, content, mtime_ns, offset, limit)
        Record that *path* was read (or written).

    check_dedup(path, offset, limit) -> str | None
        If the same range was read before and mtime has not changed,
        return ``FILE_UNCHANGED_STUB``.  Otherwise return ``None``.

    check_staleness(path) -> str | None
        If *path* was never read, or has been modified since the last
        read, return a human-readable error message.
        Otherwise return ``None`` (= safe to edit).

    update_after_write(path, new_content)
        Refresh cache after a successful edit/write.  Sets
        ``offset=None`` so the next dedup check forces a real read.

    clear()
        Drop all entries (useful in tests).
    """

    def __init__(self, max_entries: int = MAX_ENTRIES) -> None:
        self._max = max_entries
        self._lock = threading.Lock()
        self._cache: OrderedDict[str, _FileEntry] = OrderedDict()

    # -- helpers -----------------------------------------------------------

    @staticmethod
    def _key(path: Union[str, Path]) -> str:
        return str(Path(path).resolve())

    @staticmethod
    def _mtime_ns(path: Union[str, Path]) -> int:
        """Return nanosecond mtime, or 0 if the file does not exist."""
        try:
            return os.stat(path).st_mtime_ns
        except OSError:
            return 0

    def _evict(self) -> None:
        """Remove oldest entries until within *max_entries*."""
        while len(self._cache) > self._max:
            self._cache.popitem(last=False)

    # -- public API --------------------------------------------------------

    def set(
        self,
        path: Union[str, Path],
        content: str,
        mtime_ns: int,
        offset: Optional[int],
        limit: Optional[int],
    ) -> None:
        """Record a read (or post-write refresh)."""
        key = self._key(path)
        with self._lock:
            # Move to end (most recently used)
            self._cache.pop(key, None)
            self._cache[key] = _FileEntry(
                content=content,
                mtime_ns=mtime_ns,
                offset=offset,
                limit=limit,
            )
            self._evict()

    def check_dedup(
        self,
        path: Union[str, Path],
        offset: int,
        limit: int,
    ) -> Optional[str]:
        """Return ``FILE_UNCHANGED_STUB`` if the read can be skipped.

        Dedup criteria (all must be true):
        - Entry exists for *path*
        - ``entry.offset`` is not ``None`` (entry came from a read, not an edit)
        - offset and limit match the cached values
        - File mtime has not changed on disk
        """
        key = self._key(path)
        with self._lock:
            entry = self._cache.get(key)
        if entry is None:
            return None
        if entry.offset is None:
            # Entry was set by update_after_write — force a real read
            return None
        if entry.offset != offset or entry.limit != limit:
            return None

        current_mtime = self._mtime_ns(path)
        if current_mtime != entry.mtime_ns:
            return None

        logger.debug("read dedup hit for %s (offset=%d, limit=%d)", path, offset, limit)
        return FILE_UNCHANGED_STUB

    def check_staleness(self, path: Union[str, Path]) -> Optional[str]:
        """Return an error message if editing *path* would be unsafe.

        Returns ``None`` when the edit is safe to proceed.
        """
        key = self._key(path)
        resolved = Path(path).resolve()

        with self._lock:
            entry = self._cache.get(key)

        if entry is None:
            return (
                f"File '{resolved}' has not been read yet. "
                "Use read_file first before editing."
            )

        current_mtime = self._mtime_ns(resolved)
        if current_mtime != entry.mtime_ns:
            # Content comparison fallback (handles cloud-sync timestamp drift)
            try:
                disk_content = resolved.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                disk_content = None

            if disk_content is not None and disk_content == entry.content:
                # Content identical despite mtime difference — safe
                return None

            return (
                f"File '{resolved}' has been modified since the last read. "
                "Read the file again before editing."
            )

        return None

    def update_after_write(
        self,
        path: Union[str, Path],
        new_content: str,
    ) -> None:
        """Refresh cache after a successful edit or write.

        Sets ``offset=None`` so the next ``check_dedup`` will force a real
        read (prevents stale dedup against pre-edit content).
        """
        key = self._key(path)
        resolved = Path(path).resolve()
        mtime_ns = self._mtime_ns(resolved)
        with self._lock:
            self._cache.pop(key, None)
            self._cache[key] = _FileEntry(
                content=new_content,
                mtime_ns=mtime_ns,
                offset=None,
                limit=None,
            )
            self._evict()

    def get_entry(self, path: Union[str, Path]) -> Optional[_FileEntry]:
        """Return the cached entry for *path*, or ``None``."""
        key = self._key(path)
        with self._lock:
            return self._cache.get(key)

    def clear(self) -> None:
        """Remove all entries."""
        with self._lock:
            self._cache.clear()


# ---------------------------------------------------------------------------
# Module-level singleton
# ---------------------------------------------------------------------------
_read_file_state = ReadFileState()


def get_read_file_state() -> ReadFileState:
    """Return the process-global ``ReadFileState`` instance."""
    return _read_file_state
