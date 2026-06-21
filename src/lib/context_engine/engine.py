"""ContextEngine orchestration layer."""

from __future__ import annotations

import time
from pathlib import Path

from .compressors import compress_content
from .config import ContextEngineConfig
from .models import CompressionResult, ContextEntry
from .router import route_content
from .store import ContextStore, make_context_ref


CONTEXT_REF_PREFIX = "[ContextRef "


class ContextEngine:
    def __init__(
        self,
        store_dir: Path,
        *,
        config: ContextEngineConfig | None = None,
    ) -> None:
        self.config = config or ContextEngineConfig.from_runtime()
        self.store = ContextStore(
            store_dir,
            max_entries=self.config.store.max_entries,
            ttl_seconds=self.config.store.ttl_seconds,
        )

    def should_compress_tool_result(self, text: str, tool_name: str = "default") -> bool:
        if not text or len(text) < self.config.min_chars:
            return False
        if CONTEXT_REF_PREFIX in text:
            return False
        if (tool_name or "").lower() in self.config.safety.skip_tools:
            return False
        return True

    def compress_tool_result(
        self,
        text: str,
        *,
        tool_name: str = "default",
        source: str = "",
    ) -> str | None:
        if not self.should_compress_tool_result(text, tool_name):
            return None

        kind = route_content(text, tool_name=tool_name)
        compressed = compress_content(text, kind, self.config.preview_max_chars)
        ref = make_context_ref(text, tool_name=tool_name, source=source)
        entry = ContextEntry(
            ref=ref,
            kind=kind,
            tool_name=tool_name or "default",
            original=text,
            preview=compressed.preview,
            original_chars=compressed.original_chars,
            preview_chars=compressed.preview_chars,
            original_tokens_est=compressed.original_tokens_est,
            preview_tokens_est=compressed.preview_tokens_est,
            strategy=compressed.strategy,
            created_at=time.time(),
            ttl_seconds=self.config.store.ttl_seconds,
            source=source,
        )
        formatted_preview = self.format_preview(entry, compressed)
        if len(formatted_preview) >= len(text):
            return None
        self.store.put(entry)
        return formatted_preview

    def retrieve(
        self,
        ref: str,
        *,
        query: str = "",
        offset: int = 0,
        limit: int = 200,
    ) -> str | None:
        return self.store.retrieve(ref, query=query, offset=offset, limit=limit)

    def get_entry(self, ref: str) -> ContextEntry | None:
        return self.store.get(ref)

    def stats_snapshot(self) -> dict:
        return self.store.stats_snapshot()

    def format_preview(self, entry: ContextEntry, compressed: CompressionResult | None = None) -> str:
        preview = compressed.preview if compressed else entry.preview
        marker = (
            f"[ContextRef {entry.ref} kind={entry.kind.value} source={entry.tool_name} "
            f"original_chars={entry.original_chars} preview_chars={entry.preview_chars}]\n"
            f'Use loom_retrieve_context(ref="{entry.ref}", query="", offset=0, limit=200) '
            "to retrieve original content.\n\n"
        )
        return marker + preview
