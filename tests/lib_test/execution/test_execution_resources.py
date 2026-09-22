"""The public cleanup boundary closes real smol resources by Run and instance."""

from agentloom.execution import RuntimeHome, bind_run_context
from agentloom.runtimes.smolagents.tools.shell.process import ShellProcessRegistry


def test_closing_one_instance_preserves_other_shell_session(tmp_path):
    from agentloom.execution.resources import close_instance_resources, close_run_resources

    context = RuntimeHome(tmp_path / ".agentloom").context(
        application_id="resources",
        task_id="task",
        run_id="run",
    )
    with bind_run_context(context):
        registry = ShellProcessRegistry.get_instance()
        try:
            first = registry.get_or_create("worker-a", load_profile=False)
            second = registry.get_or_create("worker-b", load_profile=False)
            assert "first" in first.execute("printf first").output
            assert "second" in second.execute("printf second").output
            close_instance_resources("worker-a")
            assert registry.registered_agent_ids() == ["worker-b"]
            assert "still-alive" in second.execute("printf still-alive").output
            close_instance_resources("worker-a")  # idempotent
            close_run_resources()
            assert registry.registered_agent_ids() == []
        finally:
            registry.release_current_run()


def test_instance_cleanup_kills_only_its_real_background_process(tmp_path):
    import subprocess
    import sys
    from dataclasses import replace

    from agentloom.execution.resources import close_instance_resources, close_run_resources
    from agentloom.execution.trace import bind_explicit_execution_context, capture_explicit_execution_context
    from agentloom.runtimes.smolagents.tools.shell.background_task import BackgroundTaskRegistry

    context = RuntimeHome(tmp_path / ".agentloom").context(
        application_id="resources",
        task_id="task",
        run_id="background",
    )
    children = []
    with bind_run_context(context):
        registry = BackgroundTaskRegistry.get_instance()
        try:
            for instance in ("worker-a", "worker-b"):
                output = tmp_path / instance
                output.touch()
                process = subprocess.Popen(
                    [sys.executable, "-c", "import time; time.sleep(60)"],
                    start_new_session=True,
                )
                children.append(process)
                with bind_explicit_execution_context(
                    replace(
                        capture_explicit_execution_context(),
                        agent_id=instance,
                    )
                ):
                    registry.register(process, "test sleeper", str(output))
            close_instance_resources("worker-a")
            assert children[0].wait(timeout=5) != 0
            assert children[1].poll() is None
            close_run_resources()
            assert children[1].wait(timeout=5) != 0
        finally:
            registry.terminate_current_run()
            for child in children:
                if child.poll() is None:
                    child.kill()
                child.wait(timeout=5)


def test_late_resource_registration_is_closed_within_its_cancelled_owner(tmp_path):
    from agentloom.execution.resources import register_resource, close_instance_resources, close_run_resources
    context = RuntimeHome(tmp_path / '.agentloom').context(application_id='resources', task_id='late', run_id='run')
    closed = []
    with bind_run_context(context):
        close_instance_resources('worker-a')
        register_resource('late-a', lambda: closed.append('a'), instance_id='worker-a')
        register_resource('live-b', lambda: closed.append('b'), instance_id='worker-b')
        assert closed == ['a']
        close_run_resources()
        register_resource('late-b', lambda: closed.append('late-b'), instance_id='worker-b')
        assert closed == ['a', 'b', 'late-b']


def test_cancelled_run_cannot_start_a_new_captured_process(tmp_path):
    import os
    import pytest
    from agentloom.execution.process import run_captured_process
    from agentloom.execution.resources import close_run_resources
    context = RuntimeHome(tmp_path / '.agentloom').context(application_id='resources', task_id='no-spawn', run_id='run')
    marker = tmp_path / 'must-not-start'
    with bind_run_context(context):
        close_run_resources()
        with pytest.raises(InterruptedError):
            run_captured_process(f'touch {marker}', cwd=str(tmp_path), env=dict(os.environ), stdin=b'',
                                 timeout=2, stdout_limit=100, stderr_limit=100)
    assert not marker.exists()
