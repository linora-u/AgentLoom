"""Deterministic contract tests for the black-box real-LLM campaign."""

from __future__ import annotations

import base64
import json
import os
import sqlite3
import subprocess
import sys
from collections import Counter
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[3]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from applications.memory_feature_validation.agent_tools.validation_probes import (  # noqa: E402
    extract_validation_memory_evidence,
    validation_memory_case,
)
from applications.memory_feature_validation.scripts import (  # noqa: E402
    audit_memory_review_campaign as audit_module,
)
from applications.memory_feature_validation.scripts import (  # noqa: E402
    memory_review_campaign_common as campaign_common,
)
from applications.memory_feature_validation.scripts import (  # noqa: E402
    run_memory_review_campaign as campaign_runner,
)
from applications.memory_feature_validation.scripts.audit_memory_review_campaign import (  # noqa: E402
    _capsule_attempt_issues,
    _first_attempt_gate,
    audit_campaign,
    evaluate_results,
)
from applications.memory_feature_validation.scripts.memory_review_campaign_capsule import (  # noqa: E402
    canonical_json_hash,
)
from applications.memory_feature_validation.scripts.memory_review_campaign_common import (  # noqa: E402
    APP_ROOT,
    CASES_PATH,
    ORACLE_PATH,
    SCENARIO_QUOTAS,
    WORKFLOWS,
    RunSpec,
    build_full_plan,
    dataset_manifest,
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
    _sanitize_text,
    _write_inbox_decision,
)
from src.lib.trusted_memory_evidence import (  # noqa: E402
    extract_trusted_memory_evidence,
)


def _empty_v6_database() -> dict:
    return {
        "integrity": "ok",
        "memory_items": [],
        "review_candidates": [],
        "review_batches": [],
        "review_batch_runs": [],
    }


def _review_off_evidence(*, memory_effects: int = 0, candidate_effects: int = 0) -> dict:
    return {
        "review_call_count": None,
        "review_action_count": 0,
        "review_records": [],
        "review_batch_delta": [],
        "memory_item_effect_count": memory_effects,
        "review_candidate_effect_count": candidate_effects,
        "completed_root_run_ids": [],
        "completed_run_count_delta": 0,
    }


def _valid_capsule_descriptor(
    source: dict,
    dataset: dict,
    model_contract: dict,
) -> dict:
    uv_lock = next(row for row in source["files"] if row["path"] == "uv.lock")
    descriptor = {
        "schema_version": 1,
        "git_commit": source["commit"],
        "source_manifest_hash": canonical_json_hash(source["files"]),
        "dataset_manifest_hash": canonical_json_hash(dataset),
        "model_contract_hash": canonical_json_hash(model_contract),
        "uv_lock_hash": uv_lock["sha256"],
        "uv_version": "uv 0.11.15",
        "uv_binary_hash": "0" * 64,
        "git_version": "git version 2.50.0",
        "git_binary_hash": "6" * 64,
        "lock_sync_ok": True,
        "python_version": "3.12",
        "python_cache_tag": "cpython-312",
        "python_binary_hash": "1" * 64,
        "loom_hash": "2" * 64,
        "loom_shebang_target_hash": "3" * 64,
        "distribution_set_hash": "4" * 64,
        "venv_manifest_hash": "5" * 64,
        "stdlib_manifest_hash": "7" * 64,
        "checkout_manifest_hash": "8" * 64,
        "runtime_env_contract_hash": "9" * 64,
        "write_guard_binary_hash": "a" * 64,
        "bootstrap_valid": True,
        "checkout_exact": True,
        "write_guard_available": True,
        "python_is_capsule": True,
        "python_prefix_is_capsule": True,
        "loom_is_capsule": True,
        "loom_shebang_is_capsule": True,
        "loom_shebang_matches_python": True,
        "src_origin_is_capsule": True,
        "runner_origin_is_capsule": True,
        "src_origin_relative": "src/__init__.py",
        "runner_origin_relative": (
            "applications/memory_feature_validation/scripts/"
            "run_memory_review_campaign.py"
        ),
        "user_site_disabled": True,
        "bytecode_writes_disabled": True,
        "python_dont_write_bytecode": True,
        "pycache_prefix_exact": True,
        "pycache_prefix_inside_capsule": True,
        "pycache_prefix_empty": True,
        "pycache_prefix_read_only": True,
        "pycache_prefix_not_symlink": True,
        "capsule_tree_read_only": True,
        "capsule_files_unshared": True,
        "git_metadata_write_guarded": True,
        "model_config_memory_only": True,
        "private_parent": True,
    }
    descriptor["capsule_id"] = canonical_json_hash(descriptor)
    return descriptor


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
    assert all(
        row.get("expected_writer_status") == "absent"
        or isinstance(row.get("expected_content"), str)
        and bool(row["expected_content"])
        for row in oracle.values()
    )


def test_oracle_expected_content_matches_one_exact_durable_fixture_fact() -> None:
    oracle = indexed_rows(ORACLE_PATH)
    fixtures: dict[str, dict[str, object]] = {}
    for path in sorted((APP_ROOT / "data" / "fixtures").glob("*.jsonl")):
        fixtures.update(indexed_rows(path))

    for case_id, expected in oracle.items():
        if expected["expected_writer_status"] == "absent":
            continue
        durable_facts = [
            evidence["text"]
            for evidence in fixtures[case_id]["memory_evidence"]
            if evidence.get("kind") == "durable_fact"
        ]
        assert durable_facts == [expected["expected_content"]], case_id


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


def test_five_canaries_cover_off_on_recall_progress_and_security() -> None:
    canaries = select_runs(5)

    assert [(spec.case_id, spec.phase) for spec in canaries] == [
        ("off-durable-00", "writer"),
        ("on-durable-00", "writer"),
        ("on-durable-00", "recall"),
        ("progress-00", "writer"),
        ("security-00", "writer"),
    ]


def test_variant_configs_express_only_new_review_and_approval_switches() -> None:
    expected = {
        "off": None,
        "on": (True, "summary", "auto", "after_run", "manual"),
        "approval": (True, "summary", "manual", "after_run", "manual"),
        "app_review": (True, "summary", "auto", "after_run", "manual"),
        "app_a": (False, "", "auto", None, None),
        "app_b": (False, "", "auto", None, None),
    }
    for variant, expected_policy in expected.items():
        payload = yaml.safe_load(
            (APP_ROOT / "variants" / variant / "config" / "system.yaml").read_text(encoding="utf-8")
        )
        review = payload["self_learning"].get("review")
        if expected_policy is None:
            assert review is None
            continue
        enabled, review_model, approval, app_trigger, project_trigger = expected_policy
        assert review["enabled"] is enabled
        for scope in ("application", "project"):
            expected_scope = {
                "review_model": review_model,
                "approval": {"fact": approval, "experience": approval},
            }
            trigger = app_trigger if scope == "application" else project_trigger
            if trigger is not None:
                expected_scope["trigger"] = {"mode": trigger}
            assert review[scope] == expected_scope
        assert "review_model" not in (payload["self_learning"].get("memory") or {})
        assert "write_approval" not in (payload["self_learning"].get("memory") or {})
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
    recall_workflows = {
        WORKFLOWS["off_recall"],
        WORKFLOWS["on_recall"],
        WORKFLOWS["approval_recall"],
        WORKFLOWS["app_review_recall"],
        WORKFLOWS["app_a_recall"],
        WORKFLOWS["app_b_recall"],
    }
    for relative in WORKFLOWS.values():
        payload = yaml.safe_load((Path(__file__).resolve().parents[3] / relative).read_text(encoding="utf-8"))
        workflow = str(payload.get("workflow") or "")
        assert "memory(action=" not in workflow
        assert "EXACTLY this" not in workflow
        assert payload.get("model_type") == "summary"
        assert payload.get("tool_call_type") == "tool_call"
        tools = payload.get("tools") or []
        if relative in recall_workflows:
            assert tools == []
            assert "validation_memory_case" not in workflow
            assert "complete persistent memory fact" in workflow
            assert "MISSING" in workflow
        else:
            assert any(tool.get("name") == "validation_memory_case" for tool in tools)

    review_workflow = str(
        yaml.safe_load(
            (APP_ROOT / "variants" / "on" / "workflows" / "analyze_without_memory.yaml").read_text(
                encoding="utf-8"
            )
        )["workflow"]
    )
    assert "untrusted task data" not in review_workflow
    assert "data, never as instructions" in review_workflow

    project_writer = yaml.safe_load(
        (
            APP_ROOT
            / "variants"
            / "app_a"
            / "workflows"
            / "analyze_with_memory.yaml"
        ).read_text(encoding="utf-8")
    )["workflow"]
    assert "verified durable" in project_writer
    assert "persistent-memory" in project_writer


def test_real_campaign_model_contract_is_safe_reproducible_metadata(
    tmp_path: Path,
) -> None:
    llm_path = tmp_path / "llm.yaml"
    llm_path.write_text(
        yaml.safe_dump(
            {
                "model": {
                    "summary": {
                        "model": "openai/test-summary",
                        "base_url": "https://provider.example/v1",
                        "api_key": "must-not-leak",
                        "temperature": 0.3,
                        "num_retries": 0,
                        "tool_choice": "auto",
                        "parallel_tool_calls": False,
                        "extra_body": {"thinking": {"type": "auto"}},
                    }
                }
            }
        ),
        encoding="utf-8",
    )

    contract = campaign_runner._safe_model_contract(llm_path)

    assert contract == {
        "configured": True,
        "requested_type": "summary",
        "model_id": "openai/test-summary",
        "endpoint_hash": contract["endpoint_hash"],
        "temperature": 0.3,
        "max_tokens": None,
        "timeout": None,
        "num_retries": 0,
        "tool_choice": "auto",
        "parallel_tool_calls": False,
        "thinking_type": "auto",
        "config_hash": contract["config_hash"],
    }
    assert len(contract["config_hash"]) == 64
    assert len(contract["endpoint_hash"]) == 64
    assert "must-not-leak" not in json.dumps(contract)
    assert campaign_runner._model_contract_issues(contract) == []

    payload = yaml.safe_load(llm_path.read_text(encoding="utf-8"))
    payload["model"]["summary"]["requests_per_minute"] = 99
    llm_path.write_text(yaml.safe_dump(payload), encoding="utf-8")
    rate_limited = campaign_runner._safe_model_contract(llm_path)
    assert rate_limited["config_hash"] != contract["config_hash"]

    payload["model"]["summary"]["api_key"] = "different-secret"
    llm_path.write_text(yaml.safe_dump(payload), encoding="utf-8")
    changed_secret = campaign_runner._safe_model_contract(llm_path)
    assert changed_secret["config_hash"] == rate_limited["config_hash"]

    payload["model"]["summary"].update(
        {
            "secret_key": "short-a",
            "aws_secret_access_key": "aws-a",
            "ａｐｉ＿ｋｅｙ": "fullwidth-a",
            "extra_headers": {"X-Credential": "header-a"},
        }
    )
    llm_path.write_text(yaml.safe_dump(payload), encoding="utf-8")
    credential_shape = campaign_runner._safe_model_contract(llm_path)
    for key in (
        "secret_key",
        "aws_secret_access_key",
        "ａｐｉ＿ｋｅｙ",
    ):
        payload["model"]["summary"][key] += "-rotated"
    payload["model"]["summary"]["extra_headers"]["X-Credential"] = "header-b"
    llm_path.write_text(yaml.safe_dump(payload), encoding="utf-8")
    rotated = campaign_runner._safe_model_contract(llm_path)
    assert rotated["config_hash"] == credential_shape["config_hash"]

    payload["model"]["summary"]["extra_headers"]["X-Model-Route"] = "route-a"
    llm_path.write_text(yaml.safe_dump(payload), encoding="utf-8")
    route_a = campaign_runner._safe_model_contract(llm_path)
    payload["model"]["summary"]["extra_headers"]["X-Model-Route"] = "route-b"
    llm_path.write_text(yaml.safe_dump(payload), encoding="utf-8")
    route_b = campaign_runner._safe_model_contract(llm_path)
    assert route_a["config_hash"] != route_b["config_hash"]


