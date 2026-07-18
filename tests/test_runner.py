"""
Tests for src.runner and src.scaffold.

These tests validate the one-liner application launcher without
instantiating real LLM-backed agents.
"""

import textwrap
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

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
    def test_binds_canonical_run_context_before_agent_execution(self, mock_cls, fake_yaml: Path):
        from src.lib.runtime import get_current_run_context
        from src.runner import run_app

        observed = {}
        mock_agent = MagicMock()

        def _run(_task, **kwargs):
            context = get_current_run_context(required=True)
            observed["context"] = context
            observed["kwargs"] = kwargs
            observed["manifest_exists"] = context.manifest_path.exists()
            return "ok"

        mock_agent.run.side_effect = _run
        mock_cls.return_value = mock_agent

        assert run_app(str(fake_yaml), file_logging=False) == "ok"

        context = observed["context"]
        assert context.application_id == "test_app"
        assert observed["manifest_exists"] is True
        assert observed["kwargs"]["task_id"] == context.task_id
        assert observed["kwargs"]["run_id"] == context.run_id
        assert context.manifest_path.parent == context.run_dir
        assert not context.log_path.exists()
        assert context.task_tree_path.exists()
        assert not (fake_yaml.parents[3] / ".logs").exists()

    @patch("src.runner.YamlConfiguredSupervisorAgent")
    def test_run_finally_releases_shell_sessions_and_background_tasks(
        self,
        mock_cls,
        fake_yaml: Path,
    ):
        import os
        import subprocess

        from src.lib.runtime import bind_run_context, get_current_run_context
        from src.tools.shell.background_task import BackgroundTaskRegistry
        from src.tools.shell.process import ShellProcessRegistry
        from src.trace import clear_current_agent_id, set_current_agent_id
        from src.runner import run_app

        BackgroundTaskRegistry._reset_instance()
        observed = {}
        mock_agent = MagicMock()

        def _run(_task, **_kwargs):
            context = get_current_run_context(required=True)
            set_current_agent_id("runner-cleanup-agent")
            ShellProcessRegistry.get_instance().get_or_create(
                "runner-cleanup-agent",
                session_scoped=False,
            )
            context.background_artifacts_dir.mkdir(parents=True, exist_ok=True)
            output_path = context.background_artifacts_dir / "runner-cleanup.txt"
            output_fd = os.open(output_path, os.O_WRONLY | os.O_CREAT | os.O_APPEND)
            process = subprocess.Popen(
                ["sleep", "60"],
                stdout=output_fd,
                stderr=output_fd,
                stdin=subprocess.DEVNULL,
                start_new_session=True,
            )
            os.close(output_fd)
            task_id = BackgroundTaskRegistry.get_instance().register(
                process,
                "sleep 60",
                str(output_path),
            )
            observed.update(context=context, process=process, task_id=task_id)
            return "ok"

        mock_agent.run.side_effect = _run
        mock_cls.return_value = mock_agent

        try:
            assert run_app(str(fake_yaml), file_logging=False) == "ok"
            observed["process"].wait(timeout=5)

            with bind_run_context(observed["context"]):
                set_current_agent_id("runner-cleanup-agent")
                assert ShellProcessRegistry.get_instance().registered_agent_ids() == []
                assert BackgroundTaskRegistry.get_instance().list_all() == []
        finally:
            clear_current_agent_id()
            process = observed.get("process")
            if process is not None and process.poll() is None:
                process.kill()
                process.wait(timeout=2)
            BackgroundTaskRegistry._reset_instance()

    @patch("src.runner.YamlConfiguredSupervisorAgent")
    def test_automatic_run_cleanup_never_traverses_checkpoints(
        self,
        mock_cls,
        fake_yaml: Path,
        monkeypatch,
    ) -> None:
        from src.runner import run_app

        mock_cls.return_value.run.return_value = "ok"
        monkeypatch.setattr(
            "src.lib.runtime.retention.prune_runtime_if_due",
            lambda *_args, **_kwargs: SimpleNamespace(skipped=False),
        )
        monkeypatch.setattr(
            "src.lib.checkpoint.cleanup_expired_tasks",
            lambda **_kwargs: pytest.fail("run retention touched checkpoints"),
        )

        assert run_app(str(fake_yaml), file_logging=False) == "ok"

    @patch("src.runner.YamlConfiguredSupervisorAgent")
    def test_run_lease_covers_manifest_and_logging_configuration(
        self,
        mock_cls,
        fake_yaml: Path,
        monkeypatch,
    ) -> None:
        from src.lib.logging import LoggingConfigBuilder
        from src.lib.runtime.retention import RetentionPolicy, clean_runtime
        from src.runner import run_app

        mock_cls.return_value.run.return_value = "ok"
        original_apply = LoggingConfigBuilder.apply_mapping
        observed = {}

        def apply_while_cleaning(builder, *args, **kwargs):
            runtime_root = fake_yaml.parents[3] / ".agentloom"
            result = clean_runtime(
                runtime_root,
                policy=RetentionPolicy(
                    successful_runs=timedelta(0),
                    failed_runs=timedelta(0),
                    raw_artifacts=timedelta(0),
                ),
                now=datetime.now(UTC) + timedelta(seconds=1),
            )
            observed["removed"] = result.removed_run_count
            return original_apply(builder, *args, **kwargs)

        monkeypatch.setattr(LoggingConfigBuilder, "apply_mapping", apply_while_cleaning)

        assert run_app(str(fake_yaml), file_logging=False) == "ok"
        assert observed["removed"] == 0

    def test_concurrent_applications_and_same_application_tasks_keep_logs_isolated(
        self,
        tmp_path: Path,
        monkeypatch,
    ) -> None:
        from src.lib.logging import get_logger
        from src.lib.runtime import get_current_run_context
        from src.runner import run_app

        workflows = []
        for application in ("alpha", "beta"):
            workflow_dir = tmp_path / "applications" / application / "workflows"
            workflow_dir.mkdir(parents=True)
            workflow = workflow_dir / "agent.yaml"
            workflow.write_text(
                _SAMPLE_YAML.replace('name: "test_agent"', f'name: "{application}_agent"'),
                encoding="utf-8",
            )
            workflows.extend([workflow, workflow])

        monkeypatch.setattr("src.runner.C", _fake_c(tmp_path))
        barrier = threading.Barrier(len(workflows))
        contexts = []
        contexts_lock = threading.Lock()
        lazy_logger = get_logger("tests.concurrent_runner")

        class FakeSupervisor:
            def __init__(self, config, logger):
                self.config = config

            def run(self, _task, **_kwargs):
                context = get_current_run_context(required=True)
                marker = f"only-{context.task_id}"
                barrier.wait(timeout=10)
                lazy_logger.info(marker)
                with contexts_lock:
                    contexts.append((context, marker))
                return marker

        monkeypatch.setattr("src.runner.YamlConfiguredSupervisorAgent", FakeSupervisor)
        with ThreadPoolExecutor(max_workers=len(workflows)) as executor:
            results = list(executor.map(lambda path: run_app(path), workflows))

        assert len(results) == len(workflows)
        assert len({context.task_id for context, _marker in contexts}) == len(workflows)
        assert len({context.run_id for context, _marker in contexts}) == len(workflows)
        assert {context.application_id for context, _marker in contexts} == {"alpha", "beta"}
        for context, marker in contexts:
            log_text = context.log_path.read_text(encoding="utf-8")
            assert marker in log_text
            assert all(
                other_marker not in log_text
                for _other_context, other_marker in contexts
                if other_marker != marker
            )

    @patch("src.runner.YamlConfiguredSupervisorAgent")
    def test_resume_creates_new_run_but_reuses_task_checkpoint(self, mock_cls, fake_yaml: Path):
        from src.lib.runtime import get_current_run_context
        from src.runner import run_app

        attempts = []
        mock_agent = MagicMock()

        def _run(_task, **kwargs):
            attempts.append((get_current_run_context(required=True), kwargs))
            return "ok"

        mock_agent.run.side_effect = _run
        mock_cls.return_value = mock_agent

        run_app(str(fake_yaml), file_logging=False)
        first_context, first_kwargs = attempts[0]
        run_app(
            str(fake_yaml),
            resume_task_id=first_context.task_id,
            file_logging=False,
        )
        second_context, second_kwargs = attempts[1]

        assert second_context.task_id == first_context.task_id
        assert second_context.run_id != first_context.run_id
        assert second_context.run_dir != first_context.run_dir
        assert second_context.checkpoint_dir == first_context.checkpoint_dir
        assert first_kwargs["resume"] is False
        assert second_kwargs["resume"] is True

    @pytest.mark.parametrize(
        ("created_at", "error"),
        [
            ("not-a-timestamp", "invalid created_at"),
            ("", "invalid created_at"),
            (
                (datetime.now(UTC) - timedelta(days=30))
                .replace(tzinfo=None)
                .isoformat(),
                "expired",
            ),
        ],
    )
    @patch("src.runner.YamlConfiguredSupervisorAgent")
    def test_resume_rejects_invalid_or_expired_naive_created_at(
        self,
        mock_cls,
        fake_yaml: Path,
        created_at: str,
        error: str,
    ) -> None:
        from src.lib.checkpoint import CheckpointManager
        from src.lib.runtime import RuntimeHome
        from src.runner import run_app

        context = RuntimeHome(fake_yaml.parents[3] / ".agentloom").context(
            application_id="test_app",
            task_id="invalid_created_at",
            run_id="old_run",
        )
        manager = CheckpointManager(
            "supervisor",
            checkpoint_dir=context.checkpoint_dir,
            run_id="old_run",
        )
        manager.save_task_tree(
            context.task_id,
            {
                "task_id": context.task_id,
                "status": "interrupted",
                "created_at": created_at,
                "workers": {},
            },
        )
        manager.close()

        with pytest.raises(FileNotFoundError, match=error):
            run_app(
                str(fake_yaml),
                resume_task_id=context.task_id,
                file_logging=False,
            )

        mock_cls.assert_not_called()

    @patch("src.runner.YamlConfiguredSupervisorAgent")
    def test_resume_rejects_completed_checkpoint(
        self,
        mock_cls,
        fake_yaml: Path,
    ) -> None:
        from src.lib.checkpoint import CheckpointManager
        from src.lib.runtime import RuntimeHome
        from src.runner import run_app

        context = RuntimeHome(fake_yaml.parents[3] / ".agentloom").context(
            application_id="test_app",
            task_id="completed_task",
            run_id="old_run",
        )
        manager = CheckpointManager(
            "supervisor",
            checkpoint_dir=context.checkpoint_dir,
            run_id="old_run",
        )
        manager.save_task_tree(
            context.task_id,
            {
                "task_id": context.task_id,
                "status": "completed",
                "created_at": datetime.now(UTC).isoformat(),
                "workers": {},
            },
        )
        manager.close()

        with pytest.raises(ValueError, match="not resumable.*completed"):
            run_app(
                str(fake_yaml),
                resume_task_id=context.task_id,
                file_logging=False,
            )

        mock_cls.assert_not_called()

    @patch("src.runner.YamlConfiguredSupervisorAgent")
    def test_concurrent_resume_of_same_task_is_rejected(
        self,
        mock_cls,
        fake_yaml: Path,
    ) -> None:
        from src.runner import run_app

        mock_agent = MagicMock()
        mock_agent.run.return_value = "initial"
        mock_cls.return_value = mock_agent
        run_app(str(fake_yaml), file_logging=False)
        checkpoint_root = fake_yaml.parents[3] / ".agentloom" / "checkpoints" / "test_app"
        logical_task_id = next(checkpoint_root.iterdir()).name

        entered = threading.Event()
        release = threading.Event()

        def blocking_resume(_task, **_kwargs):
            entered.set()
            assert release.wait(timeout=10)
            return "resumed"

        mock_agent.run.side_effect = blocking_resume
        with ThreadPoolExecutor(max_workers=1) as executor:
            first_resume = executor.submit(
                run_app,
                str(fake_yaml),
                resume_task_id=logical_task_id,
                file_logging=False,
            )
            assert entered.wait(timeout=10)
            try:
                with pytest.raises(RuntimeError, match="already active"):
                    run_app(
                        str(fake_yaml),
                        resume_task_id=logical_task_id,
                        file_logging=False,
                    )
            finally:
                release.set()
            assert first_resume.result(timeout=10) == "resumed"

    @patch("src.runner.YamlConfiguredSupervisorAgent")
    def test_automatic_run_cleanup_preserves_expired_checkpoints(
        self,
        mock_cls,
        fake_yaml: Path,
    ) -> None:
        from src.lib.checkpoint import CheckpointManager
        from src.lib.runtime import RuntimeHome
        from src.runner import run_app

        home = RuntimeHome(fake_yaml.parents[3] / ".agentloom")

        def make_expired(task_id: str):
            context = home.context(
                application_id="test_app",
                task_id=task_id,
                run_id=f"run_{task_id}",
            )
            manager = CheckpointManager("supervisor", checkpoint_dir=context.checkpoint_dir)
            manager.save_task_tree(
                context.task_id,
                {
                    "task_id": context.task_id,
                    "status": "failed",
                    "created_at": (
                        datetime.now(UTC) - timedelta(days=8)
                    ).isoformat(),
                    "workers": {},
                },
            )
            return context

        mock_cls.return_value.run.return_value = "ok"
        first_expired = make_expired("expired_first")

        run_app(str(fake_yaml), file_logging=False)

        assert first_expired.checkpoint_dir.exists()

    @patch("src.runner.YamlConfiguredSupervisorAgent")
    def test_resume_rejects_symlinked_application_checkpoint_directory(
        self,
        mock_cls,
        fake_yaml: Path,
    ) -> None:
        from src.lib.checkpoint import CheckpointManager
        from src.runner import run_app

        runtime_root = fake_yaml.parents[3] / ".agentloom"
        external_app = fake_yaml.parents[3] / "external-checkpoints"
        manager = CheckpointManager(
            "supervisor",
            checkpoint_dir=external_app / "outside_task",
            run_id="old_run",
        )
        manager.save_task_tree(
            "outside_task",
            {
                "task_id": "outside_task",
                "status": "interrupted",
                "created_at": datetime.now(UTC).isoformat(),
                "workers": {},
            },
        )
        checkpoints = runtime_root / "checkpoints"
        checkpoints.mkdir(parents=True, exist_ok=True)
        (checkpoints / "test_app").symlink_to(
            external_app,
            target_is_directory=True,
        )

        with pytest.raises(RuntimeError, match="symlink"):
            run_app(
                str(fake_yaml),
                resume_task_id="outside_task",
                file_logging=False,
            )

        assert (external_app / "outside_task" / "task_tree.json").exists()
        mock_cls.assert_not_called()

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


