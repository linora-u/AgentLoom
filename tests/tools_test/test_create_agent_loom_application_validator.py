from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

SCRIPT_PATH = (
    Path(__file__).resolve().parents[2]
    / "agentloom-framework-skill"
    / "scripts"
    / "validate_application_yaml.py"
)


def _write_yaml(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(data, sort_keys=False, allow_unicode=True), encoding="utf-8")


def _write_markdown(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def _write_project_config(tmp_path: Path) -> None:
    _write_yaml(tmp_path / "config/system.yaml", {})
    _write_yaml(tmp_path / "config/llm.yaml", {"model": {
        "default_model_type": "custom-model-key",
        "custom-model-key": {"model": "openai/test", "adapter": "openai_chat"},
        "summary": {"model": "openai/test", "adapter": "openai_chat"},
    }})


def _messages(payload: dict) -> str:
    return "\n".join(error["message"] for error in payload["errors"])


def _create_min_project(tmp_path: Path, *, skills_value=None) -> Path:
    _write_project_config(tmp_path)

    app_root = tmp_path / "applications" / "demo"
    workflow_file = app_root / "workflows" / "demo_agent.yaml"
    config = {
        "name": "demo_agent",
        "agent_runtime": "smolagents",
        "description": "demo",
        "workflow": "# demo\n",
        "model_type": "custom-model-key",
    }
    if skills_value is not None:
        config["skills"] = skills_value
    _write_yaml(workflow_file, config)
    return app_root


def _run_validator(project_root: Path, app_root: str = "applications/demo") -> tuple[subprocess.CompletedProcess[str], dict]:
    completed = subprocess.run(
        [sys.executable, str(SCRIPT_PATH), "--app-root", app_root],
        cwd=project_root,
        capture_output=True,
        text=True,
        check=False,
    )
    payload = json.loads(completed.stdout)
    return completed, payload


@pytest.mark.parametrize(
    "skills_value",
    [
        {"paths": []},
        {"paths": ["skills/custom-analysis", "shared/skills"]},
    ],
)
def test_skills_paths_config_is_supported(tmp_path: Path, skills_value) -> None:
    _create_min_project(tmp_path, skills_value=skills_value)

    completed, payload = _run_validator(tmp_path)

    assert completed.returncode == 0
    assert payload["summary"]["valid"] is True
    assert payload["errors"] == []


@pytest.mark.parametrize(
    "skills_value",
    [
        123,
        "skills/custom-analysis",
        ["skills/custom-analysis"],
        {"items": ["skills/custom-analysis"]},
        {"paths": [], "load-mode": "eager"},
    ],
)
def test_legacy_or_invalid_skills_shapes_are_rejected(tmp_path: Path, skills_value) -> None:
    _create_min_project(tmp_path, skills_value=skills_value)

    completed, payload = _run_validator(tmp_path)

    assert completed.returncode == 1
    assert payload["summary"]["valid"] is False
    assert "skills" in _messages(payload)


def test_removed_tools_mapping_is_rejected(tmp_path: Path) -> None:
    _create_min_project(tmp_path)
    workflow_file = tmp_path / "applications" / "demo" / "workflows" / "demo_agent.yaml"
    config = yaml.safe_load(workflow_file.read_text(encoding="utf-8"))
    config["tools_mapping"] = {"Claude": {"Read": "read_file"}}
    _write_yaml(workflow_file, config)

    completed, payload = _run_validator(tmp_path)
    assert completed.returncode == 1
    assert "tools_mapping was removed" in _messages(payload)


def test_validator_rejects_list_workflow(tmp_path: Path) -> None:
    _create_min_project(tmp_path)
    workflow_file = tmp_path / "applications" / "demo" / "workflows" / "demo_agent.yaml"
    config = yaml.safe_load(workflow_file.read_text(encoding="utf-8"))
    config["workflow"] = [
        "# First workflow item\nRun the first task.",
        "# Second workflow item\nUse memory from the first task.",
    ]
    _write_yaml(workflow_file, config)

    completed, payload = _run_validator(tmp_path)

    assert completed.returncode == 1
    assert payload["summary"]["valid"] is False
    assert "workflow field must be a non-empty string" in _messages(payload)


def test_validator_rejects_invalid_list_workflow_item(tmp_path: Path) -> None:
    _create_min_project(tmp_path)
    workflow_file = tmp_path / "applications" / "demo" / "workflows" / "demo_agent.yaml"
    config = yaml.safe_load(workflow_file.read_text(encoding="utf-8"))
    config["workflow"] = ["# First workflow item", ""]
    _write_yaml(workflow_file, config)

    completed, payload = _run_validator(tmp_path)

    assert completed.returncode == 1
    assert payload["summary"]["valid"] is False
    assert "workflow" in _messages(payload)


@pytest.mark.parametrize(
    "goal",
    [True, False, {"enabled": True}, {"enabled": False}, {"enabled": True, "token_budget": 1000}, {"enabled": True, "token_budget": "10"}],
)
def test_validator_accepts_goal_supervisor_forms(tmp_path: Path, goal) -> None:
    app_root = _create_min_project(tmp_path)
    workflow_file = app_root / "workflows" / "demo_agent.yaml"
    config = yaml.safe_load(workflow_file.read_text(encoding="utf-8"))
    config["goal"] = goal
    _write_yaml(workflow_file, config)

    completed, payload = _run_validator(tmp_path)

    assert completed.returncode == 0
    assert payload["summary"]["valid"] is True


@pytest.mark.parametrize(
    "goal",
    [None, {}, {"token_budget": 10}, {"enabled": True, "extra": 1}],
)
def test_validator_rejects_invalid_goal_forms(tmp_path: Path, goal) -> None:
    app_root = _create_min_project(tmp_path)
    workflow_file = app_root / "workflows" / "demo_agent.yaml"
    config = yaml.safe_load(workflow_file.read_text(encoding="utf-8"))
    config["goal"] = goal
    _write_yaml(workflow_file, config)

    completed, payload = _run_validator(tmp_path)

    assert completed.returncode == 1
    assert "goal" in _messages(payload)


def test_validator_rejects_goal_on_worker(tmp_path: Path) -> None:
    app_root = _create_min_project(tmp_path)
    worker_file = app_root / "workflows" / "worker_agents" / "worker.yaml"
    _write_yaml(
        worker_file,
        {
            "name": "worker",
            "agent_runtime": "smolagents",
            "description": "worker",
            "workflow": "work",
            "goal": False,
            "input_schema": {
                "type": "object",
                "properties": {"task": {"type": "string"}},
                "required": ["task"],
                "additionalProperties": False,
            },
        },
    )

    completed, payload = _run_validator(tmp_path)

    assert completed.returncode == 1
    assert "must not define goal" in _messages(payload)
    assert "Supervisor-only" in _messages(payload)


@pytest.mark.parametrize("mode", ["auto", "on", "off"])
def test_validator_accepts_todo_modes(tmp_path: Path, mode: str) -> None:
    app_root = _create_min_project(tmp_path)
    workflow_file = app_root / "workflows" / "demo_agent.yaml"
    config = yaml.safe_load(workflow_file.read_text(encoding="utf-8"))
    config["runtime_options"] = {"todo_mode": mode}
    _write_yaml(workflow_file, config)

    completed, payload = _run_validator(tmp_path)

    assert completed.returncode == 0
    assert payload["summary"]["valid"] is True


def test_validator_ignores_empty_legacy_todo_mapping(tmp_path: Path) -> None:
    app_root = _create_min_project(tmp_path)
    workflow_file = app_root / "workflows" / "demo_agent.yaml"
    config = yaml.safe_load(workflow_file.read_text(encoding="utf-8"))
    config["todo"] = {}
    _write_yaml(workflow_file, config)

    completed, payload = _run_validator(tmp_path)

    assert completed.returncode == 0
    assert payload["summary"]["valid"] is True


@pytest.mark.parametrize("todo", ["always", False, {"mode": "on"}])
def test_validator_rejects_invalid_todo_config(tmp_path: Path, todo) -> None:
    app_root = _create_min_project(tmp_path)
    workflow_file = app_root / "workflows" / "demo_agent.yaml"
    config = yaml.safe_load(workflow_file.read_text(encoding="utf-8"))
    config["runtime_options"] = {"todo_mode": todo}
    _write_yaml(workflow_file, config)

    completed, payload = _run_validator(tmp_path)

    assert completed.returncode == 1
    assert payload["summary"]["valid"] is False
    assert "todo" in _messages(payload)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("runtime", {"root_dir": "/tmp/split-runtime"}),
        ("logging", {"file_enabled": False}),
        ("logging", {"enabled": True, "dir": ".logs"}),
    ],
)
def test_agent_yaml_rejects_global_only_runtime_and_logging(
    tmp_path: Path,
    field: str,
    value: dict,
) -> None:
    app_root = _create_min_project(tmp_path)
    workflow_file = app_root / "workflows" / "demo_agent.yaml"
    config = yaml.safe_load(workflow_file.read_text(encoding="utf-8"))
    config[field] = value
    _write_yaml(workflow_file, config)

    completed, payload = _run_validator(tmp_path)

    assert completed.returncode == 1
    assert payload["summary"]["valid"] is False
    assert field in _messages(payload)
    assert "global-only" in _messages(payload)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("runtime", {"root_dir": "/tmp/split-runtime"}),
        ("logging", {"file_enabled": False}),
        ("logging", {"enabled": True, "dir": ".logs"}),
    ],
)
def test_application_system_yaml_rejects_global_only_runtime_and_logging(
    tmp_path: Path,
    field: str,
    value: dict,
) -> None:
    app_root = _create_min_project(tmp_path)
    _write_yaml(app_root / "config" / "system.yaml", {field: value})

    completed, payload = _run_validator(tmp_path)

    assert completed.returncode == 1
    assert payload["summary"]["valid"] is False
    assert field in _messages(payload)
    assert "global-only" in _messages(payload)


