from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import agentloom.application.factory as application_factory
import agentloom.application.runner as runner
import pytest
from agentloom.application import definition as application_definition
from agentloom.application.definition import (
    inspect_supervisor_definition,
    load_agent_definition,
    prepare_application_definition,
)
from agentloom.application.factory import YamlAgentFactory, YamlConfiguredSupervisorAgent
from agentloom.configuration.config import UnifiedConfig, load_project_config


def _project_config(root: Path) -> UnifiedConfig:
    config = root / "config"
    config.mkdir(parents=True, exist_ok=True)
    (config / "system.yaml").write_text("{}\n", encoding="utf-8")
    (config / "llm.yaml").write_text(
        (
            "model:\n"
            "  default_model_type: test\n"
            "  test: {model: openai/test, adapter: openai_chat}\n"
            "  summary: {model: openai/test-summary, adapter: openai_chat}\n"
        ),
        encoding="utf-8",
    )
    return load_project_config(root)


def _supervisor(root: Path) -> Path:
    path = root / "applications/demo/workflows/root.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        ("name: demo\nagent_runtime: smolagents\ndescription: Demo supervisor.\nworkflow: Run the task.\n"),
        encoding="utf-8",
    )
    return path


def _supervisor_with_worker(root: Path) -> tuple[Path, Path]:
    supervisor = _supervisor(root)
    worker = supervisor.parent / "worker_agents/worker.yaml"
    worker.parent.mkdir()
    worker.write_text(
        (
            "name: worker\n"
            "agent_runtime: smolagents\n"
            "description: Demo worker.\n"
            "workflow: Complete the delegated task.\n"
            "agent_function_schema:\n"
            "  description: Work.\n"
            "  inputs: {task: {description: Task, required: true}}\n"
            "  output: {description: Result}\n"
        ),
        encoding="utf-8",
    )
    supervisor.write_text(
        supervisor.read_text(encoding="utf-8") + "worker_agents:\n  - path: worker.yaml\n",
        encoding="utf-8",
    )
    return supervisor, worker


@pytest.mark.parametrize(
    "replacement",
    ["symlink", "invalid", "worker_symlink", "worker_race"],
)
def test_scheduled_supervisor_target_is_revalidated_before_run_allocation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    replacement: str,
) -> None:
    base = _project_config(tmp_path)
    if replacement in {"worker_symlink", "worker_race"}:
        path, worker = _supervisor_with_worker(tmp_path)
        outside = tmp_path.parent / f"{tmp_path.name}-outside-worker.yaml"
        outside.write_text(worker.read_text(encoding="utf-8"), encoding="utf-8")
        if replacement == "worker_symlink":
            worker.unlink()
            worker.symlink_to(outside)
        else:
            from agentloom.application import paths as application_paths

            original_candidate = application_paths.worker_reference_candidate

            def swap_after_resolution(*args, **kwargs):
                candidate = original_candidate(*args, **kwargs)
                worker.unlink()
                worker.symlink_to(outside)
                return candidate

            monkeypatch.setattr(
                application_paths,
                "worker_reference_candidate",
                swap_after_resolution,
            )
    else:
        path = _supervisor(tmp_path)
    if replacement == "symlink":
        outside = tmp_path.parent / f"{tmp_path.name}-outside.yaml"
        outside.write_text(path.read_text(encoding="utf-8"), encoding="utf-8")
        path.unlink()
        path.symlink_to(outside)
    elif replacement == "invalid":
        path.write_text("name: invalid\n", encoding="utf-8")

    monkeypatch.setattr(runner, "C", SimpleNamespace(agent_root=tmp_path))
    monkeypatch.setattr(runner, "get_config", lambda: base)
    monkeypatch.setattr(
        runner,
        "generate_runtime_id",
        lambda *args: pytest.fail("allocated a Run for an invalid target"),
    )
    events = []

    with pytest.raises(ValueError):
        runner.execute_app(
            path.relative_to(tmp_path).as_posix(),
            event_sink=events.append,
            require_valid_supervisor_target=True,
        )

    assert not (tmp_path / ".agentloom").exists()
    assert len(events) == 1 and events[0].event == "run.rejected"


