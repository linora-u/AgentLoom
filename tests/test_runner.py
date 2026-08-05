"""
Tests for src.runner and src.scaffold.

These tests validate the one-liner application launcher without
instantiating real LLM-backed agents.
"""

import json
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


def test_per_run_event_projection_never_inherits_legacy_preamble_on_resume() -> None:
    from src.runner import _events_for_run

    old_events = [
        {"type": "worker_call_finished", "agent_name": "old", "call_index": 0},
        {"type": "task_status_changed", "status": "completed", "result": "old result"},
    ]
    resumed = [
        {"type": "run_resumed", "run_id": "run-2"},
        {"type": "worker_call_started", "agent_name": "new", "call_index": 0},
    ]

    assert _events_for_run([*old_events, *resumed], "run-2") == resumed


def test_streamed_run_event_count_ignores_a_legacy_boundary_newline(tmp_path: Path) -> None:
    from src.lib.runtime.storage import SecureDirectory
    from src.runner import _run_event_chunks

    task_dir = tmp_path / "task"
    task_dir.mkdir()
    legacy_tail = b'{"type":"legacy_crash_tail"}'
    current = b'\n{"type":"run_resumed","run_id":"run-2"}\n{"type":"task_status_changed","status":"completed"}\n'
    (task_dir / "task_events.jsonl").write_bytes(legacy_tail + current)

    class FakeCheckpointManager:
        @staticmethod
        def task_storage(_task_id: str) -> SecureDirectory:
            return SecureDirectory(task_dir, create=False)

    stats = {"count": 0, "complete": True}
    copied = b"".join(
        _run_event_chunks(
            FakeCheckpointManager(),
            "task-1",
            start_offset=len(legacy_tail),
            stats=stats,
        )
    )

    assert copied == current
    assert stats == {"count": 2, "complete": True}


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
    def test_manifest_declares_when_task_tree_observation_is_disabled(
        self,
        mock_cls,
        fake_yaml: Path,
        monkeypatch,
    ) -> None:
        from src.lib.runtime import get_current_run_context
        from src.runner import run_app

        observed: dict[str, object] = {}

        def effective_config(config, *, source_name):
            del source_name
            return {
                **config,
                "checkpoint": {"enabled": False},
                "logging": {},
            }

        monkeypatch.setattr(
            "src.runner.build_effective_agent_config",
            effective_config,
        )

        def run_without_checkpoint(_task, **kwargs):
            observed["context"] = get_current_run_context(required=True)
            assert kwargs["checkpoint_manager"] is None
            return "ok"

        mock_cls.return_value.run.side_effect = run_without_checkpoint

        assert run_app(str(fake_yaml), file_logging=False) == "ok"

        context = observed["context"]
        manifest = json.loads(context.manifest_path.read_text(encoding="utf-8"))
        assert manifest["task_tree_observation"] == {
            "enabled": False,
            "worker_agents_configured": False,
        }
        assert "task_tree_artifact" not in manifest

    @patch("src.runner.YamlConfiguredSupervisorAgent")
    def test_completed_run_keeps_result_and_observability_after_checkpoint_cleanup(
        self,
        mock_cls,
        fake_yaml: Path,
        monkeypatch,
    ) -> None:
        from src.lib.checkpoint import CheckpointManager
        from src.lib.runtime import get_current_run_context
        from src.runner import run_app

        observed = {}

        def _run(_task, **kwargs):
            context = get_current_run_context(required=True)
            manager = kwargs["checkpoint_manager"]
            with manager._tree_lock:
                manager._append_task_event_unlocked(
                    context.task_id,
                    {
                        "type": "worker_call_finished",
                        "agent_name": "large-worker",
                        "payload": "x" * (1024 * 1024 + 128),
                    },
                )
            manager.record_task_status_changed(
                context.task_id,
                "completed",
                result="final answer",
            )
            observed["context"] = context
            return "final answer"

        mock_cls.return_value.run.side_effect = _run

        def reject_cumulative_tree_replay(*_args, **_kwargs):
            raise AssertionError("completed Run persistence must copy the maintained projection")

        monkeypatch.setattr(CheckpointManager, "load_task_tree", reject_cumulative_tree_replay)

        assert run_app(str(fake_yaml), file_logging=False) == "final answer"

        context = observed["context"]
        manifest = json.loads(context.manifest_path.read_text(encoding="utf-8"))
        assert manifest["result_artifact"] == "artifacts/result.txt"
        assert manifest["result_size"] == len(b"final answer")
        assert (context.run_dir / manifest["result_artifact"]).read_text(encoding="utf-8") == "final answer"
        assert json.loads((context.audit_dir / "task_tree.json").read_text(encoding="utf-8"))["status"] == "completed"
        events = [
            json.loads(line)
            for line in (context.audit_dir / "task_events.jsonl").read_text(encoding="utf-8").splitlines()
        ]
        assert events[-1]["type"] == "task_status_changed"
        assert events[-1]["result"] == "final answer"
        assert next(event for event in events if event.get("agent_name") == "large-worker")["payload"] == (
            "x" * (1024 * 1024 + 128)
        )
        assert manifest["task_events_count"] == len(events)
        assert manifest["task_events_complete"] is True
        assert not context.checkpoint_dir.exists()

    @patch("src.runner.YamlConfiguredSupervisorAgent")
    def test_completed_run_commits_manifest_before_checkpoint_cleanup(
        self,
        mock_cls,
        fake_yaml: Path,
        monkeypatch,
    ) -> None:
        from src.lib.checkpoint import CheckpointManager
        from src.lib.runtime import get_current_run_context
        from src.runner import run_app

        observed = {}

        def _run(_task, **kwargs):
            context = get_current_run_context(required=True)
            kwargs["checkpoint_manager"].record_task_status_changed(
                context.task_id,
                "completed",
                result="final answer",
            )
            observed["context"] = context
            return "final answer"

        original_delete_task = CheckpointManager.delete_task

        def delete_after_manifest_commit(manager, task_id):
            context = observed["context"]
            manifest = json.loads(context.manifest_path.read_text(encoding="utf-8"))
            observed["manifest_at_delete"] = manifest
            return original_delete_task(manager, task_id)

        mock_cls.return_value.run.side_effect = _run
        monkeypatch.setattr(CheckpointManager, "delete_task", delete_after_manifest_commit)

        assert run_app(str(fake_yaml), file_logging=False) == "final answer"

        manifest = observed["manifest_at_delete"]
        assert manifest["status"] == "completed"
        assert manifest["result_artifact"] == "artifacts/result.txt"
        assert manifest["task_tree_artifact"] == "audit/task_tree.json"
        assert manifest["task_events_artifact"] == "audit/task_events.jsonl"

    @patch("src.runner.YamlConfiguredSupervisorAgent")
    def test_completed_run_keeps_checkpoint_when_tree_exceeds_cleanup_budget(
        self,
        mock_cls,
        fake_yaml: Path,
        monkeypatch,
    ) -> None:
        from src.lib.runtime import get_current_run_context
        from src.runner import run_app

        observed = {}

        def _run(_task, **kwargs):
            context = get_current_run_context(required=True)
            kwargs["checkpoint_manager"].save_task_tree(
                context.task_id,
                {
                    "task_id": context.task_id,
                    "run_id": context.run_id,
                    "status": "completed",
                    "workers": {},
                    "padding": "x" * 512,
                },
            )
            observed["context"] = context
            return "final answer"

        mock_cls.return_value.run.side_effect = _run
        monkeypatch.setattr("src.runner._TASK_TREE_CLEANUP_MAX_BYTES", 128)

        assert run_app(str(fake_yaml), file_logging=False) == "final answer"

        context = observed["context"]
        manifest = json.loads(context.manifest_path.read_text(encoding="utf-8"))
        assert manifest["status"] == "completed"
        assert (context.run_dir / manifest["task_tree_artifact"]).stat().st_size > 128
        assert context.checkpoint_dir.exists()

    @patch("src.runner.YamlConfiguredSupervisorAgent")
    def test_finalization_failure_marks_run_failed_and_keeps_checkpoint(
        self,
        mock_cls,
        fake_yaml: Path,
        monkeypatch,
    ) -> None:
        from src.lib.checkpoint.checkpoint_manager import CheckpointTaskLease
        from src.lib.runtime import RuntimeContext, get_current_run_context
        from src.runner import run_app

        observed = {"manifest_updates": 0}

        def _run(_task, **kwargs):
            context = get_current_run_context(required=True)
            kwargs["checkpoint_manager"].record_task_status_changed(
                context.task_id,
                "completed",
                result="final answer",
            )
            observed["context"] = context
            return "final answer"

        original_atomic_write = RuntimeContext.atomic_write_run_file
        original_update_manifest = RuntimeContext.update_manifest

        def fail_result_write(context, path, content, **kwargs):
            if path == context.artifacts_dir / "result.txt":
                raise OSError("result persistence failed")
            return original_atomic_write(context, path, content, **kwargs)

        def record_manifest_update(context, **updates):
            observed["manifest_updates"] += 1
            return original_update_manifest(context, **updates)

        mock_cls.return_value.run.side_effect = _run
        monkeypatch.setattr(RuntimeContext, "atomic_write_run_file", fail_result_write)
        monkeypatch.setattr(RuntimeContext, "update_manifest", record_manifest_update)

        with pytest.raises(OSError, match="result persistence failed"):
            run_app(str(fake_yaml), file_logging=False)

        context = observed["context"]
        manifest = json.loads(context.manifest_path.read_text(encoding="utf-8"))
        assert manifest["status"] == "failed"
        assert "result persistence failed" in manifest["error"]
        assert observed["manifest_updates"] == 1
        assert context.checkpoint_dir.exists()
        with CheckpointTaskLease(context.checkpoint_dir, require_exists=True):
            pass

    @patch("src.runner.YamlConfiguredSupervisorAgent")
    def test_completed_run_keeps_checkpoint_when_manifest_commit_fails(
        self,
        mock_cls,
        fake_yaml: Path,
        monkeypatch,
    ) -> None:
        from src.lib.checkpoint.checkpoint_manager import CheckpointTaskLease
        from src.lib.runtime import RuntimeContext, get_current_run_context
        from src.runner import run_app

        observed = {"manifest_updates": 0}

        def _run(_task, **kwargs):
            context = get_current_run_context(required=True)
            kwargs["checkpoint_manager"].record_task_status_changed(
                context.task_id,
                "completed",
                result="final answer",
            )
            observed["context"] = context
            return "final answer"

        def fail_manifest_commit(_context, **_updates):
            observed["manifest_updates"] += 1
            raise OSError("manifest commit failed")

        mock_cls.return_value.run.side_effect = _run
        monkeypatch.setattr(RuntimeContext, "update_manifest", fail_manifest_commit)

        with pytest.raises(OSError, match="manifest commit failed"):
            run_app(str(fake_yaml), file_logging=False)

        context = observed["context"]
        manifest = json.loads(context.manifest_path.read_text(encoding="utf-8"))
        assert manifest["status"] == "running"
        # The structured runner makes one best-effort transition to ``failed``
        # after the completed-manifest commit itself fails.
        assert observed["manifest_updates"] == 2
        assert context.checkpoint_dir.exists()
        with CheckpointTaskLease(context.checkpoint_dir, require_exists=True):
            pass

    @patch("src.runner.YamlConfiguredSupervisorAgent")
    def test_transient_manifest_failure_keeps_written_artifact_references(
        self,
        mock_cls,
        fake_yaml: Path,
        monkeypatch,
    ) -> None:
        from src.application_run import ApplicationRunError
        from src.lib.runtime import RuntimeContext, get_current_run_context
        from src.runner import execute_app

        observed: dict[str, object] = {"failed_once": False}
        marker = OSError("manifest commit failed once")

        def _run(_task, **kwargs):
            context = get_current_run_context(required=True)
            kwargs["checkpoint_manager"].record_task_status_changed(
                context.task_id,
                "completed",
                result="final answer",
            )
            observed["context"] = context
            return "final answer"

        original_update_manifest = RuntimeContext.update_manifest

        def fail_completed_manifest_once(context, **updates):
            if updates.get("status") == "completed" and not observed["failed_once"]:
                observed["failed_once"] = True
                raise marker
            return original_update_manifest(context, **updates)

        mock_cls.return_value.run.side_effect = _run
        monkeypatch.setattr(
            RuntimeContext,
            "update_manifest",
            fail_completed_manifest_once,
        )

        with pytest.raises(ApplicationRunError) as caught:
            execute_app(str(fake_yaml), file_logging=False)

        assert caught.value.phase == "finalization"
        assert caught.value.original_error is marker
        context = observed["context"]
        manifest = json.loads(context.manifest_path.read_text(encoding="utf-8"))
        assert manifest["status"] == "failed"
        assert manifest["result_artifact"] == "artifacts/result.txt"
        assert manifest["task_tree_artifact"] == "audit/task_tree.json"
        assert manifest["task_events_artifact"] == "audit/task_events.jsonl"
        assert (context.run_dir / manifest["result_artifact"]).read_text(encoding="utf-8") == "final answer"
        assert (context.run_dir / manifest["task_tree_artifact"]).is_file()
        assert (context.run_dir / manifest["task_events_artifact"]).is_file()
        assert context.checkpoint_dir.is_dir()

    @patch("src.runner.YamlConfiguredSupervisorAgent")
    def test_run_finally_releases_shell_sessions_and_background_tasks(
        self,
        mock_cls,
        fake_yaml: Path,
    ):
        import os
        import subprocess

        from src.lib.runtime import bind_run_context, get_current_run_context
        from src.runner import run_app
        from src.tools.shell.background_task import BackgroundTaskRegistry
        from src.tools.shell.process import ShellProcessRegistry
        from src.trace import clear_current_agent_id, set_current_agent_id

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
                other_marker not in log_text for _other_context, other_marker in contexts if other_marker != marker
            )

    @patch("src.runner.YamlConfiguredSupervisorAgent")
    def test_resume_creates_new_run_but_reuses_task_checkpoint(
        self,
        mock_cls,
        fake_yaml: Path,
        monkeypatch,
    ):
        from src.lib.checkpoint import CheckpointManager
        from src.lib.runtime import get_current_run_context
        from src.runner import run_app

        attempts = []
        mock_agent = MagicMock()

        def _run(_task, **kwargs):
            attempts.append((get_current_run_context(required=True), kwargs))
            return "ok"

        mock_agent.run.side_effect = _run
        mock_cls.return_value = mock_agent

        def reject_cumulative_event_load(*_args, **_kwargs):
            raise AssertionError("Run persistence must not materialize cumulative events")

        monkeypatch.setattr(
            CheckpointManager,
            "load_task_events",
            reject_cumulative_event_load,
        )

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
        second_events = [
            json.loads(line)
            for line in (second_context.audit_dir / "task_events.jsonl").read_text(encoding="utf-8").splitlines()
        ]
        assert second_events[0]["type"] == "run_resumed"
        assert second_events[0]["run_id"] == second_context.run_id
        assert all(event.get("run_id") != first_context.run_id for event in second_events)

    @pytest.mark.parametrize(
        ("created_at", "error"),
        [
            ("not-a-timestamp", "invalid created_at"),
            ("", "invalid created_at"),
            (
                (datetime.now(UTC) - timedelta(days=30)).replace(tzinfo=None).isoformat(),
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
                    "created_at": (datetime.now(UTC) - timedelta(days=8)).isoformat(),
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
    def test_numeric_description_is_rejected_before_agent_construction(
        self,
        mock_cls,
        fake_yaml: Path,
    ) -> None:
        from src.runner import run_app

        fake_yaml.write_text(
            "name: test_agent\ndescription: 123\nworkflow: do the task\n",
            encoding="utf-8",
        )

        with pytest.raises(ValueError, match="description must be a non-empty string"):
            run_app(str(fake_yaml))

        mock_cls.assert_not_called()

    @patch("src.runner.YamlConfiguredSupervisorAgent")
    def test_invalid_execution_environment_is_rejected_before_agent_construction(
        self,
        mock_cls,
        fake_yaml: Path,
    ) -> None:
        from src.runner import run_app

        fake_yaml.write_text(_SAMPLE_YAML + "execution_env: []\n", encoding="utf-8")

        with pytest.raises(ValueError, match="execution_env must be a dictionary"):
            run_app(str(fake_yaml))

        mock_cls.assert_not_called()

    @pytest.mark.parametrize(
        ("invalid_config", "error_pattern"),
        [
            ("toolsets: core_shell\n", "toolsets must be a list"),
            ("toolsets:\n  - missing_toolset\n", "Unknown toolset 'missing_toolset'"),
            ("tools:\n  - name: missing_registered_tool\n", "missing_registered_tool"),
            (
                "tools:\n  - name: read_file\n    fixed_args:\n      definitely_unknown: 1\n",
                "Unknown fixed_args for tool 'read_file': definitely_unknown",
            ),
            ("max_steps: 0\n", "max_steps must be a positive integer"),
            ("max_steps: true\n", "max_steps must be a positive integer"),
        ],
    )
    @patch("src.runner.YamlConfiguredSupervisorAgent")
    def test_runtime_preflight_rejects_unresolvable_tools_and_invalid_step_budget(
        self,
        mock_cls,
        invalid_config: str,
        error_pattern: str,
        fake_yaml: Path,
    ) -> None:
        from src.runner import run_app

        fake_yaml.write_text(
            "name: test_agent\ndescription: test\nworkflow: do the task\n" + invalid_config,
            encoding="utf-8",
        )

        with pytest.raises(ValueError, match=error_pattern):
            run_app(str(fake_yaml))

        mock_cls.assert_not_called()

    @patch("src.runner.YamlConfiguredSupervisorAgent")
    def test_runtime_preflight_only_structurally_validates_dynamic_tools(
        self,
        mock_cls,
        fake_yaml: Path,
    ) -> None:
        from src.runner import run_app

        fake_yaml.write_text(
            """\
name: test_agent
description: test
workflow: do the task
tools:
  - name: external_tool
    module: package_that_does_not_exist
    function: external_tool
""",
            encoding="utf-8",
        )
        mock_agent = MagicMock()
        mock_agent.run.return_value = "done"
        mock_cls.return_value = mock_agent

        assert run_app(str(fake_yaml), file_logging=False) == "done"
        mock_cls.assert_called_once()

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
    def test_resume_rejects_disabling_a_persisted_active_goal(
        self,
        mock_cls,
        fake_yaml: Path,
    ) -> None:
        from src.application_run import (
            ApplicationRunBudgetLimited,
            ApplicationRunError,
        )
        from src.lib.goal import GoalBudgetLimitedError, GoalState
        from src.runner import execute_app

        fake_yaml.write_text(_SAMPLE_YAML + "\ngoal:\n  enabled: true\n  token_budget: 100\n")
        state = GoalState.create(
            objective="Finish the application.",
            objective_fingerprint="fingerprint",
            token_budget=100,
        ).with_usage(90, 20)

        def _limit(_task, **kwargs):
            manager = kwargs["checkpoint_manager"]
            manager.save_goal(kwargs["task_id"], state.to_dict())
            manager.record_task_status_changed(kwargs["task_id"], "budget_limited")
            raise GoalBudgetLimitedError(state)

        mock_cls.return_value.run.side_effect = _limit
        with pytest.raises(ApplicationRunBudgetLimited) as limited:
            execute_app(str(fake_yaml), file_logging=False)

        fake_yaml.write_text(_SAMPLE_YAML + "\ngoal: false\n", encoding="utf-8")
        with pytest.raises(ApplicationRunError) as rejected:
            execute_app(
                str(fake_yaml),
                file_logging=False,
                resume_task_id=limited.value.run.task_id,
            )

        assert isinstance(rejected.value.original_error, ValueError)
        assert "Goal mode is disabled" in str(rejected.value.original_error)

    @patch("src.runner.YamlConfiguredSupervisorAgent")
    def test_budget_limited_is_structured_resumable_outcome(
        self,
        mock_cls,
        fake_yaml: Path,
    ) -> None:
        from src.application_run import ApplicationRunBudgetLimited
        from src.lib.goal import GoalBudgetLimitedError, GoalState
        from src.runner import execute_app

        state = GoalState.create(
            objective="Finish the application.",
            objective_fingerprint="fingerprint",
            token_budget=100,
        ).with_usage(90, 20)

        def _run(_task, **kwargs):
            manager = kwargs["checkpoint_manager"]
            manager.save_goal(kwargs["task_id"], state.to_dict())
            manager.record_task_status_changed(kwargs["task_id"], "budget_limited")
            raise GoalBudgetLimitedError(state)

        mock_cls.return_value.run.side_effect = _run
        events = []

        with pytest.raises(ApplicationRunBudgetLimited) as caught:
            execute_app(str(fake_yaml), file_logging=False, event_sink=events.append)

        assert caught.value.resumable is True
        assert caught.value.goal["used_tokens"] == 110
        with pytest.raises(TypeError):
            caught.value.goal["status"] = "active"
        assert [event.event for event in events] == [
            "run.started",
            "run.budget_limited",
        ]
        assert events[-1].goal["status"] == "budget_limited"
        with pytest.raises(TypeError):
            events[-1].goal["status"] = "active"
        manifest = json.loads(
            caught.value.run.manifest_path.read_text(encoding="utf-8")
        )
        assert manifest["status"] == "budget_limited"
        assert manifest["goal"]["used_tokens"] == 110
        assert manifest["goal_artifact"] == "audit/goal.json"
        assert (
            caught.value.run.run_dir.parent.parent.parent
            / "checkpoints"
            / "test_app"
            / caught.value.run.task_id
            / "goal.json"
        ).exists()

    @patch("src.runner.YamlConfiguredSupervisorAgent")
    def test_completed_goal_is_copied_before_checkpoint_cleanup(
        self,
        mock_cls,
        fake_yaml: Path,
    ) -> None:
        from src.lib.goal import GoalState
        from src.runner import execute_app

        state = GoalState.create(
            objective="Finish the application.",
            objective_fingerprint="fingerprint",
            token_budget=None,
        ).with_completion("Delivered and verified.")

        def _run(_task, **kwargs):
            manager = kwargs["checkpoint_manager"]
            manager.save_goal(kwargs["task_id"], state.to_dict())
            manager.record_task_status_changed(
                kwargs["task_id"],
                "completed",
                result=state.evidence,
            )
            return state.evidence

        mock_cls.return_value.run.side_effect = _run

        result = execute_app(str(fake_yaml), file_logging=False)

        manifest = json.loads(result.run.manifest_path.read_text(encoding="utf-8"))
        assert result.goal["status"] == "complete"
        with pytest.raises(TypeError):
            result.goal["status"] = "active"
        assert manifest["goal"]["evidence"] == "Delivered and verified."
        assert json.loads(
            (result.run.run_dir / manifest["goal_artifact"]).read_text(
                encoding="utf-8"
            )
        )["status"] == "complete"

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
        assert result.goal is None
        assert "goal" not in json.loads(
            result.run.manifest_path.read_text(encoding="utf-8")
        )
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
        assert manifest["application_revision"].startswith("sha256:")
        assert manifest["result_artifact"] == "artifacts/result.txt"
        assert (result.run.run_dir / manifest["result_artifact"]).read_text(
            encoding="utf-8"
        ) == "structured-output"

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
        from src.lib.goal import GoalState
        from src.runner import execute_app

        fake_yaml.write_text(_SAMPLE_YAML + "\ngoal: true\n", encoding="utf-8")
        state = GoalState.create(
            objective="Finish the application.",
            objective_fingerprint="fingerprint",
            token_budget=None,
        )

        def _fail(_task, **kwargs):
            kwargs["checkpoint_manager"].save_goal(
                kwargs["task_id"], state.to_dict()
            )
            raise RuntimeError("boom")

        mock_cls.return_value.run.side_effect = _fail
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
        assert events[-1].goal["status"] == "active"
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
        from src.lib.goal import GoalState
        from src.runner import execute_app

        fake_yaml.write_text(_SAMPLE_YAML + "\ngoal: true\n", encoding="utf-8")
        state = GoalState.create(
            objective="Finish the application.",
            objective_fingerprint="fingerprint",
            token_budget=None,
        )

        def _interrupt(_task, **kwargs):
            kwargs["checkpoint_manager"].save_goal(
                kwargs["task_id"], state.to_dict()
            )
            raise KeyboardInterrupt()

        mock_cls.return_value.run.side_effect = _interrupt
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
        assert events[-1].goal["status"] == "active"

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
        from src.lib.checkpoint import CheckpointManager
        from src.lib.checkpoint.checkpoint_manager import CheckpointTaskLease
        from src.runner import execute_app

        mock_cls.return_value.run.return_value = "ok"
        original_release = CheckpointTaskLease.release
        original_close = CheckpointManager.close
        marker = OSError("task lease release failed")
        closed_managers = []

        def release_then_fail(lease):
            original_release(lease)
            raise marker

        def record_close(manager):
            closed_managers.append(manager)
            original_close(manager)

        monkeypatch.setattr(CheckpointTaskLease, "release", release_then_fail)
        monkeypatch.setattr(CheckpointManager, "close", record_close)
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
        assert len(closed_managers) == 1
        assert closed_managers[0]._task_storages == {}

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
    def test_interrupt_after_checkpoint_deletion_is_not_resumable(
        self,
        mock_cls,
        fake_yaml: Path,
        monkeypatch,
    ) -> None:
        from src.application_run import ApplicationRunInterrupted
        from src.lib.checkpoint import CheckpointManager
        from src.lib.runtime import get_current_run_context
        from src.runner import execute_app

        observed = {}

        def _run(_task, **kwargs):
            context = get_current_run_context(required=True)
            kwargs["checkpoint_manager"].record_task_status_changed(
                context.task_id,
                "completed",
                result="final answer",
            )
            observed["context"] = context
            return "final answer"

        original_delete_task = CheckpointManager.delete_task

        def delete_then_interrupt(manager, task_id):
            assert original_delete_task(manager, task_id) is True
            raise KeyboardInterrupt("interrupted after checkpoint deletion")

        mock_cls.return_value.run.side_effect = _run
        monkeypatch.setattr(
            CheckpointManager,
            "delete_task",
            delete_then_interrupt,
        )

        with pytest.raises(ApplicationRunInterrupted) as caught:
            execute_app(str(fake_yaml), file_logging=False)

        assert caught.value.phase == "finalization"
        assert caught.value.resumable is False
        context = observed["context"]
        assert not context.checkpoint_dir.exists()
        manifest = json.loads(context.manifest_path.read_text(encoding="utf-8"))
        assert manifest["status"] == "interrupted"
        assert manifest["result_artifact"] == "artifacts/result.txt"
        assert (context.run_dir / manifest["result_artifact"]).read_text(encoding="utf-8") == "final answer"

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