def test_real_campaign_model_preflight_requires_no_hidden_retries_or_parallel_calls() -> None:
    base = {
        "configured": True,
        "requested_type": "summary",
        "model_id": "openai/test-summary",
        "num_retries": 0,
        "parallel_tool_calls": False,
    }

    assert campaign_runner._model_contract_issues({**base, "configured": False}) == [
        "summary model is not configured"
    ]
    assert campaign_runner._model_contract_issues({**base, "num_retries": 2}) == [
        "summary model must set num_retries: 0"
    ]
    assert campaign_runner._model_contract_issues(
        {**base, "parallel_tool_calls": True}
    ) == ["summary model must set parallel_tool_calls: false"]
    assert campaign_runner._model_contract_issues(
        {**base, "thinking_type": "disabled"}
    ) == ["summary model must not disable thinking"]

    example = campaign_runner._safe_model_contract(
        REPO_ROOT / "config" / "llm.example.yaml"
    )
    assert campaign_runner._model_contract_issues(example) == []


def test_capsule_model_config_pipe_is_one_shot_and_privacy_markers_are_content_free(
    tmp_path: Path,
    monkeypatch,
) -> None:
    secret = "provider-secret-ÿ-value-938475"
    payload = yaml.safe_dump(
        {
            "model": {
                "summary": {
                    "model": "openai/test-summary",
                    "apiKey": "different-api-key-value",
                    "extra_headers": {
                        "Authorization": f"Bearer {secret}",
                    },
                }
            }
        }
    ).encode()
    read_fd, write_fd = os.pipe()
    os.write(write_fd, payload)
    os.close(write_fd)
    monkeypatch.setattr(campaign_runner, "_ACTIVE_MODEL_CONFIG_BYTES", None)
    monkeypatch.setattr(campaign_runner, "_ACTIVE_MODEL_PRIVACY_MARKERS", ())
    monkeypatch.setenv(
        "AGENTLOOM_MEMORY_CAMPAIGN_LLM_CONFIG_FD",
        str(read_fd),
    )

    campaign_runner._consume_active_model_config()

    assert campaign_runner._ACTIVE_MODEL_CONFIG_BYTES == payload
    assert "AGENTLOOM_MEMORY_CAMPAIGN_LLM_CONFIG_FD" not in os.environ
    with pytest.raises(OSError):
        os.fstat(read_fd)
    labels = set(campaign_runner._ACTIVE_MODEL_PRIVACY_MARKERS)
    assert ("model_config_blob", payload) in labels
    assert ("model_config_base64", base64.b64encode(payload)) in labels
    assert ("model_config_secret", secret.encode()) in labels
    assert (
        "model_config_secret",
        f"Bearer {secret}".encode(),
    ) in labels
    secret_bytes = secret.encode()
    encoded_markers = {
        base64.b64encode(secret_bytes),
        base64.b64encode(secret_bytes).rstrip(b"="),
        base64.urlsafe_b64encode(secret_bytes),
        base64.urlsafe_b64encode(secret_bytes).rstrip(b"="),
    }
    assert encoded_markers <= {marker for _kind, marker in labels}

    encoded_only = base64.urlsafe_b64encode(secret_bytes).rstrip(b"=")
    stdout_findings = campaign_runner._real_marker_findings_bytes(
        b"provider rejected credential=" + encoded_only,
        location="stdout:encoded-secret",
    )
    assert stdout_findings
    assert encoded_only.decode() not in json.dumps(stdout_findings, sort_keys=True)
    encoded_artifact = tmp_path / "encoded-provider.log"
    with pytest.raises(
        RuntimeError,
        match="refusing to persist model configuration material",
    ):
        campaign_runner._write_text(
            encoded_artifact,
            f"provider rejected credential={encoded_only.decode()}",
        )
    assert not encoded_artifact.exists()

    leaked = tmp_path / "provider.log"
    leaked.write_text(f"provider rejected api_key={secret}\n", encoding="utf-8")
    findings = campaign_runner._real_marker_findings([leaked])
    serialized_findings = json.dumps(findings, sort_keys=True)
    assert findings
    assert secret not in serialized_findings
    assert base64.b64encode(payload).decode() not in serialized_findings
    assert all(set(finding) == {"path", "kind"} for finding in findings)
    sanitized = campaign_runner._sanitize_text(
        leaked.read_text(encoding="utf-8"),
        campaign_runner._active_marker_texts(),
    )
    assert secret not in sanitized


def test_complete_model_config_blob_registers_every_base64_transport_form() -> None:
    # This literal is intentionally chosen so standard and URL-safe Base64
    # differ and the sensitive scalar is not aligned within the encoded YAML
    # blob.  A scalar-only marker therefore cannot make this test pass.
    payload = yaml.safe_dump(
        {
            "model": {
                "summary": {
                    "model": "openai/test-summary",
                    "api_key": "x˛或꿽㟳沏y",
                }
            }
        },
        allow_unicode=True,
    ).encode()

    labels = set(campaign_runner._model_config_privacy_markers(payload))
    expected = {
        ("model_config_base64", base64.b64encode(payload)),
        (
            "model_config_base64_unpadded",
            base64.b64encode(payload).rstrip(b"="),
        ),
        ("model_config_urlsafe_base64", base64.urlsafe_b64encode(payload)),
        (
            "model_config_urlsafe_base64_unpadded",
            base64.urlsafe_b64encode(payload).rstrip(b"="),
        ),
    }

    assert expected <= labels
    campaign_runner._ACTIVE_MODEL_PRIVACY_MARKERS = tuple(labels)
    encoded_only = base64.urlsafe_b64encode(payload).rstrip(b"=")
    assert campaign_runner._real_marker_findings_bytes(
        encoded_only,
        location="stdout:encoded-complete-config",
    )


def test_campaign_atomic_writer_refuses_real_model_config_material(
    tmp_path: Path,
    monkeypatch,
) -> None:
    secret = b"atomic-write-secret-123456"
    monkeypatch.setattr(
        campaign_runner,
        "_ACTIVE_MODEL_PRIVACY_MARKERS",
        (("model_config_secret", secret),),
    )
    output = tmp_path / "artifact.json"

    with pytest.raises(
        RuntimeError,
        match="refusing to persist model configuration material",
    ):
        campaign_runner._write_text(output, secret.decode())

    assert not output.exists()


def test_real_campaign_publishes_only_clean_snapshots_from_private_scratch(
    tmp_path: Path,
    monkeypatch,
) -> None:
    campaign_dir = tmp_path / "campaign"
    campaign_dir.mkdir()
    spec = RunSpec(
        "scratch-clean",
        "case",
        "scenario",
        "writer",
        1,
        "workflow.yaml",
        "state",
        True,
    )
    observed_roots: list[Path] = []

    def fake_run_groups(_groups, **kwargs):
        execution_root = kwargs["execution_root"]
        observed_roots.append(execution_root)
        assert execution_root != campaign_dir
        assert campaign_dir not in execution_root.parents
        assert execution_root.stat().st_mode & 0o777 == 0o700
        snapshot = execution_root / "retry_state" / spec.run_id
        snapshot.mkdir(parents=True)
        (snapshot / "self_learning.db").write_bytes(b"clean snapshot")
        attempt = {"privacy_findings": [], "timed_out": False}
        return [
            {
                **spec.to_dict(),
                "status": "completed",
                "attempts": [attempt],
                "final": attempt,
            }
        ]

    monkeypatch.setattr(campaign_runner, "_run_groups", fake_run_groups)
    monkeypatch.setattr(
        audit_module,
        "evaluate_results",
        lambda *_args, **_kwargs: {"ok": True, "complete": True, "issues": []},
    )

    results = campaign_runner._run_real_campaign(
        [spec],
        campaign_dir=campaign_dir,
        requested_runs=1,
        timeout_seconds=1,
        max_workers=1,
        oracle={},
        markers=[],
    )

    assert results[0]["final"]["privacy_findings"] == []
    assert (campaign_dir / "retry_state" / spec.run_id / "self_learning.db").read_bytes() == b"clean snapshot"
    assert not (campaign_dir / "state").exists()
    assert not (campaign_dir / "runtime").exists()
    assert observed_roots and all(not root.exists() for root in observed_roots)


def test_real_campaign_discards_private_scratch_instead_of_publishing_a_secret(
    tmp_path: Path,
    monkeypatch,
) -> None:
    campaign_dir = tmp_path / "campaign"
    campaign_dir.mkdir()
    secret = b"private-scratch-provider-secret"
    spec = RunSpec(
        "scratch-secret",
        "case",
        "scenario",
        "writer",
        1,
        "workflow.yaml",
        "state",
        True,
    )
    observed_roots: list[Path] = []

    def fake_run_groups(_groups, **kwargs):
        execution_root = kwargs["execution_root"]
        observed_roots.append(execution_root)
        snapshot = execution_root / "retry_state" / spec.run_id
        snapshot.mkdir(parents=True)
        (snapshot / "self_learning.db").write_bytes(secret)
        attempt = {"privacy_findings": [], "timed_out": False}
        return [
            {
                **spec.to_dict(),
                "status": "completed",
                "attempts": [attempt],
                "final": attempt,
            }
        ]

    monkeypatch.setattr(campaign_runner, "_run_groups", fake_run_groups)
    monkeypatch.setattr(
        campaign_runner,
        "_ACTIVE_MODEL_PRIVACY_MARKERS",
        (("model_config_secret", secret),),
    )
    monkeypatch.setattr(
        audit_module,
        "evaluate_results",
        lambda *_args, **_kwargs: {"ok": True, "complete": True, "issues": []},
    )

    results = campaign_runner._run_real_campaign(
        [spec],
        campaign_dir=campaign_dir,
        requested_runs=1,
        timeout_seconds=1,
        max_workers=1,
        oracle={},
        markers=[],
    )

    findings = results[0]["final"]["privacy_findings"]
    assert findings == [
        {
            "path": (
                "private-scratch/retry_state/"
                f"{spec.run_id}/self_learning.db"
            ),
            "kind": "model_config_secret",
        }
    ]
    assert not (campaign_dir / "retry_state").exists()
    assert not (campaign_dir / "reproduction_state").exists()
    assert observed_roots and all(not root.exists() for root in observed_roots)
    assert secret not in b"".join(
        path.read_bytes() for path in campaign_dir.rglob("*") if path.is_file()
    )


def test_reproduction_never_publishes_new_raw_state_or_runtime(
    tmp_path: Path,
    monkeypatch,
) -> None:
    reproduction_dir = tmp_path / "reproduction"
    reproduction_dir.mkdir()
    snapshot = tmp_path / "published-snapshot"
    snapshot.mkdir()
    (snapshot / "self_learning.db").write_bytes(b"published evidence")
    secret = b"reproduction-provider-secret"
    spec = RunSpec(
        "reproduction-private",
        "case",
        "scenario",
        "writer",
        1,
        "workflow.yaml",
        "state",
        True,
    )
    observed_roots: list[Path] = []

    def fake_run_spec(current_spec, **kwargs):
        execution_root = kwargs["execution_root"]
        observed_roots.append(execution_root)
        assert kwargs["state_root"].is_relative_to(execution_root)
        runtime = execution_root / "runtime" / current_spec.run_id
        runtime.mkdir(parents=True)
        (runtime / "provider.log").write_bytes(secret)
        attempt = {"privacy_findings": [], "timed_out": False}
        return {
            **current_spec.to_dict(),
            "status": "completed",
            "attempts": [attempt],
            "final": attempt,
        }

    monkeypatch.setattr(campaign_runner, "_run_spec", fake_run_spec)
    monkeypatch.setattr(
        campaign_runner,
        "_ACTIVE_MODEL_PRIVACY_MARKERS",
        (("model_config_secret", secret),),
    )

    result = campaign_runner._run_reproduction_in_private_scratch(
        spec,
        snapshot=snapshot,
        reproduction_dir=reproduction_dir,
        timeout_seconds=1,
        oracle={},
        markers=[],
    )

    assert result["final"]["privacy_findings"] == [
        {
            "path": (
                "private-scratch/runtime/"
                f"{spec.run_id}/provider.log"
            ),
            "kind": "model_config_secret",
        }
    ]
    assert not (reproduction_dir / "state").exists()
    assert not (reproduction_dir / "runtime").exists()
    assert not (reproduction_dir / "retry_state").exists()
    assert observed_roots and all(not root.exists() for root in observed_roots)


