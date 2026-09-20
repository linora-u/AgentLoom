"""Native selection declarations, never a Python implementation of Pi tools.

The bridge checks the installed SDK schema before accepting this declaration.
Unknown input properties are rejected by AgentLoom's native host.
"""
from agentloom.runtime.model_protocol import ToolDefinition


def read(path: str, offset: float | None = None, limit: float | None = None):
    raise RuntimeError("Pi native tools require the official Pi execution bridge")


read._agentloom_tool_definition = ToolDefinition(  # type: ignore[attr-defined]
    name="read", description="Read a file with the official Pi SDK.", parameters={
        "type": "object", "additionalProperties": False,
        "properties": {
            "path": {"type": "string", "description": "Path to the file to read (relative or absolute)"},
            "offset": {"type": "number", "description": "Line number to start reading from (1-indexed)"},
            "limit": {"type": "number", "description": "Maximum number of lines to read"},
        }, "required": ["path"],
    },
)
