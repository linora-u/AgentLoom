import agentloom.runtime.agent as base_agent_module
from agentloom.adapters.smolagents.loom_mixin import LoomAgentMixin
import pytest
from agentloom.adapters.smolagents.options import normalize_runtime_options
from agentloom.runtime.factory import (
    YamlConfiguredAgent,
    YamlConfiguredSupervisorAgent,
)
from agentloom.runtime.hooks import HookPlan, HookRun
from agentloom.runtime.model_binding import ModelTurnBinding
from agentloom.runtime.model_protocol import (
    MessageItem,
    ModelTurnRequest,
    ModelTurnResult,
)
from agentloom.runtime.skills.catalog import SkillCatalog
from agentloom.runtime.tool_gateway import (
    AgentLoomToolGateway,
    final_answer_binding,
)
from agentloom.runtime.trace.task_context import (
    clear_current_hook_run,
    set_current_hook_run,
)
from smolagents.models import ChatMessage, MessageRole


@pytest.fixture(autouse=True)
def isolated_project_config(tmp_path):
    from agentloom.configuration.config import LLMConfig, UnifiedConfig, bind_config

    with bind_config(UnifiedConfig({}, agent_root=tmp_path, llm_config=LLMConfig())):
        yield

_UNSET = object()


class _ModelAdapter:
    adapter_id = "openai_chat"

    def turn(self, request: ModelTurnRequest) -> ModelTurnResult:
        return ModelTurnResult(
            items=(MessageItem(role="assistant", text=request.model),)
        )


def _model_binding(
    *,
    max_tokens: int = 9000,
    context_window: int = 12000,
    max_output_tokens: int = 3000,
) -> ModelTurnBinding:
    return ModelTurnBinding(
        model_type="test",
        model_id="provider/opaque-model",
        adapter=_ModelAdapter(),
        max_tokens=max_tokens,
        context_window=context_window,
        max_output_tokens=max_output_tokens,
        input_token_limit=context_window - max_output_tokens,
    )


def _make_worker(config: dict) -> YamlConfiguredAgent:
    worker = object.__new__(YamlConfiguredAgent)
    worker._config = config
    worker._normalized = None
    worker._execution_normalized = None
    worker._effective_agent_config = dict(config)
    worker._logger = None
    worker._model_binding = _model_binding()
    worker._skill_catalog = SkillCatalog.empty()
    worker._build_tool_gateway = lambda: AgentLoomToolGateway(
        [final_answer_binding()]
    )
    return worker


def _make_supervisor(config: dict) -> YamlConfiguredSupervisorAgent:
    supervisor = object.__new__(YamlConfiguredSupervisorAgent)
    supervisor._config = config
    supervisor._normalized = None
    supervisor._execution_normalized = None
    supervisor._effective_agent_config = dict(config)
    supervisor._logger = None
    supervisor._model_binding = _model_binding()
    supervisor._skill_catalog = SkillCatalog.empty()
    supervisor._build_tool_gateway = lambda: AgentLoomToolGateway(
        [final_answer_binding()]
    )
    return supervisor


def _worker_config(
    planning_interval=_UNSET,
    max_tokens=_UNSET,
    llm_max_tokens=_UNSET,
) -> dict:
    config = {
        "name": "worker_env_test",
        "agent_runtime": "smolagents",
        "description": "worker",
        "tools": [],
        "workflow": "wf",
    }
    if planning_interval is not _UNSET:
        config.setdefault("runtime_options", {})["planning_interval"] = planning_interval
    if max_tokens is not _UNSET:
        config["max_tokens"] = max_tokens
    if llm_max_tokens is not _UNSET:
        config["llm"] = {"max_tokens": llm_max_tokens}
    return config


