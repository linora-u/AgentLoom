"""Application logging and Trace policy may vary without moving storage roots."""

from copy import deepcopy

import pytest
import yaml
from agentloom.app.definition import inspect_application_definition, load_agent_definition
from agentloom.config.config import extract_workflow_overlay, load_project_config


def inspect(root, application):
    path = root / "applications/quiet/workflows/root.yaml"
    path.parent.mkdir(parents=True)
    path.write_text("name: quiet\nagent_runtime: pi\ndescription: Quiet fixture.\ntask: Answer.\ntools: []\ntoolsets: []\n")
    config = path.parents[1] / "config/system.yaml"
    config.parent.mkdir()
    config.write_text(yaml.safe_dump(application))
    model = root / "config/model.yaml"
    model.parent.mkdir()
    model.write_text("model:\n  default_model_type: test\n  test: {model: fixture, adapter: openai_chat}\n")
    base = load_project_config(root)
    original = deepcopy(base.raw)
    result = inspect_application_definition(root, str(path), load_agent_definition(path), base_config=base)
    assert base.raw == original
    return result, path, base


def test_application_logging_and_trace_only_affect_current_application(tmp_path):
    result, path, base = inspect(tmp_path, {
        "logging": {"console_enabled": False, "file_enabled": False},
        "runtime": {"trace": {"mode": "off"}},
    })
    assert not result.errors
    values = result.snapshots[path].values
    assert values["logging"]["console_enabled"] is False
    assert values["logging"]["file_enabled"] is False
    assert values["runtime"]["trace"]["mode"] == "off"
    assert base.raw.get("runtime", {}).get("trace", {}).get("mode") != "off"


@pytest.mark.parametrize("runtime", [{"root_dir": "/tmp/other-root"}, {"failed_run_retention_days": 1}])
def test_application_storage_and_retention_still_require_global_config(tmp_path, runtime):
    result, _, _ = inspect(tmp_path, {"runtime": runtime})
    assert any("global-only runtime storage" in error for error in result.errors)


@pytest.mark.parametrize("section", [
    {"logging": {"console_enabled": False}}, {"runtime": {"trace": {"mode": "off"}}},
])
def test_agent_yaml_cannot_override_application_logging_and_trace(section):
    with pytest.raises(ValueError, match="Agent YAML"):
        extract_workflow_overlay(section)
