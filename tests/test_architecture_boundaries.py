"""Canonical ownership, state and lazy imports after the package migration."""

from __future__ import annotations

import subprocess
import sys
import textwrap
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def run_fresh(source: str) -> None:
    result = subprocess.run(
        [sys.executable, "-c", textwrap.dedent(source)],
        cwd=ROOT, text=True, capture_output=True, timeout=60,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_canonical_modules_own_configuration_and_context_state() -> None:
    run_fresh("""
        import importlib
        import agentloom
        from agentloom.configuration import config
        from agentloom.runtime import invocation
        assert agentloom.C is importlib.import_module('agentloom.configuration').C
        assert config.__spec__.name == 'agentloom.configuration.config'
        sentinel = object()
        config._ACTIVE_CONFIG = sentinel
        assert agentloom.get_config() is sentinel
        with config.bind_config('bound'):
            assert agentloom.get_config() == 'bound'
        assert agentloom.get_config() is sentinel
        assert not hasattr(invocation, "current_worker_memory")
    """)


def test_legacy_package_and_alias_loader_are_absent() -> None:
    run_fresh("""
        import importlib.util
        import sys
        import agentloom
        for name in ('src', 'src.application', 'src.runtime'):
            try:
                importlib.import_module(name)
            except ImportError as exc:
                assert "Import AgentLoom as 'agentloom'" in str(exc)
            else:
                raise AssertionError(f'legacy import succeeded: {name}')
        assert importlib.util.find_spec('agentloom._compat') is None
        assert not any(type(finder).__name__ == '_LegacyFinder' for finder in sys.meta_path)
        assert not any(name == 'src' or name.startswith('src.') for name in sys.modules)
    """)
    assert not (ROOT / 'agentloom').exists()
    for package in ('application', 'configuration', 'runtime', 'runtimes', 'integrations', 'tools'):
        assert (ROOT / 'src' / package).is_dir()
    for removed in ('adapters', 'encoding', 'ui', 'utils', 'tui_bridge'):
        assert not (ROOT / 'src' / removed).exists()


def test_tool_terminal_records_and_hook_outcomes_do_not_load_the_engine() -> None:
    run_fresh("""
        import sys
        from agentloom.runtime.tool_protocol import ToolCallRecord, Blocked
        from agentloom.runtime.hooks import HookPlan, HookRun
        record = ToolCallRecord.blocked(
            call_id='call', tool_name='write', input={'path': 'forbidden'},
            message='policy', stage='guard',
        )
        assert ToolCallRecord.from_dict(record.to_dict()).status == 'blocked'
        assert HookRun(HookPlan(), local_run_id='local', root_run_id='root').step_number == 0
        assert 'smolagents' not in sys.modules
        assert 'litellm' not in sys.modules
        assert not any(name.startswith('agentloom.runtimes.smolagents.tools.shell') for name in sys.modules)
    """)


def test_definition_inspection_preserves_lazy_engine_patch_installation() -> None:
    run_fresh("""
        import sys
        from agentloom.application.definition import read_agent_definition
        from agentloom.application.validation import AgentConfigNormalizer
        from agentloom.tools import catalog
        assert 'agentloom.runtimes.smolagents.agents' not in sys.modules
        assert 'agentloom.runtimes.smolagents.monkey_patch' not in sys.modules
        assert 'agentloom.runtime.agent' not in sys.modules
        assert not any(name.startswith('agentloom.runtimes.smolagents.tools.shell') for name in sys.modules)

        from agentloom.runtimes.smolagents.agents import ToolCallingAgentV2
        from agentloom.runtimes.smolagents import monkey_patch
        from agentloom.runtime.agent import RoleDrivenAgent
        assert ToolCallingAgentV2.__module__ == 'agentloom.runtimes.smolagents.agents'
        assert RoleDrivenAgent.__module__ == 'agentloom.runtime.agent'
        assert monkey_patch._INSTALLED is True
    """)


def test_generic_runtime_modules_do_not_load_smolagents_adapter() -> None:
    run_fresh("""
        import sys
        import agentloom.runtime.logging.levels
        import agentloom.runtime.logging.logger_manager
        assert 'smolagents' not in sys.modules
        assert not any(
            name == 'agentloom.runtimes.smolagents'
            or name.startswith('agentloom.runtimes.smolagents.')
            for name in sys.modules
        )
    """)


def test_smolagents_specific_implementations_have_no_generic_runtime_aliases() -> None:
    legacy_paths = (
        ROOT / "src/runtime/loom_mixin.py",
        ROOT / "src/runtime/memory/context_compression.py",
        ROOT / "src/runtime/logging/agent_logger.py",
    )
    assert not any(path.exists() for path in legacy_paths)