def _supervisor_config(
    prompt=None,
    planning_interval=_UNSET,
    max_tokens=_UNSET,
    llm_max_tokens=_UNSET,
) -> dict:
    config = {
        "name": "supervisor_env_test",
        "agent_runtime": "smolagents",
        "description": "supervisor",
        "tools": [],
        "workflow": "wf",
        "worker_agents": [],
    }
    if prompt is not None:
        config.setdefault("runtime_options", {})["prompt_template_path"] = prompt
    if planning_interval is not _UNSET:
        config.setdefault("runtime_options", {})["planning_interval"] = planning_interval
    if max_tokens is not _UNSET:
        config["max_tokens"] = max_tokens
    if llm_max_tokens is not _UNSET:
        config["llm"] = {"max_tokens": llm_max_tokens}
    return config


def _worker_config_with_prompt(prompt) -> dict:
    config = _worker_config()
    config.setdefault("runtime_options", {})["prompt_template_path"] = prompt
    return config


def _build_definition(agent, monkeypatch, root):
    monkeypatch.setattr(
        base_agent_module,
        "C",
        type("ConfigProxy", (), {"agent_root": root})(),
    )
    monkeypatch.setattr(
        base_agent_module,
        "get_agent_environment_prompt",
        lambda: "",
    )
    from agentloom.configuration.config import EffectiveAgentConfigSnapshot, ConfigLayerSnapshot
    from agentloom.runtime.hooks import HookPlan
    agent._effective_agent_config_snapshot = EffectiveAgentConfigSnapshot(
        values=agent._effective_agent_config,
        layers=(ConfigLayerSnapshot("agent", agent._effective_agent_config, root, root / "agent.yaml"),),
    )
    agent._model_selection = None
    agent._agent_id = "test-instance"
    agent._hook_plan = HookPlan()
    return agent._build_runtime_definition()


@pytest.mark.parametrize("config_builder", [_worker_config, _supervisor_config])
@pytest.mark.parametrize("interval", [None, 2, 3])
def test_planning_interval_uses_canonical_value(config_builder, interval, tmp_path):
    options, _ = normalize_runtime_options(
        config_builder(planning_interval=interval), agent_root=tmp_path,
    )
    assert options["planning_interval"] == interval


@pytest.mark.parametrize("interval", ["2", "abc", True, 0, -1])
def test_canonical_planning_interval_rejects_nonpositive_or_noninteger_values(interval, tmp_path):
    with pytest.raises(ValueError, match="planning_interval"):
        normalize_runtime_options(_worker_config(planning_interval=interval), agent_root=tmp_path)


@pytest.mark.parametrize("legacy", [
    {"planning_interval": 3}, {"planning_interval": "abc"},
    {"max_steps": 1, "smart_summary": False, "max_consecutive_parse_errors": 1},
    {"max_steps": [], "smart_summary": {}, "max_consecutive_parse_errors": "bad"},
])
def test_top_level_smol_execution_fields_are_ignored(legacy, tmp_path):
    expected, _ = normalize_runtime_options(_worker_config(), agent_root=tmp_path)
    actual, sources = normalize_runtime_options({**_worker_config(), **legacy}, agent_root=tmp_path)
    assert actual == expected
    assert all(source == "default:smolagents" for source in sources.values())


def test_worker_definition_ignores_agent_max_tokens_fields(monkeypatch, tmp_path):
    worker = _make_worker(_worker_config(max_tokens=3100, llm_max_tokens=2200))
    binding = _model_binding(
        max_tokens=9000,
        context_window=12000,
        max_output_tokens=3000,
    )
    worker._model_binding = binding

    worker._validate_config()
    definition = _build_definition(worker, monkeypatch, tmp_path)

    assert definition.model is binding
    assert definition.model.max_tokens == 9000
    assert definition.model.context_window == 12000
    assert definition.model.max_output_tokens == 3000


def test_worker_definition_budget_comes_only_from_model_binding(
    monkeypatch,
    tmp_path,
):
    worker = _make_worker(_worker_config(max_tokens=2200, llm_max_tokens=1800))
    binding = _model_binding(max_tokens=9000)
    worker._model_binding = binding

    worker._validate_config()
    definition = _build_definition(worker, monkeypatch, tmp_path)

    assert definition.model is binding
    assert definition.model.max_tokens == 9000


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