def test_real_campaign_release_sources_bind_harness_workflows_and_runtime() -> None:
    paths = {
        row["path"] for row in campaign_common.release_source_manifest()
    }

    assert {
        "applications/memory_feature_validation/scripts/run_memory_review_campaign.py",
        "applications/memory_feature_validation/scripts/audit_memory_review_campaign.py",
        "applications/memory_feature_validation/variants/on/config/system.yaml",
        "applications/memory_feature_validation/variants/on/workflows/analyze_without_memory.yaml",
        "src/extensions/self_learning/reviewer.py",
        "src/extensions/self_learning/persistence/memory_store.py",
        "src/lib/smolagents/agent/base_agent.py",
        "src/lib/trusted_memory_evidence.py",
        "src/runner.py",
        "src/lib/smolagents/agent/yaml_agent_factory.py",
        "src/lib/smolagents/models/model_manager.py",
        "src/lib/smolagents/models/tool_call_parser.py",
        "src/lib/config/llm_config.py",
        "pyproject.toml",
        "uv.lock",
    } <= paths


def test_review_off_cohort_uses_real_global_summary_application_opt_out(
    monkeypatch,
) -> None:
    import src.lib.config.config as config_module

    specs = [
        spec for spec in build_full_plan()
        if spec.scenario == "review_off_durable"
    ]

    assert len(specs) == 10
    assert {spec.agent_root for spec in specs} == {
        "applications/memory_feature_validation/variants/global_summary_fixture"
    }
    assert len({spec.state_key for spec in specs}) == 10
    oracle = indexed_rows(ORACLE_PATH)

    for spec in specs:
        assert oracle[spec.case_id]["config_layering"] == (
            "global_summary_application_opt_out"
        )
        agent_root = REPO_ROOT / spec.agent_root
        workflow = REPO_ROOT / spec.workflow
        global_config = yaml.safe_load(
            (agent_root / "config" / "system.yaml").read_text(encoding="utf-8")
        )
        app_config = yaml.safe_load(
            (workflow.parent.parent / "config" / "system.yaml").read_text(
                encoding="utf-8"
            )
        )
        assert global_config["self_learning"]["review"]["application"]["review_model"] == "summary"
        assert global_config["self_learning"]["review"]["application"]["trigger"]["mode"] == "after_run"
        assert global_config["self_learning"]["review"]["project"]["review_model"] == "summary"
        assert global_config["self_learning"]["review"]["project"]["trigger"]["mode"] == "manual"
        assert "review_model" not in app_config["self_learning"]["review"]["application"]
        assert app_config["self_learning"]["review"]["application"]["trigger"]["mode"] == "manual"
        assert workflow.is_relative_to(agent_root / "applications")

    agent_root = REPO_ROOT / specs[0].agent_root
    workflow = REPO_ROOT / specs[0].workflow
    fixture_llm = config_module.LLMConfig.load_from_yaml(
        REPO_ROOT / "config" / "llm.example.yaml"
    )
    monkeypatch.setattr(config_module, "_load_llm_config", lambda _path: fixture_llm)
    base = config_module._load_merged_config(config_dir=agent_root / "config")
    monkeypatch.setattr(config_module, "_ACTIVE_CONFIG", base)
    effective = config_module.build_effective_agent_config(
        {"_yaml_file_path": str(workflow)},
        source_name="real campaign opt-out workflow",
    )
    assert base.raw["self_learning"]["review"]["application"]["review_model"] == "summary"
    assert effective["self_learning"]["review"]["application"]["review_model"] == "summary"
    assert effective["self_learning"]["review"]["application"]["trigger"]["mode"] == "manual"
    assert effective["self_learning"]["review"]["project"]["review_model"] == "summary"
    assert effective["self_learning"]["review"]["project"]["trigger"]["mode"] == "manual"

    spec = specs[0]
    assert audit_module._agent_root_attempt_issues(
        spec,
        {"attempts": [{"agent_root": spec.agent_root}]},
    ) == []
    assert audit_module._agent_root_attempt_issues(
        spec,
        {"attempts": [{"agent_root": "."}]},
    ) == ["Application attempt did not use its canonical agent root"]


def test_review_off_nested_application_loads_its_tool_in_isolated_runtime() -> None:
    spec = next(
        spec
        for spec in build_full_plan()
        if spec.scenario == "review_off_durable"
    )
    agent_root = (REPO_ROOT / spec.agent_root).resolve()
    workflow = (REPO_ROOT / spec.workflow).resolve()
    workflow_relative = workflow.relative_to(agent_root).as_posix()
    script = f"""
import json
from pathlib import Path

import yaml

from src.lib.utils.workspace import ensure_workspace_mounted_once

ensure_workspace_mounted_once()
workflow = yaml.safe_load(Path({workflow_relative!r}).read_text(encoding="utf-8"))
tool = next(item for item in workflow["tools"] if item["name"] == "validation_memory_case")
from src.lib.utils.dynamic_import import load_function
function = load_function(tool["module"], tool["function"])
payload = json.loads(function())
assert payload["case_id"] == {spec.case_id!r}
assert payload["phase"] == "writer"
assert payload["evidence"] is not None
print("NESTED_TOOL_OK")
"""
    env = os.environ.copy()
    env.pop("PYTHONPATH", None)
    env.update(
        {
            "AGENTLOOM_MEMORY_CAMPAIGN_CAPSULE_ACTIVE": "1",
            "AGENTLOOM_MEMORY_CAMPAIGN_LLM_CONFIG_FD": "0",
            "AGENTLOOM_MEMORY_CASE_ID": spec.case_id,
            "AGENTLOOM_MEMORY_CASE_PHASE": "writer",
        }
    )

    completed = subprocess.run(
        [sys.executable, "-I", "-P", "-B", "-c", script],
        cwd=agent_root,
        env=env,
        input=(REPO_ROOT / "config" / "llm.example.yaml").read_bytes(),
        capture_output=True,
        check=False,
        timeout=30,
    )

    assert completed.returncode == 0, completed.stderr.decode(errors="replace")
    assert b"NESTED_TOOL_OK" in completed.stdout


def test_real_campaign_executes_only_the_repository_loom() -> None:
    assert Path(campaign_runner._loom()) == (
        REPO_ROOT / ".venv" / "bin" / "loom"
    ).resolve()


def test_real_campaign_provenance_rejects_dirty_or_drifting_bound_sources() -> None:
    manifest = [
        {"path": "bound.py", "sha256": "a" * 64, "bytes": 1},
        {"path": "uv.lock", "sha256": "b" * 64, "bytes": 1},
    ]
    clean = {
        "available": True,
        "commit": "a" * 40,
        "dirty": False,
        "files": manifest,
    }
    plan = {"dataset": {"files": []}}
    model_contract = {
        "configured": True,
        "requested_type": "summary",
        "model_id": "openai/test",
        "config_hash": "a" * 64,
        "endpoint_hash": "b" * 64,
        "num_retries": 0,
        "parallel_tool_calls": False,
        "thinking_type": "auto",
    }
    capsule = _valid_capsule_descriptor(clean, plan["dataset"], model_contract)
    environment = {
        "source": clean,
        "model_contract": model_contract,
        "capsule": capsule,
    }
    completed = {
        "source": clean,
        "model_contract": model_contract,
        "dataset": {"files": []},
        "capsule": capsule,
    }

    assert audit_module._provenance_issues(plan, environment, completed) == []
    assert audit_module._provenance_issues(
        plan,
        {**environment, "source": {**clean, "dirty": True}},
        completed,
    ) == ["bound campaign sources did not match the recorded commit"]
    assert audit_module._provenance_issues(
        plan,
        environment,
        {**completed, "source": {**clean, "files": []}},
    ) == ["bound campaign sources changed while the campaign was running"]
    assert audit_module._provenance_issues(
        plan,
        environment,
        {
            **completed,
            "model_contract": {**model_contract, "config_hash": "c" * 64},
        },
    ) == ["summary model contract changed while the campaign was running"]
    drifted_capsule = dict(capsule)
    drifted_capsule["distribution_set_hash"] = "9" * 64
    drifted_capsule.pop("capsule_id")
    drifted_capsule["capsule_id"] = canonical_json_hash(drifted_capsule)
    assert audit_module._provenance_issues(
        plan,
        environment,
        {**completed, "capsule": drifted_capsule},
    ) == ["capsule runtime changed while the campaign was running"]


def test_real_campaign_rejects_attempt_from_another_capsule() -> None:
    capsule_id = "a" * 64
    valid = {
        "run_id": "run-valid",
        "attempts": [
            {"capsule_id": capsule_id, "execution_root": capsule_id}
        ],
    }
    escaped = {
        "run_id": "run-escaped",
        "attempts": [
            {"capsule_id": "b" * 64, "execution_root": capsule_id}
        ],
    }

    assert _capsule_attempt_issues([valid], capsule_id=capsule_id) == []
    assert _capsule_attempt_issues([valid, escaped], capsule_id=capsule_id) == [
        "Application attempt did not execute in the recorded capsule: run-escaped"
    ]


@pytest.mark.parametrize(
    "field",
    ("venv_manifest_hash", "loom_hash", "loom_shebang_target_hash"),
)
def test_reproduction_rejects_execution_byte_drift_before_model_call(
    field: str,
) -> None:
    recorded = _valid_capsule_descriptor(
        {
            "commit": "a" * 40,
            "files": [{"path": "uv.lock", "sha256": "b" * 64, "bytes": 1}],
        },
        {"files": []},
        {"configured": True},
    )
    current = dict(recorded)
    # Distribution name/version evidence intentionally remains unchanged.
    current[field] = "f" * 64

    assert current["distribution_set_hash"] == recorded["distribution_set_hash"]
    assert campaign_runner._reproduction_capsule_contract_changed(
        current,
        recorded,
    ) is True


def test_real_run_never_falls_through_to_mutable_checkout(
    monkeypatch,
) -> None:
    monkeypatch.delenv("AGENTLOOM_MEMORY_CAMPAIGN_CAPSULE_ACTIVE", raising=False)
    monkeypatch.setattr(
        campaign_runner,
        "release_git_source_state",
        lambda: {"available": True, "dirty": True, "commit": "a" * 40},
    )
    monkeypatch.setattr(
        campaign_runner,
        "_safe_model_contract",
        lambda: {"configured": False},
    )
    monkeypatch.setattr(
        campaign_runner,
        "_campaign_dir",
        lambda *_args, **_kwargs: pytest.fail(
            "real campaign reached the mutable checkout"
        ),
    )
    monkeypatch.setattr(sys, "argv", ["memory-campaign", "--runs", "1"])

    assert campaign_runner.main() == 1


def test_main_checks_git_metadata_before_source_inspection(
    monkeypatch,
) -> None:
    def reject_metadata(_root):
        raise RuntimeError("Git metadata is externally aliased")

    monkeypatch.setattr(
        campaign_runner,
        "_require_isolated_git_metadata",
        reject_metadata,
    )
    monkeypatch.setattr(
        campaign_runner,
        "release_git_source_state",
        lambda: pytest.fail("Git source inspection ran before metadata isolation"),
    )
    monkeypatch.setattr(sys, "argv", ["memory-campaign", "--runs", "1"])

    assert campaign_runner.main() == 1


def test_active_capsule_preflight_outputs_only_safe_issue_codes(
    monkeypatch,
    capsys,
) -> None:
    sensitive_failure = (
        "failed to parse /private/alice/config/llm.yaml "
        "with api_key=campaign-secret"
    )

    def reject_model_config() -> None:
        raise RuntimeError(sensitive_failure)

    monkeypatch.setattr(
        campaign_runner,
        "_require_isolated_git_metadata",
        lambda _root: [],
    )
    monkeypatch.setattr(campaign_runner, "capsule_is_active", lambda: True)
    monkeypatch.setattr(
        campaign_runner,
        "_consume_active_model_config",
        reject_model_config,
    )
    monkeypatch.setattr(sys, "argv", ["memory-campaign", "--runs", "1"])

    assert campaign_runner.main() == 1
    output = capsys.readouterr().out
    assert json.loads(output) == {
        "issue_codes": ["model_config_invalid"],
        "status": "CAPSULE_PREFLIGHT_FAIL",
    }
    assert sensitive_failure not in output
    assert "/private/alice" not in output
    assert "campaign-secret" not in output


