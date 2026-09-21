"""Execute smol runtime options at the public Application boundary."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml
from agentloom.application.run import RunEvent
from agentloom.application.runner import execute_app
from agentloom.configuration.config import bind_config, load_project_config
from agentloom.runtime.model_binding import ModelTurnBinding
from agentloom.runtime.model_protocol import (
    FunctionCallItem,
    FunctionCallOutputItem,
    ModelTurnRequest,
    ModelTurnResult,
    ModelUsage,
)


class _CompatibilityProvider:
    adapter_id = "openai_chat"

    def __init__(self, *, goal: bool, receipt: Path, shell: bool = False) -> None:
        self.goal = goal
        self.requests: list[ModelTurnRequest] = []
        self.background_tasks = []
        self.run_context = None
        self.calls = [
            ("write_file", {"file_path": str(receipt), "content": "smol-tool-result"}),
            ("todo_write", {"todos": [{"content": "Pending work", "status": "pending"}]}),
            ("final_answer", {"answer": "smol-complete"}),
        ]
        if goal:
            self.calls.extend(
                [
                    ("update_goal", {"status": "complete", "evidence": "The receipt was written and verified."}),
                ]
            )
        if shell:
            self.calls.insert(1, ("shell_tool", {
                "command": "sleep 60", "run_in_background": True, "load_profile": False,
            }))

    def turn(self, request: ModelTurnRequest) -> ModelTurnResult:
        index = len(self.requests)
        self.requests.append(request)
        if self.goal and index == len(self.calls):
            # Native smol asks for a final summary after Goal completion stops
            # further ordinary model/tool work.
            return ModelTurnResult(
                items=(
                    FunctionCallItem(
                        call_id="compat-summary",
                        name="final_answer",
                        arguments_json=json.dumps({"answer": "smol-complete"}),
                    ),
                )
            )
        if index >= len(self.calls):
            raise AssertionError("The application unexpectedly requested another model turn")
        assert request.model == "compatibility-model"
        assert {tool.name for tool in request.tools} >= {"write_file", "todo_write", "final_answer"}
        if index:
            assert any(
                isinstance(item, FunctionCallOutputItem)
                and item.call_id == "compat-0"
                and "Created" in item.output
                and "receipt.txt" in item.output
                and not item.is_error
                for item in request.items
            )
        name, arguments = self.calls[index]
        if name == "final_answer":
            from agentloom.runtime import get_current_run_context
            from agentloom.runtimes.smolagents.tools.shell.background_task import BackgroundTaskRegistry
            self.run_context = get_current_run_context()
            self.background_tasks = BackgroundTaskRegistry.get_instance().list_running()
        return ModelTurnResult(
            items=(
                FunctionCallItem(
                    call_id=f"compat-{index}",
                    name=name,
                    arguments_json=json.dumps(arguments),
                ),
            ),
            usage=ModelUsage(input_tokens=10, output_tokens=5, total_tokens=15),
        )


@pytest.mark.parametrize("goal,shell", [(False, False), (True, False), (False, True)])
def test_smol_runtime_options_execute_tools_todo_and_goal_through_application(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    goal: bool,
    shell: bool,
) -> None:
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    (config_dir / "system.yaml").write_text(
        yaml.safe_dump(
            {
                "runtime": {"root_dir": str(tmp_path / "runtime")},
                "checkpoint": {"enabled": True, "cleanup_on_success": False},
                "logging": {"console_enabled": False, "file_enabled": False},
                "self_learning": {"enabled": False},
                "lsp_servers": {"enabled": False},
                "default_toolsets": [],
                "skills": {"paths": []},
            }
        )
    )
    (config_dir / "llm.yaml").write_text(
        yaml.safe_dump(
            {
                "model": {
                    "default_model_type": "compatibility",
                    "compatibility": {"model": "compatibility-model", "adapter": "openai_chat"},
                    "summary": {"model": "summary-model", "adapter": "openai_chat"},
                },
            }
        )
    )
    app = tmp_path / "applications" / "smol_compatibility"
    workflows = app / "workflows"
    workflows.mkdir(parents=True)
    workflow = workflows / "supervisor.yaml"
    workflow.write_text(
        yaml.safe_dump(
            {
                "name": "smol_compatibility",
                "agent_runtime": "smolagents",
                "description": "Write the receipt, track a pending Todo, and finish.",
                "workflow": "Use the supplied tools to complete the requested task.",
                "model_type": "compatibility",
                "runtime_options": {"max_steps": 8, "smart_summary": False, "todo_mode": "auto"},
                "goal": goal,
                "toolsets": [],
                "tools": [{"name": "write_file"}, *([{"name": "shell_tool"}] if shell else [])],
            }
        )
    )
    provider = _CompatibilityProvider(goal=goal, receipt=tmp_path / "receipt.txt", shell=shell)
    binding = ModelTurnBinding(
        model_type="compatibility",
        model_id="compatibility-model",
        adapter=provider,
        max_tokens=4096,
        context_window=32768,
        max_output_tokens=4096,
        input_token_limit=28672,
        requests_per_minute=60,
    )
    monkeypatch.setattr(
        "agentloom.integrations.litellm.model_binding.resolve_litellm_model_turn_binding",
        lambda *_args, **_kwargs: binding,
    )
    events: list[RunEvent] = []
    with bind_config(load_project_config(tmp_path)):
        result = execute_app(workflow, file_logging=False, event_sink=events.append)

    assert result.output == "smol-complete"
    assert (tmp_path / "receipt.txt").read_text() == "smol-tool-result"
    assert len(provider.requests) == len(provider.calls) + int(goal)
    if shell:
        import os
        from agentloom.runtime import bind_run_context
        from agentloom.runtimes.smolagents.tools.shell.background_task import BackgroundTaskRegistry
        from agentloom.runtimes.smolagents.tools.shell.process import ShellProcessRegistry
        assert len(provider.background_tasks) == 1
        task = provider.background_tasks[0]
        assert task.is_terminal
        with pytest.raises(ProcessLookupError):
            os.kill(task.pid, 0)
        with bind_run_context(provider.run_context):
            assert BackgroundTaskRegistry.get_instance().list_running() == []
            assert ShellProcessRegistry.get_instance().registered_agent_ids() == []
    assert [event.event for event in events] == ["run.started", "run.completed"]
    manifest = json.loads(result.run.manifest_path.read_text())
    assert manifest["status"] == "completed"
    assert manifest["task_events_complete"] is True
    assert manifest["run_id"] == result.run.run_id
