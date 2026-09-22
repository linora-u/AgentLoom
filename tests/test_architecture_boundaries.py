"""Canonical ownership, state and lazy imports after the package migration."""

from __future__ import annotations

import ast
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
        from agentloom.application import invocation
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
        for name in ('src', 'src.application', 'src.execution'):
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
    for package in ('application', 'configuration', 'execution', 'runtimes', 'integrations', 'tools'):
        assert (ROOT / 'src' / package).is_dir()
    assert not (ROOT / 'src' / 'runtime').exists()
    for removed in ('adapters', 'encoding', 'ui', 'utils', 'tui_bridge'):
        assert not (ROOT / 'src' / removed).exists()
    for removed in ("agent.py", "factory.py", "invocation.py"):
        assert not (ROOT / "src" / "runtime" / removed).exists()
    assert not (ROOT / "src" / "scaffold.py").exists()
    run_fresh("""
        import importlib.util
        assert importlib.util.find_spec("agentloom.execution.agent") is None
        assert importlib.util.find_spec("agentloom.execution.factory") is None
        assert importlib.util.find_spec("agentloom.execution.invocation") is None
        assert importlib.util.find_spec("agentloom.scaffold") is None
        assert importlib.util.find_spec("agentloom.application.scaffold") is not None
        assert importlib.util.find_spec("agentloom.application.agent") is not None
        assert importlib.util.find_spec("agentloom.application.factory") is not None
        assert importlib.util.find_spec("agentloom.application.invocation") is not None
    """)


def test_runtime_contract_does_not_export_smolagents_capabilities() -> None:
    run_fresh("""
        from agentloom.execution import agent_runtime
        assert not hasattr(agent_runtime, "SMOLAGENTS_CAPABILITIES")
    """)


def test_configuration_does_not_load_runtime_implementations() -> None:
    run_fresh("""
        import sys
        from agentloom.configuration import config, llm_config
        from agentloom.configuration import model_adapters, runtime_options
        assert config.__spec__.name == "agentloom.configuration.config"
        assert llm_config.__spec__.name == "agentloom.configuration.llm_config"
        assert model_adapters.MODEL_ADAPTERS
        assert runtime_options.runtime_config_layers
        assert not any(
            name == "agentloom.execution" or name.startswith("agentloom.execution.")
            for name in sys.modules
        )
    """)

def test_configuration_owns_its_vocabulary_without_reverse_imports() -> None:
    offenders: list[str] = []
    for source_path in (ROOT / "src").rglob("*.py"):
        tree = ast.parse(source_path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imports = [(alias.name, None) for alias in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module is not None:
                imports = [(node.module, alias.name) for alias in node.names]
            else:
                continue
            for module, name in imports:
                if (
                    "configuration" in source_path.relative_to(ROOT / "src").parts
                    and (
                        module == "agentloom.execution"
                        or module.startswith("agentloom.execution.")
                    )
                ):
                    offenders.append(
                        f"{source_path.relative_to(ROOT)}:{node.lineno}:{module}"
                    )
                if (
                    module == "agentloom.execution.model_protocol"
                    and name in {"AdapterKind", "MODEL_ADAPTERS"}
                ):
                    offenders.append(
                        f"{source_path.relative_to(ROOT)}:{node.lineno}:"
                        f"{module}.{name}"
                    )
                if (
                    module == "agentloom.application.runtime_options"
                    and name == "runtime_config_layers"
                ):
                    offenders.append(
                        f"{source_path.relative_to(ROOT)}:{node.lineno}:"
                        f"{module}.{name}"
                    )
    assert offenders == []

def test_model_protocol_does_not_reexport_configuration_vocabulary() -> None:
    run_fresh("""
        from agentloom.application import runtime_options
        from agentloom.execution import model_protocol
        assert not hasattr(runtime_options, "runtime_config_layers")
        assert not hasattr(model_protocol, "AdapterKind")
        assert not hasattr(model_protocol, "MODEL_ADAPTERS")
    """)

def test_tool_gateway_does_not_export_smolagents_final_answer_binding() -> None:
    run_fresh("""
        from agentloom.execution import tool_gateway
        assert not hasattr(tool_gateway, "final_answer_binding")
    """)


def test_application_invocation_does_not_reexport_goal_rendering_helpers() -> None:
    run_fresh("""
        from agentloom.application import invocation
        assert not hasattr(invocation, "goal_continuation_prompt")
        assert not hasattr(invocation, "goal_completion_output")
    """)


def test_schedules_do_not_depend_on_application_implementations() -> None:
    offenders: list[str] = []
    for source_path in (ROOT / "src" / "schedules").rglob("*.py"):
        tree = ast.parse(source_path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module is not None:
                names = [node.module]
            else:
                continue
            for name in names:
                if name == "agentloom.application" or name.startswith(
                    "agentloom.application."
                ):
                    offenders.append(
                        f"{source_path.relative_to(ROOT)}:{node.lineno}:{name}"
                    )
    assert offenders == []


def test_studio_bridge_does_not_assemble_schedule_business_dependencies() -> None:
    source_path = ROOT / "src/application/studio/bridge.py"
    source = source_path.read_text(encoding="utf-8")
    tree = ast.parse(source)
    forbidden_imports: list[str] = []
    forbidden_calls: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module is not None:
            forbidden_imports.extend(
                f"{node.module}.{alias.name}"
                for alias in node.names
                if (
                    node.module == "agentloom.application.definition"
                    and alias.name == "resolve_valid_supervisor_definition"
                )
                or (
                    node.module == "agentloom.schedules.mutations"
                    and alias.name == "ScheduleMutationService"
                )
            )
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "ScheduleMutationService"
        ):
            forbidden_calls.append(
                f"{source_path.relative_to(ROOT)}:{node.lineno}"
            )

    assert forbidden_imports == []
    assert forbidden_calls == []


def test_tool_terminal_records_and_hook_outcomes_do_not_load_the_engine() -> None:
    run_fresh("""
        import sys
        from agentloom.execution.tool_protocol import ToolCallRecord, Blocked
        from agentloom.execution.hooks import HookPlan, HookRun
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
        assert 'agentloom.application.agent' not in sys.modules
        assert not any(name.startswith('agentloom.runtimes.smolagents.tools.shell') for name in sys.modules)

        from agentloom.runtimes.smolagents.agents import ToolCallingAgentV2
        from agentloom.runtimes.smolagents import monkey_patch
        from agentloom.application.agent import RoleDrivenAgent
        assert ToolCallingAgentV2.__module__ == 'agentloom.runtimes.smolagents.agents'
        assert RoleDrivenAgent.__module__ == 'agentloom.application.agent'
        assert monkey_patch._INSTALLED is True
    """)


def test_generic_runtime_modules_do_not_load_smolagents_adapter() -> None:
    run_fresh("""
        import sys
        import agentloom.execution.logging.levels
        import agentloom.execution.logging.logger_manager
        assert 'smolagents' not in sys.modules
        assert not any(
            name == 'agentloom.runtimes.smolagents'
            or name.startswith('agentloom.runtimes.smolagents.')
            for name in sys.modules
        )
    """)


def test_smolagents_specific_implementations_have_no_generic_runtime_aliases() -> None:
    legacy_paths = (
        ROOT / "src/execution/loom_mixin.py",
        ROOT / "src/execution/memory/context_compression.py",
        ROOT / "src/execution/logging/agent_logger.py",
    )
    assert not any(path.exists() for path in legacy_paths)
