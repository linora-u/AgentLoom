"""Local persistent store for original context payloads."""

from __future__ import annotations

import hashlib
import json
import re
import threading
import time
from pathlib import Path
from typing import Iterable

from src.lib.logging import get_logger
from src.lib.runtime import SecureDirectory

from .models import ContextEntry

_logger = get_logger(__name__)
_REF_RE = re.compile(r"^ctx_[0-9a-f]{16}$")
_SECRET_RE = re.compile(r"(?i)(api[_-]?key|token|secret|password)=([^\s]+)")


class ContextStore:
    def __init__(
        self,
        root_dir: Path,
        *,
        max_entries: int = 1000,
        ttl_seconds: int | None = None,
        storage: SecureDirectory | None = None,
    ) -> None:
        self.root_dir = Path(root_dir)
        self.entries_dir = self.root_dir / "entries"
        self.events_path = self.root_dir / "events.jsonl"
        self.max_entries = max_entries
        self.ttl_seconds = ttl_seconds
        self._lock = threading.RLock()
        self._storage = storage or SecureDirectory(self.root_dir, create=True)
        self._storage.ensure_dir("entries")

    def put(self, entry: ContextEntry) -> ContextEntry:
        if not _REF_RE.match(entry.ref):
            raise ValueError(f"Invalid context ref: {entry.ref}")
        with self._lock:
            self._evict_if_needed()
            self._storage.atomic_write_json(
                f"entries/{entry.ref}.json",
                entry.to_json(),
            )
            self._append_event(
                {
                    "type": "compressed",
                    "ref": entry.ref,
                    "kind": entry.kind.value,
                    "tool_name": entry.tool_name,
                    "original_chars": entry.original_chars,
                    "preview_chars": entry.preview_chars,
                    "chars_saved": max(0, entry.original_chars - entry.preview_chars),
                    "created_at": entry.created_at,
                }
            )
        return entry

    def get(self, ref: str) -> ContextEntry | None:
        if not _REF_RE.match(ref):
            return None
        try:
            data = self._storage.read_json(f"entries/{ref}.json")
            entry = ContextEntry.from_json(data)
        except Exception as exc:
            _logger.warning("Failed to read context entry %s: %s", ref, exc)
            return None
        if self._is_expired(entry):
            return None
        return entry

    def retrieve(
        self,
        ref: str,
        *,
        query: str = "",
        offset: int = 0,
        limit: int = 200,
    ) -> str | None:
        entry = self.get(ref)
        if entry is None:
            return None
        content = entry.original
        if query:
            matches = list(self._search_lines(content, query))
            safe_offset = max(0, int(offset or 0))
            safe_limit = max(0, int(limit or 0))
            selected = matches[safe_offset:] if safe_limit == 0 else matches[safe_offset:safe_offset + safe_limit]
            content = "\n".join(selected)
        else:
            content = self._slice_lines(content, offset=offset, limit=limit)
        self._append_event(
            {
                "type": "retrieved",
                "ref": ref,
                "query": _redact(query),
                "offset": offset,
                "limit": limit,
                "retrieved_chars": len(content),
                "created_at": time.time(),
            }
        )
        return content

    def stats_snapshot(self) -> dict:
        refs = self.refs()
        total_original = 0
        total_preview = 0
        by_kind: dict[str, int] = {}
        for ref in refs:
            entry = self.get(ref)
            if entry is None:
                continue
            total_original += entry.original_chars
            total_preview += entry.preview_chars
            by_kind[entry.kind.value] = by_kind.get(entry.kind.value, 0) + 1
        return {
            "version": 1,
            "store_path": str(self.root_dir),
            "ref_count": len(refs),
            "total_original_chars": total_original,
            "total_preview_chars": total_preview,
            "total_chars_saved": max(0, total_original - total_preview),
            "by_kind": by_kind,
        }

    def refs(self) -> list[str]:
        try:
            names = self._storage.regular_file_names("entries")
        except (FileNotFoundError, OSError, RuntimeError):
            return []
        return sorted(
            Path(name).stem
            for name in names
            if name.startswith("ctx_") and name.endswith(".json")
        )

    def _entry_path(self, ref: str) -> Path:
        return self.entries_dir / f"{ref}.json"

    def _is_expired(self, entry: ContextEntry) -> bool:
        ttl = entry.ttl_seconds if entry.ttl_seconds is not None else self.ttl_seconds
        return bool(ttl and ttl > 0 and time.time() - entry.created_at > ttl)

    def _slice_lines(self, content: str, *, offset: int, limit: int) -> str:
        lines = content.splitlines()
        safe_offset = max(0, int(offset or 0))
        safe_limit = max(0, int(limit or 0))
        if safe_limit == 0:
            selected = lines[safe_offset:]
        else:
            selected = lines[safe_offset:safe_offset + safe_limit]
        return "\n".join(selected)

    def _search_lines(self, content: str, query: str) -> Iterable[str]:
        terms = [term.lower() for term in query.split() if term.strip()]
        if not terms:
            return content.splitlines()
        matches = []
        for line_no, line in enumerate(content.splitlines(), start=1):
            lowered = line.lower()
            if all(term in lowered for term in terms):
                matches.append(f"{line_no}: {line}")
        return matches

    def _evict_if_needed(self) -> None:
        if self.max_entries <= 0:
            return
        entries = [
            name
            for name in self._storage.regular_file_names("entries")
            if name.startswith("ctx_") and name.endswith(".json")
        ]
        entries.sort(
            key=lambda name: self._storage.stat_file(f"entries/{name}").st_mtime
        )
        overflow = len(entries) - max(0, self.max_entries - 1)
        for name in entries[:max(0, overflow)]:
            try:
                self._storage.unlink(f"entries/{name}")
            except OSError:
                pass

    def _append_event(self, event: dict) -> None:
        try:
            self._storage.append_text(
                "events.jsonl",
                json.dumps(event, ensure_ascii=False, default=str) + "\n",
            )
        except Exception as exc:
            _logger.debug("Failed to write context store event: %s", exc)

    def close(self) -> None:
        self._storage.close()

    def __del__(self) -> None:
        try:
            self.close()
        except Exception:
            pass


def make_context_ref(original: str, *, tool_name: str, source: str = "") -> str:
    digest = hashlib.sha256()
    digest.update((tool_name or "default").encode("utf-8"))
    digest.update(b"\0")
    digest.update((source or "").encode("utf-8"))
    digest.update(b"\0")
    digest.update(original.encode("utf-8", errors="replace"))
    return f"ctx_{digest.hexdigest()[:16]}"


def _redact(text: str) -> str:
    return _SECRET_RE.sub(r"\1=[REDACTED]", text or "")
