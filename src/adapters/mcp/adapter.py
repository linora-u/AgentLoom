"""AgentLoom MCP adapter with protocol-correct error handling."""

from __future__ import annotations

import json
import keyword
import re
from collections.abc import Callable
from copy import deepcopy
from typing import Any

from agentloom.runtime.model_protocol import ToolDefinition
from agentloom.runtime.tool_gateway import ToolBinding
from jsonschema.validators import validator_for
from mcp.types import CallToolResult, TextContent, Tool
from mcpadapt.core import ToolAdapter
from referencing import Registry


def _visible_name(name: str) -> str:
    """Keep legacy MCP tool names without changing the remote call target."""
    name = re.sub(r"[^\w_]", "", name.replace("-", "_"))
    if not name:
        raise ValueError("MCP tool name has no usable identifier characters")
    if name[0].isdigit():
        name = f"_{name}"
    return f"{name}_" if keyword.iskeyword(name) else name


class McpToolExecutionError(RuntimeError):
    """An MCP server returned a terminal tool error result."""

    kind = "mcp_error"
    stage = "tool_execution"

    def __init__(self, message: str, *, retryable: bool = False) -> None:
        super().__init__(message)
        self.retryable = retryable


def _mcp_error_text(result: CallToolResult) -> str:
    text_parts = [item.text for item in result.content if isinstance(item, TextContent) and item.text]
    if text_parts:
        return "\n".join(text_parts)
    if result.structuredContent is not None:
        return str(result.structuredContent)
    return "MCP tool returned an error without details."


def _result_value(result: CallToolResult) -> Any:
    """Preserve structured data and every MCP content block without SDK types."""
    if result.isError:
        raise McpToolExecutionError(_mcp_error_text(result))
    if result.structuredContent is not None:
        return result.structuredContent
    if len(result.content) == 1 and isinstance(result.content[0], TextContent):
        text = result.content[0].text
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            return text
    return [item.model_dump(mode="json", exclude_none=True) for item in result.content]


class AgentLoomMCPAdapter(ToolAdapter):
    """Adapt discovered MCP tools to AgentLoom's runtime-neutral binding.

    The MCP session owns the callable and transport. A binding does not clone
    that connection or acquire a framework-specific Tool implementation.
    """

    def adapt(
        self,
        func: Callable[[dict | None], CallToolResult],
        mcp_tool: Tool,
    ) -> ToolBinding:
        schema = deepcopy(mcp_tool.inputSchema)
        validator_type = validator_for(schema)
        validator_type.check_schema(schema)
        # Only references carried by the discovery schema may be resolved;
        # tool invocation must not fetch arbitrary remote schema resources.
        validator = validator_type(schema, registry=Registry())

        def forward(**arguments: Any) -> Any:
            validator.validate(arguments)
            return _result_value(func(arguments))

        required = schema.get("required", [])
        inputs = {
            name: {**(deepcopy(value) if isinstance(value, dict) else {}), "required": name in required}
            for name, value in schema.get("properties", {}).items()
        }
        return ToolBinding(
            definition=ToolDefinition(
                name=_visible_name(mcp_tool.name),
                description=mcp_tool.description or "",
                parameters=schema,
            ),
            forward=forward,
            inputs_schema=inputs,
            output_type="object",
            input_validator=validator.validate,
        )
