from __future__ import annotations

import inspect
from pathlib import Path

import agentloom.runtime.agent as agent_module
import pytest
from agentloom.application.definition import load_agent_definition
from agentloom.application.readiness import validate_runtime_agent_config
from agentloom.configuration.llm_config import LLMConfig
from agentloom.runtime.factory import YamlAgentFactory

PROJECT_ROOT = Path(__file__).resolve().parents[2]


def test_python_agent_api_does_not_expose_removed_execution_environment() -> None:
    callables = (
        agent_module.BaseAgent.__init__,
        agent_module.RoleDrivenAgent.__init__,
        YamlAgentFactory.create_agent_tool,
        YamlAgentFactory.create_agent_as_tool,
        YamlAgentFactory.create_agents_as_tools_from_folder,
    )

    for callable_ in callables:
        assert "execution_env" not in inspect.signature(callable_).parameters


def _agent_config(**overrides: object) -> dict[str, object]:
    return {
        "name": "runtime-contract",
        "description": "Exercise the configured Agent runtime.",
        "workflow": "Complete the task with structured tools.",
        "tools": [],
        **overrides,
    }


def _model_config(adapter: str | None) -> dict[str, object]:
    powerful: dict[str, object] = {"model": "opaque-primary-model"}
    summary: dict[str, object] = {"model": "opaque-summary-model"}
    model: dict[str, object] = {
        "default_model_type": "powerful",
        "powerful": powerful,
        "summary": summary,
    }
    if adapter is not None:
        powerful["adapter"] = adapter
        summary["adapter"] = adapter
    return {"model": model}


def test_agent_runtime_is_required_and_smolagents_is_registered(
    tmp_path: Path,
) -> None:
    validate_runtime_agent_config(
        _agent_config(agent_runtime="smolagents"),
        tmp_path / "agent.yaml",
        agent_root=tmp_path,
    )

    with pytest.raises(ValueError, match="missing required 'agent_runtime'"):
        validate_runtime_agent_config(
            _agent_config(),
            tmp_path / "missing-runtime.yaml",
            agent_root=tmp_path,
        )


@pytest.mark.parametrize("runtime_id", ["langgraph", "unknown", "", 1])
def test_unregistered_agent_runtime_fails_without_fallback(
    tmp_path: Path,
    runtime_id: object,
) -> None:
    with pytest.raises(ValueError, match="agent_runtime.*smolagents"):
        validate_runtime_agent_config(
            _agent_config(agent_runtime=runtime_id),
            tmp_path / "agent.yaml",
            agent_root=tmp_path,
        )


def test_role_driven_agent_builds_selected_runtime_through_registry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = object()
    observed: dict[str, object] = {}

    class _RecordingRegistry:
        def create(self, runtime_id: str) -> object:
            observed["runtime_id"] = runtime_id
            return runtime

    def build_registry(*, smolagents_factory: object) -> _RecordingRegistry:
        observed["smolagents_factory"] = smolagents_factory
        return _RecordingRegistry()

    class _RuntimeOwner:
        _config = {"agent_runtime": "smolagents"}

        def _build_smolagents_runtime(self) -> object:
            raise AssertionError("registry owns runtime construction")

    owner = _RuntimeOwner()
    monkeypatch.setattr(
        agent_module,
        "build_builtin_runtime_registry",
        build_registry,
    )

    selected = agent_module.RoleDrivenAgent.build_runtime(owner)  # type: ignore[arg-type]

    assert selected is runtime
    assert observed == {
        "runtime_id": "smolagents",
        "smolagents_factory": owner._build_smolagents_runtime,
    }


def test_role_driven_agent_rejects_langgraph_before_runtime_construction() -> None:
    calls: list[str] = []

    class _RuntimeOwner:
        _config = {"agent_runtime": "langgraph"}

        def _build_smolagents_runtime(self) -> object:
            calls.append("smolagents")
            return object()

    with pytest.raises(ValueError, match="agent_runtime.*smolagents"):
        agent_module.RoleDrivenAgent.build_runtime(_RuntimeOwner())  # type: ignore[arg-type]

    assert calls == []


@pytest.mark.parametrize(
    "removed_fields",
    [
        {"tool_call_type": "code_act"},
        {"execution_env": ["not", "a", "mapping"]},
        {"code_agent": object()},
        {
            "tool_call_type": "unknown",
            "execution_env": {"type": "unknown"},
            "code_agent": {"anything": True},
        },
    ],
)
def test_removed_execution_mode_fields_have_no_runtime_effect(
    tmp_path: Path,
    removed_fields: dict[str, object],
) -> None:
    validate_runtime_agent_config(
        _agent_config(agent_runtime="smolagents", **removed_fields),
        tmp_path / "agent.yaml",
        agent_root=tmp_path,
    )


@pytest.mark.parametrize(
    "adapter",
    ["openai_chat", "openai_responses", "anthropic_messages"],
)
def test_each_model_type_requires_an_explicit_known_adapter(adapter: str) -> None:
    config = LLMConfig.from_dict(_model_config(adapter))

    assert config.models["powerful"].adapter == adapter
    assert config.models["summary"].adapter == adapter
    assert config.models["powerful"].extra_completion_params is None
    assert config.models["summary"].extra_completion_params is None


def test_model_adapter_is_not_inferred_from_model_name() -> None:
    with pytest.raises(ValueError, match="powerful.*adapter"):
        LLMConfig.from_dict(_model_config(None))


def test_unknown_model_adapter_fails_at_config_load() -> None:
    with pytest.raises(ValueError, match="powerful.*adapter"):
        LLMConfig.from_dict(_model_config("chat"))


def test_live_agent_definitions_use_the_current_runtime_contract() -> None:
    roots = (
        PROJECT_ROOT / "applications",
        PROJECT_ROOT / "tests/agent_test/llm_cfg_test/fixtures",
    )
    definitions: list[tuple[Path, dict[str, object]]] = []
    for root in roots:
        for path in sorted((*root.rglob("*.yaml"), *root.rglob("*.yml"))):
            try:
                parsed = load_agent_definition(path)
            except ValueError:
                continue
            if {"name", "description", "workflow"}.issubset(parsed):
                definitions.append((path, parsed))
    markdown_worker = (
        PROJECT_ROOT
        / "applications/architecture_contract_validation/workflows/worker_agents/change_planner.md"
    )
    definitions.append((markdown_worker, load_agent_definition(markdown_worker)))

    assert definitions
    for path, definition in definitions:
        assert definition.get("agent_runtime") == "smolagents", path
        assert "tool_call_type" not in definition, path
        assert "execution_env" not in definition, path
        assert "code_agent" not in definition, path


def test_shipped_llm_example_declares_adapter_for_every_model_type() -> None:
    config = LLMConfig.load_from_yaml(PROJECT_ROOT / "config/llm.example.yaml")

    assert config.models
    assert all(settings.adapter in {
        "openai_chat",
        "openai_responses",
        "anthropic_messages",
    } for settings in config.models.values())
