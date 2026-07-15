from __future__ import annotations

import threading
import time
from concurrent.futures import ThreadPoolExecutor

import pytest

import src.lib.smolagents.agent.base_agent as base_agent_module
import src.lib.smolagents.prompts.prompt_builder as prompt_builder_module
from src.lib.logging import get_global_logger, set_global_logger
from src.lib.smolagents.agent.loom_mixin import LoomAgentMixin
from src.lib.smolagents.hooks.types import HookEvent, HookResult


@pytest.fixture(autouse=True)
def _isolate_self_learning_state(tmp_path, monkeypatch):
    """Runtime lifecycle tests must never append to the developer's ledger."""
    monkeypatch.setenv(
        "AGENTLOOM_SELF_LEARNING_ROOT",
        str(tmp_path / ".agentloom"),
    )


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


def _make_agent(*, logger=None) -> DummyAgent:
    agent = DummyAgent(
        config={"name": "runtime_dummy"},
        model=object(),
        logger=logger,
    )
    return agent


def _make_tool_call_agent(*, logger=None) -> DummyToolCallingAgent:
    agent = DummyToolCallingAgent(
        config={"name": "runtime_dummy_tool_call"},
        model=object(),
        logger=logger,
    )
    return agent


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
    """Agent construction requires initialize_global_logger_once to be called first."""
    from src.lib.logging import initialize_global_logger_once
    import src.lib.logging.logger_manager as logger_manager

    _patch_agent_classes(monkeypatch)
    previous_global_logger = get_global_logger(create_if_missing=False)
    set_global_logger(None)
    monkeypatch.setattr(logger_manager, "_INITIALIZED", False, raising=True)
    monkeypatch.setattr(logger_manager, "_ACTIVE_LOG_FILE_PATH", None, raising=True)
    monkeypatch.setattr(logger_manager, "_PROCESS_LOG_FILE_PATH", None, raising=True)

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
        hook_check = object()

        monkeypatch.setattr(agent._hook_manager, "build_stop_check", lambda: hook_check)

        runtime_agent = agent._create_agent(
            tools=[],
            use_customized_prompt=False,
        )
        checks = runtime_agent.kwargs["final_answer_checks"]
        assert len(checks) == 1
        assert checks[0] is hook_check
    finally:
        set_global_logger(previous_global_logger)


def test_base_run_emits_task_complete_on_success(monkeypatch):
    agent = _make_agent(logger=DummyLoggerBackend())
    runtime_agent = DummyRuntimeRunner(result="ok")
    events = []

    monkeypatch.setattr(agent, "build_runtime_agent", lambda: runtime_agent)

    def _record(event, tool_name, tool_input, tool_response=None):
        events.append((event, tool_name, dict(tool_input), tool_response))
        return HookResult(success=True, decision="allow")

    monkeypatch.setattr(agent._hook_manager, "trigger_hooks", _record)

    result = agent.run("do work", task_id="task-complete")

    assert result == "ok"
    assert any(event is HookEvent.TASK_COMPLETED for event, *_ in events)
    complete_event = next(item for item in events if item[0] is HookEvent.TASK_COMPLETED)
    assert complete_event[2]["task_id"] == "task-complete"
    assert complete_event[2]["agent_name"] == agent.name
    assert runtime_agent.calls == [
        {"task": "do work", "return_full_result": True}
    ]


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

    def _record(event, tool_name, tool_input, tool_response=None):
        events.append(event)
        return HookResult(success=True, decision="allow")

    monkeypatch.setattr(agent._hook_manager, "trigger_hooks", _record)

    assert agent.run("top-level") == "ok"
    first_root = snapshot_roots[0]
    assert first_root != agent._hook_manager._session_id
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
    monkeypatch.setattr(
        agent._hook_manager,
        "trigger_hooks",
        lambda *args, **kwargs: HookResult(success=True, decision="allow"),
    )

    with pytest.raises(RuntimeError, match="boom-root"):
        agent.run("top-level-failure")

    assert len(snapshot_roots) == 1
    assert snapshot_roots[0] != agent._hook_manager._session_id
    assert get_current_session_run_id() is None


def test_root_memory_review_runs_after_session_end_inside_owned_root(monkeypatch):
    from src.extensions.self_learning import reviewer
    from src.trace import bind_root_run, require_root_run_id

    agent = _make_agent(logger=DummyLoggerBackend())
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

    agent = _make_agent(logger=DummyLoggerBackend())
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
        lambda **_kwargs: (_ for _ in ()).throw(
            AssertionError("failed runs must not be reviewed")
        ),
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
        lambda **_kwargs: (_ for _ in ()).throw(
            AssertionError("max-steps runs must not be reviewed")
        ),
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

    checkpoint_manager = CheckpointManager("supervisor", base_dir=tmp_path)
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
    assert (
        coordinator.check_worker_skip(
            "max_steps_worker",
            worker_call["input_hash"],
        )
        is None
    )


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
    checkpoint_manager = CheckpointManager("supervisor", base_dir=tmp_path)
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
    assert (
        coordinator.check_worker_skip(
            "max_steps_managed_worker",
            worker_call["input_hash"],
        )
        is None
    )


