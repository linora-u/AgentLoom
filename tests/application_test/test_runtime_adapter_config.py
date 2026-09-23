from __future__ import annotations

import inspect
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import agentloom.application.agent as agent_module
import agentloom.application.validation as validation_module
import pytest
from agentloom.application.definition import load_agent_definition
from agentloom.application.factory import YamlAgentFactory
from agentloom.application.readiness import validate_runtime_agent_config
from agentloom.configuration.llm_config import LLMConfig
from agentloom.execution.agent_runtime import (
    RuntimeCapabilities,
    RuntimeDefinition,
    RuntimeRegistry,
    RuntimeRequirements,
    UnsupportedRuntimeError,
)
from agentloom.execution.model_binding import ModelTurnBinding
from agentloom.execution.model_protocol import (
    MessageItem,
    ModelTurnRequest,
    ModelTurnResult,
    ToolDefinition,
)
from agentloom.execution.tool_protocol import ToolCallRecord

PROJECT_ROOT = Path(__file__).resolve().parents[2]


class _ModelAdapter:
    adapter_id = "openai_chat"

    def turn(self, request: ModelTurnRequest) -> ModelTurnResult:
        return ModelTurnResult(
            items=(MessageItem(role="assistant", text=request.model),)
        )


class _ClosableToolGateway:
    def __init__(self) -> None:
        self.closed = False

    @property
    def definitions(self) -> tuple[ToolDefinition, ...]:
        return ()

    def invoke(
        self,
        *,
        call_id: str,
        tool_name: str,
        arguments: Mapping[str, Any],
    ) -> ToolCallRecord:
        return ToolCallRecord.completed(
            call_id=call_id,
            tool_name=tool_name,
            input=dict(arguments),
            output="done",
        )

    def close(self) -> None:
        self.closed = True


def _runtime_definition(
    runtime_id: str,
) -> tuple[RuntimeDefinition, _ClosableToolGateway]:
    gateway = _ClosableToolGateway()
    definition = RuntimeDefinition(
        runtime_id=runtime_id,
        name="runtime-owner",
        description="Exercise the owner-to-registry seam.",
        model=ModelTurnBinding(
            model_type="test",
            model_id="opaque-model",
            adapter=_ModelAdapter(),
        ),
        tool_gateway=gateway,
        runtime_options={'max_steps': 5},
    )
    return definition, gateway


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
    definition, _gateway = _runtime_definition("smolagents")
    observed: dict[str, object] = {}

    class _RecordingRegistry:
        def create(self, received_definition: RuntimeDefinition) -> object:
            observed["definition"] = received_definition
            return runtime

    def build_registry() -> _RecordingRegistry:
        observed["registry_built"] = True
        return _RecordingRegistry()

    class _RuntimeOwner:
        _config = {"agent_runtime": "smolagents"}

        def _build_runtime_definition(self) -> RuntimeDefinition:
            return definition

        def _role_profile(self) -> agent_module.AgentRoleProfile:
            return agent_module.AgentRoleProfile(
                agent_type=agent_module.AgentType.SUPERVISOR,
            )

    owner = _RuntimeOwner()
    monkeypatch.setattr(
        agent_module,
        "build_builtin_runtime_registry",
        build_registry,
    )

    selected = agent_module.RoleDrivenAgent.build_runtime(owner)  # type: ignore[arg-type]

    assert selected is runtime
    assert observed == {
        "registry_built": True,
        "definition": definition,
    }
    assert observed["definition"] is definition


