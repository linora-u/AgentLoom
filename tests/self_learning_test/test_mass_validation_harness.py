"""Behavioral contract for the offline self-learning mass-validation campaign."""

from __future__ import annotations

import gzip
import hashlib
import importlib.util
import json
import math
import os
import random
import re
import sqlite3
import sys
from pathlib import Path

import pytest

SCRIPT_PATH = (
    Path(__file__).resolve().parents[2]
    / "applications"
    / "memory_feature_validation"
    / "scripts"
    / "run_offline_mass_validation.py"
)
FIXED_POINT_SCRIPT_PATH = SCRIPT_PATH.with_name("run_fixed_point_benchmark.py")


def _load_harness():
    spec = importlib.util.spec_from_file_location("memory_mass_validation", SCRIPT_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _load_fixed_point_driver():
    spec = importlib.util.spec_from_file_location(
        "memory_fixed_point_benchmark",
        FIXED_POINT_SCRIPT_PATH,
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_allocate_quotas_uses_the_seven_literal_campaign_buckets():
    allocate_quotas = _load_harness().allocate_quotas

    assert allocate_quotas(100) == {
        "ledger_fts_search_scroll": 20,
        "redaction_digest_injection": 20,
        "exact_evidence_conflict": 15,
        "revision_trust_feedback": 15,
        "root_run_worker_concurrency": 10,
        "outbox_crash_lease_migration": 10,
        "ranking_snapshot_retention": 10,
    }
    assert allocate_quotas(100000) == {
        "ledger_fts_search_scroll": 20000,
        "redaction_digest_injection": 20000,
        "exact_evidence_conflict": 15000,
        "revision_trust_feedback": 15000,
        "root_run_worker_concurrency": 10000,
        "outbox_crash_lease_migration": 10000,
        "ranking_snapshot_retention": 10000,
    }
    assert sum(allocate_quotas(17).values()) == 17
    assert all(value >= 0 for value in allocate_quotas(3).values())


def test_payload_sizes_have_literal_percentile_boundaries():
    payload_size_for_position = _load_harness().payload_size_for_position

    assert payload_size_for_position(0, 1000) == 96
    assert payload_size_for_position(499, 1000) == 159
    assert payload_size_for_position(500, 1000) == 160
    assert payload_size_for_position(949, 1000) == 2047
    assert payload_size_for_position(950, 1000) == 2048
    assert payload_size_for_position(989, 1000) == 31999
    assert payload_size_for_position(990, 1000) == 32000
    assert payload_size_for_position(998, 1000) == 59999
    assert payload_size_for_position(999, 1000) == 60000


def test_cli_defaults_are_the_release_campaign_defaults():
    harness = _load_harness()
    args = harness._parser().parse_args([])

    assert args.cases == 100000
    assert args.seed == 20260711
    assert args.workers == 4


def test_offline_root_oracle_requires_distinct_worker_local_context_and_cleanup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    harness = _load_harness()
    import src.extensions.self_learning.finalizer as finalizer

    monkeypatch.setenv("AGENTLOOM_SELF_LEARNING_ROOT", str(tmp_path))
    monkeypatch.setattr(finalizer, "kick_learning_worker", lambda *_args, **_kwargs: False)
    ledger = harness.SelfLearningLedger(tmp_path / "self_learning.db")
    sink = harness.SecurityArtifactSink(tmp_path / "security.jsonl")
    executor = harness.ThreadPoolExecutor(max_workers=2)
    runtime = harness.CampaignRuntime(
        ledger=ledger,
        memory_store=harness.MemoryStore(tmp_path / "self_learning.db"),
        queue=harness.LearningJobQueue(tmp_path / "self_learning.db"),
        nested_executor=executor,
        security_sink=sink,
        worker_artifacts=tmp_path / "worker-artifacts",
        events_per_run=2,
        source_stats={},
        kick_call_timings_ms=[],
        detached_launch_timings_ms=[],
        detached_launch_lock=harness.threading.Lock(),
        event_append_trace={},
        event_append_trace_lock=harness.threading.Lock(),
    )
    spec = next(
        item
        for item in harness.build_case_plan(100, 20260711)
        if item.category == "root_run_worker_concurrency"
    )
    try:
        observed = harness._root_observation(spec, runtime)
    finally:
        executor.shutdown(wait=True)
        sink.close()

    assert observed["root_ok"] is True
    assert observed["worker_local_run_id"] != observed["owner_local_run_id"]
    assert observed["hook_local_run_id"] == observed["worker_local_run_id"]
    assert observed["hook_root_run_id"] == f"root-{spec.case_id}"
    assert observed["root_isolated_after"] is True
    assert observed["local_isolated_after"] is True
    assert observed["unbound_missing"] is True


def test_session_end_guard_keeps_reviewer_enabled_and_uses_isolated_detached_proof(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    harness = _load_harness()
    import src.extensions.self_learning.finalizer as finalizer

    monkeypatch.setenv("AGENTLOOM_SELF_LEARNING_ROOT", str(tmp_path))
    reviewer_enabled = lambda _name, _default=True: True
    detached_db_paths: list[Path] = []

    def fake_detached_kick(db_path=None):
        assert db_path is not None
        detached_db_paths.append(Path(db_path))
        proof_queue = harness.LearningJobQueue(db_path)
        worker = harness.LearningJobWorker(
            proof_queue,
            handlers={"retention": lambda _job: {"proof": "succeeded"}},
            owner="massval-detached-proof-test",
        )
        summary = worker.run_until_idle(max_jobs=1, wait_for_retries=False)
        return summary["succeeded"] == 1

    monkeypatch.setattr(finalizer, "config_bool", reviewer_enabled)
    monkeypatch.setattr(finalizer, "kick_learning_worker", fake_detached_kick)
    central_db = tmp_path / "self_learning.db"
    ledger = harness.SelfLearningLedger(central_db)
    sink = harness.SecurityArtifactSink(tmp_path / "security.jsonl")
    executor = harness.ThreadPoolExecutor(max_workers=2)
    runtime = harness.CampaignRuntime(
        ledger=ledger,
        memory_store=harness.MemoryStore(central_db),
        queue=harness.LearningJobQueue(central_db),
        nested_executor=executor,
        security_sink=sink,
        worker_artifacts=tmp_path / "worker-artifacts",
        events_per_run=2,
        source_stats={},
        kick_call_timings_ms=[],
        detached_launch_timings_ms=[],
        detached_launch_lock=harness.threading.Lock(),
        event_append_trace={},
        event_append_trace_lock=harness.threading.Lock(),
    )
    spec = next(
        item
        for item in harness.build_case_plan(100, 20260711)
        if item.category == "root_run_worker_concurrency"
    )
    guard = harness._SessionEndGuard(runtime)
    guard.install()
    try:
        assert finalizer.config_bool is reviewer_enabled
        observed = harness._root_observation(spec, runtime)
        central_jobs = runtime.queue.list_jobs(limit=10)
    finally:
        guard.restore()

    try:
        detached = guard.run_detached_retention_proof()
    finally:
        executor.shutdown(wait=True)
        sink.close()

    assert observed["root_ok"] is True
    assert [job["kind"] for job in central_jobs] == ["session_review", "retention"]
    assert all(job["status"] == "pending" for job in central_jobs)
    assert len(runtime.kick_call_timings_ms) == 2
    assert guard.model_calls == 0
    assert detached["detached_central_job_terminal"] is True
    assert detached["detached_proof_db_isolated"] is True
    assert detached["detached_proof_kick_used_explicit_db_path"] is True
    assert detached_db_paths == [tmp_path / "detached-retention-proof" / "self_learning.db"]
    assert detached_db_paths[0] != runtime.queue.db_path
    assert all(job["status"] == "pending" for job in runtime.queue.list_jobs(limit=10))


def test_full_v3_to_v4_migration_is_safe_under_a_real_two_process_race(
    tmp_path: Path,
):
    detail = _load_harness()._migration_probe(tmp_path)

    assert detail == {
        "genuine_two_process_race": True,
        "single_v4_marker": True,
        "run_event_counts_preserved": True,
        "root_backfill": True,
        "origin_only_evidence": True,
        "auto_active_downgraded": True,
        "ambiguous_target_stale": True,
        "text_json_cleaned": True,
        "fts_rebuilt": True,
        "no_historical_jobs": True,
    }


def test_wal_mode_retry_is_locked_only_and_deterministic():
    ledger_class = _load_harness().SelfLearningLedger

    class LockedThenSuccess:
        def __init__(self):
            self.calls = 0

        def execute(self, statement: str):
            assert statement == "PRAGMA journal_mode=WAL"
            self.calls += 1
            if self.calls < 3:
                raise sqlite3.OperationalError("database is locked")
            return object()

    connection = LockedThenSuccess()
    ticks = iter((0.0, 0.1, 0.2))
    sleeps: list[float] = []
    ledger_class._enable_wal_mode(
        connection,
        timeout_seconds=1.0,
        monotonic_fn=lambda: next(ticks),
        sleep_fn=sleeps.append,
    )
    assert connection.calls == 3
    assert sleeps == [0.01, 0.01]

    class NonLockFailure:
        calls = 0

        def execute(self, _statement: str):
            self.calls += 1
            raise sqlite3.OperationalError("disk I/O error")

    failure = NonLockFailure()
    forbidden_sleeps: list[float] = []
    with pytest.raises(sqlite3.OperationalError, match="disk I/O error"):
        ledger_class._enable_wal_mode(
            failure,
            timeout_seconds=1.0,
            monotonic_fn=lambda: 0.0,
            sleep_fn=forbidden_sleeps.append,
        )
    assert failure.calls == 1
    assert forbidden_sleeps == []


def test_evidence_oracle_rejects_an_active_second_non_exact_conflict(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    harness = _load_harness()
    database = tmp_path / "self_learning.db"
    ledger = harness.SelfLearningLedger(database)
    store = harness.MemoryStore(database)
    sink = harness.SecurityArtifactSink(tmp_path / "security.jsonl")
    executor = harness.ThreadPoolExecutor(max_workers=1)
    runtime = harness.CampaignRuntime(
        ledger=ledger,
        memory_store=store,
        queue=harness.LearningJobQueue(database),
        nested_executor=executor,
        security_sink=sink,
        worker_artifacts=tmp_path / "worker-artifacts",
        events_per_run=2,
        source_stats={},
        kick_call_timings_ms=[],
        detached_launch_timings_ms=[],
        detached_launch_lock=harness.threading.Lock(),
        event_append_trace={},
        event_append_trace_lock=harness.threading.Lock(),
    )
    spec = next(
        item
        for item in harness.build_case_plan(100, 20260711)
        if item.category == "exact_evidence_conflict"
        and not item.oracle["exact"]
        and not item.oracle["same_run"]
    )
    auto_apply_pending = store.auto_apply_pending

    def corrupt_second_row(*args, **kwargs):
        result = auto_apply_pending(*args, **kwargs)
        scope_id = f"evidence-{spec.global_index}"
        with store._connect_for_write() as conn:
            conn.execute(
                """
                UPDATE memory_items SET status = 'active'
                WHERE id = (
                    SELECT MAX(id) FROM memory_items
                    WHERE scope_type = 'application' AND scope_id = ?
                )
                """,
                (scope_id,),
            )
        return result

    monkeypatch.setattr(store, "auto_apply_pending", corrupt_second_row)
    try:
        observed = harness._evidence_observation(spec, runtime)
    finally:
        executor.shutdown(wait=True)
        sink.close()

    assert observed["engine_ok"] is False


def test_smoke_campaign_writes_isolated_ledger_and_privacy_safe_artifacts(tmp_path: Path):
    harness = _load_harness()
    # Force multiple forensic rotations in a small campaign; the release
    # corpus uses the production 2,000-case segment size.
    harness._WAL_FORENSIC_CHUNK_CASES = 1
    source_db = tmp_path / "source.db"
    source_secret = "SOURCE_ONLY_SECRET_DO_NOT_COPY"
    with sqlite3.connect(source_db) as conn:
        conn.execute("CREATE TABLE runs (run_id TEXT PRIMARY KEY, task_text TEXT)")
        conn.execute("CREATE TABLE events (id INTEGER PRIMARY KEY, run_id TEXT, content_text TEXT)")
        conn.execute("INSERT INTO runs VALUES ('source-run', ?)", (source_secret,))
        conn.execute("INSERT INTO events VALUES (1, 'source-run', ?)", (source_secret,))
    source_before = _sha256(source_db)

    campaign_dir = harness.run_campaign(
        cases=100,
        seed=20260711,
        workers=2,
        output_root=tmp_path / "campaigns",
        source_db=source_db,
        campaign_id="smoke-campaign",
    )

    assert campaign_dir == tmp_path / "campaigns" / "smoke-campaign"
    expected_artifacts = {
        "environment.json",
        "manifest.jsonl.gz",
        "offline_metrics.json",
        "failures.jsonl",
        "privacy_audit.json",
        "report.md",
        "repro.sh",
        "self_learning.db",
    }
    assert expected_artifacts <= {path.name for path in campaign_dir.iterdir()}
    assert os.access(campaign_dir / "repro.sh", os.X_OK)
    assert _sha256(source_db) == source_before

    with gzip.open(campaign_dir / "manifest.jsonl.gz", "rt", encoding="utf-8") as handle:
        manifest = [json.loads(line) for line in handle if line.strip()]
    assert len(manifest) == 100
    assert all(row["passed"] is True for row in manifest)
    assert all(row["oracle"]["expected"] is True for row in manifest)
    assert len({row["oracle"]["variant"] for row in manifest}) >= 20
    assert all("payload" not in row for row in manifest)

    metrics = json.loads((campaign_dir / "offline_metrics.json").read_text(encoding="utf-8"))
    # A reduced corpus is useful for development, but must never be reported as
    # evidence that the fixed 100k release gate passed.
    assert metrics["status"] == "smoke_passed"
    assert metrics["campaign_type"] == "smoke"
    assert metrics["release_eligible"] is False
    assert metrics["requested_cases"] == 100
    assert metrics["executed_cases"] == 100
    assert metrics["failed_cases"] == 0
    assert metrics["central_ledger"]["events"] >= 100
    assert metrics["category_quotas"] == {
        "ledger_fts_search_scroll": 20,
        "redaction_digest_injection": 20,
        "exact_evidence_conflict": 15,
        "revision_trust_feedback": 15,
        "root_run_worker_concurrency": 10,
        "outbox_crash_lease_migration": 10,
        "ranking_snapshot_retention": 10,
    }
    assert metrics["payload_bytes"]["max"] == 60000
    assert 150 <= metrics["payload_bytes"]["p50"] <= 180
    assert 1900 <= metrics["payload_bytes"]["p95"] <= 2200
    assert metrics["coverage"]["fts_search"]["checked"] == 20
    assert metrics["coverage"]["fts_search"]["accuracy"] == 1.0
    assert metrics["coverage"]["fts_scroll"]["accuracy"] == 1.0
    assert metrics["coverage"]["session_end"]["checked"] == 10
    assert metrics["coverage"]["session_end"]["model_calls"] == 0
    assert metrics["coverage"]["session_end"]["kick_calls"] == 20
    assert metrics["coverage"]["session_end"]["expected_kick_calls"] == 20
    assert metrics["coverage"]["session_end"]["detached_launch_checked"] == 1
    assert metrics["coverage"]["root_persistence"] == {
        "checked": 10,
        "accuracy": 1.0,
        "repeated_finalize_cas_accuracy": 1.0,
    }
    assert metrics["coverage"]["production_jobs"]["session_review_checked"] >= 1
    assert metrics["coverage"]["production_jobs"]["retention_checked"] >= 1
    assert metrics["coverage"]["production_jobs"]["detached_central_job_terminal"] is True
    assert metrics["coverage"]["production_jobs"]["detached_central_job_attempts"] == 1
    assert (
        metrics["coverage"]["production_jobs"]["initial_review_audit_count"]
        == metrics["coverage"]["production_jobs"]["session_review_checked"]
    )
    assert metrics["coverage"]["production_jobs"]["root_review_jobs_attributed"] is True
    assert metrics["coverage"]["production_jobs"]["frozen_digest_reused"] is True
    assert metrics["coverage"]["production_jobs"]["review_audit_idempotent"] is True
    assert metrics["coverage"]["production_jobs"]["retention_crash_recovered"] is True
    assert set(metrics["coverage"]["security_routes"]) == {
        "task",
        "final_answer",
        "event",
        "session_note",
        "existing_memory",
        "repeated_failure",
        "curator",
    }
    assert all(metrics["coverage"]["security_routes"].values())
    assert metrics["coverage"]["exact_evidence"]["checked"] == 15
    assert metrics["coverage"]["revision_paths"]["checked"] == 15
    assert metrics["coverage"]["outbox_worker"]["checked"] == 10
    assert metrics["coverage"]["snapshot"] == {"checked": 3, "accuracy": 1.0}
    assert metrics["coverage"]["retention"] == {"checked": 1, "accuracy": 1.0}
    assert metrics["postflight"]["sqlite_integrity"] is True
    assert metrics["postflight"]["migration_v4"] is True
    assert all(metrics["postflight"]["migration_detail"].values())
    assert metrics["postflight"]["job_terminal_state"] is True

    environment = json.loads((campaign_dir / "environment.json").read_text(encoding="utf-8"))
    source_stats = environment["source_database"]
    assert source_stats["open_mode"] == "mode=ro&immutable=1"
    assert source_stats["tables"]["events"]["rows"] == 1
    assert source_stats["tables"]["runs"]["rows"] == 1
    assert source_secret not in json.dumps(source_stats, ensure_ascii=False)
    assert environment["live_shape_replay"]["source_events"] == 1
    assert environment["live_shape_replay"]["source_runs"] == 1
    assert environment["live_shape_replay"]["events_per_run"] == 2
    assert environment["live_shape_replay"]["source_content_rows_read"] == 0
    assert environment["live_shape_replay"]["content_length"]["count"] == 1

    privacy = json.loads((campaign_dir / "privacy_audit.json").read_text(encoding="utf-8"))
    assert privacy["passed"] is True
    assert privacy["source_open_mode"] == "mode=ro&immutable=1"
    assert privacy["source_content_rows_read"] == 0
    assert privacy["source_text_copied"] is False
    assert privacy["database_forbidden_hits"] == 0
    assert privacy["fts_forbidden_hits"] == 0
    assert privacy["artifact_forbidden_hits"] == 0
    assert privacy["wal_shm_files_scanned"] >= 40
    assert privacy["database_tables_scanned"] >= 10
    assert (campaign_dir / "failures.jsonl").read_text(encoding="utf-8") == ""
    assert "--only-case" in (campaign_dir / "repro.sh").read_text(encoding="utf-8")

    with sqlite3.connect(campaign_dir / "self_learning.db") as conn:
        assert conn.execute("SELECT COUNT(*) FROM events").fetchone()[0] >= 100
        stored = "\n".join(
            str(value or "")
            for row in conn.execute("SELECT content_text, input_json, output_json, metadata_json FROM events")
            for value in row
        )
    assert source_secret not in stored

    rendered_artifacts = "\n".join(
        path.read_text(encoding="utf-8")
        for path in campaign_dir.iterdir()
        if path.suffix in {".json", ".jsonl", ".md", ".sh"}
    )
    assert source_secret not in rendered_artifacts

    reproduced_dir = harness.run_campaign(
        cases=100,
        seed=20260711,
        workers=1,
        output_root=tmp_path / "campaigns",
        source_db=source_db,
        campaign_id="single-case-reproduction",
        only_case=manifest[0]["case_id"],
    )
    reproduced = json.loads((reproduced_dir / "offline_metrics.json").read_text(encoding="utf-8"))
    assert reproduced["status"] == "smoke_passed"
    assert reproduced["executed_cases"] == 1
    assert reproduced["central_ledger"]["events"] >= 1
    assert _sha256(source_db) == source_before

    safe_rows = [
        row
        for row in manifest
        if row["category"] == "redaction_digest_injection" and row["oracle"].get("positive") is False
    ]
    assert safe_rows
    assert all(row["observed"]["safe_retained"] is True for row in safe_rows)
    assert all(row["observed"]["blocked"] is False for row in safe_rows)


def test_release_gate_requires_live_fixed_point_execution():
    harness = _load_harness()

    assert harness.release_eligibility(100_000, 20_260_711) == {
        "fixed_case_count": True,
        "fixed_seed": True,
        "full_campaign": True,
        "fixed_reference_commit": True,
        "fixed_point_executed": False,
        "eligible": False,
    }
    assert (
        harness.release_eligibility(
            100_000,
            20_260_711,
            fixed_point_valid=True,
        )["eligible"]
        is True
    )
    assert (
        harness.release_eligibility(
            1_000,
            20_260_711,
            fixed_point_valid=True,
        )["eligible"]
        is False
    )
    assert (
        harness.release_eligibility(
            100_000,
            20_260_711,
            fixed_point_valid=True,
            reference_commit="f" * 40,
        )["eligible"]
        is False
    )


def test_release_resource_gates_measure_the_final_artifact_bundle(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    harness = _load_harness()
    campaign_dir = tmp_path / "release-tail"
    campaign_dir.mkdir()
    measured_after_outputs: list[set[str]] = []

    def oversized_final_bundle(path: Path) -> int:
        names = {candidate.name for candidate in path.iterdir()}
        required = {
            "privacy_audit.json",
            "offline_metrics.json",
            "failures.jsonl",
            "report.md",
        }
        assert required <= names
        measured_after_outputs.append(names)
        return harness._MAX_ARTIFACT_BYTES + 1

    monkeypatch.setattr(harness, "_artifact_bytes", oversized_final_bundle)
    monkeypatch.setattr(
        harness.time,
        "perf_counter",
        lambda: harness._MAX_DURATION_SECONDS + 1.0,
    )

    harness.finalize_campaign_outputs(
        campaign_dir,
        metrics={
            "status": "release_passed",
            "campaign_type": "release",
            "requested_cases": 100_000,
            "executed_cases": 100_000,
            "central_ledger": {"events": 100_000},
            "duration_seconds": 0.0,
            "artifact_bytes": 0,
            "privacy_passed": True,
            "gates": {},
        },
        gates={"privacy": True, "rss_under_2gb": True},
        case_failures=[],
        privacy={"passed": True, "forbidden_hits": 0},
        repro_base="loom validate --case release-tail",
        started=0.0,
    )

    persisted = json.loads(
        (campaign_dir / "offline_metrics.json").read_text(encoding="utf-8")
    )
    failure_ids = {
        json.loads(line)["case_id"]
        for line in (campaign_dir / "failures.jsonl").read_text(encoding="utf-8").splitlines()
        if line.strip()
    }
    assert measured_after_outputs
    assert len(measured_after_outputs) == 1
    assert persisted["status"] == "release_failed"
    assert persisted["artifact_bytes"] > harness._MAX_ARTIFACT_BYTES
    assert persisted["duration_seconds"] == harness._MAX_DURATION_SECONDS + 1.0
    assert persisted["gates"]["artifacts_under_3gb"] is False
    assert persisted["gates"]["duration_under_30m"] is False
    assert {"gate:artifacts_under_3gb", "gate:duration_under_30m"} <= failure_ids


def test_finalizer_persists_a_late_privacy_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    harness = _load_harness()
    campaign_dir = tmp_path / "late-privacy-failure"
    campaign_dir.mkdir()
    scans = 0

    def changing_scan(*_args, **_kwargs):
        nonlocal scans
        scans += 1
        passed = scans == 1
        return {
            "passed": passed,
            "artifact_forbidden_hits": 0 if passed else 1,
            "artifact_files_scanned": 4,
            "artifact_read_errors": 0,
            "artifact_read_error_paths": [],
        }

    monkeypatch.setattr(harness, "scan_artifact_files_privacy", changing_scan)
    monkeypatch.setattr(harness.time, "perf_counter", lambda: 1.0)

    harness.finalize_campaign_outputs(
        campaign_dir,
        metrics={
            "status": "smoke_passed",
            "campaign_type": "smoke",
            "requested_cases": 1,
            "executed_cases": 1,
            "central_ledger": {"events": 1},
            "duration_seconds": 0.0,
            "artifact_bytes": 0,
            "privacy_passed": True,
            "gates": {},
        },
        gates={"privacy": True},
        case_failures=[],
        privacy={"passed": True, "artifact_forbidden_hits": 0},
        repro_base="loom validate --case late-privacy-failure",
        started=0.0,
    )

    persisted = json.loads(
        (campaign_dir / "offline_metrics.json").read_text(encoding="utf-8")
    )
    privacy = json.loads(
        (campaign_dir / "privacy_audit.json").read_text(encoding="utf-8")
    )
    failure_ids = {
        json.loads(line)["case_id"]
        for line in (campaign_dir / "failures.jsonl").read_text(encoding="utf-8").splitlines()
        if line.strip()
    }
    assert scans >= 3
    assert persisted["status"] == "smoke_failed"
    assert persisted["privacy_passed"] is False
    assert persisted["gates"]["privacy"] is False
    assert privacy["passed"] is False
    assert "gate:privacy" in failure_ids


def test_finalizer_persists_exact_final_artifact_bytes(tmp_path: Path):
    harness = _load_harness()
    campaign_dir = tmp_path / "exact-final-bytes"
    campaign_dir.mkdir()
    (campaign_dir / "existing.bin").write_bytes(b"x" * 137)

    harness.finalize_campaign_outputs(
        campaign_dir,
        metrics={
            "status": "smoke_passed",
            "campaign_type": "smoke",
            "requested_cases": 1,
            "executed_cases": 1,
            "central_ledger": {"events": 1},
            "duration_seconds": 0.0,
            "artifact_bytes": 0,
            "privacy_passed": True,
            "gates": {},
        },
        gates={"privacy": True},
        case_failures=[],
        privacy={"passed": True, "artifact_forbidden_hits": 0},
        repro_base="loom validate --case exact-final-bytes",
        started=harness.time.perf_counter(),
    )

    persisted = json.loads(
        (campaign_dir / "offline_metrics.json").read_text(encoding="utf-8")
    )
    actual_bytes = sum(
        path.stat().st_size for path in campaign_dir.rglob("*") if path.is_file()
    )
    assert persisted["artifact_bytes"] == actual_bytes


def test_finalizer_duration_sampling_cannot_prevent_resource_stabilization(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    harness = _load_harness()
    campaign_dir = tmp_path / "duration-fixed-point"
    campaign_dir.mkdir()
    samples = iter((1.1, 1.111, 1.2, 1.222, 1.3, 1.333))
    monkeypatch.setattr(harness.time, "perf_counter", lambda: next(samples))

    harness.finalize_campaign_outputs(
        campaign_dir,
        metrics={
            "status": "smoke_passed",
            "campaign_type": "smoke",
            "requested_cases": 1,
            "executed_cases": 1,
            "central_ledger": {"events": 1},
            "duration_seconds": 0.0,
            "artifact_bytes": 0,
            "privacy_passed": True,
            "gates": {},
        },
        gates={"privacy": True},
        case_failures=[],
        privacy={"passed": True, "artifact_forbidden_hits": 0},
        repro_base="loom validate --case duration-fixed-point",
        started=0.0,
    )

    persisted = json.loads(
        (campaign_dir / "offline_metrics.json").read_text(encoding="utf-8")
    )
    assert persisted["duration_seconds"] == 1.1


def test_release_duration_metric_and_gate_use_the_same_boundary_sample(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    harness = _load_harness()
    campaign_dir = tmp_path / "duration-boundary"
    campaign_dir.mkdir()
    samples = iter((1799.999, 1800.001, 1800.002, 1800.003, 1800.004, 1800.005))
    monkeypatch.setattr(harness.time, "perf_counter", lambda: next(samples))

    harness.finalize_campaign_outputs(
        campaign_dir,
        metrics={
            "status": "release_passed",
            "campaign_type": "release",
            "requested_cases": 100_000,
            "executed_cases": 100_000,
            "central_ledger": {"events": 100_000},
            "duration_seconds": 0.0,
            "artifact_bytes": 0,
            "privacy_passed": True,
            "gates": {},
        },
        gates={"privacy": True},
        case_failures=[],
        privacy={"passed": True, "artifact_forbidden_hits": 0},
        repro_base="loom validate --case duration-boundary",
        started=0.0,
    )

    persisted = json.loads(
        (campaign_dir / "offline_metrics.json").read_text(encoding="utf-8")
    )
    assert persisted["gates"]["duration_under_30m"] is False
    assert persisted["duration_seconds"] > harness._MAX_DURATION_SECONDS


def test_release_corpus_contract_requires_exact_counts_quotas_and_source_shape():
    harness = _load_harness()
    quotas = harness.allocate_quotas(100_000)

    assert harness.release_corpus_contract(
        100_000,
        quotas,
        source_runs=82,
        source_events=1_706,
    ) == {
        "requested_exactly_100k": True,
        "executed_exactly_100k": True,
        "category_quotas_exact": True,
        "source_shape_82_runs_1706_events": True,
        "passed": True,
    }
    missing = dict(quotas)
    missing["ledger_fts_search_scroll"] -= 1
    failed = harness.release_corpus_contract(
        100_000,
        missing,
        source_runs=82,
        source_events=1_705,
    )
    assert failed["executed_exactly_100k"] is False
    assert failed["category_quotas_exact"] is False
    assert failed["source_shape_82_runs_1706_events"] is False
    assert failed["passed"] is False


def test_payload_profile_is_measured_in_utf8_bytes():
    harness = _load_harness()
    plan = harness.build_case_plan(1000, 20260711)

    payloads = [harness._payload_for(spec) for spec in plan]

    assert all(len(payload.encode("utf-8")) == spec.payload_bytes for spec, payload in zip(plan, payloads, strict=True))
    assert max(len(payload.encode("utf-8")) for payload in payloads) == 60_000


def test_security_negative_corpus_is_diverse_and_unicode_aware():
    harness = _load_harness()
    safe_specs = [
        spec
        for spec in harness.build_case_plan(1000, 20260711)
        if spec.category == "redaction_digest_injection"
        and spec.oracle.get("positive") is False
    ]
    variants = {str(spec.oracle["variant"]) for spec in safe_specs}
    fixtures = [harness._security_fixture(spec)["text"] for spec in safe_specs]

    assert len(variants) >= 12
    assert any("\u200d" in text for text in fixtures)
    assert any("\u200c" in text for text in fixtures)
    assert any("使用 UTF-8" in text for text in fixtures)
    assert any("يتم تصدير" in text for text in fixtures)


def test_security_oracle_rejects_partial_numeric_spaced_and_short_secret_leaks():
    harness = _load_harness()
    security_specs = [
        spec
        for spec in harness.build_case_plan(1000, 20260711)
        if spec.category == "redaction_digest_injection"
    ]

    def fixture(variant: str):
        spec = next(item for item in security_specs if item.oracle["variant"] == variant)
        return harness._security_fixture(spec)

    nested = fixture("nested_token_number")
    assert harness._security_surface_contract(
        '{"refresh_token":"[REDACTED]","password":"[REDACTED]"}',
        nested,
    )
    assert not harness._security_surface_contract(
        '{"refresh_token":7,"password":"[REDACTED]"}',
        nested,
    )

    spaced = fixture("client_secret_spaces")
    assert not harness._security_surface_contract(
        '{"clientSecret":"[REDACTED] spaced confidential tail"}',
        spaced,
    )

    password = fixture("password_short")
    authorization = fixture("bearer_short")
    assert 1 <= len(password["value"]["password"]) <= 3
    assert 1 <= len(authorization["text"].rsplit(":", 1)[-1].strip()) <= 3
    assert harness._security_surface_contract(
        '{"password":"[REDACTED]"}',
        password,
    )
    assert not harness._security_surface_contract(password["text"], password)

    for variant in (
        "fullwidth_password_key",
        "camel_credential",
        "tuple_authorization",
        "kebab_cookie",
        "free_text_secret",
        "secret_in_key",
        "set_api_key",
    ):
        partial_fixture = fixture(variant)
        marker = re.search(r"MVSECRET_[0-9a-f]+", partial_fixture["text"])
        assert marker is not None
        raw_marker = marker.group(0)
        partially_replaced = partial_fixture["text"].replace(
            raw_marker,
            "[REDACTED]" + raw_marker[len(raw_marker) // 2 :],
        )
        assert not harness._security_surface_contract(
            partially_replaced,
            partial_fixture,
        ), variant


def test_forensic_replay_preserves_every_route_and_fixture_contract_once():
    harness = _load_harness()
    source = [
        spec
        for spec in harness.build_case_plan(1000, 20260711)
        if spec.category == "redaction_digest_injection"
    ]
    selected = harness._security_forensic_support_set(source)

    def key(spec):
        return (
            spec.oracle["route"],
            spec.oracle["variant"],
            bool(spec.oracle["positive"]),
            bool(spec.oracle["injection"]),
        )

    assert {key(spec) for spec in selected} == {key(spec) for spec in source}
    assert len(selected) == len({key(spec) for spec in selected})


def test_default_privacy_scan_rejects_partial_and_short_secret_artifacts(
    tmp_path: Path,
):
    harness = _load_harness()
    (tmp_path / "partial.json").write_text(
        '{"client_secret":"spaced confidential tail [REDACTED]",'
        '"password":"§"}',
        encoding="utf-8",
    )

    audit = harness.scan_campaign_privacy(tmp_path)

    assert audit["passed"] is False
    assert audit["artifact_forbidden_hits"] >= 2


def test_final_status_privacy_scan_checks_only_the_supplied_files(tmp_path: Path):
    harness = _load_harness()
    safe = tmp_path / "offline_metrics.json"
    leaked = tmp_path / "failures.jsonl"
    unrelated = tmp_path / "large-existing-artifact.jsonl"
    safe.write_text('{"status":"release_passed"}\n', encoding="utf-8")
    leaked.write_text('{"error":"MVSECRET_final_status"}\n', encoding="utf-8")
    unrelated.write_text("MVSECRET_previously_scanned", encoding="utf-8")

    audit = harness.scan_artifact_files_privacy(
        [safe, leaked],
        ["MVSECRET_"],
    )

    assert audit == {
        "passed": False,
        "artifact_forbidden_hits": 1,
        "artifact_files_scanned": 2,
        "artifact_read_errors": 0,
        "artifact_read_error_paths": [],
    }


def test_wal_privacy_scan_streams_and_detects_chunk_boundary_secret(
    tmp_path: Path,
):
    harness = _load_harness()
    secret = b"MVSECRET_boundary_probe"
    wal = tmp_path / "self_learning.db-wal"
    boundary = 4 * 1024 * 1024
    wal.write_bytes(b"x" * (boundary - 5) + secret + b"safe-tail")

    leaked = harness.scan_wal_shm_privacy(tmp_path, [secret.decode()])

    assert leaked == {
        "passed": False,
        "wal_shm_files_scanned": 1,
        "wal_shm_forbidden_hits": 1,
        "wal_shm_forbidden_hit_fingerprints": [
            {
                "path": "self_learning.db-wal",
                "prefix_sha256": hashlib.sha256(secret).hexdigest(),
                "count": 1,
                "offsets": [boundary - 5],
            }
        ],
        "wal_shm_read_errors": 0,
        "wal_shm_disappeared_files": 0,
        "wal_shm_read_error_paths": [],
    }
    wal.write_bytes(b"safe" * 1024)
    assert harness.scan_wal_shm_privacy(tmp_path, [secret.decode()]) == {
        "passed": True,
        "wal_shm_files_scanned": 1,
        "wal_shm_forbidden_hits": 0,
        "wal_shm_forbidden_hit_fingerprints": [],
        "wal_shm_read_errors": 0,
        "wal_shm_disappeared_files": 0,
        "wal_shm_read_error_paths": [],
    }


def test_wal_privacy_scan_does_not_mislabel_open_race_as_secret_hit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    harness = _load_harness()
    wal = tmp_path / "self_learning.db-wal"
    wal.write_bytes(b"entirely-safe")
    original_open = Path.open

    def disappearing_open(path: Path, *args, **kwargs):
        if path == wal:
            raise FileNotFoundError(path)
        return original_open(path, *args, **kwargs)

    monkeypatch.setattr(Path, "open", disappearing_open)
    audit = harness.scan_wal_shm_privacy(tmp_path, ["MVSECRET_"])

    assert audit["passed"] is False
    assert audit["wal_shm_files_scanned"] == 1
    assert audit["wal_shm_forbidden_hits"] == 0
    assert audit["wal_shm_forbidden_hit_fingerprints"] == []
    assert audit["wal_shm_read_errors"] == 1
    assert audit["wal_shm_disappeared_files"] == 1
    assert audit["wal_shm_read_error_paths"] == ["self_learning.db-wal"]


def test_fixed_point_benchmark_executes_clean_git_tree_and_binds_manifest(
    tmp_path: Path,
):
    harness = _load_harness()
    assert harness.FIXED_POINT_BENCHMARK_PAIRS == 10
    assert harness.FIXED_POINT_BENCHMARK_EVENTS == 2_000
    assert harness.FIXED_POINT_WARMUP_EVENTS == 100
    result = harness.run_fixed_point_comparison(
        tmp_path,
        source_shape_sha256="shape-sha",
        source_runs=82,
        source_events=1_706,
        events_per_run=21,
        benchmark_events=20,
        benchmark_pairs=2,
        warmup_events=3,
    )

    assert result["valid"] is True, result["reason"]
    manifest = result["manifest"]
    assert manifest["reference_commit"] == harness.DEFAULT_BASELINE_COMMIT
    assert manifest["reference_clean"] is True
    assert manifest["source_runs"] == 82
    assert manifest["source_events"] == 1_706
    assert manifest["source_shape_sha256"] == "shape-sha"
    assert manifest["driver_sha256"] == hashlib.sha256(harness.FIXED_POINT_DRIVER.read_bytes()).hexdigest()
    assert manifest["current_code_stable_during_benchmark"] is True
    assert manifest["loaded_module_hashes_match_manifest"] is True
    assert manifest["current_loaded_module_hashes"]
    assert manifest["reference_loaded_module_hashes"]
    assert manifest["benchmark_protocol"] == {
        "pair_count": 2,
        "measured_events_per_implementation_per_pair": 20,
        "warmup_events_per_implementation_per_pair": 3,
        "warmup_database": "disposable_and_excluded_from_measurement",
        "fts_queries_per_implementation_per_pair": 20,
        "schedule_seed": 20_260_711,
        "pair_seeds": [20_260_711, 20_260_712],
        "execution_order": [
            ["reference", "current"],
            ["current", "reference"],
        ],
        "payload_profile": {
            "algorithm": "seeded_quantile_slots_v1",
            "encoding": "utf-8",
            "target_decimal_bytes": {
                "p50_approx": 160,
                "p95_approx": 2_000,
                "p99_approx": 32_000,
                "max": 60_000,
            },
            "actual_nearest_rank_bytes": {
                "p50": 155,
                "p95": 2_034,
                "p99": 60_000,
                "max": 60_000,
            },
        },
        "payload_order": "independently_verified_seeded_by_pair_seed",
        "statistic": "paired_log_ratio_one_sided_t_ucb_95",
        "max_regression": 0.2,
    }
    claimed_manifest_sha = manifest.pop("manifest_sha256")
    assert (
        claimed_manifest_sha
        == hashlib.sha256(json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    )
    assert all(float(value) > 0 for value in result["performance"].values())
    assert all(float(value) > 0 for value in result["current_performance"].values())
    paired = result["paired_analysis"]
    assert paired["valid"] is True
    assert paired["pair_count"] == 2
    assert set(paired["metrics"]) == {
        "ledger_append_p95_ms",
        "fts_query_p95_ms",
        "bytes_per_event",
    }
    benchmark_dir = tmp_path / "fixed_point_benchmark"
    assert (benchmark_dir / "summary.json").is_file()
    pair_dirs = sorted((benchmark_dir / "pairs").iterdir())
    assert [path.name for path in pair_dirs] == ["pair-01", "pair-02"]
    assert json.loads((pair_dirs[0] / "pair.json").read_text())["execution_order"] == [
        "reference",
        "current",
    ]
    assert json.loads((pair_dirs[1] / "pair.json").read_text())["execution_order"] == [
        "current",
        "reference",
    ]
    database_artifacts = sorted(benchmark_dir.glob("pairs/*/*.db"))
    assert len(database_artifacts) == 4
    for pair_dir in pair_dirs:
        pair_manifest = json.loads((pair_dir / "pair.json").read_text())
        for implementation in ("reference", "current"):
            artifact = pair_manifest["artifacts"][implementation]["database"]
            database = pair_dir / artifact["path"]
            assert artifact["size_bytes"] == database.stat().st_size
            assert artifact["sha256"] == _sha256(database)
            raw = json.loads((pair_dir / f"{implementation}.json").read_text())
            assert len(raw["samples"]["ledger_append"]) == 20
            assert len(raw["samples"]["fts_query"]) == 20
            assert raw["samples"]["system_load"]
        assert pair_manifest["query_order_sha256"]
    args = harness._parser().parse_args([])
    assert not hasattr(args, "baseline_metrics")
    assert not hasattr(args, "baseline_sha256")


def test_fixed_point_driver_excludes_warmup_and_persists_raw_latency_and_load(
    tmp_path: Path,
):
    driver = _load_fixed_point_driver()
    result = driver.run_benchmark(
        target_repo=Path(__file__).resolve().parents[2],
        db_path=tmp_path / "fixed-point.db",
        events=10,
        warmup_events=3,
        seed=20_260_711,
        events_per_run=2,
        require_clean=False,
    )

    assert result["protocol"]["spec"]["events"] == 10
    assert result["protocol"]["spec"]["warmup_events"] == 3
    assert result["counts"] == {
        "warmup_events": 3,
        "measured_events": 10,
        "database_events": 10,
    }
    assert "performance" not in result
    append_samples = result["samples"]["ledger_append"]
    query_samples = result["samples"]["fts_query"]
    assert len(append_samples) == 10
    assert len(query_samples) == 10
    assert [sample["index"] for sample in append_samples] == list(range(10))
    assert len({sample["index"] for sample in query_samples}) == 10
    assert all(float(sample["elapsed_ms"]) > 0 for sample in append_samples + query_samples)
    load_samples = result["samples"]["system_load"]
    assert {sample["phase"] for sample in load_samples} >= {
        "start",
        "warmup_complete",
        "append_complete",
        "query_complete",
    }
    assert all(
        {"elapsed_seconds", "load_1m", "load_5m", "load_15m", "user_cpu_seconds", "system_cpu_seconds"}
        <= set(sample)
        for sample in load_samples
    )
    assert not list(tmp_path.glob(".fixed-point-warmup-*"))


def test_fixed_point_driver_persists_seeded_literal_payload_profile_for_2000_events(
    tmp_path: Path,
):
    """The measured DB, not a protocol label, must carry the 160B/2K/32K/60K corpus."""
    driver = _load_fixed_point_driver()
    seed = 20_260_711
    events = 2_000
    result = driver.run_benchmark(
        target_repo=Path(__file__).resolve().parents[2],
        db_path=tmp_path / "fixed-point-profile.db",
        events=events,
        warmup_events=0,
        seed=seed,
        events_per_run=50,
        require_clean=False,
    )

    # Independent oracle: translate evenly spaced ranks onto the literal
    # 1,000-slot contract, then seed-shuffle the sizes assigned to event ids.
    # This intentionally does not call the benchmark's payload helpers.
    expected_sizes: list[int] = []
    for rank in range(events):
        slot = (rank * 999) // (events - 1)
        if slot <= 499:
            size = 96 + (slot * 63) // 499
        elif slot <= 949:
            size = 160 + ((slot - 500) * 1_887) // 449
        elif slot <= 989:
            size = 2_048 + ((slot - 950) * 29_951) // 39
        elif slot <= 998:
            size = 32_000 + ((slot - 990) * 27_999) // 8
        else:
            size = 60_000
        expected_sizes.append(size)
    random.Random(seed).shuffle(expected_sizes)

    with sqlite3.connect(tmp_path / "fixed-point-profile.db") as connection:
        rows = connection.execute(
            """
            SELECT event_id, length(CAST(content_text AS BLOB))
            FROM events ORDER BY event_id
            """
        ).fetchall()
    actual_sizes = [int(row[1]) for row in rows]

    def nearest_rank(values: list[int], quantile: float) -> int:
        ordered = sorted(values)
        return ordered[math.ceil(len(ordered) * quantile) - 1]

    expected_profile = {
        "p50": 159,
        "p95": 2_047,
        "p99": 31_999,
        "max": 60_000,
    }
    assert [str(row[0]) for row in rows] == [
        f"fixed-point-event-{index:09d}" for index in range(events)
    ]
    assert actual_sizes == expected_sizes
    assert {
        "p50": nearest_rank(actual_sizes, 0.50),
        "p95": nearest_rank(actual_sizes, 0.95),
        "p99": nearest_rank(actual_sizes, 0.99),
        "max": max(actual_sizes),
    } == expected_profile
    assert result["protocol"]["spec"]["payload_profile"] == {
        "algorithm": "seeded_quantile_slots_v1",
        "encoding": "utf-8",
        "target_decimal_bytes": {
            "p50_approx": 160,
            "p95_approx": 2_000,
            "p99_approx": 32_000,
            "max": 60_000,
        },
        "actual_nearest_rank_bytes": expected_profile,
    }
    assert result["protocol"]["spec"]["payload_order_sha256"] == hashlib.sha256(
        json.dumps(actual_sizes, separators=(",", ":")).encode()
    ).hexdigest()


def test_fixed_point_query_order_is_seeded_unique_and_uses_one_thousand_queries():
    driver = _load_fixed_point_driver()

    first = driver._query_order(2_000, 20_260_711)
    second = driver._query_order(2_000, 20_260_711)

    assert first == second
    assert len(first) == 1_000
    assert len(set(first)) == 1_000
    assert set(first) <= set(range(2_000))


def test_orchestrator_recomputes_performance_from_raw_samples_not_driver_claims(
    tmp_path: Path,
):
    harness = _load_harness()
    driver = _load_fixed_point_driver()
    database = tmp_path / "measured.db"
    query_order = [0, 2, 5, 7, 4, 9, 3, 1, 8, 6]
    result = driver.run_benchmark(
        target_repo=Path(__file__).resolve().parents[2],
        db_path=database,
        events=10,
        warmup_events=3,
        seed=20_260_711,
        events_per_run=2,
        require_clean=False,
    )
    result["performance"] = {
        "ledger_append_p95_ms": 0.0,
        "fts_query_p95_ms": 0.0,
        "bytes_per_event": 0.0,
    }
    result["samples"]["ledger_append"] = [
        {"index": index, "elapsed_ms": float(index + 1)} for index in range(10)
    ]
    result["samples"]["fts_query"] = [
        {"index": event_index, "elapsed_ms": float(position + 2)}
        for position, event_index in enumerate(query_order)
    ]

    performance = harness._performance_from_raw_samples(
        result,
        database,
        measured_events=10,
        pair_seed=20_260_711,
        warmup_events=3,
    )

    assert performance == {
        "ledger_append_p95_ms": 10.0,
        "fts_query_p95_ms": 11.0,
        "bytes_per_event": round(database.stat().st_size / 10, 6),
    }

    result["samples"]["fts_query"][0], result["samples"]["fts_query"][1] = (
        result["samples"]["fts_query"][1],
        result["samples"]["fts_query"][0],
    )
    with pytest.raises(ValueError, match="pre-registered order"):
        harness._performance_from_raw_samples(
            result,
            database,
            measured_events=10,
            pair_seed=20_260_711,
            warmup_events=3,
        )

    result["samples"]["fts_query"].sort(key=lambda sample: query_order.index(sample["index"]))
    result["counts"]["database_events"] = 13
    with pytest.raises(ValueError, match="count contract"):
        harness._performance_from_raw_samples(
            result,
            database,
            measured_events=10,
            pair_seed=20_260_711,
            warmup_events=3,
        )


def test_orchestrator_fails_closed_when_measured_payload_tail_is_missing(
    tmp_path: Path,
):
    driver = _load_fixed_point_driver()
    harness = _load_harness()
    database = tmp_path / "measured-tail.db"
    result = driver.run_benchmark(
        target_repo=Path(__file__).resolve().parents[2],
        db_path=database,
        events=100,
        warmup_events=0,
        seed=20_260_711,
        events_per_run=10,
        require_clean=False,
    )
    with sqlite3.connect(database) as connection:
        longest_event_id = str(
            connection.execute(
                """
                SELECT event_id FROM events
                ORDER BY length(CAST(content_text AS BLOB)) DESC LIMIT 1
                """
            ).fetchone()[0]
        )
        connection.execute(
            "UPDATE events SET content_text = 'tail removed' WHERE event_id = ?",
            (longest_event_id,),
        )
        connection.commit()
        connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")

    with pytest.raises(ValueError, match="measured payload distribution/order mismatch"):
        harness._performance_from_raw_samples(
            result,
            database,
            measured_events=100,
            pair_seed=20_260_711,
            warmup_events=0,
        )


def test_paired_log_ucb_detects_uncertainty_hidden_by_median_ratio():
    harness = _load_harness()
    ratios = [1.10] * 5 + [1.25] * 5
    pairs = [
        {
            "pair_index": index + 1,
            "performance": {
                "reference": {
                    "ledger_append_p95_ms": 1.0,
                    "fts_query_p95_ms": 1.0,
                    "bytes_per_event": 1.0,
                },
                "current": {
                    "ledger_append_p95_ms": ratio,
                    "fts_query_p95_ms": ratio,
                    "bytes_per_event": ratio,
                },
            },
        }
        for index, ratio in enumerate(ratios)
    ]

    analysis = harness._paired_log_analysis(pairs)

    metric = analysis["metrics"]["ledger_append_p95_ms"]
    assert metric["median_ratio"] == 1.175
    assert math.isclose(metric["geometric_mean_ratio"], 1.17260394, abs_tol=1e-8)
    assert math.isclose(metric["upper_confidence_ratio"], 1.21930664, abs_tol=1e-8)
    assert metric["passed"] is False
    assert analysis["passed"] is False

    gate = harness._regression_gate(
        {
            "ledger_append_p95_ms": 1.175,
            "fts_query_p95_ms": 1.175,
            "bytes_per_event": 1.175,
        },
        {
            "ledger_append_p95_ms": 1.0,
            "fts_query_p95_ms": 1.0,
            "bytes_per_event": 1.0,
        },
        paired_analysis=analysis,
    )
    assert gate["ratios"]["ledger_append_p95_ms"] == 1.175
    assert gate["upper_confidence_ratios"]["ledger_append_p95_ms"] == 1.21930664
    assert gate["passed"] is False


def test_bytes_regression_requires_every_pair_to_stay_within_limit():
    harness = _load_harness()
    pairs = []
    for index in range(10):
        pairs.append(
            {
                "pair_index": index + 1,
                "performance": {
                    "reference": {
                        "ledger_append_p95_ms": 1.0,
                        "fts_query_p95_ms": 1.0,
                        "bytes_per_event": 1.0,
                    },
                    "current": {
                        "ledger_append_p95_ms": 1.0,
                        "fts_query_p95_ms": 1.0,
                        "bytes_per_event": 1.21 if index == 9 else 1.0,
                    },
                },
            }
        )

    analysis = harness._paired_log_analysis(pairs)

    bytes_metric = analysis["metrics"]["bytes_per_event"]
    assert bytes_metric["upper_confidence_ratio"] < 1.20
    assert bytes_metric["max_pair_ratio"] == 1.21
    assert bytes_metric["all_pairs_within_limit"] is False
    assert bytes_metric["passed"] is False
    assert analysis["passed"] is False


def test_benchmark_execution_order_is_seed_shuffled_and_balanced():
    harness = _load_harness()

    orders = harness._benchmark_execution_orders(10, 20_260_711)

    assert orders == [
        ["reference", "current"],
        ["reference", "current"],
        ["current", "reference"],
        ["current", "reference"],
        ["reference", "current"],
        ["current", "reference"],
        ["reference", "current"],
        ["reference", "current"],
        ["current", "reference"],
        ["current", "reference"],
    ]
    assert orders.count(["reference", "current"]) == 5
    assert orders.count(["current", "reference"]) == 5


def test_regression_gate_fails_closed_without_preregistered_paired_analysis():
    harness = _load_harness()
    current = {
        "ledger_append_p95_ms": 1.0,
        "fts_query_p95_ms": 1.0,
        "bytes_per_event": 1.0,
    }

    gate = harness._regression_gate(current, current, paired_analysis={})

    assert gate == {
        "applicable": False,
        "passed": False,
        "reason": "valid paired fixed-point analysis required",
    }


@pytest.mark.parametrize(
    "malformed",
    [
        {},
        {
            "ledger_append_p95_ms": 0.0,
            "fts_query_p95_ms": 1.0,
            "bytes_per_event": 1.0,
        },
        {
            "ledger_append_p95_ms": float("nan"),
            "fts_query_p95_ms": 1.0,
            "bytes_per_event": 1.0,
        },
    ],
)
def test_regression_gate_fails_closed_for_missing_zero_or_nan_metrics(malformed):
    harness = _load_harness()
    valid = {
        "ledger_append_p95_ms": 1.0,
        "fts_query_p95_ms": 1.0,
        "bytes_per_event": 1.0,
    }

    gate = harness._regression_gate(malformed, valid, paired_analysis={})

    assert gate == {
        "applicable": False,
        "passed": False,
        "reason": "live fixed-point benchmark required",
    }


def test_regression_gate_fails_closed_for_malformed_paired_metric_details():
    harness = _load_harness()
    valid = {
        "ledger_append_p95_ms": 1.0,
        "fts_query_p95_ms": 1.0,
        "bytes_per_event": 1.0,
    }
    malformed_analysis = {
        "valid": True,
        "pair_count": 10,
        "method": "paired_log_ratio_one_sided_t_ucb_95",
        "metrics": {
            "ledger_append_p95_ms": None,
            "fts_query_p95_ms": {},
            "bytes_per_event": {"upper_confidence_ratio": "nan"},
        },
    }

    gate = harness._regression_gate(
        valid,
        valid,
        paired_analysis=malformed_analysis,
    )

    assert gate == {
        "applicable": False,
        "passed": False,
        "reason": "valid paired fixed-point analysis required",
    }


def test_scroll_oracle_requires_the_exact_adjacent_event():
    source = SCRIPT_PATH.read_text(encoding="utf-8")

    assert "expected_neighbor_event_id" in source
    assert 'item.get("event_id") == expected_neighbor_event_id' in source


def test_scroll_oracle_does_not_trust_a_shared_uut_ordering_bug(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    harness = _load_harness()
    ledger = harness.SelfLearningLedger(tmp_path / "scroll.db")
    run_id = "massval-run-000000"
    specs = []
    event_ids = []
    for index in range(3):
        event_id = f"massval-event-{index:09d}"
        marker = f"mvtoken-scroll-{index}"
        event_ids.append(event_id)
        specs.append(
            harness.CaseSpec(
                global_index=index,
                category_index=index,
                category="ledger_fts_search_scroll",
                case_id=f"scroll-{index}",
                marker=marker,
                payload_bytes=160,
                oracle={"variant": "unique_marker", "expected": True},
            )
        )
        ledger.append_event(
            harness.CanonicalSessionEvent(
                event_id=event_id,
                run_id=run_id,
                root_run_id=run_id,
                event_type="tool_result",
                content_text=marker,
            )
        )

    # Simulate one shared UUT defect: append persisted the reverse ordinal, and
    # scroll consistently follows that same wrong ordinal.  A DB-derived oracle
    # would accept C,B,A; the independent trace must still require A,B,C.
    with ledger._connect() as conn:
        for ordinal, event_id in enumerate(reversed(event_ids)):
            conn.execute(
                "UPDATE events SET ordinal = ? WHERE event_id = ?",
                (ordinal, event_id),
            )

    def wrong_scroll(
        requested_run_id: str,
        anchor_id: int,
        *,
        direction: str = "after",
        window: int = 5,
    ):
        comparator = ">" if direction == "after" else "<"
        order = "ASC" if direction == "after" else "DESC"
        with ledger._connect() as conn:
            anchor = conn.execute(
                "SELECT ordinal FROM events WHERE id = ?",
                (anchor_id,),
            ).fetchone()
            rows = conn.execute(
                f"SELECT * FROM events WHERE run_id = ? AND ordinal {comparator} ? "
                f"ORDER BY ordinal {order} LIMIT ?",
                (requested_run_id, int(anchor["ordinal"]), int(window)),
            ).fetchall()
        return [ledger._row_to_event_dict(row) for row in rows]

    monkeypatch.setattr(ledger, "scroll_events", wrong_scroll)
    result = harness._fts_postflight(ledger, specs, {run_id: event_ids})

    assert result["search_accuracy"] == 1.0
    assert result["scroll_checked"] == 3
    assert result["scroll_accuracy"] == 0.0


def test_privacy_scanner_covers_main_db_fts_shadow_and_artifacts(tmp_path: Path):
    harness = _load_harness()
    db = tmp_path / "campaign.db"
    with sqlite3.connect(db) as conn:
        conn.executescript(
            """
            CREATE TABLE events (content_text TEXT);
            CREATE TABLE events_fts_content (c0 TEXT);
            INSERT INTO events VALUES ('safe');
            INSERT INTO events_fts_content VALUES ('MVSECRET_in_fts');
            """
        )
    (tmp_path / "proposal.json").write_text('{"proposal":"MVINJECT_in_artifact"}', encoding="utf-8")

    audit = harness.scan_campaign_privacy(tmp_path, ["MVSECRET_", "MVINJECT_"])

    assert audit["database_forbidden_hits"] >= 1
    assert audit["fts_forbidden_hits"] >= 1
    assert audit["artifact_forbidden_hits"] >= 1
    assert audit["passed"] is False


def test_wal_privacy_guard_catches_insert_delete_even_after_autocheckpoint(
    tmp_path: Path,
):
    harness = _load_harness()
    db = tmp_path / "campaign.db"
    with sqlite3.connect(db) as setup:
        setup.execute("PRAGMA journal_mode=WAL")
        setup.execute("CREATE TABLE events (content_text TEXT)")
    keeper = sqlite3.connect(db)
    try:
        keeper.execute("PRAGMA journal_mode=WAL")
        keeper.execute("PRAGMA wal_autocheckpoint=0")
        keeper.execute("BEGIN")
        keeper.execute("SELECT COUNT(*) FROM events").fetchone()
        with sqlite3.connect(db) as writer:
            writer.execute("PRAGMA wal_autocheckpoint=1")
            writer.execute("INSERT INTO events VALUES ('MVSECRET_wal_only')")
            writer.commit()
            writer.execute("DELETE FROM events")
            writer.commit()
            writer.execute("PRAGMA wal_checkpoint(PASSIVE)").fetchone()

        with sqlite3.connect(db) as verifier:
            assert verifier.execute("SELECT COUNT(*) FROM events").fetchone()[0] == 0
        audit = harness.scan_campaign_privacy(tmp_path, ["MVSECRET_"])
    finally:
        keeper.rollback()
        keeper.close()

    assert audit["wal_shm_files_scanned"] >= 2
    assert audit["wal_shm_forbidden_hits"] >= 1
    assert audit["passed"] is False


def test_harness_does_not_forge_terminal_job_state():
    source = SCRIPT_PATH.read_text(encoding="utf-8")

    assert "UPDATE learning_jobs\n            SET status='succeeded'" not in source
    assert "LearningJobWorker" in source
    assert "session_finalize_hook" in source
    assert 'lambda job: {"validated": True' not in source
    assert "process_session_review_job" in source
    assert "process_retention_job" in source
