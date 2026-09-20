from __future__ import annotations

import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import agentloom.runtime.agent as base_agent_module
import agentloom.runtime.invocation as invocation_module
import pytest
import yaml
from agentloom.adapters.smolagents.agents import ToolCallingAgentV2
from agentloom.adapters.smolagents.loom_mixin import LoomAgentMixin
from agentloom.adapters.smolagents.tools.tools import tool
from agentloom.runtime import RuntimeHome, bind_run_context
from agentloom.runtime.agent_runtime import (
    AgentRuntimeRequest,
    AgentRuntimeResult,
    RuntimeCapabilities,
    RuntimeCheckpointEnvelope,
    RuntimeEvent,
    RuntimeRequirements,
    RuntimeUsage,
    require_runtime_state,
)
from agentloom.runtime.hooks import HookEvent, HookHandler, HookPlan, HookResult, HookRun
from agentloom.runtime.model_binding import ModelTurnBinding
from agentloom.runtime.model_protocol import ModelTurnRequest, ModelTurnResult
from agentloom.runtime.tool_gateway import AgentLoomToolGateway, final_answer_binding
from agentloom.runtime.trace import (
    bind_explicit_execution_context,
    capture_explicit_execution_context,
    clear_current_hook_run,
    set_current_hook_run,
)
from agentloom.self_learning.persistence.review_engine import ReviewEngine


@pytest.fixture(autouse=True)
def _isolate_self_learning_state(tmp_path, monkeypatch):
    """Runtime lifecycle tests must never append to the developer's ledger."""
    monkeypatch.setenv(
        "AGENTLOOM_RUNTIME_ROOT",
        str(tmp_path / ".agentloom"),
    )
    set_current_hook_run(HookRun(HookPlan(), local_run_id="test-local", root_run_id="test-root"))
    yield
    clear_current_hook_run()


class DummyLoggerBackend:
    def info(self, msg, *args, **kwargs):
        return None

    def warning(self, msg, *args, **kwargs):
        return None

    def debug(self, msg, *args, **kwargs):
        return None

    def error(self, msg, *args, **kwargs):
        return None


class _ModelAdapter:
    adapter_id = "openai_chat"

    def turn(self, _request: ModelTurnRequest) -> ModelTurnResult:
        return ModelTurnResult()


def _model_binding(
    *,
    max_tokens: int = 32_000,
    context_window: int = 32_000,
    max_output_tokens: int = 4_000,
) -> ModelTurnBinding:
    return ModelTurnBinding(
        model_type="test",
        model_id="provider/test-model",
        adapter=_ModelAdapter(),
        max_tokens=max_tokens,
        context_window=context_window,
        max_output_tokens=max_output_tokens,
        input_token_limit=max(context_window - max_output_tokens, 0),
        requests_per_minute=60,
    )


class RecordingAgentRuntime:
    runtime_id = "smolagents"
    capabilities = RuntimeCapabilities(True, True, True, True)

    def __init__(
        self,
        output="ok",
        *,
        state: str = "success",
        exc: BaseException | None = None,
        side_effect=None,
    ):
        self.output = output
        self.state = state
        self.exc = exc
        self.side_effect = side_effect
        self.requests: list[AgentRuntimeRequest] = []
        self.close_calls = 0

    def run(self, request: AgentRuntimeRequest) -> AgentRuntimeResult:
        self.requests.append(request)
        if self.exc is not None:
            raise self.exc
        if self.side_effect is not None:
            result = self.side_effect(request)
        else:
            result = AgentRuntimeResult(
            state=self.state,
            output=self.output,
            checkpoint=RuntimeCheckpointEnvelope(
                runtime_id=self.runtime_id,
                runtime_version="test",
                state_schema_version=1,
                payload={},
            ),
        )
        if request.checkpoint_sink is not None and result.checkpoint is not None:
            request.checkpoint_sink(result.checkpoint)
        return result

    def close(self):
        self.close_calls += 1
        return None

    def snapshot(self):
        return RuntimeCheckpointEnvelope(
            runtime_id=self.runtime_id,
            runtime_version="test",
            state_schema_version=1,
            payload={},
        )


class DummyBaseRuntime:
    def run(self, task: str, *args, **kwargs):
        self.memory.steps.append(self.TaskStep(task=task))
        return task


class DummyLoomRuntime(LoomAgentMixin, DummyBaseRuntime):
    def __init__(self):
        from smolagents.memory import AgentMemory, TaskStep

        self.memory = AgentMemory("")
        self.TaskStep = TaskStep
        self._init_loom_agent(before_run_callbacks=None)


class DummyAgent(base_agent_module.RoleDrivenAgent):
    max_steps = 3

    def __init__(self, *args, **kwargs):
        config = dict(kwargs.pop("config", None) or {})
        config.setdefault("agent_runtime", "smolagents")
        super().__init__(*args, config=config, **kwargs)

    def _role_profile(self) -> base_agent_module.AgentRoleProfile:
        return base_agent_module.AgentRoleProfile(
            agent_type=base_agent_module.AgentType.WORKER,
        )

    def _get_tools(self):
        return []


class DummyGoalAgent(DummyAgent):
    def _role_profile(self) -> base_agent_module.AgentRoleProfile:
        return base_agent_module.AgentRoleProfile(
            agent_type=base_agent_module.AgentType.SUPERVISOR,
        )


def _make_agent(*, logger=None) -> DummyAgent:
    agent = DummyAgent(
        config={"name": "runtime_dummy"},
        model_binding=_model_binding(),
        logger=logger,
    )
    return agent


def _make_review_agent(*, logger=None) -> DummyAgent:
    return DummyAgent(
        config={
            "name": "runtime_dummy_memory_review",
            "self_learning": {
                "enabled": True,
                "review": {
                    "application": {
                        "review_model": "summary",
                        "trigger": {"mode": "after_run"},
                    },
                },
            },
        },
        model_binding=_model_binding(),
        logger=logger,
    )


def test_role_driven_agent_reports_to_application_lifecycle(monkeypatch):
    agent = _make_agent(logger=DummyLoggerBackend())
    runtime = RecordingAgentRuntime("reported-result")
    lifecycle = MagicMock()
    monkeypatch.setattr(agent, "build_runtime", lambda: runtime)
    monkeypatch.setattr(agent, "_inject_memory_snapshot", lambda tasks: tasks)

    result = agent.run(
        "application task",
        task_id="task-1",
        application_lifecycle=lifecycle,
    )

    assert result == "reported-result"
    report = lifecycle.report_agent_invocation.call_args.kwargs
    assert report["coordinator"] is None
    assert report["runtime_result"].state == "success"
    assert report["runtime_result"].output == "reported-result"
    assert report["runtime_result"].checkpoint.runtime_id == "smolagents"
    assert report["result"] == "reported-result"
    assert report["error"] is None
    assert report["goal"] is None
    assert runtime.close_calls == 1


def test_role_driven_agent_delegates_one_run_to_the_invocation_module(monkeypatch):
    agent = _make_agent(logger=DummyLoggerBackend())
    observed = []

    class RecordingInvocation:
        def __init__(self, owner, **arguments):
            observed.append((owner, arguments))

        def run(self):
            return "invocation-result"

    monkeypatch.setattr(invocation_module, "AgentInvocation", RecordingInvocation)

    assert agent.run("delegated task", task_id="task-invocation") == "invocation-result"
    assert len(observed) == 1
    owner, arguments = observed[0]
    assert owner is agent
    assert arguments["task"] == "delegated task"
    assert arguments["task_id"] == "task-invocation"
    assert arguments["owns_root_run"] is True


def test_invocation_uses_runtime_neutral_request(monkeypatch):
    agent = _make_agent(logger=DummyLoggerBackend())
    runtime = RecordingAgentRuntime("runtime-result")
    monkeypatch.setattr(agent, "build_runtime", lambda: runtime)
    monkeypatch.setattr(agent, "_inject_memory_snapshot", lambda tasks: tasks)

    result = agent.run(
        "runtime-neutral task",
        task_id="task-runtime-neutral",
        additional_args={"scope": "contract"},
    )

    assert result == "runtime-result"
    assert len(runtime.requests) == 1
    request = runtime.requests[0]
    assert request.task == "runtime-neutral task"
    assert request.task_id == "task-runtime-neutral"
    assert request.run_id
    assert request.requirements == RuntimeRequirements(
        structured_tools=True,
        checkpoint_resume=True,
    )
    assert callable(request.event_sink)
    assert request.continue_session is False
    assert request.record_task is False
    assert dict(request.additional_args) == {"scope": "contract"}


def test_invocation_populates_runtime_context_identity_and_real_requirements(
    tmp_path,
    monkeypatch,
):
    agent = _make_agent(logger=DummyLoggerBackend())
    agent._effective_agent_config = {
        **agent._config,
        "concurrency": 2,
        "checkpoint": {"enabled": True},
        "worker_agents": [{"path": "worker.yaml"}],
    }
    runtime = RecordingAgentRuntime("runtime-result")
    monkeypatch.setattr(agent, "build_runtime", lambda: runtime)
    monkeypatch.setattr(agent, "_inject_memory_snapshot", lambda tasks: tasks)
    context = RuntimeHome(tmp_path / ".agentloom").context(
        application_id="application",
        task_id="canonical-task",
        run_id="canonical-run",
    )

    with bind_run_context(context):
        result = agent.run(
            "runtime-neutral task",
            task_id="agent-local-task",
            run_id="worker-local-run",
        )

    assert result == "runtime-result"
    assert len(runtime.requests) == 1
    request = runtime.requests[0]
    assert request.application_id == "application"
    assert request.task_id == "canonical-task"
    assert request.run_id == "canonical-run"
    assert request.requirements == RuntimeRequirements(
        structured_tools=True,
        parallel_tools=True,
        checkpoint_resume=True,
        subagents=True,
    )