def test_scheduled_worker_definition_stays_pinned_during_agent_construction(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    base = _project_config(tmp_path)
    supervisor, worker = _supervisor_with_worker(tmp_path)
    inspection = inspect_supervisor_definition(
        tmp_path,
        supervisor.relative_to(tmp_path).as_posix(),
        base_config=base,
    )
    assert inspection.errors == ()
    assert inspection.prepared_definition is not None
    outside = tmp_path.parent / f"{tmp_path.name}-outside-worker.yaml"
    outside.write_text(
        worker.read_text(encoding="utf-8").replace("name: worker", "name: outside"),
        encoding="utf-8",
    )
    worker.unlink()
    worker.symlink_to(outside)
    captured: list[dict] = []
    original_create_agent_as_tool = YamlAgentFactory.create_agent_as_tool

    class CapturingAgent:
        def __init__(self, *, config: dict, **_kwargs) -> None:
            captured.append(config)

        @staticmethod
        def agent_as_tool():
            return lambda: None

    def create_from_snapshot(config: dict, **kwargs):
        return original_create_agent_as_tool(
            config,
            agent_class=CapturingAgent,
            **kwargs,
        )

    monkeypatch.setattr(
        YamlAgentFactory,
        "get_tools_from_config",
        staticmethod(lambda *_args, **_kwargs: ([], None)),
    )
    monkeypatch.setattr(
        YamlAgentFactory,
        "create_agent_as_tool",
        staticmethod(create_from_snapshot),
    )
    monkeypatch.setattr(
        application_factory,
        "C",
        SimpleNamespace(agent_root=tmp_path),
    )
    agent = object.__new__(YamlConfiguredSupervisorAgent)
    agent._config = inspection.prepared_definition
    agent._effective_agent_config = {}
    agent._mcp_manager = None
    agent.logger = None
    agent._logger = None

    agent._get_tools()

    assert captured[0]["name"] == "worker"
    assert captured[0]["_yaml_file_path"] == str(worker)


def test_scheduled_supervisor_uses_pinned_application_identity(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    base = _project_config(tmp_path)
    supervisor = _supervisor(tmp_path)
    inspection = inspect_supervisor_definition(
        tmp_path,
        supervisor.relative_to(tmp_path).as_posix(),
        base_config=base,
    )
    assert inspection.errors == ()
    assert inspection.prepared_definition is not None
    application_root = tmp_path / "applications/demo"
    application_root.rename(tmp_path / "old-demo")
    outside = tmp_path.parent / f"{tmp_path.name}-outside-application"
    outside.mkdir()
    application_root.symlink_to(outside, target_is_directory=True)
    monkeypatch.setattr(
        application_factory,
        "infer_category_from_yaml_path",
        lambda *_args: pytest.fail("resolved a live Supervisor path"),
    )
    agent = object.__new__(YamlConfiguredSupervisorAgent)
    agent._config = inspection.prepared_definition

    agent._before_config_validation()

    assert agent.workflow_category == "demo"


def test_ordinary_yaml_cannot_forge_pinned_application_identity(
    tmp_path: Path,
) -> None:
    base = _project_config(tmp_path)
    supervisor = tmp_path / "applications/real/workflows/root.yaml"
    supervisor.parent.mkdir(parents=True)
    supervisor.write_text(
        (
            "name: real\n"
            "agent_runtime: smolagents\n"
            "description: Real supervisor.\n"
            "workflow: Run the task.\n"
            "_application_id: forged\n"
        ),
        encoding="utf-8",
    )
    prepared = prepare_application_definition(
        tmp_path,
        supervisor,
        load_agent_definition(supervisor),
        base_config=base,
    )
    agent = object.__new__(YamlConfiguredSupervisorAgent)
    agent._config = prepared

    agent._before_config_validation()

    assert "_application_id" not in prepared
    assert agent.workflow_category == "real"


def test_scheduled_worker_path_is_not_resolved_again_after_snapshot(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    base = _project_config(tmp_path)
    supervisor, worker = _supervisor_with_worker(tmp_path)
    outside = tmp_path.parent / f"{tmp_path.name}-outside-worker-after-read.yaml"
    outside.write_text(
        worker.read_text(encoding="utf-8").replace("name: worker", "name: outside"),
        encoding="utf-8",
    )
    from agentloom.application import paths as application_paths

    original_candidate = application_paths.worker_reference_candidate
    calls = 0

    def swap_after_snapshot(*args, **kwargs):
        nonlocal calls
        candidate = original_candidate(*args, **kwargs)
        calls += 1
        if calls == 2:
            worker.unlink()
            worker.symlink_to(outside)
        return candidate

    monkeypatch.setattr(
        application_paths,
        "worker_reference_candidate",
        swap_after_snapshot,
    )

    with pytest.raises(ValueError, match="real project Supervisor"):
        inspect_supervisor_definition(
            tmp_path,
            supervisor.relative_to(tmp_path).as_posix(),
            base_config=base,
        )


def test_scheduled_definition_snapshot_rejects_application_directory_replacement(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    base = _project_config(tmp_path)
    supervisor, worker = _supervisor_with_worker(tmp_path)
    app_config = tmp_path / "applications/demo/config/system.yaml"
    app_config.parent.mkdir()
    app_config.write_text(
        'runtime_options: {todo_mode: "on"}\n',
        encoding="utf-8",
    )
    replacement = tmp_path / "replacement"
    replacement_supervisor = replacement / "workflows/root.yaml"
    replacement_worker = replacement / "workflows/worker_agents/worker.yaml"
    replacement_config = replacement / "config/system.yaml"
    replacement_worker.parent.mkdir(parents=True)
    replacement_config.parent.mkdir(parents=True)
    replacement_supervisor.write_text(
        (
            "name: replacement\n"
            "agent_runtime: smolagents\n"
            "description: Replacement supervisor.\n"
            "workflow: Run the replacement task.\n"
            "worker_agents:\n"
            "  - path: worker.yaml\n"
        ),
        encoding="utf-8",
    )
    replacement_worker.write_text(
        worker.read_text(encoding="utf-8").replace(
            "name: worker",
            "name: replacement_worker",
        ),
        encoding="utf-8",
    )
    replacement_config.write_text(
        'runtime_options: {todo_mode: "off"}\n',
        encoding="utf-8",
    )
    original_read = application_definition._DefinitionSnapshotSession.read
    swapped = False

    def swap_application_after_supervisor_read(
        session,
        relative: Path,
        *,
        optional: bool = False,
    ):
        nonlocal swapped
        snapshot = original_read(session, relative, optional=optional)
        if not swapped and relative.as_posix() == "applications/demo/workflows/root.yaml":
            application_root = tmp_path / "applications/demo"
            application_root.rename(tmp_path / "old-demo")
            replacement.rename(application_root)
            swapped = True
        return snapshot

    monkeypatch.setattr(
        application_definition._DefinitionSnapshotSession,
        "read",
        swap_application_after_supervisor_read,
    )
    monkeypatch.setattr(runner, "C", SimpleNamespace(agent_root=tmp_path))
    monkeypatch.setattr(runner, "get_config", lambda: base)
    monkeypatch.setattr(
        runner,
        "generate_runtime_id",
        lambda *args: pytest.fail("allocated a Run for a mixed Application snapshot"),
    )
    events = []

    with pytest.raises(ValueError, match="real project Supervisor"):
        runner.execute_app(
            "applications/demo/workflows/root.yaml",
            event_sink=events.append,
            require_valid_supervisor_target=True,
        )

    assert not (tmp_path / ".agentloom").exists()
    assert len(events) == 1 and events[0].event == "run.rejected"

    monkeypatch.setattr(
        application_definition._DefinitionSnapshotSession,
        "read",
        original_read,
    )
    inspection = inspect_supervisor_definition(
        tmp_path,
        "applications/demo/workflows/root.yaml",
        base_config=base,
    )
    assert inspection.errors == ()
    assert inspection.definition["name"] == "replacement"
    assert inspection.prepared_definition is not None
    pinned = inspection.prepared_definition["_worker_definitions"]
    worker_config = next(iter(pinned.values()))
    assert worker_config["name"] == "replacement_worker"
    snapshot = worker_config["_effective_agent_config_snapshot"]
    assert snapshot.values["runtime_options"]["todo_mode"] == "off"


def test_scheduled_definition_snapshot_rejects_new_application_config(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    base = _project_config(tmp_path)
    supervisor = _supervisor(tmp_path)
    app_config = tmp_path / "applications/demo/config/system.yaml"
    original_read_config = application_definition._DefinitionSnapshotSession.read_config
    created = False

    def create_config_after_absence(session, path: Path):
        nonlocal created
        config = original_read_config(session, path)
        if not created:
            app_config.parent.mkdir()
            app_config.write_text(
                'runtime_options: {todo_mode: "on"}\n',
                encoding="utf-8",
            )
            created = True
        return config

    monkeypatch.setattr(
        application_definition._DefinitionSnapshotSession,
        "read_config",
        create_config_after_absence,
    )

    with pytest.raises(ValueError, match="real project Supervisor"):
        inspect_supervisor_definition(
            tmp_path,
            supervisor.relative_to(tmp_path).as_posix(),
            base_config=base,
        )


def test_definition_snapshot_revision_tracks_definitions_and_application_config(
    tmp_path: Path,
) -> None:
    base = _project_config(tmp_path)
    supervisor, worker = _supervisor_with_worker(tmp_path)
    app_config = tmp_path / "applications/demo/config/system.yaml"
    app_config.parent.mkdir()
    app_config.write_text(
        'runtime_options: {todo_mode: "on"}\n',
        encoding="utf-8",
    )
    relative = supervisor.relative_to(tmp_path).as_posix()

    first = inspect_supervisor_definition(tmp_path, relative, base_config=base)
    unchanged = inspect_supervisor_definition(tmp_path, relative, base_config=base)
    worker.write_text(
        worker.read_text(encoding="utf-8").replace(
            "Complete the delegated task.",
            "Complete the updated task.",
        ),
        encoding="utf-8",
    )
    worker_changed = inspect_supervisor_definition(tmp_path, relative, base_config=base)
    app_config.write_text(
        'runtime_options: {todo_mode: "off"}\n',
        encoding="utf-8",
    )
    config_changed = inspect_supervisor_definition(tmp_path, relative, base_config=base)

    assert unchanged.definition_snapshot_revision == first.definition_snapshot_revision
    assert worker_changed.definition_snapshot_revision != first.definition_snapshot_revision
    assert config_changed.definition_snapshot_revision != worker_changed.definition_snapshot_revision


def test_scheduled_run_manifest_preserves_both_revision_contracts(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    base = _project_config(tmp_path)
    supervisor = _supervisor(tmp_path)

    class SuccessfulSupervisor:
        def __init__(self, **_kwargs) -> None:
            self._mcp_manager = None

        @staticmethod
        def run(*_args, **_kwargs) -> str:
            return "completed"

    monkeypatch.setattr(runner, "C", SimpleNamespace(agent_root=tmp_path))
    monkeypatch.setattr(runner, "get_config", lambda: base)
    monkeypatch.setattr(
        runner,
        "YamlConfiguredSupervisorAgent",
        SuccessfulSupervisor,
    )

    result = runner.execute_app(
        supervisor.relative_to(tmp_path).as_posix(),
        file_logging=False,
        require_valid_supervisor_target=True,
    )

    manifest = json.loads(result.run.manifest_path.read_text(encoding="utf-8"))
    assert manifest["application_revision"].startswith("sha256:")
    assert manifest["definition_snapshot_revision"].startswith("sha256:")
