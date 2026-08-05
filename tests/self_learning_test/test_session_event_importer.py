from __future__ import annotations

import json
from pathlib import Path

from src.extensions.self_learning.event_schema import CanonicalSessionEvent, now_iso
from src.extensions.self_learning.persistence.event_importer import (
    SessionEventImporter,
)
from src.extensions.self_learning.persistence.ledger import SelfLearningLedger


def test_event_importer_replaces_one_run_from_canonical_jsonl(tmp_path: Path) -> None:
    ledger = SelfLearningLedger(tmp_path / "self_learning.db")
    ledger.append_event(
        CanonicalSessionEvent(
            event_id="event-stale",
            run_id="run-imported",
            event_type="tool_result",
            content_text="stale",
            created_at=now_iso(),
        )
    )
    events = [
        CanonicalSessionEvent(
            event_id=f"event-{index}",
            run_id="run-imported",
            event_type="tool_result",
            content_text=f"imported {index}",
            created_at=now_iso(),
        )
        for index in range(2)
    ]
    archive = tmp_path / "run-imported.jsonl"
    archive.write_text(
        "\n".join(json.dumps(event.to_record()) for event in events) + "\n",
        encoding="utf-8",
    )

    result = SessionEventImporter(ledger).index_run(archive)

    assert result["run_id"] == "run-imported"
    assert result["events_indexed"] == 2
    assert ledger.count_events()["events_indexed"] == 2
    assert ledger.search_events("stale") == []
    assert len(ledger.search_events("imported")) == 2