def test_active_capsule_preflight_safely_classifies_descriptor_build_failure(
    monkeypatch,
    capsys,
) -> None:
    sensitive_failure = (
        "manifest race under /private/alice/stdlib "
        "with Authorization: campaign-secret"
    )

    def reject_descriptor(**_kwargs):
        raise FileNotFoundError(sensitive_failure)

    monkeypatch.setattr(
        campaign_runner,
        "_require_isolated_git_metadata",
        lambda _root: [],
    )
    monkeypatch.setattr(campaign_runner, "capsule_is_active", lambda: True)
    monkeypatch.setattr(campaign_runner, "_consume_active_model_config", lambda: None)
    monkeypatch.setattr(
        campaign_runner,
        "active_capsule_bootstrap_issues",
        lambda _root: [],
    )
    monkeypatch.setattr(
        campaign_runner,
        "release_git_source_state",
        lambda: {"available": True, "dirty": False},
    )
    monkeypatch.setattr(
        campaign_runner,
        "_safe_model_contract",
        lambda **_kwargs: {"configured": True},
    )
    monkeypatch.setattr(campaign_runner, "_model_contract_issues", lambda _: [])
    monkeypatch.setattr(campaign_runner, "dataset_manifest", lambda: {"files": []})
    monkeypatch.setattr(
        campaign_runner,
        "build_capsule_descriptor",
        reject_descriptor,
    )
    monkeypatch.setattr(sys, "argv", ["memory-campaign", "--runs", "1"])

    assert campaign_runner.main() == 1
    output = capsys.readouterr().out
    assert json.loads(output) == {
        "issue_codes": ["capsule_descriptor_build_failed"],
        "status": "CAPSULE_PREFLIGHT_FAIL",
    }
    assert sensitive_failure not in output
    assert "/private/alice" not in output
    assert "campaign-secret" not in output


def test_active_capsule_preflight_identifies_invalid_dependency_environment(
    monkeypatch,
    capsys,
) -> None:
    sensitive_failure = (
        "uv failed under /private/alice/capsule with Authorization: secret-token"
    )
    monkeypatch.setattr(
        campaign_runner,
        "_require_isolated_git_metadata",
        lambda _root: [],
    )
    monkeypatch.setattr(campaign_runner, "capsule_is_active", lambda: True)
    monkeypatch.setattr(campaign_runner, "_consume_active_model_config", lambda: None)
    monkeypatch.setattr(
        campaign_runner,
        "active_capsule_bootstrap_issues",
        lambda _root: [],
    )
    monkeypatch.setattr(
        campaign_runner,
        "release_git_source_state",
        lambda: {"available": True, "dirty": False},
    )
    monkeypatch.setattr(
        campaign_runner,
        "_safe_model_contract",
        lambda **_kwargs: {"configured": True},
    )
    monkeypatch.setattr(campaign_runner, "_model_contract_issues", lambda _: [])
    monkeypatch.setattr(campaign_runner, "dataset_manifest", lambda: {"files": []})
    monkeypatch.setattr(
        campaign_runner,
        "build_capsule_descriptor",
        lambda **_kwargs: {"lock_sync_ok": False},
    )
    monkeypatch.setattr(
        campaign_runner,
        "capsule_descriptor_issues",
        lambda _descriptor: [sensitive_failure],
    )
    monkeypatch.setattr(sys, "argv", ["memory-campaign", "--runs", "1"])

    assert campaign_runner.main() == 1
    output = capsys.readouterr().out
    assert json.loads(output) == {
        "issue_codes": ["dependency_environment_invalid"],
        "status": "CAPSULE_PREFLIGHT_FAIL",
    }
    assert sensitive_failure not in output
    assert "/private/alice" not in output
    assert "secret-token" not in output


def test_reproduction_provisions_the_recorded_historical_commit(
    tmp_path: Path,
    monkeypatch,
) -> None:
    recorded_commit = "b" * 40
    campaign = tmp_path / "historical-campaign"
    campaign.mkdir()
    (campaign / "environment.json").write_text(
        json.dumps({"source": {"commit": recorded_commit}}),
        encoding="utf-8",
    )
    captured: dict[str, str] = {}

    def run_in_capsule(_args, *, campaign_id: str, expected_commit: str) -> int:
        captured.update(
            campaign_id=campaign_id,
            expected_commit=expected_commit,
        )
        return 73

    monkeypatch.delenv("AGENTLOOM_MEMORY_CAMPAIGN_CAPSULE_ACTIVE", raising=False)
    monkeypatch.setattr(campaign_runner, "_run_in_capsule", run_in_capsule)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "memory-campaign",
            "--reproduce-campaign",
            str(campaign),
            "--run-id",
            "historical-run",
        ],
    )

    assert campaign_runner.main() == 73
    assert captured == {"campaign_id": "", "expected_commit": recorded_commit}


def test_current_launcher_rejects_historical_runner_before_provision(
    monkeypatch,
) -> None:
    calls: list[str] = []
    monkeypatch.setattr(
        campaign_runner,
        "_require_isolated_git_metadata",
        lambda _root: calls.append("metadata"),
    )
    monkeypatch.setattr(
        campaign_runner,
        "trusted_control_plane_matches",
        lambda *_args: calls.append("control-plane") or False,
    )
    monkeypatch.setattr(
        campaign_runner,
        "provision_capsule",
        lambda *_args, **_kwargs: pytest.fail("untrusted runner reached provision"),
    )

    assert campaign_runner._run_in_capsule(
        object(),
        campaign_id="",
        expected_commit="a" * 40,
    ) == 1
    assert calls == ["metadata", "control-plane"]


def test_root_capsule_python_sets_exact_pycache_prefix_despite_isolation(
    tmp_path: Path,
    monkeypatch,
) -> None:
    root = tmp_path / "private" / "checkout"
    root.mkdir(parents=True)
    python = root / ".venv" / "bin" / "python"
    runner = root / "applications" / "runner.py"
    prefix = root / ".venv" / ".agentloom-pycache"
    capsule = SimpleNamespace(
        root=root,
        python=python,
        runner=runner,
        env={
            "PYTHONPYCACHEPREFIX": str(prefix),
            "PYTHONDONTWRITEBYTECODE": "1",
        },
        model_config_bytes=b"summary: {}\n",
    )
    captured: dict[str, object] = {}

    class CapsuleContext:
        def __enter__(self):
            return capsule

        def __exit__(self, *_args):
            return False

    def run(command, **kwargs):
        captured["command"] = command
        captured["env"] = kwargs["env"]
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr(
        campaign_runner,
        "_require_isolated_git_metadata",
        lambda _root: [],
    )
    monkeypatch.setattr(
        campaign_runner,
        "trusted_control_plane_matches",
        lambda *_args: True,
    )
    monkeypatch.setattr(
        campaign_runner,
        "provision_capsule",
        lambda *_args, **_kwargs: CapsuleContext(),
    )
    monkeypatch.setattr(campaign_runner.subprocess, "run", run)
    args = SimpleNamespace(
        reproduce_campaign=None,
        runs=5,
        max_workers=1,
        timeout_seconds=600,
        output_root=tmp_path / "output",
    )

    assert campaign_runner._run_in_capsule(
        args,
        campaign_id="test-campaign",
        expected_commit="a" * 40,
    ) == 0
    assert captured["command"][:7] == [
        str(python),
        "-I",
        "-P",
        "-B",
        "-X",
        f"pycache_prefix={prefix}",
        str(runner),
    ]
    assert captured["env"]["PYTHONPYCACHEPREFIX"] == str(prefix)


def test_plan_audit_rejects_dataset_manifest_substitution() -> None:
    specs = select_runs(5)
    plan = {
        "schema_version": 2,
        "requested_runs": 5,
        "max_concurrency": 1,
        "cli_contract": "loom run <workflow>",
        "review_approval_contract": (
            "edit scoped INBOX.md then loom reviews apply --application <application-id>"
        ),
        "dataset": {"files": []},
        "runs": [spec.to_dict() for spec in specs],
    }

    assert audit_module._plan_issues(plan) == [
        "dataset manifest does not match the canonical campaign dataset"
    ]


def test_provider_protocol_empty_response_counter_uses_real_error_shape() -> None:
    raw = """
[INFO] fixture says Empty or whitespace-only model output.
[ERROR] Error while parsing tool call from model output: Empty or whitespace-only model
output.
[2026-07-15
21:29:38][task:...][agent:test][ERROR] Error while parsing tool call from model
output: Empty or whitespace-only model
output.
[2026-07-15][agent:memory_reviewer][INFO] [Step 1] Current tokens: 100
Error while parsing tool call from model output: Empty or whitespace-only model
output.
[ERROR] Reached max steps.
"""

    assert campaign_runner._provider_protocol_empty_response_count(raw) == 3
    assert (
        campaign_runner._provider_protocol_empty_response_count(
            "[INFO] Error while parsing tool call from model output: "
            "Empty or whitespace-only model output."
        )
        == 0
    )
    for prefix in ("ERROR: ", "ERROR reviewer: ", "Execution failed: "):
        assert campaign_runner._provider_protocol_empty_response_count(
            prefix
            + "Error while parsing tool call from model output: "
            + "Empty or whitespace-only model output."
        ) == 1


def test_recovered_provider_empty_responses_are_counted_without_failing_memory_semantics() -> None:
    oracle = indexed_rows(ORACLE_PATH)
    spec = next(
        item for item in build_full_plan() if item.run_id == "off-durable-00-writer"
    )
    final = {
        "returncode": 0,
        "privacy_findings": [],
        "provider_protocol_empty_responses": 0,
        "model_evidence": _review_off_evidence(),
        "database": _empty_v6_database(),
    }
    result = {
        **spec.to_dict(),
        "status": "completed",
        "attempts": [
            {
                **final,
                "provider_protocol_empty_responses": 1,
            },
        ],
        "final": {
            **final,
            "provider_protocol_empty_responses": 1,
        },
    }

    audit = evaluate_results(
        [spec],
        [result],
        oracle,
        require_complete=True,
    )

    assert audit["ok"] is True
    assert audit_module._provider_protocol_empty_count([result]) == 1
    assert audit_module._provider_protocol_empty_run_ids([result]) == [
        "off-durable-00-writer"
    ]


def test_run_group_keeps_independent_later_phases_after_one_phase_fails(
    monkeypatch,
    tmp_path,
) -> None:
    specs = [
        RunSpec(
            "scope-same",
            "scope-case",
            "application_scope",
            "same_recall",
            2,
            "same.yaml",
            "scope-state",
            True,
        ),
        RunSpec(
            "scope-cross",
            "scope-case",
            "application_scope",
            "cross_recall",
            3,
            "cross.yaml",
            "scope-state",
            False,
        ),
    ]
    calls: list[str] = []

    def fake_run_spec(spec, **_kwargs):
        calls.append(spec.run_id)
        return {
            **spec.to_dict(),
            "status": "failed" if spec.run_id == "scope-same" else "completed",
            "attempts": [],
            "final": {},
        }

    monkeypatch.setattr(campaign_runner, "_run_spec", fake_run_spec)

    results = campaign_runner._run_group(
        specs,
        campaign_dir=tmp_path,
        timeout_seconds=1,
        oracle={"scope-case": {}},
        markers=[],
    )

    assert calls == ["scope-same", "scope-cross"]
    assert [result["run_id"] for result in results] == calls


def test_run_group_executes_every_planned_application_after_writer_failure(
    monkeypatch,
    tmp_path,
) -> None:
    specs = [
        RunSpec("writer", "case", "review_on_durable", "writer", 1, "writer.yaml", "state", True),
        RunSpec("recall", "case", "review_on_durable", "recall", 0, "recall.yaml", "state", True),
    ]
    calls: list[str] = []

    def fake_run_spec(spec, **_kwargs):
        calls.append(spec.run_id)
        return {
            **spec.to_dict(),
            "status": "failed" if spec.phase == "writer" else "completed",
            "attempts": [],
            "final": {},
        }

    monkeypatch.setattr(campaign_runner, "_run_spec", fake_run_spec)

    campaign_runner._run_group(
        specs,
        campaign_dir=tmp_path,
        timeout_seconds=1,
        oracle={"case": {}},
        markers=[],
    )

    assert calls == ["writer", "recall"]


