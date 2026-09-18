from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from agentloom.adapters.smolagents import runtime_factory as factory_module
from agentloom.adapters.smolagents.model_turn_bridge import (
    SmolagentsModelTurnBridge,
)
from agentloom.adapters.smolagents.runtime_adapter import (
    SmolagentsRuntimeAdapter,
)
from agentloom.adapters.smolagents.runtime_factory import (
    SmolagentsRuntimeFactory,
)
from agentloom.adapters.smolagents.tool_proxy import (
    SmolagentsToolGatewayProxy,
)
from agentloom.runtime.agent_runtime import RuntimeDefinition
from agentloom.runtime.logging import RichLoggerBackend
from agentloom.runtime.model_binding import ModelTurnBinding
from agentloom.runtime.model_protocol import (
    MessageItem,
    ModelTurnRequest,
    ModelTurnResult,
    ToolDefinition,
)
from agentloom.runtime.tool_gateway import ToolGateway
from agentloom.runtime.tool_protocol import ToolCallRecord
from rich.console import Console
from smolagents import AgentLogger


class _ModelAdapter:
    adapter_id = "openai_chat"

    def turn(self, request: ModelTurnRequest) -> ModelTurnResult:
        return ModelTurnResult(
            items=(MessageItem(role="assistant", text=request.model),)
        )


class _RecordingGateway:
    def __init__(self) -> None:
        self._definitions = (
            ToolDefinition(
                name="proof_tool",
                description="Prove the factory.",
                parameters={
                    "type": "object",
                    "properties": {
                        "value": {
                            "type": "string",
                            "description": "Proof value.",
                        }
                    },
                    "required": ["value"],
                    "additionalProperties": False,
                },
                strict=True,
            ),
            ToolDefinition(
                name="final_answer",
                description="Finish.",
                parameters={
                    "type": "object",
                    "properties": {
                        "answer": {
                            "type": "string",
                            "description": "Final answer.",
                        }
                    },
                    "required": ["answer"],
                },
            ),
        )
        self.close_calls = 0

    @property
    def definitions(self) -> tuple[ToolDefinition, ...]:
        return self._definitions

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
        self.close_calls += 1


def _definition(
    *,
    gateway: _RecordingGateway | None = None,
    metadata: Mapping[str, Any] | None = None,
    planning_interval: int | None = 3,
    instructions: str = "Use proof_tool, then finish.",
    prompt_template_path: str | None = None,
    project_root: str | None = None,
    max_consecutive_model_errors: int = 9,
) -> RuntimeDefinition:
    resolved_gateway = gateway or _RecordingGateway()
    assert isinstance(resolved_gateway, ToolGateway)
    return RuntimeDefinition(
        runtime_id="smolagents",
        name="proof_agent",
        description="Prove the smolagents runtime factory.",
        instructions=instructions,
        model=ModelTurnBinding(
            model_type="proof",
            model_id="provider/opaque-model",
            adapter=_ModelAdapter(),
            max_tokens=32_000,
            context_window=32_000,
            max_output_tokens=4_000,
            input_token_limit=28_000,
            requests_per_minute=30,
        ),
        tool_gateway=resolved_gateway,
        max_steps=7,
        planning_interval=planning_interval,
        smart_summary=False,
        todo_mode="on",
        prompt_template_path=prompt_template_path,
        project_root=project_root or str(Path.cwd()),
        max_consecutive_model_errors=max_consecutive_model_errors,
        metadata=metadata or {},
    )


