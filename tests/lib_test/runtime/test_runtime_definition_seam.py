"""Stage 1 proof tests for the complete-run Agent runtime seam."""

from __future__ import annotations

import ast
import subprocess
import sys
import textwrap
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from agentloom.runtime.agent_runtime import (
    AgentRuntimeRequest,
    AgentRuntimeResult,
    RuntimeCapabilities,
    RuntimeDefinition,
    RuntimeRegistry,
)
from agentloom.runtime.model_binding import ModelTurnBinding
from agentloom.runtime.model_protocol import (
    MessageItem,
    ModelTurnRequest,
    ModelTurnResult,
    ToolDefinition,
)
from agentloom.runtime.tool_gateway import ToolGateway
from agentloom.runtime.tool_protocol import ToolCallRecord

PROJECT_ROOT = Path(__file__).resolve().parents[3]
RUNTIME_OWNER = PROJECT_ROOT / "src" / "runtime" / "agent.py"

_FORBIDDEN_RUNTIME_IMPORT_PREFIXES = (
    "smolagents",
    "agentloom.adapters.smolagents",
)
_FORBIDDEN_RUNTIME_SYMBOLS = (
    "ToolCallingAgentV2",
    "SmolagentsModelTurnBridge",
)
_FORBIDDEN_RUNTIME_SOURCE_FRAGMENTS = (
    "smolagents",
    "agentloom.adapters.smolagents",
    "ToolCallingAgentV2",
    "SmolagentsModelTurnBridge",
    "adapters.smolagents.models.model_manager",
)


class _StubModelTurnAdapter:
    adapter_id = "openai_chat"

    def turn(self, request: ModelTurnRequest) -> ModelTurnResult:
        return ModelTurnResult(
            items=(MessageItem(role="assistant", text=request.model),)
        )


class _StubToolGateway:
    def __init__(self) -> None:
        self._definitions = (
            ToolDefinition(
                name="proof_tool",
                description="Prove the runtime seam.",
                parameters={
                    "type": "object",
                    "properties": {},
                    "required": [],
                },
            ),
        )

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
            output="proved",
        )

    def close(self) -> None:
        return None


def _complete_runtime_definition(
    *,
    runtime_id: str = "fake",
) -> RuntimeDefinition:
    """Build one definition with every input the fake runtime needs."""

    model = ModelTurnBinding(
        model_type="proof",
        model_id="opaque-model",
        adapter=_StubModelTurnAdapter(),
    )
    gateway = _StubToolGateway()
    assert isinstance(gateway, ToolGateway)
    return RuntimeDefinition(
        runtime_id=runtime_id,
        name="proof-agent",
        description="Prove the complete-run runtime seam.",
        instructions="Use the supplied tool, then finish.",
        model=model,
        tool_gateway=gateway,
        max_steps=7,
    )


def _runtime_imports(tree: ast.AST) -> set[str]:
    imports: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imports.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            module = node.module or ""
            imports.add(module)
            imports.update(
                f"{module}.{alias.name}" if module else alias.name
                for alias in node.names
            )
    return imports


def test_generic_runtime_owner_has_no_smolagents_dependency_or_symbol() -> None:
    source = RUNTIME_OWNER.read_text(encoding="utf-8")
    tree = ast.parse(source, filename=str(RUNTIME_OWNER))
    imports = _runtime_imports(tree)

    forbidden_imports = sorted(
        imported
        for imported in imports
        if imported.startswith(_FORBIDDEN_RUNTIME_IMPORT_PREFIXES)
    )
    referenced_symbols = {
        node.id
        for node in ast.walk(tree)
        if isinstance(node, ast.Name)
        and node.id in _FORBIDDEN_RUNTIME_SYMBOLS
    }
    leaked_fragments = sorted(
        fragment
        for fragment in _FORBIDDEN_RUNTIME_SOURCE_FRAGMENTS
        if fragment in source
    )

    assert forbidden_imports == [], (
        "src/runtime/agent.py is a generic runtime owner and must not import "
        f"the smolagents adapter: {forbidden_imports}"
    )
    assert referenced_symbols == set(), (
        "src/runtime/agent.py must not construct or type against smolagents "
        f"implementation symbols: {sorted(referenced_symbols)}"
    )
    assert leaked_fragments == [], (
        "src/runtime/agent.py still contains smolagents-specific source; move "
        f"construction and model-manager knowledge behind the adapter: {leaked_fragments}"
    )


