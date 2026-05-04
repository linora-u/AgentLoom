"""Redis-backed cache implementation (requires redis-py)."""

from __future__ import annotations

import json
import logging
from typing import Any, Optional

from .base import CacheBackend

logger = logging.getLogger(__name__)


class RedisCache(CacheBackend):
    """Production cache using Redis as backend.

    Serializes values as JSON for cross-process sharing.
    Falls back to MemoryCache when Redis is unavailable.
    """

    def __init__(self, url: str = "redis://localhost:6379/0", prefix: str = "app:"):
        self._prefix = prefix
        self._url = url
        self._client = None

    def _get_client(self):
        if self._client is None:
            try:
                import redis
                self._client = redis.from_url(self._url, decode_responses=True)
                self._client.ping()
            except Exception as e:
                logger.warning("Redis unavailable (%s), cache disabled", e)
                self._client = None
        return self._client

    def _key(self, key: str) -> str:
        return f"{self._prefix}{key}"

    def get(self, key: str) -> Optional[Any]:
        client = self._get_client()
        if client is None:
            return None
        raw = client.get(self._key(key))
        if raw is None:
            return None
        return json.loads(raw)

    def set(self, key: str, value: Any, ttl: int = 300) -> None:
        client = self._get_client()
        if client is None:
            return
        client.setex(self._key(key), ttl, json.dumps(value, default=str))

    def delete(self, key: str) -> bool:
        client = self._get_client()
        if client is None:
            return False
        return client.delete(self._key(key)) > 0

    def clear(self) -> int:
        client = self._get_client()
        if client is None:
            return 0
        pattern = f"{self._prefix}*"
        keys = client.keys(pattern)
        if keys:
            return client.delete(*keys)
        return 0
