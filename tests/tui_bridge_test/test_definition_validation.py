from pathlib import Path

from src.tui_bridge.bridge import TuiBridge


def test_catalog_uses_runner_required_fields_for_agent_validation(tmp_path: Path) -> None:
    config = tmp_path / "config"
    config.mkdir()
    (config / "llm.yaml").write_text(
        "model:\n  default_model_type: powerful\n  powerful:\n    model: openai/test\n",
        encoding="utf-8",
    )
    valid = tmp_path / "applications/valid/workflows/valid.yaml"
    valid.parent.mkdir(parents=True)
    valid.write_text(
        "name: valid\ndescription: valid agent\nworkflow:\n  - first\n  - second\n",
        encoding="utf-8",
    )
    invalid = tmp_path / "applications/invalid/workflows/invalid.yaml"
    invalid.parent.mkdir(parents=True)
    invalid.write_text(
        "name: invalid\nworkflow: []\n",
        encoding="utf-8",
    )

    systems = {item["application_id"]: item for item in TuiBridge(tmp_path).bootstrap()["systems"]}

    assert systems["valid"]["validation"] == {"valid": True, "errors": []}
    assert systems["invalid"]["validation"]["valid"] is False
    assert any("description" in error for error in systems["invalid"]["validation"]["errors"])
    assert any("workflow" in error for error in systems["invalid"]["validation"]["errors"])


def test_catalog_validation_reuses_runtime_model_structure_and_worker_checks(tmp_path: Path) -> None:
    config = tmp_path / "config"
    config.mkdir()
    (config / "llm.yaml").write_text(
        "model:\n  default_model_type: powerful\n  powerful:\n    model: openai/test\n",
        encoding="utf-8",
    )
    invalid = tmp_path / "applications/invalid/workflows/invalid.yaml"
    invalid.parent.mkdir(parents=True)
    invalid.write_text(
        """\
name: invalid
description: invalid runtime config
model_type: missing-model
tool_call_type: unsupported
worker_agents:
  - path: absent.yaml
workflow: do the task
""",
        encoding="utf-8",
    )

    invalid_extension = tmp_path / "applications/invalid_extension/workflows/invalid.yaml"
    invalid_extension.parent.mkdir(parents=True)
    (invalid_extension.parent / "worker_agents").mkdir()
    (invalid_extension.parent / "worker_agents/worker.txt").write_text("not an agent", encoding="utf-8")
    invalid_extension.write_text(
        """\
name: invalid_extension
description: invalid worker extension
worker_agents:
  - path: worker.txt
workflow: do the task
""",
        encoding="utf-8",
    )

    systems = {system["application_id"]: system for system in TuiBridge(tmp_path).bootstrap()["systems"]}

    errors = "\n".join(systems["invalid"]["validation"]["errors"])
    assert systems["invalid"]["validation"]["valid"] is False
    assert "missing-model" in errors
    assert "tool_call_type" in errors
    assert "absent.yaml" in errors
    extension_errors = "\n".join(systems["invalid_extension"]["validation"]["errors"])
    assert systems["invalid_extension"]["validation"]["valid"] is False
    assert "unsupported extension" in extension_errors


def test_system_detail_includes_runtime_supported_markdown_workers(tmp_path: Path) -> None:
    config = tmp_path / "config"
    config.mkdir()
    (config / "llm.yaml").write_text(
        "model:\n  default_model_type: powerful\n  powerful:\n    model: openai/test\n",
        encoding="utf-8",
    )
    workflow = tmp_path / "applications/markdown/workflows/supervisor.yaml"
    workflow.parent.mkdir(parents=True)
    workflow.write_text(
        """\
name: markdown_supervisor
description: delegates to a Markdown worker
worker_agents:
  - path: worker.md
workflow: delegate the task
""",
        encoding="utf-8",
    )
    worker = workflow.parent / "worker_agents/worker.md"
    worker.parent.mkdir()
    worker.write_text(
        """\
```yaml
name: markdown_worker
description: worker stored as Markdown
agent_function_schema:
  description: Handle one task.
  inputs:
    task:
      description: Task to handle.
      required: true
  output:
    description: Worker result.
```

Handle the supplied task.
""",
        encoding="utf-8",
    )

    bridge = TuiBridge(tmp_path)
    [system] = bridge.bootstrap()["systems"]
    detail = bridge.system_detail(system["id"])

    assert system["validation"] == {"valid": True, "errors": []}
    assert detail["topology"]["workers"] == [
        {
            "name": "markdown_worker",
            "path": "applications/markdown/workflows/worker_agents/worker.md",
            "description": "worker stored as Markdown",
        }
    ]
    assert detail["files"][1]["path"].endswith("worker.md")


