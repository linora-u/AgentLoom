from pathlib import Path

import agentloom.runtime.factory as yaml_factory_module
import pytest
from agentloom.application.validation import (
    AgentConfigNormalizer,
    NormalizedAgentConfig,
)
from agentloom.runtime.factory import YamlConfiguredAgent, YamlConfiguredSupervisorAgent
from agentloom.runtimes.smolagents.options import normalize_runtime_options


@pytest.fixture(autouse=True)
def isolated_project_config(tmp_path):
    from agentloom.configuration.config import LLMConfig, UnifiedConfig, bind_config

    with bind_config(UnifiedConfig({}, agent_root=tmp_path, llm_config=LLMConfig())):
        yield


def _make_worker(config: dict) -> YamlConfiguredAgent:
    worker = object.__new__(YamlConfiguredAgent)
    worker._config = config
    worker._normalized = None
    worker._execution_normalized = None
    return worker


def _make_supervisor(config: dict) -> YamlConfiguredSupervisorAgent:
    supervisor = object.__new__(YamlConfiguredSupervisorAgent)
    supervisor._config = config
    supervisor._normalized = None
    supervisor._execution_normalized = None
    return supervisor


def _worker_config() -> dict:
    return {
        "name": "worker_validation_test",
        "agent_runtime": "smolagents",
        "description": "worker",
        "tools": [],
        "workflow": "wf",
    }


def _supervisor_config() -> dict:
    return {
        "name": "supervisor_validation_test",
        "agent_runtime": "smolagents",
        "description": "supervisor",
        "tools": [],
        "workflow": "wf",
        "worker_agents": [],
    }


def test_build_worker_normalized_config_defaults(tmp_path: Path):
    config = _worker_config()

    normalized = AgentConfigNormalizer.build_worker_normalized_config(
        config,
        agent_root=tmp_path,
        source_name="agent",
    )

    assert isinstance(normalized, NormalizedAgentConfig)
    assert not hasattr(normalized, "prompt_template_path")
    assert normalized.agent_function_schema is None


def test_build_supervisor_normalized_config_defaults(tmp_path: Path):
    config = _supervisor_config()

    normalized = AgentConfigNormalizer.build_supervisor_normalized_config(
        config,
        agent_root=tmp_path,
        source_name="supervisor",
    )

    assert isinstance(normalized, NormalizedAgentConfig)
    assert not hasattr(normalized, "prompt_template_path")
    assert normalized.agent_function_schema is None


def test_canonical_prompt_template_path_resolves_relative_string(tmp_path: Path):
    prompt_file = tmp_path / "prompts" / "agent_prompt.yaml"
    prompt_file.parent.mkdir(parents=True, exist_ok=True)
    prompt_file.write_text("system_prompt: test", encoding="utf-8")
    options, _ = normalize_runtime_options(
        {"runtime_options": {"prompt_template_path": "prompts/agent_prompt.yaml"}},
        agent_root=tmp_path,
    )
    assert options["prompt_template_path"] == str(prompt_file.resolve())


@pytest.mark.parametrize("raw_prompt", [["bad"], {"name": "missing_path"},
                                        {"path": "prompts/agent.yaml"}, "", 12])
def test_canonical_prompt_template_path_rejects_invalid_shape(tmp_path: Path, raw_prompt):
    with pytest.raises(ValueError, match="prompt_template_path"):
        normalize_runtime_options(
            {"runtime_options": {"prompt_template_path": raw_prompt}}, agent_root=tmp_path,
        )


@pytest.mark.parametrize("raw_prompt", ["prompts/agent.yaml", {"path": "prompts/agent.yaml"},
                                        ["bad"], {"name": "missing_path"}, {"path": ""}])
def test_legacy_prompt_is_ignored_without_path_resolution(tmp_path: Path, raw_prompt):
    options, sources = normalize_runtime_options({"prompt": raw_prompt}, agent_root=tmp_path)
    assert options["prompt_template_path"] is None
    assert sources["prompt_template_path"] == "default:smolagents"


@pytest.mark.parametrize("maker,config_builder", [
    (_make_worker, _worker_config),
    (_make_supervisor, _supervisor_config),
])
def test_common_validation_ignores_malformed_old_smol_fields(maker, config_builder):
    config = {**config_builder(), "max_steps": [], "planning_interval": "bad",
              "smart_summary": {}, "todo": "bad", "prompt": {"path": ""},
              "max_consecutive_parse_errors": False}
    assert maker(config)._validate_config() is not None