def test_factory_builds_native_runtime_from_complete_definition(
    monkeypatch,
) -> None:
    logger = object()
    captured: dict[str, Any] = {}

    class _NativeAgent:
        def __init__(self, **kwargs: Any) -> None:
            captured.update(kwargs)
            self.memory = SimpleNamespace(steps=[])
            self.step_callbacks = SimpleNamespace(
                register=lambda *_args, **_kwargs: None
            )

    monkeypatch.setattr(factory_module, "ToolCallingAgentV2", _NativeAgent)
    monkeypatch.setattr(
        factory_module,
        "get_global_logger",
        lambda **_kwargs: logger,
    )
    definition = _definition()

    runtime = SmolagentsRuntimeFactory()(definition)

    assert isinstance(runtime, SmolagentsRuntimeAdapter)
    assert runtime._model_binding is definition.model
    assert captured["tool_gateway"] is definition.tool_gateway
    assert isinstance(captured["model"], SmolagentsModelTurnBridge)
    assert captured["model"].binding is definition.model
    assert captured["model"].model_id == "provider/opaque-model"
    assert captured["max_steps"] == 7
    assert captured["planning_interval"] == 3
    assert captured["max_tokens"] == 32_000
    assert captured["context_window"] == 32_000
    assert captured["max_output_tokens"] == 4_000
    assert captured["smart_summary"] is False
    assert captured["stream_outputs"] is False
    assert captured["name"] == "proof_agent"
    assert captured["description"] == "Prove the smolagents runtime factory."
    assert "instructions" not in captured
    assert captured["logger"] is logger
    assert captured["prompt_templates"]["system_prompt"].count(
        "Use proof_tool, then finish."
    ) == 1
    assert len(captured["final_answer_checks"]) == 1
    native = runtime._native_runtime
    assert native._agent_loom_todo_mode == "on"
    assert native._max_consecutive_parse_errors == 9


def test_factory_adapts_runtime_neutral_logger_only_at_smolagents_boundary(
    monkeypatch,
) -> None:
    captured: dict[str, Any] = {}

    class _NativeAgent:
        def __init__(self, **kwargs: Any) -> None:
            captured.update(kwargs)
            self.memory = SimpleNamespace(steps=[])
            self.step_callbacks = SimpleNamespace(
                register=lambda *_args, **_kwargs: None
            )

    console = Console()
    neutral_logger = RichLoggerBackend(console=console)
    monkeypatch.setattr(factory_module, "ToolCallingAgentV2", _NativeAgent)
    monkeypatch.setattr(
        factory_module,
        "get_global_logger",
        lambda **_kwargs: neutral_logger,
    )

    SmolagentsRuntimeFactory()(_definition())

    assert isinstance(captured["logger"], AgentLogger)
    assert captured["logger"].console is console
    assert captured["logger"] is not neutral_logger


def test_factory_uses_smolagents_default_prompt_and_exact_proxy_definitions(
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        factory_module,
        "get_global_logger",
        lambda **_kwargs: None,
    )
    definition = _definition(planning_interval=None)

    runtime = SmolagentsRuntimeFactory()(definition)
    native = runtime._native_runtime

    assert native.planning_interval is None
    assert native.prompt_templates["system_prompt"].count(
        definition.instructions
    ) == 1
    assert tuple(native.tools) == ("proof_tool", "final_answer")
    proof_proxy = native.tools["proof_tool"]
    assert isinstance(proof_proxy, SmolagentsToolGatewayProxy)
    assert proof_proxy._agentloom_tool_definition is (
        definition.tool_gateway.definitions[0]
    )
    assert proof_proxy._agentloom_tool_definition.strict is True
    assert proof_proxy.inputs["value"]["type"] == "string"


def test_factory_stop_check_resolves_current_hook_run_each_call(
    monkeypatch,
) -> None:
    observed: list[tuple[str, Any, Any, dict[str, Any]]] = []
    current = {"label": "first"}

    class _HookRun:
        def build_stop_check(self):
            label = current["label"]

            def check(final_answer, memory, **kwargs):
                observed.append((label, final_answer, memory, kwargs))
                return True

            return check

    monkeypatch.setattr(
        factory_module,
        "get_current_hook_run",
        lambda **_kwargs: _HookRun(),
    )
    check = factory_module._run_scoped_stop_check
    memory = object()

    assert check("one", memory, agent="a") is True
    current["label"] = "second"
    assert check("two", memory, agent="b") is True
    assert observed == [
        ("first", "one", memory, {"agent": "a"}),
        ("second", "two", memory, {"agent": "b"}),
    ]


def test_factory_stop_check_preserves_hook_block(monkeypatch) -> None:
    class _HookRun:
        def build_stop_check(self):
            def blocked(_final_answer, _memory, **_kwargs):
                raise AssertionError("blocked by STOP hook")

            return blocked

    monkeypatch.setattr(
        factory_module,
        "get_current_hook_run",
        lambda **_kwargs: _HookRun(),
    )

    with pytest.raises(AssertionError, match="blocked by STOP hook"):
        factory_module._run_scoped_stop_check("answer", object())


