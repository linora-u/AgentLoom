from pathlib import Path

import pytest

import src.lib.smolagents.agent.yaml_agent_factory as yaml_factory_module
from src.lib.smolagents.agent.agent_validation import AgentConfigNormalizer
from src.lib.smolagents.agent.yaml_agent_factory import YamlAgentFactory, YamlConfiguredSupervisorAgent


class _DummyLogger:
    def __init__(self):
        self.errors = []
        self.infos = []
        self.warnings = []

    def error(self, msg):
        self.errors.append(msg)

    def info(self, msg):
        self.infos.append(msg)

    def warning(self, msg):
        self.warnings.append(msg)


def _write_min_worker_yaml(path: Path, name: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f'name: "{name}"\n', encoding="utf-8")


def _build_supervisor_for_get_tools(worker_agents: list[dict]) -> YamlConfiguredSupervisorAgent:
    # Bypass heavy __init__ path; _get_tools only needs these fields.
    agent = object.__new__(YamlConfiguredSupervisorAgent)
    agent._config = {
        "name": "test_supervisor",
        "description": "desc",
        "tools": [],
        "workflow": "wf",
        "worker_agents": worker_agents,
    }
    agent._inferred_category = "dummy_category"
    agent._execution_env = None
    agent._logger = _DummyLogger()
    return agent


def test_validate_worker_agents_config_requires_path_only():
    with pytest.raises(ValueError, match="unsupported field 'name'"):
        AgentConfigNormalizer.validate_worker_agents_config([{"name": "step1"}])

    with pytest.raises(ValueError, match="missing required non-empty 'path'"):
        AgentConfigNormalizer.validate_worker_agents_config([{"path": ""}])


def test_precheck_resolves_shorthand_relative_and_absolute_paths(tmp_path, monkeypatch):
    worker_folder = tmp_path / "applications" / "demo" / "workflows" / "worker_agents"
    shorthand_file = worker_folder / "alpha.yaml"
    relative_file = tmp_path / "applications" / "other" / "workflows" / "worker_agents" / "beta.yaml"
    absolute_file = tmp_path / "gamma.yaml"

    _write_min_worker_yaml(shorthand_file, "alpha")
    _write_min_worker_yaml(relative_file, "beta")
    _write_min_worker_yaml(absolute_file, "gamma")

    monkeypatch.setattr(yaml_factory_module, "AGENT_ROOT", tmp_path)

    expected_agents = [
        {"path": "alpha.yaml"},
        {"path": "applications/other/workflows/worker_agents/beta.yaml"},
        {"path": str(absolute_file)},
    ]
    resolved = AgentConfigNormalizer.precheck_worker_agent_paths(
        expected_agents,
        worker_folder,
        agent_root=tmp_path,
    )

    assert resolved == [
        ("alpha.yaml", shorthand_file.resolve()),
        ("applications/other/workflows/worker_agents/beta.yaml", relative_file.resolve()),
        (str(absolute_file), absolute_file.resolve()),
    ]


def test_precheck_aggregates_errors_and_raises(tmp_path, monkeypatch):
    worker_folder = tmp_path / "applications" / "demo" / "workflows" / "worker_agents"
    worker_folder.mkdir(parents=True, exist_ok=True)

    bad_ext_file = worker_folder / "bad.txt"
    bad_ext_file.write_text("name: bad", encoding="utf-8")

    monkeypatch.setattr(yaml_factory_module, "AGENT_ROOT", tmp_path)

    expected_agents = [
        {"path": "missing_worker.yaml"},
        {"path": "bad.txt"},
        {"path": "applications/not_found/workflows/worker_agents/none.yaml"},
    ]

    with pytest.raises(ValueError, match="worker_agents precheck failed") as exc:
        AgentConfigNormalizer.precheck_worker_agent_paths(
            expected_agents,
            worker_folder,
            agent_root=tmp_path,
        )

    msg = str(exc.value)
    assert "worker_agents[0]" in msg
    assert "worker_agents[1]" in msg
    assert "worker_agents[2]" in msg


def test_get_tools_precheck_failure_aborts_loading(tmp_path, monkeypatch):
    worker_folder = tmp_path / "applications" / "demo" / "workflows" / "worker_agents"
    _write_min_worker_yaml(worker_folder / "ok_worker.yaml", "ok_worker")

    monkeypatch.setattr(yaml_factory_module, "AGENT_ROOT", tmp_path)
    monkeypatch.setattr(yaml_factory_module, "get_worker_agent_yaml_path", lambda _category: worker_folder)
    monkeypatch.setattr(YamlAgentFactory, "get_tools_from_config", lambda *_args, **_kwargs: ([], None))

    create_calls = {"count": 0}

    def _fake_create_agent_as_tool(*_args, **_kwargs):
        create_calls["count"] += 1
        return "tool"

    monkeypatch.setattr(YamlAgentFactory, "create_agent_as_tool", _fake_create_agent_as_tool)

    supervisor = _build_supervisor_for_get_tools(
        [{"path": "missing_worker.yaml"}, {"path": "ok_worker.yaml"}]
    )

    with pytest.raises(ValueError, match="worker_agents precheck failed"):
        supervisor._get_tools()

    assert create_calls["count"] == 0


