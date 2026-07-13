#!/usr/bin/env python3
"""Run the same ledger benchmark against an explicitly selected Git tree."""

from __future__ import annotations

import argparse
import hashlib
import inspect
import json
import os
import platform
import random
import resource
import subprocess
import sys
import tempfile
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

SCHEMA_VERSION = 2
SPEC_VERSION = "memory-ledger-fixed-point-v3-paired-payload"
_PAYLOAD_TARGET_DECIMAL_BYTES = {
    "p50_approx": 160,
    "p95_approx": 2_000,
    "p99_approx": 32_000,
    "max": 60_000,
}


def _percentile(values: list[float], percentile: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = max(0, min(len(ordered) - 1, int(percentile * len(ordered) + 0.999999) - 1))
    return float(ordered[index])


def _payload_size(index: int, total: int) -> int:
    """Map one sorted rank to the literal 1,000-slot offline profile."""
    index = int(index)
    total = int(total)
    if total < 1 or index < 0 or index >= total:
        raise ValueError("payload position must satisfy 0 <= index < total")
    slot = 999 if total == 1 else (index * 999) // (total - 1)
    if slot <= 499:
        return 96 + (slot * 63) // 499
    if slot <= 949:
        return 160 + ((slot - 500) * 1_887) // 449
    if slot <= 989:
        return 2_048 + ((slot - 950) * 29_951) // 39
    if slot <= 998:
        return 32_000 + ((slot - 990) * 27_999) // 8
    return 60_000


def _payload_size_plan(total: int, seed: int) -> list[int]:
    """Assign the complete rank profile to event ids without a size/time trend."""
    sizes = [_payload_size(index, total) for index in range(total)]
    random.Random(seed).shuffle(sizes)
    return sizes


def _payload(index: int, target_bytes: int) -> str:
    marker = f"fpmarker{index:09d} "
    if target_bytes < len(marker):
        raise ValueError("payload target is smaller than its unique marker")
    return marker + ("x" * (target_bytes - len(marker)))


def _payload_profile(sizes: list[int]) -> dict[str, Any]:
    return {
        "algorithm": "seeded_quantile_slots_v1",
        "encoding": "utf-8",
        "target_decimal_bytes": dict(_PAYLOAD_TARGET_DECIMAL_BYTES),
        "actual_nearest_rank_bytes": {
            "p50": int(_percentile([float(value) for value in sizes], 0.50)),
            "p95": int(_percentile([float(value) for value in sizes], 0.95)),
            "p99": int(_percentile([float(value) for value in sizes], 0.99)),
            "max": max(sizes),
        },
    }


def _query_order(events: int, seed: int) -> list[int]:
    """Return the pair-replayable order for at most 1,000 unique FTS probes."""
    indices = list(range(events))
    random.Random(seed).shuffle(indices)
    return indices[: min(1_000, events)]


def _git(repo: Path, *args: str) -> str:
    return subprocess.check_output(
        ["git", "-C", str(repo), *args],
        text=True,
        stderr=subprocess.DEVNULL,
    ).strip()


def _environment_fingerprint() -> str:
    value = {
        "python": sys.version,
        "platform": platform.platform(),
        "machine": platform.machine(),
        "processor": platform.processor(),
        "cpu_count": os.cpu_count(),
    }
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


def _loaded_self_learning_hashes(target_repo: Path) -> dict[str, str]:
    """Hash the exact source files imported by this benchmark process."""
    hashes: dict[str, str] = {}
    for name, module in sorted(sys.modules.items()):
        if not name.startswith("src.extensions.self_learning"):
            continue
        raw_path = getattr(module, "__file__", None)
        if not raw_path:
            continue
        path = Path(raw_path).resolve()
        if path.suffix == ".pyc" and path.with_suffix("").exists():
            path = path.with_suffix("")
        try:
            relative = path.relative_to(target_repo)
        except ValueError:
            continue
        if path.is_file():
            hashes[str(relative)] = hashlib.sha256(path.read_bytes()).hexdigest()
    return hashes


def _max_rss_mb() -> float:
    return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / (
        1024 * 1024 if sys.platform == "darwin" else 1024
    )


def _load_sample(*, phase: str, started: float, index: int | None = None) -> dict[str, Any]:
    """Capture low-overhead host/process load beside the raw latency samples."""
    try:
        load_1m, load_5m, load_15m = os.getloadavg()
    except (AttributeError, OSError):
        load_1m = load_5m = load_15m = 0.0
    usage = resource.getrusage(resource.RUSAGE_SELF)
    sample: dict[str, Any] = {
        "phase": phase,
        "elapsed_seconds": round(time.perf_counter() - started, 6),
        "load_1m": round(float(load_1m), 6),
        "load_5m": round(float(load_5m), 6),
        "load_15m": round(float(load_15m), 6),
        "user_cpu_seconds": round(float(usage.ru_utime), 6),
        "system_cpu_seconds": round(float(usage.ru_stime), 6),
        "max_rss_mb": round(_max_rss_mb(), 3),
    }
    if index is not None:
        sample["index"] = int(index)
    return sample


def run_benchmark(
    *,
    target_repo: Path,
    db_path: Path,
    events: int,
    seed: int,
    events_per_run: int,
    require_clean: bool,
    warmup_events: int = 100,
) -> dict[str, Any]:
    target_repo = target_repo.resolve()
    clean = not _git(target_repo, "status", "--porcelain=v1", "--untracked-files=all")
    if require_clean and not clean:
        raise RuntimeError("fixed-point target worktree is not clean")
    sys.path.insert(0, str(target_repo))
    from src.extensions.self_learning.event_schema import CanonicalSessionEvent
    from src.extensions.self_learning.ledger import SelfLearningLedger

    event_fields = inspect.signature(CanonicalSessionEvent).parameters
    if events < 1 or warmup_events < 0:
        raise ValueError("events must be positive and warmup_events must be non-negative")
    db_path.parent.mkdir(parents=True, exist_ok=True)
    load_started = time.perf_counter()
    load_samples = [_load_sample(phase="start", started=load_started)]
    started = datetime(2026, 7, 11, tzinfo=UTC)
    measured_payload_sizes = _payload_size_plan(events, seed)
    warmup_payload_sizes = (
        _payload_size_plan(warmup_events, seed) if warmup_events else []
    )

    # Warm caches and the production append path without contaminating either
    # the measured DB byte denominator or its FTS/index state.
    with tempfile.TemporaryDirectory(
        prefix=".fixed-point-warmup-",
        dir=db_path.parent,
    ) as warmup_dir:
        warmup_ledger = SelfLearningLedger(Path(warmup_dir) / "warmup.db")
        for index in range(warmup_events):
            run_id = f"fixed-point-warmup-run-{index // max(1, events_per_run):07d}"
            values: dict[str, Any] = {
                "event_id": f"fixed-point-warmup-event-{index:09d}",
                "run_id": run_id,
                "task_id": f"fixed-point-warmup-task-{index:09d}",
                "application_id": "memory_feature_validation",
                "event_type": "tool_result",
                "source": "fixed_point_benchmark_warmup",
                "content_text": _payload(index, warmup_payload_sizes[index]),
                "created_at": (
                    started - timedelta(seconds=1) + timedelta(milliseconds=index)
                ).isoformat(),
            }
            if "root_run_id" in event_fields:
                values["root_run_id"] = run_id
            warmup_ledger.append_event(CanonicalSessionEvent(**values))
    load_samples.append(_load_sample(phase="warmup_complete", started=load_started))

    ledger = SelfLearningLedger(db_path)
    append_samples: list[dict[str, Any]] = []
    for index in range(events):
        run_id = f"fixed-point-run-{index // max(1, events_per_run):07d}"
        values: dict[str, Any] = {
            "event_id": f"fixed-point-event-{index:09d}",
            "run_id": run_id,
            "task_id": f"fixed-point-task-{index:09d}",
            "application_id": "memory_feature_validation",
            "event_type": "tool_result",
            "source": "fixed_point_benchmark",
            "content_text": _payload(index, measured_payload_sizes[index]),
            "created_at": (started + timedelta(milliseconds=index)).isoformat(),
        }
        if "root_run_id" in event_fields:
            values["root_run_id"] = run_id
        event = CanonicalSessionEvent(**values)
        before = time.perf_counter()
        ledger.append_event(event)
        append_samples.append(
            {
                "index": index,
                "elapsed_ms": round((time.perf_counter() - before) * 1000.0, 6),
            }
        )
        if (index + 1) % 100 == 0 and index + 1 < events:
            load_samples.append(
                _load_sample(phase="append_progress", started=load_started, index=index + 1)
            )
    load_samples.append(_load_sample(phase="append_complete", started=load_started, index=events))

    query_samples: list[dict[str, Any]] = []
    query_order = _query_order(events, seed)
    query_count = len(query_order)
    for index in query_order:
        before = time.perf_counter()
        rows = ledger.search_events(f"fpmarker{index:09d}", scope="all")
        query_samples.append(
            {
                "index": index,
                "elapsed_ms": round((time.perf_counter() - before) * 1000.0, 6),
            }
        )
        if not any(str(row.get("event_id")) == f"fixed-point-event-{index:09d}" for row in rows):
            raise RuntimeError(f"fixed-point FTS marker missing: {index}")
        if len(query_samples) % 50 == 0 and len(query_samples) < query_count:
            load_samples.append(
                _load_sample(
                    phase="query_progress",
                    started=load_started,
                    index=len(query_samples),
                )
            )
    load_samples.append(
        _load_sample(
            phase="query_complete",
            started=load_started,
            index=len(query_samples),
        )
    )
    with ledger._connect() as conn:
        integrity = str(conn.execute("PRAGMA integrity_check").fetchone()[0]).lower() == "ok"
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    counts = ledger.count_events()
    database_events = events
    if int(counts["events_indexed"]) != database_events:
        raise RuntimeError("fixed-point event count mismatch")

    spec = {
        "version": SPEC_VERSION,
        "events": events,
        "warmup_events": warmup_events,
        "seed": seed,
        "events_per_run": events_per_run,
        "query_count": query_count,
        "query_order_sha256": hashlib.sha256(
            json.dumps(query_order, separators=(",", ":")).encode()
        ).hexdigest(),
        "payload_profile": _payload_profile(measured_payload_sizes),
        "payload_order_sha256": hashlib.sha256(
            json.dumps(measured_payload_sizes, separators=(",", ":")).encode()
        ).hexdigest(),
    }
    spec_sha = hashlib.sha256(json.dumps(spec, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    loaded_hashes = _loaded_self_learning_hashes(target_repo)
    if not loaded_hashes:
        raise RuntimeError("fixed-point benchmark did not bind any loaded self-learning sources")
    return {
        "schema_version": SCHEMA_VERSION,
        "target": {
            "commit": _git(target_repo, "rev-parse", "HEAD"),
            "tree": _git(target_repo, "rev-parse", "HEAD^{tree}"),
            "clean": clean,
            "loaded_module_hashes": loaded_hashes,
        },
        "protocol": {
            "driver_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
            "spec": spec,
            "spec_sha256": spec_sha,
            "environment_fingerprint": _environment_fingerprint(),
        },
        "counts": {
            "warmup_events": warmup_events,
            "measured_events": events,
            "database_events": database_events,
        },
        "samples": {
            "ledger_append": append_samples,
            "fts_query": query_samples,
            "system_load": load_samples,
        },
        "integrity": integrity,
        "max_rss_mb": round(_max_rss_mb(), 3),
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--target-repo", required=True, type=Path)
    parser.add_argument("--db", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--events", required=True, type=int)
    parser.add_argument("--seed", required=True, type=int)
    parser.add_argument("--events-per-run", required=True, type=int)
    parser.add_argument("--warmup-events", default=100, type=int)
    parser.add_argument("--require-clean", action="store_true")
    return parser


def main() -> int:
    args = _parser().parse_args()
    result = run_benchmark(
        target_repo=args.target_repo,
        db_path=args.db,
        events=args.events,
        seed=args.seed,
        events_per_run=args.events_per_run,
        require_clean=args.require_clean,
        warmup_events=args.warmup_events,
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
