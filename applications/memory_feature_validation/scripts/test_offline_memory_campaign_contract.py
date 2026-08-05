"""Contract tests for the deterministic v6 offline memory campaign."""

from __future__ import annotations

import ast
import gzip
import inspect
import json
import math
import sqlite3
import subprocess
import sys
import threading
from collections import Counter
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from applications.memory_feature_validation.scripts import (  # noqa: E402
    run_offline_memory_campaign as offline_runner,
)
from applications.memory_feature_validation.scripts.offline_memory_campaign_common import (  # noqa: E402
    CATEGORY_WEIGHTS,
    DEFAULT_EVENTS,
    DEFAULT_SEED,
    allocate_quotas,
    build_case_plan,
    case_artifact_row,
    payload_size_for_position,
)
from applications.memory_feature_validation.scripts.run_offline_memory_campaign import (  # noqa: E402
    _REQUIRED_CORE_GATES,
    _default_campaign_id,
    _load_baseline_metrics,
    _parser,
    _privacy_scan,
    _replayed_event_type,
    _source_manifest,
    _source_shape,
    audit_campaign,
    run_campaign,
)


def test_default_offline_campaign_ids_are_collision_resistant_path_components() -> None:
    campaign_ids = {_default_campaign_id("offline-v6") for _ in range(64)}

    assert len(campaign_ids) == 64
    assert all(Path(value).name == value for value in campaign_ids)


def test_release_plan_is_exactly_100k_v6_events_with_fixed_seed() -> None:
    assert DEFAULT_EVENTS == 100_000
    assert DEFAULT_SEED == 20_260_711
    assert allocate_quotas(DEFAULT_EVENTS) == {
        "ledger_fts_search_scroll": 50_000,
        "redaction_injection": 20_000,
        "root_isolation": 20_000,
        "active_pending_memory": 10_000,
    }

    plan = build_case_plan(DEFAULT_EVENTS, DEFAULT_SEED)

    assert len(plan) == DEFAULT_EVENTS
    assert Counter(case.category for case in plan) == Counter(allocate_quotas(DEFAULT_EVENTS))
    assert len({case.case_id for case in plan}) == DEFAULT_EVENTS
    assert not {
        "exact_evidence_conflict",
        "revision_trust_feedback",
        "outbox_crash_lease_migration",
        "ranking_snapshot_retention",
    } & {case.category for case in plan}


def test_memory_state_cohort_is_not_cut_off_by_runtime_prompt_budget(
    tmp_path: Path,
) -> None:
    cases = [case for case in build_case_plan(5_000, DEFAULT_SEED) if case.category == "active_pending_memory"]
    failures: list[dict[str, object]] = []
    db_path = tmp_path / "self_learning.db"
    ledger = offline_runner.SelfLearningLedger(db_path)
    offline_runner._append_cases(ledger, cases)

    metrics = offline_runner._validate_memory_cases(
        db_path,
        cases,
        failures,
    )

    assert len(cases) == 500
    assert metrics["persistent_failures"] == 0
    assert failures == []

    approval_case = next(case for case in cases if case.variant == "approve_pending")
    tampered_db = tmp_path / "uncompleted-root.db"
    tampered_ledger = offline_runner.SelfLearningLedger(tampered_db)
    offline_runner._append_cases(tampered_ledger, [approval_case])
    with offline_runner.SelfLearningDatabase(tampered_db).connect() as conn:
        conn.execute(
            "UPDATE runs SET status='indexed' WHERE run_id=?",
            (f"memory-run-{approval_case.category_index:05d}",),
        )
    store = offline_runner.MemoryStore(
        tampered_db,
        agent_config=offline_runner._memory_config(
            "offline_validation",
            approval=False,
        ),
    )
    with pytest.raises(
        offline_runner.ReviewConflictError,
        match="offline_evidence_root_not_completed",
    ):
        offline_runner._exercise_memory_case(store, approval_case)

    with offline_runner.SelfLearningDatabase(tampered_db).connect() as conn:
        conn.execute(
            "UPDATE runs SET status='completed' WHERE run_id=?",
            (f"memory-run-{approval_case.category_index:05d}",),
        )
        conn.execute(
            "UPDATE events SET content_text='tampered completion evidence' "
            "WHERE run_id=? AND event_type='run_completed'",
            (f"memory-run-{approval_case.category_index:05d}",),
        )
    with pytest.raises(
        offline_runner.ReviewConflictError,
        match="offline_evidence_scope_mismatch",
    ):
        offline_runner._exercise_memory_case(store, approval_case)


