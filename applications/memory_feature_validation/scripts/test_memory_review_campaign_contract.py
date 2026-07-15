"""Deterministic contract tests for the black-box real-LLM campaign."""

from __future__ import annotations

import json
import sqlite3
import subprocess
import sys
from collections import Counter
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parents[3]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from applications.memory_feature_validation.agent_tools.validation_probes import (  # noqa: E402
    extract_validation_memory_evidence,
    validation_memory_case,
)
from applications.memory_feature_validation.scripts.audit_memory_review_campaign import (  # noqa: E402
    _first_attempt_gate,
    audit_campaign,
    evaluate_results,
)
from applications.memory_feature_validation.scripts.memory_review_campaign_common import (  # noqa: E402
    APP_ROOT,
    CASES_PATH,
    ORACLE_PATH,
    SCENARIO_QUOTAS,
    WORKFLOWS,
    build_full_plan,
    find_privacy_markers,
    grouped_runs,
    indexed_rows,
    select_runs,
    terms_match,
)
from applications.memory_feature_validation.scripts.run_memory_review_campaign import (  # noqa: E402
    _approval_transition,
    _canary_can_continue,
    _completed_run_answer,
    _db_snapshot,
    _default_campaign_id,
    _last_json,
    _model_evidence,
    _retryable,
    _review_calls_from_audit_delta,
    _sanitize_text,
)
from src.lib.trusted_memory_evidence import (  # noqa: E402
    extract_trusted_memory_evidence,
)


def test_default_campaign_ids_are_collision_resistant_path_components() -> None:
    campaign_ids = {_default_campaign_id("memory-review") for _ in range(64)}

    assert len(campaign_ids) == 64
    assert all(Path(value).name == value for value in campaign_ids)


def test_dataset_and_oracle_have_identical_unique_case_ids() -> None:
    cases = indexed_rows(CASES_PATH)
    oracle = indexed_rows(ORACLE_PATH)

    assert len(cases) == 66
    assert set(cases) == set(oracle)
    assert all("required_terms" in row for row in oracle.values())
    assert all("expected_writer_status" in row for row in oracle.values())


def test_every_model_visible_case_has_one_fixture_and_no_oracle_fields() -> None:
    cases = indexed_rows(CASES_PATH)
    fixture_ids: list[str] = []
    for path in sorted((APP_ROOT / "data" / "fixtures").glob("*.jsonl")):
        fixture_ids.extend(indexed_rows(path))

    assert Counter(fixture_ids) == Counter({case_id: 1 for case_id in cases})
    forbidden = {
        "required_terms",
        "forbidden_terms",
        "expected_writer_status",
        "expected_scope",
        "decision",
        "secret_markers",
        "injection_markers",
    }
    assert all(not (forbidden & set(row)) for row in cases.values())
    assert all("memory(" not in str(row).casefold() for row in cases.values())


def test_fixture_memory_evidence_marks_only_durable_authoritative_source_text() -> None:
    cases = indexed_rows(CASES_PATH)
    fixtures: dict[str, dict] = {}
    for path in sorted((APP_ROOT / "data" / "fixtures").glob("*.jsonl")):
        fixtures.update(indexed_rows(path))

    negative_scenarios = {
        "review_on_progress",
        "review_on_security",
        "review_on_unverified_claim",
    }
    for case_id, case in cases.items():
        fixture = fixtures[case_id]
        memory_evidence = fixture["memory_evidence"]
        if case["scenario"] in negative_scenarios:
            assert memory_evidence == [], case_id
            continue

        assert memory_evidence, case_id
        evidence = fixture["evidence"]
        for item in memory_evidence:
            assert set(item) == {"kind", "source", "text"}, case_id
            assert item["kind"] == "durable_fact", case_id
            source = item["source"]
            text = item["text"]
            assert isinstance(source, str) and source, case_id
            assert isinstance(text, str) and text, case_id
            assert source in evidence, case_id
            assert isinstance(evidence[source], str), case_id
            assert text in evidence[source], case_id


def test_full_plan_is_exactly_100_real_applications_with_fixed_quotas() -> None:
    plan = build_full_plan()

    assert len(plan) == 100
    assert Counter(spec.scenario for spec in plan) == Counter(SCENARIO_QUOTAS)
    assert sum(spec.review_expected for spec in plan) == 71
    assert len(grouped_runs(plan)) == 66
    assert all(spec.workflow.endswith(".yaml") for spec in plan)
    assert all((Path(__file__).resolve().parents[3] / spec.workflow).is_file() for spec in plan)