def test_hooked_memory_reserves_output_budget_from_context_window():
    dummy = _DummyHistoryMixin()
    dummy._init_loom_agent(
        before_run_callbacks=None,
        context_window=128000,
        max_output_tokens=16000,
    )

    assert dummy._history_manager._max_tokens == 112000


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
    hook_run.queue_user_message("Hook observer updated runtime state.")
    set_current_hook_run(hook_run)

    try:
        messages = dummy.write_memory_to_messages(summary_mode=False)
    finally:
        clear_current_hook_run()

    assert messages[-1].role == MessageRole.SYSTEM
    assert "phase-1 still active" in str(messages[-1].content)
    assert dummy.logger.entries == [("[hook] Hook observer updated runtime state.", 1)]
    assert hook_run.consume_pending_agent_context() == []
    assert hook_run.consume_pending_user_messages() == []


def test_worker_definition_uses_effective_smart_summary_override(
    monkeypatch,
    tmp_path,
):
    worker = _make_worker(_worker_config())
    worker._effective_agent_config = {**worker._config, "runtime_options": {"smart_summary": False}}

    worker._validate_config()
    definition = _build_definition(worker, monkeypatch, tmp_path)

    assert definition.runtime_options["smart_summary"] is False


def test_worker_prompt_path_passthrough_from_canonical_string(monkeypatch, tmp_path):
    prompt_file = tmp_path / "prompts" / "worker_prompt.yaml"
    prompt_file.parent.mkdir(parents=True, exist_ok=True)
    prompt_file.write_text("system_prompt: worker", encoding="utf-8")
    worker = _make_worker(_worker_config_with_prompt("prompts/worker_prompt.yaml"))
    definition = _build_definition(worker, monkeypatch, tmp_path)

    assert definition.runtime_options["prompt_template_path"] == str(prompt_file.resolve())


def test_supervisor_prompt_path_passthrough_from_string(monkeypatch, tmp_path):
    prompt_file = tmp_path / "prompts" / "supervisor_prompt.yaml"
    prompt_file.parent.mkdir(parents=True, exist_ok=True)
    prompt_file.write_text("system_prompt: supervisor", encoding="utf-8")
    supervisor = _make_supervisor(_supervisor_config(prompt="prompts/supervisor_prompt.yaml"))
    definition = _build_definition(supervisor, monkeypatch, tmp_path)

    assert definition.runtime_options["prompt_template_path"] == str(prompt_file.resolve())


def test_runtime_definition_autonormalizes_execution_config_without_validate(
    monkeypatch,
    tmp_path,
):
    worker_prompt = tmp_path / "prompts" / "worker_prompt.yaml"
    worker_prompt.parent.mkdir(parents=True, exist_ok=True)
    worker_prompt.write_text("system_prompt: worker", encoding="utf-8")
    worker = _make_worker(
        _worker_config_with_prompt("prompts/worker_prompt.yaml")
    )
    assert worker._normalized is None
    worker_definition = _build_definition(worker, monkeypatch, tmp_path)
    assert worker_definition.runtime_options["prompt_template_path"] == str(
        worker_prompt.resolve()
    )

    supervisor_prompt = tmp_path / "prompts" / "supervisor_prompt.yaml"
    supervisor_prompt.write_text("system_prompt: supervisor", encoding="utf-8")
    supervisor = _make_supervisor(
        _supervisor_config(
            prompt="prompts/supervisor_prompt.yaml",
        )
    )
    assert supervisor._normalized is None
    supervisor_definition = _build_definition(supervisor, monkeypatch, tmp_path)
    assert supervisor_definition.runtime_options["prompt_template_path"] == str(
        supervisor_prompt.resolve()
    )