def test_all_offline_events_use_the_public_ledger_append_boundary() -> None:
    source = inspect.getsource(offline_runner._append_cases)

    assert "_append_event_in_conn" not in source
    assert "ledger._connect" not in source


def test_case_plan_is_reproducible_and_seed_bound() -> None:
    first = build_case_plan(1_000, DEFAULT_SEED)
    repeated = build_case_plan(1_000, DEFAULT_SEED)
    other_seed = build_case_plan(1_000, DEFAULT_SEED + 1)

    assert first == repeated
    assert [case.case_id for case in first] == [case.case_id for case in other_seed]
    assert [case.payload_bytes for case in first] == [case.payload_bytes for case in other_seed]
    assert [case.private_token for case in first] != [case.private_token for case in other_seed]


def test_oracle_is_stdlib_only_and_does_not_call_production_classifiers() -> None:
    path = Path(__file__).with_name("offline_memory_campaign_common.py")
    source = path.read_text(encoding="utf-8")
    tree = ast.parse(source)
    imports = {alias.name for node in ast.walk(tree) if isinstance(node, ast.Import) for alias in node.names} | {
        str(node.module or "") for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)
    }

    assert not any(name == "src" or name.startswith("src.") for name in imports)
    assert "redact_text(" not in source
    assert "scan_injection_patterns(" not in source
    assert "memory_content_hash(" not in source


def test_payload_profile_has_literal_percentile_boundaries() -> None:
    assert payload_size_for_position(0, 1_000) == 96
    assert payload_size_for_position(499, 1_000) == 159
    assert payload_size_for_position(500, 1_000) == 160
    assert payload_size_for_position(949, 1_000) == 2_047
    assert payload_size_for_position(950, 1_000) == 2_048
    assert payload_size_for_position(989, 1_000) == 31_999
    assert payload_size_for_position(990, 1_000) == 32_000
    assert payload_size_for_position(998, 1_000) == 59_999
    assert payload_size_for_position(999, 1_000) == 60_000


def test_every_v6_category_gets_the_full_payload_profile() -> None:
    plan = build_case_plan(DEFAULT_EVENTS, DEFAULT_SEED)
    by_category = {
        category: [case.payload_bytes for case in plan if case.category == category] for category in CATEGORY_WEIGHTS
    }

    assert all(min(values) == 96 for values in by_category.values())
    assert all(max(values) == 60_000 for values in by_category.values())


def test_case_artifact_never_contains_generated_payload_or_private_marker() -> None:
    security = next(
        case
        for case in build_case_plan(100, DEFAULT_SEED)
        if case.category == "redaction_injection" and case.expected_class != "safe"
    )

    row = case_artifact_row(security)
    encoded = json.dumps(row, sort_keys=True)

    assert set(row) == {
        "case_id",
        "category",
        "variant",
        "expected_class",
        "payload_bytes",
    }
    assert "MVSECRET_" not in encoded
    assert "MVINJECT_" not in encoded
    assert "payload" not in row
    assert "marker" not in row


def test_cli_defaults_are_the_only_release_eligible_shape() -> None:
    args = _parser().parse_args([])

    assert args.events == DEFAULT_EVENTS
    assert args.seed == DEFAULT_SEED
    assert args.only_case is None
    assert args.source_db == REPO_ROOT / ".agentloom" / "self_learning.db"
    assert args.baseline_metrics is None


