"""
Tests for src.runner and src.scaffold.

These tests validate the one-liner application launcher without
instantiating real LLM-backed agents.
"""

import textwrap
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import click
import pytest

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_SAMPLE_YAML = textwrap.dedent("""\
    name: "test_agent"
    description: |
      这是一个测试 agent 的描述，用于验证默认任务提取。
    model_type: "powerful"
    tool_call_type: "code_act"
    workflow: |
      # Test Workflow
      ## 概述
      这是一个测试 workflow。
    tools: []
    worker_agents: []
""")

_SAMPLE_YAML_NO_DESC = textwrap.dedent("""\
    name: "test_agent_no_desc"
    description: ""
    model_type: "powerful"
    tool_call_type: "code_act"
    workflow: |
      # Test Workflow
    tools: []
    worker_agents: []
""")

_SAMPLE_YAML_NO_NAME = textwrap.dedent("""\
    description: "A test description"
    model_type: "powerful"
    tool_call_type: "code_act"
    workflow: |
      # Test Workflow
    tools: []
    worker_agents: []
""")

_SAMPLE_YAML_NO_WORKFLOW = textwrap.dedent("""\
    name: "test_agent_no_workflow"
    description: "A test description"
    model_type: "powerful"
    tool_call_type: "code_act"
    workflow: ""
    tools: []
    worker_agents: []
""")


def _fake_c(tmp_path: Path):
    """Return a lightweight stand-in for the ``C`` config proxy."""
    return SimpleNamespace(
        agent_root=str(tmp_path),
        get_nested=lambda *keys, default=None: default,
    )


@pytest.fixture()
def fake_yaml(tmp_path: Path, monkeypatch) -> Path:
    """Create a temporary YAML config and make it resolvable."""
    # Build a path that matches the expected applications/{category}/workflows/ pattern.
    workflows_dir = tmp_path / "applications" / "test_app" / "workflows"
    workflows_dir.mkdir(parents=True)
    yaml_file = workflows_dir / "test_app_agent.yaml"
    yaml_file.write_text(_SAMPLE_YAML, encoding="utf-8")

    # Patch the C object in the modules that import it.
    fake = _fake_c(tmp_path)
    monkeypatch.setattr("src.runner.C", fake)
    monkeypatch.setattr("src.scaffold.C", fake)
    return yaml_file


@pytest.fixture()
def fake_yaml_no_desc(tmp_path: Path, monkeypatch) -> Path:
    """Create a temporary YAML config with empty description."""
    workflows_dir = tmp_path / "applications" / "test_app" / "workflows"
    workflows_dir.mkdir(parents=True)
    yaml_file = workflows_dir / "test_app_agent.yaml"
    yaml_file.write_text(_SAMPLE_YAML_NO_DESC, encoding="utf-8")

    fake = _fake_c(tmp_path)
    monkeypatch.setattr("src.runner.C", fake)
    monkeypatch.setattr("src.scaffold.C", fake)
    return yaml_file


@pytest.fixture()
def fake_yaml_no_name(tmp_path: Path, monkeypatch) -> Path:
    """Create a temporary YAML config missing the name field."""
    workflows_dir = tmp_path / "applications" / "test_app" / "workflows"
    workflows_dir.mkdir(parents=True)
    yaml_file = workflows_dir / "test_app_agent.yaml"
    yaml_file.write_text(_SAMPLE_YAML_NO_NAME, encoding="utf-8")

    fake = _fake_c(tmp_path)
    monkeypatch.setattr("src.runner.C", fake)
    return yaml_file


@pytest.fixture()
def fake_yaml_no_workflow(tmp_path: Path, monkeypatch) -> Path:
    """Create a temporary YAML config with empty workflow."""
    workflows_dir = tmp_path / "applications" / "test_app" / "workflows"
    workflows_dir.mkdir(parents=True)
    yaml_file = workflows_dir / "test_app_agent.yaml"
    yaml_file.write_text(_SAMPLE_YAML_NO_WORKFLOW, encoding="utf-8")

    fake = _fake_c(tmp_path)
    monkeypatch.setattr("src.runner.C", fake)
    return yaml_file


# ===================================================================
# runner.py tests
# ===================================================================


class TestResolveYamlPath:
    """Tests for _resolve_yaml_path."""

    def test_absolute_path(self, fake_yaml: Path):
        from src.runner import _resolve_yaml_path

        result = _resolve_yaml_path(fake_yaml)
        assert result == fake_yaml.resolve()

    def test_relative_path(self, fake_yaml: Path, monkeypatch):
        from src.runner import _resolve_yaml_path

        rel = "applications/test_app/workflows/test_app_agent.yaml"
        result = _resolve_yaml_path(rel)
        assert result == fake_yaml.resolve()

    def test_nonexistent_raises(self, monkeypatch, tmp_path: Path):
        from src.runner import _resolve_yaml_path

        monkeypatch.setattr("src.runner.C", _fake_c(tmp_path))
        with pytest.raises(FileNotFoundError, match="YAML configuration file not found"):
            _resolve_yaml_path("applications/nope/workflows/nope.yaml")


