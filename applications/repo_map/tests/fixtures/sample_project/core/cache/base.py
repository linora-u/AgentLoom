"""Abstract cache backend — all implementations must inherit from this."""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any, Optional


class CacheBackend(ABC):
    """Base class for all cache backends."""

    @abstractmethod
    def get(self, key: str) -> Optional[Any]:
        """Retrieve a cached value, or None if expired / missing."""

    @abstractmethod
    def set(self, key: str, value: Any, ttl: int = 300) -> None:
        """Store a value with a time-to-live (seconds)."""

    @abstractmethod
    def delete(self, key: str) -> bool:
        """Remove a key. Returns True if it existed."""

    @abstractmethod
    def clear(self) -> int:
        """Flush the entire cache. Returns number of evicted entries."""

    def get_or_set(self, key: str, factory, ttl: int = 300) -> Any:
        """Read-through helper: return cached value or compute + store it."""
        value = self.get(key)
        if value is None:
            value = factory()
            self.set(key, value, ttl)
        return value
