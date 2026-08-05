"""Import canonical session-event archives into the self-learning ledger."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from src.lib.logging import get_logger

from ..event_schema import CanonicalSessionEvent, safe_run_id
from ..paths import session_events_dir
from .ledger import SelfLearningLedger

logger = get_logger(__name__)


class SessionEventImporter:
    """Own JSONL discovery, parsing, and replacement import semantics."""

    def __init__(self, ledger: SelfLearningLedger | None = None) -> None:
        self.ledger = ledger or SelfLearningLedger()

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
                logger.warning(
                    "Skipping malformed self-learning event export line in %s",
                    path,
                )
                continue
            if isinstance(data, dict):
                events.append(CanonicalSessionEvent.from_record(data))
        return [event for event in events if event.run_id]

    @staticmethod
    def _event_file_for_run(run_id: str) -> Path:
        return session_events_dir() / f"{safe_run_id(run_id)}.jsonl"

    @classmethod
    def _event_files(cls, target: str | Path | None = None) -> list[Path]:
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
        run_file = cls._event_file_for_run(str(target))
        return [run_file] if run_file.exists() else []

    def index_run(self, target: str | Path) -> dict[str, Any]:
        """Replace one run with the contents of one canonical JSONL export."""
        files = self._event_files(target)
        if len(files) != 1:
            raise FileNotFoundError(
                "Expected one canonical event export file, "
                f"found {len(files)} for {target}"
            )
        event_file = files[0]
        events = self._read_event_file(event_file)
        if not events:
            return {
                "run_id": "",
                "events_indexed": 0,
                "db_path": str(self.ledger.db_path),
                "event_file": str(event_file),
            }
        run_id = events[0].run_id
        self.ledger.delete_run(run_id)
        inserted = sum(
            1 for event in events if self.ledger.append_event(event).get("indexed")
        )
        return {
            "run_id": run_id,
            "events_indexed": inserted,
            "db_path": str(self.ledger.db_path),
            "event_file": str(event_file),
        }

    def index_all(
        self,
        events_root: str | Path | None = None,
    ) -> dict[str, Any]:
        """Import all JSONL exports, or report persisted event counts."""
        if events_root is None:
            return self.ledger.count_events()
        runs = 0
        events = 0
        for path in self._event_files(events_root):
            stats = self.index_run(path)
            if stats.get("run_id"):
                runs += 1
            events += int(stats.get("events_indexed") or 0)
        return {
            "runs_indexed": runs,
            "events_indexed": events,
            "db_path": str(self.ledger.db_path),
        }


__all__ = ["SessionEventImporter"]
