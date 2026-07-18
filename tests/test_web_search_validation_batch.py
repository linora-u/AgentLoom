from __future__ import annotations

import json
import signal
import subprocess
from pathlib import Path

import pytest

from applications.web_search.scripts import (
    run_us_after_close_validation_batch as batch,
)

NOW_UTC = "2026-06-18T00:30:00Z"


def _context() -> dict[str, object]:
    return {
        "query_terms": {
            "us_trading_day_iso": "2026-06-17",
            "a_share_prediction_date_iso": "2026-06-18",
        },
        "news_window": {
            "start_asia_shanghai": "2026-06-17T08:00:00+08:00",
            "end_asia_shanghai": "2026-06-18T08:00:00+08:00",
        },
    }


def _run_info(run_dir: Path) -> dict[str, object]:
    return {
        "application_id": "web_search",
        "task_id": "task_test",
        "run_id": run_dir.name,
        "run_dir": str(run_dir),
        "manifest_path": str(run_dir / "manifest.json"),
        "log_path": str(run_dir / "logs" / "runtime.log"),
    }


def _events(
    run: dict[str, object],
    terminal: str = "run.completed",
    *,
    output: str = (
        "applications/web_search/outputs/"
        "us_after_close_a_share_signal_2026-06-17_to_2026-06-18.md"
    ),
) -> str:
    records = [
        {
            "schema_version": 1,
            "event": "run.started",
            "occurred_at": "2026-07-18T01:00:00+00:00",
            "run": run,
        },
        {
            "schema_version": 1,
            "event": terminal,
            "occurred_at": "2026-07-18T01:00:01+00:00",
            "run": run,
        },
    ]
    if terminal == "run.completed":
        records[-1]["output"] = output
    return "\n".join(json.dumps(record) for record in records) + "\n"


def _prepare_completed_run(
    run: dict[str, object],
) -> None:
    run_dir = Path(str(run["run_dir"]))
    (run_dir / "logs").mkdir(parents=True)
    (run_dir / "logs" / "runtime.log").write_text("canonical", encoding="utf-8")
    (run_dir / "manifest.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "application_id": run["application_id"],
                "task_id": run["task_id"],
                "run_id": run["run_id"],
                "status": "completed",
            }
        ),
        encoding="utf-8",
    )


def test_parse_lifecycle_events_accepts_one_started_and_terminal(
    tmp_path: Path,
) -> None:
    run = _run_info(tmp_path / "run_test")

    events, errors = batch._parse_lifecycle_events(_events(run))

    assert errors == []
    assert [event["event"] for event in events] == [
        "run.started",
        "run.completed",
    ]
    assert batch._run_identity(events) == run


@pytest.mark.parametrize(
    "stdout",
    [
        "console contamination\n",
        json.dumps({"schema_version": 2, "event": "run.started"}) + "\n",
    ],
)
def test_parse_lifecycle_events_rejects_non_protocol_stdout(stdout: str) -> None:
    _events_seen, errors = batch._parse_lifecycle_events(stdout)

    assert errors


def test_parse_lifecycle_events_rejects_identity_changes(
    tmp_path: Path,
) -> None:
    first = _run_info(tmp_path / "run_first")
    second = _run_info(tmp_path / "run_second")
    stdout = "\n".join(
        [
            json.dumps(
                {
                    "schema_version": 1,
                    "event": "run.started",
                    "run": first,
                }
            ),
            json.dumps(
                {
                    "schema_version": 1,
                    "event": "run.failed",
                    "run": second,
                }
            ),
        ]
    )

    _events_seen, errors = batch._parse_lifecycle_events(stdout)

    assert "terminal RunInfo differs from run.started" in errors


def test_run_rejected_is_valid_only_as_standalone_event() -> None:
    rejected = json.dumps(
        {
            "schema_version": 1,
            "event": "run.rejected",
            "phase": "preflight",
            "error": {"kind": "ValueError", "message": "invalid"},
        }
    )

    events, errors = batch._parse_lifecycle_events(rejected + "\n")

    assert [event["event"] for event in events] == ["run.rejected"]
    assert errors == []


