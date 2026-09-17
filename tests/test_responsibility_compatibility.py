"""Old imports must observe the same runtime state after responsibility moves."""

from __future__ import annotations

import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def run_fresh(source: str) -> None:
    result = subprocess.run(
        [sys.executable, "-c", textwrap.dedent(source)],
        cwd=ROOT,
        text=True,
        capture_output=True,
        timeout=60,
    )
    assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.parametrize("canonical_first", [False, True])
def test_legacy_imports_share_module_identity_and_mutable_globals(canonical_first: bool) -> None:
    run_fresh(f"""
        import importlib
        pairs = [
            ('src.lib.config', 'src.configuration'),
            ('src.lib.config.config', 'src.configuration.config'),
            ('src.lib.smolagents.agent.base_agent', 'src.runtime.agent'),
            ('src.lib.smolagents.agent.invocation', 'src.runtime.invocation'),
            ('src.lib.smolagents.agent.yaml_agent_factory', 'src.runtime.factory'),
            ('src.lib.smolagents.agent.agent_validation', 'src.application.validation'),
            ('src.lib.smolagents.hooks.runtime', 'src.runtime.hooks.runtime'),
            ('src.lib.smolagents.hooks.tool_shim', 'src.adapters.smolagents.tool_shim'),
            ('src.lib.smolagents.tool_protocol', 'src.adapters.smolagents.tool_protocol'),
            ('src.lib.smolagents.models.model_manager', 'src.adapters.smolagents.models.model_manager'),
            ('src.lib.smolagents.skills.catalog', 'src.runtime.skills.catalog'),
            ('src.trace.task_context', 'src.runtime.trace.task_context'),
            ('src.lib.checkpoint.coordinator', 'src.runtime.checkpoint.coordinator'),
            ('src.extensions.self_learning.persistence.ledger', 'src.self_learning.persistence.ledger'),
            ('src.runner', 'src.application.runner'),
        ]
        for old, new in pairs:
            first, second = (new, old) if {canonical_first!r} else (old, new)
            one, two = importlib.import_module(first), importlib.import_module(second)
            assert one is two, (old, new)
            assert one.__name__ == new
            assert one.__spec__.name == new
            assert one.__package__ == (new if hasattr(one, '__path__') else new.rpartition('.')[0])

        # Python's from-list resolution is distinct from import_module. Renamed
        # leaf modules must remain reachable through their historical parents.
        from src.lib.smolagents.agent import base_agent, invocation, yaml_agent_factory
        from src.lib.smolagents.hooks import tool_shim, HookRun
        from src.lib import config
        from src import runner
        from src.runtime import agent
        from src.runtime.hooks import HookRun as CanonicalHookRun
        assert base_agent is agent
        assert runner is importlib.import_module('src.application.runner')
        assert tool_shim is importlib.import_module('src.adapters.smolagents.tool_shim')
        assert HookRun is CanonicalHookRun
        assert config.C is importlib.import_module('src.configuration').C

        legacy_config = importlib.import_module('src.lib.config.config')
        current_config = importlib.import_module('src.configuration.config')
        sentinel = object()
        legacy_config._ACTIVE_CONFIG = sentinel
        assert current_config.get_config() is sentinel
        with current_config.bind_config('bound'):
            assert legacy_config.get_config() == 'bound'
        assert legacy_config.get_config() is sentinel

        token = invocation.current_worker_memory.set(['from legacy'])
        try:
            assert importlib.import_module('src.runtime.invocation').current_worker_memory.get() == ['from legacy']
        finally:
            invocation.current_worker_memory.reset(token)

        sentinel_builder = object()
        base_agent.CodeAgentV2 = sentinel_builder
        assert agent.CodeAgentV2 is sentinel_builder
        assert agent.RoleDrivenAgent.build_runtime_agent.__globals__['CodeAgentV2'] is sentinel_builder
    """)


def test_tool_terminal_records_and_hook_outcomes_do_not_load_the_engine() -> None:
    run_fresh("""
        import sys
        from src.runtime.tool_protocol import ToolCallRecord, Blocked
        from src.runtime.hooks import HookPlan, HookRun
        from src.lib.smolagents.hooks.types import Blocked as LegacyBlocked
        assert LegacyBlocked is Blocked
        record = ToolCallRecord.blocked(
            call_id='call', tool_name='write', input={'path': 'forbidden'},
            message='policy', stage='guard',
        )
        assert ToolCallRecord.from_dict(record.to_dict()).status == 'blocked'
        assert HookRun(HookPlan(), local_run_id='local', root_run_id='root').step_number == 0
        assert 'smolagents' not in sys.modules
        assert 'litellm' not in sys.modules
        assert not any(name.startswith('src.tools.shell') for name in sys.modules)
    """)


def test_definition_inspection_preserves_lazy_engine_patch_installation() -> None:
    run_fresh("""
        import sys
        from src.application.definition import read_agent_definition
        from src.lib.smolagents.agent.agent_validation import AgentConfigNormalizer
        from src.application.validation import AgentConfigNormalizer as CurrentNormalizer
        from src.tools import catalog
        assert AgentConfigNormalizer is CurrentNormalizer
        assert 'src.adapters.smolagents.agents' not in sys.modules
        assert 'src.adapters.smolagents.monkey_patch' not in sys.modules
        assert 'src.runtime.agent' not in sys.modules
        assert not any(name.startswith('src.tools.shell') for name in sys.modules)

        from src.lib.smolagents.agent.base_agent import CodeAgentV2, ToolCallingAgentV2
        from src.adapters.smolagents.agents import CodeAgentV2 as CurrentCodeAgent
        from src.adapters.smolagents import monkey_patch
        assert CodeAgentV2 is CurrentCodeAgent
        assert monkey_patch._INSTALLED is True
        from src.runtime.tool_protocol import ToolCallRecord
        from src.lib.smolagents.tool_protocol import ToolCallRecord as LegacyRecord
        assert LegacyRecord is ToolCallRecord
    """)
