from __future__ import annotations

import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path
from unittest.mock import MagicMock

import pytest
import yaml

import src.lib.smolagents.agent.base_agent as base_agent_module
import src.lib.smolagents.agent.invocation as invocation_module
import src.lib.smolagents.prompts.prompt_builder as prompt_builder_module
from src.extensions.self_learning.persistence.review_engine import ReviewEngine
from src.lib.logging import get_global_logger, set_global_logger
from src.lib.smolagents.agent.loom_mixin import LoomAgentMixin
from src.lib.smolagents.hooks import HookEvent, HookHandler, HookPlan, HookResult, HookRun
from src.lib.smolagents.hooks.tool_shim import inject_hooks
from src.lib.smolagents.skills.catalog import SkillCatalog
from src.lib.smolagents.tools.tools import tool
from src.trace import (
    bind_explicit_execution_context,
    capture_explicit_execution_context,
    clear_current_hook_run,
    get_current_hook_run,
    set_current_hook_run,
)


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


class DummyCodeAgent:
    def __init__(self, *args, **kwargs):
        self.logger = kwargs.get("logger")
        self.kwargs = kwargs
        self.tools = kwargs.get("tools", [])


class DummyWrapper:
    def __init__(self, agent, agent_name: str):
        self.agent = agent
        self.agent_name = agent_name


class DummyRunResult:
    def __init__(self, output, state: str = "success"):
        self.output = output
        self.state = state


class DummyRuntimeRunner:
    def __init__(self, result=None, exc: Exception | None = None):
        self._result = result
        self._exc = exc
        self.calls = []

    def run(self, *, task, **kwargs):
        self.calls.append({"task": task, **kwargs})
        if self._exc is not None:
            raise self._exc
        if hasattr(self._result, "state"):
            return self._result
        return DummyRunResult(self._result)


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

    def _role_profile(self) -> base_agent_module.AgentRoleProfile:
        return base_agent_module.AgentRoleProfile(
            agent_type=base_agent_module.AgentType.WORKER,
            tool_call_type="code_act",
        )

    def _get_tools(self):
        return []


class DummyToolCallingAgent(base_agent_module.RoleDrivenAgent):
    max_steps = 3

    def _role_profile(self) -> base_agent_module.AgentRoleProfile:
        return base_agent_module.AgentRoleProfile(
            agent_type=base_agent_module.AgentType.WORKER,
            tool_call_type="tool_call",
        )

    def _get_tools(self):
        return []


class DummyGoalAgent(DummyAgent):
    def _role_profile(self) -> base_agent_module.AgentRoleProfile:
        return base_agent_module.AgentRoleProfile(
            agent_type=base_agent_module.AgentType.SUPERVISOR,
            tool_call_type="code_act",
        )


def _make_agent(*, logger=None) -> DummyAgent:
    agent = DummyAgent(
        config={"name": "runtime_dummy"},
        model=object(),
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
        model=object(),
        logger=logger,
    )


def test_role_driven_agent_reports_to_application_lifecycle(monkeypatch):
    agent = _make_agent(logger=DummyLoggerBackend())
    runtime = DummyRuntimeRunner("reported-result")
    lifecycle = MagicMock()
    monkeypatch.setattr(agent, "build_runtime_agent", lambda: runtime)
    monkeypatch.setattr(agent, "_inject_memory_snapshot", lambda tasks: tasks)

    result = agent.run(
        "application task",
        task_id="task-1",
        application_lifecycle=lifecycle,
    )

    assert result == "reported-result"
    lifecycle.report_agent_invocation.assert_called_once_with(
        coordinator=None,
        runtime_agent=runtime,
        result="reported-result",
        error=None,
        goal=None,
    )


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


