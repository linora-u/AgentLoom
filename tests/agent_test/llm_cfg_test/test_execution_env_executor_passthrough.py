import pytest

import src.lib.smolagents.agent.base_agent as base_agent_module
import src.lib.smolagents.agent.yaml_agent_factory as yaml_factory_module
from src.lib.smolagents.agent.agent_validation import NormalizedExecutionConfig
from smolagents.models import ChatMessage, MessageRole

from src.trace.task_context import (
    clear_current_hook_run,
    set_current_hook_run,
)
from src.lib.smolagents.agent.loom_mixin import LoomAgentMixin
from src.lib.smolagents.hooks import HookPlan, HookRun
from src.lib.smolagents.agent.yaml_agent_factory import (
    YamlConfiguredAgent,
    YamlConfiguredSupervisorAgent,
)

_UNSET = object()


def _make_worker(config: dict) -> YamlConfiguredAgent:
    worker = object.__new__(YamlConfiguredAgent)
    worker._config = config
    worker._normalized = None
    worker._execution_normalized = None
    worker._effective_agent_config = None
    worker._logger = None
    return worker


def _make_supervisor(config: dict) -> YamlConfiguredSupervisorAgent:
    supervisor = object.__new__(YamlConfiguredSupervisorAgent)
    supervisor._config = config
    supervisor._normalized = None
    supervisor._execution_normalized = None
    supervisor._effective_agent_config = None
    supervisor._logger = None
    return supervisor


def _worker_config(
    execution_env=None,
    planning_interval=_UNSET,
    max_tokens=_UNSET,
    llm_max_tokens=_UNSET,
) -> dict:
    config = {
        "name": "worker_env_test",
        "description": "worker",
        "tools": [],
        "workflow": "wf",
    }
    if execution_env is not None:
        config["execution_env"] = execution_env
    if planning_interval is not _UNSET:
        config["planning_interval"] = planning_interval
    if max_tokens is not _UNSET:
        config["max_tokens"] = max_tokens
    if llm_max_tokens is not _UNSET:
        config["llm"] = {"max_tokens": llm_max_tokens}
    return config


def _supervisor_config(
    execution_env=None,
    prompt=None,
    planning_interval=_UNSET,
    max_tokens=_UNSET,
    llm_max_tokens=_UNSET,
) -> dict:
    config = {
        "name": "supervisor_env_test",
        "description": "supervisor",
        "tools": [],
        "workflow": "wf",
        "worker_agents": [],
    }
    if execution_env is not None:
        config["execution_env"] = execution_env
    if prompt is not None:
        config["prompt"] = prompt
    if planning_interval is not _UNSET:
        config["planning_interval"] = planning_interval
    if max_tokens is not _UNSET:
        config["max_tokens"] = max_tokens
    if llm_max_tokens is not _UNSET:
        config["llm"] = {"max_tokens": llm_max_tokens}
    return config


def _worker_config_with_prompt(prompt, execution_env=None) -> dict:
    config = _worker_config(execution_env=execution_env)
    config["prompt"] = prompt
    return config


def _build_execution_kwargs(agent):
    return agent._build_execution_agent_kwargs(agent._role_profile())


def test_worker_execution_env_defaults_to_local_and_empty_kwargs():
    worker = _make_worker(_worker_config())

    worker._validate_config()
    config = _build_execution_kwargs(worker)

    assert config["executor_type"] == "local"
    assert config["executor_kwargs"] == {}


def test_worker_execution_env_passthrough_docker_type_and_kwargs():
    worker = _make_worker(
        _worker_config(
            {
                "type": "docker",
                "executor_kwargs": {
                    "host": "127.0.0.1",
                    "image_name": "agentloom-smolagents-jupyter-kernel:local",
                    "build_new_image": False,
                },
                "config": {"ignored": True},
                "unknown_key": "ignored",
            }
        )
    )

    worker._validate_config()
    config = _build_execution_kwargs(worker)

    assert config["executor_type"] == "docker"
    assert config["executor_kwargs"] == {
        "host": "127.0.0.1",
        "image_name": "agentloom-smolagents-jupyter-kernel:local",
        "build_new_image": False,
    }