def test_worker_path_must_point_to_file(tmp_path: Path) -> None:
    _write_project_config(tmp_path)

    app_root = tmp_path / "applications" / "demo"
    workflows = app_root / "workflows"
    workflows.mkdir(parents=True, exist_ok=True)

    worker_dir_path = app_root / "not_a_worker.yaml"
    worker_dir_path.mkdir(parents=True, exist_ok=True)

    supervisor = {
        "name": "demo_supervisor",
        "agent_runtime": "smolagents",
        "description": "demo",
        "workflow": "# demo\n",
        "worker_agents": [{"path": "applications/demo/not_a_worker.yaml"}],
    }
    _write_yaml(workflows / "demo_agent.yaml", supervisor)

    completed, payload = _run_validator(tmp_path)

    assert completed.returncode == 1
    assert payload["summary"]["valid"] is False
    assert "directory" in _messages(payload).lower()


def test_markdown_agent_body_is_used_as_workflow(tmp_path: Path) -> None:
    _write_project_config(tmp_path)

    workflow_file = tmp_path / "applications" / "demo" / "workflows" / "demo_agent.md"
    _write_markdown(
        workflow_file,
        """```yaml
name: demo_agent
agent_runtime: smolagents
description: demo
```

# Workflow

Run checks here.
""",
    )

    completed, payload = _run_validator(tmp_path)

    assert completed.returncode == 0
    assert payload["summary"]["valid"] is True
    assert payload["errors"] == []


