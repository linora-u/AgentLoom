from __future__ import annotations

import json
import os
import subprocess
import sys
import textwrap
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest
from click.testing import CliRunner
from litellm.exceptions import Timeout

from src.__main__ import main
from src.application_run import (
    ApplicationRunError,
    ApplicationRunInterrupted,
    RunInfo,
    RunLifecycleEvent,
    RunRejectedEvent,
    RunRejection,
)

_REPO_ROOT = Path(__file__).resolve().parents[1]


def _run_info() -> RunInfo:
    run_dir = Path("/tmp/agentloom/runs/demo/run_123")
    return RunInfo(
        application_id="demo",
        task_id="task_123",
        run_id="run_123",
        run_dir=run_dir,
        manifest_path=run_dir / "manifest.json",
        log_path=run_dir / "logs" / "runtime.log",
    )


def _event(
    event: str,
    *,
    output: str | None = None,
    error: str | None = None,
    phase: str | None = None,
) -> RunLifecycleEvent:
    return RunLifecycleEvent(
        event=event,
        run=_run_info(),
        occurred_at=datetime(2026, 7, 18, 1, 2, 3, tzinfo=UTC),
        output=output,
        error=error,
        phase=phase,
    )


def test_run_accepts_task_override(monkeypatch: pytest.MonkeyPatch) -> None:
    observed: dict[str, object] = {}

    def succeed(*_args, **kwargs):
        observed.update(kwargs)
        return SimpleNamespace(output="completed")

    monkeypatch.setattr("src.runner.execute_app", succeed)

    result = CliRunner().invoke(
        main,
        ["run", "unused.yaml", "--task", "inspect this repository"],
    )

    assert result.exit_code == 0
    assert result.stdout == "completed\n"
    assert observed["task_override"] == "inspect this repository"


def test_python_module_invocation_exposes_run_command() -> None:
    env = os.environ.copy()
    env["PYTHONPATH"] = str(_REPO_ROOT)

    completed = subprocess.run(
        [sys.executable, "-m", "src", "run", "--help"],
        cwd=_REPO_ROOT,
        env=env,
        text=True,
        capture_output=True,
        timeout=30,
        check=False,
    )

    assert completed.returncode == 0
    assert "Usage:" in completed.stdout
    assert "--output-format [text|jsonl]" in completed.stdout


def test_jsonl_run_emits_only_lifecycle_events_on_stdout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def execute(*_args, event_sink=None, **_kwargs):
        print("application console output")
        event_sink(_event("run.started"))
        event_sink(_event("run.completed", output="final answer"))
        return SimpleNamespace(output="final answer")

    monkeypatch.setattr("src.runner.execute_app", execute, raising=False)

    result = CliRunner().invoke(
        main,
        ["run", "unused.yaml", "--output-format", "jsonl"],
    )

    assert result.exit_code == 0
    assert "application console output" in result.stderr
    records = [json.loads(line) for line in result.stdout.splitlines()]
    assert records == [
        {
            "schema_version": 1,
            "event": "run.started",
            "occurred_at": "2026-07-18T01:02:03+00:00",
            "run": {
                "application_id": "demo",
                "task_id": "task_123",
                "run_id": "run_123",
                "run_dir": "/tmp/agentloom/runs/demo/run_123",
                "manifest_path": "/tmp/agentloom/runs/demo/run_123/manifest.json",
                "log_path": "/tmp/agentloom/runs/demo/run_123/logs/runtime.log",
            },
        },
        {
            "schema_version": 1,
            "event": "run.completed",
            "occurred_at": "2026-07-18T01:02:03+00:00",
            "run": {
                "application_id": "demo",
                "task_id": "task_123",
                "run_id": "run_123",
                "run_dir": "/tmp/agentloom/runs/demo/run_123",
                "manifest_path": "/tmp/agentloom/runs/demo/run_123/manifest.json",
                "log_path": "/tmp/agentloom/runs/demo/run_123/logs/runtime.log",
            },
            "output": "final answer",
        },
    ]


