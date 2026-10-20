"""Harness configuration through public Application composition and inspection."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml
from agentloom.app import Application
from agentloom.app.definition import inspect_application_definition, load_agent_definition
from agentloom.config.config import bind_config, build_effective_agent_config_snapshot, load_project_config
from pydantic import ValidationError


def write_yaml(path: Path, data: dict) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(data), encoding="utf-8")
    return path


def project(tmp_path: Path, *, defaults=None, policy=None):
    system = {
        "runtime": {"root_dir": str(tmp_path / "runtime")},
        "checkpoint": {"enabled": False}, "self_learning": {"enabled": False},
        "logging": {"console_enabled": False}, "default_toolsets": [],
    }
    if defaults is not None:
        system["harnessing"] = defaults
    write_yaml(tmp_path / "config/system.yaml", system)
    write_yaml(tmp_path / "config/model.yaml", {"model": {
        "default_model_type": "test", "test": {"model": "fixture/model", "adapter": "openai_chat"},
    }})
    app = tmp_path / "applications/example"
    entry = write_yaml(app / "workflows/main.yaml", {
        "name": "example", "description": "Configuration only.", "task": "Complete the task.",
        "agent_runtime": "pi", "tools": [], "toolsets": [],
    })
    if policy is not None:
        write_yaml(app / "config/system.yaml", {"harnessing": policy})
    return app, entry


def test_application_default_is_disabled_and_opening_enabled_policy_does_no_work(tmp_path):
    app_root, _ = project(tmp_path)
    with bind_config(load_project_config(tmp_path)):
        with Application.open(app_root, file_logging=False) as app:
            assert not app.harnessing_policy.enabled
            assert (app.harnessing_policy.recursion.mode, app.harnessing_policy.recursion.k) == ("parallel", 1)
            assert app.harnessing_policy.editable == (
                "system_prompt", "skills", "tools", "toolsets", "mcp_servers", "worker_agents",
            )
    write_yaml(app_root / "config/system.yaml", {"harnessing": {"enabled": True}})
    with bind_config(load_project_config(tmp_path)):
        with Application.open(app_root, file_logging=False) as app:
            assert app.harnessing_policy.enabled
            assert not app.harnessing_policy.roles.overrides()
            policy = app.harnessing_policy
            assert type(policy).model_validate(policy.model_dump()) == policy
            assert not app.runtime_context.manifest_path.parent.joinpath("batches").exists()


def test_project_defaults_do_not_enable_applications_and_keep_original_sources(tmp_path):
    defaults = {"recursion": {"mode": "sequential", "k": 3}, "editable": ["skills", "tools"]}
    app_root, entry = project(tmp_path, defaults=defaults)
    base = load_project_config(tmp_path)
    with bind_config(base):
        with Application.open(app_root, file_logging=False) as app:
            assert not app.harnessing_policy.enabled
            assert app.harnessing_policy.recursion.k == 3
    application = {"enabled": True, "recursion": {"k": 2}}
    write_yaml(app_root / "config/system.yaml", {"harnessing": application})
    snapshot = build_effective_agent_config_snapshot(load_agent_definition(entry), base_config=base)
    assert snapshot.values["harnessing"]["enabled"] is True
    assert snapshot.values["harnessing"]["recursion"] == {"mode": "sequential", "k": 2}
    sources = {layer.name: layer for layer in snapshot.layers}
    assert sources["global_system"].data["harnessing"] == defaults
    assert sources["application_system"].data["harnessing"] == application
    assert sources["application_system"].source_path == app_root / "config/system.yaml"
    assert base.raw["harnessing"] == defaults


def test_development_feedback_is_completed_across_sources_and_policy_is_immutable(tmp_path):
    app_root, _ = project(tmp_path,
        defaults={"feedback": {"mode": "development_score", "evaluator": "news_outcome"}},
        policy={"enabled": True, "feedback": {"partition": "development"}},
    )
    with bind_config(load_project_config(tmp_path)):
        with Application.open(app_root, file_logging=False) as app:
            policy = app.harnessing_policy
            assert policy.feedback.model_dump() == {
                "mode": "development_score", "evaluator": "news_outcome", "partition": "development",
            }
            with pytest.raises(ValidationError, match="frozen"):
                policy.enabled = False
            with pytest.raises(ValidationError, match="frozen"):
                policy.recursion.k = 4
            with pytest.raises(ValidationError, match="frozen"):
                policy.feedback.partition = "validation"
            write_yaml(app_root / "config/system.yaml", {"harnessing": {"enabled": False}})
            assert app.harnessing_policy is policy and policy.enabled


@pytest.mark.parametrize("policy", [
    {"enabled": "true"}, {"enabled": 1}, {"enabled": None},
    {"recursion": {"k": True}}, {"recursion": {"k": False}}, {"recursion": {"k": 0}},
    {"recursion": {"k": -1}}, {"recursion": {"k": 1.0}}, {"recursion": {"k": "1"}},
    {"recursion": {"k": None}}, {"recursion": {"mode": "automatic"}},
    {"feedback": {"mode": "oracle"}}, {"feedback": {"mode": "development_score"}},
    {"feedback": {"mode": "development_score", "evaluator": "score"}},
    {"feedback": {"mode": "development_score", "evaluator": "", "partition": "dev"}},
    {"feedback": {"evaluator": 12}}, {"feedback": {"split": "development"}},
    {"editable": "skills"}, {"editable": ["skills", "skills"]}, {"editable": ["business_python"]},
    {"roles": {"proposer": "workflows/main.yaml"}}, {"roles": {"general": None}},
])
def test_disabled_application_rejects_invalid_policy_before_run_allocation(tmp_path, policy):
    app_root, _ = project(tmp_path, policy={"enabled": False, **policy})
    with bind_config(load_project_config(tmp_path)):
        with pytest.raises(ValueError, match="harnessing"):
            with Application.open(app_root, file_logging=False):
                pytest.fail("invalid policy must fail before opening an Application")
    assert not (tmp_path / "runtime").exists()


@pytest.mark.parametrize("section", [True, False, None, []])
def test_harnessing_section_requires_a_mapping(tmp_path, section):
    app_root, _ = project(tmp_path)
    write_yaml(app_root / "config/system.yaml", {"harnessing": section})
    with bind_config(load_project_config(tmp_path)):
        with pytest.raises(ValueError, match="harnessing must be a mapping"):
            with Application.open(app_root, file_logging=False):
                pass


@pytest.mark.parametrize("key", ["scope", "trigger", "budget", "selection", "adoption", "agents", "driver"])
def test_removed_or_unknown_policy_fields_are_not_silently_ignored(tmp_path, key):
    app_root, _ = project(tmp_path, policy={"enabled": False, key: {}})
    with bind_config(load_project_config(tmp_path)):
        with pytest.raises(ValueError, match=key):
            with Application.open(app_root, file_logging=False):
                pass


@pytest.mark.parametrize("defaults", [
    {"enabled": True}, {"recursion": {"k": "2"}}, {"feedback": {"mode": "unknown"}},
    {"scope": "application"},
])
def test_application_override_cannot_hide_invalid_project_source(tmp_path, defaults):
    project(tmp_path, defaults=defaults, policy={"enabled": False, "recursion": {"k": 1}})
    with pytest.raises(ValueError, match="harnessing"):
        load_project_config(tmp_path)


@pytest.mark.parametrize("path", ["../outside.yaml", "/tmp/role.yaml", "workflows/../main.yaml", "workflows\\role.yaml", "", "workflows/role.json"])
def test_role_paths_must_stay_application_relative(tmp_path, path):
    app_root, _ = project(tmp_path, policy={"roles": {"general": path}})
    with bind_config(load_project_config(tmp_path)):
        with pytest.raises(ValueError, match="harnessing"):
            with Application.open(app_root, file_logging=False):
                pass


def test_custom_roles_are_real_ordinary_agent_definitions_and_frozen(tmp_path):
    app_root, entry = project(tmp_path, policy={"enabled": True, "roles": {"general": "workflows/main.yaml"}})
    with bind_config(load_project_config(tmp_path)):
        with Application.open(app_root, file_logging=False) as app:
            assert app.harnessing_policy.roles.overrides() == (("general", "workflows/main.yaml"),)
            with pytest.raises(ValidationError, match="frozen"):
                app.harnessing_policy.roles.general = "workflows/changed.yaml"
    entry.write_text("not: an Agent definition\n")
    with bind_config(load_project_config(tmp_path)):
        with pytest.raises(ValueError):
            with Application.open(app_root, file_logging=False):
                pass


@pytest.mark.parametrize("kind", ["missing", "symlink"])
def test_role_binding_rejects_missing_and_symlinked_files(tmp_path, kind):
    app_root, entry = project(tmp_path, policy={"roles": {"reflect": "workflows/role.yaml"}})
    if kind == "symlink":
        (app_root / "workflows/role.yaml").symlink_to(entry)
    with bind_config(load_project_config(tmp_path)):
        with pytest.raises((ValueError, FileNotFoundError)):
            with Application.open(app_root, file_logging=False):
                pass


def test_inspection_rejects_policy_in_agent_yaml_and_invalid_application_source(tmp_path):
    app_root, entry = project(tmp_path, policy={"enabled": False, "recursion": {"k": "2"}})
    base = load_project_config(tmp_path)
    inspection = inspect_application_definition(tmp_path, str(entry), load_agent_definition(entry), base_config=base)
    assert any("harnessing" in error for error in inspection.errors)
    write_yaml(app_root / "config/system.yaml", {"harnessing": {"enabled": True}})
    definition = load_agent_definition(entry)
    definition["harnessing"] = {"enabled": False}
    inspection = inspect_application_definition(tmp_path, str(entry), definition, base_config=base)
    assert any("not configurable in Agent YAML" in error for error in inspection.errors)