def test_markdown_without_yaml_block_is_rejected(tmp_path: Path) -> None:
    _write_project_config(tmp_path)

    workflow_file = tmp_path / "applications" / "demo" / "workflows" / "demo_agent.md"
    _write_markdown(workflow_file, "# markdown only\n\nno yaml block")

    completed, payload = _run_validator(tmp_path)

    assert completed.returncode == 1
    assert payload["summary"]["valid"] is False
    assert "No YAML code block" in _messages(payload)


def _canonical_errors(project_root: Path, workflow_file: Path) -> list[str]:
    from agentloom.application.definition import definition_error, load_agent_definition, validate_agent_definition

    try:
        definition = load_agent_definition(workflow_file)
    except (OSError, yaml.YAMLError, TypeError, ValueError) as exc:
        return [f"{workflow_file}: {definition_error(exc)}"]
    return validate_agent_definition(project_root, str(workflow_file), definition)


def _assert_canonical_parity(project_root: Path, workflow_file: Path, *, valid: bool) -> dict:
    canonical = _canonical_errors(project_root, workflow_file)
    completed, payload = _run_validator(project_root)
    assert (not canonical) is valid
    assert completed.returncode == (0 if valid else 1), completed.stderr + completed.stdout
    assert payload["summary"]["valid"] is valid
    assert set(canonical).issubset({error["message"] for error in payload["errors"]})
    assert payload["summary"]["error_count"] == len(payload["errors"])
    assert all(set(error) == {"file", "field", "rule", "message", "suggestion"} for error in payload["errors"])
    return payload


