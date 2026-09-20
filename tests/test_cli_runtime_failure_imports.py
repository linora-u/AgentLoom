"""Failure reporting must not load another runtime or contact its provider."""
import subprocess
import sys
import textwrap


def test_cold_cli_runtime_failures_never_import_provider_sdks():
    result = subprocess.run(
        [sys.executable, "-I", "-c", textwrap.dedent('''
            import importlib.abc
            import sys

            attempted = []
            class DenyProviderImports(importlib.abc.MetaPathFinder):
                def find_spec(self, fullname, path=None, target=None):
                    if fullname.split(".")[0] in {"litellm", "smolagents"}:
                        attempted.append(fullname)
                        raise AssertionError("unexpected provider import: " + fullname)
            sys.meta_path.insert(0, DenyProviderImports())

            from agentloom.__main__ import main
            from agentloom.application import runner
            from agentloom.runtime.agent_runtime import AgentRuntimeError
            from click.testing import CliRunner

            for category, retryable, expected in (
                ("provider", True, 75), ("provider", False, 1),
                ("interrupted", True, 1), ("internal", True, 1),
            ):
                error = RuntimeError("outer application failure")
                error.__cause__ = AgentRuntimeError("runtime failure", category=category, retryable=retryable)
                def fail(*args, **kwargs):
                    raise error
                runner.execute_app = fail
                reply = CliRunner().invoke(main, ["run", "unused.yaml"])
                assert reply.exit_code == expected, (category, reply.exit_code, reply.exception)
                assert not attempted, attempted
                assert "Traceback" not in reply.output
            assert "litellm" not in sys.modules
            assert "smolagents" not in sys.modules
        ''')],
        capture_output=True, text=True, timeout=20,
    )
    assert result.returncode == 0, result.stdout + result.stderr