def test_subtask_runtime_projects_owned_subagent_lifecycle_events() -> None:
    runtime = RecordingAgentRuntime("worker-result")
    worker = base_agent_module.SubTaskTrackedAgent(runtime, "worker-agent")
    observed: list[RuntimeEvent] = []

    result = worker.run(
        AgentRuntimeRequest(
            task="delegate",
            application_id="application",
            task_id="canonical-task",
            run_id="canonical-run",
            event_sink=observed.append,
        )
    )

    assert [event.kind for event in result.events] == ["subagent", "subagent"]
    assert [event.details["phase"] for event in result.events] == [
        "started",
        "completed",
    ]
    assert observed == list(result.events)
    assert {
        (event.application_id, event.task_id, event.run_id)
        for event in result.events
    } == {("application", "canonical-task", "canonical-run")}


def test_standalone_checkpoint_failure_still_deactivates_coordinator(
    tmp_path,
    monkeypatch,
):
    from agentloom.runtime.checkpoint import CheckpointManager
    from agentloom.runtime.checkpoint.coordinator import CheckpointCoordinator

    agent = _make_agent(logger=DummyLoggerBackend())
    runtime = RecordingAgentRuntime("reported-result")
    manager = CheckpointManager("runtime-dummy", checkpoints_root=tmp_path)
    monkeypatch.setattr(agent, "build_runtime", lambda: runtime)
    monkeypatch.setattr(agent, "_inject_memory_snapshot", lambda tasks: tasks)
    monkeypatch.setattr(
        CheckpointCoordinator,
        "save_runtime_checkpoint",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            OSError("checkpoint write failed")
        ),
    )

    with pytest.raises(OSError, match="checkpoint write failed"):
        agent.run(
            "standalone task",
            task_id="task-checkpoint-failure",
            checkpoint_manager=manager,
        )

    assert CheckpointCoordinator.current() is None


def test_standalone_base_exception_is_persisted_as_failure(tmp_path, monkeypatch):
    from agentloom.runtime.checkpoint import CheckpointManager

    agent = _make_agent(logger=DummyLoggerBackend())
    runtime = RecordingAgentRuntime(exc=SystemExit("runtime exited"))
    manager = CheckpointManager("runtime-dummy", checkpoints_root=tmp_path)
    monkeypatch.setattr(agent, "build_runtime", lambda: runtime)
    monkeypatch.setattr(agent, "_inject_memory_snapshot", lambda tasks: tasks)

    with pytest.raises(SystemExit, match="runtime exited"):
        agent.run(
            "standalone task",
            task_id="task-system-exit",
            checkpoint_manager=manager,
        )

    tree = manager.load_task_tree("task-system-exit")
    assert tree is not None
    assert tree["status"] == "failed"
    assert tree["error"] == "runtime exited"


