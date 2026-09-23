from pathlib import Path

from agentloom.app.studio.query_service import StudioQueryService


def test_catalog_uses_runner_required_fields_for_agent_validation(tmp_path: Path) -> None:
    config = tmp_path / "config"
    config.mkdir()
    (config / "llm.yaml").write_text(
        "model:\n  summary:\n    model: openai/test-summary\n    adapter: openai_chat\n  default_model_type: powerful\n  powerful:\n    model: openai/test\n    adapter: openai_chat\n",
        encoding="utf-8",
    )
    valid = tmp_path / "applications/valid/workflows/valid.yaml"
    valid.parent.mkdir(parents=True)
    valid.write_text(
        "name: valid\nagent_runtime: smolagents\ndescription: valid agent\nworkflow: |\n  first\n  second\n",
        encoding="utf-8",
    )
    invalid = tmp_path / "applications/invalid/workflows/invalid.yaml"
    invalid.parent.mkdir(parents=True)
    invalid.write_text(
        "name: invalid\nagent_runtime: smolagents\nworkflow: []\n",
        encoding="utf-8",
    )

    systems = {item["application_id"]: item for item in StudioQueryService(tmp_path).bootstrap()["systems"]}

    assert systems["valid"]["validation"] == {"valid": True, "errors": []}
    assert systems["invalid"]["validation"]["valid"] is False
    assert any("description" in error for error in systems["invalid"]["validation"]["errors"])
    assert any("workflow" in error for error in systems["invalid"]["validation"]["errors"])


def test_catalog_validation_reuses_runtime_model_structure_and_worker_checks(tmp_path: Path) -> None:
    config = tmp_path / "config"
    config.mkdir()
    (config / "llm.yaml").write_text(
        "model:\n  summary:\n    model: openai/test-summary\n    adapter: openai_chat\n  default_model_type: powerful\n  powerful:\n    model: openai/test\n    adapter: openai_chat\n",
        encoding="utf-8",
    )
    invalid = tmp_path / "applications/invalid/workflows/invalid.yaml"
    invalid.parent.mkdir(parents=True)
    invalid.write_text(
        """\
name: invalid
agent_runtime: langgraph
description: invalid runtime config
model_type: missing-model
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
agent_runtime: smolagents
description: invalid worker extension
worker_agents:
  - path: worker.txt
workflow: do the task
""",
        encoding="utf-8",
    )

    systems = {system["application_id"]: system for system in StudioQueryService(tmp_path).bootstrap()["systems"]}

    errors = "\n".join(systems["invalid"]["validation"]["errors"])
    assert systems["invalid"]["validation"]["valid"] is False
    assert "missing-model" in errors
    assert "langgraph" in errors
    assert "absent.yaml" in errors
    extension_errors = "\n".join(systems["invalid_extension"]["validation"]["errors"])
    assert systems["invalid_extension"]["validation"]["valid"] is False
    assert "unsupported extension" in extension_errors


def test_system_detail_includes_runtime_supported_markdown_workers(tmp_path: Path) -> None:
    config = tmp_path / "config"
    config.mkdir()
    (config / "llm.yaml").write_text(
        "model:\n  summary:\n    model: openai/test-summary\n    adapter: openai_chat\n  default_model_type: powerful\n  powerful:\n    model: openai/test\n    adapter: openai_chat\n",
        encoding="utf-8",
    )
    workflow = tmp_path / "applications/markdown/workflows/supervisor.yaml"
    workflow.parent.mkdir(parents=True)
    workflow.write_text(
        """\
name: markdown_supervisor
agent_runtime: smolagents
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
agent_runtime: smolagents
description: worker stored as Markdown
input_schema:
  type: object
  properties:
    task:
      type: string
      description: Task to handle.
  required: [task]
  additionalProperties: false
```

Handle the supplied task.
""",
        encoding="utf-8",
    )

    bridge = StudioQueryService(tmp_path)
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
        "model:\n  summary:\n    model: openai/test-summary\n    adapter: openai_chat\n  default_model_type: powerful\n  powerful:\n    model: openai/test\n    adapter: openai_chat\n",
        encoding="utf-8",
    )
    workflow = tmp_path / "applications/markdown/workflows/supervisor.yaml"
    workflow.parent.mkdir(parents=True)
    workflow.write_text(
        """\
name: markdown_supervisor
agent_runtime: smolagents
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
agent_runtime: smolagents
description: invalid worker schema
input_schema:
  type: array
  items:
    type: string
```

Handle the supplied task.
""",
        encoding="utf-8",
    )

    [system] = StudioQueryService(tmp_path).bootstrap()["systems"]

    errors = "\n".join(system["validation"]["errors"])
    assert system["validation"]["valid"] is False
    assert "worker.md" in errors
    assert "input_schema root type must be object" in errors


def test_catalog_accepts_referenced_worker_without_explicit_input_schema(tmp_path: Path) -> None:
    config = tmp_path / "config"
    config.mkdir()
    (config / "llm.yaml").write_text(
        "model:\n  summary:\n    model: openai/test-summary\n    adapter: openai_chat\n  default_model_type: powerful\n  powerful:\n    model: openai/test\n    adapter: openai_chat\n",
        encoding="utf-8",
    )
    workflow = tmp_path / "applications/no_schema/workflows/supervisor.yaml"
    workflow.parent.mkdir(parents=True)
    workflow.write_text(
        """\
name: supervisor
agent_runtime: smolagents
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
        "name: worker\nagent_runtime: smolagents\ndescription: worker without tool schema\nworkflow: do the task\n",
        encoding="utf-8",
    )

    [system] = StudioQueryService(tmp_path).bootstrap()["systems"]

    assert system["validation"] == {"valid": True, "errors": []}


def test_catalog_rejects_existing_worker_with_unconfigured_model(tmp_path: Path) -> None:
    config = tmp_path / "config"
    config.mkdir()
    (config / "llm.yaml").write_text(
        "model:\n  summary:\n    model: openai/test-summary\n    adapter: openai_chat\n  default_model_type: powerful\n  powerful:\n    model: openai/test\n    adapter: openai_chat\n",
        encoding="utf-8",
    )
    workflow = tmp_path / "applications/bad_model/workflows/supervisor.yaml"
    workflow.parent.mkdir(parents=True)
    workflow.write_text(
        """\
name: supervisor
agent_runtime: smolagents
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
agent_runtime: smolagents
description: worker with missing model
model_type: definitely_missing
workflow: do the task
input_schema:
  type: object
  properties:
    task:
      type: string
      description: Task to handle.
  required: [task]
  additionalProperties: false
""",
        encoding="utf-8",
    )

    [system] = StudioQueryService(tmp_path).bootstrap()["systems"]

    errors = "\n".join(system["validation"]["errors"])
    assert system["validation"]["valid"] is False
    assert "worker.yaml" in errors
    assert "model_type 'definitely_missing' is not configured" in errors