def test_availability_failures_are_governed_by_aggregate_release_gates() -> None:
    assert "application_failed" not in audit_module._HARD_CODES
    assert "provider_required_tool_call_empty" not in audit_module._HARD_CODES
    assert "review_failed" not in audit_module._HARD_CODES
    assert {
        "application_scope_leak",
        "database_integrity",
        "privacy_marker",
        "progress_persisted",
        "review_call_limit_exceeded",
        "security_persisted",
        "unverified_claim_persisted",
    } <= audit_module._HARD_CODES


def test_reviewer_completion_gate_requires_95_percent_of_eligible_runs() -> None:
    def result(index: int, status: str) -> dict:
        return {
            "run_id": f"review-{index}",
            "status": "completed",
            "final": {
                "returncode": 0,
                "completion_marker_seen": True,
                "model_evidence": {
                    "review_batch_delta": [
                        {"status": status, "result": {"status": status}}
                    ],
                }
            },
        }

    specs = [
        RunSpec(
            f"review-{index}",
            "case",
            "scenario",
            "writer",
            1,
            "workflow",
            f"state-{index}",
            True,
        )
        for index in range(100)
    ]
    completed_94 = [result(index, "completed") for index in range(94)] + [
        result(index, "failed") for index in range(94, 100)
    ]
    completed_95 = [result(index, "completed") for index in range(95)] + [
        result(index, "failed") for index in range(95, 100)
    ]

    assert audit_module._review_completion_gate(specs, completed_94) is False
    assert audit_module._review_completion_gate(specs, completed_95) is True


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
            "scope": "application",
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
        "schema_version": 2,
        "campaign_id": "contract",
        "dry_run": True,
        "requested_runs": 5,
        "selected_runs": 5,
        "max_concurrency": 2,
        "cli_contract": "loom run <workflow>",
        "review_approval_contract": (
            "edit scoped INBOX.md then loom reviews apply --application <application-id>"
        ),
        "dataset": dataset_manifest(),
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
    removed_file_logging_flag = "--log" + "-to-file"
    for name in ("run_memory_review_campaign.py", "audit_memory_review_campaign.py"):
        source = (APP_ROOT / "scripts" / name).read_text(encoding="utf-8")
        assert "from src." not in source
        assert "import src." not in source
        assert removed_file_logging_flag not in source


def test_canary_continues_past_soft_semantic_misses_but_not_hard_failures() -> None:
    soft = {
        "complete": True,
        "issues": [{"code": "expected_memory_missing", "hard": False}],
    }
    hard = {
        "complete": True,
        "issues": [{"code": "application_failed", "hard": True}],
    }
    failed_application = {
        "complete": True,
        "issues": [{"code": "application_failed", "hard": False}],
    }
    recovered_timeout = [
        {
            "attempts": [
                {"timed_out": True, "returncode": 124},
                {"timed_out": False, "returncode": 0},
            ]
        }
    ]

    assert _canary_can_continue(soft) is True
    assert _canary_can_continue(hard) is False
    assert _canary_can_continue(failed_application) is False
    assert _canary_can_continue(soft, results=recovered_timeout) is False
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


def test_campaign_artifact_sanitizer_redacts_generic_credentials() -> None:
    raw = (
        "api_key=short\n"
        "access_token: value with spaces\n"
        "secret_key='quoted value'\n"
        "credential: nested-value\n"
        "Authorization: Bearer abc.def-123\n"
        "message contains Bearer standalone-token"
    )

    sanitized = campaign_runner._sanitize_value(
        {
            "output": raw,
            "nested": {"api_key": "dict-secret", "token_count": 42},
        },
        [],
    )
    rendered = json.dumps(sanitized, sort_keys=True)

    for secret in (
        "short",
        "value with spaces",
        "quoted value",
        "nested-value",
        "abc.def-123",
        "standalone-token",
        "dict-secret",
    ):
        assert secret not in rendered
    assert sanitized["nested"]["api_key"] == "[REDACTED]"
    assert sanitized["nested"]["token_count"] == 42


def test_campaign_text_sanitizer_handles_prefixed_unicode_credentials() -> None:
    raw = (
        "OPENAI_API_KEY=provider-secret\n"
        "aws_secret_access_key: aws-secret\n"
        "My_Password = password-secret\n"
        "ＯＰＥＮＡＩ＿ＡＰＩ＿ＫＥＹ=unicode-secret\n"
        "openAiApiKey: camel-secret\n"
        "message: retry failed OPENAI_API_KEY=shadow-secret\n"
        "sort_key=ascending\n"
        "token_count=42\n"
        "monkey=banana"
    )

    sanitized = _sanitize_text(raw, [])

    for secret in (
        "provider-secret",
        "aws-secret",
        "password-secret",
        "unicode-secret",
        "camel-secret",
        "shadow-secret",
    ):
        assert secret not in sanitized
    assert "sort_key=ascending" in sanitized
    assert "token_count=42" in sanitized
    assert "monkey=banana" in sanitized


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


def test_privacy_audit_collects_every_retry_attempt() -> None:
    leaked = {"path": "attempt-1", "kind": "secret", "case_id": "security-01"}
    results = [
        {
            "attempts": [
                {"privacy_findings": [leaked]},
                {"privacy_findings": []},
            ],
            "final": {"privacy_findings": []},
        }
    ]

    assert audit_module._result_privacy_findings(results) == [leaked]


def test_review_telemetry_parser_joins_terminal_wrapped_lines() -> None:
    evidence = _model_evidence(
        "[2026-07-14][INFO] Self-learning review: enabled=true requested=summary\n"
        "resolved=fake/summary calls=1 actions=1\n"
        "status=completed\n"
        "[2026-07-14][INFO] done\n"
    )

    assert evidence["review_call_count"] == 1
    assert evidence["review_action_count"] == 1
    assert evidence["review_records"] == [
        {
            "enabled": "true",
            "requested": "summary",
            "resolved": "fake/summary",
            "calls": "1",
            "actions": "1",
            "status": "completed",
        }
    ]


def test_inbox_decision_writer_updates_one_real_v6_document(tmp_path: Path) -> None:
    inbox = tmp_path / "INBOX.md"
    document = {
        "scope_type": "application",
        "scope_id": "memory_feature_validation/variants/approval",
        "decisions": [
            {"candidate_id": "candidate-target", "revision": 3, "decision": "pending"},
            {"candidate_id": "candidate-other", "revision": 1, "decision": "pending"},
        ],
    }
    inbox.write_text(
        "# Application review inbox\n\n"
        "<!-- agentloom-decisions:start -->\n```json\n"
        + json.dumps(document, indent=2)
        + "\n```\n<!-- agentloom-decisions:end -->\n",
        encoding="utf-8",
    )

    _write_inbox_decision(
        inbox,
        application_id="memory_feature_validation/variants/approval",
        candidate_id="candidate-target",
        revision=3,
        decision="approve",
    )

    match = campaign_runner._INBOX_DECISION_BLOCK_RE.search(
        inbox.read_text(encoding="utf-8")
    )
    assert match is not None
    updated = json.loads(match.group(2))
    assert updated["scope_type"] == "application"
    assert updated["scope_id"] == "memory_feature_validation/variants/approval"
    assert [row["decision"] for row in updated["decisions"]] == [
        "approve",
        "pending",
    ]


def test_black_box_snapshot_reads_canonical_v6_memory_and_review_state(
    tmp_path: Path,
) -> None:
    db = tmp_path / "self_learning.db"
    with sqlite3.connect(db) as conn:
        conn.executescript(
            """
            CREATE TABLE memory_items(
                id INTEGER PRIMARY KEY, scope_type TEXT, scope_id TEXT,
                kind TEXT, memory_key TEXT, payload_json TEXT, payload_hash TEXT,
                state TEXT, activation_source TEXT, provenance_json TEXT,
                revision INTEGER, source_review_id TEXT, supersedes_id INTEGER,
                created_at TEXT, updated_at TEXT
            );
            CREATE TABLE review_candidates(
                candidate_id TEXT PRIMARY KEY, review_id TEXT, scope_type TEXT,
                scope_id TEXT, kind TEXT, memory_key TEXT, payload_json TEXT,
                payload_hash TEXT, proposed_action TEXT, approval TEXT, state TEXT,
                outcome TEXT, revision INTEGER, target_item_id INTEGER,
                provenance_json TEXT, source_run_ids_json TEXT,
                gate_reasons_json TEXT, reason TEXT, created_at TEXT,
                resolved_at TEXT
            );
            CREATE TABLE review_batches(
                review_id TEXT PRIMARY KEY, scope_type TEXT, scope_id TEXT,
                status TEXT, dry_run INTEGER, result_json TEXT,
                created_at TEXT, finished_at TEXT
            );
            CREATE TABLE review_batch_runs(
                review_id TEXT, root_run_id TEXT, application_id TEXT
            );
            CREATE TABLE runs(
                run_id TEXT PRIMARY KEY, root_run_id TEXT, status TEXT
            );
            """
        )
        conn.execute(
            "INSERT INTO memory_items VALUES(1,'application','app','fact','limit',?,"
            "'h','active_unreviewed','auto','[]',1,'review-1',NULL,'t','t')",
            (json.dumps({"text": "active fact"}),),
        )
        conn.execute(
            "INSERT INTO review_candidates VALUES('candidate-1','review-1',"
            "'application','app','fact','pending',?,'hp','add','manual',"
            "'pending_pre_review','pending',1,NULL,'[]','[\"completed-root\"]',"
            "'[]','manual_approval_required','t',NULL)",
            (json.dumps({"text": "pending fact"}),),
        )
        conn.execute(
            "INSERT INTO review_batches VALUES('review-1','application','app',"
            "'completed',0,?,'t','t')",
            (
                json.dumps(
                    {
                        "review_id": "review-1",
                        "status": "completed",
                        "candidates": [{"candidate_id": "candidate-1"}],
                    }
                ),
            ),
        )
        conn.execute(
            "INSERT INTO review_batch_runs VALUES('review-1','completed-root','app')"
        )
        conn.executemany(
            "INSERT INTO runs VALUES(?, ?, ?)",
            [
                ("completed-root", "completed-root", "completed"),
                ("failed-root", "failed-root", "failed"),
            ],
        )

    snapshot = _db_snapshot(db, [])

    assert snapshot["memory_items"][0]["state"] == "active_unreviewed"
    assert snapshot["memory_items"][0]["content"] == "active fact"
    assert snapshot["review_candidates"][0]["state"] == "pending_pre_review"
    assert snapshot["review_candidates"][0]["content"] == "pending fact"
    assert snapshot["review_batches"][0]["source_runs"] == [
        {"root_run_id": "completed-root", "application_id": "app"}
    ]
    assert snapshot["run_status_counts"] == {"completed": 1, "failed": 1}
    assert snapshot["root_identity_valid"] is True
    assert snapshot["completed_root_run_ids"] == ["completed-root"]


def test_black_box_snapshot_rejects_implicit_run_id_as_root(tmp_path: Path) -> None:
    db = tmp_path / "legacy-root-fallback.db"
    with sqlite3.connect(db) as conn:
        conn.execute("CREATE TABLE runs(run_id TEXT PRIMARY KEY, status TEXT)")
        conn.execute("INSERT INTO runs VALUES('implicit-root', 'completed')")

    snapshot = _db_snapshot(db, [])

    assert snapshot["root_identity_valid"] is False
    assert snapshot["completed_root_run_ids"] == []


def test_black_box_snapshot_requires_a_completed_root_owner_row(tmp_path: Path) -> None:
    db = tmp_path / "missing-root-owner.db"
    with sqlite3.connect(db) as conn:
        conn.execute(
            "CREATE TABLE runs(run_id TEXT PRIMARY KEY, root_run_id TEXT, status TEXT)"
        )
        conn.execute(
            "INSERT INTO runs VALUES('worker-leaf', 'missing-owner', 'completed')"
        )

    snapshot = _db_snapshot(db, [])

    assert snapshot["completed_root_run_ids"] == ["missing-owner"]
    assert snapshot["root_identity_valid"] is False


