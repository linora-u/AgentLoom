"""Application acceptance for a provider-owning runtime contract fixture (not Pi)."""
from __future__ import annotations

from pathlib import Path

import pytest
from agentloom.app.runner import execute_app
from agentloom.config.config import bind_config, load_project_config
from agentloom.execution.agent_runtime import (
    AgentRuntimeResult,
    RuntimeCapabilities,
)


def write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    return path


@pytest.fixture
def native_project(tmp_path, monkeypatch):
    write(tmp_path / "config/system.yaml", "checkpoint: {enabled: false}\nself_learning: {enabled: false}\nsmart_summary: false\ntodo: {mode: auto}\ndefault_toolsets: []\n")
    write(tmp_path / "config/llm.yaml", "model:\n  default_model_type: test\n  test: {model: fixture/model, adapter: openai_chat, api_key: fixture-secret}\n  summary: {model: fixture/model, adapter: openai_chat}\n")
    path = write(tmp_path / "applications/native/workflows/root.yaml", "name: native\nagent_runtime: native-fixture\ndescription: Return the requested answer.\nworkflow: Answer directly.\ntools: []\ntoolsets: []\nruntime_options: {thinking: low}\n")
    definitions = []
    class Observations(list):
        barrier = None
        contexts = []
    requests = Observations()

    def forbidden_binding(*args, **kwargs):
        raise AssertionError("native runtime attempted Python provider binding")
    monkeypatch.setattr("agentloom.app.agent.BaseAgent._resolve_model_binding", forbidden_binding)

    class NativeRuntime:
        runtime_id = "native-fixture"
        capabilities = RuntimeCapabilities(False, False, False, False)

        def __init__(self, definition):
            definitions.append(definition)

        def run(self, request):
            from agentloom.runtimes.smolagents.todo import get_current_todo_provider
            assert get_current_todo_provider() is None
            from agentloom.execution.trace import capture_explicit_execution_context
            context = capture_explicit_execution_context()
            requests.contexts.append((context.hook_run, context.local_run_id))
            if requests.barrier is not None:
                requests.barrier.wait(timeout=5)
            assert request.requirements.structured_tools is False
            requests.append(request)
            return AgentRuntimeResult(state="success", output="native answer")

        def snapshot(self):
            return None

        def close(self):
            pass

    from agentloom.app.composition import build_builtin_runtime_registry
    registry = build_builtin_runtime_registry()
    registry.register("native-fixture", capabilities=NativeRuntime.capabilities, factory=NativeRuntime)
    monkeypatch.setattr("agentloom.app.validation.build_builtin_runtime_registry", lambda: registry)
    monkeypatch.setattr("agentloom.app.agent.build_builtin_runtime_registry", lambda: registry)
    with bind_config(load_project_config(tmp_path)):
        yield path, definitions, requests


def test_no_tools_no_goal_application_uses_native_model_selection(native_project):
    path, definitions, requests = native_project
    result = execute_app(path, file_logging=False)
    assert result.output == "native answer"
    assert len(definitions) == 1
    definition = definitions[0]
    assert definition.model is None
    assert definition.model_selection.model_id == "fixture/model"
    assert definition.model_selection.protocol == "openai_chat"
    assert "fixture-secret" not in repr(definition)
    assert definition.runtime_options == {"thinking": "low"}
    assert definition.tool_gateway.definitions == ()
    assert not hasattr(definition, "max_steps")
    assert not hasattr(definition, "smart_summary")
    assert not hasattr(definition, "todo_mode")
    assert "todo_write" not in definition.instructions
    assert "final_answer" not in definition.instructions
    assert requests


def test_binding_free_workers_have_fresh_instances_and_hook_runs(native_project, monkeypatch):
    from io import StringIO

    from agentloom.app.definition import load_agent_definition
    from agentloom.app.factory import YamlConfiguredAgent
    from agentloom.execution.logging import RichLoggerBackend
    from rich.console import Console

    path, definitions, requests = native_project
    config = load_agent_definition(path)
    config["input_schema"] = {
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "The request."},
        },
        "required": ["query"],
        "additionalProperties": False,
    }
    from threading import Barrier
    requests.barrier = Barrier(2)
    agent = YamlConfiguredAgent(config=config, logger=RichLoggerBackend(console=Console(file=StringIO())))
    worker = agent.agent_as_tool()
    from agentloom.execution.tool_gateway import bind_tool
    manifest = bind_tool(worker).manifest_entry
    assert (manifest.owner, manifest.provider, manifest.capability) == ("platform", "agentloom", "worker.invoke")
    results = worker.batch([{"query": "one"}, {"query": "two"}], concurrency=2)
    assert [result.status for result in results] == ["completed", "completed"]
    assert len({definition.instance_id for definition in definitions}) == 2
    assert len({id(item[0]) for item in requests.contexts}) == 2
    assert len({item[1] for item in requests.contexts}) == 2
    assert all(definition.model is None for definition in definitions)


