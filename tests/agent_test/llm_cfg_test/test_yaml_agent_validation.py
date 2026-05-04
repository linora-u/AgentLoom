from pathlib import Path

import pytest

import src.lib.smolagents.agent.yaml_agent_factory as yaml_factory_module
from src.lib.smolagents.agent.yaml_agent_factory import YamlConfiguredAgent, YamlConfiguredSupervisorAgent
from src.lib.smolagents.agent.agent_validation import (
    AgentConfigNormalizer,
    NormalizedAgentConfig,
    normalize_execution_env,
    normalize_execution_prompt_template_path,
)


class _CaptureLogger:
    def __init__(self):
        self.warning_messages: list[str] = []

    def warning(self, msg, *args, **kwargs):
        self.warning_messages.append(str(msg))


class _DummySkillMetadata:
    def __init__(self, allowed_tools):
        self.allowed_tools = allowed_tools


class _DummySkill:
    def __init__(self, allowed_tools):
        self.metadata = _DummySkillMetadata(allowed_tools)


class _DummySkillsManager:
    def __init__(self, skills):
        self.skills = skills


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
        "description": "worker",
        "tools": [],
        "workflow": "wf",
    }


def _supervisor_config() -> dict:
    return {
        "name": "supervisor_validation_test",
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
    assert not hasattr(normalized, "execution_env")
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
    assert not hasattr(normalized, "execution_env")
    assert not hasattr(normalized, "prompt_template_path")
    assert normalized.agent_function_schema is None


def test_normalize_prompt_template_path_supports_string_and_mapping(tmp_path: Path):
    prompt_file = tmp_path / "prompts" / "agent_prompt.yaml"
    prompt_file.parent.mkdir(parents=True, exist_ok=True)
    prompt_file.write_text("system_prompt: test", encoding="utf-8")

    by_string = normalize_execution_prompt_template_path(
        {"prompt": "prompts/agent_prompt.yaml"},
        "worker.prompt",
        agent_root=tmp_path,
    )
    by_mapping = normalize_execution_prompt_template_path(
        {"prompt": {"path": "prompts/agent_prompt.yaml"}},
        "worker.prompt",
        agent_root=tmp_path,
    )

    assert by_string == str(prompt_file.resolve())
    assert by_mapping == str(prompt_file.resolve())


@pytest.mark.parametrize(
    "raw_prompt",
    [
        ["bad"],
        {"name": "missing_path"},
        {"path": ""},
    ],
)
def test_normalize_prompt_template_path_rejects_invalid_shape(tmp_path: Path, raw_prompt):
    with pytest.raises(ValueError, match="prompt"):
        normalize_execution_prompt_template_path(
            {"prompt": raw_prompt},
            "worker.prompt",
            agent_root=tmp_path,
        )


def test_normalize_execution_env_defaults_and_rejects_invalid():
    assert normalize_execution_env({}, "worker.execution_env") == {"type": "local", "executor_kwargs": {}}

    with pytest.raises(ValueError, match="must be one of"):
        normalize_execution_env({"execution_env": {"type": "host"}}, "worker.execution_env")


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
    monkeypatch.setattr(yaml_factory_module, "AGENT_ROOT", tmp_path)

    worker = object.__new__(YamlConfiguredAgent)
    worker._config = {
        **_worker_config(),
        "prompt": {"path": "prompts/worker_prompt.yaml"},
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
    assert worker._execution_normalized.prompt_template_path == str(prompt_file.resolve())

    supervisor = object.__new__(YamlConfiguredSupervisorAgent)
    supervisor._config = {
        **_supervisor_config(),
        "prompt": "prompts/worker_prompt.yaml",
    }
    supervisor._normalized = None
    normalized_supervisor = supervisor._validate_config()
    assert isinstance(normalized_supervisor, NormalizedAgentConfig)
    assert not hasattr(normalized_supervisor, "prompt_template_path")
    assert normalized_supervisor.agent_function_schema is None
    assert supervisor._execution_normalized.prompt_template_path == str(prompt_file.resolve())


def test_ensure_normalized_autobuilds(monkeypatch, tmp_path: Path):
    prompt_file = tmp_path / "prompts" / "worker_prompt.yaml"
    prompt_file.parent.mkdir(parents=True, exist_ok=True)
    prompt_file.write_text("system_prompt: worker", encoding="utf-8")
    monkeypatch.setattr(yaml_factory_module, "AGENT_ROOT", tmp_path)

    worker = object.__new__(YamlConfiguredAgent)
    worker._config = {
        **_worker_config(),
        "prompt": "prompts/worker_prompt.yaml",
    }
    worker._normalized = None
    worker._execution_normalized = None
    normalized = worker._ensure_normalized()
    assert normalized.agent_function_schema is None
    execution_normalized = worker._ensure_execution_normalized()
    assert execution_normalized.prompt_template_path == str(prompt_file.resolve())
    assert worker._normalized is not None

    supervisor = object.__new__(YamlConfiguredSupervisorAgent)
    supervisor._config = {
        **_supervisor_config(),
        "prompt": "prompts/worker_prompt.yaml",
    }
    supervisor._normalized = None
    supervisor._execution_normalized = None
    normalized_supervisor = supervisor._ensure_normalized()
    assert normalized_supervisor.agent_function_schema is None
    execution_normalized_supervisor = supervisor._ensure_execution_normalized()
    assert execution_normalized_supervisor.prompt_template_path == str(prompt_file.resolve())
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

    with pytest.raises(ValueError, match="skills must be a list, dict, or string path"):
        agent._validate_config()


@pytest.mark.parametrize("maker,config_builder", [
    (_make_worker, _worker_config),
    (_make_supervisor, _supervisor_config),
])
def test_common_validate_config_rejects_invalid_tool_call_type(maker, config_builder):
    agent = maker(config_builder())
    agent._config["tool_call_type"] = "invalid"

    with pytest.raises(ValueError, match="tool_call_type must be 'tool_call' or 'code_act'"):
        agent._validate_config()


def test_worker_role_profile_defaults_tool_call_type_to_tool_call():
    worker = _make_worker(_worker_config())
    assert worker._role_profile().tool_call_type == "tool_call"


def test_supervisor_role_profile_defaults_tool_call_type_to_tool_call():
    supervisor = _make_supervisor(_supervisor_config())
    assert supervisor._role_profile().tool_call_type == "tool_call"


@pytest.mark.parametrize("maker,config_builder", [
    (_make_worker, _worker_config),
    (_make_supervisor, _supervisor_config),
])
def test_role_profile_respects_explicit_tool_call_type_tool_call(maker, config_builder):
    agent = maker(config_builder())
    agent._config["tool_call_type"] = "tool_call"
    assert agent._role_profile().tool_call_type == "tool_call"


def test_supervisor_validate_config_rejects_invalid_worker_agents():
    supervisor = _make_supervisor(_supervisor_config())
    supervisor._config["worker_agents"] = [{"name": "legacy_name"}]

    with pytest.raises(ValueError, match="unsupported field 'name'"):
        supervisor._validate_config()


def test_validate_skill_dependencies_logs_missing_tools_warning():
    config = {
        "name": "worker_validation_test",
        "tools": [{"name": "shell_tool"}],
    }
    skills_manager = _DummySkillsManager(
        {
            "skill_with_missing_tools": _DummySkill(["shell_tool", "missing_tool"]),
        }
    )
    logger = _CaptureLogger()

    AgentConfigNormalizer.validate_skill_dependencies(
        config,
        skills_manager,
        default_tools=["read_file"],
        logger=logger,
    )

    assert len(logger.warning_messages) == 1
    warning_msg = logger.warning_messages[0]
    assert "SKILL CONFIGURATION INTEGRITY CHECK FAILED" in warning_msg
    assert "Missing Tools by Skill:" in warning_msg
    assert "[Skill: skill_with_missing_tools]" in warning_msg
    assert "'missing_tool'" in warning_msg


# ---------------------------------------------------------------------------
# tools field is optional (not required)
# ---------------------------------------------------------------------------


def _worker_config_without_tools() -> dict:
    """Worker config with no ``tools`` key at all."""
    return {
        "name": "worker_no_tools",
        "description": "worker without tools field",
        "workflow": "wf",
    }


def _supervisor_config_without_tools() -> dict:
    """Supervisor config with no ``tools`` key at all."""
    return {
        "name": "supervisor_no_tools",
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
    from src.lib.smolagents.agent.yaml_agent_factory import YamlAgentFactory

    result = YamlAgentFactory.get_tools_from_config(config_builder())
    assert isinstance(result, tuple)
    tools, mcp_mgr = result
    assert isinstance(tools, list)
    assert mcp_mgr is None