def test_get_tools_loads_all_workers_after_precheck(tmp_path, monkeypatch):
    worker_folder = tmp_path / "applications" / "demo" / "workflows" / "worker_agents"
    shorthand_file = worker_folder / "alpha.yaml"
    relative_file = tmp_path / "applications" / "other" / "workflows" / "worker_agents" / "beta.yaml"
    absolute_file = tmp_path / "gamma.yaml"

    _write_min_worker_yaml(shorthand_file, "alpha")
    _write_min_worker_yaml(relative_file, "beta")
    _write_min_worker_yaml(absolute_file, "gamma")

    monkeypatch.setattr(yaml_factory_module, "AGENT_ROOT", tmp_path)
    monkeypatch.setattr(yaml_factory_module, "get_worker_agent_yaml_path", lambda _category: worker_folder)
    monkeypatch.setattr(YamlAgentFactory, "get_tools_from_config", lambda *_args, **_kwargs: ([], None))

    loaded_paths = []

    def _fake_create_agent_as_tool(config, **_kwargs):
        loaded_paths.append(config.get("_yaml_file_path"))
        return config.get("name")

    monkeypatch.setattr(YamlAgentFactory, "create_agent_as_tool", _fake_create_agent_as_tool)

    supervisor = _build_supervisor_for_get_tools(
        [
            {"path": "alpha.yaml"},
            {"path": "applications/other/workflows/worker_agents/beta.yaml"},
            {"path": str(absolute_file)},
        ]
    )

    tools = supervisor._get_tools()

    assert tools == ["alpha", "beta", "gamma"]
    assert loaded_paths == [
        str(shorthand_file.resolve()),
        str(relative_file.resolve()),
        str(absolute_file.resolve()),
    ]


def test_resolve_bare_name_without_extension_raises(tmp_path):
    """Bare shorthand name without file extension must raise ValueError."""
    worker_folder = tmp_path / "applications" / "demo" / "workflows" / "worker_agents"
    worker_folder.mkdir(parents=True, exist_ok=True)
    # Even if a matching .yaml file exists, bare name is rejected
    (worker_folder / "alpha.yaml").write_text('name: "alpha"\n', encoding="utf-8")

    with pytest.raises(ValueError, match="missing a file extension"):
        AgentConfigNormalizer.resolve_worker_agent_config_path(
            "alpha",
            worker_folder,
            agent_root=tmp_path,
        )


def test_get_tools_registers_worker_as_tool_when_schema_present(tmp_path, monkeypatch):
    """
    Test that supervisor actually registers the worker as a tool if agent_function_schema is present.
    """
    worker_folder = tmp_path / "applications" / "demo" / "workflows" / "worker_agents"
    worker_folder.mkdir(parents=True, exist_ok=True)
    valid_file = worker_folder / "valid.yaml"
    valid_file.write_text("""
name: "valid_worker"
description: "desc"
tools: []
workflow: "wf"
agent_function_schema:
  description: "valid description"
  inputs:
    query:
      description: "tool query"
      required: true
  output:
    description: "worker textual output"
""", encoding="utf-8")

    monkeypatch.setattr(yaml_factory_module, "AGENT_ROOT", tmp_path)
    monkeypatch.setattr(yaml_factory_module, "get_worker_agent_yaml_path", lambda _category: worker_folder)
    monkeypatch.setattr(YamlAgentFactory, "get_tools_from_config", lambda *_args, **_kwargs: ([], None))
    # DO NOT mock create_agent_as_tool here to test the real integration

    supervisor = _build_supervisor_for_get_tools([{"path": "valid.yaml"}])
    tools = supervisor._get_tools()

    # With the new Optional[Callable] return, each worker becomes one tool
    assert len(tools) == 1
    assert tools[0].__name__ == "valid_worker"
    assert "valid description" in (tools[0].__doc__ or "")
    assert "Args:" in (tools[0].__doc__ or "")


def test_get_tools_ignores_worker_as_tool_when_schema_missing(tmp_path, monkeypatch):
    """
    Test that supervisor ignores the worker (does not register as a tool) if agent_function_schema is missing.
    """
    worker_folder = tmp_path / "applications" / "demo" / "workflows" / "worker_agents"
    worker_folder.mkdir(parents=True, exist_ok=True)
    missing_desc_file = worker_folder / "missing.yaml"
    missing_desc_file.write_text("""
name: "missing_worker"
description: "desc"
tools: []
workflow: "wf"
""", encoding="utf-8")

    monkeypatch.setattr(yaml_factory_module, "AGENT_ROOT", tmp_path)
    monkeypatch.setattr(yaml_factory_module, "get_worker_agent_yaml_path", lambda _category: worker_folder)
    monkeypatch.setattr(YamlAgentFactory, "get_tools_from_config", lambda *_args, **_kwargs: ([], None))
    # DO NOT mock create_agent_as_tool here to test the real integration

    supervisor = _build_supervisor_for_get_tools([{"path": "missing.yaml"}])
    tools = supervisor._get_tools()

    assert len(tools) == 0