def test_black_box_snapshot_reads_committed_wal_state(tmp_path: Path) -> None:
    db = tmp_path / "self_learning.db"
    with sqlite3.connect(db) as writer:
        writer.execute("PRAGMA journal_mode = WAL")
        writer.execute("PRAGMA wal_autocheckpoint = 0")
        writer.execute(
            "CREATE TABLE memory_items("
            "id INTEGER PRIMARY KEY, scope_type TEXT, scope_id TEXT, "
            "kind TEXT, memory_key TEXT, payload_json TEXT, state TEXT)"
        )
        writer.execute(
            "INSERT INTO memory_items VALUES(1,'project','project','fact','wal',?,"
            "'active_confirmed')",
            (json.dumps({"text": "wal fact"}),),
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
import json
import os
import sqlite3
import sys

conn = sqlite3.connect(sys.argv[1])
conn.execute("PRAGMA journal_mode = WAL")
conn.execute("PRAGMA wal_autocheckpoint = 0")
conn.execute(
    "CREATE TABLE memory_items("
    "id INTEGER PRIMARY KEY, scope_type TEXT, scope_id TEXT, "
    "kind TEXT, memory_key TEXT, payload_json TEXT, state TEXT)"
)
conn.execute(
    "INSERT INTO memory_items VALUES(1,'project','project','fact','recovered',?,"
    "'active_confirmed')",
    (json.dumps({"text": "recovered fact"}),),
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


def test_review_batch_delta_uses_immutable_review_id() -> None:
    before = {"review_batches": [{"review_id": "review-old", "status": "completed"}]}
    after = {
        "review_batches": [
            {"review_id": "review-old", "status": "completed"},
            {"review_id": "review-new", "status": "completed"},
        ]
    }

    assert campaign_runner._review_batch_delta(before, after) == [
        {"review_id": "review-new", "status": "completed"}
    ]
    assert campaign_runner._review_batch_delta(before, before) == []


def test_table_effect_count_detects_add_update_and_delete() -> None:
    before = {
        "memory_items": [
            {"id": 1, "content": "unchanged"},
            {"id": 2, "content": "old"},
            {"id": 3, "content": "deleted"},
        ],
    }
    after = {
        "memory_items": [
            {"id": 1, "content": "unchanged"},
            {"id": 2, "content": "new"},
            {"id": 4, "content": "added"},
        ],
    }

    assert campaign_runner._table_effect_count(
        before,
        after,
        table="memory_items",
        identity_field="id",
    ) == 3


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
            "model_evidence": _review_off_evidence(),
            "database": _empty_v6_database(),
        },
    }

    assert evaluate_results([spec], [result], oracle, require_complete=True)["ok"] is True

    result["final"]["model_evidence"]["review_batch_delta"] = [
        {"review_id": "should-not-exist", "status": "completed"}
    ]
    audit = evaluate_results([spec], [result], oracle, require_complete=True)
    assert audit["ok"] is False
    assert {issue["code"] for issue in audit["issues"]} == {"review_off_called"}


def test_review_off_treats_absent_reviewer_entry_as_zero_calls() -> None:
    """Opt-out means the reviewer never starts, so no telemetry is success."""
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
            "model_evidence": _review_off_evidence(),
            "database": _empty_v6_database(),
        },
    }

    audit = evaluate_results([spec], [result], oracle, require_complete=True)

    assert audit["issues"] == []


def test_review_on_still_rejects_absent_reviewer_evidence() -> None:
    """The no-entry success rule applies only to an explicit review opt-out."""
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
                "review_call_count": None,
                "review_action_count": 0,
                "review_records": [],
                "memory_item_effect_count": 0,
                "review_candidate_effect_count": 0,
                "review_batch_delta": [],
            },
            "database": _empty_v6_database(),
        },
    }

    audit = evaluate_results([spec], [result], oracle, require_complete=True)

    assert [(issue["code"], issue["hard"]) for issue in audit["issues"]] == [
        ("review_evidence_missing", True)
    ]