def test_role_driven_agent_rejects_langgraph_and_closes_definition_gateway(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []
    definition, gateway = _runtime_definition("langgraph")
    registry = RuntimeRegistry()

    def build_smolagents(
        _definition: RuntimeDefinition,
    ) -> object:
        calls.append("smolagents")
        return object()

    registry.register(
        "smolagents",
        capabilities=RuntimeCapabilities(True, True, True, True),
        factory=build_smolagents,
    )

    class _RuntimeOwner:
        _config = {"agent_runtime": "langgraph"}

        def _build_runtime_definition(self) -> RuntimeDefinition:
            return definition

    monkeypatch.setattr(
        agent_module,
        "build_builtin_runtime_registry",
        lambda: registry,
    )

    with pytest.raises(ValueError, match="agent_runtime.*smolagents"):
        agent_module.RoleDrivenAgent.build_runtime(_RuntimeOwner())  # type: ignore[arg-type]

    assert calls == []
    assert gateway.closed is True


def test_runtime_preflight_uses_registry_capabilities(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    registry = RuntimeRegistry()
    registry.register(
        "minimal",
        capabilities=RuntimeCapabilities(
            structured_tools=True,
            parallel_tools=False,
            checkpoint_resume=False,
            subagents=False,
        ),
    )
    observed: list[RuntimeRequirements] = []
    original_validate = registry.validate

    def validate(runtime_id: object, *, requirements: RuntimeRequirements):
        observed.append(requirements)
        return original_validate(runtime_id, requirements=requirements)

    registry.validate = validate  # type: ignore[method-assign]
    monkeypatch.setattr(
        validation_module,
        "build_builtin_runtime_registry",
        lambda: registry,
    )

    validate_runtime_agent_config(
        _agent_config(agent_runtime="minimal"),
        tmp_path / "agent.yaml",
        agent_root=tmp_path,
    )
    assert observed == [RuntimeRequirements(structured_tools=False)]

    with pytest.raises(
        UnsupportedRuntimeError,
        match="parallel_tools, checkpoint_resume, subagents",
    ):
        validate_runtime_agent_config(
            _agent_config(
                agent_runtime="minimal",
                concurrency=2,
                checkpoint={"enabled": True},
                worker_agents=[{"path": "worker.yaml"}],
            ),
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
    import subprocess

    roots = (
        PROJECT_ROOT / "applications",
        PROJECT_ROOT / "tests/agent_test/llm_cfg_test/fixtures",
    )
    # Validate shipped/current source definitions, not ignored local experiments
    # or a user's private application. Include new, not-yet-committed sources.
    source_paths = None
    if (PROJECT_ROOT / ".git").exists():
        listed = subprocess.check_output([
            "git", "ls-files", "--cached", "--others", "--exclude-standard", "-z", "--",
            *(str(root.relative_to(PROJECT_ROOT)) for root in roots),
        ], cwd=PROJECT_ROOT)
        source_paths = {PROJECT_ROOT / name.decode() for name in listed.split(b"\0") if name}
    definitions: list[tuple[Path, dict[str, object]]] = []
    for root in roots:
        for path in sorted((*root.rglob("*.yaml"), *root.rglob("*.yml"))):
            if source_paths is not None and path not in source_paths:
                continue
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
        # Shipped mixed Applications can select either supported native runtime.
        assert definition.get("agent_runtime") in {"smolagents", "pi"}, path
        assert isinstance(definition.get("workflow"), str), path
        assert definition["workflow"].strip(), path
        assert "agent_function_schema" not in definition, path
        assert "tool_call_type" not in definition, path
        assert "execution_env" not in definition, path
        assert "code_agent" not in definition, path
        if "worker_agents" in path.parts:
            input_schema = definition.get("input_schema")
            if input_schema is not None:
                assert isinstance(input_schema, dict), path
                assert input_schema.get("type") == "object", path
            output_schema = definition.get("output_schema")
            if output_schema is not None:
                assert isinstance(output_schema, dict), path


def test_shipped_llm_example_declares_adapter_for_every_model_type() -> None:
    config = LLMConfig.load_from_yaml(PROJECT_ROOT / "config/llm.example.yaml")

    assert config.models
    assert all(settings.adapter in {
        "openai_chat",
        "openai_responses",
        "anthropic_messages",
    } for settings in config.models.values())


@pytest.mark.parametrize(
    ("relative_path", "payload_tool"),
    [
        (
            "applications/context_engine_text_retrieve_validation/"
            "workflows/worker_agents/text_payload_worker.yaml",
            "make_context_engine_text_payload",
        ),
        (
            "applications/context_engine_json_retrieve_validation/"
            "workflows/worker_agents/json_payload_worker.yaml",
            "make_context_engine_json_payload",
        ),
        (
            "applications/context_engine_multi_worker_validation/"
            "workflows/worker_agents/log_payload_worker.yaml",
            "make_context_engine_log_payload",
        ),
        (
            "applications/context_engine_multi_worker_validation/"
            "workflows/worker_agents/search_payload_worker.yaml",
            "make_context_engine_search_payload",
        ),
    ],
)
def test_context_engine_workers_use_structured_context_ref_handoff(
    relative_path: str,
    payload_tool: str,
) -> None:
    definition = load_agent_definition(PROJECT_ROOT / relative_path)
    workflow = str(definition["workflow"])
    normalized_workflow = " ".join(workflow.split())

    assert payload_tool in workflow
    assert "native structured tool call" in normalized_workflow
    assert "ContextRef" in workflow
    assert "final_answer" in workflow
    assert "one code block" not in workflow
    assert "payload =" not in workflow
    assert "final_answer(payload)" not in workflow


@pytest.mark.parametrize(
    ("relative_path", "expected_tools"),
    [
        (
            "applications/context_engine_text_retrieve_validation/"
            "workflows/context_engine_text_retrieve_validation_agent.yaml",
            {
                "loom_retrieve_context": {
                    "query": "TARGET_RECORD case=text",
                    "offset": 0,
                    "limit": 20,
                }
            },
        ),
        (
            "applications/context_engine_json_retrieve_validation/"
            "workflows/context_engine_json_retrieve_validation_agent.yaml",
            {
                "loom_retrieve_context": {
                    "query": "verification_value",
                    "offset": 0,
                    "limit": 30,
                }
            },
        ),
        (
            "applications/context_engine_multi_worker_validation/"
            "workflows/context_engine_multi_worker_validation_agent.yaml",
            {
                "retrieve_log_context": {
                    "query": "LOG_TARGET_RECORD",
                    "offset": 0,
                    "limit": 20,
                },
                "retrieve_search_context": {
                    "query": "SEARCH_TARGET_RECORD",
                    "offset": 0,
                    "limit": 20,
                },
            },
        ),
    ],
)
def test_context_engine_supervisors_fix_retrieval_parameters(
    relative_path: str,
    expected_tools: dict[str, dict[str, object]],
) -> None:
    definition = load_agent_definition(PROJECT_ROOT / relative_path)
    configured_tools = {
        tool["name"]: tool.get("fixed_args")
        for tool in definition["tools"]
    }

    assert configured_tools == expected_tools


def test_smol_runtime_options_keep_explicit_layers_and_source(tmp_path):
    from agentloom.application.runtime_options import normalize_runtime_options
    from agentloom.configuration.config import ConfigLayerSnapshot, EffectiveAgentConfigSnapshot

    config = _agent_config(agent_runtime="smolagents", runtime_options={"max_steps": 6, "planning_interval": 2})
    config["_yaml_file_path"] = str(tmp_path / "agent.yaml")
    snapshot = EffectiveAgentConfigSnapshot(
        values={"runtime_options": {"smart_summary": False}},
        layers=(ConfigLayerSnapshot("global_system", {"runtime_options": {"smart_summary": False}}, tmp_path, tmp_path / "system.yaml"),),
    )
    options, sources = normalize_runtime_options(config, snapshot=snapshot, agent_root=tmp_path)
    assert options["max_steps"] == 6
    assert options["planning_interval"] == 2
    assert options["smart_summary"] is False
    assert sources["smart_summary"] == f"{tmp_path / 'system.yaml'}:runtime_options.smart_summary"
    assert sources["max_steps"] == f"{tmp_path / 'agent.yaml'}:runtime_options.max_steps"
    assert sources["todo_mode"] == "default:smolagents"


def test_old_smol_options_are_ignored_even_when_conflicting(tmp_path):
    from agentloom.application.runtime_options import normalize_runtime_options

    config = _agent_config(agent_runtime="smolagents", max_steps={"invalid": True},
                          todo=False, prompt=["missing"], smart_summary="invalid",
                          runtime_options={"max_steps": 7})
    options, sources = normalize_runtime_options(config, agent_root=tmp_path)
    assert options["max_steps"] == 7
    assert options["todo_mode"] == "auto"
    assert options["prompt_template_path"] is None
    assert options["smart_summary"] is True
    assert sources["todo_mode"] == "default:smolagents"


def test_legacy_prompt_does_not_affect_canonical_template_path(tmp_path):
    from agentloom.application.runtime_options import normalize_runtime_options

    options, _ = normalize_runtime_options(_agent_config(
        agent_runtime="smolagents", prompt={"path": "missing/ignored.yaml"},
        runtime_options={"prompt_template_path": "prompts/custom.yaml"},
    ), agent_root=tmp_path)
    assert options["prompt_template_path"] == str(tmp_path / "prompts/custom.yaml")