def test_run_rejected_preserves_structured_diagnostic_without_a_log(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rejected_error = {
        "kind": "ValueError",
        "message": "invalid application configuration",
        "retryable": False,
    }
    rejected = json.dumps(
        {
            "schema_version": 1,
            "event": "run.rejected",
            "phase": "preflight",
            "error": rejected_error,
        }
    ) + "\n"

    class Process:
        pid = 4101
        returncode = 1

        def communicate(self, timeout=None):
            return rejected, "human diagnostic"

    monkeypatch.setattr(batch, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(
        batch,
        "OUTPUT_DIR",
        tmp_path / "applications" / "web_search" / "outputs",
    )
    monkeypatch.setattr(batch, "_context", lambda _now: _context())
    monkeypatch.setattr(batch.subprocess, "Popen", lambda *_args, **_kwargs: Process())

    result = batch._run_one(NOW_UTC, timeout_seconds=5, skip_existing_ok=False)

    assert result["status"] == "rejected"
    assert result["log_path"] is None
    assert result["error"] == rejected_error
    assert result["phase"] == "preflight"


def test_batch_uses_cli_jsonl_and_canonical_log_without_sidecar(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    output_dir = tmp_path / "applications" / "web_search" / "outputs"
    report_path = output_dir / (
        "us_after_close_a_share_signal_2026-06-17_to_2026-06-18.md"
    )
    run_dir = tmp_path / ".agentloom" / "runs" / "web_search" / "run_test"
    run = _run_info(run_dir)
    observed: dict[str, object] = {}

    class Process:
        pid = 4102
        returncode = 0

        def communicate(self, timeout=None):
            observed["timeout"] = timeout
            report_path.parent.mkdir(parents=True, exist_ok=True)
            report_path.write_text("new report", encoding="utf-8")
            _prepare_completed_run(run)
            return _events(run), "human diagnostics"

    def popen(command, **kwargs):
        observed["command"] = command
        observed["kwargs"] = kwargs
        return Process()

    monkeypatch.setattr(batch, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(batch, "OUTPUT_DIR", output_dir)
    monkeypatch.setattr(batch, "_context", lambda _now: _context())
    monkeypatch.setattr(batch.subprocess, "Popen", popen)
    monkeypatch.setattr(
        batch,
        "_audit_payload",
        lambda _report, _log: {"ok": True, "issues": [], "log": {"ok": True}},
    )

    result = batch._run_one(NOW_UTC, timeout_seconds=15, skip_existing_ok=False)

    assert result["status"] == "completed"
    assert result["task_id"] == "task_test"
    assert result["run_id"] == "run_test"
    assert result["manifest_path"] == str(run_dir / "manifest.json")
    assert result["log_path"] == str(run_dir / "logs" / "runtime.log")
    assert result["protocol_errors"] == []
    assert observed["command"][:4] == [
        batch.sys.executable,
        "-m",
        "src.__main__",
        "run",
    ]
    assert "--output-format" in observed["command"]
    assert "jsonl" in observed["command"]
    assert observed["kwargs"]["stdout"] is subprocess.PIPE
    assert observed["kwargs"]["stderr"] is subprocess.PIPE
    assert observed["kwargs"]["start_new_session"] is True
    assert not (output_dir / "validation_logs").exists()


def test_timeout_sends_sigint_and_keeps_started_identity(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_dir = tmp_path / ".agentloom" / "runs" / "web_search" / "run_test"
    run = _run_info(run_dir)
    calls = 0
    sent_signals: list[int] = []

    class Process:
        pid = 4103
        returncode = None

        def communicate(self, timeout=None):
            nonlocal calls
            calls += 1
            if calls == 1:
                raise subprocess.TimeoutExpired(["loom"], timeout)
            self.returncode = 130
            return _events(run, "run.interrupted"), ""

    monkeypatch.setattr(batch, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(
        batch,
        "OUTPUT_DIR",
        tmp_path / "applications" / "web_search" / "outputs",
    )
    monkeypatch.setattr(batch, "_context", lambda _now: _context())
    monkeypatch.setattr(batch.subprocess, "Popen", lambda *_args, **_kwargs: Process())
    monkeypatch.setattr(
        batch.os,
        "killpg",
        lambda _pid, sig: sent_signals.append(sig),
    )

    result = batch._run_one(NOW_UTC, timeout_seconds=1, skip_existing_ok=False)

    assert result["timed_out"] is True
    assert result["force_killed"] is False
    assert result["status"] == "interrupted"
    assert result["run_id"] == "run_test"
    assert sent_signals == [signal.SIGINT]


def test_parent_interrupt_stops_child_and_persists_started_identity(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_dir = tmp_path / ".agentloom" / "runs" / "web_search" / "run_test"
    run = _run_info(run_dir)
    calls = 0
    sent_signals: list[int] = []

    class Process:
        pid = 4105
        returncode = None

        def communicate(self, timeout=None):
            nonlocal calls
            calls += 1
            if calls == 1:
                raise KeyboardInterrupt()
            self.returncode = 130
            return _events(run, "run.interrupted"), ""

    monkeypatch.setattr(batch, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(
        batch,
        "OUTPUT_DIR",
        tmp_path / "applications" / "web_search" / "outputs",
    )
    monkeypatch.setattr(batch, "_context", lambda _now: _context())
    monkeypatch.setattr(batch.subprocess, "Popen", lambda *_args, **_kwargs: Process())
    monkeypatch.setattr(
        batch.os,
        "killpg",
        lambda _pid, sig: sent_signals.append(sig),
    )

    result = batch._run_one(NOW_UTC, timeout_seconds=30, skip_existing_ok=False)

    assert result["parent_interrupted"] is True
    assert result["timed_out"] is False
    assert result["status"] == "interrupted"
    assert result["run_id"] == "run_test"
    assert sent_signals == [signal.SIGINT]


def test_parent_interrupt_before_communicate_guard_still_stops_child(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_dir = tmp_path / ".agentloom" / "runs" / "web_search" / "run_test"
    run = _run_info(run_dir)
    sent_signals: list[int] = []

    class Process:
        pid = 4108
        returncode = None

        def communicate(self, timeout=None):
            self.returncode = 130
            return _events(run, "run.interrupted"), ""

    monkeypatch.setattr(batch, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(
        batch,
        "OUTPUT_DIR",
        tmp_path / "applications" / "web_search" / "outputs",
    )
    monkeypatch.setattr(batch, "_context", lambda _now: _context())
    monkeypatch.setattr(batch.subprocess, "Popen", lambda *_args, **_kwargs: Process())
    monkeypatch.setattr(
        batch,
        "_communicate",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(KeyboardInterrupt()),
    )
    monkeypatch.setattr(
        batch.os,
        "killpg",
        lambda _pid, sig: sent_signals.append(sig),
    )

    result = batch._run_one(NOW_UTC, timeout_seconds=30, skip_existing_ok=False)

    assert result["parent_interrupted"] is True
    assert result["status"] == "interrupted"
    assert result["run_id"] == "run_test"
    assert sent_signals == [signal.SIGINT]


def test_force_kill_still_keeps_run_started_receipt(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_dir = tmp_path / ".agentloom" / "runs" / "web_search" / "run_test"
    run = _run_info(run_dir)
    started = json.dumps(
        {
            "schema_version": 1,
            "event": "run.started",
            "run": run,
        }
    ) + "\n"
    calls = 0
    sent_signals: list[int] = []

    class Process:
        pid = 4104
        returncode = None

        def communicate(self, timeout=None):
            nonlocal calls
            calls += 1
            if calls < 3:
                raise subprocess.TimeoutExpired(["loom"], timeout)
            self.returncode = -signal.SIGKILL
            return started, ""

    monkeypatch.setattr(batch, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(
        batch,
        "OUTPUT_DIR",
        tmp_path / "applications" / "web_search" / "outputs",
    )
    monkeypatch.setattr(batch, "_context", lambda _now: _context())
    monkeypatch.setattr(batch.subprocess, "Popen", lambda *_args, **_kwargs: Process())
    monkeypatch.setattr(
        batch.os,
        "killpg",
        lambda _pid, sig: sent_signals.append(sig),
    )

    result = batch._run_one(NOW_UTC, timeout_seconds=1, skip_existing_ok=False)

    assert result["status"] == "killed"
    assert result["run_id"] == "run_test"
    assert result["log_path"] == str(run_dir / "logs" / "runtime.log")
    assert sent_signals == [signal.SIGINT, signal.SIGKILL]
    assert "expected exactly one terminal event" in result["protocol_errors"]


def test_parent_interrupt_during_timeout_cleanup_keeps_started_receipt(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_dir = tmp_path / ".agentloom" / "runs" / "web_search" / "run_test"
    run = _run_info(run_dir)
    started = json.dumps(
        {"schema_version": 1, "event": "run.started", "run": run}
    ) + "\n"
    calls = 0
    sent_signals: list[int] = []

    class Process:
        pid = 4107
        returncode = None

        def communicate(self, timeout=None):
            nonlocal calls
            calls += 1
            if calls == 1:
                raise subprocess.TimeoutExpired(
                    ["loom"],
                    timeout,
                    output=started,
                )
            if calls == 2:
                raise KeyboardInterrupt()
            self.returncode = -signal.SIGKILL
            return "", ""

    monkeypatch.setattr(batch, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(
        batch,
        "OUTPUT_DIR",
        tmp_path / "applications" / "web_search" / "outputs",
    )
    monkeypatch.setattr(batch, "_context", lambda _now: _context())
    monkeypatch.setattr(batch.subprocess, "Popen", lambda *_args, **_kwargs: Process())
    monkeypatch.setattr(
        batch.os,
        "killpg",
        lambda _pid, sig: sent_signals.append(sig),
    )

    result = batch._run_one(NOW_UTC, timeout_seconds=1, skip_existing_ok=False)

    assert result["timed_out"] is True
    assert result["force_killed"] is True
    assert result["parent_interrupted"] is True
    assert result["status"] == "killed"
    assert result["run_id"] == "run_test"
    assert sent_signals == [signal.SIGINT, signal.SIGKILL]


def test_force_kill_pipe_drain_has_a_hard_timeout(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_dir = tmp_path / ".agentloom" / "runs" / "web_search" / "run_test"
    run = _run_info(run_dir)
    started = json.dumps(
        {"schema_version": 1, "event": "run.started", "run": run}
    ) + "\n"
    calls: list[int | None] = []
    waits: list[int] = []

    class Pipe:
        closed = False

        def close(self):
            self.closed = True

    class Process:
        pid = 4106
        returncode = None
        stdout = Pipe()
        stderr = Pipe()

        def communicate(self, timeout=None):
            calls.append(timeout)
            raise subprocess.TimeoutExpired(
                ["loom"],
                timeout,
                output=started if len(calls) == 3 else None,
            )

        def wait(self, timeout=None):
            waits.append(timeout)
            self.returncode = -signal.SIGKILL
            return self.returncode

    process = Process()
    monkeypatch.setattr(batch, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(
        batch,
        "OUTPUT_DIR",
        tmp_path / "applications" / "web_search" / "outputs",
    )
    monkeypatch.setattr(batch, "_context", lambda _now: _context())
    monkeypatch.setattr(batch.subprocess, "Popen", lambda *_args, **_kwargs: process)
    monkeypatch.setattr(batch.os, "killpg", lambda *_args: None)

    result = batch._run_one(NOW_UTC, timeout_seconds=1, skip_existing_ok=False)

    assert calls == [1, batch.INTERRUPT_GRACE_SECONDS, batch.KILL_DRAIN_SECONDS]
    assert waits == [batch.KILL_DRAIN_SECONDS]
    assert process.stdout.closed and process.stderr.closed
    assert result["status"] == "killed"
    assert result["run_id"] == "run_test"


def test_historical_skip_revalidates_canonical_manifest_and_log(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    output_dir = tmp_path / "applications" / "web_search" / "outputs"
    report_path = output_dir / (
        "us_after_close_a_share_signal_2026-06-17_to_2026-06-18.md"
    )
    report_path.parent.mkdir(parents=True)
    report_path.write_text("report", encoding="utf-8")
    run_dir = tmp_path / ".agentloom" / "runs" / "web_search" / "run_previous"
    run = _run_info(run_dir)
    _prepare_completed_run(run)
    previous = {
        "now_utc": NOW_UTC,
        "status": "completed",
        "returncode": 0,
        "timed_out": False,
        "force_killed": False,
        "parent_interrupted": False,
        "protocol_errors": [],
        "audit": {"ok": True},
        "report_path": report_path.relative_to(tmp_path).as_posix(),
        **run,
    }

    monkeypatch.setattr(batch, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(batch, "OUTPUT_DIR", output_dir)
    monkeypatch.setattr(
        batch,
        "_audit_payload",
        lambda _report, _log: {"ok": True, "issues": [], "log": {"ok": True}},
    )

    skipped = batch._historical_skip(
        NOW_UTC,
        _context(),
        report_path,
        {NOW_UTC: previous},
    )

    assert skipped is not None
    assert skipped["skipped"] is True
    assert skipped["run_id"] == "run_previous"
    assert skipped["log_path"] == str(run_dir / "logs" / "runtime.log")


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("returncode", 1),
        ("timed_out", True),
        ("force_killed", True),
        ("parent_interrupted", True),
        ("protocol_errors", ["stdout contamination"]),
    ],
)
def test_historical_skip_never_relabels_failed_validation_as_success(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    field: str,
    value: object,
) -> None:
    output_dir = tmp_path / "applications" / "web_search" / "outputs"
    report_path = output_dir / (
        "us_after_close_a_share_signal_2026-06-17_to_2026-06-18.md"
    )
    report_path.parent.mkdir(parents=True)
    report_path.write_text("report", encoding="utf-8")
    run_dir = tmp_path / ".agentloom" / "runs" / "web_search" / "run_previous"
    run = _run_info(run_dir)
    _prepare_completed_run(run)
    previous = {
        "now_utc": NOW_UTC,
        "status": "completed",
        "returncode": 0,
        "timed_out": False,
        "force_killed": False,
        "parent_interrupted": False,
        "protocol_errors": [],
        "audit": {"ok": True},
        "report_path": report_path.relative_to(tmp_path).as_posix(),
        **run,
        field: value,
    }
    monkeypatch.setattr(batch, "REPO_ROOT", tmp_path)

    assert batch._historical_skip(
        NOW_UTC,
        _context(),
        report_path,
        {NOW_UTC: previous},
    ) is None


def test_legacy_sidecar_result_without_run_identity_is_not_skipped(
    tmp_path: Path,
) -> None:
    report_path = tmp_path / "applications" / "web_search" / "outputs" / "report.md"
    previous = {
        "now_utc": NOW_UTC,
        "status": "completed",
        "audit": {"ok": True},
        "report_path": report_path.as_posix(),
        "log_path": "applications/web_search/outputs/validation_logs/old.log",
    }

    skipped = batch._historical_skip(
        NOW_UTC,
        _context(),
        report_path,
        {NOW_UTC: previous},
    )

    assert skipped is None


def test_main_manifest_contains_canonical_identity_and_no_duplicate_log_dir(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    output_dir = tmp_path / "applications" / "web_search" / "outputs"
    manifest_path = output_dir / "validation_us_after_close_batch.json"
    run_dir = tmp_path / ".agentloom" / "runs" / "web_search" / "run_test"
    result = {
        "now_utc": NOW_UTC,
        "returncode": 0,
        "timed_out": False,
        "force_killed": False,
        "parent_interrupted": False,
        "skipped": False,
        "status": "completed",
        "application_id": "web_search",
        "task_id": "task_test",
        "run_id": "run_test",
        "run_dir": str(run_dir),
        "manifest_path": str(run_dir / "manifest.json"),
        "log_path": str(run_dir / "logs" / "runtime.log"),
        "context": batch._context_payload(_context()),
        "report_path": "applications/web_search/outputs/report.md",
        "audit": {"ok": True},
        "protocol_errors": [],
    }

    monkeypatch.setattr(batch, "OUTPUT_DIR", output_dir)
    monkeypatch.setattr(batch, "MANIFEST_PATH", manifest_path)
    monkeypatch.setattr(batch, "_run_one", lambda *_args, **_kwargs: result)

    assert batch.main(["--now", NOW_UTC]) == 0

    payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert payload[0]["task_id"] == "task_test"
    assert payload[0]["run_id"] == "run_test"
    assert payload[0]["manifest_path"] == str(run_dir / "manifest.json")
    assert payload[0]["log_path"] == str(run_dir / "logs" / "runtime.log")
    assert not (output_dir / "validation_logs").exists()


def test_main_parent_interrupt_saves_current_receipt_and_stops_later_runs(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    output_dir = tmp_path / "outputs"
    manifest_path = output_dir / "validation_us_after_close_batch.json"
    calls: list[str] = []
    result = {
        "now_utc": NOW_UTC,
        "returncode": 130,
        "timed_out": False,
        "force_killed": False,
        "parent_interrupted": True,
        "skipped": False,
        "status": "interrupted",
        "application_id": "web_search",
        "task_id": "task_test",
        "run_id": "run_test",
        "run_dir": str(tmp_path / "run_test"),
        "manifest_path": str(tmp_path / "run_test" / "manifest.json"),
        "log_path": str(tmp_path / "run_test" / "logs" / "runtime.log"),
        "context": batch._context_payload(_context()),
        "report_path": "report.md",
        "audit": None,
        "protocol_errors": [],
    }

    def run_one(now_utc, *_args, **_kwargs):
        calls.append(now_utc)
        return result

    monkeypatch.setattr(batch, "OUTPUT_DIR", output_dir)
    monkeypatch.setattr(batch, "MANIFEST_PATH", manifest_path)
    monkeypatch.setattr(batch, "_run_one", run_one)

    exit_code = batch.main(
        ["--now", NOW_UTC, "--now", "2026-06-19T00:30:00Z"]
    )

    assert exit_code == 130
    assert calls == [NOW_UTC]
    payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert payload[0]["run_id"] == "run_test"
    assert payload[0]["parent_interrupted"] is True
