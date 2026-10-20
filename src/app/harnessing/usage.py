"""Read actual persisted Runtime cumulative observations without counting them twice."""
from __future__ import annotations

import json
from pathlib import Path
from collections.abc import Iterable, Mapping

from .models import UsageObservation


def runtime_usage(events: Iterable[Mapping]) -> tuple[UsageObservation, ...]:
    """Keep one cumulative snapshot per actual Agent/runtime attempt.

    Counts that never arrived remain None. Failure after an earlier paid turn
    retains that known count and marks completeness unknown. Runtime events
    without an invocation identity remain conservative legacy observations.
    """
    groups = {}
    for index, event in enumerate(events):
        kind, details = event.get("kind"), event.get("details", {})
        if kind not in {"run", "usage", "terminal"} or not isinstance(details, Mapping):
            continue
        invocation = details.get("instance_id") or details.get("hook_run_id")
        identity_known = bool(invocation)
        invocation = invocation or "unidentified:" + str(event.get("run_id") or "runtime")
        attempt = details.get("attempt_id") or details.get("local_run_id") or details.get("hook_run_id") or "legacy"
        key = str(invocation), str(attempt)
        value = groups.setdefault(
            key,
            {
                "invocation_id": key[0],
                "attempt_id": key[1],
                "sequence": 0,
                "input_tokens": None,
                "output_tokens": None,
                "total_tokens": None,
                "complete": False,
            },
        )
        sequence = details.get("sequence", index)
        if type(sequence) is not int or sequence < value["sequence"]:
            continue
        value["sequence"] = sequence
        if kind == "usage":
            inconsistent = False
            for field in ("input_tokens", "output_tokens", "total_tokens"):
                reported = details.get(field)
                if type(reported) is not int or reported < 0:
                    continue
                previous = value[field]
                if previous is not None and reported < previous:
                    inconsistent = True
                value[field] = max(previous or 0, reported)
            value["complete"] = identity_known and bool(details.get("usage_complete")) and not inconsistent
        elif kind == "terminal" and details.get("state") not in {"success", "max_steps_error"}:
            value["complete"] = False
    return tuple(UsageObservation(**groups[key]) for key in sorted(groups))


def read_run_usage(run_dir: Path) -> tuple[UsageObservation, ...]:
    path = Path(run_dir) / "audit/runtime_events.jsonl"
    if not path.exists():
        return ()
    events = []
    for line in path.read_text().splitlines():
        try:
            event = json.loads(line)
        except ValueError:
            # A torn final line is not a zero-token report. Previously persisted
            # model observations remain readable and conservative.
            continue
        if isinstance(event, dict):
            events.append(event)
    return runtime_usage(events)