def test_manual_review_policy_never_enters_run_end_reviewer(monkeypatch):
    from agentloom.self_learning import reviewer

    agent = DummyAgent(
        config={
            "name": "runtime_dummy_manual_review",
            "self_learning": {
                "enabled": True,
                "review": {
                    "application": {
                        "review_model": "summary",
                        "trigger": {"mode": "manual"},
                    },
                },
            },
        },
        model_binding=_model_binding(),
        logger=DummyLoggerBackend(),
    )
    agent._effective_agent_config = {
        "self_learning": {
            "enabled": True,
            "review": {
                "enabled": True,
                "application": {
                    "review_model": "summary",
                    "trigger": {"mode": "manual"},
                },
                "project": {
                    "review_model": "summary",
                    "trigger": {"mode": "manual"},
                },
            },
        }
    }
    runtime_agent = RecordingAgentRuntime(output="main-result")
    calls = []
    monkeypatch.setattr(agent, "build_runtime", lambda: runtime_agent)
    monkeypatch.setattr(agent, "_inject_memory_snapshot", lambda tasks: tasks)
    monkeypatch.setattr(agent, "_emit_task_lifecycle_event", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(agent, "_emit_session_lifecycle_event", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        reviewer,
        "review_finished_run",
        lambda **kwargs: calls.append(kwargs),
    )

    assert agent.run("top-level") == "main-result"
    assert calls == []


def _append_hook_handler(agent, event, callback, *, source="test"):
    agent._hook_plan = HookPlan((*agent._hook_plan.handlers, HookHandler(event, "*", callback, source=source)))


def test_runtime_definition_contains_complete_neutral_runtime_input(
    monkeypatch,
    tmp_path,
):
    model_binding = _model_binding(
        max_tokens=64_000,
        context_window=64_000,
        max_output_tokens=8_000,
    )
    prompt_path = tmp_path / "prompts" / "custom.yaml"
    agent = DummyAgent(
        config={
            "name": "definition_agent",
            "description": "Complete neutral definition.",
            "workflow": "Use the proof tool.",
            "runtime_options": {
                "prompt_template_path": "prompts/custom.yaml", "planning_interval": 3,
                "smart_summary": False, "todo_mode": "on", "max_steps": 11,
                "max_consecutive_model_errors": 7,
            },
        },
        model_binding=model_binding,
        logger=DummyLoggerBackend(),
    )
    monkeypatch.setattr(
        base_agent_module,
        "C",
        SimpleNamespace(agent_root=tmp_path),
    )
    agent._effective_agent_config = {
        **agent._effective_agent_config,
        "runtime_options": {"prompt_template_path": "prompts/custom.yaml", "planning_interval": 3},
    }

    @tool
    def proof(value: str) -> str:
        """Return a proof value.

        Args:
            value: Value to return.
        """

        return value

    monkeypatch.setattr(agent, "get_all_tools", lambda agent_type: [proof, proof])
    monkeypatch.setattr(
        base_agent_module,
        "get_agent_environment_prompt",
        lambda: "environment",
    )

    definition = agent._build_runtime_definition()

    assert definition.runtime_id == "smolagents"
    assert definition.name == "definition_agent"
    assert definition.description == "Complete neutral definition."
    assert definition.model is model_binding
    assert definition.runtime_options["max_steps"] == 11
    assert definition.runtime_options["planning_interval"] == 3
    assert definition.runtime_options["smart_summary"] is False
    assert definition.runtime_options["todo_mode"] == "on"
    assert definition.instructions == "environment"
    assert definition.runtime_options["prompt_template_path"] == str(prompt_path.resolve())
    assert definition.project_root == str(tmp_path)
    assert definition.runtime_options["max_consecutive_model_errors"] == 7
    assert definition.metadata == {}
    assert tuple(item.name for item in definition.tool_gateway.definitions) == (
        "proof",
    )


def test_each_invocation_builds_and_closes_a_fresh_runtime(monkeypatch):
    agent = _make_agent(logger=DummyLoggerBackend())
    runtimes: list[RecordingAgentRuntime] = []

    def build_runtime() -> RecordingAgentRuntime:
        runtime = RecordingAgentRuntime(output=f"run-{len(runtimes) + 1}")
        runtimes.append(runtime)
        return runtime

    monkeypatch.setattr(agent, "build_runtime", build_runtime)
    monkeypatch.setattr(agent, "_inject_memory_snapshot", lambda tasks: tasks)

    assert agent.run("first") == "run-1"
    assert agent.run("second") == "run-2"
    assert len(runtimes) == 2
    assert runtimes[0] is not runtimes[1]
    assert [runtime.close_calls for runtime in runtimes] == [1, 1]


def test_task_created_is_emitted_once_through_runtime_request(monkeypatch):
    agent = _make_agent(logger=DummyLoggerBackend())
    runtime = RecordingAgentRuntime("done")
    events: list[HookEvent] = []
    monkeypatch.setattr(agent, "build_runtime", lambda: runtime)
    monkeypatch.setattr(agent, "_inject_memory_snapshot", lambda tasks: tasks)

    def record(context):
        events.append(HookEvent(context.hook_event_name))
        return HookResult()

    _append_hook_handler(agent, HookEvent.TASK_CREATED, record)

    assert agent.run("one task", task_id="task-created") == "done"
    assert events == [HookEvent.TASK_CREATED]
    assert [request.task for request in runtime.requests] == ["one task"]


@pytest.mark.parametrize("mode", ["auto", "on", "off"])
def test_common_runtime_does_not_inject_smol_tools(monkeypatch, mode):
    agent = DummyAgent(
        config={"name": "runtime_dummy", "runtime_options": {"todo_mode": mode}},
        model_binding=_model_binding(), logger=DummyLoggerBackend(),
    )
    monkeypatch.setattr(agent, "get_all_tools", lambda agent_type: [])
    assert agent._build_tool_gateway().definitions == ()


def test_base_run_emits_task_complete_on_success(monkeypatch):
    agent = _make_agent(logger=DummyLoggerBackend())
    runtime_agent = RecordingAgentRuntime(output="ok")
    events = []

    monkeypatch.setattr(agent, "build_runtime", lambda: runtime_agent)

    def _record(context):
        events.append(
            (HookEvent(context.hook_event_name), context.tool_name, dict(context.tool_input), context.tool_response)
        )
        return HookResult()

    _append_hook_handler(agent, HookEvent.TASK_COMPLETED, _record)

    result = agent.run("do work", task_id="task-complete")

    assert result == "ok"
    assert any(event is HookEvent.TASK_COMPLETED for event, *_ in events)
    complete_event = next(item for item in events if item[0] is HookEvent.TASK_COMPLETED)
    assert complete_event[2]["task_id"] == "task-complete"
    assert complete_event[2]["agent_name"] == agent.name
    assert len(runtime_agent.requests) == 1
    request = runtime_agent.requests[0]
    assert request.task == "do work"
    assert request.task_id == "task-complete"
    assert request.run_id
    assert request.requirements == RuntimeRequirements(
        structured_tools=True,
        checkpoint_resume=True,
    )
    assert callable(request.event_sink)
    assert request.continue_session is False
    assert request.record_task is False


def test_base_run_binds_root_before_memory_snapshot_and_only_owner_emits_session(monkeypatch):
    from agentloom.runtime.trace import bind_root_run, get_current_session_run_id, require_root_run_id

    agent = _make_agent(logger=DummyLoggerBackend())
    runtimes: list[RecordingAgentRuntime] = []
    events = []
    snapshot_roots = []

    def build_runtime() -> RecordingAgentRuntime:
        runtime = RecordingAgentRuntime(output="ok")
        runtimes.append(runtime)
        return runtime

    monkeypatch.setattr(agent, "build_runtime", build_runtime)
    monkeypatch.setattr(
        agent,
        "_inject_memory_snapshot",
        lambda tasks: snapshot_roots.append(require_root_run_id()) or tasks,
    )

    def _record(context):
        events.append(HookEvent(context.hook_event_name))
        return HookResult()

    _append_hook_handler(agent, HookEvent.SESSION_START, _record)
    _append_hook_handler(agent, HookEvent.SESSION_END, _record)

    assert agent.run("top-level") == "ok"
    first_root = snapshot_roots[0]
    assert first_root
    assert events.count(HookEvent.SESSION_START) == 1
    assert events.count(HookEvent.SESSION_END) == 1
    assert get_current_session_run_id() is None

    events.clear()
    snapshot_roots.clear()
    assert agent.run("same-instance-second-run") == "ok"
    assert snapshot_roots[0] != first_root
    assert events.count(HookEvent.SESSION_START) == 1
    assert events.count(HookEvent.SESSION_END) == 1

    events.clear()
    snapshot_roots.clear()
    with bind_root_run("supervisor-root"):
        assert agent.run("nested-worker") == "ok"
        assert get_current_session_run_id() == "supervisor-root"

    assert snapshot_roots == ["supervisor-root"]
    assert HookEvent.SESSION_START not in events
    assert HookEvent.SESSION_END not in events


def test_main_agent_and_worker_inject_the_same_frozen_root_memory_snapshot() -> None:
    from agentloom.runtime.trace import bind_root_run
    from agentloom.self_learning.persistence.memory_store import MemoryStore

    config = {
        "application_id": "runtime_snapshot_app",
        "self_learning": {"enabled": True},
    }
    main_agent = _make_agent(logger=DummyLoggerBackend())
    worker_agent = _make_agent(logger=DummyLoggerBackend())
    main_agent._effective_agent_config = config
    worker_agent._effective_agent_config = config
    store = MemoryStore(agent_config=config)
    original = store.add(
        "project",
        "The root task sees the original memory.",
        memory_key="runtime:frozen-memory",
    )

    with bind_root_run("root-main-worker-a"):
        main_task = main_agent._inject_memory_snapshot(["main task"])[0]
        store.replace(
            "project",
            str(original["id"]),
            "A mid-run review activated replacement memory.",
        )
        worker_task = worker_agent._inject_memory_snapshot(["worker task"])[0]

    with bind_root_run("root-main-worker-b"):
        next_task = main_agent._inject_memory_snapshot(["next task"])[0]

    assert "original memory" in main_task
    assert "original memory" in worker_task
    assert "replacement memory" not in worker_task
    assert "replacement memory" in next_task
    assert "original memory" not in next_task


def test_failed_initial_memory_store_open_freezes_empty_for_workers(
    monkeypatch,
) -> None:
    from agentloom.runtime.trace import bind_root_run
    from agentloom.self_learning.persistence import (
        memory_store as memory_store_module,
    )
    from agentloom.self_learning.persistence.memory_store import MemoryStore

    config = {
        "application_id": "runtime_snapshot_failure_app",
        "self_learning": {"enabled": True},
    }
    main_agent = _make_agent(logger=DummyLoggerBackend())
    worker_agent = _make_agent(logger=DummyLoggerBackend())
    main_agent._effective_agent_config = config
    worker_agent._effective_agent_config = config

    class _FailingMemoryStore:
        def __init__(self):
            raise OSError("memory database unavailable")

    with bind_root_run("root-failed-memory-open"):
        monkeypatch.setattr(memory_store_module, "MemoryStore", _FailingMemoryStore)
        main_task = main_agent._inject_memory_snapshot(["main task"])[0]

        monkeypatch.setattr(memory_store_module, "MemoryStore", MemoryStore)
        MemoryStore(agent_config=config).add(
            "project",
            "This memory appeared after the failed root-start read.",
        )
        worker_task = worker_agent._inject_memory_snapshot(["worker task"])[0]

    assert main_task == "main task"
    assert worker_task == "worker task"


def test_base_run_uses_runner_supplied_run_id_for_root_lifecycle(monkeypatch):
    from agentloom.runtime.trace import capture_explicit_execution_context

    agent = _make_agent(logger=DummyLoggerBackend())
    runtime_agent = RecordingAgentRuntime(output="ok")
    observed = []

    monkeypatch.setattr(agent, "build_runtime", lambda: runtime_agent)
    monkeypatch.setattr(
        agent,
        "_inject_memory_snapshot",
        lambda tasks: observed.append(capture_explicit_execution_context()) or tasks,
    )

    assert agent.run("top-level", task_id="task-1", run_id="run-from-runner") == "ok"
    assert observed[0].root_run_id == "run-from-runner"
    assert observed[0].local_run_id == "run-from-runner"


def test_base_run_releases_owned_root_after_failure(monkeypatch):
    from agentloom.runtime.trace import get_current_session_run_id, require_root_run_id

    agent = _make_agent(logger=DummyLoggerBackend())
    runtime_agent = RecordingAgentRuntime(exc=RuntimeError("boom-root"))
    snapshot_roots = []

    monkeypatch.setattr(agent, "build_runtime", lambda: runtime_agent)
    monkeypatch.setattr(
        agent,
        "_inject_memory_snapshot",
        lambda tasks: snapshot_roots.append(require_root_run_id()) or tasks,
    )
    with pytest.raises(RuntimeError, match="boom-root"):
        agent.run("top-level-failure")

    assert len(snapshot_roots) == 1
    assert snapshot_roots[0]
    assert get_current_session_run_id() is None


def test_runtime_close_failure_does_not_replace_run_failure(monkeypatch):
    agent = _make_agent(logger=DummyLoggerBackend())
    run_error = RuntimeError("provider failed")
    runtime_agent = RecordingAgentRuntime(exc=run_error)

    def fail_close():
        runtime_agent.close_calls += 1
        raise OSError("runtime close failed")

    runtime_agent.close = fail_close
    monkeypatch.setattr(agent, "build_runtime", lambda: runtime_agent)
    monkeypatch.setattr(agent, "_inject_memory_snapshot", lambda tasks: tasks)

    with pytest.raises(RuntimeError) as captured:
        agent.run("top-level-failure")

    assert captured.value is run_error
    assert runtime_agent.close_calls == 1
    assert any(
        "runtime close failed" in note
        for note in getattr(captured.value, "__notes__", ())
    )


def test_root_memory_review_runs_after_session_end_inside_owned_root(monkeypatch):
    from agentloom.runtime.trace import bind_root_run, require_root_run_id
    from agentloom.self_learning import reviewer

    agent = _make_review_agent(logger=DummyLoggerBackend())
    runtime_agent = RecordingAgentRuntime(output="ok")
    order = []

    monkeypatch.setattr(agent, "build_runtime", lambda: runtime_agent)
    monkeypatch.setattr(agent, "_inject_memory_snapshot", lambda tasks: tasks)
    monkeypatch.setattr(agent, "_emit_task_lifecycle_event", lambda *_args, **_kwargs: None)

    def _capture_session(event, *_args, **_kwargs):
        order.append((event.value, require_root_run_id()))

    def _capture_review(*, root_run_id, agent_config, **_kwargs):
        assert root_run_id == require_root_run_id()
        order.append(("MemoryReview", root_run_id, agent_config))
        return {"status": "skipped"}

    monkeypatch.setattr(agent, "_emit_session_lifecycle_event", _capture_session)
    monkeypatch.setattr(reviewer, "review_finished_run", _capture_review)

    assert agent.run("top-level") == "ok"
    assert [item[0] for item in order] == [
        HookEvent.SESSION_START.value,
        HookEvent.SESSION_END.value,
        "MemoryReview",
    ]
    assert len({item[1] for item in order}) == 1
    assert order[-1][2] == agent._effective_agent_config

    order.clear()
    with bind_root_run("supervisor-root"):
        assert agent.run("nested-worker") == "ok"
    assert order == []


def test_memory_review_failure_does_not_change_root_run_result(monkeypatch):
    from agentloom.self_learning import reviewer

    agent = _make_review_agent(logger=DummyLoggerBackend())
    runtime_agent = RecordingAgentRuntime(output="main-result")
    session_events = []

    monkeypatch.setattr(agent, "build_runtime", lambda: runtime_agent)
    monkeypatch.setattr(agent, "_inject_memory_snapshot", lambda tasks: tasks)
    monkeypatch.setattr(agent, "_emit_task_lifecycle_event", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        agent,
        "_emit_session_lifecycle_event",
        lambda event, *_args, **_kwargs: session_events.append(event),
    )
    monkeypatch.setattr(
        reviewer,
        "review_finished_run",
        lambda **_kwargs: (_ for _ in ()).throw(RuntimeError("review failed")),
    )

    assert agent.run("top-level") == "main-result"
    assert session_events == [HookEvent.SESSION_START, HookEvent.SESSION_END]


def test_disabled_self_learning_never_enters_completed_run_review(monkeypatch):
    from agentloom.self_learning import reviewer

    agent = DummyAgent(
        config={
            "name": "runtime_dummy_disabled_learning",
            "self_learning": {"enabled": False},
        },
        model_binding=_model_binding(),
        logger=DummyLoggerBackend(),
    )
    runtime_agent = RecordingAgentRuntime(output="main-result")
    review_calls = []

    monkeypatch.setattr(agent, "build_runtime", lambda: runtime_agent)
    monkeypatch.setattr(agent, "_inject_memory_snapshot", lambda tasks: tasks)
    monkeypatch.setattr(agent, "_emit_task_lifecycle_event", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(agent, "_emit_session_lifecycle_event", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        reviewer,
        "review_finished_run",
        lambda **kwargs: review_calls.append(kwargs),
    )

    assert agent.run("top-level") == "main-result"
    assert review_calls == []


def test_failed_root_records_session_end_without_running_memory_review(monkeypatch):
    from agentloom.self_learning import reviewer

    agent = _make_agent(logger=DummyLoggerBackend())
    runtime_agent = RecordingAgentRuntime(exc=RuntimeError("main failed"))
    session_events = []

    monkeypatch.setattr(agent, "build_runtime", lambda: runtime_agent)
    monkeypatch.setattr(agent, "_inject_memory_snapshot", lambda tasks: tasks)
    monkeypatch.setattr(agent, "_emit_task_lifecycle_event", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        agent,
        "_emit_session_lifecycle_event",
        lambda event, *_args, **_kwargs: session_events.append(event),
    )
    monkeypatch.setattr(
        reviewer,
        "review_finished_run",
        lambda **_kwargs: (_ for _ in ()).throw(AssertionError("failed runs must not be reviewed")),
    )

    with pytest.raises(RuntimeError, match="main failed"):
        agent.run("top-level")

    assert session_events == [HookEvent.SESSION_START, HookEvent.SESSION_END]


def test_max_steps_root_is_a_failure_and_never_runs_memory_review(monkeypatch):
    from agentloom.self_learning import reviewer

    agent = _make_agent(logger=DummyLoggerBackend())
    runtime_agent = RecordingAgentRuntime(
        output="fallback answer",
        state="max_steps_error",
    )
    task_events = []
    session_events = []

    monkeypatch.setattr(agent, "build_runtime", lambda: runtime_agent)
    monkeypatch.setattr(agent, "_inject_memory_snapshot", lambda tasks: tasks)
    monkeypatch.setattr(
        agent,
        "_emit_task_lifecycle_event",
        lambda event, *_args, **_kwargs: task_events.append(event),
    )
    monkeypatch.setattr(
        agent,
        "_emit_session_lifecycle_event",
        lambda event, *_args, **_kwargs: session_events.append(event),
    )
    monkeypatch.setattr(
        reviewer,
        "review_finished_run",
        lambda **_kwargs: (_ for _ in ()).throw(AssertionError("max-steps runs must not be reviewed")),
    )

    with pytest.raises(RuntimeError, match="max_steps_error"):
        agent.run("top-level")

    assert HookEvent.TASK_COMPLETED not in task_events
    assert HookEvent.STOP_FAILURE in task_events
    assert session_events == [HookEvent.SESSION_START, HookEvent.SESSION_END]


def test_max_steps_worker_is_failed_before_checkpoint_success(tmp_path, monkeypatch):
    from agentloom.runtime.checkpoint import CheckpointManager
    from agentloom.runtime.checkpoint.coordinator import CheckpointCoordinator

    class MaxStepsWorkerRuntime:
        runtime_id = "test"
        capabilities = base_agent_module.RuntimeCapabilities(
            structured_tools=True,
            parallel_tools=True,
            checkpoint_resume=True,
            subagents=True,
        )

        def run(self, request):
            return AgentRuntimeResult(
                output="fallback answer",
                state="max_steps_error",
            )

        def snapshot(self):
            return None

        def close(self):
            return None

    checkpoint_manager = CheckpointManager(
        "supervisor",
        checkpoints_root=tmp_path,
        run_id="run_test",
    )
    coordinator = CheckpointCoordinator(
        checkpoint_manager,
        "task-max-steps-worker",
        "delegate work",
    )
    worker = base_agent_module.SubTaskTrackedAgent(
        MaxStepsWorkerRuntime(),
        "max_steps_worker",
    )
    monkeypatch.setattr(
        CheckpointCoordinator,
        "current",
        staticmethod(lambda: coordinator),
    )

    with pytest.raises(RuntimeError, match="max_steps_error"):
        worker.run(AgentRuntimeRequest(task="delegate work"))

    tree = checkpoint_manager.load_task_tree("task-max-steps-worker")
    worker_call = tree["workers"]["max_steps_worker"][0]
    assert worker_call["status"] == "failed"
    checkpoint = checkpoint_manager.load_worker_checkpoint(
        "task-max-steps-worker",
        "max_steps_worker",
        call_index=worker_call["call_index"],
    )
    assert checkpoint["status"] == "failed"


@pytest.mark.parametrize("output", ["worker answer", "", None])
def test_completed_worker_resume_replays_output_in_requested_shape(tmp_path, monkeypatch, output):
    from agentloom.runtime.checkpoint import CheckpointManager
    from agentloom.runtime.checkpoint.coordinator import CheckpointCoordinator


    class SuccessfulRuntime:
        runtime_id = "test"
        capabilities = base_agent_module.RuntimeCapabilities(
            structured_tools=True,
            parallel_tools=True,
            checkpoint_resume=True,
            subagents=True,
        )

        def __init__(self):
            self.calls = 0

        def run(self, request):
            self.calls += 1
            return AgentRuntimeResult(output=output, state="success", usage=RuntimeUsage(input_tokens=20, output_tokens=11))

        def snapshot(self):
            return None

        def close(self):
            return None

    manager = CheckpointManager("supervisor", checkpoints_root=tmp_path, run_id="run_initial")
    coordinator = CheckpointCoordinator(manager, "task-completed-worker", "delegate")
    monkeypatch.setattr(CheckpointCoordinator, "current", staticmethod(lambda: coordinator))
    runtime = SuccessfulRuntime()
    worker = base_agent_module.SubTaskTrackedAgent(runtime, "completed_worker")

    initial = worker.run(AgentRuntimeRequest(task="delegate"))
    assert initial.output == output
    assert initial.usage == RuntimeUsage(input_tokens=20, output_tokens=11)
    checkpoint = manager.load_worker_checkpoint("task-completed-worker", "completed_worker", call_index=0)
    assert checkpoint.get("result") == output
    manager.close()
    manager = CheckpointManager("supervisor", checkpoints_root=tmp_path, run_id="run_resume")

    coordinator = CheckpointCoordinator(manager, "task-completed-worker", "delegate", resume=True)
    resumed = worker.run(AgentRuntimeRequest(task="delegate"))
    require_runtime_state(
        resumed,
        allowed_states={"success"},
        error_prefix="Agent run did not complete successfully",
    )
    assert resumed.output == initial.output
    assert resumed.usage == RuntimeUsage()
    assert runtime.calls == 1
    assert len(manager.load_task_tree("task-completed-worker")["workers"]["completed_worker"]) == 1

    manager.close()
    manager = CheckpointManager("supervisor", checkpoints_root=tmp_path, run_id="run_resume_plain")
    coordinator = CheckpointCoordinator(manager, "task-completed-worker", "delegate", resume=True)
    assert worker.run(AgentRuntimeRequest(task="delegate")).output == output
    assert runtime.calls == 1
    manager.close()


def test_max_steps_managed_worker_fails_before_call_discards_state(
    tmp_path,
    monkeypatch,
):
    from agentloom.runtime.checkpoint import CheckpointManager
    from agentloom.runtime.checkpoint.coordinator import CheckpointCoordinator
    from smolagents import RunResult

    native_runtime = ToolCallingAgentV2(
        tool_gateway=AgentLoomToolGateway([final_answer_binding()]),
        model=_model_binding().adapter,
        max_steps=1,
        max_tokens=32,
        verbosity_level=0,
    )
    monkeypatch.setattr(
        LoomAgentMixin,
        "run",
        lambda *_args, **_kwargs: RunResult(
            output="fallback answer",
            state="max_steps_error",
            steps=[],
            token_usage=None,
            timing=None,
        ),
    )
    checkpoint_manager = CheckpointManager(
        "supervisor",
        checkpoints_root=tmp_path,
        run_id="run_test",
    )
    coordinator = CheckpointCoordinator(
        checkpoint_manager,
        "task-max-steps-managed-worker",
        "delegate managed work",
    )
    from agentloom.adapters.smolagents.runtime_adapter import (
        SmolagentsRuntimeAdapter,
    )
    worker = base_agent_module.SubTaskTrackedAgent(
        SmolagentsRuntimeAdapter(
            native_runtime,
            model_binding=_model_binding(),
        ),
        "max_steps_managed_worker",
    )
    monkeypatch.setattr(
        CheckpointCoordinator,
        "current",
        staticmethod(lambda: coordinator),
    )

    with pytest.raises(RuntimeError, match="max_steps_error"):
        worker.run(AgentRuntimeRequest(task="delegate managed work"))

    tree = checkpoint_manager.load_task_tree("task-max-steps-managed-worker")
    worker_call = tree["workers"]["max_steps_managed_worker"][0]
    assert worker_call["status"] == "failed"
    checkpoint = checkpoint_manager.load_worker_checkpoint(
        "task-max-steps-managed-worker",
        "max_steps_managed_worker",
        call_index=worker_call["call_index"],
    )
    assert checkpoint["status"] == "failed"


def test_builtin_session_end_has_only_the_recorder_hook():
    agent = _make_agent(logger=DummyLoggerBackend())
    session_end_hooks = [handler for handler in agent._hook_plan.handlers if handler.event is HookEvent.SESSION_END]
    assert [handler.source for handler in session_end_hooks] == ["builtin:self_learning_recorder"]
    all_sources = {handler.source for handler in agent._hook_plan.handlers}
    assert "builtin:self_learning_reviewer" not in all_sources
    assert "builtin:self_learning_finalizer" not in all_sources


def test_default_system_config_does_not_enable_shell_hook_bundles():
    agent = _make_agent(logger=DummyLoggerBackend())

    assert all(handler.source.startswith("builtin:") for handler in agent._hook_plan.handlers)
    assert len(agent._hook_plan.fingerprint) == 64


def test_real_config_builder_compiles_global_application_and_agent_hook_layers(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import agentloom.configuration.config as config_module

    agent_root = tmp_path / "AgentLoom"
    config_dir = agent_root / "config"
    app_root = agent_root / "applications" / "demo"
    workflow_path = app_root / "workflows" / "agent.yaml"

    def write_yaml(path: Path, payload: dict) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            yaml.safe_dump(payload, sort_keys=False),
            encoding="utf-8",
        )

    write_yaml(
        config_dir / "system.yaml",
        {
            "system": {"name": "AgentLoom"},
            "hooks": {"SessionStart": [{"id": "layer.global", "command": "python global.py"}]},
        },
    )
    write_yaml(
        config_dir / "llm.yaml",
        {
            "model": {
                "default_model_type": "powerful",
                "common": {
                    "model": "openai/test-common",
                    "base_url": "https://example.test/v1",
                    "api_key": "test-key",
                },
                "powerful": {
                    "model": "openai/test-model",
                    "adapter": "openai_chat",
                },
                "summary": {
                    "model": "openai/test-summary",
                    "adapter": "openai_chat",
                },
            }
        },
    )
    write_yaml(
        app_root / "config" / "system.yaml",
        {"hooks": {"SessionStart": [{"id": "layer.application", "command": "python app.py"}]}},
    )
    write_yaml(workflow_path, {"name": "layered-agent"})
    monkeypatch.setattr(
        config_module,
        "_ACTIVE_CONFIG",
        config_module._load_merged_config(config_dir=config_dir),
    )

    agent = DummyAgent(
        config={
            "name": "layered-agent",
            "_yaml_file_path": str(workflow_path),
            "hooks": {"SessionStart": [{"id": "layer.agent", "command": "python agent.py"}]},
        },
        model_binding=_model_binding(),
        logger=DummyLoggerBackend(),
    )
    configured = [handler for handler in agent._hook_plan.handlers if handler.hook_id]

    assert [handler.hook_id for handler in configured] == [
        "layer.global",
        "layer.application",
        "layer.agent",
    ]
    assert [handler.cwd for handler in configured] == [
        str(agent_root.resolve()),
        str(app_root.resolve()),
        str(app_root.resolve()),
    ]


def test_successful_root_review_waits_for_session_end_recorder_commit(monkeypatch):
    """The public run seam must not review an incompletely finalized root."""
    from agentloom.self_learning import reviewer
    from agentloom.self_learning.persistence.ledger import SelfLearningLedger

    agent = _make_review_agent(logger=DummyLoggerBackend())
    runtime_agent = RecordingAgentRuntime(output="main-result")
    recorder_committed = threading.Event()
    observations = []

    monkeypatch.setattr(agent, "build_runtime", lambda: runtime_agent)
    monkeypatch.setattr(agent, "_inject_memory_snapshot", lambda tasks: tasks)

    session_end_handler = next(
        handler
        for handler in agent._hook_plan.handlers
        if handler.event is HookEvent.SESSION_END and handler.source == "builtin:self_learning_recorder"
    )
    original_recorder = session_end_handler.callback

    def _delayed_recorder(context):
        # Trusted Python handlers are synchronous, so review cannot overtake
        # this recorder commit.
        time.sleep(0.05)
        result = original_recorder(context)
        recorder_committed.set()
        return result

    agent._hook_plan = HookPlan(
        HookHandler(
            handler.event,
            handler.pattern,
            _delayed_recorder if handler is session_end_handler else handler.callback,
            source=handler.source,
        )
        for handler in agent._hook_plan.handlers
    )

    def _capture_review(*, root_run_id, **_kwargs):
        observations.append(
            {
                "recorder_committed": recorder_committed.is_set(),
                "completed_context": SelfLearningLedger().completed_review_context(
                    root_run_id,
                    tool_result_limit=1,
                ),
            }
        )
        return {"status": "skipped"}

    monkeypatch.setattr(reviewer, "review_finished_run", _capture_review)

    assert agent.run("top-level") == "main-result"
    # Let a timed-out daemon recorder finish before tmp-path cleanup when the
    # regression fails, so the test never leaks a background database write.
    assert recorder_committed.wait(timeout=2)
    assert len(observations) == 1
    assert observations[0]["recorder_committed"] is True
    assert observations[0]["completed_context"] is not None


def test_custom_session_end_telemetry_cannot_disable_persisted_review(monkeypatch):
    """The completed ledger projection, not shared telemetry, authorizes review."""
    from agentloom.self_learning import reviewer
    from agentloom.self_learning.persistence.ledger import SelfLearningLedger

    agent = _make_review_agent(logger=DummyLoggerBackend())
    runtime_agent = RecordingAgentRuntime(output="main-result")
    reviewed_roots = []

    monkeypatch.setattr(agent, "build_runtime", lambda: runtime_agent)
    monkeypatch.setattr(agent, "_inject_memory_snapshot", lambda tasks: tasks)

    def _overwrite_shared_telemetry(_context):
        return HookResult(
            decision="allow",
            telemetry={
                "self_learning_session_end_persisted_root_run_id": "other-root",
            },
        )

    def _capture_review(*, root_run_id, **_kwargs):
        assert (
            SelfLearningLedger().completed_review_context(
                root_run_id,
                tool_result_limit=0,
            )
            is not None
        )
        reviewed_roots.append(root_run_id)
        return {"status": "skipped"}

    _append_hook_handler(
        agent,
        HookEvent.SESSION_END,
        _overwrite_shared_telemetry,
        source="test:custom_session_end",
    )
    monkeypatch.setattr(reviewer, "review_finished_run", _capture_review)

    assert agent.run("top-level") == "main-result"
    assert len(reviewed_roots) == 1


def test_session_end_persistence_failure_never_builds_completed_run_review(
    monkeypatch,
):
    """A successful task is not reviewable until its SessionEnd is durable."""
    from agentloom.self_learning import reviewer
    from agentloom.self_learning.persistence.ledger import SelfLearningLedger
    from agentloom.self_learning.session_recorder import SessionRecorder

    agent = _make_review_agent(logger=DummyLoggerBackend())
    runtime_agent = RecordingAgentRuntime(output="main-result")
    failed_root_ids = []
    review_calls = []

    monkeypatch.setattr(agent, "build_runtime", lambda: runtime_agent)
    monkeypatch.setattr(agent, "_inject_memory_snapshot", lambda tasks: tasks)

    original_append = SessionRecorder.append

    def _fail_only_session_end(self, event, *, trusted_evidence=()):
        if event.event_type == "run_completed":
            failed_root_ids.append(event.root_run_id)
            raise OSError("simulated SessionEnd persistence failure")
        return original_append(
            self,
            event,
            trusted_evidence=trusted_evidence,
        )

    original_review = reviewer.review_finished_run

    def _capture_review(**kwargs):
        review_calls.append(kwargs)
        return original_review(**kwargs)

    monkeypatch.setattr(SessionRecorder, "append", _fail_only_session_end)
    monkeypatch.setattr(reviewer, "review_finished_run", _capture_review)
    monkeypatch.setattr(
        reviewer,
        "_resolve_review_model",
        lambda _model_type: pytest.fail("incomplete root resolved a review model"),
    )

    assert agent.run("top-level") == "main-result"
    assert len(failed_root_ids) == 1
    assert len(review_calls) == 1

    root_run_id = failed_root_ids[0]
    ledger = SelfLearningLedger()
    assert ledger.completed_review_context(root_run_id, tool_result_limit=1) is None
    assert ReviewEngine(ledger.db_path).status()["batches"] == []


def test_custom_session_end_telemetry_cannot_create_orphan_review_audit(
    monkeypatch,
):
    """Reviewer independently requires the persisted completed-run projection."""
    from agentloom.self_learning import reviewer
    from agentloom.self_learning.persistence.ledger import SelfLearningLedger
    from agentloom.self_learning.session_recorder import (
        SessionRecorder,
    )

    agent = _make_review_agent(logger=DummyLoggerBackend())
    runtime_agent = RecordingAgentRuntime(output="main-result")
    failed_root_ids = []

    monkeypatch.setattr(agent, "build_runtime", lambda: runtime_agent)
    monkeypatch.setattr(agent, "_inject_memory_snapshot", lambda tasks: tasks)

    original_append = SessionRecorder.append

    def _fail_only_session_end(self, event, *, trusted_evidence=()):
        if event.event_type == "run_completed":
            failed_root_ids.append(event.root_run_id)
            raise OSError("simulated SessionEnd persistence failure")
        return original_append(
            self,
            event,
            trusted_evidence=trusted_evidence,
        )

    def _forge_shared_telemetry(context):
        return HookResult(
            decision="allow",
            telemetry={
                "self_learning_session_end_persisted_root_run_id": (context.root_run_id),
            },
        )

    monkeypatch.setattr(SessionRecorder, "append", _fail_only_session_end)
    monkeypatch.setattr(
        reviewer,
        "_resolve_review_model",
        lambda _model_type: pytest.fail("forged receipt resolved a review model"),
    )
    _append_hook_handler(
        agent,
        HookEvent.SESSION_END,
        _forge_shared_telemetry,
        source="test:forged_shared_telemetry",
    )

    assert agent.run("top-level") == "main-result"
    assert len(failed_root_ids) == 1

    root_run_id = failed_root_ids[0]
    ledger = SelfLearningLedger()
    assert ledger.completed_review_context(root_run_id, tool_result_limit=1) is None
    assert ReviewEngine(ledger.db_path).status()["batches"] == []


def test_same_base_agent_concurrent_top_level_runs_do_not_cross_context(monkeypatch):
    from agentloom.runtime.trace import (
        capture_explicit_execution_context,
        clear_current_task_id,
        set_current_task_id,
    )

    agent = _make_agent(logger=DummyLoggerBackend())
    runtime_observations = {}
    lifecycle_observations = []
    runtimes: list[_InterleavedRuntime] = []

    class _InterleavedRuntime:
        runtime_id = "smolagents"
        capabilities = RuntimeCapabilities(True, True, True, True)

        def run(self, request: AgentRuntimeRequest) -> AgentRuntimeResult:
            current = capture_explicit_execution_context()
            runtime_observations[request.task] = {
                "task_id": current.task_id,
                "root_run_id": current.root_run_id,
                "local_run_id": current.local_run_id,
                "hook_run": current.hook_run,
                "agent_name": current.agent_name,
                "agent_config": current.agent_config,
            }
            return AgentRuntimeResult(state="success", output=request.task)

        def snapshot(self):
            return None

        def close(self):
            self.closed = True
            return None

    def build_runtime() -> _InterleavedRuntime:
        runtime = _InterleavedRuntime()
        runtime.closed = False
        runtimes.append(runtime)
        return runtime

    monkeypatch.setattr(agent, "build_runtime", build_runtime)
    monkeypatch.setattr(agent, "_inject_memory_snapshot", lambda tasks: tasks)

    def _record(context):
        current = capture_explicit_execution_context()
        lifecycle_observations.append(
            (
                HookEvent(context.hook_event_name),
                current.task_id,
                current.local_run_id,
                current.root_run_id,
            )
        )
        return HookResult()

    _append_hook_handler(agent, HookEvent.SESSION_START, _record)
    _append_hook_handler(agent, HookEvent.SESSION_END, _record)

    # Poison only the legacy process-wide fallback. Fresh worker threads have
    # no task ContextVar and must still use their explicit task_id arguments.
    set_current_task_id("wrong-global-task")
    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = {label: pool.submit(agent.run, label, task_id=f"task-{label}") for label in ("A", "B")}
            assert {label: future.result() for label, future in futures.items()} == {
                "A": "A",
                "B": "B",
            }
    finally:
        clear_current_task_id()

    roots = set()
    for label in ("A", "B"):
        observed = runtime_observations[label]
        assert observed["task_id"] == f"task-{label}"
        assert observed["root_run_id"] == observed["local_run_id"]
        assert isinstance(observed["hook_run"], HookRun)
        assert observed["hook_run"].plan is agent._hook_plan
        assert observed["agent_name"] == agent.name
        assert observed["agent_config"] == agent._effective_agent_config
        roots.add(observed["root_run_id"])
    assert len(roots) == 2
    assert len(runtimes) == 2
    assert all(runtime.closed for runtime in runtimes)

    for label in ("A", "B"):
        events = [item for item in lifecycle_observations if item[1] == f"task-{label}"]
        assert [item[0] for item in events] == [
            HookEvent.SESSION_START,
            HookEvent.SESSION_END,
        ]
        assert all(item[2] == item[3] == runtime_observations[label]["root_run_id"] for item in events)


def test_subagent_lifecycle_belongs_to_parent_while_worker_tools_belong_to_child():
    root_events: list[str] = []
    child_events: list[str] = []

    def record_root(context):
        root_events.append(context.hook_event_name)
        return HookResult()

    def record_child(context):
        child_events.append(context.hook_event_name)
        return HookResult()

    root_run = HookRun(
        HookPlan(
            (
                HookHandler(HookEvent.SUBAGENT_START, "*", record_root),
                HookHandler(HookEvent.SUBAGENT_STOP, "*", record_root),
            )
        ),
        local_run_id="root",
        root_run_id="root",
    )
    child_run = HookRun(
        HookPlan(
            (
                HookHandler(HookEvent.PRE_TOOL_USE, "add", record_child),
                HookHandler(HookEvent.POST_TOOL_USE, "add", record_child),
                HookHandler(HookEvent.SUBAGENT_START, "*", record_child),
                HookHandler(HookEvent.SUBAGENT_STOP, "*", record_child),
            )
        ),
        local_run_id="worker",
        root_run_id="root",
        parent=root_run,
    )

    @tool
    def add(a: int, b: int) -> int:
        """Add two numbers.

        Args:
            a: First number.
            b: Second number.
        """

        return a + b

    child_gateway = AgentLoomToolGateway.from_tools([add])

    class _WorkerRuntime:
        runtime_id = "test"
        capabilities = base_agent_module.RuntimeCapabilities(
            structured_tools=True,
            parallel_tools=True,
            checkpoint_resume=True,
            subagents=True,
        )

        def run(self, _request):
            current = capture_explicit_execution_context()
            with bind_explicit_execution_context(
                replace(
                    current,
                    hook_run=child_run,
                    local_run_id="worker",
                    root_run_id="root",
                )
            ):
                return AgentRuntimeResult(
                    output=child_gateway.invoke(
                        call_id="child-add",
                        tool_name="add",
                        arguments={"a": 1, "b": 2},
                    ).direct_result(),
                    state="success",
                )

        def snapshot(self):
            return None

        def close(self):
            return None

    set_current_hook_run(root_run)
    worker = base_agent_module.SubTaskTrackedAgent(_WorkerRuntime(), "worker-agent")

    assert worker.run(AgentRuntimeRequest(task="delegated")).output == 3
    assert root_events == ["SubagentStart", "SubagentStop"]
    assert child_events == ["PreToolUse", "PostToolUse"]

    class _NestedRuntime:
        runtime_id = "test"
        capabilities = base_agent_module.RuntimeCapabilities(
            structured_tools=True,
            parallel_tools=True,
            checkpoint_resume=True,
            subagents=True,
        )

        def run(self, _request):
            return AgentRuntimeResult(output="nested", state="success")

        def snapshot(self):
            return None

        def close(self):
            return None

    set_current_hook_run(child_run)
    grandchild = base_agent_module.SubTaskTrackedAgent(
        _NestedRuntime(),
        "grandchild-agent",
    )
    assert (
        grandchild.run(
            AgentRuntimeRequest(task="nested delegated")
        ).output
        == "nested"
    )
    assert root_events == ["SubagentStart", "SubagentStop"]
    assert child_events == [
        "PreToolUse",
        "PostToolUse",
        "SubagentStart",
        "SubagentStop",
    ]

    # A managed-agent adapter may pre-bind the callee run before entering the
    # lifecycle wrapper. The wrapper must still emit on its parent run.
    current = capture_explicit_execution_context()
    with bind_explicit_execution_context(
        replace(
            current,
            agent_name="worker-agent",
            runtime_agent_path="parent/worker-agent",
            hook_run=child_run,
            local_run_id="worker",
            root_run_id="root",
        )
    ):
        prebound_worker = base_agent_module.SubTaskTrackedAgent(
            _NestedRuntime(),
            "worker-agent",
        )
        assert (
            prebound_worker.run(
                AgentRuntimeRequest(task="prebound")
            ).output
            == "nested"
        )

    assert root_events == [
        "SubagentStart",
        "SubagentStop",
        "SubagentStart",
        "SubagentStop",
    ]
    assert child_events == [
        "PreToolUse",
        "PostToolUse",
        "SubagentStart",
        "SubagentStop",
    ]


def test_worker_subtask_cannot_emit_root_task_lifecycle() -> None:
    from agentloom.runtime.trace import sub_task_context

    events: list[HookEvent] = []
    agent = _make_agent(logger=DummyLoggerBackend())
    agent._task_id = "worker-task"

    def record(context):
        events.append(HookEvent(context.hook_event_name))
        return HookResult()

    _append_hook_handler(agent, HookEvent.TASK_COMPLETED, record)
    _append_hook_handler(agent, HookEvent.STOP_FAILURE, record)
    run = HookRun(
        agent._hook_plan,
        local_run_id="worker-run",
        root_run_id="root-run",
    )

    with bind_explicit_execution_context(replace(capture_explicit_execution_context(), hook_run=run)):
        with sub_task_context("worker"):
            agent._emit_task_lifecycle_event(
                HookEvent.TASK_COMPLETED,
                "delegated",
                result="done",
            )
            agent._emit_task_lifecycle_event(
                HookEvent.STOP_FAILURE,
                "delegated",
                error=RuntimeError("failed"),
            )

    assert events == []


def test_root_task_lifecycle_never_reads_legacy_subtask_fallback() -> None:
    from agentloom.runtime.trace import clear_current_sub_task_id, set_current_sub_task_id

    events: list[HookEvent] = []
    agent = _make_agent(logger=DummyLoggerBackend())

    def record(context):
        events.append(HookEvent(context.hook_event_name))
        return HookResult()

    _append_hook_handler(agent, HookEvent.TASK_COMPLETED, record)
    run = HookRun(
        agent._hook_plan,
        local_run_id="root-local",
        root_run_id="root-local",
    )
    explicit_root = replace(
        capture_explicit_execution_context(),
        task_id="root-task",
        sub_task_id=None,
        hook_run=run,
    )

    # Poison the legacy process-wide fallback. A fresh executor thread has no
    # subtask ContextVar and must still treat its explicit context as root.
    set_current_sub_task_id("other-run-worker")
    try:

        def emit_from_root_thread() -> None:
            with bind_explicit_execution_context(explicit_root):
                agent._emit_task_lifecycle_event(
                    HookEvent.TASK_COMPLETED,
                    "root task",
                    result="done",
                )

        with ThreadPoolExecutor(max_workers=1) as pool:
            pool.submit(emit_from_root_thread).result()
    finally:
        clear_current_sub_task_id()

    assert events == [HookEvent.TASK_COMPLETED]


def test_each_run_rebinds_message_sink_for_fresh_runtime(monkeypatch):
    agent = _make_agent(logger=DummyLoggerBackend())
    runtimes: list[RecordingAgentRuntime] = []
    delivered: list[str] = []

    def build_runtime() -> RecordingAgentRuntime:
        runtime = RecordingAgentRuntime(output="ok")
        runtimes.append(runtime)
        return runtime

    monkeypatch.setattr(agent, "build_runtime", build_runtime)
    monkeypatch.setattr(agent, "_inject_memory_snapshot", lambda tasks: tasks)
    monkeypatch.setattr(
        agent,
        "_emit_hook_user_message",
        lambda _logger, message: delivered.append(message),
    )
    _append_hook_handler(
        agent,
        HookEvent.SESSION_START,
        lambda context: HookResult(user_message=context.local_run_id),
    )

    assert agent.run("first") == "ok"
    assert agent.run("second") == "ok"

    assert len(delivered) == 2
    assert delivered[0] != delivered[1]
    assert len(runtimes) == 2
    assert [runtime.close_calls for runtime in runtimes] == [1, 1]


def test_same_base_agent_serializes_fresh_runtime_runs(monkeypatch):
    from agentloom.runtime.trace import require_root_run_id

    agent = _make_agent(logger=DummyLoggerBackend())

    state_lock = threading.Lock()
    first_entered = threading.Event()
    second_attempted = threading.Event()
    second_entered = threading.Event()
    release_first = threading.Event()
    active = 0
    max_active = 0
    runtimes = []

    def build_runtime() -> RecordingAgentRuntime:
        nonlocal active, max_active

        def run(request: AgentRuntimeRequest) -> AgentRuntimeResult:
            nonlocal active, max_active
            with state_lock:
                active += 1
                max_active = max(max_active, active)
            if request.task == "A":
                first_entered.set()
                assert release_first.wait(timeout=5)
            else:
                second_entered.set()
            with state_lock:
                active -= 1
            return AgentRuntimeResult(state="success", output=request.task)

        runtime = RecordingAgentRuntime(side_effect=run)
        runtimes.append(runtime)
        return runtime

    monkeypatch.setattr(agent, "build_runtime", build_runtime)
    monkeypatch.setattr(agent, "_inject_memory_snapshot", lambda tasks: tasks)
    monkeypatch.setattr(agent, "_emit_task_lifecycle_event", lambda *_args, **_kwargs: None)
    session_ends = []

    def _capture_session(event, task, *, result=None, error=None):
        if event is HookEvent.SESSION_END:
            session_ends.append((task, result, require_root_run_id()))

    monkeypatch.setattr(agent, "_emit_session_lifecycle_event", _capture_session)

    def _run_second():
        second_attempted.set()
        return agent.run("B", task_id="task-B")

    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(agent.run, "A", task_id="task-A")
        assert first_entered.wait(timeout=5)
        second = pool.submit(_run_second)
        assert second_attempted.wait(timeout=5)
        # The second BaseAgent invocation has started, but it must not enter the
        # shared smolagents runtime until A has completely finalized.
        assert not second_entered.wait(timeout=0.1)
        release_first.set()
        assert first.result(timeout=5) == "A"
        assert second.result(timeout=5) == "B"

    assert max_active == 1
    assert len(runtimes) == 2
    assert [runtime.close_calls for runtime in runtimes] == [1, 1]
    assert {task: result for task, result, _root in session_ends} == {
        "A": "A",
        "B": "B",
    }
    assert len({root for _task, _result, root in session_ends}) == 2


def test_base_run_executes_transformed_tasks_sequentially_with_reset_false(tmp_path, monkeypatch):
    monkeypatch.setenv(
        "AGENTLOOM_RUNTIME_ROOT",
        str(tmp_path / ".agentloom"),
    )
    agent = _make_agent(logger=DummyLoggerBackend())
    results = iter(["first-result", "second-result", "final-result"])
    build_calls = []

    def _recording_run(_request: AgentRuntimeRequest) -> AgentRuntimeResult:
        return AgentRuntimeResult(
            state="success",
            output=next(results),
        )

    runtime_agent = RecordingAgentRuntime(side_effect=_recording_run)
    monkeypatch.setattr(
        agent,
        "build_runtime",
        lambda: build_calls.append(runtime_agent) or runtime_agent,
    )
    monkeypatch.setattr(agent, "_transform_tasks", lambda _task: ["first task", "second task", "third task"])

    result = agent.run("do work", task_id="multi-workflow")

    assert result == "final-result"
    assert build_calls == [runtime_agent]
    assert [request.task for request in runtime_agent.requests] == [
        "first task",
        "second task",
        "third task",
    ]
    assert [
        (request.continue_session, request.record_task)
        for request in runtime_agent.requests
    ] == [(False, False), (True, True), (True, True)]
    assert {
        request.task_id for request in runtime_agent.requests
    } == {"multi-workflow"}
    assert len(
        {request.run_id for request in runtime_agent.requests}
    ) == 1
    assert all(request.run_id for request in runtime_agent.requests)
    assert all(
        request.requirements
        == RuntimeRequirements(structured_tools=True, checkpoint_resume=True)
        for request in runtime_agent.requests
    )
    assert all(
        callable(request.event_sink)
        for request in runtime_agent.requests
    )


def test_invocation_collects_ordered_runtime_events_without_sink_duplicates(
    monkeypatch,
) -> None:
    agent = _make_agent(logger=DummyLoggerBackend())
    lifecycle = MagicMock()
    call_index = 0

    def _recording_run(request: AgentRuntimeRequest) -> AgentRuntimeResult:
        nonlocal call_index
        call_index += 1
        event = RuntimeEvent(
            kind="model",
            task_id=request.task_id,
            run_id=request.run_id,
            details={"segment": call_index},
        )
        assert request.event_sink is not None
        request.event_sink(event)
        return AgentRuntimeResult(
            state="success",
            output=f"result-{call_index}",
            events=(event,),
        )

    runtime = RecordingAgentRuntime(side_effect=_recording_run)
    monkeypatch.setattr(agent, "build_runtime", lambda: runtime)
    monkeypatch.setattr(agent, "_inject_memory_snapshot", lambda tasks: tasks)
    monkeypatch.setattr(
        agent,
        "_transform_tasks",
        lambda _task: ["first task", "second task"],
    )

    assert agent.run(
        "do work",
        task_id="event-task",
        application_lifecycle=lifecycle,
    ) == "result-2"

    reported = lifecycle.report_agent_invocation.call_args.kwargs[
        "runtime_result"
    ]
    assert [
        event.details["segment"] for event in reported.events
    ] == [1, 2]
    observed_by_lifecycle = [
        call.args[0]
        for call in lifecycle.observe_runtime_event.call_args_list
    ]
    assert [
        event.details["segment"] for event in observed_by_lifecycle
    ] == [1, 2]


def test_goal_mode_continues_after_normal_final_until_update_goal(monkeypatch):
    from agentloom.runtime.goal import get_current_goal_provider

    agent = DummyGoalAgent(
        config={
            "name": "goal-runtime",
            "description": "Finish all work.",
            "workflow": "Implement and verify.",
            "goal": {"enabled": True},
        },
        model_binding=_model_binding(),
        logger=DummyLoggerBackend(),
    )
    def _run(_request: AgentRuntimeRequest) -> AgentRuntimeResult:
        if len(runtime.requests) == 2:
            get_current_goal_provider(required=True).complete("implemented; tests passed")
        return AgentRuntimeResult(
            state="success",
            output=f"segment-{len(runtime.requests)}",
        )

    runtime = RecordingAgentRuntime(side_effect=_run)
    monkeypatch.setattr(agent, "build_runtime", lambda: runtime)
    monkeypatch.setattr(agent, "_inject_memory_snapshot", lambda tasks: tasks)

    result = agent.run("Add Goal mode", task_id="goal-task")

    assert result == "segment-2"
    assert len(runtime.requests) == 2
    assert runtime.requests[0].task == "Add Goal mode"
    assert "Continue working toward the active Goal" in runtime.requests[1].task
    assert "Implement and verify." not in runtime.requests[1].task
    assert "Goal ID: goal_" in runtime.requests[1].task
    assert runtime.requests[1].continue_session is True
    assert runtime.requests[1].record_task is True


def test_goal_mode_treats_max_steps_as_continuation_boundary(monkeypatch):
    from agentloom.runtime.goal import get_current_goal_provider

    agent = DummyGoalAgent(
        config={
            "name": "goal-runtime",
            "description": "Finish all work.",
            "workflow": "Implement and verify.",
            "goal": True,
        },
        model_binding=_model_binding(),
        logger=DummyLoggerBackend(),
    )
    def _run(_request: AgentRuntimeRequest) -> AgentRuntimeResult:
        if len(runtime.requests) == 2:
            get_current_goal_provider(required=True).complete("done")
        state = "max_steps_error" if len(runtime.requests) == 1 else "success"
        output = "segment" if len(runtime.requests) == 1 else "done"
        return AgentRuntimeResult(output=output, state=state)

    runtime = RecordingAgentRuntime(side_effect=_run)
    monkeypatch.setattr(agent, "build_runtime", lambda: runtime)
    monkeypatch.setattr(agent, "_inject_memory_snapshot", lambda tasks: tasks)

    assert agent.run("Add Goal mode", task_id="goal-task") == "done"
    assert len(runtime.requests) == 2


def test_goal_mode_uses_evidence_when_max_steps_final_delivery_failed(monkeypatch):
    from agentloom.runtime.goal import get_current_goal_provider

    agent = DummyGoalAgent(
        config={
            "name": "goal-runtime",
            "description": "Finish all work.",
            "workflow": "Implement and verify.",
            "goal": True,
        },
        model_binding=_model_binding(),
        logger=DummyLoggerBackend(),
    )
    def _run(_request: AgentRuntimeRequest) -> AgentRuntimeResult:
        get_current_goal_provider(required=True).complete("durable evidence")
        return AgentRuntimeResult(
            state="max_steps_error",
            output="Error in generating final LLM output: Goal is already complete",
        )

    runtime = RecordingAgentRuntime(side_effect=_run)
    monkeypatch.setattr(agent, "build_runtime", lambda: runtime)
    monkeypatch.setattr(agent, "_inject_memory_snapshot", lambda tasks: tasks)

    assert agent.run("Add Goal mode", task_id="goal-task") == "durable evidence"


def test_goal_mode_ignores_legacy_budget_and_continues_until_completed(monkeypatch):
    from agentloom.runtime.goal import get_current_goal_provider

    agent = DummyGoalAgent(
        config={
            "name": "goal-runtime",
            "description": "Finish all work.",
            "workflow": "Implement and verify.",
            "goal": {"enabled": True, "token_budget": 100},
        },
        model_binding=_model_binding(),
        logger=DummyLoggerBackend(),
    )
    def _run(_request: AgentRuntimeRequest) -> AgentRuntimeResult:
        if len(runtime.requests) == 2:
            get_current_goal_provider(required=True).complete("verified")
        return AgentRuntimeResult(state="success", output="ordinary final",
                                  usage=RuntimeUsage(input_tokens=9000, output_tokens=2000))

    runtime = RecordingAgentRuntime(side_effect=_run)
    monkeypatch.setattr(agent, "build_runtime", lambda: runtime)
    monkeypatch.setattr(agent, "_inject_memory_snapshot", lambda tasks: tasks)

    assert agent.run("Add Goal mode", task_id="goal-task") == "ordinary final"
    assert len(runtime.requests) == 2
    assert "budget" not in runtime.requests[1].task.lower()


def test_goal_mode_resume_after_completion_commit_does_not_restart_work(
    tmp_path,
    monkeypatch,
):
    from agentloom.runtime.checkpoint import CheckpointManager
    from agentloom.runtime.goal import get_current_goal_provider

    config = {
        "name": "goal-runtime",
        "description": "Finish all work.",
        "workflow": "Implement and verify.",
        "goal": True,
    }
    manager = CheckpointManager("goal-runtime", checkpoints_root=tmp_path)
    first_agent = DummyGoalAgent(
        config=config,
        model_binding=_model_binding(),
        logger=DummyLoggerBackend(),
    )
    def _commit_then_interrupt(_request: AgentRuntimeRequest) -> AgentRuntimeResult:
        get_current_goal_provider(required=True).complete("delivered; tests passed")
        raise KeyboardInterrupt("crash after completion commit")

    first_runtime = RecordingAgentRuntime(side_effect=_commit_then_interrupt)
    monkeypatch.setattr(first_agent, "build_runtime", lambda: first_runtime)
    monkeypatch.setattr(first_agent, "_inject_memory_snapshot", lambda tasks: tasks)

    with pytest.raises(KeyboardInterrupt):
        first_agent.run(
            "Add Goal mode",
            task_id="goal-complete-crash",
            checkpoint_manager=manager,
        )

    persisted = manager.load_goal("goal-complete-crash")
    assert persisted["status"] == "complete"
    assert persisted["evidence"] == "delivered; tests passed"

    resumed_agent = DummyGoalAgent(
        config=config,
        model_binding=_model_binding(),
        logger=DummyLoggerBackend(),
    )
    resumed_runtime = RecordingAgentRuntime()
    monkeypatch.setattr(resumed_agent, "build_runtime", lambda: resumed_runtime)
    monkeypatch.setattr(resumed_agent, "_inject_memory_snapshot", lambda tasks: tasks)

    result = resumed_agent.run(
        "Add Goal mode",
        task_id="goal-complete-crash",
        checkpoint_manager=manager,
        resume=True,
    )

    assert result == "delivered; tests passed"
    assert resumed_runtime.requests == []
    assert manager.load_goal("goal-complete-crash")["goal_id"] == persisted["goal_id"]


@pytest.mark.parametrize("goal", [None, False, {"enabled": False}])
def test_goal_tools_are_absent_when_goal_mode_is_disabled(monkeypatch, goal):
    config = {
        "name": "goal-runtime",
        "description": "Finish all work.",
        "workflow": "Implement and verify.",
        "runtime_options": {"todo_mode": "off"},
    }
    if goal is not None:
        config["goal"] = goal
    agent = DummyGoalAgent(
        config=config,
        model_binding=_model_binding(),
        logger=DummyLoggerBackend(),
    )
    monkeypatch.setattr(agent, "get_all_tools", lambda agent_type: [])

    assert agent._build_runtime_tools(agent._role_profile()) == []


def test_goal_tools_are_added_only_for_enabled_root_supervisor(monkeypatch):
    agent = DummyGoalAgent(
        config={
            "name": "goal-runtime",
            "description": "Finish all work.",
            "workflow": "Implement and verify.",
            "goal": True,
            "runtime_options": {"todo_mode": "off"},
        },
        model_binding=_model_binding(),
        logger=DummyLoggerBackend(),
    )
    monkeypatch.setattr(agent, "get_all_tools", lambda agent_type: [])

    gateway = agent._build_tool_gateway()
    names = {tool.name for tool in gateway.definitions}

    assert names == {"get_goal", "update_goal"}
    assert {entry.owner for entry in gateway.manifest} == {"platform"}


def test_loom_runtime_can_keep_task_step_for_sequential_reset_false():
    runtime_agent = DummyLoomRuntime()
    runtime_agent.memory.steps.append(runtime_agent.TaskStep(task="original task"))

    runtime_agent.run("resume task", reset=False)
    assert [step.task for step in runtime_agent.memory.steps] == ["original task"]

    runtime_agent.run(
        "next workflow task",
        reset=False,
        _skip_task_step_on_reset_false=False,
    )
    assert [step.task for step in runtime_agent.memory.steps] == [
        "original task",
        "next workflow task",
    ]


def test_base_run_emits_task_fail_on_exception(monkeypatch):
    agent = _make_agent(logger=DummyLoggerBackend())
    runtime_agent = RecordingAgentRuntime(exc=RuntimeError("boom-run"))
    events = []

    monkeypatch.setattr(agent, "build_runtime", lambda: runtime_agent)

    def _record(context):
        events.append(
            (HookEvent(context.hook_event_name), context.tool_name, dict(context.tool_input), context.tool_response)
        )
        return HookResult()

    _append_hook_handler(agent, HookEvent.STOP_FAILURE, _record)

    with pytest.raises(RuntimeError, match="boom-run"):
        agent.run("do work", task_id="task-fail")

    assert any(event is HookEvent.STOP_FAILURE for event, *_ in events)
    fail_event = next(item for item in events if item[0] is HookEvent.STOP_FAILURE)
    assert fail_event[2]["task_id"] == "task-fail"
    assert fail_event[2]["error"] == "boom-run"
