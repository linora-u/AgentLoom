"""In-memory LRU cache with TTL expiration."""

from __future__ import annotations

import threading
import time
from collections import OrderedDict
from typing import Any, Optional

from .base import CacheBackend


class MemoryCache(CacheBackend):
    """Thread-safe in-memory LRU cache.

    Uses :class:`OrderedDict` for O(1) LRU eviction and per-entry
    monotonic timestamps for TTL enforcement.
    """

    def __init__(self, max_size: int = 1024):
        self._store: OrderedDict[str, tuple[Any, float]] = OrderedDict()
        self._max_size = max_size
        self._lock = threading.Lock()
        self._hits = 0
        self._misses = 0

    # ── public API ───────────────────────────────────────────────

    def get(self, key: str) -> Optional[Any]:
        with self._lock:
            entry = self._store.get(key)
            if entry is None:
                self._misses += 1
                return None
            value, expires_at = entry
            if time.monotonic() > expires_at:
                del self._store[key]
                self._misses += 1
                return None
            self._store.move_to_end(key)
            self._hits += 1
            return value

    def set(self, key: str, value: Any, ttl: int = 300) -> None:
        with self._lock:
            expires_at = time.monotonic() + ttl
            if key in self._store:
                self._store.move_to_end(key)
            self._store[key] = (value, expires_at)
            while len(self._store) > self._max_size:
                self._store.popitem(last=False)

    def delete(self, key: str) -> bool:
        with self._lock:
            return self._store.pop(key, None) is not None

    def clear(self) -> int:
        with self._lock:
            n = len(self._store)
            self._store.clear()
            return n

    # ── diagnostics ──────────────────────────────────────────────

    @property
    def stats(self) -> dict:
        """Return hit/miss/size statistics."""
        return {
            "hits": self._hits,
            "misses": self._misses,
            "size": len(self._store),
            "max_size": self._max_size,
            "hit_rate": self._hits / max(self._hits + self._misses, 1),
        }