def test_supervisor_execution_env_passthrough_e2b_kwargs():
    supervisor = _make_supervisor(
        _supervisor_config(
            {
                "type": "e2b",
                "executor_kwargs": {"timeout": 300},
            }
        )
    )

    supervisor._validate_config()
    config = _build_execution_kwargs(supervisor)

    assert config["executor_type"] == "e2b"
    assert config["executor_kwargs"] == {"timeout": 300}


def test_worker_planning_interval_passthrough_from_int():
    worker = _make_worker(_worker_config(planning_interval=3))

    worker._validate_config()
    config = _build_execution_kwargs(worker)

    assert config["planning_interval"] == 3


def test_supervisor_planning_interval_passthrough_from_numeric_string():
    supervisor = _make_supervisor(_supervisor_config(planning_interval="2"))

    supervisor._validate_config()
    config = _build_execution_kwargs(supervisor)

    assert config["planning_interval"] == 2


def test_invalid_planning_interval_falls_back_to_none():
    worker = _make_worker(_worker_config(planning_interval="abc"))

    worker._validate_config()
    config = _build_execution_kwargs(worker)

    assert config["planning_interval"] is None


def test_agent_max_tokens_fields_do_not_affect_execution_normalized():
    worker = _make_worker(_worker_config(max_tokens=3000, llm_max_tokens=2600))

    worker._validate_config()
    execution_normalized = worker._ensure_execution_normalized()

    assert not hasattr(execution_normalized, "max_tokens")


def test_worker_model_config_builder_ignores_agent_max_tokens_fields():
    worker = _make_worker(_worker_config(max_tokens=3100, llm_max_tokens=2200))

    worker._validate_config()
    builder = worker._build_model_config_builder()

    assert builder is None


def test_worker_execution_builder_ignores_agent_max_tokens_and_uses_config_value(monkeypatch):
    worker = _make_worker(_worker_config(max_tokens=2200, llm_max_tokens=1800))
    fake_llm = type(
        "FakeLlmView",
        (),
        {"for_type": staticmethod(lambda _model_type: type("FakeTypeView", (), {"max_tokens": 9000})())},
    )()
    fake_config = type(
        "FakeConfigProxy",
        (),
        {"llm": fake_llm, "get": staticmethod(lambda *args, **kwargs: None), "agent_root": "."},
    )()
    monkeypatch.setattr(base_agent_module, "C", fake_config)

    worker._validate_config()
    config = _build_execution_kwargs(worker)

    assert config["max_tokens"] == 9000


class _DummyHistoryMixin(LoomAgentMixin):
    pass


class _PassthroughHistoryManager:
    def __init__(self):
        self.seen_messages = []

    def sync_from_messages(self, messages):
        self.seen_messages = list(messages)

    def get_compressed_messages(self, model_id=None, step=None):
        return list(self.seen_messages)


class _RecordingLogger:
    def __init__(self):
        self.entries = []

    def log(self, text, level=None):
        self.entries.append((str(text), level))


class _MemoryParent:
    def write_memory_to_messages(self, summary_mode: bool = False):
        return [
            ChatMessage(role=MessageRole.SYSTEM, content="base-system"),
            ChatMessage(role=MessageRole.USER, content="base-user"),
        ]


class _DummyHookedMemoryAgent(LoomAgentMixin, _MemoryParent):
    pass


def test_hooked_memory_uses_max_tokens_override():
    dummy = _DummyHistoryMixin()
    dummy._init_loom_agent(before_run_callbacks=None, max_tokens=3500)

    assert dummy._history_manager._max_tokens == 3500


def test_hooked_memory_uses_smart_summary_override():
    dummy = _DummyHistoryMixin()
    dummy._init_loom_agent(
        before_run_callbacks=None,
        max_tokens=3500,
        smart_summary=False,
    )

    assert dummy._history_manager._smart_summary is False


