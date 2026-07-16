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


def _create_min_project(tmp_path: Path, *, skills_value=None) -> Path:
    (tmp_path / "config").mkdir(parents=True, exist_ok=True)
    (tmp_path / "config" / "system.yaml").write_text("system: {}\n", encoding="utf-8")

    app_root = tmp_path / "applications" / "demo"
    workflow_file = app_root / "workflows" / "demo_agent.yaml"
    config = {
        "name": "demo_agent",
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
        "skills/agent-recall-with-files",
        {"path": "skills/agent-recall-with-files", "platform": "Claude"},
        [
            "skills/agent-recall-with-files",
            {
                "path": "skills/agent-visualization",
                "load-mode": "eager",
                "allow-scripts": False,
            },
        ],
        {
            "load-mode": "on-demand",
            "allow-network": False,
            "items": [
                "skills/agent-recall-with-files",
                {"path": "skills/agent-visualization", "load-mode": "eager"},
            ],
        },
    ],
)
def test_skills_config_formats_are_supported(tmp_path: Path, skills_value) -> None:
    _create_min_project(tmp_path, skills_value=skills_value)

    completed, payload = _run_validator(tmp_path)

    assert completed.returncode == 0
    assert payload["summary"]["valid"] is True
    assert payload["errors"] == []


def test_invalid_skills_type_is_rejected(tmp_path: Path) -> None:
    _create_min_project(tmp_path, skills_value=123)

    completed, payload = _run_validator(tmp_path)

    assert completed.returncode == 1
    assert payload["summary"]["valid"] is False
    assert any(
        err["field"] == "skills" and err["rule"] == "type_list_dict_or_string"
        for err in payload["errors"]
    )


def test_validator_accepts_list_workflow(tmp_path: Path) -> None:
    _create_min_project(tmp_path)
    workflow_file = tmp_path / "applications" / "demo" / "workflows" / "demo_agent.yaml"
    config = yaml.safe_load(workflow_file.read_text(encoding="utf-8"))
    config["workflow"] = [
        "# First workflow item\nRun the first task.",
        "# Second workflow item\nUse memory from the first task.",
    ]
    _write_yaml(workflow_file, config)

    completed, payload = _run_validator(tmp_path)

    assert completed.returncode == 0
    assert payload["summary"]["valid"] is True
    assert payload["errors"] == []


def test_validator_rejects_invalid_list_workflow_item(tmp_path: Path) -> None:
    _create_min_project(tmp_path)
    workflow_file = tmp_path / "applications" / "demo" / "workflows" / "demo_agent.yaml"
    config = yaml.safe_load(workflow_file.read_text(encoding="utf-8"))
    config["workflow"] = ["# First workflow item", ""]
    _write_yaml(workflow_file, config)

    completed, payload = _run_validator(tmp_path)

    assert completed.returncode == 1
    assert payload["summary"]["valid"] is False
    assert any(
        err["field"] == "workflow[1]" and err["rule"] == "required_non_empty_string"
        for err in payload["errors"]
    )


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
    matching = [error for error in payload["errors"] if error["field"] == field]
    assert matching
    assert all(error["rule"] == "global_only_top_level_key" for error in matching)
    assert all("enabled: true" not in error["suggestion"] for error in matching)


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
    matching = [error for error in payload["errors"] if error["field"] == field]
    assert matching
    assert all(error["rule"] == "global_only_top_level_key" for error in matching)
    assert all("enabled: true" not in error["suggestion"] for error in matching)


def test_worker_path_must_point_to_file(tmp_path: Path) -> None:
    (tmp_path / "config").mkdir(parents=True, exist_ok=True)
    (tmp_path / "config" / "system.yaml").write_text("system: {}\n", encoding="utf-8")

    app_root = tmp_path / "applications" / "demo"
    workflows = app_root / "workflows"
    workflows.mkdir(parents=True, exist_ok=True)

    worker_dir_path = app_root / "not_a_worker.yaml"
    worker_dir_path.mkdir(parents=True, exist_ok=True)

    supervisor = {
        "name": "demo_supervisor",
        "description": "demo",
        "workflow": "# demo\n",
        "worker_agents": [{"path": "applications/demo/not_a_worker.yaml"}],
    }
    _write_yaml(workflows / "demo_agent.yaml", supervisor)

    completed, payload = _run_validator(tmp_path)

    assert completed.returncode == 1
    assert payload["summary"]["valid"] is False
    assert any(err["rule"] == "path_is_file" for err in payload["errors"])


def test_markdown_agent_body_is_used_as_workflow(tmp_path: Path) -> None:
    (tmp_path / "config").mkdir(parents=True, exist_ok=True)
    (tmp_path / "config" / "system.yaml").write_text("system: {}\n", encoding="utf-8")

    workflow_file = tmp_path / "applications" / "demo" / "workflows" / "demo_agent.md"
    _write_markdown(
        workflow_file,
        """```yaml
name: demo_agent
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
    (tmp_path / "config").mkdir(parents=True, exist_ok=True)
    (tmp_path / "config" / "system.yaml").write_text("system: {}\n", encoding="utf-8")

    workflow_file = tmp_path / "applications" / "demo" / "workflows" / "demo_agent.md"
    _write_markdown(workflow_file, "# markdown only\n\nno yaml block")

    completed, payload = _run_validator(tmp_path)

    assert completed.returncode == 1
    assert payload["summary"]["valid"] is False
    assert any(
        err["field"] == "yaml_parse" and err["rule"] == "parse_success"
        for err in payload["errors"]
    )