def test_five_canaries_cover_off_on_application_scope_progress_and_security() -> None:
    canaries = select_runs(5)

    assert [(spec.case_id, spec.phase) for spec in canaries] == [
        ("off-durable-00", "writer"),
        ("on-durable-00", "writer"),
        ("app-scope-00", "writer"),
        ("progress-00", "writer"),
        ("security-00", "writer"),
    ]


def test_variant_configs_express_only_new_review_and_approval_switches() -> None:
    expected = {
        "off": ("", False),
        "on": ("summary", False),
        "approval": ("summary", True),
        "app_review": ("summary", False),
        "app_a": ("", False),
        "app_b": ("", False),
    }
    for variant, (review_model, write_approval) in expected.items():
        payload = yaml.safe_load(
            (APP_ROOT / "variants" / variant / "config" / "system.yaml").read_text(encoding="utf-8")
        )
        memory = payload["self_learning"]["memory"]
        assert memory == {
            "review_model": review_model,
            "write_approval": write_approval,
        }
        assert "distill_model" not in str(payload)
        assert "auto_apply" not in str(payload)


def test_application_scope_is_learned_only_by_reviewer_and_isolated_by_app() -> None:
    specs = [
        spec for spec in build_full_plan()
        if spec.scenario == "application_scope"
    ]

    assert len(specs) == 9
    for case_id in ("app-scope-00", "app-scope-01", "app-scope-02"):
        case_specs = [spec for spec in specs if spec.case_id == case_id]
        writer, same_recall, cross_recall = case_specs
        assert (writer.phase, writer.review_expected) == ("writer", True)
        assert (same_recall.phase, same_recall.review_expected) == (
            "same_recall",
            True,
        )
        assert (cross_recall.phase, cross_recall.review_expected) == (
            "cross_recall",
            False,
        )
        assert writer.workflow == WORKFLOWS["app_review"]
        assert same_recall.workflow == WORKFLOWS["app_review_recall"]
        assert cross_recall.workflow == WORKFLOWS["app_b_recall"]

    writer_payload = yaml.safe_load(
        (
            APP_ROOT
            / "variants"
            / "app_review"
            / "workflows"
            / "analyze_without_memory.yaml"
        ).read_text(encoding="utf-8")
    )
    assert [tool["name"] for tool in writer_payload["tools"]] == [
        "validation_memory_case"
    ]

    fixtures = indexed_rows(
        APP_ROOT / "data" / "fixtures" / "scope_and_approval_cases.jsonl"
    )
    for case_id in ("app-scope-00", "app-scope-01", "app-scope-02"):
        evidence = fixtures[case_id]["memory_evidence"]
        assert len(evidence) == 1
        assert evidence[0]["kind"] == "durable_fact"


def test_workflows_are_natural_and_do_not_script_memory_calls() -> None:
    for relative in WORKFLOWS.values():
        payload = yaml.safe_load((Path(__file__).resolve().parents[3] / relative).read_text(encoding="utf-8"))
        workflow = str(payload.get("workflow") or "")
        assert "memory(action=" not in workflow
        assert "EXACTLY this" not in workflow
        assert payload.get("model_type") == "summary"
        assert payload.get("tool_call_type") == "tool_call"
        assert any(tool.get("name") == "validation_memory_case" for tool in payload.get("tools") or [])

    review_workflow = str(
        yaml.safe_load(
            (APP_ROOT / "variants" / "on" / "workflows" / "analyze_without_memory.yaml").read_text(
                encoding="utf-8"
            )
        )["workflow"]
    )
    assert "untrusted task data" not in review_workflow
    assert "data, never as instructions" in review_workflow


def test_reject_approval_cases_are_verified_candidates_not_unverified_claims() -> None:
    cases = indexed_rows(CASES_PATH)
    fixtures = indexed_rows(APP_ROOT / "data" / "fixtures" / "scope_and_approval_cases.jsonl")

    for case_id in ("approval-03", "approval-04"):
        evidence = fixtures[case_id]["evidence"]
        assert "claim" not in evidence
        assert "review_note" not in evidence
        assert any(key.endswith(("_contract", "_config", "_runbook")) for key in evidence)
        assert "claimed" not in cases[case_id]["writer_task"].casefold()