def test_hooked_memory_injects_pending_agent_context_and_logs_user_messages():
    dummy = _DummyHookedMemoryAgent()
    dummy._init_loom_agent(before_run_callbacks=None, max_tokens=3500)
    dummy._history_manager = _PassthroughHistoryManager()
    dummy.model = type("DummyModel", (), {"model_id": "test-model"})()
    dummy.step_number = 1
    dummy.logger = _RecordingLogger()

    hook_run = HookRun(HookPlan(), local_run_id="local", root_run_id="root")
    hook_run.queue_agent_context("phase-1 still active")
    hook_run.queue_user_message("[agent-recall-with-files] File updated.")
    set_current_hook_run(hook_run)

    try:
        messages = dummy.write_memory_to_messages(summary_mode=False)
    finally:
        clear_current_hook_run()

    assert messages[-1].role == MessageRole.SYSTEM
    assert "phase-1 still active" in str(messages[-1].content)
    assert dummy.logger.entries == [("[hook] [agent-recall-with-files] File updated.", 1)]
    assert hook_run.consume_pending_agent_context() == []
    assert hook_run.consume_pending_user_messages() == []


def test_worker_execution_builder_uses_effective_smart_summary_override(monkeypatch):
    worker = _make_worker(_worker_config())
    worker._effective_agent_config = {"smart_summary": False}
    fake_llm = type(
        "FakeLlmView",
        (),
        {"for_type": staticmethod(lambda _model_type: type("FakeTypeView", (), {"max_tokens": 9000})())},
    )()
    fake_config = type(
        "FakeConfigProxy",
        (),
        {"llm": fake_llm, "get": staticmethod(lambda *args, **kwargs: None), "agent_root": "."},
    )()
    monkeypatch.setattr(base_agent_module, "C", fake_config)

    worker._validate_config()
    config = _build_execution_kwargs(worker)

    assert config["smart_summary"] is False


def test_worker_prompt_path_passthrough_from_mapping(monkeypatch, tmp_path):
    prompt_file = tmp_path / "prompts" / "worker_prompt.yaml"
    prompt_file.parent.mkdir(parents=True, exist_ok=True)
    prompt_file.write_text("system_prompt: worker", encoding="utf-8")
    monkeypatch.setattr(yaml_factory_module, "AGENT_ROOT", tmp_path)
    monkeypatch.setattr(base_agent_module, "C", type("ConfigProxy", (), {"agent_root": tmp_path, "get": staticmethod(lambda *args, **kwargs: None), "llm": type("L", (), {"for_type": staticmethod(lambda _model_type: type("V", (), {"max_tokens": 1000})())})()})())

    worker = _make_worker(_worker_config_with_prompt({"path": "prompts/worker_prompt.yaml"}))
    worker._validate_config()
    config = _build_execution_kwargs(worker)

    assert config["prompt_template_path"] == str(prompt_file.resolve())


def test_supervisor_prompt_path_passthrough_from_string(monkeypatch, tmp_path):
    prompt_file = tmp_path / "prompts" / "supervisor_prompt.yaml"
    prompt_file.parent.mkdir(parents=True, exist_ok=True)
    prompt_file.write_text("system_prompt: supervisor", encoding="utf-8")
    monkeypatch.setattr(yaml_factory_module, "AGENT_ROOT", tmp_path)
    monkeypatch.setattr(base_agent_module, "C", type("ConfigProxy", (), {"agent_root": tmp_path, "get": staticmethod(lambda *args, **kwargs: None), "llm": type("L", (), {"for_type": staticmethod(lambda _model_type: type("V", (), {"max_tokens": 1000})())})()})())

    supervisor = _make_supervisor(_supervisor_config(prompt="prompts/supervisor_prompt.yaml"))
    supervisor._validate_config()
    config = _build_execution_kwargs(supervisor)

    assert config["prompt_template_path"] == str(prompt_file.resolve())


