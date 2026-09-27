"""
Tests for agent_as_tool() factory mode (Phase 3.5a).

Validates that each call to the returned tool creates a NEW Agent instance,
ensuring thread-safe concurrent execution (no memory.steps crosstalk).
"""

from __future__ import annotations

import threading
from unittest.mock import MagicMock

# ─── Helpers ────────────────────────────────────────────────────── #

def _make_minimal_config(concurrency=None):
    """Build a minimal YAML config dict for testing."""
    config = {
        "name": "test_worker",
        "description": "Test worker agent",
        "task": "Analyze the supplied query and return a result.",
        "model_type": "powerful",
        "agent_runtime": "smolagents",
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "The query to process",
                },
            },
            "required": ["query"],
            "additionalProperties": False,
        },
    }
    if concurrency is not None:
        config["concurrency"] = concurrency
    return config


def _create_tool_with_mock_agent(config=None, agent_instances=None):
    """Create a tool from config using a mock Agent class that tracks instances.

    Returns (tool_function, agent_class_mock, config).
    """
    config = config or _make_minimal_config()
    if agent_instances is None:
        agent_instances = []

    mock_model_binding = MagicMock(name="shared_model_binding")

    class FakeAgent:
        """Lightweight fake that mimics YamlConfiguredAgent enough for agent_as_tool."""

        REQUIRED_CONFIG_FIELDS = ["name", "description", "task"]

        def __init__(self, config, model_binding=None, logger=None, **kw):
            self._config = config
            self._model_binding = model_binding
            self.logger = logger
            self.name = config["name"]
            self.description = config.get("description", "")
            self._id = id(self)
            agent_instances.append(self)

        def _ensure_normalized(self):
            from agentloom.app.validation import AgentConfigNormalizer
            return AgentConfigNormalizer.build_worker_normalized_config(
                self._config, agent_root=".", source_name="test",
            )

        def process_tool_query(self, query):
            return query

        def run(self, *, additional_args=None):
            self.task = self._config["task"]
            self.additional_args = additional_args
            return f"result_from_{self._id}"

        def agent_as_tool(self):
            from agentloom.app.factory import YamlConfiguredAgent
            # Delegate to the real agent_as_tool logic but with our class
            real = YamlConfiguredAgent.__dict__['agent_as_tool']
            return real(self)

    agent = FakeAgent(config, model_binding=mock_model_binding)
    tool = agent.agent_as_tool()
    assert tool is not None, "agent_as_tool() returned None"
    return tool, FakeAgent, config


# ─── Tests ──────────────────────────────────────────────────────── #