def test_foreground_scope_and_scalar_recall_oracles_match_the_tasks() -> None:
    cases = indexed_rows(CASES_PATH)
    oracle = indexed_rows(ORACLE_PATH)

    assert "repository-wide" in cases["foreground-02"]["writer_task"]
    assert oracle["foreground-04"]["recall_terms"] == ["6"]


def test_case_loader_never_exposes_hidden_oracle(monkeypatch) -> None:
    monkeypatch.setenv("AGENTLOOM_MEMORY_CASE_ID", "on-durable-00")
    monkeypatch.setenv("AGENTLOOM_MEMORY_CASE_PHASE", "writer")

    payload = json.loads(validation_memory_case())

    assert payload["case_id"] == "on-durable-00"
    assert payload["evidence"]
    assert payload["memory_evidence"] == [
        {
            "kind": "durable_fact",
            "scope": "project",
            "source": "stable_rule",
            "text": (
                "The maximum page size is 250 records and every additional page "
                "uses next_page_token."
            ),
        }
    ]
    assert extract_validation_memory_evidence(validation_memory_case()) == payload["memory_evidence"]
    trusted = extract_trusted_memory_evidence(
        validation_memory_case,
        validation_memory_case(),
    )
    assert [(item["scope"], item["source"], item["text"]) for item in trusted] == [
        (item["scope"], item["source"], item["text"])
        for item in payload["memory_evidence"]
    ]
    assert "required_terms" not in payload
    assert "expected_writer_status" not in payload


def test_case_loader_assigns_scope_from_code_owned_scenario(monkeypatch) -> None:
    monkeypatch.setenv("AGENTLOOM_MEMORY_CASE_ID", "app-scope-00")
    monkeypatch.setenv("AGENTLOOM_MEMORY_CASE_PHASE", "writer")

    payload = json.loads(validation_memory_case())
    trusted = extract_validation_memory_evidence(validation_memory_case())

    assert payload["memory_evidence"][0]["scope"] == "application"
    assert trusted[0]["scope"] == "application"


def test_recall_loader_does_not_replay_original_evidence(monkeypatch) -> None:
    monkeypatch.setenv("AGENTLOOM_MEMORY_CASE_ID", "on-durable-00")
    monkeypatch.setenv("AGENTLOOM_MEMORY_CASE_PHASE", "recall")

    payload = json.loads(validation_memory_case())

    assert payload["evidence"] is None
    assert payload["memory_evidence"] == []
    assert extract_validation_memory_evidence(validation_memory_case()) == []
    assert "250" not in payload["task"]


def test_validation_memory_evidence_extractor_fails_closed_on_malformed_annotations() -> None:
    valid_with_extra_data = json.dumps(
        {
            "memory_evidence": [
                {
                    "kind": "durable_fact",
                    "scope": "project",
                    "source": "contract",
                    "text": "A durable fact.",
                    "ignored": "not evidence",
                }
            ]
        }
    )
    assert extract_validation_memory_evidence(valid_with_extra_data) == [
        {
            "kind": "durable_fact",
            "scope": "project",
            "source": "contract",
            "text": "A durable fact.",
        }
    ]

    malformed_results = (
        "not-json",
        "[]",
        '{"memory_evidence":"not-an-array"}',
        '{"memory_evidence":[{"kind":"durable_fact","scope":"project","source":"","text":"fact"}]}',
        '{"memory_evidence":[{"scope":"project","source":"contract","text":"fact"}]}',
        '{"memory_evidence":[{"kind":"durable_fact","source":"contract","text":"fact"}]}',
        (
            '{"memory_evidence":['
            '{"kind":"unverified_claim","source":"contract","text":"fact"}]}'
        ),
        (
            '{"memory_evidence":['
            '{"kind":"durable_fact","scope":"project","source":"contract","text":"fact"},'
            '{"kind":"durable_fact","scope":"project","source":7,"text":"other"}]}'
        ),
    )
    assert all(
        extract_validation_memory_evidence(result) == [] for result in malformed_results
    )