def test_build_execution_config_builder_autonormalizes_when_validate_not_called(monkeypatch, tmp_path):
    worker_prompt = tmp_path / "prompts" / "worker_prompt.yaml"
    worker_prompt.parent.mkdir(parents=True, exist_ok=True)
    worker_prompt.write_text("system_prompt: worker", encoding="utf-8")
    monkeypatch.setattr(yaml_factory_module, "AGENT_ROOT", tmp_path)
    monkeypatch.setattr(base_agent_module, "C", type("ConfigProxy", (), {"agent_root": tmp_path, "get": staticmethod(lambda *args, **kwargs: None), "llm": type("L", (), {"for_type": staticmethod(lambda _model_type: type("V", (), {"max_tokens": 1000})())})()})())

    worker = _make_worker(
        _worker_config_with_prompt(
            "prompts/worker_prompt.yaml",
            execution_env={"type": "docker", "executor_kwargs": {"host": "127.0.0.1"}},
        )
    )
    assert worker._normalized is None
    worker_config = _build_execution_kwargs(worker)
    assert worker_config["executor_type"] == "docker"
    assert worker_config["executor_kwargs"] == {"host": "127.0.0.1"}
    assert worker_config["prompt_template_path"] == str(worker_prompt.resolve())
    assert worker._normalized is not None

    supervisor_prompt = tmp_path / "prompts" / "supervisor_prompt.yaml"
    supervisor_prompt.write_text("system_prompt: supervisor", encoding="utf-8")
    supervisor = _make_supervisor(
        _supervisor_config(
            execution_env={"type": "e2b", "executor_kwargs": {"timeout": 120}},
            prompt="prompts/supervisor_prompt.yaml",
        )
    )
    assert supervisor._normalized is None
    supervisor_config = _build_execution_kwargs(supervisor)
    assert supervisor_config["executor_type"] == "e2b"
    assert supervisor_config["executor_kwargs"] == {"timeout": 120}
    assert supervisor_config["prompt_template_path"] == str(supervisor_prompt.resolve())
    assert supervisor._normalized is not None


@pytest.mark.parametrize("maker,config_builder", [
    (_make_worker, _worker_config),
    (_make_supervisor, _supervisor_config),
])
def test_execution_env_rejects_host_type(maker, config_builder):
    agent = maker(config_builder({"type": "host"}))

    with pytest.raises(ValueError, match="must be one of"):
        agent._validate_config()


@pytest.mark.parametrize("maker,config_builder", [
    (_make_worker, _worker_config),
    (_make_supervisor, _supervisor_config),
])
def test_execution_env_rejects_non_dict_executor_kwargs(maker, config_builder):
    agent = maker(config_builder({"type": "local", "executor_kwargs": ["bad"]}))

    with pytest.raises(ValueError, match="executor_kwargs"):
        agent._validate_config()


@pytest.mark.parametrize("maker,config_builder", [
    (_make_worker, _worker_config),
    (_make_supervisor, _supervisor_config),
])
def test_execution_env_rejects_non_string_bash_path(maker, config_builder):
    # bash_path is silently ignored — no validation error expected
    agent = maker(config_builder({"type": "local", "bash_path": 123}))
    agent._validate_config()  # should not raise


@pytest.mark.parametrize(
    "maker,config",
    [
        (_make_worker, _worker_config_with_prompt(prompt=["bad"])),
        (_make_supervisor, _supervisor_config(prompt=["bad"])),
        (_make_worker, _worker_config_with_prompt(prompt={"name": "missing_path"})),
        (_make_supervisor, _supervisor_config(prompt={"name": "missing_path"})),
    ],
)
def test_prompt_config_rejects_invalid_shape(maker, config):
    agent = maker(config)

    with pytest.raises(ValueError, match="prompt"):
        agent._validate_config()


def test_build_execution_kwargs_rejects_non_dict_normalized_execution_env():
    worker = _make_worker(_worker_config())
    worker._execution_normalized = "bad"

    with pytest.raises(ValueError, match="execution normalized config must be NormalizedExecutionConfig"):
        _build_execution_kwargs(worker)


def test_build_execution_kwargs_rejects_non_dict_normalized_executor_kwargs():
    worker = _make_worker(_worker_config())
    worker._execution_normalized = NormalizedExecutionConfig(
        executor_type="local",
        executor_kwargs=["bad"],  # type: ignore[arg-type]
        prompt_template_path=None,
    )

    with pytest.raises(ValueError, match="execution normalized executor_kwargs must be a dictionary"):
        _build_execution_kwargs(worker)
