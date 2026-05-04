"""
Tests for YamlAgentFactory.run_agents_parallel().

Uses mocks to avoid LLM calls.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from src.lib.concurrency.rate_limiter import GlobalRateLimiterRegistry


@pytest.fixture(autouse=True)
def _reset():
    GlobalRateLimiterRegistry.reset()
    yield
    GlobalRateLimiterRegistry.reset()


class TestRunAgentsParallel:
    def _patch_factory(self):
        """Return context managers that mock create_agent_as_tool and ParallelAgentExecutor."""
        mock_tool = MagicMock(return_value="result")
        mock_tool.__name__ = "test_agent"

        return (
            patch(
                "src.lib.smolagents.agent.yaml_agent_factory.YamlAgentFactory.create_agent_as_tool",
                return_value=mock_tool,
            ),
            patch(
                "src.lib.concurrency.parallel_executor.ParallelAgentExecutor.execute_batch",
                return_value=[MagicMock(status="completed")],
            ),
            mock_tool,
        )

    def test_creates_tool_and_executes(self):
        from src.lib.smolagents.agent.yaml_agent_factory import YamlAgentFactory

        cm_create, cm_exec, mock_tool = self._patch_factory()
        with cm_create as m_create, cm_exec as m_exec:
            results = YamlAgentFactory.run_agents_parallel(
                config_path={"name": "test", "model_type": "fast"},
                tasks=[{"task_id": "a"}],
            )
            m_create.assert_called_once()
            m_exec.assert_called_once()
            assert len(results) == 1

    def test_reads_model_type_from_dict(self):
        from src.lib.smolagents.agent.yaml_agent_factory import YamlAgentFactory

        mock_tool = MagicMock()
        mock_tool.__name__ = "t"
        with patch.object(YamlAgentFactory, "create_agent_as_tool", return_value=mock_tool):
            with patch("src.lib.concurrency.parallel_executor.ParallelAgentExecutor.__init__", return_value=None) as init_mock:
                with patch("src.lib.concurrency.parallel_executor.ParallelAgentExecutor.execute_batch", return_value=[]):
                    YamlAgentFactory.run_agents_parallel(
                        config_path={"name": "test", "model_type": "summary"},
                        tasks=[],
                    )
                    # Check that model_type="summary" was passed to executor
                    init_mock.assert_called_once()
                    assert init_mock.call_args.kwargs["model_type"] == "summary"

    def test_default_model_type_powerful(self):
        from src.lib.smolagents.agent.yaml_agent_factory import YamlAgentFactory

        mock_tool = MagicMock()
        mock_tool.__name__ = "t"
        with patch.object(YamlAgentFactory, "create_agent_as_tool", return_value=mock_tool):
            with patch("src.lib.concurrency.parallel_executor.ParallelAgentExecutor.__init__", return_value=None) as init_mock:
                with patch("src.lib.concurrency.parallel_executor.ParallelAgentExecutor.execute_batch", return_value=[]):
                    YamlAgentFactory.run_agents_parallel(
                        config_path={"name": "test"},  # no model_type
                        tasks=[],
                    )
                    assert init_mock.call_args.kwargs["model_type"] == "powerful"

    def test_raises_on_empty_tools(self):
        from src.lib.smolagents.agent.yaml_agent_factory import YamlAgentFactory

        with patch.object(YamlAgentFactory, "create_agent_as_tool", return_value=None):
            with pytest.raises(RuntimeError, match="Failed to create"):
                YamlAgentFactory.run_agents_parallel(
                    config_path={"name": "test"},
                    tasks=[{"task_id": "a"}],
                )

    def test_max_workers_passed(self):
        from src.lib.smolagents.agent.yaml_agent_factory import YamlAgentFactory

        mock_tool = MagicMock()
        mock_tool.__name__ = "t"
        with patch.object(YamlAgentFactory, "create_agent_as_tool", return_value=mock_tool):
            with patch("src.lib.concurrency.parallel_executor.ParallelAgentExecutor.__init__", return_value=None) as init_mock:
                with patch("src.lib.concurrency.parallel_executor.ParallelAgentExecutor.execute_batch", return_value=[]):
                    YamlAgentFactory.run_agents_parallel(
                        config_path={"name": "test"},
                        tasks=[],
                        max_workers=7,
                    )
                    assert init_mock.call_args.kwargs["max_workers"] == 7