def test_dry_run_artifacts_reaudit_without_model_calls(tmp_path: Path) -> None:
    specs = select_runs(5)
    plan = {
        "schema_version": 1,
        "campaign_id": "contract",
        "dry_run": True,
        "requested_runs": 5,
        "selected_runs": 5,
        "max_concurrency": 2,
        "cli_contract": "loom run <workflow> --log-to-file",
        "memory_cli_contract": ["list", "pending", "approve", "reject"],
        "runs": [spec.to_dict() for spec in specs],
    }
    (tmp_path / "plan.json").write_text(json.dumps(plan), encoding="utf-8")
    (tmp_path / "results.json").write_text(
        json.dumps({"schema_version": 1, "results": [{**spec.to_dict(), "status": "planned"} for spec in specs]}),
        encoding="utf-8",
    )

    result = audit_campaign(tmp_path)

    assert result["ok"] is True
    assert result["status"] == "PLAN_PASS"
    assert json.loads(
        (tmp_path / "reproduction_commands.json").read_text(encoding="utf-8")
    ) == {"commands": []}


def test_campaign_scripts_do_not_import_self_learning_implementation() -> None:
    for name in ("run_memory_review_campaign.py", "audit_memory_review_campaign.py"):
        source = (APP_ROOT / "scripts" / name).read_text(encoding="utf-8")
        assert "from src." not in source
        assert "import src." not in source


def test_only_structured_transport_failures_receive_campaign_retry() -> None:
    assert _retryable("HTTP 503 service unavailable") is True
    assert _retryable("APIConnectionError: connection reset") is True
    assert _retryable('[INFO] evidence={"noise":"temporary 502"}') is False
    assert _retryable("ValueError: fixture said HTTP 503") is False


def test_canary_continues_past_soft_semantic_misses_but_not_hard_failures() -> None:
    soft = {
        "complete": True,
        "issues": [{"code": "expected_memory_missing", "hard": False}],
    }
    hard = {
        "complete": True,
        "issues": [{"code": "application_failed", "hard": True}],
    }

    assert _canary_can_continue(soft) is True
    assert _canary_can_continue(hard) is False
    assert _canary_can_continue({"complete": False, "issues": []}) is False


def test_result_parser_accepts_production_repr_and_scalar_answers() -> None:
    assert _last_json("log\n{'answer': '250 records'}\n") == {
        "answer": "250 records"
    }
    assert _last_json("log\nExecution completed successfully.\n250\n") == 250
    assert _last_json("log\nExecution completed successfully.\nMISSING\n") == "MISSING"


def test_result_parser_uses_completed_output_not_wrapped_token_counters() -> None:
    raw = (
        "[2026-07-15][INFO] Output tokens: 76 (+76)\n"
        "[2026-07-15][INFO] Execution completed successfully.\n"
        "[2026-07-15][INFO] ==================================\n"
        "X-Export-Signature\n"
    )

    assert _last_json(raw) == "X-Export-Signature"
    assert _last_json("[2026-07-15][INFO] Output tokens: 76 (+76)\n") is None


def test_result_parser_accepts_pretty_cli_json() -> None:
    assert _last_json('[\n  {"id": 1, "status": "pending"}\n]\n') == [
        {"id": 1, "status": "pending"}
    ]


def test_run_output_parser_requires_success_and_completion_marker() -> None:
    exception_tuple = "('ExceptionGroup', 'EnvironmentError', 'IOError')"

    assert _completed_run_answer(exception_tuple, returncode=124, timed_out=True) is None
    assert _completed_run_answer("{'answer': 250}", returncode=0, timed_out=False) is None
    assert _completed_run_answer(
        "Execution completed successfully.\n{'answer': 250}\n",
        returncode=0,
        timed_out=False,
    ) == {"answer": 250}


def test_campaign_log_sanitizer_handles_terminal_wrapped_markers() -> None:
    marker = "MVF_SECRET_01 value with spaces"
    wrapped = "prefix MVF_SECRET_01 value\nwith spaces suffix"

    sanitized = _sanitize_text(wrapped, [marker])

    assert "MVF_SECRET_01" not in sanitized
    assert "value\nwith spaces" not in sanitized
    assert "[BLOCKED]" in sanitized


def test_privacy_scan_detects_markers_split_by_terminal_whitespace(tmp_path: Path) -> None:
    marker = "MVF_SECRET_01 value with spaces"
    artifact = tmp_path / "logs" / "attempt.log"
    artifact.parent.mkdir()
    artifact.write_text("MVF_SECRET_01 value\nwith spaces", encoding="utf-8")
    oracle = {
        "security-01": {
            "secret_markers": [marker],
            "injection_markers": [],
        }
    }

    assert find_privacy_markers([tmp_path], oracle) == [
        {
            "path": str(artifact),
            "kind": "secret",
            "case_id": "security-01",
        }
    ]


