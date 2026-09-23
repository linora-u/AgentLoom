"""
Tests for tool.batch() automatic parallel execution (Phase 3.5b).

Validates that the .batch() method on agent-as-tool functions correctly
reads YAML concurrency, respects priority chain, and delegates to
ParallelAgentExecutor.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from agentloom.execution.concurrency.models import TaskResult

# ─── Helpers ────────────────────────────────────────────────────── #

def _make_config(concurrency=None):
    """Build a minimal YAML config dict for testing."""
    config = {
        "name": "batch_worker",
        "description": "Batch test worker",
        "workflow": "Process the query.",
        "model_type": "powerful",
        "agent_runtime": "smolagents",
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "Input query",
                },
            },
            "required": ["query"],
            "additionalProperties": False,
        },
    }
    if concurrency is not None:
        config["concurrency"] = concurrency
    return config


def _create_tool(config):
    """Create a tool function with .batch() from config using a mock agent."""

    class SimpleAgent:
        REQUIRED_CONFIG_FIELDS = ["name", "description", "workflow"]

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

        def run(self, q, additional_args=None):
            return "mock_result"

        def agent_as_tool(self):
            from agentloom.app.factory import YamlConfiguredAgent
            return YamlConfiguredAgent.__dict__['agent_as_tool'](self)

    agent = SimpleAgent(config, model_binding=MagicMock())
    tool = agent.agent_as_tool()
    assert tool is not None
    return tool


# ─── Tests ──────────────────────────────────────────────────────── #

class TestToolBatch:
    """Verify tool.batch() reads YAML concurrency and executes in parallel."""

    def test_batch_auto_concurrency_from_yaml(self):
        """YAML concurrency: auto → executor gets max_workers=None (auto)."""
        tool = _create_tool(_make_config(concurrency="auto"))

        with patch("agentloom.execution.concurrency.ParallelAgentExecutor") as MockExecutor:
            mock_instance = MockExecutor.return_value
            mock_instance.execute_batch.return_value = [
                TaskResult(task_id="q1", status="completed", result="r1"),
            ]

            results = tool.batch([{"query": "q1"}])

            # "auto" should be normalized to None (executor auto-calculates)
            MockExecutor.assert_called_once_with(
                max_workers=None,
                model_type="powerful",
            )
            assert len(results) == 1
            assert results[0].status == "completed"

    def test_batch_explicit_concurrency_overrides_yaml(self):
        """tool.batch(tasks, concurrency=3) should override YAML concurrency: 6."""
        tool = _create_tool(_make_config(concurrency=6))

        with patch("agentloom.execution.concurrency.ParallelAgentExecutor") as MockExecutor:
            mock_instance = MockExecutor.return_value
            mock_instance.execute_batch.return_value = []

            tool.batch([{"query": "q1"}], concurrency=3)

            MockExecutor.assert_called_once_with(
                max_workers=3,
                model_type="powerful",
            )

    def test_batch_no_yaml_concurrency_defaults_auto(self):
        """No concurrency in YAML → executor gets max_workers=None (auto)."""
        tool = _create_tool(_make_config())  # no concurrency field

        with patch("agentloom.execution.concurrency.ParallelAgentExecutor") as MockExecutor:
            mock_instance = MockExecutor.return_value
            mock_instance.execute_batch.return_value = []

            tool.batch([{"query": "q1"}])

            MockExecutor.assert_called_once_with(
                max_workers=None,
                model_type="powerful",
            )

    def test_batch_returns_task_results(self):
        """batch() should return list[TaskResult]."""
        tool = _create_tool(_make_config(concurrency=2))

        expected = [
            TaskResult(task_id="q1", status="completed", result="r1"),
            TaskResult(task_id="q2", status="failed", error="boom"),
        ]

        with patch("agentloom.execution.concurrency.ParallelAgentExecutor") as MockExecutor:
            mock_instance = MockExecutor.return_value
            mock_instance.execute_batch.return_value = expected

            results = tool.batch([{"query": "q1"}, {"query": "q2"}])

            assert results == expected
            assert results[0].status == "completed"
            assert results[1].status == "failed"

    def test_batch_on_progress_callback(self):
        """on_progress callback should be forwarded to executor."""
        tool = _create_tool(_make_config(concurrency=2))
        progress_calls = []

        def on_progress(done, total, result):
            progress_calls.append((done, total, result.task_id))

        with patch("agentloom.execution.concurrency.ParallelAgentExecutor") as MockExecutor:
            mock_instance = MockExecutor.return_value
            mock_instance.execute_batch.return_value = []

            tool.batch([{"query": "q1"}], on_progress=on_progress)

            # Verify on_progress was passed through
            call_args = mock_instance.execute_batch.call_args
            assert call_args[0][1] is tool  # agent_tool
            assert call_args[1].get("on_progress") is on_progress or call_args[0][2] is on_progress

    def test_batch_empty_tasks_returns_empty(self):
        """batch([]) should return empty list."""
        tool = _create_tool(_make_config(concurrency=2))

        with patch("agentloom.execution.concurrency.ParallelAgentExecutor") as MockExecutor:
            mock_instance = MockExecutor.return_value
            mock_instance.execute_batch.return_value = []

            results = tool.batch([])
            assert results == []


class TestToolMetadata:
    """Verify tool function has correct metadata attributes."""

    def test_agent_loom_concurrency_attribute(self):
        tool = _create_tool(_make_config(concurrency=6))
        assert tool._agent_loom_concurrency == 6

    def test_agent_loom_concurrency_auto(self):
        tool = _create_tool(_make_config(concurrency="auto"))
        assert tool._agent_loom_concurrency == "auto"

    def test_agent_loom_concurrency_none(self):
        tool = _create_tool(_make_config())
        assert tool._agent_loom_concurrency is None

    def test_agent_loom_model_type_attribute(self):
        tool = _create_tool(_make_config())
        assert tool._agent_loom_model_type == "powerful"

    def test_batch_method_exists(self):
        tool = _create_tool(_make_config())
        assert hasattr(tool, "batch")
        assert callable(tool.batch)
