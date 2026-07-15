"""Search facade over the DB-first self-learning ledger."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from src.lib.logging import get_logger

from .event_schema import CanonicalSessionEvent
from .ledger import SelfLearningLedger
from .paths import session_events_dir, session_index_db

logger = get_logger(__name__)


class SessionIndex:
    """Durable SQLite/FTS5 search API for canonical self-learning events."""

    def __init__(self, db_path: str | Path | None = None):
        self.db_path = Path(db_path).resolve() if db_path else session_index_db()
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._ledger = SelfLearningLedger(self.db_path)

    @staticmethod
    def _read_event_file(path: Path) -> list[CanonicalSessionEvent]:
        events: list[CanonicalSessionEvent] = []
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except OSError:
            return events
        for line in lines:
            raw = line.strip()
            if not raw:
                continue
            try:
                data = json.loads(raw)
            except json.JSONDecodeError:
                logger.warning("Skipping malformed self-learning event export line in %s", path)
                continue
            if isinstance(data, dict):
                events.append(CanonicalSessionEvent.from_record(data))
        return [event for event in events if event.run_id]

    @staticmethod
    def _event_file_for_run(run_id: str) -> Path:
        safe_name = "".join(ch if ch.isalnum() or ch in "._-" else "_" for ch in str(run_id))
        return session_events_dir() / f"{safe_name}.jsonl"

    @staticmethod
    def _event_files(target: str | Path | None = None) -> list[Path]:
        if target is None:
            root = session_events_dir()
            return sorted(root.glob("*.jsonl")) if root.exists() else []
        path = Path(target).expanduser()
        if not path.is_absolute():
            path = Path.cwd() / path
        path = path.resolve()
        if path.is_file():
            return [path]
        if path.is_dir():
            return sorted(path.glob("*.jsonl"))
        run_file = SessionIndex._event_file_for_run(str(target))
        return [run_file] if run_file.exists() else []

    def record_event(self, event: CanonicalSessionEvent) -> dict[str, Any]:
        """Append one already-redacted canonical event to the ledger."""
        return self._ledger.append_event(event)

    def record_runtime_event(
        self,
        event: CanonicalSessionEvent,
        *,
        trusted_evidence: tuple[dict[str, str], ...] = (),
    ) -> dict[str, Any]:
        """Atomically persist one live event and its code-owned provenance."""

        return self._ledger.append_runtime_event(
            event,
            trusted_evidence=trusted_evidence,
        )

    def index_run(self, target: str | Path) -> dict[str, Any]:
        """Import one optional JSONL event export into the DB ledger."""
        files = self._event_files(target)
        if len(files) != 1:
            raise FileNotFoundError(f"Expected one canonical event export file, found {len(files)} for {target}")
        event_file = files[0]
        events = self._read_event_file(event_file)
        if not events:
            return {"run_id": "", "events_indexed": 0, "db_path": str(self.db_path), "event_file": str(event_file)}
        run_id = events[0].run_id
        self._ledger.delete_run(run_id)
        inserted = sum(1 for event in events if self.record_event(event).get("indexed"))
        return {"run_id": run_id, "events_indexed": inserted, "db_path": str(self.db_path), "event_file": str(event_file)}

    def index_all(self, events_root: str | Path | None = None) -> dict[str, Any]:
        """Import optional JSONL exports, or report current ledger counts."""
        if events_root is None:
            return self._ledger.count_events()
        runs = 0
        events = 0
        for path in self._event_files(events_root):
            stats = self.index_run(path)
            if stats.get("run_id"):
                runs += 1
            events += int(stats.get("events_indexed") or 0)
        return {"runs_indexed": runs, "events_indexed": events, "db_path": str(self.db_path)}

    def search(
        self,
        query: str,
        limit: int = 10,
        agent: str | None = None,
        app: str | None = None,
        since: str | None = None,
        exclude_run_id: str | None = None,
        scope: str = "all",
    ) -> list[dict[str, Any]]:
        """Search redacted real event records."""
        return self._ledger.search_events(
            query,
            limit=limit,
            agent=agent,
            app=app,
            since=since,
            exclude_run_id=exclude_run_id,
            scope=scope,
        )

    def scroll(
        self,
        run_id: str,
        event_id: int,
        direction: str = "after",
        window: int = 5,
    ) -> list[dict[str, Any]]:
        """Return neighboring canonical events around an event row id."""
        return self._ledger.scroll_events(run_id, event_id, direction=direction, window=window)

    def root_run_id_for(self, run_id: str) -> str:
        return self._ledger.root_run_id_for(run_id)