def test_validate_agent_function_schema_normalizes_and_rejects():
    config = {
        "agent_function_schema": {
            "description": "tool description",
            "inputs": {
                "query": {
                    "description": "query text",
                    "type": "number",
                }
            },
            "output": {
                "description": "final text",
            },
        }
    }
    normalized = AgentConfigNormalizer.validate_agent_function_schema(config)
    assert normalized is not None
    assert normalized["inputs"]["query"]["type"] == "string"

    with pytest.raises(ValueError, match="description must be a non-empty string"):
        AgentConfigNormalizer.validate_agent_function_schema({"agent_function_schema": {"inputs": {"x": {"description": "d"}}, "output": {"description": "o"}}})


def test_validate_config_returns_normalized_object(monkeypatch, tmp_path: Path):
    prompt_file = tmp_path / "prompts" / "worker_prompt.yaml"
    prompt_file.parent.mkdir(parents=True, exist_ok=True)
    prompt_file.write_text("system_prompt: worker", encoding="utf-8")
    monkeypatch.setattr(yaml_factory_module, "C", _config_at(tmp_path))

    worker = object.__new__(YamlConfiguredAgent)
    worker._config = {
        **_worker_config(),
        "runtime_options": {"prompt_template_path": "prompts/worker_prompt.yaml"},
        "agent_function_schema": {
            "description": "desc",
            "inputs": {"query": {"description": "q"}},
            "output": {"description": "o"},
        },
    }
    worker._normalized = None

    normalized = worker._validate_config()
    assert isinstance(normalized, NormalizedAgentConfig)
    assert not hasattr(normalized, "prompt_template_path")
    assert normalized.agent_function_schema is not None

    supervisor = object.__new__(YamlConfiguredSupervisorAgent)
    supervisor._config = {
        **_supervisor_config(),
        "runtime_options": {"prompt_template_path": "prompts/worker_prompt.yaml"},
    }
    supervisor._normalized = None
    normalized_supervisor = supervisor._validate_config()
    assert isinstance(normalized_supervisor, NormalizedAgentConfig)
    assert not hasattr(normalized_supervisor, "prompt_template_path")
    assert normalized_supervisor.agent_function_schema is None


def test_ensure_normalized_autobuilds():
    worker = object.__new__(YamlConfiguredAgent)
    worker._config = _worker_config()
    worker._normalized = None
    normalized = worker._ensure_normalized()
    assert normalized.agent_function_schema is None
    assert worker._normalized is not None

    supervisor = object.__new__(YamlConfiguredSupervisorAgent)
    supervisor._config = _supervisor_config()
    supervisor._normalized = None
    normalized_supervisor = supervisor._ensure_normalized()
    assert normalized_supervisor.agent_function_schema is None
    assert supervisor._normalized is not None


@pytest.mark.parametrize("maker,config_builder", [
    (_make_worker, _worker_config),
    (_make_supervisor, _supervisor_config),
])
def test_common_validate_config_rejects_non_dict_tool_item(maker, config_builder):
    agent = maker(config_builder())
    agent._config["tools"] = ["bad"]

    with pytest.raises(ValueError, match="Tool configuration must be a dictionary"):
        agent._validate_config()


@pytest.mark.parametrize("maker,config_builder", [
    (_make_worker, _worker_config),
    (_make_supervisor, _supervisor_config),
])
def test_common_validate_config_rejects_tool_missing_name(maker, config_builder):
    agent = maker(config_builder())
    agent._config["tools"] = [{"module": "x", "function": "y"}]

    with pytest.raises(ValueError, match="missing required 'name' field"):
        agent._validate_config()


@pytest.mark.parametrize("maker,config_builder", [
    (_make_worker, _worker_config),
    (_make_supervisor, _supervisor_config),
])
def test_common_validate_config_rejects_unpaired_dynamic_tool_fields(maker, config_builder):
    agent = maker(config_builder())
    agent._config["tools"] = [{"name": "dyn_tool", "module": "x"}]

    with pytest.raises(ValueError, match="must include both 'module' and 'function' fields"):
        agent._validate_config()


@pytest.mark.parametrize("maker,config_builder", [
    (_make_worker, _worker_config),
    (_make_supervisor, _supervisor_config),
])
def test_common_validate_config_accepts_string_workflow(maker, config_builder):
    agent = maker(config_builder())
    agent._config["workflow"] = "Run this workflow."

    assert agent._validate_config() is not None