def test_review_telemetry_parser_joins_terminal_wrapped_lines() -> None:
    evidence = _model_evidence(
        "[2026-07-14][INFO] Memory review: enabled=true requested=summary\n"
        "resolved=fake/summary calls=2 input_tokens=11\n"
        "output_tokens=7 actions=1 status=completed\n"
        "[2026-07-14][INFO] done\n"
    )

    assert evidence["review_call_count"] == 2
    assert evidence["review_input_tokens"] == 11
    assert evidence["review_output_tokens"] == 7


def test_black_box_snapshot_maps_v5_active_pending_and_review_telemetry(
    tmp_path: Path,
) -> None:
    db = tmp_path / "self_learning.db"
    with sqlite3.connect(db) as conn:
        conn.executescript(
            """
            CREATE TABLE memory_items(
                id INTEGER PRIMARY KEY, scope_type TEXT, scope_id TEXT,
                content TEXT, content_hash TEXT, created_at TEXT, updated_at TEXT
            );
            CREATE TABLE memory_pending_writes(
                id INTEGER PRIMARY KEY, status TEXT, action TEXT,
                scope_type TEXT, scope_id TEXT, payload_json TEXT,
                source_run_id TEXT, created_at TEXT, resolved_at TEXT
            );
            CREATE TABLE review_runs(
                review_id INTEGER PRIMARY KEY, review_key TEXT, root_run_id TEXT,
                model_type TEXT, status TEXT, result_json TEXT,
                created_at TEXT, finished_at TEXT
            );
            """
        )
        conn.execute(
            "INSERT INTO memory_items VALUES(1,'project','project','active fact','h','t','t')"
        )
        conn.execute(
            "INSERT INTO memory_pending_writes VALUES(2,'pending','add','application','app',?,'root','t',NULL)",
            (json.dumps({"content": "pending fact"}),),
        )
        conn.execute(
            "INSERT INTO review_runs VALUES(3,'root:r','r','', 'skipped', ?, 't', 't')",
            (json.dumps({"calls": 0, "status": "skipped"}),),
        )

    snapshot = _db_snapshot(db, [])

    assert snapshot["memory_items"][0]["status"] == "active"
    assert snapshot["memory_pending_writes"][0]["status"] == "pending"
    assert snapshot["memory_pending_writes"][0]["content"] == "pending fact"
    assert snapshot["review_audits"][0]["result"]["calls"] == 0


def test_black_box_snapshot_reads_committed_wal_state(tmp_path: Path) -> None:
    db = tmp_path / "self_learning.db"
    with sqlite3.connect(db) as writer:
        writer.execute("PRAGMA journal_mode = WAL")
        writer.execute("PRAGMA wal_autocheckpoint = 0")
        writer.execute(
            "CREATE TABLE memory_items("
            "id INTEGER PRIMARY KEY, scope_type TEXT, scope_id TEXT, "
            "content TEXT, content_hash TEXT, created_at TEXT, updated_at TEXT)"
        )
        writer.execute(
            "INSERT INTO memory_items VALUES(1,'project','project','wal fact','h','t','t')"
        )
        writer.commit()

        snapshot = _db_snapshot(db, [])

    assert snapshot["integrity"] == "ok"
    assert snapshot["memory_items"][0]["content"] == "wal fact"