def _write_worker(path: Path, **overrides) -> None:
    _write_yaml(path, {
        "name": path.stem,
        "agent_runtime": "smolagents",
        "description": "Worker contract",
        "workflow": "Return the requested evidence.",
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "Requested task",
                }
            },
            "required": ["query"],
            "additionalProperties": False,
        },
        **overrides,
    })


def test_validator_shares_nested_source_relative_worker_paths_and_mcp_null(tmp_path: Path) -> None:
    app = _create_min_project(tmp_path)
    _write_yaml(app / "config/system.yaml", {"mcp_servers": None})
    workflow = app / "workflows/demo_agent.yaml"
    definition = yaml.safe_load(workflow.read_text())
    definition.update({"worker_agents": [{"path": "worker_agents/first.yaml"}], "mcp_servers": None})
    _write_yaml(workflow, definition)
    _write_worker(app / "workflows/worker_agents/first.yaml", worker_agents=[{"path": "./nested/second.md"}])
    second = app / "workflows/worker_agents/nested/second.md"
    _write_markdown(second, """```yaml
name: second
agent_runtime: smolagents
description: Markdown Worker
mcp_servers: null
input_schema:
  type: object
  properties:
    query: {type: string, description: Requested task}
  required: [query]
  additionalProperties: false
```

Return the evidence.
""")

    payload = _assert_canonical_parity(tmp_path, workflow, valid=True)
    assert payload["summary"]["files_checked"] == 3
    assert payload["summary"]["config_files_checked"] == 1


@pytest.mark.parametrize("suffix", [".yaml", ".md"])
@pytest.mark.parametrize("duplicate", ["name: changed\n", "hooks:\n  PreToolUse: []\n  PreToolUse: []\n"])
def test_validator_duplicate_keys_match_shared_parser(tmp_path: Path, suffix: str, duplicate: str) -> None:
    app = _create_min_project(tmp_path)
    old_path = app / "workflows/demo_agent.yaml"
    body = old_path.read_text() + duplicate
    old_path.unlink()
    workflow = old_path.with_suffix(suffix)
    workflow.write_text(f"```yaml\n{body}```\n\nRun the task.\n" if suffix == ".md" else body)

    payload = _assert_canonical_parity(tmp_path, workflow, valid=False)
    assert "Duplicate YAML mapping key" in _messages(payload)


@pytest.mark.parametrize("role_error", ["goal", "invalid_input_schema"])
def test_validator_invalid_referenced_worker_matches_shared_walk(tmp_path: Path, role_error: str) -> None:
    app = _create_min_project(tmp_path)
    workflow = app / "workflows/demo_agent.yaml"
    definition = yaml.safe_load(workflow.read_text())
    definition["worker_agents"] = [{"path": "worker_agents/worker.yaml"}]
    _write_yaml(workflow, definition)
    worker = app / "workflows/worker_agents/worker.yaml"
    _write_worker(worker)
    config = yaml.safe_load(worker.read_text())
    if role_error == "goal":
        config["goal"] = False
    else:
        config["input_schema"] = []
    _write_yaml(worker, config)

    payload = _assert_canonical_parity(tmp_path, workflow, valid=False)
    assert ("goal" if role_error == "goal" else "input_schema") in _messages(payload)