def test_release_source_gate_ignores_unrelated_worktree_changes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert {
        "src/extensions/self_learning/application_scope.py",
        "src/extensions/self_learning/paths.py",
        "src/extensions/self_learning/persistence/review_engine.py",
        "src/extensions/self_learning/review_types.py",
        "src/lib/runtime/context.py",
        "src/lib/trusted_memory_evidence.py",
    } <= set(offline_runner._SOURCE_FILES)

    repo = tmp_path / "repo"
    repo.mkdir()
    for relative in offline_runner._SOURCE_FILES:
        path = repo / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"bound source: {relative}\n", encoding="utf-8")
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(["git", "add", "."], cwd=repo, check=True)
    subprocess.run(
        [
            "git",
            "-c",
            "user.name=offline-test",
            "-c",
            "user.email=offline@example.invalid",
            "-c",
            "commit.gpgsign=false",
            "commit",
            "-qm",
            "bound source fixture",
        ],
        cwd=repo,
        check=True,
    )
    monkeypatch.setattr(offline_runner, "REPO_ROOT", repo)

    clean = offline_runner._git_source_state()
    assert clean["dirty"] is False
    assert clean["worktree_dirty"] is False

    (repo / "unrelated-user-file.txt").write_text("user owned\n", encoding="utf-8")
    unrelated_dirty = offline_runner._git_source_state()
    assert unrelated_dirty["dirty"] is False
    assert unrelated_dirty["worktree_dirty"] is True

    real_run = subprocess.run

    def fail_global_status(command: list[str], *args: object, **kwargs: object) -> subprocess.CompletedProcess[str]:
        if command[:2] == ["git", "status"]:
            raise subprocess.TimeoutExpired(command, 30)
        return real_run(command, *args, **kwargs)

    with monkeypatch.context() as status_patch:
        status_patch.setattr(offline_runner.subprocess, "run", fail_global_status)
        unknown_global_state = offline_runner._git_source_state()
    assert unknown_global_state["dirty"] is False
    assert unknown_global_state["worktree_dirty"] is None

    bound_path = repo / offline_runner._SOURCE_FILES[-1]
    bound_path.write_text("changed production source\n", encoding="utf-8")
    bound_dirty = offline_runner._git_source_state()
    assert bound_dirty["dirty"] is True
    assert bound_dirty["worktree_dirty"] is True


def test_source_event_type_replay_preserves_deidentified_distribution() -> None:
    shape = {
        "available": True,
        "events": 4,
        "event_type_distribution": [
            {"type_hash": "type_a", "count": 1},
            {"type_hash": "type_b", "count": 3},
        ],
    }

    assert [_replayed_event_type(shape, index) for index in range(8)] == [
        "replay_type_a",
        "replay_type_b",
        "replay_type_b",
        "replay_type_b",
        "replay_type_a",
        "replay_type_b",
        "replay_type_b",
        "replay_type_b",
    ]