@pytest.mark.parametrize("option", ["max_steps: 4", "todo: {mode: off}", "planning_interval: 3", "smart_summary: true"])
def test_legacy_smol_options_are_ignored_by_native_construction(native_project, option):
    path, definitions, _ = native_project
    path.write_text(path.read_text() + option + "\n")
    assert execute_app(path, file_logging=False).output == "native answer"
    assert definitions[0].runtime_options == {"thinking": "low"}


@pytest.mark.parametrize("selection, capability", [
    ("tools: [{name: memory}]\n", "structured_tools"),
    ("goal: {enabled: true}\n", "goal"),
])
def test_selected_functions_require_capabilities(native_project, selection, capability):
    path, definitions, _ = native_project
    path.write_text(path.read_text().replace("tools: []\n", "") + selection)
    with pytest.raises(ValueError, match=capability):
        execute_app(path, file_logging=False)
    assert definitions == []


@pytest.mark.parametrize("bundle", [False, True])
def test_native_runtime_rejects_configured_stop_without_support(native_project, bundle):
    path, definitions, _ = native_project
    if bundle:
        write(path.parent.parent / "hooks/stop/HOOK.yaml", "name: stop\ndescription: A Stop gate.\nhooks:\n  Stop:\n    - id: gate\n      command: 'true'\n")
        hooks = "hooks:\n  bundles:\n    stop: {path: hooks/stop}\n"
    else:
        hooks = "hooks:\n  Stop:\n    - id: gate\n      command: 'true'\n"
    path.write_text(path.read_text() + hooks)
    with pytest.raises(ValueError, match="stop_hooks"):
        execute_app(path, file_logging=False)
    assert definitions == []


def test_native_application_ignores_historical_global_smol_prompt(native_project):
    path, definitions, _ = native_project
    from agentloom.config.config import get_config
    system = get_config().agent_root / "config/system.yaml"
    system.write_text(system.read_text() + "prompt: missing-historical-smol-template.yaml\n")
    assert execute_app(path, file_logging=False).output == "native answer"
    assert definitions[0].runtime_options == {"thinking": "low"}


def test_native_application_does_not_inherit_smol_basic_tool_defaults(native_project):
    path, definitions, _ = native_project
    from agentloom.config.config import get_config
    system = get_config().agent_root / "config/system.yaml"
    system.write_text(system.read_text().replace(
        "default_toolsets: []", "default_toolsets: [core_file, core_shell, core_search]",
    ))
    path.write_text(path.read_text().replace("toolsets: []\n", ""))
    assert execute_app(path, file_logging=False).output == "native answer"
    assert definitions[0].tool_gateway.definitions == ()


@pytest.mark.parametrize("selection", [
    "toolsets: [core_file]\n", "tools: [{name: read_file}]\n",
])
def test_native_explicit_smol_tool_without_mapping_is_rejected(native_project, selection):
    path, definitions, _ = native_project
    key = selection.split(":", 1)[0]
    path.write_text(path.read_text().replace(f"{key}: []\n", selection))
    with pytest.raises(ValueError, match="no compatible mapping"):
        execute_app(path, file_logging=False)
    assert definitions == []


def test_native_application_tool_defaults_are_explicit(native_project):
    path, definitions, _ = native_project
    path.write_text(path.read_text().replace("toolsets: []\n", ""))
    write(path.parent.parent / "config/system.yaml", "default_toolsets: [core_file]\n")
    with pytest.raises(ValueError, match="no compatible mapping"):
        execute_app(path, file_logging=False)
    assert definitions == []


def test_duplicate_explicit_tool_names_are_rejected_before_construction(native_project):
    path, definitions, _ = native_project
    path.write_text(path.read_text().replace("tools: []", "tools: [{name: memory}, {name: memory}]"))
    with pytest.raises(ValueError, match="Duplicate tool name: memory"):
        execute_app(path, file_logging=False)
    assert definitions == []


def test_native_model_projection_uses_only_model_headers(native_project):
    path, definitions, _ = native_project
    from agentloom.config.config import get_config
    root = get_config().agent_root
    system = root / "config/system.yaml"
    system.write_text(system.read_text() + (
        "model_request_headers:\n  profile: opencode\n"
        "  headers: {X-Global: global, X-Level: global}\n"
    ))
    write(path.parent.parent / "config/system.yaml", (
        "model_request_headers:\n  headers: {X-App: app, X-Level: app}\n"
    ))
    path.write_text(path.read_text() + (
        "model_request_headers:\n  headers: {X-Agent: agent, X-Level: agent}\n"
    ))
    model = root / "config/llm.yaml"
    model.write_text(model.read_text().replace(
        "api_key: fixture-secret", "api_key: fixture-secret, extra_headers: {x-level: model, Authorization: fixture-header-secret}",
    ))
    assert execute_app(path, file_logging=False).output == "native answer"
    selection = definitions[0].model_selection
    assert selection.request_headers == {
        "x-level": "model", "Authorization": "fixture-header-secret",
    }
    assert "fixture-header-secret" not in repr(definitions[0])
    assert "fixture-header-secret" not in repr(selection)
