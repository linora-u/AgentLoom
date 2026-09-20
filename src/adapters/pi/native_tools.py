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


def write(path: str, content: str):
    raise RuntimeError("Pi native tools require the official Pi execution bridge")


write._agentloom_tool_definition = ToolDefinition(  # type: ignore[attr-defined]
    name="write", description="Write a file with the official Pi SDK.", parameters={
        "type": "object", "additionalProperties": False,
        "properties": {
            "path": {"type": "string", "description": "Path to the file to write (relative or absolute)"},
            "content": {"type": "string", "description": "Content to write to the file"},
        }, "required": ["path", "content"],
    },
)


def edit(path: str, edits: list[dict[str, str]]):
    raise RuntimeError("Pi native tools require the official Pi execution bridge")


edit._agentloom_tool_definition = ToolDefinition(  # type: ignore[attr-defined]
    name="edit", description="Edit a file with the official Pi SDK.", parameters={'type': 'object', 'required': ['path', 'edits'], 'properties': {'path': {'type': 'string', 'description': 'Path to the file to edit (relative or absolute)'}, 'edits': {'type': 'array', 'items': {'type': 'object', 'required': ['oldText', 'newText'], 'properties': {'oldText': {'type': 'string', 'description': 'Exact text for one targeted replacement. It must be unique in the original file and must not overlap with any other edits[].oldText in the same call.'}, 'newText': {'type': 'string', 'description': 'Replacement text for this targeted edit.'}}, 'additionalProperties': False}, 'description': 'One or more targeted replacements. Each edit is matched against the original file, not incrementally. Do not include overlapping or nested edits. If two changes touch the same block or nearby lines, merge them into one edit instead.'}}, 'additionalProperties': False},
)


def bash(command: str, timeout: float | None = None):
    raise RuntimeError("Pi native tools require the official Pi execution bridge")


bash._agentloom_tool_definition = ToolDefinition(  # type: ignore[attr-defined]
    name="bash", description="Run a command with the official Pi SDK.", parameters={'type': 'object', 'required': ['command'], 'properties': {'command': {'type': 'string', 'description': 'Bash command to execute'}, 'timeout': {'type': 'number', 'description': 'Timeout in seconds (optional, no default timeout)'}}, 'additionalProperties': False},
)
