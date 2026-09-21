"""A non-smol Application can use platform storage without smol private state."""

import subprocess
import sys
from textwrap import dedent


def test_checkpoint_import_does_not_load_backend_todo_state():
    result = subprocess.run([sys.executable, "-c", dedent('''
        import sys
        from agentloom.runtime.checkpoint.checkpoint_manager import CheckpointManager
        assert not any(name.startswith((
            "agentloom.runtimes.smolagents.todo", "agentloom.runtimes.smolagents.todo",
        )) for name in sys.modules)
    ''')], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


def test_platform_run_logging_does_not_load_smol_tools(tmp_path):
    result = subprocess.run([sys.executable, "-c", dedent('''
        import sys
        from agentloom.runtime import RuntimeHome, bind_run_context
        from agentloom.runtime.logging import bind_logger_backend, NullLoggerBackend
        context = RuntimeHome(sys.argv[1]).context(application_id="native", task_id="task", run_id="run")
        with bind_run_context(context), bind_logger_backend(NullLoggerBackend(), context=context):
            pass
        assert not any(name.startswith((
            "agentloom.runtimes.smolagents.tools.shell", "agentloom.runtimes.smolagents.tools",
        )) for name in sys.modules)
    '''), str(tmp_path / "runtime")], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