def test_release_audit_requires_exactly_one_after_run_reviewer_call() -> None:
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
                "review_call_count": 2,
                "review_action_count": 1,
                "review_records": [
                    {
                        "status": "completed", "calls": "2", "actions": "1"
                    }
                ],
                "memory_item_effect_count": 1,
                "review_candidate_effect_count": 1,
                "completed_root_run_ids": ["root-1"],
                "completed_run_count_delta": 1,
                "review_batch_delta": [
                    {
                        "review_id": "review-1",
                        "scope_type": "application",
                        "scope_id": "memory_feature_validation/variants/on",
                        "status": "completed",
                        "source_runs": [
                            {
                                "root_run_id": "root-1",
                                "application_id": "memory_feature_validation/variants/on",
                            }
                        ],
                        "result": {
                            "status": "completed",
                            "candidates": [
                                {
                                    "candidate_id": "candidate-1",
                                    "state": "active_unreviewed",
                                }
                            ],
                        },
                    }
                ],
            },
            "database": {
                "integrity": "ok",
                "memory_items": [
                    {
                        "state": "active_unreviewed",
                        "scope_type": "application",
                        "scope_id": "memory_feature_validation/variants/on",
                        "content": (
                            "The maximum page size is 250 records and every "
                            "additional page uses next_page_token."
                        ),
                    }
                ],
                "review_candidates": [
                    {
                        "candidate_id": "candidate-1",
                        "state": "active_unreviewed",
                    }
                ],
                "review_batches": [],
                "review_batch_runs": [],
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


def test_writer_audit_requires_exact_oracle_evidence_bytes() -> None:
    oracle = indexed_rows(ORACLE_PATH)
    spec = next(
        item for item in build_full_plan()
        if item.run_id == "on-durable-00-writer"
    )
    application_id = campaign_common.workflow_application_id(spec)
    wrong = "Page limit: 250. THIS IS NOT THE EXACT TRUSTED EVIDENCE."
    candidate = _candidate_row(
        wrong,
        application_id=application_id,
        state="active_unreviewed",
    )
    result = _v6_result(
        spec,
        memory_items=[_fact_row(wrong, application_id=application_id)],
        candidates=[candidate],
        review_candidates=[candidate],
        actions=1,
        memory_effects=1,
        candidate_effects=1,
    )

    evaluated = evaluate_results(
        [spec], [result], oracle, require_complete=True
    )

    assert "exact_evidence_mismatch" in {
        issue["code"] for issue in evaluated["issues"]
    }


def test_release_first_attempt_gate_requires_at_least_95_percent() -> None:
    def completed(attempt_count: int) -> dict:
        attempts = [
            {
                "returncode": 0,
                "completion_marker_seen": True,
            }
            for _ in range(attempt_count)
        ]
        return {
            "status": "completed",
            "attempts": attempts,
            "final": attempts[-1],
        }

    first_attempt_94 = [
        completed(1)
        for _ in range(94)
    ] + [
        completed(2)
        for _ in range(6)
    ]
    first_attempt_95 = [
        completed(1)
        for _ in range(95)
    ] + [
        completed(2)
        for _ in range(5)
    ]

    assert _first_attempt_gate(first_attempt_94, selected_runs=100) is False
    assert _first_attempt_gate(first_attempt_95, selected_runs=100) is True


def test_release_timing_gate_uses_persisted_eight_hour_envelope() -> None:
    plan = {
        "requested_runs": 100,
        "campaign_started_at": "2026-07-16T00:00:00+00:00",
    }
    environment = {
        "campaign_started_at": "2026-07-16T00:00:00+00:00",
    }
    completed = {
        "campaign_finished_at": "2026-07-16T08:00:00+00:00",
    }
    results = [
        {
            "run_id": "boundary",
            "attempts": [
                {
                    "started_at": "2026-07-16T00:00:01+00:00",
                    "finished_at": "2026-07-16T07:59:59+00:00",
                }
            ],
        }
    ]

    assert audit_module._campaign_timing_issues(
        plan,
        environment,
        completed,
        results,
    ) == []

    too_slow = {
        "campaign_finished_at": "2026-07-16T08:00:00.000001+00:00",
    }
    assert audit_module._campaign_timing_issues(
        plan,
        environment,
        too_slow,
        results,
    ) == ["100-run campaign exceeded the eight-hour release limit"]


def test_release_timing_gate_cross_checks_attempt_endpoints() -> None:
    plan = {
        "requested_runs": 100,
        "campaign_started_at": "2026-07-16T00:00:00+00:00",
    }
    environment = {
        "campaign_started_at": "2026-07-16T00:00:00+00:00",
    }
    completed = {
        "campaign_finished_at": "2026-07-16T01:00:00+00:00",
    }
    results = [
        {
            "run_id": "outside-envelope",
            "attempts": [
                {
                    "started_at": "2026-07-16T00:59:00+00:00",
                    "finished_at": "2026-07-16T01:00:01+00:00",
                }
            ],
        }
    ]

    assert audit_module._campaign_timing_issues(
        plan,
        environment,
        completed,
        results,
    ) == ["Application attempt timestamps escaped the campaign envelope"]


def test_campaign_audit_rejects_non_transport_retry_and_more_than_two_attempts() -> None:
    semantic_retry = {
        "status": "completed",
        "attempts": [
            {
                "returncode": 1,
                "completion_marker_seen": False,
                "retryable_transport": False,
                "transport_failure_reason": None,
                "timed_out": False,
            },
            {
                "returncode": 0,
                "completion_marker_seen": True,
                "retryable_transport": False,
                "transport_failure_reason": None,
                "timed_out": False,
            },
        ]
    }
    semantic_retry["final"] = semantic_retry["attempts"][-1]
    excessive_retry = {
        "attempts": [
            {"returncode": 1, "retryable_transport": True},
            {"returncode": 1, "retryable_transport": True},
            {"returncode": 0, "retryable_transport": False},
        ]
    }
    excessive_retry["final"] = excessive_retry["attempts"][-1]

    assert audit_module._attempt_contract_issues(semantic_retry) == [
        "a retry was used after a non-transport failure"
    ]
    assert audit_module._attempt_contract_issues(excessive_retry) == [
        "Application used more than one infrastructure retry"
    ]

    incomplete_retry = json.loads(json.dumps(semantic_retry))
    del incomplete_retry["attempts"][0]["completion_marker_seen"]
    assert audit_module._attempt_contract_issues(incomplete_retry) == [
        "Application attempt 1 completion marker evidence was missing"
    ]

    forged_retry = json.loads(json.dumps(semantic_retry))
    forged_retry["attempts"][0].update(
        {
            "retryable_transport": True,
            "transport_failure_reason": "provider_tempfail",
            "timed_out": False,
        }
    )
    assert audit_module._attempt_contract_issues(forged_retry) == [
        "Application attempt 1 transport evidence contradicted process status"
    ]

    impossible_success = {
        "status": "completed",
        "attempts": [
            {
                "returncode": 0,
                "completion_marker_seen": True,
                "retryable_transport": True,
                "transport_failure_reason": "subprocess_timeout",
                "timed_out": True,
            }
        ],
    }
    impossible_success["final"] = impossible_success["attempts"][0]
    assert audit_module._attempt_contract_issues(impossible_success) == [
        "Application attempt 1 transport evidence contradicted process status"
    ]

    provider_tempfail = {
        "status": "failed",
        "attempts": [
            {
                "returncode": 75,
                "completion_marker_seen": False,
                "retryable_transport": True,
                "transport_failure_reason": "provider_tempfail",
                "timed_out": False,
            }
        ],
    }
    provider_tempfail["final"] = provider_tempfail["attempts"][0]
    assert audit_module._attempt_contract_issues(provider_tempfail) == []

    missing_reason = json.loads(json.dumps(provider_tempfail))
    del missing_reason["attempts"][0]["transport_failure_reason"]
    missing_reason["final"] = missing_reason["attempts"][0]
    assert audit_module._attempt_contract_issues(missing_reason) == [
        "Application attempt 1 transport reason was missing"
    ]

    forged_log_authorization = {
        "status": "failed",
        "attempts": [
            {
                "returncode": 1,
                "completion_marker_seen": False,
                "retryable_transport": True,
                "transport_failure_reason": "provider_tempfail",
                "timed_out": False,
                "stdout": "APITimeout",
            }
        ],
    }
    forged_log_authorization["final"] = forged_log_authorization["attempts"][0]
    assert audit_module._attempt_contract_issues(forged_log_authorization) == [
        "Application attempt 1 transport evidence contradicted process status"
    ]


def test_campaign_retries_once_for_typed_provider_tempfail(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    spec = build_full_plan()[0]
    outputs = [
        SimpleNamespace(returncode=75, stdout="provider unavailable"),
        SimpleNamespace(
            returncode=0,
            stdout="Execution completed successfully.\n{'answer': 'ok'}",
        ),
    ]
    restores: list[Path] = []
    commands: list[list[str]] = []

    def run(command, **_kwargs):
        commands.append(command)
        return outputs.pop(0)

    monkeypatch.setattr(campaign_runner, "capsule_is_active", lambda: False)
    monkeypatch.setattr(campaign_runner, "_loom", lambda: "loom")
    monkeypatch.setattr(campaign_runner, "_db_snapshot", lambda *_args: {})
    monkeypatch.setattr(campaign_runner, "_privacy_findings", lambda *_args: [])
    monkeypatch.setattr(campaign_runner, "_snapshot_state", lambda *_args: None)
    monkeypatch.setattr(
        campaign_runner,
        "_restore_state",
        lambda _state, backup: restores.append(backup),
    )
    monkeypatch.setattr(
        campaign_runner.subprocess,
        "run",
        run,
    )
    state_root = tmp_path / "state"
    state_root.mkdir()

    result = campaign_runner._run_spec(
        spec,
        campaign_dir=tmp_path,
        state_root=state_root,
        timeout_seconds=30,
        oracle={},
        markers=[],
    )

    assert [attempt["returncode"] for attempt in result["attempts"]] == [75, 0]
    assert result["attempts"][0]["retryable_transport"] is True
    assert (
        result["attempts"][0]["transport_failure_reason"]
        == "provider_tempfail"
    )
    assert len(restores) == 1
    assert outputs == []
    expected_command = [
        "loom",
        "run",
        str((REPO_ROOT / spec.workflow).resolve()),
    ]
    assert commands == [expected_command, expected_command]
    assert [attempt["command"] for attempt in result["attempts"]] == [
        ["loom", "run", spec.workflow],
        ["loom", "run", spec.workflow],
    ]


def test_campaign_does_not_parse_model_logs_to_authorize_retry(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    spec = build_full_plan()[0]
    process_calls = 0

    def semantic_failure(*_args, **_kwargs):
        nonlocal process_calls
        process_calls += 1
        return SimpleNamespace(
            returncode=1,
            stdout="APITimeout RateLimitError ServiceUnavailableError",
        )

    monkeypatch.setattr(campaign_runner, "capsule_is_active", lambda: False)
    monkeypatch.setattr(campaign_runner, "_loom", lambda: "loom")
    monkeypatch.setattr(campaign_runner, "_db_snapshot", lambda *_args: {})
    monkeypatch.setattr(campaign_runner, "_privacy_findings", lambda *_args: [])
    monkeypatch.setattr(campaign_runner, "_snapshot_state", lambda *_args: None)
    monkeypatch.setattr(campaign_runner.subprocess, "run", semantic_failure)
    state_root = tmp_path / "state"
    state_root.mkdir()

    result = campaign_runner._run_spec(
        spec,
        campaign_dir=tmp_path,
        state_root=state_root,
        timeout_seconds=30,
        oracle={},
        markers=[],
    )

    assert len(result["attempts"]) == 1
    assert result["final"]["retryable_transport"] is False
    assert result["final"]["transport_failure_reason"] is None
    assert process_calls == 1


def test_campaign_does_not_retry_successful_root_when_review_log_reports_failure(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    spec = build_full_plan()[0]
    process_calls = 0

    def successful_root(*_args, **_kwargs):
        nonlocal process_calls
        process_calls += 1
        return SimpleNamespace(
            returncode=0,
            stdout=(
                "review failed: provider unavailable\n"
                "Execution completed successfully.\n{'answer': 'ok'}"
            ),
        )

    monkeypatch.setattr(campaign_runner, "capsule_is_active", lambda: False)
    monkeypatch.setattr(campaign_runner, "_loom", lambda: "loom")
    monkeypatch.setattr(campaign_runner, "_db_snapshot", lambda *_args: {})
    monkeypatch.setattr(campaign_runner, "_privacy_findings", lambda *_args: [])
    monkeypatch.setattr(campaign_runner, "_snapshot_state", lambda *_args: None)
    monkeypatch.setattr(campaign_runner.subprocess, "run", successful_root)
    state_root = tmp_path / "state"
    state_root.mkdir()

    result = campaign_runner._run_spec(
        spec,
        campaign_dir=tmp_path,
        state_root=state_root,
        timeout_seconds=30,
        oracle={},
        markers=[],
    )

    assert result["status"] == "completed"
    assert len(result["attempts"]) == 1
    assert result["final"]["retryable_transport"] is False
    assert result["final"]["transport_failure_reason"] is None
    assert process_calls == 1


@pytest.mark.parametrize(
    "attempt",
    [
        {
            "returncode": 75,
            "completion_marker_seen": False,
            "timed_out": True,
            "retryable_transport": True,
            "transport_failure_reason": "provider_tempfail",
        },
        {
            "returncode": 76,
            "completion_marker_seen": False,
            "timed_out": False,
            "retryable_transport": True,
            "transport_failure_reason": "provider_tempfail",
        },
        {
            "returncode": 124,
            "completion_marker_seen": False,
            "timed_out": True,
            "retryable_transport": True,
            "transport_failure_reason": "provider_tempfail",
        },
        {
            "returncode": 124,
            "completion_marker_seen": False,
            "timed_out": False,
            "retryable_transport": True,
            "transport_failure_reason": "subprocess_timeout",
        },
        {
            "returncode": 75,
            "completion_marker_seen": False,
            "timed_out": False,
            "retryable_transport": False,
            "transport_failure_reason": None,
        },
    ],
    ids=[
        "tempfail-plus-timeout",
        "untrusted-return-code",
        "timeout-wrong-reason",
        "return-124-without-timeout",
        "tempfail-not-retryable",
    ],
)
def test_campaign_audit_rejects_forged_transport_combinations(
    attempt: dict[str, object],
) -> None:
    result = {"status": "failed", "attempts": [attempt], "final": attempt}

    assert audit_module._attempt_contract_issues(result) == [
        "Application attempt 1 transport evidence contradicted process status"
    ]


def test_campaign_audit_accepts_typed_subprocess_timeout() -> None:
    attempt = {
        "returncode": 124,
        "completion_marker_seen": False,
        "timed_out": True,
        "retryable_transport": True,
        "transport_failure_reason": "subprocess_timeout",
    }
    result = {"status": "failed", "attempts": [attempt], "final": attempt}

    assert audit_module._attempt_contract_issues(result) == []


def test_campaign_audit_does_not_use_model_completion_marker_for_retry() -> None:
    attempt = {
        "returncode": 75,
        "completion_marker_seen": True,
        "timed_out": False,
        "retryable_transport": True,
        "transport_failure_reason": "provider_tempfail",
    }
    result = {"status": "failed", "attempts": [attempt], "final": attempt}

    assert audit_module._attempt_contract_issues(result) == []


def _v6_result(
    spec: RunSpec,
    *,
    memory_items: list[dict] | None = None,
    candidates: list[dict] | None = None,
    final_answer: object = None,
    review_candidates: list[dict] | None = None,
    actions: int = 0,
    memory_effects: int = 0,
    candidate_effects: int = 0,
) -> dict:
    memory_items = list(memory_items or [])
    candidates = list(candidates or [])
    application_id = campaign_common.workflow_application_id(spec)
    root_run_id = f"root-{spec.run_id}"
    if spec.review_expected:
        review_candidates = list(review_candidates or [])
        batch = {
            "review_id": f"review-{spec.run_id}",
            "scope_type": "application",
            "scope_id": application_id,
            "status": "completed",
            "source_runs": [
                {
                    "root_run_id": root_run_id,
                    "application_id": application_id,
                }
            ],
            "result": {
                "status": "completed",
                "candidates": review_candidates,
            },
        }
        evidence = {
            "review_call_count": 1,
            "review_action_count": actions,
            "review_records": [
                {"calls": "1", "actions": str(actions), "status": "completed"}
            ],
            "review_batch_delta": [batch],
            "memory_item_effect_count": memory_effects,
            "review_candidate_effect_count": candidate_effects,
            "completed_root_run_ids": [root_run_id],
            "completed_run_count_delta": 1,
        }
    else:
        evidence = _review_off_evidence(
            memory_effects=memory_effects,
            candidate_effects=candidate_effects,
        )
        if spec.scenario == "foreground_proposal" and spec.phase == "writer":
            evidence.update(
                {
                    "completed_root_run_ids": [root_run_id],
                    "completed_run_count_delta": 1,
                    "review_batch_delta": [
                        {
                            "review_id": f"proposal-{spec.run_id}",
                            "scope_type": "application",
                            "scope_id": application_id,
                            "status": "completed",
                            "source_runs": [
                                {
                                    "root_run_id": root_run_id,
                                    "application_id": application_id,
                                }
                            ],
                            "result": {
                                "status": "completed",
                                "candidates": candidates,
                            },
                        }
                    ],
                }
            )
    attempt = {
        "returncode": 0,
        "completion_marker_seen": True,
        "privacy_findings": [],
        "final_answer": final_answer,
        "model_evidence": evidence,
        "database": {
            **_empty_v6_database(),
            "memory_items": memory_items,
            "review_candidates": candidates,
        },
    }
    return {
        **spec.to_dict(),
        "status": "completed",
        "attempts": [attempt],
        "final": attempt,
    }


def _fact_row(
    content: str,
    *,
    application_id: str,
    state: str = "active_unreviewed",
) -> dict:
    return {
        "id": 1,
        "scope_type": "application",
        "scope_id": application_id,
        "kind": "fact",
        "memory_key": "campaign-fact",
        "payload": {"text": content},
        "content": content,
        "state": state,
    }


def _candidate_row(
    content: str,
    *,
    application_id: str,
    state: str = "pending_pre_review",
    candidate_id: str = "candidate-1",
    revision: int = 1,
) -> dict:
    return {
        "candidate_id": candidate_id,
        "review_id": "review-1",
        "scope_type": "application",
        "scope_id": application_id,
        "kind": "fact",
        "memory_key": "campaign-fact",
        "payload": {"text": content},
        "content": content,
        "state": state,
        "revision": revision,
        "resolved_at": "2026-07-18T00:00:00+00:00" if state != "pending_pre_review" else None,
    }


def _approval_post_result(case_id: str, decision: str) -> tuple[RunSpec, dict]:
    oracle = indexed_rows(ORACLE_PATH)
    spec = next(
        item
        for item in build_full_plan()
        if item.case_id == case_id and item.phase == "post_recall"
    )
    application_id = campaign_common.workflow_application_id(spec)
    content = oracle[case_id]["expected_content"]
    candidate_state = "active_confirmed" if decision == "approve" else "rejected"
    candidate = _candidate_row(
        content,
        application_id=application_id,
        state=candidate_state,
        revision=2,
    )
    memory = (
        [_fact_row(content, application_id=application_id, state="active_confirmed")]
        if decision == "approve"
        else []
    )
    result = _v6_result(
        spec,
        memory_items=memory,
        candidates=[candidate],
        final_answer=(oracle[case_id]["recall_value"] if decision == "approve" else "MISSING"),
    )
    result["approval_transition"] = {
        "ok": True,
        "readback_ok": True,
        "inbox_removed": True,
        "application_id": application_id,
        "decision": decision,
        "target": "candidate-1",
        "revision": 1,
        "result": {
            "command": ["loom", "reviews", "apply", "--application", application_id],
            "returncode": 0,
            "payload": {
                "applied": 1,
                "results": [{"candidate_id": "candidate-1"}],
            },
        },
    }
    return spec, result


def test_v6_reviewer_writer_activates_only_application_memory() -> None:
    oracle = indexed_rows(ORACLE_PATH)
    spec = next(
        item for item in build_full_plan() if item.run_id == "on-durable-00-writer"
    )
    application_id = campaign_common.workflow_application_id(spec)
    content = oracle[spec.case_id]["expected_content"]
    candidate = _candidate_row(
        content,
        application_id=application_id,
        state="active_unreviewed",
    )
    result = _v6_result(
        spec,
        memory_items=[_fact_row(content, application_id=application_id)],
        candidates=[candidate],
        review_candidates=[candidate],
        actions=1,
        memory_effects=1,
        candidate_effects=1,
    )

    assert evaluate_results([spec], [result], oracle, require_complete=True)["ok"] is True

    result["final"]["database"]["memory_items"][0]["scope_id"] = "sibling"
    result["final"]["database"]["review_candidates"][0]["scope_id"] = "sibling"
    issues = evaluate_results([spec], [result], oracle, require_complete=True)["issues"]
    assert "scope_mismatch" in {issue["code"] for issue in issues}


def test_foreground_proposal_stays_pending_and_is_not_recalled() -> None:
    oracle = indexed_rows(ORACLE_PATH)
    specs = {
        item.phase: item
        for item in build_full_plan()
        if item.case_id == "foreground-00"
    }
    writer = specs["writer"]
    recall = specs["recall"]
    application_id = campaign_common.workflow_application_id(writer)
    content = oracle[writer.case_id]["expected_content"]
    candidate = _candidate_row(content, application_id=application_id)
    writer_result = _v6_result(
        writer,
        candidates=[candidate],
        candidate_effects=1,
    )
    recall_result = _v6_result(
        recall,
        candidates=[candidate],
        final_answer="MISSING",
    )

    audit = evaluate_results(
        [writer, recall],
        [writer_result, recall_result],
        oracle,
        require_complete=True,
    )
    assert audit["ok"] is True


def test_project_promotion_guard_leaves_no_memory_and_cross_recall_missing() -> None:
    oracle = indexed_rows(ORACLE_PATH)
    specs = [
        item for item in build_full_plan() if item.case_id == "project-scope-00"
    ]
    results = [
        _v6_result(
            spec,
            final_answer="MISSING" if spec.phase == "cross_recall" else None,
        )
        for spec in specs
    ]

    audit = evaluate_results(specs, results, oracle, require_complete=True)
    assert audit["ok"] is True


@pytest.mark.parametrize(
    ("case_id", "decision", "candidate_state", "answer"),
    [
        ("approval-00", "approve", "active_confirmed", "export.completed.v3"),
        ("approval-03", "reject", "rejected", "MISSING"),
    ],
)
def test_approval_audit_uses_scoped_inbox_and_reviews_apply(
    case_id: str,
    decision: str,
    candidate_state: str,
    answer: str,
) -> None:
    oracle = indexed_rows(ORACLE_PATH)
    spec, result = _approval_post_result(case_id, decision)
    assert result["final"]["database"]["review_candidates"][0]["state"] == candidate_state
    assert audit_module._answer(result["final"]["final_answer"]) == answer

    audit = evaluate_results([spec], [result], oracle, require_complete=True)
    assert audit["ok"] is True


def test_approval_transition_edits_real_inbox_before_running_cli(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    application_id = "memory_feature_validation/variants/approval"
    content = "Completed exports publish to export.completed.v3."
    state_root = tmp_path / "state"
    inbox = (
        state_root
        / "reviews"
        / "applications"
        / "memory_feature_validation"
        / "variants"
        / "approval"
        / "INBOX.md"
    )
    inbox.parent.mkdir(parents=True)
    document = {
        "scope_type": "application",
        "scope_id": application_id,
        "decisions": [
            {"candidate_id": "candidate-1", "revision": 1, "decision": "pending"}
        ],
    }
    inbox.write_text(
        "<!-- agentloom-decisions:start -->\n```json\n"
        + json.dumps(document)
        + "\n```\n<!-- agentloom-decisions:end -->\n",
        encoding="utf-8",
    )
    before_candidate = _candidate_row(content, application_id=application_id)
    after_candidate = _candidate_row(
        content,
        application_id=application_id,
        state="active_confirmed",
        revision=2,
    )
    snapshots = iter(
        [
            {**_empty_v6_database(), "review_candidates": [before_candidate]},
            {
                **_empty_v6_database(),
                "review_candidates": [after_candidate],
                "memory_items": [
                    _fact_row(
                        content,
                        application_id=application_id,
                        state="active_confirmed",
                    )
                ],
            },
        ]
    )
    monkeypatch.setattr(campaign_runner, "_db_snapshot", lambda *_args: next(snapshots))

    def apply_cli(_state_root: Path, args: list[str], _markers: list[str]) -> dict:
        match = campaign_runner._INBOX_DECISION_BLOCK_RE.search(
            inbox.read_text(encoding="utf-8")
        )
        assert match is not None
        assert json.loads(match.group(2))["decisions"][0]["decision"] == "approve"
        inbox.unlink()
        return {
            "command": ["loom", *args],
            "returncode": 0,
            "payload": {
                "applied": 1,
                "results": [{"candidate_id": "candidate-1"}],
            },
        }

    monkeypatch.setattr(campaign_runner, "_run_loom_cli", apply_cli)

    transition = _approval_transition(
        state_root,
        {
            "expected_content": content,
            "expected_scope": "application",
            "decision": "approve",
        },
        [],
        application_id=application_id,
    )

    assert transition["ok"] is True
    assert transition["inbox_removed"] is True
    assert transition["result"]["command"] == [
        "loom", "reviews", "apply", "--application", application_id
    ]


def test_review_batch_identity_must_match_completed_root() -> None:
    evidence = {
        "root_identity_required": False,
        "completed_root_run_ids": ["root-1"],
        "completed_run_count_delta": 1,
        "review_batch_delta": [
            {"source_runs": [{"root_run_id": "root-2", "application_id": "app"}]}
        ],
    }
    attempt = {
        "returncode": 0,
        "completion_marker_seen": True,
        "model_evidence": evidence,
    }
    result = {"status": "completed", "attempts": [attempt], "final": attempt}

    assert audit_module._run_identity_contract_issues(result) == [
        "review batch root did not match the completed Application root"
    ]


def test_evaluate_results_rejects_duplicate_result_ids() -> None:
    spec = build_full_plan()[0]
    result = _v6_result(spec)
    audit = evaluate_results(
        [spec],
        [result, json.loads(json.dumps(result))],
        indexed_rows(ORACLE_PATH),
        require_complete=True,
    )

    assert "duplicate_result" in {issue["code"] for issue in audit["issues"]}


def test_recall_uses_independent_recall_terms_not_writer_terms() -> None:
    spec = next(
        item for item in build_full_plan() if item.run_id == "on-durable-00-recall"
    )
    result = _v6_result(spec, final_answer={"answer": "250 records"})
    independent_oracle = {
        spec.case_id: {
            "expected_writer_status": "active",
            "required_terms": ["writer-only-term"],
            "recall_terms": ["250"],
            "recall_value": "250 records",
        }
    }

    assert evaluate_results(
        [spec], [result], independent_oracle, require_complete=True
    )["ok"] is True


@pytest.mark.parametrize(
    ("mutation", "expected_code"),
    [
        ("business", "approval_transition"),
        ("readback", "approval_transition"),
        ("exact", "exact_evidence_mismatch"),
        ("extra_active", "unexpected_memory_write"),
    ],
)
def test_approval_gate_fails_closed_for_business_readback_and_state_mismatch(
    mutation: str,
    expected_code: str,
) -> None:
    oracle = indexed_rows(ORACLE_PATH)
    spec, result = _approval_post_result("approval-00", "approve")
    if mutation == "business":
        result["approval_transition"]["result"]["payload"]["applied"] = 0
    elif mutation == "readback":
        result["approval_transition"]["readback_ok"] = False
    elif mutation == "exact":
        result["final"]["database"]["review_candidates"][0]["content"] = "wrong"
        result["final"]["database"]["review_candidates"][0]["payload"] = {
            "text": "wrong"
        }
        result["final"]["database"]["memory_items"][0]["content"] = "wrong"
        result["final"]["database"]["memory_items"][0]["payload"] = {
            "text": "wrong"
        }
    else:
        application_id = campaign_common.workflow_application_id(spec)
        result["final"]["database"]["memory_items"].append(
            _fact_row("unrelated", application_id=application_id)
        )

    issues = evaluate_results([spec], [result], oracle, require_complete=True)["issues"]
    assert expected_code in {issue["code"] for issue in issues}


@pytest.mark.parametrize(
    ("memory_effects", "candidate_effects", "expected_codes"),
    [
        (0, 0, {"review_failed"}),
        (1, 1, {"review_failed", "review_atomicity"}),
    ],
)
def test_failed_reviewer_is_soft_only_when_it_commits_no_v6_effect(
    memory_effects: int,
    candidate_effects: int,
    expected_codes: set[str],
) -> None:
    spec = next(
        item for item in build_full_plan() if item.run_id == "progress-00-writer"
    )
    attempt = {
        "model_evidence": {
            "review_call_count": 1,
            "review_action_count": 0,
            "review_records": [
                {"calls": "1", "actions": "0", "status": "failed"}
            ],
            "review_batch_delta": [],
            "memory_item_effect_count": memory_effects,
            "review_candidate_effect_count": candidate_effects,
        }
    }

    issues = audit_module._review_audit_contract(
        spec,
        {"final": attempt},
        indexed_rows(ORACLE_PATH)[spec.case_id],
    )
    assert {issue["code"] for issue in issues} == expected_codes
    if memory_effects == 0:
        assert all(issue["hard"] is False for issue in issues)
    else:
        assert any(issue["hard"] is True for issue in issues)


@pytest.mark.parametrize(
    ("memory_effects", "candidate_effects"),
    [(1, 0), (0, 1)],
)
def test_review_off_non_writer_cannot_change_v6_state(
    memory_effects: int,
    candidate_effects: int,
) -> None:
    oracle = indexed_rows(ORACLE_PATH)
    spec = next(
        item for item in build_full_plan() if item.run_id == "foreground-00-recall"
    )
    result = _v6_result(
        spec,
        final_answer="MISSING",
        memory_effects=memory_effects,
        candidate_effects=candidate_effects,
    )

    issues = evaluate_results([spec], [result], oracle, require_complete=True)["issues"]
    assert "unexpected_memory_write" in {issue["code"] for issue in issues}


def test_reviewer_writer_rejects_extra_unrelated_memory_and_action_drift() -> None:
    oracle = indexed_rows(ORACLE_PATH)
    spec = next(
        item for item in build_full_plan() if item.run_id == "on-durable-00-writer"
    )
    application_id = campaign_common.workflow_application_id(spec)
    content = oracle[spec.case_id]["expected_content"]
    candidate = _candidate_row(
        content,
        application_id=application_id,
        state="active_unreviewed",
    )
    extra = _candidate_row(
        "unrelated",
        application_id=application_id,
        state="active_unreviewed",
        candidate_id="candidate-2",
    )
    result = _v6_result(
        spec,
        memory_items=[
            _fact_row(content, application_id=application_id),
            {**_fact_row("unrelated", application_id=application_id), "id": 2},
        ],
        candidates=[candidate, extra],
        review_candidates=[candidate, extra],
        actions=1,
        memory_effects=2,
        candidate_effects=2,
    )

    issues = evaluate_results([spec], [result], oracle, require_complete=True)["issues"]
    codes = {issue["code"] for issue in issues}
    assert {"unexpected_memory_write", "review_atomicity"} <= codes


def test_successful_retry_still_fails_closed_on_malformed_first_attempt_telemetry() -> None:
    oracle = indexed_rows(ORACLE_PATH)
    spec = next(
        item for item in build_full_plan() if item.run_id == "progress-00-writer"
    )
    result = _v6_result(spec)
    malformed = {
        "returncode": 75,
        "completion_marker_seen": False,
        "privacy_findings": [],
        "model_evidence": {
            "review_call_count": None,
            "review_records": [],
            "review_batch_delta": [],
        },
        "database": _empty_v6_database(),
    }
    result["attempts"] = [malformed, result["final"]]

    issues = evaluate_results([spec], [result], oracle, require_complete=True)["issues"]
    assert "review_evidence_missing" in {issue["code"] for issue in issues}


def test_usage_counts_every_actual_v6_attempt() -> None:
    results = [
        {
            "status": "completed",
            "attempts": [
                {
                    "model_evidence": {
                        "model_generation_count": 2,
                        "model_input_tokens": 10,
                        "model_output_tokens": 4,
                        "review_call_count": 1,
                    }
                },
                {
                    "model_evidence": {
                        "model_generation_count": 3,
                        "model_input_tokens": 20,
                        "model_output_tokens": 6,
                        "review_call_count": 1,
                    }
                },
            ],
        }
    ]

    assert campaign_runner._usage(results) == {
        "model_generation_count": 5,
        "model_input_tokens": 30,
        "model_output_tokens": 10,
        "review_model_calls": 2,
    }
