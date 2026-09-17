"""Deterministic checks for the real Application's independent acceptance seam."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

from applications.architecture_contract_validation.agent_tools import workspace_tools as tools
from applications.architecture_contract_validation.validation import (
    APP_ROOT,
    REQUIRED_CASES,
    WORKERS,
    _contains_exact_json,
    reset_fixture,
    validate_artifacts,
    validate_trace,
)


@pytest.fixture
def workspace(tmp_path, monkeypatch):
    root = reset_fixture(tmp_path / "workspace", "case-unique")
    monkeypatch.setenv("AGENTLOOM_ARCHITECTURE_WORKSPACE", str(root))
    return root


def _repair(root: Path) -> None:
    """A hand-reviewed positive control for the validator, never a live tool."""
    (root / "orderdesk/domain/lines.py").write_text('''def line_total(line):
    price, quantity = line["unit_cents"], line["quantity"]
    if type(price) is not int or price < 0:
        raise ValueError("invalid price")
    if type(quantity) is not int or not 1 <= quantity <= 100:
        raise ValueError("invalid quantity")
    return price * quantity
''')
    (root / "orderdesk/config/settings.py").write_text('''DEFAULTS = {"discount_percent": 0, "shipping_cents": 500, "free_shipping_at": 5000}
def load_settings(overrides=None):
    if overrides is not None and not isinstance(overrides, dict):
        raise ValueError("invalid configuration shape")
    values = dict(DEFAULTS)
    values.update(overrides or {})
    if any(k not in DEFAULTS or type(v) is not int or v < 0 for k, v in values.items()):
        raise ValueError("invalid configuration value")
    if values["discount_percent"] > 50:
        raise ValueError("invalid discount")
    return values
''')
    (root / "orderdesk/service/checkout.py").write_text('''from orderdesk.domain.lines import line_total
from orderdesk.config.settings import load_settings
def quote(lines, overrides=None):
    settings = load_settings(overrides)
    lines = list(lines)
    subtotal = sum(line_total(line) for line in lines)
    discount = subtotal * settings["discount_percent"] // 100
    shipping = 0 if not lines or subtotal >= settings["free_shipping_at"] else settings["shipping_cents"]
    return {"subtotal_cents": subtotal, "discount_cents": discount, "shipping_cents": shipping, "total_cents": subtotal - discount + shipping}
''')


GENERATED = '''import pytest
from orderdesk.service.checkout import quote
from orderdesk.config.settings import load_settings

def test_threshold_equal():
    assert quote([{"unit_cents": 5000, "quantity": 1}])["shipping_cents"] == 0
def test_discount_before_shipping():
    assert quote([{"unit_cents": 999, "quantity": 1}], {"discount_percent": 10})["total_cents"] == 1400
def test_override_precedence():
    assert load_settings({"shipping_cents": 123})["shipping_cents"] == 123
@pytest.mark.parametrize("quantity", [0, True, False, 101, 1.0, "1"])
def test_invalid_quantity(quantity):
    with pytest.raises(ValueError): quote([{"unit_cents": 100, "quantity": quantity}])
@pytest.mark.parametrize("price", [True, False, -1, 1.0, "1"])
def test_invalid_price(price):
    with pytest.raises(ValueError): quote([{"unit_cents": price, "quantity": 1}])
@pytest.mark.parametrize("config", [{"discount_percent": True}, {"shipping_cents": -1}, [], False])
def test_invalid_config(config):
    with pytest.raises(ValueError): load_settings(config)
def test_empty_cart():
    assert quote([]) == dict(subtotal_cents=0, discount_cents=0, shipping_cents=0, total_cents=0)
def test_multiple_lines():
    assert quote([{"unit_cents": 333, "quantity": 3}, {"unit_cents": 501, "quantity": 2}], {"discount_percent": 15})["total_cents"] == 2201
'''


def _complete_artifacts(root: Path) -> None:
    _repair(root)
    tools.write_workspace_file(str(root), "tests/generated/test_regressions.py", GENERATED)
    result = json.loads(tools.run_workspace_tests(str(root), "verifier"))
    tools.write_workspace_file(str(root), "reports/final.json", json.dumps({
        "workspace": str(root), "case_nonce": "case-unique", "workers": WORKERS,
        "generated_tests": ["tests/generated/test_regressions.py"],
        "test_report": result["report"], "verified": True, "summary": "Actual pytest evidence.",
    }))


def test_fixture_is_resettable_without_deleting_previous_evidence(workspace):
    with pytest.raises(FileExistsError, match="refusing"):
        reset_fixture(workspace, "other-case")
    assert json.loads((workspace / ".architecture-case.json").read_text())["case_nonce"] == "case-unique"
    assert len(list(workspace.glob("orderdesk/*/*.py"))) >= 6


@pytest.mark.parametrize("path", ["../outside.py", "/tmp/escape.py", "tests/test_existing.py", "CONTRACT.md", "configs/valid.json", "conftest.py"])
def test_workspace_tool_rejects_escapes_and_protected_fixture_writes(workspace, path):
    with pytest.raises(ValueError):
        tools.write_workspace_file(str(workspace), path, "replacement")


def test_workspace_symlink_cannot_escape(workspace, tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    (workspace / "reports").symlink_to(outside, target_is_directory=True)
    with pytest.raises(ValueError, match="inside"):
        tools.write_workspace_file(str(workspace), "reports/result.json", "{}")
    assert not list(outside.iterdir())


def test_oracle_rejects_original_defects_and_accepts_independent_positive_control(workspace):
    command = [sys.executable, str(APP_ROOT / "oracle.py"), str(workspace)]
    original = subprocess.run(command, text=True, capture_output=True, check=False)
    report = json.loads(original.stdout)
    assert original.returncode == 1
    assert report["checks"] == 50
    assert any("threshold_equal" in error for error in report["failures"])
    assert any("invalid_config_shape" in error for error in report["failures"])
    _repair(workspace)
    fixed = subprocess.run(command, text=True, capture_output=True, check=False)
    assert fixed.returncode == 0, fixed.stdout + fixed.stderr
    assert json.loads(fixed.stdout)["checks"] == 50


def test_independent_validation_executes_tests_and_negative_control(workspace, tmp_path):
    _complete_artifacts(workspace)
    result = validate_artifacts(workspace, tmp_path / "evidence", "case-unique")
    assert result["passed"], result["errors"]
    assert result["pytest"]["tests"] > 3
    assert result["pytest"]["failures"] == result["pytest"]["errors"] == 0
    assert result["negative_control"]["failures"] >= 5
    assert all(any(required in name for name in result["pytest"]["cases"]) for required in REQUIRED_CASES)


@pytest.mark.parametrize("tamper", ["baseline_test", "report_identity", "reported_count", "trivial_generated"])
def test_validation_rejects_forged_or_weakened_success(workspace, tmp_path, tamper):
    _complete_artifacts(workspace)
    report_path = workspace / "reports/final.json"
    report = json.loads(report_path.read_text())
    if tamper == "baseline_test":
        (workspace / "tests/test_existing.py").write_text("def test_fake(): assert True\n")
    elif tamper == "report_identity":
        report["case_nonce"] = "previous-case"
        report_path.write_text(json.dumps(report))
    elif tamper == "reported_count":
        test_report = workspace / report["test_report"]
        payload = json.loads(test_report.read_text())
        payload["tests"] = 999
        test_report.write_text(json.dumps(payload))
    else:
        (workspace / "tests/generated/test_regressions.py").write_text(
            "\n".join(f"def test_{name}(): assert True" for name in REQUIRED_CASES))
    result = validate_artifacts(workspace, tmp_path / "evidence", "case-unique")
    assert not result["passed"]
    assert result["errors"]


def test_trace_validation_does_not_accept_an_artifact_only_success(tmp_path):
    result = validate_trace(tmp_path, {"case_nonce": "case-unique", "run": {}})
    assert not result["passed"]
    assert len(result["errors"]) >= len(WORKERS)


@pytest.mark.parametrize("tamper", [None, "wrapped_input", "wrapped_json_text", "sibling_context", "wrapped_omission", "wrapped_alteration", "wrapped_fabrication",
                                  "cross_run", "dropped_input", "no_model_usage", "no_python"])
def test_codeact_trace_checks_real_checkpoint_contract_and_independent_ids(tmp_path, tamper):
    run = {"run_id": "run-current", "task_id": "task-current", "manifest_path": str(tmp_path / "manifest.json")}
    (tmp_path / "manifest.json").write_text('{"status":"completed"}')
    report_dir = tmp_path / "workspace/reports"
    report_dir.mkdir(parents=True)
    (report_dir / "final.json").write_text('{"test_report":"reports/pytest-verifier.json"}')
    checkpoints = tmp_path / "runtime/checkpoints/app/task-current"
    checkpoints.mkdir(parents=True)
    (checkpoints / "checkpoint.json").write_text(json.dumps({
        "memory_steps": [{"code_action": None if tamper == "no_python" else "result = repository_investigator(query=payload)"}]
    }))
    previous = {"workspace": str(tmp_path / "workspace"), "case_nonce": "case-unique"}
    ledger = []
    for index, name in enumerate(WORKERS):
        query = dict(previous)
        if tamper == "dropped_input" and index == 2:
            query.pop("findings")
        if tamper == "sibling_context":
            query["additional_context"] = {"source_files": ["CONTRACT.md"]}
        if isinstance(tamper, str) and tamper.startswith("wrapped_"):
            if index == 2:
                if tamper == "wrapped_omission":
                    query.pop("findings")
                elif tamper == "wrapped_alteration":
                    query["findings"] = [*query["findings"], "unreported claim"]
                elif tamper == "wrapped_fabrication":
                    query = {"workspace": query["workspace"], "case_nonce": query["case_nonce"], "findings": ["fabricated"]}
            query = {"workspace": query["workspace"], "case_nonce": query["case_nonce"], "preceding_result": {"data": [query]}}
            if tamper == "wrapped_json_text":
                query["preceding_result"]["data"][0] = json.dumps(query["preceding_result"]["data"][0])
        output = {**previous, "findings": [name, index]}
        call_dir = checkpoints / f"workers/{name}/calls/0"
        call_dir.mkdir(parents=True)
        (call_dir / "checkpoint.json").write_text(json.dumps({
            "status": "completed", "task_input": "task\n<inputs>\nPlease process the following call inputs in order:\n"
            "1. JSON result from the previous stage, with absolute workspace and unique case_nonce.: "
            + json.dumps(json.dumps(query)) + "\n</inputs>",
            "memory_steps": [{"token_usage": {"input_tokens": 0 if tamper == "no_model_usage" else 10},
                              "tool_results": [{"tool_name": "final_answer", "status": "completed", "output": json.dumps(output)}]}],
        }))
        ledger.append({"agent_name": name, "root_run_id": "other-run" if tamper == "cross_run" else "run-current",
                       "task_id": "task-current", "case_nonce": "case-unique", "local_run_id": f"local-{index}",
                       "operation": "pytest" if index >= 2 else "read", "exit_code": 0,
                       "report": "reports/pytest-verifier.json"})
        previous = output
    (tmp_path / "tool-ledger.jsonl").write_text("\n".join(json.dumps(row) for row in ledger))
    result = validate_trace(tmp_path, {"case_nonce": "case-unique", "run": run, "mode": "codeact"})
    assert result["passed"] is (tamper in {None, "wrapped_input", "wrapped_json_text", "sibling_context"}), result["errors"]
    if result["passed"]:
        assert len(result["transfers"]) == 3
        assert all(row["query_path"].startswith("$") and len(row["original_output_sha256"]) == 64 for row in result["transfers"])


@pytest.mark.parametrize("replacement", [True, 1.0, "1", 2])
def test_exact_json_transfer_rejects_scalar_value_or_type_substitution(replacement):
    assert not _contains_exact_json({"result": {"count": replacement}}, {"count": 1})


@pytest.mark.parametrize("mutation", ["missing_key", "changed_key", "nested_addition", "nested_change", "list_order",
                                     "malformed_json", "overencoded_json", "fabricated_data"])
def test_json_query_envelopes_cannot_disguise_changed_or_unreadable_original_data(mutation):
    expected = {"case_nonce": "current", "findings": {"count": 2, "files": ["one.py", "two.py"]}}
    result = json.loads(json.dumps(expected))
    if mutation == "missing_key":
        result.pop("case_nonce")
    elif mutation == "changed_key":
        result["case_nonce"] = "previous"
    elif mutation == "nested_addition":
        result["findings"]["fabricated"] = True
    elif mutation == "nested_change":
        result["findings"]["count"] = 3
    elif mutation == "list_order":
        result["findings"]["files"].reverse()
    elif mutation == "malformed_json":
        result = json.dumps(result)[:-1]
    elif mutation == "overencoded_json":
        for _ in range(4):
            result = json.dumps(result)
    else:
        result = {"case_nonce": "current", "findings": {"count": 2, "files": ["invented.py"]}}
    assert not _contains_exact_json({"prior_worker": result, "additional_context": "retained"}, expected)


def test_native_and_codeact_definitions_have_four_real_typed_workers():
    from agentloom.application.readiness import validate_runtime_agent_config
    from agentloom.runtime.factory import YamlAgentFactory

    for mode, expected in (("native", "tool_call"), ("codeact", "code_act")):
        source = APP_ROOT / "workflows" / f"{mode}.yaml"
        definition = YamlAgentFactory._load_config_from_file(source)
        validate_runtime_agent_config(definition, source, agent_root=APP_ROOT.parents[1])
        assert definition["tool_call_type"] == expected
        assert len(definition["worker_agents"]) == 4
        names = set()
        for item in definition["worker_agents"]:
            worker_source = source.parent / item["path"]
            worker = YamlAgentFactory._load_config_from_file(worker_source)
            assert worker["agent_function_schema"]["inputs"]["query"]["required"] is True
            assert worker["tools"]
            names.add(worker["name"])
        assert names == set(WORKERS)
    assert (APP_ROOT / "workflows/worker_agents/change_planner.md").is_file()


def test_application_config_retains_run_evidence_and_disables_unused_connections():
    config = yaml.safe_load((APP_ROOT / "config/system.yaml").read_text())
    assert config["checkpoint"]["cleanup_on_success"] is False
    assert config["lsp_servers"]["enabled"] is False
    assert config["mcp_servers"] is None


@pytest.mark.parametrize("mode", ["native", "codeact"])
def test_prepared_nested_application_relocates_only_tool_namespaces_and_loads_local_tools(tmp_path, monkeypatch, mode):
    from agentloom.application.definition import load_agent_definition

    from applications.architecture_contract_validation import run_acceptance

    project = tmp_path / "candidate"
    (project / "config").mkdir(parents=True)
    (project / "config/system.yaml").write_text("{}\n")
    (project / "config/llm.yaml").write_text(
        "model:\n  default_model_type: powerful\n  powerful:\n    model: openai/test\n"
        "  summary:\n    model: openai/test\n")
    monkeypatch.setattr(run_acceptance, "_revision", lambda _project: "test-candidate")
    attempt, request = run_acceptance.prepare_attempt(project, tmp_path / "evidence", f"nested-{mode}")
    prepared = Path(request["project"]) / "applications" / request["application_id"]
    assert request["namespace_adaptations"]
    assert any(row["definition"].endswith(".md") for row in request["namespace_adaptations"])
    recorded = {(row["definition"], row["field"]): row for row in request["namespace_adaptations"]}
    for relative, digest in request["prepared_definition_sha256"].items():
        assert run_acceptance.sha256(prepared / relative) == digest
        original = load_agent_definition(APP_ROOT / relative)
        copied = load_agent_definition(prepared / relative)
        original.pop("_yaml_file_path")
        copied.pop("_yaml_file_path")
        for index, (before, after) in enumerate(zip(original.get("tools", []), copied.get("tools", []), strict=True)):
            if before.get("module"):
                change = recorded[(relative, f"tools[{index}].module")]
                assert change["from"] == before["module"]
                assert change["to"] == after["module"]
                assert after["module"].startswith("applications.nested.suite.architecture_contract_validation.")
                after["module"] = before["module"]
        assert copied == original  # includes exact workflow, Worker and resource path values

    script = r'''
import importlib.util, json, sys
from pathlib import Path
from agentloom.application.definition import load_agent_definition, validate_agent_definition
from agentloom.configuration import C
from agentloom.utils.dynamic_import import load_function
definition, outside = map(Path, sys.argv[1:])
config = load_agent_definition(definition)
assert validate_agent_definition(C.agent_root, str(definition.relative_to(C.agent_root)), config) == []
# Cached code under the old absolute namespace must never bypass project isolation.
old = "applications.architecture_contract_validation.agent_tools.workspace_tools"
spec = importlib.util.spec_from_file_location(old, outside)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
sys.modules[old] = module
try:
    load_function(old, "write_workspace_file")
except ImportError as exc:
    assert "did not resolve inside" in str(exc), str(exc)
else:
    raise AssertionError("outside cached original tool was accepted")
tool = config["tools"][0]
loaded = load_function(tool["module"], tool["function"])
origin = Path(sys.modules[loaded.__module__].__file__).resolve()
assert origin.is_relative_to(C.agent_root / "applications/nested/suite/architecture_contract_validation")
print(json.dumps({"origin": str(origin), "outside_cache_rejected": True}))
'''
    completed = subprocess.run([sys.executable, "-I", "-c", script, request["definition"],
                                str(APP_ROOT / "agent_tools/workspace_tools.py")],
                               cwd=attempt / "project", text=True, capture_output=True, timeout=30)
    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert json.loads(completed.stdout)["outside_cache_rejected"] is True