class TestRunApp:
    """Tests for run_app (agent execution is mocked)."""

    @patch("src.runner.YamlConfiguredSupervisorAgent")
    def test_uses_description_as_default_task(self, mock_cls, fake_yaml: Path):
        from src.runner import run_app

        mock_agent = MagicMock()
        mock_agent.run.return_value = "ok"
        mock_cls.return_value = mock_agent

        result = run_app(str(fake_yaml))

        # Supervisor.run() was called with the description text from YAML.
        called_task = mock_agent.run.call_args[0][0]
        assert "测试 agent 的描述" in called_task
        assert result == "ok"

    @patch("src.runner.YamlConfiguredSupervisorAgent")
    def test_relative_path(self, mock_cls, fake_yaml: Path):
        from src.runner import run_app

        mock_agent = MagicMock()
        mock_agent.run.return_value = "done"
        mock_cls.return_value = mock_agent

        result = run_app("applications/test_app/workflows/test_app_agent.yaml")
        assert result == "done"

    def test_missing_description_raises(self, fake_yaml_no_desc: Path):
        from src.runner import run_app

        with pytest.raises(ValueError, match="缺少必填字段.*description"):
            run_app(str(fake_yaml_no_desc))

    def test_missing_name_raises(self, fake_yaml_no_name: Path):
        from src.runner import run_app

        with pytest.raises(ValueError, match="缺少必填字段.*name"):
            run_app(str(fake_yaml_no_name))

    def test_missing_workflow_raises(self, fake_yaml_no_workflow: Path):
        from src.runner import run_app

        with pytest.raises(ValueError, match="缺少必填字段.*workflow"):
            run_app(str(fake_yaml_no_workflow))

    @patch("src.runner.YamlConfiguredSupervisorAgent")
    def test_agent_exception_raises_runtime_error(self, mock_cls, fake_yaml: Path):
        from src.runner import run_app

        mock_agent = MagicMock()
        mock_agent.run.side_effect = RuntimeError("boom")
        mock_cls.return_value = mock_agent

        with pytest.raises(RuntimeError, match="Agent execution failed"):
            run_app(str(fake_yaml))

    @patch("src.runner.YamlConfiguredSupervisorAgent")
    def test_none_result_returns_empty_string(self, mock_cls, fake_yaml: Path):
        from src.runner import run_app

        mock_agent = MagicMock()
        mock_agent.run.return_value = None
        mock_cls.return_value = mock_agent

        result = run_app(str(fake_yaml))
        assert result == ""


# ===================================================================
# scaffold.py tests
# ===================================================================


class TestCreateDemoScript:
    """Tests for create_demo_script."""

    def test_generates_demo(self, fake_yaml: Path, tmp_path: Path):
        from src.scaffold import create_demo_script

        generated = create_demo_script(str(fake_yaml))
        assert generated.exists()
        # 文件名从 YAML name 字段提取：test_agent -> test_agent_app.py
        assert generated.name == "test_agent_app.py"

        content = generated.read_text(encoding="utf-8")
        assert "from src.runner import run_app" in content
        assert "run_app(" in content
        assert "test_app_agent.yaml" in content

    def test_custom_output_path(self, fake_yaml: Path, tmp_path: Path):
        from src.scaffold import create_demo_script

        custom_out = tmp_path / "my_demo.py"
        generated = create_demo_script(str(fake_yaml), output_path=str(custom_out))
        assert generated == custom_out.resolve()
        assert custom_out.exists()

    def test_no_overwrite_raises_by_default(self, fake_yaml: Path, tmp_path: Path):
        from src.scaffold import create_demo_script

        # First call succeeds.
        create_demo_script(str(fake_yaml))

        # Second call (interactive=False, default) raises.
        with pytest.raises(FileExistsError, match="already exists"):
            create_demo_script(str(fake_yaml))

    @patch("src.scaffold.click")
    def test_interactive_overwrite_confirmed(self, mock_click, fake_yaml: Path, tmp_path: Path):
        from src.scaffold import create_demo_script

        mock_click.echo = MagicMock()
        mock_click.confirm = MagicMock(return_value=True)

        create_demo_script(str(fake_yaml))
        # Second call with interactive=True and user confirms.
        generated = create_demo_script(str(fake_yaml), interactive=True)
        assert generated.exists()
        mock_click.confirm.assert_called_once()

    @patch("src.scaffold.click")
    def test_interactive_overwrite_declined(self, mock_click, fake_yaml: Path, tmp_path: Path):
        from src.scaffold import create_demo_script

        mock_click.echo = MagicMock()
        mock_click.confirm = MagicMock(return_value=False)

        create_demo_script(str(fake_yaml))
        # Second call with interactive=True and user declines.
        with pytest.raises(FileExistsError, match="Overwrite cancelled by user"):
            create_demo_script(str(fake_yaml), interactive=True)

    def test_infers_category(self, fake_yaml: Path, tmp_path: Path):
        from src.scaffold import create_demo_script

        generated = create_demo_script(str(fake_yaml))
        # Output is placed inside applications/test_app/.
        assert "applications" in str(generated)
        assert "test_app" in str(generated)

    def test_generated_script_contains_agent_name(self, fake_yaml: Path):
        from src.scaffold import create_demo_script

        generated = create_demo_script(str(fake_yaml))
        content = generated.read_text(encoding="utf-8")
        assert "test_agent" in content


# ===================================================================
# __init__.py export test
# ===================================================================


def test_run_app_is_exported():
    """Ensure run_app is importable from the top-level package."""
    from src import run_app

    assert callable(run_app)