def test_structured_run_api_is_publicly_exported():
    from src import (
        ApplicationRunError,
        ApplicationRunInterrupted,
        ApplicationRunResult,
        RunInfo,
        RunPhase,
        RunRejectedEvent,
        RunRejection,
        execute_app,
    )

    assert callable(execute_app)
    assert all(
        value is not None
        for value in (
            ApplicationRunError,
            ApplicationRunInterrupted,
            ApplicationRunResult,
            RunInfo,
            RunPhase,
            RunRejectedEvent,
            RunRejection,
        )
    )


class TestExecuteApp:
    @patch("src.runner.YamlConfiguredSupervisorAgent")
    def test_returns_canonical_receipt_after_durable_finalization(
        self,
        mock_cls,
        fake_yaml: Path,
    ) -> None:
        import json

        from src.runner import execute_app

        mock_cls.return_value.run.return_value = "structured-output"

        result = execute_app(str(fake_yaml), file_logging=False)

        assert result.output == "structured-output"
        assert result.started_at <= result.ended_at
        assert result.run.application_id == "test_app"
        assert result.run.run_dir.is_absolute()
        assert result.run.manifest_path == result.run.run_dir / "manifest.json"
        assert result.run.log_path is None
        manifest = json.loads(result.run.manifest_path.read_text(encoding="utf-8"))
        assert manifest["status"] == "completed"
        assert manifest["application_id"] == result.run.application_id
        assert manifest["task_id"] == result.run.task_id
        assert manifest["run_id"] == result.run.run_id
        assert "result_artifact" not in manifest
        assert not (result.run.run_dir / "artifacts" / "result.txt").exists()

    @patch("src.runner.YamlConfiguredSupervisorAgent")
    def test_emits_started_then_completed_and_ignores_sink_errors(
        self,
        mock_cls,
        fake_yaml: Path,
    ) -> None:
        from src.runner import execute_app

        mock_cls.return_value.run.return_value = "ok"
        observed = []

        def sink(event):
            observed.append(event)
            raise RuntimeError("observer failed")

        result = execute_app(
            str(fake_yaml),
            file_logging=False,
            event_sink=sink,
        )

        assert result.output == "ok"
        assert [event.event for event in observed] == [
            "run.started",
            "run.completed",
        ]
        assert observed[0].run == observed[1].run == result.run
        assert observed[1].output == "ok"

    @patch("src.runner.YamlConfiguredSupervisorAgent")
    def test_failure_carries_run_info_and_terminal_event(
        self,
        mock_cls,
        fake_yaml: Path,
    ) -> None:
        import json

        from src.application_run import ApplicationRunError
        from src.runner import execute_app

        mock_cls.return_value.run.side_effect = RuntimeError("boom")
        events = []

        with pytest.raises(ApplicationRunError, match="Agent execution failed") as caught:
            execute_app(
                str(fake_yaml),
                file_logging=False,
                event_sink=events.append,
            )

        assert caught.value.phase == "execution"
        assert [event.event for event in events] == ["run.started", "run.failed"]
        assert events[0].run == events[1].run == caught.value.run
        manifest = json.loads(
            caught.value.run.manifest_path.read_text(encoding="utf-8")
        )
        assert manifest["status"] == "failed"

    @patch("src.runner.YamlConfiguredSupervisorAgent")
    def test_interruption_carries_run_info(
        self,
        mock_cls,
        fake_yaml: Path,
    ) -> None:
        from src.application_run import ApplicationRunInterrupted
        from src.runner import execute_app

        mock_cls.return_value.run.side_effect = KeyboardInterrupt()
        events = []

        with pytest.raises(ApplicationRunInterrupted) as caught:
            execute_app(
                str(fake_yaml),
                file_logging=False,
                event_sink=events.append,
            )

        assert caught.value.phase == "execution"
        assert [event.event for event in events] == [
            "run.started",
            "run.interrupted",
        ]
        assert events[-1].run == caught.value.run

    def test_preflight_failure_emits_rejected_without_allocating(
        self,
        fake_yaml_no_desc: Path,
    ) -> None:
        from src.application_run import RunRejectedEvent
        from src.runner import execute_app

        events = []
        with pytest.raises(ValueError, match="缺少必填字段.*description"):
            execute_app(str(fake_yaml_no_desc), event_sink=events.append)

        assert len(events) == 1
        assert isinstance(events[0], RunRejectedEvent)
        assert events[0].event == "run.rejected"
        assert events[0].phase == "preflight"
        assert events[0].error.kind == "ValueError"
        assert "description" in events[0].error.message
        assert events[0].error.retryable is False
        assert not (fake_yaml_no_desc.parents[3] / ".agentloom").exists()

    @patch("src.runner.YamlConfiguredSupervisorAgent")
    def test_manifest_cleanup_failure_still_persists_terminal_status(
        self,
        mock_cls,
        fake_yaml: Path,
        monkeypatch,
    ) -> None:
        import json

        from src.application_run import ApplicationRunError
        from src.lib.runtime.context import RuntimeContext
        from src.runner import execute_app

        mock_cls.return_value.run.return_value = "ok"
        original_remove = RuntimeContext.remove_run_file

        def fail_starting_marker_cleanup(self, path):
            if Path(path).name == ".run-starting.json":
                raise OSError("starting marker cleanup failed")
            return original_remove(self, path)

        monkeypatch.setattr(
            RuntimeContext,
            "remove_run_file",
            fail_starting_marker_cleanup,
        )
        events = []

        with pytest.raises(ApplicationRunError) as caught:
            execute_app(
                str(fake_yaml),
                file_logging=False,
                event_sink=events.append,
            )

        manifest = json.loads(
            caught.value.run.manifest_path.read_text(encoding="utf-8")
        )
        assert caught.value.phase == "initialization"
        assert manifest["status"] == "failed"
        assert [event.event for event in events] == ["run.started", "run.failed"]

    @patch("src.runner.YamlConfiguredSupervisorAgent")
    def test_file_logging_receipt_points_to_closed_canonical_log(
        self,
        mock_cls,
        fake_yaml: Path,
    ) -> None:
        from src.runner import execute_app

        mock_cls.return_value.run.return_value = "ok"

        result = execute_app(str(fake_yaml), file_logging=True)

        assert result.run.log_path == result.run.run_dir / "logs" / "runtime.log"
        assert result.run.log_path.is_file()
        assert "Execution completed successfully" in result.run.log_path.read_text(
            encoding="utf-8"
        )

    @patch("src.runner.YamlConfiguredSupervisorAgent")
    def test_task_lease_release_failure_is_typed_cleanup_failure(
        self,
        mock_cls,
        fake_yaml: Path,
        monkeypatch,
    ) -> None:
        from src.application_run import ApplicationRunError
        from src.lib.checkpoint.checkpoint_manager import CheckpointTaskLease
        from src.runner import execute_app

        mock_cls.return_value.run.return_value = "ok"
        original_release = CheckpointTaskLease.release
        marker = OSError("task lease release failed")

        def release_then_fail(lease):
            original_release(lease)
            raise marker

        monkeypatch.setattr(CheckpointTaskLease, "release", release_then_fail)
        events = []

        with pytest.raises(ApplicationRunError) as caught:
            execute_app(
                str(fake_yaml),
                file_logging=False,
                event_sink=events.append,
            )

        assert caught.value.phase == "cleanup"
        assert caught.value.original_error is marker
        assert [event.event for event in events] == ["run.started", "run.failed"]

    @patch("src.runner.YamlConfiguredSupervisorAgent")
    def test_run_app_preserves_run_lease_release_error(
        self,
        mock_cls,
        fake_yaml: Path,
        monkeypatch,
    ) -> None:
        from src.lib.runtime.context import RuntimeRunLease
        from src.runner import run_app

        mock_cls.return_value.run.return_value = "ok"
        original_release = RuntimeRunLease.release
        marker = OSError("run lease release failed")

        def release_then_fail(lease):
            original_release(lease)
            raise marker

        monkeypatch.setattr(RuntimeRunLease, "release", release_then_fail)

        with pytest.raises(OSError) as caught:
            run_app(str(fake_yaml), file_logging=False)

        assert caught.value is marker

    @patch("src.runner.YamlConfiguredSupervisorAgent")
    def test_finalization_interrupt_reopens_checkpoint_for_resume(
        self,
        mock_cls,
        fake_yaml: Path,
        monkeypatch,
    ) -> None:
        import json

        from src.application_run import ApplicationRunInterrupted
        from src.lib.runtime.context import RuntimeContext
        from src.runner import execute_app

        mock_cls.return_value.run.return_value = "ok"
        original_update = RuntimeContext.update_manifest

        def interrupt_completed_manifest(self, **updates):
            if updates.get("status") == "completed":
                raise KeyboardInterrupt()
            return original_update(self, **updates)

        monkeypatch.setattr(
            RuntimeContext,
            "update_manifest",
            interrupt_completed_manifest,
        )

        with pytest.raises(ApplicationRunInterrupted) as caught:
            execute_app(str(fake_yaml), file_logging=False)

        assert caught.value.phase == "finalization"
        assert caught.value.resumable is True
        checkpoint_path = (
            caught.value.run.run_dir.parents[2]
            / "checkpoints"
            / caught.value.run.application_id
            / caught.value.run.task_id
            / "task_tree.json"
        )
        checkpoint = json.loads(checkpoint_path.read_text(encoding="utf-8"))
        assert checkpoint["status"] == "interrupted"

    @patch("src.runner.YamlConfiguredSupervisorAgent")
    def test_execute_app_wraps_system_exit_and_run_app_preserves_compatibility(
        self,
        mock_cls,
        fake_yaml: Path,
    ) -> None:
        from src.application_run import ApplicationRunError
        from src.runner import execute_app, run_app

        mock_cls.return_value.run.side_effect = SystemExit(7)
        events = []

        with pytest.raises(ApplicationRunError) as caught:
            execute_app(
                str(fake_yaml),
                file_logging=False,
                event_sink=events.append,
            )

        assert isinstance(caught.value.original_error, SystemExit)
        assert caught.value.original_error.code == 7
        assert caught.value.run.run_id
        assert [event.event for event in events] == ["run.started", "run.failed"]

        with pytest.raises(SystemExit) as compatibility:
            run_app(str(fake_yaml), file_logging=False)

        assert compatibility.value.code == 7

    @patch("src.runner.YamlConfiguredSupervisorAgent")
    def test_execute_app_wraps_generator_exit_and_run_app_preserves_compatibility(
        self,
        mock_cls,
        fake_yaml: Path,
    ) -> None:
        from src.application_run import ApplicationRunError
        from src.runner import execute_app, run_app

        mock_cls.return_value.run.side_effect = GeneratorExit()

        with pytest.raises(ApplicationRunError) as caught:
            execute_app(str(fake_yaml), file_logging=False)

        assert isinstance(caught.value.original_error, GeneratorExit)
        assert str(caught.value) == "GeneratorExit"
        assert caught.value.run.run_id

        with pytest.raises(GeneratorExit):
            run_app(str(fake_yaml), file_logging=False)