@pytest.mark.parametrize("maker,config_builder", [
    (_make_worker, _worker_config),
    (_make_supervisor, _supervisor_config),
])
def test_common_validate_config_accepts_list_workflow(maker, config_builder):
    agent = maker(config_builder())
    agent._config["workflow"] = [
        "First workflow item.",
        "Second workflow item.",
    ]

    assert agent._validate_config() is not None


@pytest.mark.parametrize(
    "workflow_value",
    [
        "",
        "   ",
        [],
        ["valid", ""],
        ["valid", "   "],
        ["valid", 123],
    ],
)
@pytest.mark.parametrize("maker,config_builder", [
    (_make_worker, _worker_config),
    (_make_supervisor, _supervisor_config),
])
def test_common_validate_config_rejects_invalid_workflow_values(maker, config_builder, workflow_value):
    agent = maker(config_builder())
    agent._config["workflow"] = workflow_value

    with pytest.raises(ValueError, match="workflow field must be a non-empty string or non-empty list"):
        agent._validate_config()


@pytest.mark.parametrize("maker,config_builder", [
    (_make_worker, _worker_config),
    (_make_supervisor, _supervisor_config),
])
def test_common_validate_config_rejects_dict_workflow(maker, config_builder):
    agent = maker(config_builder())
    agent._config["workflow"] = {"bad": True}

    with pytest.raises(ValueError, match="workflow field must be a non-empty string or non-empty list"):
        agent._validate_config()


@pytest.mark.parametrize("maker,config_builder", [
    (_make_worker, _worker_config),
    (_make_supervisor, _supervisor_config),
])
def test_common_validate_config_rejects_invalid_skills_type(maker, config_builder):
    agent = maker(config_builder())
    agent._config["skills"] = 123

    with pytest.raises(ValueError, match="skills must contain only a 'paths' list"):
        agent._validate_config()


@pytest.mark.parametrize("maker,config_builder", [
    (_make_worker, _worker_config),
    (_make_supervisor, _supervisor_config),
])
def test_common_validate_config_rejects_removed_tools_mapping(maker, config_builder):
    agent = maker(config_builder())
    agent._config["tools_mapping"] = {"Claude": {"Read": "read_file"}}

    with pytest.raises(ValueError, match="tools_mapping was removed"):
        agent._validate_config()


def test_supervisor_validate_config_rejects_invalid_worker_agents():
    supervisor = _make_supervisor(_supervisor_config())
    supervisor._config["worker_agents"] = [{"name": "legacy_name"}]

    with pytest.raises(ValueError, match="unsupported field 'name'"):
        supervisor._validate_config()


# ---------------------------------------------------------------------------
# tools field is optional (not required)
# ---------------------------------------------------------------------------


def _worker_config_without_tools() -> dict:
    """Worker config with no ``tools`` key at all."""
    return {
        "name": "worker_no_tools",
        "agent_runtime": "smolagents",
        "description": "worker without tools field",
        "workflow": "wf",
    }


def _supervisor_config_without_tools() -> dict:
    """Supervisor config with no ``tools`` key at all."""
    return {
        "name": "supervisor_no_tools",
        "agent_runtime": "smolagents",
        "description": "supervisor without tools field",
        "workflow": "wf",
        "worker_agents": [],
    }


@pytest.mark.parametrize("maker,config_builder", [
    (_make_worker, _worker_config_without_tools),
    (_make_supervisor, _supervisor_config_without_tools),
])
def test_validate_config_accepts_missing_tools_field(maker, config_builder):
    """Omitting ``tools`` should NOT raise – it is optional, defaults to ``[]``."""
    agent = maker(config_builder())
    normalized = agent._validate_config()
    assert normalized is not None


@pytest.mark.parametrize("config_builder", [
    _worker_config_without_tools,
    _supervisor_config_without_tools,
])
def test_get_tools_from_config_returns_list_when_tools_missing(config_builder):
    """When ``tools`` key is absent, ``get_tools_from_config`` should return a (list, manager) tuple."""
    from agentloom.runtime.factory import YamlAgentFactory

    result = YamlAgentFactory.get_tools_from_config(config_builder())
    assert isinstance(result, tuple)
    tools, mcp_mgr = result
    assert isinstance(tools, list)
    assert mcp_mgr is None


def _config_at(root):
    from agentloom.configuration import C

    class ProjectConfig:
        agent_root = root

        def __getattr__(self, name):
            return getattr(C, name)

    return ProjectConfig()
