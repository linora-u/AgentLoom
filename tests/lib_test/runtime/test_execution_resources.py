"""The public cleanup boundary closes real smol resources by Run and instance."""

from agentloom.runtime import RuntimeHome, bind_run_context
from agentloom.tools.shell.process import ShellProcessRegistry


def test_closing_one_instance_preserves_other_shell_session(tmp_path):
    from agentloom.runtime.resources import close_instance_resources, close_run_resources

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

    from agentloom.runtime.resources import close_instance_resources, close_run_resources
    from agentloom.runtime.trace import bind_explicit_execution_context, capture_explicit_execution_context
    from agentloom.tools.shell.background_task import BackgroundTaskRegistry

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
