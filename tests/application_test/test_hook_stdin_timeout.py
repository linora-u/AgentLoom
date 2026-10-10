"""An unread, large Hook request cannot defeat the Application's deadline."""
import shlex
import sys

import pytest
import yaml
from agentloom.app.runner import execute_app
from agentloom.config.config import bind_config, load_project_config

from tests.application_test.mixed_runtime_support import finish, model_service, project, tool_messages, write_yaml


@pytest.mark.parametrize("runtime", ["pi", "smolagents"])
def test_hook_deadline_blocks_write_even_when_child_never_reads_large_request(tmp_path, runtime):
    target = tmp_path / "must-not-be-written.txt"
    late = tmp_path / "late-hook-effect.txt"
    hook = tmp_path / "unread.py"
    hook.write_text("import time\nfrom pathlib import Path\ntime.sleep(2)\n"
                    f"Path({str(late)!r}).write_text('late')\nprint('{{}}')\n")
    tool = "write" if runtime == "pi" else "write_file"

    def program(request):
        messages = tool_messages(request)
        if not messages:
            arguments = {"path" if runtime == "pi" else "file_path": str(target), "content": "x" * 16000}
            return [("large-write", tool, arguments)]
        assert "timed out" in messages[-1]["content"]
        return finish(request, "hook-deadline-enforced")

    with model_service(program) as (url, requests):
        workflow = project(tmp_path, url, supervisor=runtime, worker="pi")
        definition = yaml.safe_load(workflow.read_text())
        definition.update(worker_agents=[], tools=[{"name": tool}], hooks={"PreToolUse": [{
            "id": "unread-request", "matcher": tool, "timeout": 0.1,
            "command": f"{shlex.quote(sys.executable)} {shlex.quote(str(hook))}",
        }]})
        write_yaml(workflow, definition)
        with bind_config(load_project_config(tmp_path)):
            result = execute_app(workflow, file_logging=False)
    assert result.output == "hook-deadline-enforced"
    assert len(requests) == 2
    assert not target.exists()
    assert not late.exists()
