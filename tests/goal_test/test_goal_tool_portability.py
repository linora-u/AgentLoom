"""Goal construction uses the public neutral tool contract."""

import subprocess
import sys
import textwrap


def test_goal_tools_construct_without_importing_a_runtime_sdk():
    result = subprocess.run(
        [sys.executable, "-c", textwrap.dedent("""
            import importlib.abc
            import sys

            class NoSmol(importlib.abc.MetaPathFinder):
                def find_spec(self, fullname, path=None, target=None):
                    if fullname == 'smolagents' or fullname.startswith(('smolagents.', 'agentloom.runtimes.smolagents')):
                        raise ImportError(f'Runtime dependency in platform Goal: {fullname}')

            sys.meta_path.insert(0, NoSmol())
            from agentloom.execution.tool_gateway import bind_tool
            from agentloom.tools.goal import get_goal, update_goal

            read = bind_tool(get_goal).definition
            update = bind_tool(update_goal).definition
            assert read.name == 'get_goal'
            assert read.parameters['properties'] == {}
            assert update.name == 'update_goal'
            assert update.parameters['required'] == ['status', 'evidence']
            assert update.parameters['properties']['evidence']['type'] == 'string'
            assert 'evidence' in update.parameters['properties']['evidence']['description'].lower()
        """)],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