@pytest.mark.parametrize("yaml_workflow,body,valid", [
    ("workflow: Kept YAML workflow\n", "", True),
    ("workflow: ''\n", "Body workflow", True),
    ("", "", False),
])
def test_validator_markdown_workflow_matches_shared_parser(tmp_path: Path, yaml_workflow: str, body: str, valid: bool) -> None:
    app = _create_min_project(tmp_path)
    (app / "workflows/demo_agent.yaml").unlink()
    workflow = app / "workflows/demo_agent.md"
    workflow.write_text(
        f"```yaml\nname: demo\nagent_runtime: smolagents\ndescription: Demo\n{yaml_workflow}```\n\n{body}\n"
    )
    _assert_canonical_parity(tmp_path, workflow, valid=valid)


@pytest.mark.parametrize("model_type", ["unconfigured", 42])
def test_validator_model_reference_matches_shared_preflight(tmp_path: Path, model_type) -> None:
    app = _create_min_project(tmp_path)
    workflow = app / "workflows/demo_agent.yaml"
    definition = yaml.safe_load(workflow.read_text())
    definition["model_type"] = model_type
    _write_yaml(workflow, definition)
    _assert_canonical_parity(tmp_path, workflow, valid=False)


def test_validator_missing_model_catalog_is_a_failure(tmp_path: Path) -> None:
    app = _create_min_project(tmp_path)
    (tmp_path / "config/llm.yaml").unlink()
    payload = _assert_canonical_parity(tmp_path, app / "workflows/demo_agent.yaml", valid=False)
    assert "llm.yaml" in _messages(payload)


def test_validator_unreferenced_nested_worker_uses_shared_role_validation(tmp_path: Path) -> None:
    from agentloom.application.definition import definition_error, load_agent_definition
    from agentloom.application.readiness import validate_runtime_worker_config

    app = _create_min_project(tmp_path)
    worker = app / "workflows/worker_agents/nested/orphan.yaml"
    _write_worker(worker, goal=False)
    with pytest.raises(ValueError) as canonical:
        validate_runtime_worker_config(load_agent_definition(worker), worker, agent_root=tmp_path)
    completed, payload = _run_validator(tmp_path)
    assert completed.returncode == 1
    assert f"{worker}: {definition_error(canonical.value)}" in _messages(payload)


def test_validator_accepts_nested_only_supervisor_and_worker_tree(tmp_path: Path) -> None:
    app = _create_min_project(tmp_path)
    top_level = app / "workflows/demo_agent.yaml"
    top_level.unlink()
    supervisor = app / "workflows/groups/review/supervisor.md"
    worker = app / "workflows/groups/review/worker_agents/deep/worker.yaml"
    _write_markdown(
        supervisor,
        """```yaml
name: nested_supervisor
agent_runtime: smolagents
description: Coordinate a nested workflow
model_type: custom-model-key
worker_agents:
  - path: worker_agents/deep/worker.yaml
```

Ask the Worker for evidence and return it.
""",
    )
    _write_worker(worker)

    completed, payload = _run_validator(tmp_path)

    assert completed.returncode == 0, completed.stderr + completed.stdout
    assert payload["summary"]["valid"] is True
    assert payload["summary"]["files_checked"] == 2
    assert payload["errors"] == []


def test_validator_top_level_definition_cannot_hide_invalid_nested_definitions(tmp_path: Path) -> None:
    app = _create_min_project(tmp_path)
    nested_supervisor = app / "workflows/groups/review/supervisor.md"
    nested_worker = app / "workflows/groups/review/worker_agents/deep/orphan.yaml"
    _write_markdown(
        nested_supervisor,
        """```yaml
name: invalid_nested_supervisor
agent_runtime: smolagents
description: Must be discovered
tools_mapping: {}
```

Run the nested workflow.
""",
    )
    _write_worker(nested_worker, goal=False)

    completed, payload = _run_validator(tmp_path)

    assert completed.returncode == 1
    assert payload["summary"]["valid"] is False
    assert payload["summary"]["files_checked"] == 3
    messages = _messages(payload)
    assert "tools_mapping was removed" in messages
    assert "must not define goal" in messages


