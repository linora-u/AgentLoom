"""Simple in-memory database abstraction."""
from typing import Any, Optional


class Database:
    """Minimal in-memory key-value store used as a database stand-in."""

    def __init__(self, url: str = "sqlite:///sample.db"):
        self.url = url
        self._store: dict[str, dict[str, Any]] = {}
        self._connected = False

    def connect(self) -> None:
        self._connected = True

    def disconnect(self) -> None:
        self._connected = False

    def _ensure_connected(self) -> None:
        if not self._connected:
            raise RuntimeError("Database not connected")

    def save(self, table: str, key: str, data: dict) -> None:
        self._ensure_connected()
        self._store.setdefault(table, {})[key] = data

    def get(self, table: str, key: str) -> Optional[dict]:
        self._ensure_connected()
        return self._store.get(table, {}).get(key)

    def delete(self, table: str, key: str) -> bool:
        self._ensure_connected()
        tbl = self._store.get(table, {})
        if key in tbl:
            del tbl[key]
            return True
        return False

    def list_all(self, table: str) -> list[dict]:
        self._ensure_connected()
        return list(self._store.get(table, {}).values())