def test_runtime_close_releases_native_and_gateway_once(monkeypatch) -> None:
    gateway = _RecordingGateway()

    @dataclass
    class _Callbacks:
        def register(self, *_args: Any, **_kwargs: Any) -> None:
            return None

    class _NativeAgent:
        def __init__(self, **_kwargs: Any) -> None:
            self.memory = SimpleNamespace(steps=[])
            self.step_callbacks = _Callbacks()
            self.close_calls = 0

        def close(self) -> None:
            self.close_calls += 1

    monkeypatch.setattr(factory_module, "ToolCallingAgentV2", _NativeAgent)
    monkeypatch.setattr(
        factory_module,
        "get_global_logger",
        lambda **_kwargs: None,
    )
    runtime = SmolagentsRuntimeFactory()(_definition(gateway=gateway))
    native = runtime._native_runtime

    runtime.close()
    runtime.close()

    assert native.close_calls == 1
    assert gateway.close_calls == 1


def test_runtime_close_releases_gateway_after_native_close_failure() -> None:
    gateway = _RecordingGateway()

    class _Native:
        memory = SimpleNamespace(steps=[])
        step_callbacks = SimpleNamespace(
            register=lambda *_args, **_kwargs: None
        )

        def close(self) -> None:
            raise RuntimeError("native close failed")

    runtime = SmolagentsRuntimeAdapter(
        _Native(),
        model_binding=_definition().model,
        tool_gateway=gateway,
    )

    with pytest.raises(RuntimeError, match="native close failed"):
        runtime.close()

    assert gateway.close_calls == 1


@pytest.mark.parametrize(
    "value",
    [0, True, "5"],
)
def test_definition_rejects_invalid_model_error_limit(value) -> None:
    with pytest.raises(ValueError, match="positive integer"):
        _definition(max_consecutive_model_errors=value)


def test_factory_loads_explicit_prompt_and_appends_instructions_once(
    monkeypatch,
    tmp_path,
) -> None:
    captured: dict[str, Any] = {}
    prompt_path = tmp_path / "custom.yaml"
    prompt_path.write_text(
        "system_prompt: Custom base prompt.\nplanning: {}\n",
        encoding="utf-8",
    )

    class _NativeAgent:
        def __init__(self, **kwargs: Any) -> None:
            captured.update(kwargs)
            self.memory = SimpleNamespace(steps=[])
            self.step_callbacks = SimpleNamespace(
                register=lambda *_args, **_kwargs: None
            )

    monkeypatch.setattr(factory_module, "ToolCallingAgentV2", _NativeAgent)
    monkeypatch.setattr(
        factory_module,
        "get_global_logger",
        lambda **_kwargs: None,
    )
    definition = _definition(
        prompt_template_path=str(prompt_path),
        project_root=str(tmp_path),
        instructions="Runtime-owned instructions.",
    )

    SmolagentsRuntimeFactory()(definition)

    assert "instructions" not in captured
    assert captured["prompt_templates"]["system_prompt"] == (
        "Custom base prompt.\n\nRuntime-owned instructions."
    )


def test_factory_uses_native_instructions_only_when_implicit_prompt_load_fails(
    monkeypatch,
) -> None:
    captured: dict[str, Any] = {}

    class _NativeAgent:
        def __init__(self, **kwargs: Any) -> None:
            captured.update(kwargs)
            self.memory = SimpleNamespace(steps=[])
            self.step_callbacks = SimpleNamespace(
                register=lambda *_args, **_kwargs: None
            )

    monkeypatch.setattr(factory_module, "ToolCallingAgentV2", _NativeAgent)
    monkeypatch.setattr(
        factory_module,
        "load_base_prompt_templates",
        lambda **_kwargs: None,
    )
    monkeypatch.setattr(
        factory_module,
        "get_global_logger",
        lambda **_kwargs: None,
    )
    definition = _definition(instructions="Fallback instructions.")

    SmolagentsRuntimeFactory()(definition)

    assert captured["instructions"] == "Fallback instructions."
    assert "prompt_templates" not in captured


def test_factory_rejects_non_smolagents_definition() -> None:
    definition = _definition()
    wrong = RuntimeDefinition(
        runtime_id="langgraph",
        name=definition.name,
        description=definition.description,
        instructions=definition.instructions,
        model=definition.model,
        tool_gateway=definition.tool_gateway,
        max_steps=definition.max_steps,
    )

    with pytest.raises(ValueError, match="runtime_id='smolagents'"):
        SmolagentsRuntimeFactory()(wrong)