def test_jsonl_run_isolates_native_and_child_fd1_from_protocol_stdout() -> None:
    env = os.environ.copy()
    env["PYTHONPATH"] = str(_REPO_ROOT)
    script = textwrap.dedent(
        """
        import os
        import subprocess
        import sys
        from datetime import UTC, datetime
        from pathlib import Path
        from types import SimpleNamespace

        import src.runner
        from src.application_run import RunInfo, RunLifecycleEvent

        run_dir = Path("/tmp/agentloom/runs/demo/run_fd_isolation")
        run = RunInfo(
            application_id="demo",
            task_id="task_fd_isolation",
            run_id="run_fd_isolation",
            run_dir=run_dir,
            manifest_path=run_dir / "manifest.json",
            log_path=run_dir / "logs" / "runtime.log",
        )

        def event(name, output=None):
            return RunLifecycleEvent(
                event=name,
                run=run,
                occurred_at=datetime(2026, 7, 18, 1, 2, 3, tzinfo=UTC),
                output=output,
            )

        def execute(*_args, event_sink=None, **_kwargs):
            print("python stdout contamination", flush=True)
            os.write(1, b"native fd1 contamination\\n")
            subprocess.run(
                [
                    sys.executable,
                    "-c",
                    "import os; os.write(1, b'child fd1 contamination\\\\n')",
                ],
                check=True,
            )
            event_sink(event("run.started"))
            event_sink(event("run.completed", output="final answer"))
            return SimpleNamespace(output="final answer")

        src.runner.execute_app = execute
        from src.__main__ import main

        main(["run", "unused.yaml", "--output-format", "jsonl"])
        """
    )

    completed = subprocess.run(
        [sys.executable, "-c", script],
        cwd=_REPO_ROOT,
        env=env,
        text=True,
        capture_output=True,
        timeout=30,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr
    records = [json.loads(line) for line in completed.stdout.splitlines()]
    assert [record["event"] for record in records] == [
        "run.started",
        "run.completed",
    ]
    assert "contamination" not in completed.stdout
    assert "python stdout contamination" in completed.stderr
    assert "native fd1 contamination" in completed.stderr
    assert "child fd1 contamination" in completed.stderr


def test_jsonl_preflight_failure_emits_run_rejected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def reject(*_args, **_kwargs):
        raise ValueError("invalid application configuration")

    monkeypatch.setattr("src.runner.execute_app", reject, raising=False)

    result = CliRunner().invoke(
        main,
        ["run", "invalid.yaml", "--output-format", "jsonl"],
    )

    assert result.exit_code == 1
    assert "Execution failed: invalid application configuration" in result.stderr
    records = [json.loads(line) for line in result.stdout.splitlines()]
    assert len(records) == 1
    rejected = records[0]
    assert rejected["schema_version"] == 1
    assert rejected["event"] == "run.rejected"
    assert rejected["phase"] == "preflight"
    assert rejected["error"] == {
        "kind": "ValueError",
        "message": "invalid application configuration",
        "retryable": False,
    }
    assert "run" not in rejected
    datetime.fromisoformat(rejected["occurred_at"])


def test_jsonl_core_preflight_rejection_is_serialized_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def reject(*_args, event_sink=None, **_kwargs):
        event_sink(
            RunRejectedEvent(
                occurred_at=datetime(2026, 7, 18, 1, 2, 3, tzinfo=UTC),
                error=RunRejection(
                    kind="ValueError",
                    message="invalid application configuration",
                ),
            )
        )
        raise ValueError("invalid application configuration")

    monkeypatch.setattr("src.runner.execute_app", reject, raising=False)

    result = CliRunner().invoke(
        main,
        ["run", "invalid.yaml", "--output-format", "jsonl"],
    )

    assert result.exit_code == 1
    records = [json.loads(line) for line in result.stdout.splitlines()]
    assert records == [
        {
            "schema_version": 1,
            "event": "run.rejected",
            "occurred_at": "2026-07-18T01:02:03+00:00",
            "phase": "preflight",
            "error": {
                "kind": "ValueError",
                "message": "invalid application configuration",
                "retryable": False,
            },
        }
    ]


def test_jsonl_allocated_failure_keeps_core_terminal_event(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def execute(*_args, event_sink=None, **_kwargs):
        event_sink(_event("run.started"))
        event_sink(
            _event(
                "run.failed",
                error="Agent execution failed: boom",
                phase="execution",
            )
        )
        raise ApplicationRunError(
            "Agent execution failed: boom",
            run=_run_info(),
            phase="execution",
            original_error=RuntimeError("boom"),
        )

    monkeypatch.setattr("src.runner.execute_app", execute)

    result = CliRunner().invoke(
        main,
        ["run", "unused.yaml", "--output-format", "jsonl"],
    )

    assert result.exit_code == 1
    records = [json.loads(line) for line in result.stdout.splitlines()]
    assert [record["event"] for record in records] == [
        "run.started",
        "run.failed",
    ]
    assert records[-1]["error"] == "Agent execution failed: boom"
    assert records[-1]["phase"] == "execution"
    assert "Execution failed: Agent execution failed: boom" in result.stderr


def test_jsonl_allocated_interrupt_keeps_core_terminal_event(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def execute(*_args, event_sink=None, **_kwargs):
        event_sink(_event("run.started"))
        event_sink(
            _event(
                "run.interrupted",
                error="Application run interrupted",
                phase="execution",
            )
        )
        raise ApplicationRunInterrupted(
            "Application run interrupted",
            run=_run_info(),
            phase="execution",
            original_error=KeyboardInterrupt(),
        )

    monkeypatch.setattr("src.runner.execute_app", execute)

    result = CliRunner().invoke(
        main,
        ["run", "unused.yaml", "--output-format", "jsonl"],
    )

    assert result.exit_code == 130
    records = [json.loads(line) for line in result.stdout.splitlines()]
    assert [record["event"] for record in records] == [
        "run.started",
        "run.interrupted",
    ]
    assert "Interrupted. Use --resume to continue." in result.stderr


def test_jsonl_transient_provider_failure_keeps_tempfail_exit_code(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def execute(*_args, event_sink=None, **_kwargs):
        event_sink(_event("run.started"))
        event_sink(
            _event(
                "run.failed",
                error="Agent execution failed: provider timed out",
                phase="execution",
            )
        )
        provider_error = Timeout(
            "provider timed out",
            model="summary",
            llm_provider="openai",
        )
        failure = ApplicationRunError(
            "Agent execution failed: provider timed out",
            run=_run_info(),
            phase="execution",
            original_error=provider_error,
        )
        raise failure from provider_error

    monkeypatch.setattr("src.runner.execute_app", execute)

    result = CliRunner().invoke(
        main,
        ["run", "unused.yaml", "--output-format", "jsonl"],
    )

    assert result.exit_code == 75
    assert [json.loads(line)["event"] for line in result.stdout.splitlines()] == [
        "run.started",
        "run.failed",
    ]


def test_text_interrupt_does_not_offer_unavailable_resume(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def execute(*_args, **_kwargs):
        raise ApplicationRunInterrupted(
            "Application run interrupted during cleanup",
            run=_run_info(),
            phase="cleanup",
            original_error=KeyboardInterrupt(),
            resumable=False,
        )

    monkeypatch.setattr("src.runner.execute_app", execute)

    result = CliRunner().invoke(main, ["run", "unused.yaml"])

    assert result.exit_code == 130
    assert "no resumable checkpoint is available" in result.stderr
    assert "--resume" not in result.stderr