class TestFactoryMode:
    """Verify agent_as_tool() creates a NEW Agent per call (factory pattern)."""

    def test_factory_creates_new_agent_each_call(self):
        """Two sequential calls should create two different Agent instances."""
        instances = []
        tool, _, _ = _create_tool_with_mock_agent(agent_instances=instances)

        # First call
        tool(query="hello")
        assert len(instances) == 2  # 1 for setup + 1 for call

        # Second call
        tool(query="world")
        assert len(instances) == 3  # 1 for setup + 2 for calls

        # The call-time agents should be different objects
        call_agents = instances[1:]  # skip the setup agent
        assert call_agents[0]._id != call_agents[1]._id

    def test_shared_model_binding_same_instance(self):
        """All Agent instances should share the same model binding."""
        instances = []
        tool, _, _ = _create_tool_with_mock_agent(agent_instances=instances)

        tool(query="a")
        tool(query="b")

        call_agents = instances[1:]
        assert call_agents[0]._model_binding is call_agents[1]._model_binding

    def test_tool_inputs_remain_data_and_do_not_replace_worker_task(self):
        instances = []
        tool, _, _ = _create_tool_with_mock_agent(agent_instances=instances)

        tool(query="payload")

        call_agent = instances[1]
        assert call_agent.task == "Analyze the supplied query and return a result."
        assert call_agent.additional_args == {"query": "payload"}

    def test_concurrent_calls_no_memory_crosstalk(self):
        """Concurrent calls should each get their own Agent (no shared state)."""
        instances = []
        tool, _, _ = _create_tool_with_mock_agent(agent_instances=instances)

        results = []
        errors = []

        def call_tool(query):
            try:
                r = tool(query=query)
                results.append(r)
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=call_tool, args=(f"q{i}",)) for i in range(5)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert not errors, f"Errors during concurrent calls: {errors}"
        assert len(results) == 5
        # Each call should have created its own agent instance
        call_agents = instances[1:]  # skip setup agent
        agent_ids = [a._id for a in call_agents]
        assert len(set(agent_ids)) == 5, "Not all agents are unique instances"

    def test_concurrent_calls_no_exception_leak(self):
        """One failing call should not affect others."""
        call_count = {"n": 0}
        lock = threading.Lock()

        config = _make_minimal_config()

        class FailOnSecond:
            REQUIRED_CONFIG_FIELDS = ["name", "description", "task"]

            def __init__(self, config, **kw):
                self._config = config
                self._model_binding = kw.get("model_binding")
                self.logger = kw.get("logger")
                self.name = config["name"]
                self.description = config.get("description", "")

            def _ensure_normalized(self):
                from agentloom.app.validation import AgentConfigNormalizer
                return AgentConfigNormalizer.build_worker_normalized_config(
                    self._config, agent_root=".", source_name="test",
                )

            def process_tool_query(self, q):
                return q

            def run(self, *, additional_args=None):
                with lock:
                    call_count["n"] += 1
                    n = call_count["n"]
                if n == 2:
                    raise RuntimeError("Intentional failure on call 2")
                return f"ok_{n}"

            def agent_as_tool(self):
                from agentloom.app.factory import YamlConfiguredAgent
                return YamlConfiguredAgent.__dict__['agent_as_tool'](self)

        agent = FailOnSecond(config)
        tool = agent.agent_as_tool()
        assert tool is not None

        results = []
        errors = []

        def call_tool(query):
            try:
                results.append(tool(query=query))
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=call_tool, args=(f"q{i}",)) for i in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        # Exactly 1 error (call #2), 3 successes
        assert len(errors) == 1
        assert len(results) == 3

    def test_large_worker_result_stays_canonical(self, tmp_path):
        from agentloom.app.factory import YamlConfiguredAgent
        from agentloom.execution.context_engine import ContextEngine, ContextEngineConfig
        from agentloom.execution.context_engine.runtime import clear_current_context_engine, set_current_context_engine

        config = _make_minimal_config()

        class LargeResultAgent:
            REQUIRED_CONFIG_FIELDS = ["name", "description", "task"]

            def __init__(self, config, model_binding=None, logger=None, **kw):
                self._config = config
                self._model_binding = (
                    model_binding or MagicMock(name="model_binding")
                )
                self.logger = logger
                self.name = config["name"]
                self.description = config.get("description", "")

            def _ensure_normalized(self):
                from agentloom.app.validation import AgentConfigNormalizer

                return AgentConfigNormalizer.build_worker_normalized_config(
                    self._config, agent_root=".", source_name="test",
                )

            def run(self, *, additional_args=None):
                return "worker payload\n" * 80

            def agent_as_tool(self):
                return YamlConfiguredAgent.__dict__["agent_as_tool"](self)

        engine = ContextEngine(
            tmp_path / "context_store",
            config=ContextEngineConfig(min_chars=10, preview_max_chars=120),
        )
        set_current_context_engine(engine)
        try:
            tool = LargeResultAgent(config).agent_as_tool()
            result = tool(query="payload")

            assert result == "worker payload\n" * 80
            assert list((tmp_path / "context_store").glob("*.json")) == []
        finally:
            clear_current_context_engine(engine)

    def test_worker_context_engine_failure_does_not_change_canonical_result(self):
        from agentloom.execution.context_engine.runtime import clear_current_context_engine, set_current_context_engine

        bad_engine = MagicMock()
        bad_engine.compress_tool_result.side_effect = RuntimeError("worker context store unavailable")
        set_current_context_engine(bad_engine)
        try:
            tool, _, _ = _create_tool_with_mock_agent()
            result = tool(query="test")

            assert result.startswith("result_from_")
            bad_engine.compress_tool_result.assert_not_called()
        finally:
            clear_current_context_engine(bad_engine)

    def test_single_synchronous_call_returns_string(self):
        """Single synchronous call returns the worker result."""
        instances = []
        tool, _, _ = _create_tool_with_mock_agent(agent_instances=instances)

        result = tool(query="test")
        assert isinstance(result, str)
        assert result.startswith("result_from_")
