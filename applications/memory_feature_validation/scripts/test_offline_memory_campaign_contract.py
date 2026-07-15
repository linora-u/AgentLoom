"""Contract tests for the deterministic v5 offline memory campaign."""

from __future__ import annotations

import ast
import gzip
import inspect
import json
import sqlite3
import subprocess
import sys
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
    campaign_ids = {_default_campaign_id("offline-v5") for _ in range(64)}

    assert len(campaign_ids) == 64
    assert all(Path(value).name == value for value in campaign_ids)


def test_release_plan_is_exactly_100k_v5_events_with_fixed_seed() -> None:
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
    cases = [
        case
        for case in build_case_plan(5_000, DEFAULT_SEED)
        if case.category == "active_pending_memory"
    ]
    failures: list[dict[str, object]] = []

    metrics = offline_runner._validate_memory_cases(
        tmp_path / "self_learning.db",
        cases,
        failures,
    )

    assert len(cases) == 500
    assert metrics["persistent_failures"] == 0
    assert failures == []


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


def test_every_v5_category_gets_the_full_payload_profile() -> None:
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
                "campaign_kind": "offline_memory_v5",
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
    assert probe["duration_seconds"] > 0


def test_baseline_refresh_rejects_non_reproducible_probe_latency() -> None:
    stored = {
        "valid": True,
        "status": "accepted",
        "probe_events": DEFAULT_EVENTS,
        "probe_duration_seconds": 100.0,
        "append_seconds_per_event": 0.001,
        "bytes_per_event": 2_000.0,
    }
    close = {
        **stored,
        "probe_duration_seconds": 110.0,
        "append_seconds_per_event": 0.0011,
    }
    inflated = {
        **stored,
        "probe_duration_seconds": 200.0,
        "append_seconds_per_event": 0.002,
    }

    assert offline_runner._baseline_refresh_matches(stored, close) is True
    assert offline_runner._baseline_refresh_matches(stored, inflated) is False


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


def test_100_event_smoke_uses_real_v5_apis_and_is_not_a_release_pass(
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
    assert metrics["status"] == "smoke_passed"
    assert metrics["semantic_failures"] == 0
    assert metrics["semantic_audit"]["ok"] is True
    assert metrics["memory"]["persistent_failures"] == 0
    assert metrics["security"]["structured_path_failures"] == 0
    assert metrics["root_isolation"]["concurrent_read_workers"] == 4
    assert metrics["migration"]["ok"] is True
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
    tampered_metrics["append"]["duration_seconds"] = tampered_metrics["duration_seconds"] * 2
    metrics_path.write_text(json.dumps(tampered_metrics), encoding="utf-8")
    timing_audit = audit_campaign(campaign_dir)
    assert timing_audit["ok"] is False
    assert "stored_gate_mismatch:timing_evidence" in timing_audit["issues"]
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

    with sqlite3.connect(campaign_dir / "self_learning.db") as conn:
        short_secret = str(
            conn.execute("SELECT input_json FROM events WHERE event_id='security-event-000001'").fetchone()[0]
        )
        nested_secret = str(
            conn.execute("SELECT input_json FROM events WHERE event_id='security-event-000002'").fetchone()[0]
        )
    assert "𐍈" not in short_secret and "[REDACTED]" in short_secret
    assert "value with spaces" not in nested_secret and "[REDACTED]" in nested_secret

    with sqlite3.connect(campaign_dir / "self_learning.db") as conn:
        conn.execute("UPDATE events SET event_type='wrong_type' WHERE event_id='ledger-event-000000'")
        conn.commit()
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    replay_audit = audit_campaign(campaign_dir)
    assert replay_audit["ok"] is False
    assert replay_audit["semantic_oracle"]["failure_codes"] == {"ledger_fts_search_scroll:event_type_replay": 1}

    with sqlite3.connect(campaign_dir / "self_learning.db") as conn:
        conn.execute("UPDATE events SET event_type='task' WHERE event_id='ledger-event-000000'")
        conn.commit()
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")

    with sqlite3.connect(campaign_dir / "self_learning.db") as conn:
        conn.execute("UPDATE events SET content_text='tampered' WHERE event_id='security-event-000006'")
        conn.commit()
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    semantic_audit = audit_campaign(campaign_dir)
    assert semantic_audit["ok"] is False
    assert "semantic_oracle_audit_failed" in semantic_audit["issues"]

    (campaign_dir / "migration_v4_to_v5.db").unlink()
    assert audit_campaign(campaign_dir)["ok"] is False


def test_category_weights_are_simple_percentages() -> None:
    assert sum(CATEGORY_WEIGHTS.values()) == 100
    assert all(weight > 0 for weight in CATEGORY_WEIGHTS.values())
    assert sum(allocate_quotas(17).values()) == 17
