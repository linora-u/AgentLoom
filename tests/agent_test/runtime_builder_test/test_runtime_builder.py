from __future__ import annotations

from pathlib import Path

import pytest

import src.lib.smolagents.agent.base_agent as base_agent_module
import src.lib.smolagents.prompts.prompt_builder as prompt_builder_module
from src.lib.logging import get_global_logger, set_global_logger
from src.lib.smolagents.agent.loom_mixin import LoomAgentMixin
from src.lib.smolagents.hooks.types import HookEvent, HookResult


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


class DummyRuntimeRunner:
    def __init__(self, result=None, exc: Exception | None = None):
        self._result = result
        self._exc = exc
        self.calls = []

    def run(self, *, task, **kwargs):
        self.calls.append({"task": task, **kwargs})
        if self._exc is not None:
            raise self._exc
        return self._result


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
    assert runtime_agent.calls == [{"task": "do work"}]


def test_base_run_executes_transformed_tasks_sequentially_with_reset_false(monkeypatch):
    agent = _make_agent(logger=DummyLoggerBackend())
    runtime_agent = DummyRuntimeRunner()
    runtime_agent._result = None
    results = iter(["first-result", "second-result", "final-result"])
    build_calls = []

    def _recording_run(*, task, **kwargs):
        runtime_agent.calls.append({"task": task, **kwargs})
        return next(results)

    runtime_agent.run = _recording_run
    monkeypatch.setattr(agent, "build_runtime_agent", lambda: build_calls.append(runtime_agent) or runtime_agent)
    monkeypatch.setattr(agent, "_transform_tasks", lambda _task: ["first task", "second task", "third task"])

    result = agent.run("do work", task_id="multi-workflow")

    assert result == "final-result"
    assert build_calls == [runtime_agent]
    assert runtime_agent.calls == [
        {"task": "first task"},
        {"task": "second task", "reset": False},
        {"task": "third task", "reset": False},
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
    default_prompt = tmp_path / "default.yaml"
    default_prompt.write_text("system_prompt: default", encoding="utf-8")

    class _DummyConfig:
        agent_root = tmp_path

        @staticmethod
        def get(key: str, default=None):
            return default

    class _NoSkills:
        def get_force_injected_prompt(self):
            return ""

        def get_skills_prompt(self):
            return ""

    agent = _make_agent(logger=DummyLoggerBackend())
    from src.lib.smolagents.skills.skills import SkillsManager as _SM
    from src.lib.smolagents.hooks.hook_manager import HookManager as _HM
    agent._skills_manager = _SM(logger=DummyLoggerBackend(), hook_manager=_HM())

    monkeypatch.setattr(base_agent_module, "C", _DummyConfig())
    monkeypatch.setattr(prompt_builder_module, "DEFAULT_CODE_AGENT_PROMPT_PATH", default_prompt)
    monkeypatch.setattr(prompt_builder_module, "get_agent_environment_prompt", lambda: "")
    monkeypatch.setattr(prompt_builder_module.SkillsManager, "get_instance", lambda logger=None: _NoSkills())

    templates = agent._build_prompt_templates(
        runtime_logger=DummyLoggerBackend(),
        use_customized_prompt=True,
        prompt_template_path=None,
    )

    assert templates["system_prompt"] == "default"


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
        def get_force_injected_prompt(self):
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