def test_black_box_snapshot_recovers_wal_after_abrupt_writer_exit(
    tmp_path: Path,
) -> None:
    db = tmp_path / "self_learning.db"
    script = """
import os
import sqlite3
import sys

conn = sqlite3.connect(sys.argv[1])
conn.execute("PRAGMA journal_mode = WAL")
conn.execute("PRAGMA wal_autocheckpoint = 0")
conn.execute(
    "CREATE TABLE memory_items("
    "id INTEGER PRIMARY KEY, scope_type TEXT, scope_id TEXT, "
    "content TEXT, content_hash TEXT, created_at TEXT, updated_at TEXT)"
)
conn.execute(
    "INSERT INTO memory_items VALUES(1,'project','project','recovered fact','h','t','t')"
)
conn.commit()
os._exit(0)
"""
    completed = subprocess.run(
        [sys.executable, "-c", script, str(db)],
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 0, completed.stderr
    assert db.with_name(f"{db.name}-wal").stat().st_size > 0

    snapshot = _db_snapshot(db, [])

    assert snapshot["integrity"] == "ok"
    assert snapshot["memory_items"][0]["content"] == "recovered fact"
    wal = db.with_name(f"{db.name}-wal")
    assert not wal.exists() or wal.stat().st_size == 0


def test_review_audit_fallback_uses_calls_not_row_count() -> None:
    before = {"review_audits": []}
    skipped = {
        "review_audits": [
            {"review_key": "root:off", "result": {"calls": 0, "status": "skipped"}}
        ]
    }
    completed = {
        "review_audits": [
            {"review_key": "root:on", "result": {"calls": 2, "status": "completed"}}
        ]
    }

    assert _review_calls_from_audit_delta(before, skipped) == 0
    assert _review_calls_from_audit_delta(before, completed) == 2
    assert _review_calls_from_audit_delta(before, before) is None


def test_review_off_requires_zero_calls_and_zero_persisted_audit() -> None:
    oracle = indexed_rows(ORACLE_PATH)
    spec = next(
        item for item in build_full_plan() if item.run_id == "off-durable-00-writer"
    )
    result = {
        **spec.to_dict(),
        "status": "completed",
        "final": {
            "returncode": 0,
            "privacy_findings": [],
            "model_evidence": {
                "review_call_count": 0,
                "review_audit_delta": [],
            },
            "database": {
                "integrity": "ok",
                "memory_items": [],
                "memory_pending_writes": [],
            },
        },
    }

    assert evaluate_results([spec], [result], oracle, require_complete=True)["ok"] is True

    result["final"]["model_evidence"]["review_audit_delta"] = [
        {"status": "skipped", "result": {"status": "skipped", "calls": 0}}
    ]
    audit = evaluate_results([spec], [result], oracle, require_complete=True)
    assert audit["ok"] is False
    assert {issue["code"] for issue in audit["issues"]} == {"review_off_called"}


def test_release_audit_rejects_more_than_four_reviewer_provider_calls() -> None:
    oracle = indexed_rows(ORACLE_PATH)
    spec = next(
        item for item in build_full_plan()
        if item.run_id == "on-durable-00-writer"
    )
    result = {
        **spec.to_dict(),
        "status": "completed",
        "final": {
            "returncode": 0,
            "privacy_findings": [],
            "model_evidence": {
                "review_call_count": 5,
                "review_audit_delta": [
                    {
                        "status": "completed",
                        "result": {
                            "status": "completed",
                            "calls": 5,
                            "actions": 1,
                        },
                    }
                ],
            },
            "database": {
                "integrity": "ok",
                "memory_items": [
                    {
                        "status": "active",
                        "scope_type": "project",
                        "content": (
                            "The maximum page size is 250 records and every "
                            "additional page uses next_page_token."
                        ),
                    }
                ],
                "memory_pending_writes": [],
            },
        },
    }

    audit = evaluate_results([spec], [result], oracle, require_complete=True)

    assert audit["ok"] is False
    assert {issue["code"] for issue in audit["issues"]} == {
        "review_call_limit_exceeded"
    }
    assert audit["issues"][0]["hard"] is True


def test_term_matching_rejects_numeric_substrings_and_negated_facts() -> None:
    assert terms_match("The page limit is 250 records.", ["250", "page"])
    assert terms_match("Upload batches contain 400 records.", ["400", "batch"])
    assert not terms_match("The page limit is 1250 records.", ["250", "page"])
    assert not terms_match("The page limit is not 250; it is 1250.", ["250", "page"])
    assert terms_match("gzip compression level 6", ["6"])
    assert not terms_match("gzip compression level 16", ["6"])


def test_release_first_attempt_gate_requires_at_least_95_percent() -> None:
    first_attempt_94 = [
        {"status": "completed", "attempts": [{}]}
        for _ in range(94)
    ] + [
        {"status": "completed", "attempts": [{}, {}]}
        for _ in range(6)
    ]
    first_attempt_95 = [
        {"status": "completed", "attempts": [{}]}
        for _ in range(95)
    ] + [
        {"status": "completed", "attempts": [{}, {}]}
        for _ in range(5)
    ]

    assert _first_attempt_gate(first_attempt_94, selected_runs=100) is False
    assert _first_attempt_gate(first_attempt_95, selected_runs=100) is True


def test_semantic_audit_reads_v5_active_and_pending_tables() -> None:
    oracle = indexed_rows(ORACLE_PATH)
    specs = {spec.run_id: spec for spec in build_full_plan()}
    active_spec = specs["on-durable-00-writer"]
    active_result = {
        **active_spec.to_dict(),
        "status": "completed",
        "final": {
            "returncode": 0,
            "privacy_findings": [],
            "model_evidence": {
                "review_call_count": 2,
                "review_audit_delta": [
                    {
                        "status": "completed",
                        "result": {"status": "completed", "calls": 2, "actions": 1},
                    }
                ],
            },
            "database": {
                "integrity": "ok",
                "memory_items": [
                    {
                        "status": "active",
                        "scope_type": "project",
                        "content": "The API page limit is 250 records.",
                    }
                ],
                "memory_pending_writes": [],
            },
        },
    }
    pending_spec = specs["approval-00-writer"]
    pending_terms = " ".join(oracle[pending_spec.case_id]["required_terms"])
    pending_result = {
        **pending_spec.to_dict(),
        "status": "completed",
        "final": {
            "returncode": 0,
            "privacy_findings": [],
            "model_evidence": {
                "review_call_count": 2,
                "review_audit_delta": [
                    {
                        "status": "completed",
                        "result": {"status": "completed", "calls": 2, "actions": 1},
                    }
                ],
            },
            "database": {
                "integrity": "ok",
                "memory_items": [],
                "memory_pending_writes": [
                    {
                        "status": "pending",
                        "scope_type": oracle[pending_spec.case_id]["expected_scope"],
                        "content": pending_terms,
                    }
                ],
            },
        },
    }

    assert evaluate_results(
        [active_spec], [active_result], oracle, require_complete=True
    )["ok"] is True
    assert evaluate_results(
        [pending_spec], [pending_result], oracle, require_complete=True
    )["ok"] is True


def test_application_scope_writer_requires_the_workflow_owned_scope_id() -> None:
    oracle = indexed_rows(ORACLE_PATH)
    spec = next(
        item for item in build_full_plan()
        if item.run_id == "app-scope-00-writer"
    )
    result = {
        **spec.to_dict(),
        "status": "completed",
        "final": {
            "returncode": 0,
            "privacy_findings": [],
            "model_evidence": {
                "review_call_count": 1,
                "review_audit_delta": [
                    {
                        "status": "completed",
                        "result": {
                            "status": "completed",
                            "calls": 1,
                            "actions": 1,
                        },
                    }
                ],
            },
            "database": {
                "integrity": "ok",
                "memory_items": [
                    {
                        "status": "active",
                        "scope_type": "application",
                        "scope_id": "memory_feature_validation/variants/app_b",
                        "content": "Only this Application calls its data lake Juniper.",
                    }
                ],
                "memory_pending_writes": [],
            },
        },
    }

    audit = evaluate_results([spec], [result], oracle, require_complete=True)

    assert audit["ok"] is False
    assert {issue["code"] for issue in audit["issues"]} == {"scope_mismatch"}

    result["final"]["database"]["memory_items"][0]["scope_id"] = (
        "memory_feature_validation/variants/app_review"
    )
    assert evaluate_results(
        [spec], [result], oracle, require_complete=True
    )["ok"] is True


def test_recall_oracle_is_independent_from_memory_content_terms() -> None:
    oracle = indexed_rows(ORACLE_PATH)
    spec = next(
        item for item in build_full_plan() if item.run_id == "on-durable-00-recall"
    )
    result = {
        **spec.to_dict(),
        "status": "completed",
        "final": {
            "returncode": 0,
            "final_answer": 250,
            "privacy_findings": [],
            "model_evidence": {
                "review_call_count": 1,
                "review_audit_delta": [
                    {
                        "status": "completed",
                        "result": {"status": "completed", "calls": 1, "actions": 0},
                    }
                ],
            },
            "database": {
                "integrity": "ok",
                "memory_items": [
                    {
                        "status": "active",
                        "scope_type": "project",
                        "content": "The page limit is 250; paginate with next_page_token.",
                    }
                ],
                "memory_pending_writes": [],
            },
        },
    }

    assert oracle[spec.case_id]["required_terms"] == ["250", "page"]
    assert oracle[spec.case_id]["recall_terms"] == ["250"]
    assert evaluate_results([spec], [result], oracle, require_complete=True)["ok"] is True


def test_approval_transition_requires_business_success(monkeypatch) -> None:
    responses = iter(
        [
            {
                "returncode": 0,
                "payload": [
                    {
                        "id": 7,
                        "status": "pending",
                        "payload": {"content": "stable fact"},
                    }
                ],
            },
            {"returncode": 0, "payload": {"ok": False, "error": "not_found"}},
        ]
    )
    monkeypatch.setattr(
        "applications.memory_feature_validation.scripts.run_memory_review_campaign._run_cli",
        lambda *_args, **_kwargs: next(responses),
    )

    result = _approval_transition(
        Path("/unused"),
        {"required_terms": ["stable", "fact"], "decision": "reject"},
        [],
    )

    assert result["ok"] is False


def test_approval_transition_requires_persisted_status_readback(monkeypatch) -> None:
    responses = iter(
        [
            {
                "returncode": 0,
                "payload": [
                    {
                        "id": 7,
                        "status": "pending",
                        "payload": {"content": "stable fact"},
                    }
                ],
            },
            {"returncode": 0, "payload": {"ok": True, "status": "rejected"}},
        ]
    )
    monkeypatch.setattr(
        "applications.memory_feature_validation.scripts.run_memory_review_campaign._run_cli",
        lambda *_args, **_kwargs: next(responses),
    )
    monkeypatch.setattr(
        "applications.memory_feature_validation.scripts.run_memory_review_campaign._db_snapshot",
        lambda *_args, **_kwargs: {
            "integrity": "ok",
            "memory_items": [],
            "memory_pending_writes": [
                {
                    "id": 7,
                    "status": "pending",
                    "content": "stable fact",
                    "resolved_at": None,
                }
            ],
        },
    )

    result = _approval_transition(
        Path("/unused"),
        {"required_terms": ["stable", "fact"], "decision": "reject"},
        [],
    )

    assert result["ok"] is False
    assert result["readback_ok"] is False


def test_failed_reviewer_audit_is_a_hard_failure_for_negative_cases() -> None:
    oracle = indexed_rows(ORACLE_PATH)
    spec = next(
        item for item in build_full_plan() if item.run_id == "progress-00-writer"
    )
    result = {
        **spec.to_dict(),
        "status": "completed",
        "final": {
            "returncode": 0,
            "privacy_findings": [],
            "model_evidence": {
                "review_call_count": 1,
                "review_audit_delta": [
                    {
                        "status": "failed",
                        "result": {"status": "failed", "calls": 1, "actions": 0},
                    }
                ],
            },
            "database": {
                "integrity": "ok",
                "memory_items": [],
                "memory_pending_writes": [],
            },
        },
    }

    audit = evaluate_results([spec], [result], oracle, require_complete=True)

    assert audit["ok"] is False
    assert [(issue["code"], issue["hard"]) for issue in audit["issues"]] == [
        ("review_failed", True)
    ]


def test_approval_audit_rejects_fabricated_success_when_target_stays_pending() -> None:
    oracle = indexed_rows(ORACLE_PATH)
    spec = next(
        item for item in build_full_plan() if item.run_id == "approval-03-post-recall"
    )
    result = {
        **spec.to_dict(),
        "status": "completed",
        "approval_transition": {
            "ok": True,
            "readback_ok": True,
            "decision": "reject",
            "target": "7",
        },
        "final": {
            "returncode": 0,
            "final_answer": "MISSING",
            "privacy_findings": [],
            "model_evidence": {
                "review_call_count": 1,
                "review_audit_delta": [
                    {
                        "status": "completed",
                        "result": {"status": "completed", "calls": 1, "actions": 0},
                    }
                ],
            },
            "database": {
                "integrity": "ok",
                "memory_items": [],
                "memory_pending_writes": [
                    {
                        "id": 7,
                        "status": "pending",
                        "content": "legacy-export-staging-03",
                        "resolved_at": None,
                    }
                ],
            },
        },
    }

    audit = evaluate_results([spec], [result], oracle, require_complete=True)

    assert audit["ok"] is False
    assert [(issue["code"], issue["hard"]) for issue in audit["issues"]] == [
        ("approval_transition", True)
    ]