def test_performance_baseline_requires_auditable_campaign_evidence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    campaign = tmp_path / "candidate"
    campaign.mkdir()
    metrics_path = campaign / "metrics.json"
    source_files = _source_manifest()
    (campaign / "manifest.json").write_text(
        json.dumps(
            {
                "campaign_kind": "offline_memory_v6",
                "release_shape": True,
                "seed": DEFAULT_SEED,
                "requested_events": DEFAULT_EVENTS,
                "selected_events": DEFAULT_EVENTS,
                "migration_events": 10_000,
                "only_case": None,
                "dry_run": False,
                "source_replay_default_local": True,
                "source_shape_exact": True,
                "source_files": source_files,
                "source_git_commit": "a" * 40,
                "source_git_dirty": False,
            }
        ),
        encoding="utf-8",
    )
    metrics_path.write_text(
        json.dumps(
            {
                "status": "baseline_candidate_passed",
                "selected_events": DEFAULT_EVENTS,
                "semantic_failures": 0,
                "append": {"duration_seconds": 50.0},
                "bytes_per_event": 2_000.0,
                "source_files": source_files,
                "privacy": {"ok": True},
                "migration": {"ok": True},
                "gates": {
                    **{name: True for name in _REQUIRED_CORE_GATES},
                    "baseline_regression": False,
                },
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        offline_runner,
        "_source_manifest_at_commit",
        lambda _commit: source_files,
    )

    baseline = _load_baseline_metrics(metrics_path)

    assert baseline["valid"] is False
    assert baseline["evidence_audit"] == "AUDIT_FAIL"
    assert baseline["probe_status"] == "NOT_RUN"
    assert baseline["metrics_path"] == str(metrics_path.resolve())
    assert baseline["reported_append_seconds_per_event"] == 0.0005
    assert baseline["append_seconds_per_event"] == 0
    assert baseline["reported_bytes_per_event"] == 2_000.0
    assert baseline["bytes_per_event"] == 0

    manifest = json.loads((campaign / "manifest.json").read_text(encoding="utf-8"))
    manifest["release_shape"] = False
    (campaign / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    assert _load_baseline_metrics(metrics_path)["valid"] is False


def test_independent_baseline_probe_executes_the_fixed_workload(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(offline_runner, "DEFAULT_EVENTS", 100)

    probe = offline_runner._run_independent_baseline_probe(None)

    assert probe["ok"] is True
    assert probe["status"] == "PROBE_PASS"
    assert probe["events"] == 100
    assert probe["append_duration_seconds"] > 0
    assert probe["wall_duration_seconds"] >= probe["append_duration_seconds"]


def test_baseline_refresh_rejects_non_reproducible_probe_latency() -> None:
    stored = {
        "valid": True,
        "status": "accepted",
        "probe_events": DEFAULT_EVENTS,
        "probe_append_duration_seconds": 100.0,
        "probe_wall_duration_seconds": 120.0,
        "append_seconds_per_event": 0.001,
        "bytes_per_event": 2_000.0,
    }
    close = {
        **stored,
        "probe_append_duration_seconds": 110.0,
        "probe_wall_duration_seconds": 130.0,
        "append_seconds_per_event": 0.0011,
    }
    inflated = {
        **stored,
        "probe_append_duration_seconds": 200.0,
        "probe_wall_duration_seconds": 220.0,
        "append_seconds_per_event": 0.002,
    }

    assert offline_runner._baseline_refresh_matches(stored, close) is True
    assert offline_runner._baseline_refresh_matches(stored, inflated) is False


def test_baseline_validation_wall_time_is_not_charged_to_candidate_budget(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FakeClock:
        def __init__(self) -> None:
            self._lock = threading.Lock()
            self._value = 0.0

        def perf_counter(self) -> float:
            with self._lock:
                self._value += 0.001
                return self._value

        def advance(self, seconds: float) -> None:
            with self._lock:
                self._value += seconds

    clock = FakeClock()

    baseline_path = tmp_path / "accepted-baseline" / "metrics.json"
    accepted_baseline: dict[str, object] = {
        "valid": True,
        "status": "accepted",
        "evidence_audit": "AUDIT_PASS",
        "probe_status": "PROBE_PASS",
        "probe_events": 100,
        "probe_append_duration_seconds": 100.0,
        "probe_wall_duration_seconds": 120.0,
        "probe_performance_artifact_bytes": 100_000,
        "metrics_path": str(baseline_path),
        "metrics_sha256": "a" * 64,
        "manifest_sha256": "b" * 64,
        "reported_append_seconds_per_event": 1.0,
        "reported_bytes_per_event": 1_000_000_000.0,
        "append_seconds_per_event": 1.0,
        "bytes_per_event": 1_000_000_000.0,
    }

    def slow_accepted_baseline(_path: Path | None) -> dict[str, object]:
        clock.advance(1_801.0)
        return dict(accepted_baseline)

    monkeypatch.setattr(offline_runner, "time", clock)
    monkeypatch.setattr(offline_runner, "_load_baseline_metrics", slow_accepted_baseline)
    monkeypatch.setattr(offline_runner, "_timing_evidence_ok", lambda *_args, **_kwargs: True)

    campaign_dir = run_campaign(
        events=100,
        seed=DEFAULT_SEED,
        output_root=tmp_path,
        campaign_id="offline-baseline-timing-contract",
        only_case=None,
        migration_events=100,
        baseline_metrics=baseline_path,
    )

    metrics = json.loads((campaign_dir / "metrics.json").read_text(encoding="utf-8"))
    total = float(metrics["duration_seconds"])
    candidate = float(metrics["candidate_duration_seconds"])
    baseline_validation = float(metrics["baseline_validation_duration_seconds"])

    assert total > offline_runner._MAX_DURATION_SECONDS
    assert baseline_validation > offline_runner._MAX_DURATION_SECONDS
    assert 0 < candidate < offline_runner._MAX_DURATION_SECONDS
    assert math.isclose(total, candidate + baseline_validation, rel_tol=1e-12, abs_tol=1e-9)
    assert metrics["gates"]["duration"] is True
    assert metrics["gates"]["wall_timing_evidence"] is True
    assert metrics["status"] == "smoke_passed"
    manifest = json.loads((campaign_dir / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["baseline_validation_requested"] is True
    assert manifest["performance_baseline"]["valid"] is True
    assert audit_campaign(campaign_dir)["ok"] is True


def test_wall_timing_evidence_covers_the_baseline_probe() -> None:
    assert offline_runner._wall_timing_evidence_ok(
        total_duration_seconds=220.0,
        candidate_duration_seconds=100.0,
        baseline_validation_duration_seconds=120.0,
        baseline_probe_wall_duration_seconds=120.0,
        baseline_validation_expected=True,
    )
    assert not offline_runner._wall_timing_evidence_ok(
        total_duration_seconds=219.0,
        candidate_duration_seconds=100.0,
        baseline_validation_duration_seconds=119.0,
        baseline_probe_wall_duration_seconds=120.0,
        baseline_validation_expected=True,
    )
    assert not offline_runner._wall_timing_evidence_ok(
        total_duration_seconds=221.0,
        candidate_duration_seconds=100.0,
        baseline_validation_duration_seconds=120.0,
        baseline_probe_wall_duration_seconds=120.0,
        baseline_validation_expected=True,
    )
    assert not offline_runner._wall_timing_evidence_ok(
        total_duration_seconds=2_000.0,
        candidate_duration_seconds=100.0,
        baseline_validation_duration_seconds=1_900.0,
        baseline_probe_wall_duration_seconds=0.0,
        baseline_validation_expected=False,
    )


def test_final_artifact_scan_is_inside_the_candidate_duration_gate(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FakeClock:
        def __init__(self) -> None:
            self._lock = threading.Lock()
            self._value = 0.0

        def perf_counter(self) -> float:
            with self._lock:
                self._value += 0.001
                return self._value

        def advance(self, seconds: float) -> None:
            with self._lock:
                self._value += seconds

    clock = FakeClock()
    original_privacy_scan = offline_runner._privacy_scan
    delayed = False
    final_scan_names: set[str] = set()

    def slow_final_privacy_scan(paths: list[Path]) -> list[dict[str, str]]:
        nonlocal delayed, final_scan_names
        names = {path.name for path in paths}
        if not delayed and {"metrics.json", "report.md"} <= names:
            delayed = True
            final_scan_names = names
            clock.advance(1_801.0)
        return original_privacy_scan(paths)

    monkeypatch.setattr(offline_runner, "time", clock)
    monkeypatch.setattr(offline_runner, "_privacy_scan", slow_final_privacy_scan)
    monkeypatch.setattr(offline_runner, "_timing_evidence_ok", lambda *_args, **_kwargs: True)

    campaign_dir = run_campaign(
        events=100,
        seed=DEFAULT_SEED,
        output_root=tmp_path,
        campaign_id="offline-final-scan-timing-contract",
        only_case=None,
        migration_events=100,
    )

    metrics = json.loads((campaign_dir / "metrics.json").read_text(encoding="utf-8"))

    assert metrics["candidate_duration_seconds"] > offline_runner._MAX_DURATION_SECONDS
    assert final_scan_names == {"metrics.json", "privacy_audit.json", "report.md"}
    assert metrics["gates"]["duration"] is False
    assert metrics["status"] == "smoke_failed"
    audit = audit_campaign(campaign_dir)
    assert audit["ok"] is False
    assert "duration_gate_failed" in audit["issues"]


def test_final_privacy_scan_does_not_erase_an_earlier_wal_hit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original_privacy_scan = offline_runner._privacy_scan
    injected = False

    def inject_first_wal_hit(paths: list[Path]) -> list[dict[str, str]]:
        nonlocal injected
        hits = original_privacy_scan(paths)
        if not injected and any(path.name == "self_learning.db-wal" for path in paths):
            injected = True
            return [*hits, {"path": "self_learning.db-wal", "pattern": "injected-test-hit"}]
        return hits

    monkeypatch.setattr(offline_runner, "_privacy_scan", inject_first_wal_hit)

    campaign_dir = run_campaign(
        events=100,
        seed=DEFAULT_SEED,
        output_root=tmp_path,
        campaign_id="offline-earlier-privacy-hit-contract",
        only_case=None,
        migration_events=100,
    )

    metrics = json.loads((campaign_dir / "metrics.json").read_text(encoding="utf-8"))
    privacy = json.loads((campaign_dir / "privacy_audit.json").read_text(encoding="utf-8"))

    assert injected is True
    assert metrics["status"] == "smoke_failed"
    assert metrics["gates"]["privacy"] is False
    assert metrics["privacy"]["raw_hit_count"] >= 1
    assert {"path": "self_learning.db-wal", "pattern": "injected-test-hit"} in privacy["raw_sensitive_hits"]


def test_baseline_probe_rejects_an_untrusted_commit_driver(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = tmp_path / "repo"
    script = repo / "applications" / "memory_feature_validation" / "scripts" / "run_offline_memory_campaign.py"
    script.parent.mkdir(parents=True)
    script.write_text(
        "import json\n"
        "print(json.dumps({'ok': True, 'status': 'PROBE_PASS', 'events': 100000, "
        "'duration_seconds': 10.0, 'performance_artifact_bytes': 200000, "
        "'bytes_per_event': 2.0}))\n",
        encoding="utf-8",
    )
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    subprocess.run(["git", "add", "."], cwd=repo, check=True)
    subprocess.run(
        [
            "git",
            "-c",
            "user.name=offline-test",
            "-c",
            "user.email=offline@example.invalid",
            "-c",
            "commit.gpgsign=false",
            "commit",
            "-qm",
            "probe fixture",
        ],
        cwd=repo,
        check=True,
    )
    commit = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    monkeypatch.setattr(offline_runner, "REPO_ROOT", repo)

    probe = offline_runner._run_baseline_probe_at_commit(
        commit,
        {"available": False},
    )

    assert probe == {
        "ok": False,
        "status": "PROBE_FAIL",
        "error": "baseline_driver_mismatch",
    }


def test_source_replay_reads_only_deidentified_shape(tmp_path: Path) -> None:
    source = tmp_path / "source.db"
    raw = "SOURCE_PRIVATE_BODY_MUST_NOT_APPEAR"
    with sqlite3.connect(source) as conn:
        conn.executescript(
            "CREATE TABLE schema_version(version INTEGER); INSERT INTO schema_version VALUES(5);"
            "CREATE TABLE runs(run_id TEXT); INSERT INTO runs VALUES('run');"
            "CREATE TABLE events(content_text TEXT,event_type TEXT);"
        )
        conn.execute("INSERT INTO events VALUES(?, 'tool_result')", (raw,))

    shape = _source_shape(source)

    assert shape["available"] is True
    assert shape["mode"] == "ro_immutable"
    assert shape["runs"] == 1 and shape["events"] == 1
    assert shape["content_selected"] is False
    assert raw not in json.dumps(shape)


def test_privacy_scan_decompresses_gzip_artifacts(tmp_path: Path) -> None:
    artifact = tmp_path / "cases.jsonl.gz"
    with gzip.open(artifact, "wt", encoding="utf-8") as handle:
        handle.write('{"leak":"MVSECRET_hidden"}\n')

    assert _privacy_scan([artifact]) == [{"path": "cases.jsonl.gz", "pattern": "sensitive-pattern-0"}]


def test_privacy_scan_fails_closed_on_invalid_gzip(tmp_path: Path) -> None:
    artifact = tmp_path / "cases.jsonl.gz"
    artifact.write_bytes(b"not-a-gzip-stream")

    assert _privacy_scan([artifact]) == [{"path": "cases.jsonl.gz", "pattern": "scan-error"}]


def test_100_event_smoke_uses_real_v6_apis_and_is_not_a_release_pass(
    tmp_path: Path,
) -> None:
    campaign_dir = run_campaign(
        events=100,
        seed=DEFAULT_SEED,
        output_root=tmp_path,
        campaign_id="offline-contract-smoke",
        only_case=None,
        migration_events=100,
    )

    metrics = json.loads((campaign_dir / "metrics.json").read_text(encoding="utf-8"))
    privacy = json.loads((campaign_dir / "privacy_audit.json").read_text(encoding="utf-8"))
    manifest = json.loads((campaign_dir / "manifest.json").read_text(encoding="utf-8"))
    with sqlite3.connect(campaign_dir / "self_learning.db") as conn:
        event_count = int(conn.execute("SELECT COUNT(*) FROM events").fetchone()[0])
        integrity = str(conn.execute("PRAGMA integrity_check").fetchone()[0])
    with gzip.open(campaign_dir / "cases.jsonl.gz", "rt", encoding="utf-8") as handle:
        case_rows = [json.loads(line) for line in handle if line.strip()]

    assert manifest["release_eligible"] is False
    assert manifest["baseline_validation_requested"] is False
    assert metrics["status"] == "smoke_passed"
    assert metrics["semantic_failures"] == 0
    assert metrics["semantic_audit"]["ok"] is True
    assert metrics["memory"]["persistent_failures"] == 0
    assert metrics["security"]["structured_path_failures"] == 0
    assert metrics["root_isolation"]["concurrent_read_workers"] == 4
    assert metrics["migration"]["ok"] is True
    assert metrics["candidate_duration_seconds"] > 0
    assert metrics["baseline_validation_duration_seconds"] == 0
    assert math.isclose(
        metrics["duration_seconds"],
        metrics["candidate_duration_seconds"] + metrics["baseline_validation_duration_seconds"],
        rel_tol=1e-12,
        abs_tol=1e-9,
    )
    assert metrics["gates"]["wall_timing_evidence"] is True
    assert privacy["ok"] is True
    assert privacy["raw_sensitive_hits"] == []
    assert event_count == 100
    assert integrity == "ok"
    assert len(case_rows) == 100
    assert all("marker" not in row and "payload" not in row for row in case_rows)
    assert (campaign_dir / "report.md").is_file()
    assert (campaign_dir / "reproduction_commands.json").is_file()

    clean_audit = audit_campaign(campaign_dir)
    assert clean_audit["ok"] is True
    assert clean_audit["semantic_oracle"]["ok"] is True

    manifest_path = campaign_dir / "manifest.json"
    original_manifest = manifest_path.read_text(encoding="utf-8")
    for field, value, issue in (
        ("schema_version", 99, "manifest_schema_version_invalid"),
        ("campaign_kind", "offline_memory_v5", "campaign_kind_invalid"),
    ):
        tampered_manifest = json.loads(original_manifest)
        tampered_manifest[field] = value
        manifest_path.write_text(json.dumps(tampered_manifest), encoding="utf-8")
        manifest_audit = audit_campaign(campaign_dir)
        assert manifest_audit["ok"] is False
        assert issue in manifest_audit["issues"]
        manifest_path.write_text(original_manifest, encoding="utf-8")

    metrics_path = campaign_dir / "metrics.json"
    original_metrics = metrics_path.read_text(encoding="utf-8")
    tampered_metrics = json.loads(original_metrics)
    tampered_metrics["baseline_comparison"]["max_ratio"] = 9.0
    metrics_path.write_text(json.dumps(tampered_metrics), encoding="utf-8")
    ratio_audit = audit_campaign(campaign_dir)
    assert ratio_audit["ok"] is False
    assert "baseline_threshold_mismatch" in ratio_audit["issues"]
    metrics_path.write_text(original_metrics, encoding="utf-8")

    tampered_metrics = json.loads(original_metrics)
    tampered_metrics["append"]["duration_seconds"] = tampered_metrics["candidate_duration_seconds"] * 2
    metrics_path.write_text(json.dumps(tampered_metrics), encoding="utf-8")
    timing_audit = audit_campaign(campaign_dir)
    assert timing_audit["ok"] is False
    assert "stored_gate_mismatch:timing_evidence" in timing_audit["issues"]
    metrics_path.write_text(original_metrics, encoding="utf-8")

    tampered_metrics = json.loads(original_metrics)
    tampered_metrics["baseline_validation_duration_seconds"] += 1
    metrics_path.write_text(json.dumps(tampered_metrics), encoding="utf-8")
    wall_timing_audit = audit_campaign(campaign_dir)
    assert wall_timing_audit["ok"] is False
    assert "stored_gate_mismatch:wall_timing_evidence" in wall_timing_audit["issues"]
    metrics_path.write_text(original_metrics, encoding="utf-8")

    tampered_metrics = json.loads(original_metrics)
    tampered_metrics["duration_seconds"] = 2_000
    tampered_metrics["candidate_duration_seconds"] = 100
    tampered_metrics["baseline_validation_duration_seconds"] = 1_900
    metrics_path.write_text(json.dumps(tampered_metrics), encoding="utf-8")
    coupled_timing_audit = audit_campaign(campaign_dir)
    assert coupled_timing_audit["ok"] is False
    assert "stored_gate_mismatch:wall_timing_evidence" in coupled_timing_audit["issues"]
    metrics_path.write_text(original_metrics, encoding="utf-8")

    tampered_manifest = json.loads(original_manifest)
    tampered_manifest["baseline_validation_requested"] = True
    manifest_path.write_text(json.dumps(tampered_manifest), encoding="utf-8")
    metrics_path.write_text(json.dumps(tampered_metrics), encoding="utf-8")
    manifest_coupled_timing_audit = audit_campaign(campaign_dir)
    assert manifest_coupled_timing_audit["ok"] is False
    assert "stored_gate_mismatch:wall_timing_evidence" in manifest_coupled_timing_audit["issues"]
    manifest_path.write_text(original_manifest, encoding="utf-8")
    metrics_path.write_text(original_metrics, encoding="utf-8")

    tampered_metrics = json.loads(original_metrics)
    tampered_metrics["performance_artifact_bytes"] += 1
    tampered_metrics["bytes_per_event"] = (
        tampered_metrics["performance_artifact_bytes"] / tampered_metrics["selected_events"]
    )
    metrics_path.write_text(json.dumps(tampered_metrics), encoding="utf-8")
    bytes_audit = audit_campaign(campaign_dir)
    assert bytes_audit["ok"] is False
    assert "stored_gate_mismatch:performance_artifact_bytes_exact" in bytes_audit["issues"]
    metrics_path.write_text(original_metrics, encoding="utf-8")

    central_db = campaign_dir / "self_learning.db"
    with sqlite3.connect(central_db) as conn:
        active_id, activation_source = conn.execute(
            "SELECT id,activation_source FROM memory_items "
            "WHERE state='active_confirmed' ORDER BY id LIMIT 1"
        ).fetchone()
        conn.execute(
            "UPDATE memory_items SET state='active_unreviewed' WHERE id=?",
            (active_id,),
        )
        conn.commit()
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    unreviewed_audit = audit_campaign(campaign_dir)
    assert unreviewed_audit["ok"] is False
    assert unreviewed_audit["semantic_oracle"]["failure_codes"] == {
        "active_pending_memory:persistent_state": 1
    }
    with sqlite3.connect(central_db) as conn:
        conn.execute(
            "UPDATE memory_items SET state='active_confirmed' WHERE id=?",
            (active_id,),
        )
        conn.execute(
            "UPDATE memory_items SET activation_source=? WHERE id=?",
            ("auto" if activation_source != "auto" else "admin", active_id),
        )
        conn.commit()
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    activation_audit = audit_campaign(campaign_dir)
    assert activation_audit["ok"] is False
    assert activation_audit["semantic_oracle"]["failure_codes"] == {
        "active_pending_memory:persistent_state": 1
    }
    with sqlite3.connect(central_db) as conn:
        conn.execute(
            "UPDATE memory_items SET activation_source=? WHERE id=?",
            (activation_source, active_id),
        )
        conn.commit()
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")

    migration_db = campaign_dir / "migration_v4_to_v6.db"
    with sqlite3.connect(migration_db) as conn:
        migrated_id = int(
            conn.execute(
                "SELECT id FROM memory_items WHERE state='active_confirmed' "
                "ORDER BY id LIMIT 1"
            ).fetchone()[0]
        )
        conn.execute(
            "UPDATE memory_items SET state='active_unreviewed' WHERE id=?",
            (migrated_id,),
        )
        conn.commit()
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    migration_state_audit = audit_campaign(campaign_dir)
    assert migration_state_audit["ok"] is False
    assert "migration_audit_failed" in migration_state_audit["issues"]
    with sqlite3.connect(migration_db) as conn:
        conn.execute(
            "UPDATE memory_items SET state='active_confirmed',"
            "activation_source='admin' WHERE id=?",
            (migrated_id,),
        )
        conn.commit()
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    migration_source_audit = audit_campaign(campaign_dir)
    assert migration_source_audit["ok"] is False
    assert "migration_audit_failed" in migration_source_audit["issues"]
    with sqlite3.connect(migration_db) as conn:
        conn.execute(
            "UPDATE memory_items SET activation_source='migration' WHERE id=?",
            (migrated_id,),
        )
        conn.commit()
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")

    with sqlite3.connect(central_db) as conn:
        short_secret = str(
            conn.execute("SELECT input_json FROM events WHERE event_id='security-event-000001'").fetchone()[0]
        )
        nested_secret = str(
            conn.execute("SELECT input_json FROM events WHERE event_id='security-event-000002'").fetchone()[0]
        )
    assert "𐍈" not in short_secret and "[REDACTED]" in short_secret
    assert "value with spaces" not in nested_secret and "[REDACTED]" in nested_secret

    with sqlite3.connect(central_db) as conn:
        conn.execute("UPDATE events SET event_type='wrong_type' WHERE event_id='ledger-event-000000'")
        conn.commit()
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    replay_audit = audit_campaign(campaign_dir)
    assert replay_audit["ok"] is False
    assert replay_audit["semantic_oracle"]["failure_codes"] == {"ledger_fts_search_scroll:event_type_replay": 1}

    with sqlite3.connect(central_db) as conn:
        conn.execute("UPDATE events SET event_type='task' WHERE event_id='ledger-event-000000'")
        conn.commit()
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")

    with sqlite3.connect(central_db) as conn:
        conn.execute("UPDATE events SET content_text='tampered' WHERE event_id='security-event-000006'")
        conn.commit()
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    semantic_audit = audit_campaign(campaign_dir)
    assert semantic_audit["ok"] is False
    assert "semantic_oracle_audit_failed" in semantic_audit["issues"]

    (campaign_dir / "migration_v4_to_v6.db").unlink()
    assert audit_campaign(campaign_dir)["ok"] is False


def test_category_weights_are_simple_percentages() -> None:
    assert sum(CATEGORY_WEIGHTS.values()) == 100
    assert all(weight > 0 for weight in CATEGORY_WEIGHTS.values())
    assert sum(allocate_quotas(17).values()) == 17
