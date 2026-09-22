"""MCP definitions and results cross the neutral tool seam intact."""

import subprocess
import sys
import textwrap


def test_mcp_constructs_and_calls_without_smol_using_full_input_schema():
    result = subprocess.run(
        [sys.executable, "-c", textwrap.dedent("""
            import importlib.abc
            import sys

            class NoSmol(importlib.abc.MetaPathFinder):
                def find_spec(self, fullname, path=None, target=None):
                    if fullname == 'smolagents' or fullname.startswith(('smolagents.', 'agentloom.runtimes.smolagents')):
                        raise ImportError(f'Runtime dependency in MCP: {fullname}')

            sys.meta_path.insert(0, NoSmol())
            from mcp.types import CallToolResult, Tool
            from agentloom.integrations.mcp.adapter import AgentLoomMCPAdapter
            from agentloom.integrations.mcp.config import McpSettings
            from agentloom.integrations.mcp.tool_wrapper import wrap_mcp_tools
            from agentloom.execution.tool_gateway import ToolBinding
            from jsonschema import ValidationError

            schema = {
                'type': 'object',
                'properties': {'query': {'$ref': '#/$defs/query'}},
                '$defs': {'query': {'type': 'string', 'minLength': 3}},
                'required': ['query'], 'additionalProperties': False,
            }
            calls = []
            def remote(arguments):
                calls.append(arguments)
                return CallToolResult(content=[], structuredContent={'answer': 42})

            original = AgentLoomMCPAdapter().adapt(
                remote, Tool(name='lookup', description='Search facts', inputSchema=schema),
            )
            tool = wrap_mcp_tools('facts', [original], McpSettings())[0]
            assert isinstance(tool, ToolBinding)
            assert original.definition.name == 'lookup'
            assert tool.definition.name == 'mcp__facts__lookup'
            assert tool.definition.parameters == schema
            assert tool.manifest_entry.owner == 'external'
            assert tool.manifest_entry.provider == 'mcp:facts'
            assert tool.manifest_entry.parameters == schema
            assert tool.forward(query='age') == {'answer': 42}
            try:
                tool.forward(query='x')
            except ValidationError:
                pass
            else:
                raise AssertionError('Invalid input reached remote tool')
            assert calls == [{'query': 'age'}]
        """)],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
