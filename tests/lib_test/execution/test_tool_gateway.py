from __future__ import annotations

import inspect
import subprocess
import sys
import textwrap
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from agentloom.execution.model_protocol import ToolDefinition
from agentloom.execution.tool_gateway import ToolGateway
from agentloom.execution.tool_protocol import ToolCallRecord

ROOT = Path(__file__).resolve().parents[3]


class RecordingToolGateway:
    def __init__(self) -> None:
        self._definitions = (
            ToolDefinition(
                name="echo",
                description="Echo a value.",
                parameters={
                    "type": "object",
                    "properties": {"text": {"type": "string"}},
                    "required": ["text"],
                },
            ),
        )
        self.calls: list[tuple[str, str, dict[str, Any]]] = []
        self.closed = False

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
        copied_arguments = dict(arguments)
        self.calls.append((call_id, tool_name, copied_arguments))
        return ToolCallRecord.completed(
            call_id=call_id,
            tool_name=tool_name,
            input=copied_arguments,
            output=copied_arguments["text"],
        )

    def close(self) -> None:
        self.closed = True


def test_tool_gateway_is_a_structural_runtime_contract() -> None:
    gateway = RecordingToolGateway()

    assert isinstance(gateway, ToolGateway)
    assert isinstance(gateway.definitions, tuple)
    assert gateway.definitions[0].name == "echo"

    record = gateway.invoke(
        call_id="provider-call-42",
        tool_name="echo",
        arguments={"text": "kept"},
    )

    assert record.call_id == "provider-call-42"
    assert record.tool_name == "echo"
    assert record.output == "kept"
    assert gateway.calls == [
        ("provider-call-42", "echo", {"text": "kept"}),
    ]


def test_tool_gateway_invocation_identity_is_keyword_only() -> None:
    parameters = inspect.signature(ToolGateway.invoke).parameters

    assert parameters["call_id"].kind is inspect.Parameter.KEYWORD_ONLY
    assert parameters["tool_name"].kind is inspect.Parameter.KEYWORD_ONLY
    assert parameters["arguments"].kind is inspect.Parameter.KEYWORD_ONLY


def test_tool_gateway_contract_imports_no_agent_engine() -> None:
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            textwrap.dedent(
                """
                import sys
                from agentloom.execution.tool_gateway import ToolGateway
                assert ToolGateway.__module__ == "agentloom.execution.tool_gateway"
                assert "smolagents" not in sys.modules
                assert "litellm" not in sys.modules
                assert "langgraph" not in sys.modules
                """
            ),
        ],
        cwd=ROOT,
        text=True,
        capture_output=True,
        timeout=30,
    )

    assert result.returncode == 0, result.stdout + result.stderr