def test_catalog_rejects_invalid_markdown_worker_schema(tmp_path: Path) -> None:
    config = tmp_path / "config"
    config.mkdir()
    (config / "llm.yaml").write_text(
        "model:\n  default_model_type: powerful\n  powerful:\n    model: openai/test\n",
        encoding="utf-8",
    )
    workflow = tmp_path / "applications/markdown/workflows/supervisor.yaml"
    workflow.parent.mkdir(parents=True)
    workflow.write_text(
        """\
name: markdown_supervisor
description: delegates to a Markdown worker
worker_agents:
  - path: worker.md
workflow: delegate the task
""",
        encoding="utf-8",
    )
    worker = workflow.parent / "worker_agents/worker.md"
    worker.parent.mkdir()
    worker.write_text(
        """\
```yaml
name: markdown_worker
description: invalid worker schema
agent_function_schema:
  description: Handle one task.
  inputs: []
  output:
    description: Worker result.
```

Handle the supplied task.
""",
        encoding="utf-8",
    )

    [system] = TuiBridge(tmp_path).bootstrap()["systems"]

    errors = "\n".join(system["validation"]["errors"])
    assert system["validation"]["valid"] is False
    assert "worker.md" in errors
    assert "agent_function_schema.inputs" in errors


def test_catalog_rejects_referenced_worker_without_function_schema(tmp_path: Path) -> None:
    config = tmp_path / "config"
    config.mkdir()
    (config / "llm.yaml").write_text(
        "model:\n  default_model_type: powerful\n  powerful:\n    model: openai/test\n",
        encoding="utf-8",
    )
    workflow = tmp_path / "applications/no_schema/workflows/supervisor.yaml"
    workflow.parent.mkdir(parents=True)
    workflow.write_text(
        """\
name: supervisor
description: delegates to a worker
worker_agents:
  - path: worker.yaml
workflow: delegate the task
""",
        encoding="utf-8",
    )
    worker = workflow.parent / "worker_agents/worker.yaml"
    worker.parent.mkdir()
    worker.write_text(
        "name: worker\ndescription: worker without tool schema\nworkflow: do the task\n",
        encoding="utf-8",
    )

    [system] = TuiBridge(tmp_path).bootstrap()["systems"]

    errors = "\n".join(system["validation"]["errors"])
    assert system["validation"]["valid"] is False
    assert "worker.yaml" in errors
    assert "agent_function_schema is required" in errors


def test_catalog_rejects_existing_worker_with_unconfigured_model(tmp_path: Path) -> None:
    config = tmp_path / "config"
    config.mkdir()
    (config / "llm.yaml").write_text(
        "model:\n  default_model_type: powerful\n  powerful:\n    model: openai/test\n",
        encoding="utf-8",
    )
    workflow = tmp_path / "applications/bad_model/workflows/supervisor.yaml"
    workflow.parent.mkdir(parents=True)
    workflow.write_text(
        """\
name: supervisor
description: delegates to a worker
worker_agents:
  - path: worker.yaml
workflow: delegate the task
""",
        encoding="utf-8",
    )
    worker = workflow.parent / "worker_agents/worker.yaml"
    worker.parent.mkdir()
    worker.write_text(
        """\
name: worker
description: worker with missing model
model_type: definitely_missing
workflow: do the task
agent_function_schema:
  description: Handle one task.
  inputs:
    task:
      description: Task to handle.
  output:
    description: Worker result.
""",
        encoding="utf-8",
    )

    [system] = TuiBridge(tmp_path).bootstrap()["systems"]

    errors = "\n".join(system["validation"]["errors"])
    assert system["validation"]["valid"] is False
    assert "worker.yaml" in errors
    assert "model_type 'definitely_missing' is not configured" in errors