def test_standalone_checkpoint_failure_still_deactivates_coordinator(
    tmp_path,
    monkeypatch,
):
    from src.lib.checkpoint import CheckpointManager
    from src.lib.checkpoint.coordinator import CheckpointCoordinator

    agent = _make_agent(logger=DummyLoggerBackend())
    runtime = DummyRuntimeRunner("reported-result")
    manager = CheckpointManager("runtime-dummy", checkpoints_root=tmp_path)
    monkeypatch.setattr(agent, "build_runtime_agent", lambda: runtime)
    monkeypatch.setattr(agent, "_inject_memory_snapshot", lambda tasks: tasks)
    monkeypatch.setattr(
        CheckpointCoordinator,
        "save_supervisor",
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
    from src.lib.checkpoint import CheckpointManager

    agent = _make_agent(logger=DummyLoggerBackend())
    runtime = DummyRuntimeRunner()
    runtime.memory = MagicMock(steps=[])
    manager = CheckpointManager("runtime-dummy", checkpoints_root=tmp_path)

    def _exit(*, task, **kwargs):
        raise SystemExit("runtime exited")

    runtime.run = _exit
    monkeypatch.setattr(agent, "build_runtime_agent", lambda: runtime)
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
    from src.extensions.self_learning import reviewer

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
        model=object(),
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
    runtime_agent = DummyRuntimeRunner(result="main-result")
    calls = []
    monkeypatch.setattr(agent, "build_runtime_agent", lambda: runtime_agent)
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


def _make_tool_call_agent(*, logger=None) -> DummyToolCallingAgent:
    agent = DummyToolCallingAgent(
        config={"name": "runtime_dummy_tool_call"},
        model=object(),
        logger=logger,
    )
    return agent


def _append_hook_handler(agent, event, callback, *, source="test"):
    agent._hook_plan = HookPlan((*agent._hook_plan.handlers, HookHandler(event, "*", callback, source=source)))


def _patch_agent_classes(monkeypatch):
    monkeypatch.setattr(base_agent_module, "CodeAgentV2", DummyCodeAgent)
    monkeypatch.setattr(base_agent_module, "ToolCallingAgentV2", DummyCodeAgent)
    monkeypatch.setattr(base_agent_module, "SubTaskTrackedAgent", DummyWrapper)


def test_create_agent_uses_global_logger_when_not_provided(monkeypatch):
    _patch_agent_classes(monkeypatch)
    previous_global_logger = get_global_logger(create_if_missing=False)
    global_logger = DummyLoggerBackend()
    set_global_logger(global_logger)

    try:
        agent = _make_agent(logger=None)
        runtime_agent = agent._create_agent(tools=[], use_customized_prompt=False)
        assert isinstance(runtime_agent, DummyCodeAgent)
        assert runtime_agent.logger is global_logger
    finally:
        set_global_logger(previous_global_logger)


def test_create_agent_requires_global_logger(monkeypatch):
    """Standalone construction can explicitly initialize a console backend."""
    from src.lib.logging import initialize_global_logger_once

    _patch_agent_classes(monkeypatch)
    previous_global_logger = get_global_logger(create_if_missing=False)
    set_global_logger(None)

    try:
        initialize_global_logger_once("test_runtime_builder")
        agent = _make_agent(logger=None)
        runtime_agent = agent._create_agent(tools=[], use_customized_prompt=False)
        assert isinstance(runtime_agent, DummyCodeAgent)
        assert runtime_agent.logger is not None
        assert get_global_logger(create_if_missing=False) is not None
    finally:
        set_global_logger(previous_global_logger)


def test_create_agent_wraps_sub_task_when_enabled(monkeypatch):
    _patch_agent_classes(monkeypatch)
    previous_global_logger = get_global_logger(create_if_missing=False)
    set_global_logger(DummyLoggerBackend())

    try:
        agent = _make_agent(logger=None)
        wrapped = agent._create_agent(
            tools=[],
            use_customized_prompt=False,
            enable_sub_task_tracking=True,
            agent_name="worker_agent",
        )
        assert isinstance(wrapped, DummyWrapper)
        assert wrapped.agent_name == "worker_agent"
        assert isinstance(wrapped.agent, DummyCodeAgent)
    finally:
        set_global_logger(previous_global_logger)


def test_create_agent_deduplicates_tools_injects_hooks_and_prompt(monkeypatch):
    _patch_agent_classes(monkeypatch)
    previous_global_logger = get_global_logger(create_if_missing=False)
    set_global_logger(DummyLoggerBackend())
    captured = {}

    def _fake_wrap(tools):
        captured["wrapped_input"] = list(tools)
        return list(tools)

    def _fake_hook(tool):
        return f"hooked:{tool}"

    monkeypatch.setattr(base_agent_module, "ensure_tool_wrapped", _fake_wrap)
    monkeypatch.setattr(base_agent_module, "clone_tool_for_runtime", lambda tool: tool)
    monkeypatch.setattr(base_agent_module, "inject_hooks", _fake_hook)
    monkeypatch.setattr(
        base_agent_module.RoleDrivenAgent,
        "_build_prompt_templates",
        lambda self, **_: {"system_prompt": "patched"},
    )

    try:
        agent = _make_agent(logger=None)
        runtime_agent = agent._create_agent(
            tools=["tool_a", "tool_a", "tool_b"],
            use_customized_prompt=True,
        )
        assert captured["wrapped_input"] == ["tool_a", "tool_b"]
        assert runtime_agent.tools == ["hooked:tool_a", "hooked:tool_b"]
        assert runtime_agent.kwargs["prompt_templates"] == {"system_prompt": "patched"}
    finally:
        set_global_logger(previous_global_logger)


def test_create_tool_call_agent_also_receives_prompt_templates(monkeypatch):
    _patch_agent_classes(monkeypatch)
    previous_global_logger = get_global_logger(create_if_missing=False)
    set_global_logger(DummyLoggerBackend())

    monkeypatch.setattr(
        base_agent_module.RoleDrivenAgent,
        "_build_prompt_templates",
        lambda self, **_: {"system_prompt": "patched"},
    )

    try:
        agent = _make_tool_call_agent(logger=None)
        runtime_agent = agent._create_agent(
            tools=[],
            use_customized_prompt=True,
        )
        assert runtime_agent.kwargs["prompt_templates"] == {"system_prompt": "patched"}
    finally:
        set_global_logger(previous_global_logger)


def test_create_agent_registers_before_finish_check(monkeypatch):
    _patch_agent_classes(monkeypatch)
    previous_global_logger = get_global_logger(create_if_missing=False)
    set_global_logger(DummyLoggerBackend())

    try:
        agent = _make_agent(logger=None)
        runtime_agent = agent._create_agent(
            tools=[],
            use_customized_prompt=False,
        )
        assert runtime_agent.kwargs["final_answer_checks"]
    finally:
        set_global_logger(previous_global_logger)


def test_create_agent_final_answer_checks_contains_hook_stop(monkeypatch):
    _patch_agent_classes(monkeypatch)
    previous_global_logger = get_global_logger(create_if_missing=False)
    set_global_logger(DummyLoggerBackend())

    try:
        agent = _make_agent(logger=None)

        def hook_check(*_args, **_kwargs):
            return True

        hook_run = get_current_hook_run(required=True)
        monkeypatch.setattr(hook_run, "build_stop_check", lambda: hook_check)

        runtime_agent = agent._create_agent(
            tools=[],
            use_customized_prompt=False,
        )
        checks = runtime_agent.kwargs["final_answer_checks"]
        assert len(checks) == 1
        assert checks[0]("answer", object()) is True
    finally:
        set_global_logger(previous_global_logger)


def test_cached_runtime_stop_check_resolves_current_hook_run_per_call():
    agent = _make_agent(logger=DummyLoggerBackend())
    observed: list[str] = []

    def stop(context):
        observed.append(context.local_run_id)
        return HookResult()

    shared_check = agent._build_runtime_final_answer_checks()[0]
    memory = type("Memory", (), {"steps": []})()
    for label in ("first", "second"):
        run = HookRun(
            HookPlan((HookHandler(HookEvent.STOP, "*", stop),)),
            local_run_id=label,
            root_run_id=label,
        )
        current = capture_explicit_execution_context()
        with bind_explicit_execution_context(replace(current, hook_run=run)):
            assert shared_check("answer", memory) is True

    assert observed == ["first", "second"]


def test_cached_runtime_refreshes_stateful_tool_instance_for_each_run(monkeypatch):
    from smolagents import Tool

    class StatefulTool(Tool):
        name = "stateful_cached_tool"
        description = "Retains calls on its instance."
        inputs = {"value": {"type": "integer", "description": "Value"}}
        output_type = "integer"

        def __init__(self):
            self.is_initialized = True
            self.calls: list[int] = []

        def forward(self, value: int) -> int:
            self.calls.append(value)
            return value

    agent = _make_agent(logger=DummyLoggerBackend())
    base_profile = agent._role_profile()
    monkeypatch.setattr(
        agent,
        "_role_profile",
        lambda: base_agent_module.AgentRoleProfile(
            agent_type=base_profile.agent_type,
            tool_call_type=base_profile.tool_call_type,
            cache_runtime_agent=True,
        ),
    )
    definition = StatefulTool()
    runtime = type("CachedRuntime", (), {"tools": {}})()
    agent._runtime_agent = runtime
    monkeypatch.setattr(agent, "_build_runtime_tools", lambda _profile: [definition])

    first_runtime = agent.build_runtime_agent()
    first_tool = first_runtime.tools["stateful_cached_tool"]
    first_tool.calls.append(1)
    second_runtime = agent.build_runtime_agent()
    second_tool = second_runtime.tools["stateful_cached_tool"]

    assert first_runtime is second_runtime
    assert first_tool is not second_tool
    assert first_tool.calls == [1]
    assert second_tool.calls == []
    assert definition.calls == []


@pytest.mark.parametrize("mode", ["auto", "on"])
def test_todo_enabled_modes_expose_tool_independent_of_planning_interval(
    monkeypatch,
    mode: str,
) -> None:
    agent = DummyAgent(
        config={"name": "runtime_dummy", "todo": {"mode": mode}},
        model=object(),
        logger=DummyLoggerBackend(),
    )
    sentinel = type("TodoTool", (), {"name": "todo_write"})()
    monkeypatch.setattr(agent, "get_all_tools", lambda agent_type: [])
    monkeypatch.setattr(
        base_agent_module,
        "resolve_tool_function",
        lambda name: sentinel,
    )

    tools = agent._build_runtime_tools(agent._role_profile())

    assert tools == [sentinel]
    assert "planning_interval" not in agent._config


def test_todo_off_hides_even_explicit_tool_with_planning_enabled(monkeypatch) -> None:
    agent = DummyAgent(
        config={
            "name": "runtime_dummy",
            "todo": {"mode": "off"},
            "planning_interval": 2,
        },
        model=object(),
        logger=DummyLoggerBackend(),
    )
    explicit = type("TodoTool", (), {"name": "todo_write"})()
    other = type("OtherTool", (), {"name": "read_file"})()
    monkeypatch.setattr(agent, "get_all_tools", lambda agent_type: [explicit, other])

    tools = agent._build_runtime_tools(agent._role_profile())

    assert tools == [other]


def test_base_run_emits_task_complete_on_success(monkeypatch):
    agent = _make_agent(logger=DummyLoggerBackend())
    runtime_agent = DummyRuntimeRunner(result="ok")
    events = []

    monkeypatch.setattr(agent, "build_runtime_agent", lambda: runtime_agent)

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
    assert runtime_agent.calls == [{"task": "do work", "return_full_result": True}]


def test_base_run_binds_root_before_memory_snapshot_and_only_owner_emits_session(monkeypatch):
    from src.trace import bind_root_run, get_current_session_run_id, require_root_run_id

    agent = _make_agent(logger=DummyLoggerBackend())
    runtime_agent = DummyRuntimeRunner(result="ok")
    events = []
    snapshot_roots = []

    monkeypatch.setattr(agent, "build_runtime_agent", lambda: runtime_agent)
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
    from src.extensions.self_learning.persistence.memory_store import MemoryStore
    from src.trace import bind_root_run

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
    from src.extensions.self_learning.persistence import (
        memory_store as memory_store_module,
    )
    from src.extensions.self_learning.persistence.memory_store import MemoryStore
    from src.trace import bind_root_run

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
    from src.trace import capture_explicit_execution_context

    agent = _make_agent(logger=DummyLoggerBackend())
    runtime_agent = DummyRuntimeRunner(result="ok")
    observed = []

    monkeypatch.setattr(agent, "build_runtime_agent", lambda: runtime_agent)
    monkeypatch.setattr(
        agent,
        "_inject_memory_snapshot",
        lambda tasks: observed.append(capture_explicit_execution_context()) or tasks,
    )

    assert agent.run("top-level", task_id="task-1", run_id="run-from-runner") == "ok"
    assert observed[0].root_run_id == "run-from-runner"
    assert observed[0].local_run_id == "run-from-runner"


def test_base_run_releases_owned_root_after_failure(monkeypatch):
    from src.trace import get_current_session_run_id, require_root_run_id

    agent = _make_agent(logger=DummyLoggerBackend())
    runtime_agent = DummyRuntimeRunner(exc=RuntimeError("boom-root"))
    snapshot_roots = []

    monkeypatch.setattr(agent, "build_runtime_agent", lambda: runtime_agent)
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


def test_root_memory_review_runs_after_session_end_inside_owned_root(monkeypatch):
    from src.extensions.self_learning import reviewer
    from src.trace import bind_root_run, require_root_run_id

    agent = _make_review_agent(logger=DummyLoggerBackend())
    runtime_agent = DummyRuntimeRunner(result="ok")
    order = []

    monkeypatch.setattr(agent, "build_runtime_agent", lambda: runtime_agent)
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
    from src.extensions.self_learning import reviewer

    agent = _make_review_agent(logger=DummyLoggerBackend())
    runtime_agent = DummyRuntimeRunner(result="main-result")
    session_events = []

    monkeypatch.setattr(agent, "build_runtime_agent", lambda: runtime_agent)
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
    from src.extensions.self_learning import reviewer

    agent = DummyAgent(
        config={
            "name": "runtime_dummy_disabled_learning",
            "self_learning": {"enabled": False},
        },
        model=object(),
        logger=DummyLoggerBackend(),
    )
    runtime_agent = DummyRuntimeRunner(result="main-result")
    review_calls = []

    monkeypatch.setattr(agent, "build_runtime_agent", lambda: runtime_agent)
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
    from src.extensions.self_learning import reviewer

    agent = _make_agent(logger=DummyLoggerBackend())
    runtime_agent = DummyRuntimeRunner(exc=RuntimeError("main failed"))
    session_events = []

    monkeypatch.setattr(agent, "build_runtime_agent", lambda: runtime_agent)
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
    from src.extensions.self_learning import reviewer

    class MaxStepsResult:
        state = "max_steps_error"
        output = "fallback answer"

    agent = _make_agent(logger=DummyLoggerBackend())
    runtime_agent = DummyRuntimeRunner(result=MaxStepsResult())
    task_events = []
    session_events = []

    monkeypatch.setattr(agent, "build_runtime_agent", lambda: runtime_agent)
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
    from smolagents import RunResult

    from src.lib.checkpoint import CheckpointManager
    from src.lib.checkpoint.coordinator import CheckpointCoordinator

    class MaxStepsWorkerRuntime:
        def __init__(self):
            self.logger = DummyLoggerBackend()
            self.memory = type("Memory", (), {"steps": []})()

        def run(self, task, *args, **kwargs):
            return RunResult(
                output="fallback answer",
                state="max_steps_error",
                steps=[],
                token_usage=None,
                timing=None,
            )

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
        worker.run("delegate work", return_full_result=True)

    tree = checkpoint_manager.load_task_tree("task-max-steps-worker")
    worker_call = tree["workers"]["max_steps_worker"][0]
    assert worker_call["status"] == "failed"
    checkpoint = checkpoint_manager.load_worker_checkpoint(
        "task-max-steps-worker",
        "max_steps_worker",
        call_index=worker_call["call_index"],
    )
    assert checkpoint["status"] == "failed"


def test_max_steps_managed_worker_fails_before_call_discards_state(
    tmp_path,
    monkeypatch,
):
    from smolagents import RunResult

    from src.lib.checkpoint import CheckpointManager
    from src.lib.checkpoint.coordinator import CheckpointCoordinator

    runtime = base_agent_module.ToolCallingAgentV2(
        tools=[],
        model=object(),
        max_steps=1,
        max_tokens=32,
        verbosity_level=0,
    )
    monkeypatch.setattr(
        base_agent_module.LoomAgentMixin,
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
    worker = base_agent_module.SubTaskTrackedAgent(
        runtime,
        "max_steps_managed_worker",
    )
    monkeypatch.setattr(
        CheckpointCoordinator,
        "current",
        staticmethod(lambda: coordinator),
    )

    with pytest.raises(RuntimeError, match="max_steps_error"):
        worker("delegate managed work")

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
    import src.lib.config.config as config_module

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
                "powerful": {"model": "openai/test-model"},
                "summary": {"model": "openai/test-summary"},
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
        model=object(),
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
    from src.extensions.self_learning import reviewer
    from src.extensions.self_learning.persistence.ledger import SelfLearningLedger

    agent = _make_review_agent(logger=DummyLoggerBackend())
    runtime_agent = DummyRuntimeRunner(result="main-result")
    recorder_committed = threading.Event()
    observations = []

    monkeypatch.setattr(agent, "build_runtime_agent", lambda: runtime_agent)
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
    from src.extensions.self_learning import reviewer
    from src.extensions.self_learning.persistence.ledger import SelfLearningLedger

    agent = _make_review_agent(logger=DummyLoggerBackend())
    runtime_agent = DummyRuntimeRunner(result="main-result")
    reviewed_roots = []

    monkeypatch.setattr(agent, "build_runtime_agent", lambda: runtime_agent)
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
    from src.extensions.self_learning import reviewer
    from src.extensions.self_learning.persistence.ledger import SelfLearningLedger
    from src.extensions.self_learning.session_recorder import SessionRecorder

    agent = _make_review_agent(logger=DummyLoggerBackend())
    runtime_agent = DummyRuntimeRunner(result="main-result")
    failed_root_ids = []
    review_calls = []

    monkeypatch.setattr(agent, "build_runtime_agent", lambda: runtime_agent)
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
    from src.extensions.self_learning import reviewer
    from src.extensions.self_learning.persistence.ledger import SelfLearningLedger
    from src.extensions.self_learning.session_recorder import (
        SessionRecorder,
    )

    agent = _make_review_agent(logger=DummyLoggerBackend())
    runtime_agent = DummyRuntimeRunner(result="main-result")
    failed_root_ids = []

    monkeypatch.setattr(agent, "build_runtime_agent", lambda: runtime_agent)
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
    from src.trace import (
        capture_explicit_execution_context,
        clear_current_task_id,
        set_current_task_id,
    )

    agent = _make_agent(logger=DummyLoggerBackend())
    barrier = threading.Barrier(2)
    runtime_observations = {}
    lifecycle_observations = []

    class _InterleavedRuntime:
        def run(self, *, task, **_kwargs):
            barrier.wait(timeout=5)
            current = capture_explicit_execution_context()
            runtime_observations[task] = {
                "task_id": current.task_id,
                "root_run_id": current.root_run_id,
                "local_run_id": current.local_run_id,
                "hook_run": current.hook_run,
                "agent_name": current.agent_name,
                "agent_config": current.agent_config,
            }
            return DummyRunResult(task)

    monkeypatch.setattr(agent, "build_runtime_agent", _InterleavedRuntime)
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

    hooked_add = inject_hooks(add)

    class _WorkerRuntime:
        logger = DummyLoggerBackend()

        def run(self, _task):
            current = capture_explicit_execution_context()
            with bind_explicit_execution_context(
                replace(
                    current,
                    hook_run=child_run,
                    local_run_id="worker",
                    root_run_id="root",
                )
            ):
                return hooked_add(a=1, b=2)

    set_current_hook_run(root_run)
    worker = base_agent_module.SubTaskTrackedAgent(_WorkerRuntime(), "worker-agent")

    assert worker.run("delegated") == 3
    assert root_events == ["SubagentStart", "SubagentStop"]
    assert child_events == ["PreToolUse", "PostToolUse"]

    class _NestedRuntime:
        logger = DummyLoggerBackend()

        def run(self, _task):
            return "nested"

    set_current_hook_run(child_run)
    grandchild = base_agent_module.SubTaskTrackedAgent(
        _NestedRuntime(),
        "grandchild-agent",
    )
    assert grandchild.run("nested delegated") == "nested"
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
        assert prebound_worker.run("prebound") == "nested"

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
    from src.trace import sub_task_context

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
    from src.trace import clear_current_sub_task_id, set_current_sub_task_id

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


def test_each_run_rebinds_message_sink_for_cached_runtime(monkeypatch):
    agent = _make_agent(logger=DummyLoggerBackend())
    runtime = DummyRuntimeRunner(result="ok")
    delivered: list[str] = []

    monkeypatch.setattr(agent, "build_runtime_agent", lambda: runtime)
    monkeypatch.setattr(agent, "_inject_memory_snapshot", lambda tasks: tasks)
    monkeypatch.setattr(
        agent,
        "_emit_hook_user_message",
        lambda _runtime, _logger, message: delivered.append(message),
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


def test_same_base_agent_serializes_real_cached_runtime_runs(monkeypatch):
    from src.trace import require_root_run_id

    agent = _make_agent(logger=DummyLoggerBackend())
    base_profile = agent._role_profile()
    monkeypatch.setattr(
        agent,
        "_role_profile",
        lambda: base_agent_module.AgentRoleProfile(
            agent_type=base_profile.agent_type,
            tool_call_type=base_profile.tool_call_type,
            cache_runtime_agent=True,
        ),
    )

    state_lock = threading.Lock()
    first_entered = threading.Event()
    second_attempted = threading.Event()
    second_entered = threading.Event()
    release_first = threading.Event()

    class _StatefulCachedRuntime:
        def __init__(self):
            self.state = {}
            self.active = 0
            self.max_active = 0

        def run(self, *, task, **_kwargs):
            with state_lock:
                self.active += 1
                self.max_active = max(self.max_active, self.active)
                self.state["task"] = task
            if task == "A":
                first_entered.set()
                assert release_first.wait(timeout=5)
            else:
                second_entered.set()
            result = self.state["task"]
            with state_lock:
                self.active -= 1
            return DummyRunResult(result)

    cached_runtime = _StatefulCachedRuntime()
    monkeypatch.setattr(agent, "_create_agent", lambda **_kwargs: cached_runtime)
    assert agent.build_runtime_agent() is cached_runtime
    assert agent.build_runtime_agent() is cached_runtime

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

    assert cached_runtime.max_active == 1
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
    runtime_agent = DummyRuntimeRunner()
    runtime_agent._result = None
    results = iter(["first-result", "second-result", "final-result"])
    build_calls = []

    def _recording_run(*, task, **kwargs):
        runtime_agent.calls.append({"task": task, **kwargs})
        return DummyRunResult(next(results))

    runtime_agent.run = _recording_run
    monkeypatch.setattr(agent, "build_runtime_agent", lambda: build_calls.append(runtime_agent) or runtime_agent)
    monkeypatch.setattr(agent, "_transform_tasks", lambda _task: ["first task", "second task", "third task"])

    result = agent.run("do work", task_id="multi-workflow")

    assert result == "final-result"
    assert build_calls == [runtime_agent]
    assert runtime_agent.calls == [
        {"task": "first task", "return_full_result": True},
        {
            "task": "second task",
            "return_full_result": True,
            "reset": False,
        },
        {
            "task": "third task",
            "return_full_result": True,
            "reset": False,
        },
    ]


def test_goal_mode_continues_after_normal_final_until_update_goal(monkeypatch):
    from src.lib.goal import get_current_goal_provider

    agent = DummyGoalAgent(
        config={
            "name": "goal-runtime",
            "description": "Finish all work.",
            "workflow": "Implement and verify.",
            "goal": {"enabled": True},
        },
        model=object(),
        logger=DummyLoggerBackend(),
    )
    runtime = DummyRuntimeRunner()

    def _run(*, task, **kwargs):
        runtime.calls.append({"task": task, **kwargs})
        if len(runtime.calls) == 2:
            get_current_goal_provider(required=True).complete("implemented; tests passed")
        return DummyRunResult(f"segment-{len(runtime.calls)}", state="success")

    runtime.run = _run
    monkeypatch.setattr(agent, "build_runtime_agent", lambda: runtime)
    monkeypatch.setattr(agent, "_inject_memory_snapshot", lambda tasks: tasks)

    result = agent.run("Add Goal mode", task_id="goal-task")

    assert result == "segment-2"
    assert len(runtime.calls) == 2
    assert runtime.calls[0]["task"] == "Add Goal mode"
    assert "Continue working toward the active Goal" in runtime.calls[1]["task"]
    assert "Implement and verify." not in runtime.calls[1]["task"]
    assert "Goal ID: goal_" in runtime.calls[1]["task"]
    assert runtime.calls[1]["reset"] is False


def test_goal_mode_treats_max_steps_as_continuation_boundary(monkeypatch):
    from src.lib.goal import get_current_goal_provider

    agent = DummyGoalAgent(
        config={
            "name": "goal-runtime",
            "description": "Finish all work.",
            "workflow": "Implement and verify.",
            "goal": True,
        },
        model=object(),
        logger=DummyLoggerBackend(),
    )
    runtime = DummyRuntimeRunner()

    def _run(*, task, **kwargs):
        runtime.calls.append({"task": task, **kwargs})
        if len(runtime.calls) == 2:
            get_current_goal_provider(required=True).complete("done")
        state = "max_steps_error" if len(runtime.calls) == 1 else "success"
        output = "segment" if len(runtime.calls) == 1 else "done"
        return DummyRunResult(output, state=state)

    runtime.run = _run
    monkeypatch.setattr(agent, "build_runtime_agent", lambda: runtime)
    monkeypatch.setattr(agent, "_inject_memory_snapshot", lambda tasks: tasks)

    assert agent.run("Add Goal mode", task_id="goal-task") == "done"
    assert len(runtime.calls) == 2


def test_goal_mode_uses_evidence_when_max_steps_final_delivery_failed(monkeypatch):
    from src.lib.goal import get_current_goal_provider

    agent = DummyGoalAgent(
        config={
            "name": "goal-runtime",
            "description": "Finish all work.",
            "workflow": "Implement and verify.",
            "goal": True,
        },
        model=object(),
        logger=DummyLoggerBackend(),
    )
    runtime = DummyRuntimeRunner()

    def _run(*, task, **kwargs):
        runtime.calls.append({"task": task, **kwargs})
        get_current_goal_provider(required=True).complete("durable evidence")
        return DummyRunResult(
            "Error in generating final LLM output: Goal is already complete",
            state="max_steps_error",
        )

    runtime.run = _run
    monkeypatch.setattr(agent, "build_runtime_agent", lambda: runtime)
    monkeypatch.setattr(agent, "_inject_memory_snapshot", lambda tasks: tasks)

    assert agent.run("Add Goal mode", task_id="goal-task") == "durable evidence"


def test_goal_mode_stops_before_next_segment_after_soft_budget_crossing(monkeypatch):
    from src.lib.goal import GoalBudgetLimitedError, get_current_goal_provider

    agent = DummyGoalAgent(
        config={
            "name": "goal-runtime",
            "description": "Finish all work.",
            "workflow": "Implement and verify.",
            "goal": {"enabled": True, "token_budget": 100},
        },
        model=object(),
        logger=DummyLoggerBackend(),
    )
    runtime = DummyRuntimeRunner()

    def _run(*, task, **kwargs):
        runtime.calls.append({"task": task, **kwargs})
        get_current_goal_provider(required=True).record_usage(
            prompt_tokens=90,
            completion_tokens=20,
        )
        return DummyRunResult("ordinary final")

    runtime.run = _run
    monkeypatch.setattr(agent, "build_runtime_agent", lambda: runtime)
    monkeypatch.setattr(agent, "_inject_memory_snapshot", lambda tasks: tasks)

    with pytest.raises(GoalBudgetLimitedError):
        agent.run("Add Goal mode", task_id="goal-task")
    assert len(runtime.calls) == 1


def test_goal_mode_resume_after_completion_commit_does_not_restart_work(
    tmp_path,
    monkeypatch,
):
    from src.lib.checkpoint import CheckpointManager
    from src.lib.goal import get_current_goal_provider

    config = {
        "name": "goal-runtime",
        "description": "Finish all work.",
        "workflow": "Implement and verify.",
        "goal": True,
    }
    manager = CheckpointManager("goal-runtime", checkpoints_root=tmp_path)
    first_agent = DummyGoalAgent(
        config=config,
        model=object(),
        logger=DummyLoggerBackend(),
    )
    first_runtime = DummyRuntimeRunner()

    def _commit_then_interrupt(*, task, **kwargs):
        first_runtime.calls.append({"task": task, **kwargs})
        get_current_goal_provider(required=True).complete("delivered; tests passed")
        raise KeyboardInterrupt("crash after completion commit")

    first_runtime.run = _commit_then_interrupt
    monkeypatch.setattr(first_agent, "build_runtime_agent", lambda: first_runtime)
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
        model=object(),
        logger=DummyLoggerBackend(),
    )
    resumed_runtime = DummyRuntimeRunner()
    monkeypatch.setattr(resumed_agent, "build_runtime_agent", lambda: resumed_runtime)
    monkeypatch.setattr(resumed_agent, "_inject_memory_snapshot", lambda tasks: tasks)

    result = resumed_agent.run(
        "Add Goal mode",
        task_id="goal-complete-crash",
        checkpoint_manager=manager,
        resume=True,
    )

    assert result == "delivered; tests passed"
    assert resumed_runtime.calls == []
    assert manager.load_goal("goal-complete-crash")["goal_id"] == persisted["goal_id"]


@pytest.mark.parametrize("goal", [None, False, {"enabled": False}])
def test_goal_tools_are_absent_when_goal_mode_is_disabled(monkeypatch, goal):
    config = {
        "name": "goal-runtime",
        "description": "Finish all work.",
        "workflow": "Implement and verify.",
        "todo": {"mode": "off"},
    }
    if goal is not None:
        config["goal"] = goal
    agent = DummyGoalAgent(
        config=config,
        model=object(),
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
            "todo": {"mode": "off"},
        },
        model=object(),
        logger=DummyLoggerBackend(),
    )
    monkeypatch.setattr(agent, "get_all_tools", lambda agent_type: [])

    names = {tool.name for tool in agent._build_runtime_tools(agent._role_profile())}

    assert names == {"get_goal", "update_goal"}


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
    runtime_agent = DummyRuntimeRunner(exc=RuntimeError("boom-run"))
    events = []

    monkeypatch.setattr(agent, "build_runtime_agent", lambda: runtime_agent)

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


def test_create_agent_injects_additional_functions_into_executor_kwargs(monkeypatch):
    _patch_agent_classes(monkeypatch)
    previous_global_logger = get_global_logger(create_if_missing=False)
    set_global_logger(DummyLoggerBackend())

    def _add(a, b):
        return a + b

    try:
        agent = _make_agent(logger=None)
        runtime_agent = agent._create_agent(
            tools=[],
            use_customized_prompt=False,
            executor_kwargs={"keep": "value"},
            additional_functions={"add": _add},
        )
        executor_kwargs = runtime_agent.kwargs["executor_kwargs"]
        assert executor_kwargs["keep"] == "value"
        assert executor_kwargs["additional_functions"]["add"] is _add
    finally:
        set_global_logger(previous_global_logger)


def test_create_agent_skips_additional_functions_for_docker_executor(monkeypatch):
    _patch_agent_classes(monkeypatch)
    previous_global_logger = get_global_logger(create_if_missing=False)
    set_global_logger(DummyLoggerBackend())

    def _add(a, b):
        return a + b

    try:
        agent = _make_agent(logger=None)
        runtime_agent = agent._create_agent(
            tools=[],
            use_customized_prompt=False,
            executor_type="docker",
            executor_kwargs={"host": "127.0.0.1"},
            additional_functions={"add": _add},
        )
        executor_kwargs = runtime_agent.kwargs["executor_kwargs"]
        assert executor_kwargs["host"] == "127.0.0.1"
        assert "additional_functions" not in executor_kwargs
    finally:
        set_global_logger(previous_global_logger)


def test_create_agent_passes_executor_type_and_kwargs(monkeypatch):
    _patch_agent_classes(monkeypatch)
    previous_global_logger = get_global_logger(create_if_missing=False)
    set_global_logger(DummyLoggerBackend())

    try:
        agent = _make_agent(logger=None)
        runtime_agent = agent._create_agent(
            tools=[],
            use_customized_prompt=False,
            executor_type="docker",
            executor_kwargs={"host": "127.0.0.1"},
        )
        assert runtime_agent.kwargs["executor_type"] == "docker"
        assert runtime_agent.kwargs["executor_kwargs"]["host"] == "127.0.0.1"
    finally:
        set_global_logger(previous_global_logger)


def test_create_agent_uses_explicit_planning_interval_only(monkeypatch):
    _patch_agent_classes(monkeypatch)
    previous_global_logger = get_global_logger(create_if_missing=False)
    set_global_logger(DummyLoggerBackend())

    try:
        agent = _make_agent(logger=None)
        runtime_agent = agent._create_agent(
            tools=[],
            use_customized_prompt=False,
            planning_interval=2,
        )
        assert runtime_agent.kwargs["planning_interval"] == 2

        runtime_agent_invalid = agent._create_agent(
            tools=[],
            use_customized_prompt=False,
            planning_interval="abc",  # type: ignore[arg-type]
        )
        assert "planning_interval" not in runtime_agent_invalid.kwargs
    finally:
        set_global_logger(previous_global_logger)


def test_create_agent_passes_max_tokens(monkeypatch):
    _patch_agent_classes(monkeypatch)
    previous_global_logger = get_global_logger(create_if_missing=False)
    set_global_logger(DummyLoggerBackend())

    try:
        agent = _make_agent(logger=None)
        runtime_agent = agent._create_agent(
            tools=[],
            use_customized_prompt=False,
            max_tokens=2048,
        )
        assert runtime_agent.kwargs["max_tokens"] == 2048
    finally:
        set_global_logger(previous_global_logger)


def test_create_agent_passes_split_context_budget(monkeypatch):
    _patch_agent_classes(monkeypatch)
    previous_global_logger = get_global_logger(create_if_missing=False)
    set_global_logger(DummyLoggerBackend())

    try:
        agent = _make_agent(logger=None)
        runtime_agent = agent._create_agent(
            tools=[],
            use_customized_prompt=False,
            context_window=128000,
            max_output_tokens=16000,
        )
        assert runtime_agent.kwargs["context_window"] == 128000
        assert runtime_agent.kwargs["max_output_tokens"] == 16000
    finally:
        set_global_logger(previous_global_logger)


def test_create_agent_keeps_wildcard_imports_for_local_executor(monkeypatch):
    _patch_agent_classes(monkeypatch)
    previous_global_logger = get_global_logger(create_if_missing=False)
    set_global_logger(DummyLoggerBackend())

    try:
        agent = _make_agent(logger=None)
        runtime_agent = agent._create_agent(
            tools=[],
            use_customized_prompt=False,
            executor_type="local",
            additional_authorized_imports=["*", "json"],
        )
        assert runtime_agent.kwargs["additional_authorized_imports"] == ["*", "json"]
    finally:
        set_global_logger(previous_global_logger)


@pytest.mark.parametrize("executor_type", ["docker", "e2b", "wasm"])
def test_create_agent_strips_wildcard_imports_for_remote_executor(monkeypatch, executor_type: str):
    _patch_agent_classes(monkeypatch)
    previous_global_logger = get_global_logger(create_if_missing=False)
    set_global_logger(DummyLoggerBackend())

    try:
        agent = _make_agent(logger=None)
        runtime_agent = agent._create_agent(
            tools=[],
            use_customized_prompt=False,
            executor_type=executor_type,
            additional_authorized_imports=["*", "numpy", "json"],
        )
        assert runtime_agent.kwargs["additional_authorized_imports"] == ["numpy", "json"]
    finally:
        set_global_logger(previous_global_logger)


def test_build_prompt_templates_uses_default_when_not_configured(monkeypatch, tmp_path):
    """When no prompt path is configured, loads smolagents built-in."""

    class _DummyConfig:
        agent_root = tmp_path

        @staticmethod
        def get(key: str, default=None):
            return default

    agent = _make_agent(logger=DummyLoggerBackend())
    agent._skill_catalog = SkillCatalog.empty()

    monkeypatch.setattr(base_agent_module, "C", _DummyConfig())
    monkeypatch.setattr(prompt_builder_module, "get_agent_environment_prompt", lambda: "")

    templates = agent._build_prompt_templates(
        runtime_logger=DummyLoggerBackend(),
        use_customized_prompt=True,
        prompt_template_path=None,
        skill_tool_enabled=False,
    )

    # Should have loaded smolagents' built-in prompt
    assert "system_prompt" in templates
    assert "planning" in templates


def test_build_prompt_templates_raises_on_invalid_explicit_prompt_path(monkeypatch, tmp_path):
    class _DummyConfig:
        agent_root = tmp_path

        @staticmethod
        def get(key: str, default=None):
            return default

    agent = _make_agent(logger=DummyLoggerBackend())

    monkeypatch.setattr(base_agent_module, "C", _DummyConfig())
    with pytest.raises(ValueError, match="prompt"):
        agent._build_prompt_templates(
            runtime_logger=DummyLoggerBackend(),
            use_customized_prompt=True,
            prompt_template_path=str(tmp_path / "missing_prompt.yaml"),
            skill_tool_enabled=False,
        )


def test_create_agent_prioritizes_explicit_prompt_over_system_default(monkeypatch, tmp_path):
    _patch_agent_classes(monkeypatch)

    explicit_prompt = tmp_path / "explicit.yaml"
    explicit_prompt.write_text("system_prompt: explicit", encoding="utf-8")
    default_prompt = tmp_path / "default.yaml"
    default_prompt.write_text("system_prompt: default", encoding="utf-8")

    class _DummyConfig:
        agent_root = tmp_path

        @staticmethod
        def get(key: str, default=None):
            if key == "prompt":
                return {"path": str(default_prompt)}
            return default

    previous_global_logger = get_global_logger(create_if_missing=False)
    set_global_logger(DummyLoggerBackend())

    monkeypatch.setattr(base_agent_module, "C", _DummyConfig())
    monkeypatch.setattr(prompt_builder_module, "get_agent_environment_prompt", lambda: "")
    try:
        agent = _make_agent(logger=None)
        runtime_agent = agent._create_agent(
            tools=[],
            use_customized_prompt=True,
            prompt_template_path=str(explicit_prompt),
        )
        system_prompt = runtime_agent.kwargs["prompt_templates"]["system_prompt"]
        assert system_prompt.startswith("explicit")
        assert "decide whether a task list" in system_prompt
    finally:
        set_global_logger(previous_global_logger)