def test_builtin_session_end_has_only_the_recorder_hook():
    from src.lib.smolagents.hooks import HookManager, register_builtin_hooks

    manager = register_builtin_hooks(HookManager())
    session_end_hooks = manager.get_registered_hooks(HookEvent.SESSION_END)
    assert [hook["source"] for hook in session_end_hooks] == [
        "builtin:self_learning_recorder"
    ]
    assert session_end_hooks[0]["must_complete"] is True

    all_sources = {
        hook["source"]
        for event in HookEvent
        for hook in manager.get_registered_hooks(event)
    }
    assert "builtin:self_learning_reviewer" not in all_sources
    assert "builtin:self_learning_finalizer" not in all_sources


def test_successful_root_review_waits_for_session_end_recorder_commit(monkeypatch):
    """The public run seam must not review an incompletely finalized root."""
    from src.extensions.self_learning import reviewer
    from src.extensions.self_learning.ledger import SelfLearningLedger

    agent = _make_agent(logger=DummyLoggerBackend())
    runtime_agent = DummyRuntimeRunner(result="main-result")
    recorder_committed = threading.Event()
    observations = []

    monkeypatch.setattr(agent, "build_runtime_agent", lambda: runtime_agent)
    monkeypatch.setattr(agent, "_inject_memory_snapshot", lambda tasks: tasks)

    session_end_hook = next(
        hook
        for hook in agent._hook_manager.get_registered_hooks(HookEvent.SESSION_END)
        if hook["source"] == "builtin:self_learning_recorder"
    )
    original_recorder = session_end_hook["func"]

    def _delayed_recorder(context):
        # Stay beyond the raw Python hook timeout.  The old implementation
        # returns from trigger_hooks while this daemon thread is still asleep.
        time.sleep(0.05)
        result = original_recorder(context)
        recorder_committed.set()
        return result

    session_end_hook["func"] = _delayed_recorder
    session_end_hook["timeout"] = 0.005

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
                "hook_manager": current.hook_manager,
                "agent_name": current.agent_name,
                "agent_config": current.agent_config,
            }
            return DummyRunResult(task)

    monkeypatch.setattr(agent, "build_runtime_agent", _InterleavedRuntime)
    monkeypatch.setattr(agent, "_inject_memory_snapshot", lambda tasks: tasks)

    def _record(event, *_args, **_kwargs):
        if event in (HookEvent.SESSION_START, HookEvent.SESSION_END):
            current = capture_explicit_execution_context()
            lifecycle_observations.append(
                (
                    event,
                    current.task_id,
                    current.local_run_id,
                    current.root_run_id,
                )
            )
        return HookResult(success=True, decision="allow")

    monkeypatch.setattr(agent._hook_manager, "trigger_hooks", _record)

    # Poison only the legacy process-wide fallback. Fresh worker threads have
    # no task ContextVar and must still use their explicit task_id arguments.
    set_current_task_id("wrong-global-task")
    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = {
                label: pool.submit(agent.run, label, task_id=f"task-{label}")
                for label in ("A", "B")
            }
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
        assert observed["hook_manager"] is agent._hook_manager
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


def test_base_run_executes_transformed_tasks_sequentially_with_reset_false(
    tmp_path, monkeypatch
):
    monkeypatch.setenv(
        "AGENTLOOM_SELF_LEARNING_ROOT",
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

    def _record(event, tool_name, tool_input, tool_response=None):
        events.append((event, tool_name, dict(tool_input), tool_response))
        return HookResult(success=True, decision="allow")

    monkeypatch.setattr(agent._hook_manager, "trigger_hooks", _record)

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
    from src.lib.smolagents.skills.skills import SkillsManager as _SM
    from src.lib.smolagents.hooks.hook_manager import HookManager as _HM
    agent._skills_manager = _SM(logger=DummyLoggerBackend(), hook_manager=_HM())

    monkeypatch.setattr(base_agent_module, "C", _DummyConfig())
    monkeypatch.setattr(prompt_builder_module, "get_agent_environment_prompt", lambda: "")

    templates = agent._build_prompt_templates(
        runtime_logger=DummyLoggerBackend(),
        use_customized_prompt=True,
        prompt_template_path=None,
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

    class _NoSkills:
        def get_eager_skills_prompt(self):
            return ""

        def get_skills_prompt(self):
            return ""

    previous_global_logger = get_global_logger(create_if_missing=False)
    set_global_logger(DummyLoggerBackend())

    monkeypatch.setattr(base_agent_module, "C", _DummyConfig())
    monkeypatch.setattr(prompt_builder_module, "get_agent_environment_prompt", lambda: "")
    monkeypatch.setattr(prompt_builder_module.SkillsManager, "get_instance", lambda logger=None: _NoSkills())

    try:
        agent = _make_agent(logger=None)
        runtime_agent = agent._create_agent(
            tools=[],
            use_customized_prompt=True,
            prompt_template_path=str(explicit_prompt),
        )
        assert runtime_agent.kwargs["prompt_templates"]["system_prompt"] == "explicit"
    finally:
        set_global_logger(previous_global_logger)