def test_importing_generic_runtime_owner_does_not_load_smolagents() -> None:
    probe = subprocess.run(
        [
            sys.executable,
            "-c",
            textwrap.dedent(
                """
                import sys
                import agentloom.runtime.agent

                loaded = sorted(
                    name
                    for name in sys.modules
                    if name in {"smolagents", "litellm", "openai"}
                    or name.startswith(("smolagents.", "litellm.", "openai."))
                )
                if loaded:
                    raise AssertionError(
                        "agentloom.runtime.agent imported engine/provider modules: "
                        + ", ".join(loaded)
                    )
                """
            ),
        ],
        cwd=PROJECT_ROOT,
        text=True,
        capture_output=True,
        timeout=60,
    )

    assert probe.returncode == 0, probe.stdout + probe.stderr


def test_runtime_registry_factory_receives_the_complete_definition() -> None:
    definition = _complete_runtime_definition()
    observed: list[RuntimeDefinition] = []

    class _Runtime:
        runtime_id = "fake"
        capabilities = RuntimeCapabilities(True, True, True, True)

        def snapshot(self):
            return None

        def run(self, request: AgentRuntimeRequest) -> AgentRuntimeResult:
            return AgentRuntimeResult(state="success", output=request.task)

        def close(self) -> None:
            return None

    def factory(received_definition: RuntimeDefinition) -> _Runtime:
        observed.append(received_definition)
        return _Runtime()

    registry = RuntimeRegistry()
    registry.register(
        "fake",
        capabilities=_Runtime.capabilities,
        factory=factory,
    )

    runtime = registry.create(definition)

    assert runtime.runtime_id == "fake"
    assert observed == [definition]
    assert observed[0] is definition
    assert observed[0].model.model_id == "opaque-model"
    assert tuple(
        tool.name for tool in observed[0].tool_gateway.definitions
    ) == ("proof_tool",)
    assert observed[0].instructions == "Use the supplied tool, then finish."


def test_fake_runtime_invokes_through_registry_with_the_same_definition() -> None:
    definition = _complete_runtime_definition()
    created: list[RuntimeDefinition] = []
    invocations: list[tuple[RuntimeDefinition, AgentRuntimeRequest]] = []

    class _FakeRuntime:
        runtime_id = "fake"
        capabilities = RuntimeCapabilities(True, True, True, True)

        def __init__(self, received_definition: RuntimeDefinition) -> None:
            self.definition = received_definition

        def snapshot(self):
            return None

        def run(self, request: AgentRuntimeRequest) -> AgentRuntimeResult:
            invocations.append((self.definition, request))
            tool_names = ",".join(
                tool.name for tool in self.definition.tool_gateway.definitions
            )
            return AgentRuntimeResult(
                state="success",
                output=(
                    f"{self.definition.name}:{request.task}:"
                    f"{self.definition.model.model_id}:{tool_names}"
                ),
            )

        def close(self) -> None:
            return None

    def factory(received_definition: RuntimeDefinition) -> _FakeRuntime:
        created.append(received_definition)
        return _FakeRuntime(received_definition)

    registry = RuntimeRegistry()
    registry.register(
        "fake",
        capabilities=_FakeRuntime.capabilities,
        factory=factory,
    )
    request = AgentRuntimeRequest(
        task="perform the proof",
        additional_args={"proof": True},
    )

    result = registry.create(definition).run(request)

    assert created == [definition]
    assert created[0] is definition
    assert invocations == [(definition, request)]
    assert invocations[0][0] is definition
    assert result.state == "success"
    assert result.output == "proof-agent:perform the proof:opaque-model:proof_tool"