def test_validator_does_not_follow_symlinked_definition_files_or_directories(tmp_path: Path) -> None:
    app = _create_min_project(tmp_path)
    outside = tmp_path / "outside"
    invalid_file = outside / "invalid.yaml"
    invalid_file.parent.mkdir()
    invalid_file.write_text(
        "name: linked\nname: duplicate\ndescription: Linked\nworkflow: Work\n",
        encoding="utf-8",
    )
    workflows = app / "workflows"
    (workflows / "linked.yaml").symlink_to(invalid_file)
    (workflows / "linked_group").symlink_to(outside, target_is_directory=True)

    completed, payload = _run_validator(tmp_path)

    assert completed.returncode == 0, completed.stderr + completed.stdout
    assert payload["summary"]["valid"] is True
    assert payload["summary"]["files_checked"] == 1
    assert payload["errors"] == []


def test_validator_does_not_follow_symlinked_workflows_root(tmp_path: Path) -> None:
    app = _create_min_project(tmp_path)
    workflows = app / "workflows"
    outside = tmp_path / "outside_workflows"
    workflows.rename(outside)
    workflows.symlink_to(outside, target_is_directory=True)

    completed, payload = _run_validator(tmp_path)

    assert completed.returncode == 1
    assert payload["summary"]["valid"] is False
    assert payload["summary"]["files_checked"] == 0
    assert "workflows 中没有 Agent YAML/Markdown 定义" in _messages(payload)


def test_skill_scripts_inspect_without_model_tool_or_hook_execution(tmp_path: Path) -> None:
    app = _create_min_project(tmp_path)
    workflow = app / "workflows/demo_agent.yaml"
    definition = yaml.safe_load(workflow.read_text())
    definition.update({
        "tools": [{"name": "read_file"}],
        "hooks": {"SessionStart": [{"id": "no-execution", "command": "touch must-not-exist"}]},
    })
    _write_yaml(workflow, definition)
    tools = app / "agent_tools"
    tools.mkdir()
    (tools / "should_not_import.py").write_text("raise RuntimeError('Tool module must not execute')\n")
    program = """
import runpy, sys
from pathlib import Path
script, app = map(Path, sys.argv[1:])
sys.argv = [str(script), '--app-root', str(app)]
try:
    runpy.run_path(str(script), run_name='__main__')
except SystemExit as exc:
    assert exc.code == 0, exc.code
scanner = runpy.run_path(str(script.with_name('scan_tools.py')))
assert 'should_not_import.py' in scanner['scan_app_structure'](str(app))
for prefix in ('litellm', 'agentloom.application.agent', 'agentloom.runtimes.smolagents.tools.file_ops', 'agentloom.runtimes.smolagents.tools.shell', 'agentloom.runtimes.smolagents.tools.search'):
    assert not any(name == prefix or name.startswith(prefix + '.') for name in sys.modules), prefix
assert not Path('.agentloom').exists()
assert not Path('must-not-exist').exists()
"""
    completed = subprocess.run(
        [sys.executable, "-c", program, str(SCRIPT_PATH), str(app)],
        cwd=tmp_path, capture_output=True, text=True, timeout=20,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr


def test_validator_duplicate_application_overlay_matches_shared_preflight(tmp_path: Path) -> None:
    app = _create_min_project(tmp_path)
    overlay = app / "config/system.yaml"
    overlay.parent.mkdir()
    overlay.write_text("mcp_servers: null\nmcp_servers: []\n")
    payload = _assert_canonical_parity(tmp_path, app / "workflows/demo_agent.yaml", valid=False)
    assert "Duplicate YAML mapping key" in _messages(payload)
