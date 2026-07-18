from __future__ import annotations

import argparse
import json
import os
import signal
import subprocess
import sys
import time
from dataclasses import asdict
from datetime import datetime
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from applications.web_search.agent_tools.market_time import get_market_time_context  # noqa: E402
from applications.web_search.scripts.audit_us_after_close_reports import audit_report  # noqa: E402
from applications.web_search.scripts.audit_us_after_close_run_logs import audit_log  # noqa: E402

WORKFLOW = "applications/web_search/workflows/us_after_close_a_share_signal_agent.yaml"
OUTPUT_DIR = REPO_ROOT / "applications/web_search/outputs"
MANIFEST_PATH = OUTPUT_DIR / "validation_us_after_close_batch.json"
INTERRUPT_GRACE_SECONDS = 30
KILL_DRAIN_SECONDS = 5
_TERMINAL_EVENTS = {"run.completed", "run.failed", "run.interrupted"}
_RUN_FIELDS = (
    "application_id",
    "task_id",
    "run_id",
    "run_dir",
    "manifest_path",
    "log_path",
)

DEFAULT_NOWS_UTC = [
    "2026-06-18T00:30:00Z",
    "2026-06-19T00:30:00Z",
    "2026-06-22T00:30:00Z",
    "2026-06-23T00:30:00Z",
    "2026-06-24T00:30:00Z",
    "2026-06-25T00:30:00Z",
    "2026-06-26T00:30:00Z",
    "2026-06-29T00:30:00Z",
    "2026-06-30T00:30:00Z",
    "2026-07-01T00:30:00Z",
    "2026-07-02T11:00:00Z",
]

TASK = """
按 workflow 的搜索优先工具流生成一次报告：先取市场时间，再做最低搜索矩阵，并按 workflow 继续补充搜索/抽取直到证据足够。
最低搜索矩阵是硬前置：即使第一轮 MarketDiscovery 已经搜到榜单，也必须先完成第二轮 DriverDiscovery batch_search；第一次 extract 之前必须已经有两次有效 batch_search。
不要为了省搜索调用牺牲准确性；但 extract 是高成本全文阅读，必须遵守 workflow 的 extra_search_round/extract_count 上限，达到停止条件或计数上限后立刻写报告。
不要使用 workflow 禁止的工具。最终 final_answer 只回复报告路径。
"""


def _context(now_utc: str) -> dict[str, object]:
    return json.loads(get_market_time_context(now_utc))


def _context_payload(context: dict[str, object]) -> dict[str, object]:
    return {
        "us_trading_day": context["query_terms"]["us_trading_day_iso"],
        "a_share_prediction_date": context["query_terms"][
            "a_share_prediction_date_iso"
        ],
        "news_window_start_asia_shanghai": context["news_window"][
            "start_asia_shanghai"
        ],
        "news_window_end_asia_shanghai": context["news_window"][
            "end_asia_shanghai"
        ],
    }


def _report_path(context: dict[str, object]) -> Path:
    us_day = context["query_terms"]["us_trading_day_iso"]
    a_day = context["query_terms"]["a_share_prediction_date_iso"]
    return OUTPUT_DIR / f"us_after_close_a_share_signal_{us_day}_to_{a_day}.md"


def _report_snapshot(path: Path) -> tuple[int, int, int, int] | None:
    try:
        metadata = path.stat()
    except FileNotFoundError:
        return None
    return (
        metadata.st_ino,
        metadata.st_size,
        metadata.st_mtime_ns,
        metadata.st_ctime_ns,
    )


def _event_run_info(event: dict[str, object]) -> dict[str, object] | None:
    run = event.get("run")
    if not isinstance(run, dict):
        return None
    required = _RUN_FIELDS[:-1]
    if not all(isinstance(run.get(field), str) and run[field] for field in required):
        return None
    log_path = run.get("log_path")
    if log_path is not None and (not isinstance(log_path, str) or not log_path):
        return None
    return {field: run.get(field) for field in _RUN_FIELDS}


def _validate_run_info(run: dict[str, object]) -> list[str]:
    run_dir = Path(str(run["run_dir"]))
    manifest_path = Path(str(run["manifest_path"]))
    raw_log_path = run.get("log_path")
    log_path = Path(str(raw_log_path)) if raw_log_path is not None else None
    paths = [run_dir, manifest_path]
    if log_path is not None:
        paths.append(log_path)

    errors: list[str] = []
    if not all(
        path.is_absolute() and Path(os.path.abspath(path)) == path
        for path in paths
    ):
        errors.append("run paths must be absolute and normalized")
    if run_dir.name != run["run_id"]:
        errors.append("run_dir does not match run_id")
    if manifest_path != run_dir / "manifest.json":
        errors.append("manifest_path is not canonical")
    if log_path is not None and log_path != run_dir / "logs" / "runtime.log":
        errors.append("log_path is not canonical")
    return errors


def _parse_lifecycle_events(
    stdout: str,
) -> tuple[list[dict[str, object]], list[str]]:
    events: list[dict[str, object]] = []
    errors: list[str] = []
    for line_number, raw_line in enumerate(stdout.splitlines(), start=1):
        if not raw_line.strip():
            continue
        try:
            event = json.loads(raw_line)
        except json.JSONDecodeError:
            errors.append(f"stdout line {line_number} is not JSON")
            continue
        if not isinstance(event, dict):
            errors.append(f"stdout line {line_number} is not an object")
            continue
        if event.get("schema_version") != 1:
            errors.append(f"stdout line {line_number} has unknown schema")
            continue
        if not isinstance(event.get("event"), str):
            errors.append(f"stdout line {line_number} has no event name")
            continue
        events.append(event)

    rejected = [event for event in events if event.get("event") == "run.rejected"]
    if rejected:
        if len(events) != 1:
            errors.append("run.rejected must be the only lifecycle event")
        return events, errors

    started = [event for event in events if event.get("event") == "run.started"]
    terminal = [
        event for event in events if event.get("event") in _TERMINAL_EVENTS
    ]
    if len(started) != 1:
        errors.append("expected exactly one run.started")
    if len(terminal) != 1:
        errors.append("expected exactly one terminal event")
    if len(started) == 1 and events and events[0] is not started[0]:
        errors.append("run.started must be first")
    if len(terminal) == 1 and events and events[-1] is not terminal[0]:
        errors.append("terminal event must be last")
    if len(events) > 2:
        errors.append("unexpected lifecycle event")

    if len(started) == 1:
        started_run = _event_run_info(started[0])
        if started_run is None:
            errors.append("run.started has incomplete RunInfo")
        else:
            errors.extend(_validate_run_info(started_run))
            if len(terminal) == 1:
                terminal_run = _event_run_info(terminal[0])
                if terminal_run != started_run:
                    errors.append("terminal RunInfo differs from run.started")
    return events, errors


def _run_identity(events: list[dict[str, object]]) -> dict[str, object]:
    for event in events:
        run = _event_run_info(event)
        if run is not None:
            return run
    return {field: None for field in _RUN_FIELDS}


def _terminal_event(
    events: list[dict[str, object]],
) -> dict[str, object] | None:
    for event in reversed(events):
        if event.get("event") in {*_TERMINAL_EVENTS, "run.rejected"}:
            return event
    return None


def _issues(issues: list[object]) -> list[dict[str, object]]:
    return [asdict(issue) for issue in issues]


def _audit_payload(
    report_path: Path,
    log_path: Path | None,
) -> dict[str, object] | None:
    if not report_path.is_file() or log_path is None or not log_path.is_file():
        return None
    report_audit = audit_report(report_path)
    log_audit = audit_log(log_path)
    return {
        "ok": report_audit.ok and log_audit.ok,
        "row_count": report_audit.row_count,
        "issues": _issues(report_audit.issues),
        "log": {
            "ok": log_audit.ok,
            "batch_search_calls": log_audit.batch_search_calls,
            "planned_search_results": log_audit.planned_search_results,
            "extract_calls": log_audit.extract_calls,
            "quote_extract_calls": log_audit.quote_extract_calls,
            "issues": _issues(log_audit.issues),
        },
    }


def _validate_completed_manifest(
    run: dict[str, object],
) -> list[str]:
    manifest_path = Path(str(run["manifest_path"]))
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return ["completed run manifest is missing or invalid"]
    if not isinstance(manifest, dict):
        return ["completed run manifest is not an object"]

    errors: list[str] = []
    if manifest.get("schema_version") != 1:
        errors.append("manifest schema is not supported")
    for field in ("application_id", "task_id", "run_id"):
        if manifest.get(field) != run[field]:
            errors.append(f"manifest {field} differs from lifecycle")
    if manifest.get("status") != "completed":
        errors.append("manifest status is not completed")
    return errors


def _load_historical_results() -> dict[str, dict[str, object]]:
    try:
        payload = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    if not isinstance(payload, list):
        return {}
    results: dict[str, dict[str, object]] = {}
    for item in payload:
        if isinstance(item, dict) and isinstance(item.get("now_utc"), str):
            results[item["now_utc"]] = item
    return results


def _historical_skip(
    now_utc: str,
    context: dict[str, object],
    report_path: Path,
    historical_results: dict[str, dict[str, object]],
) -> dict[str, object] | None:
    previous = historical_results.get(now_utc)
    if not isinstance(previous, dict):
        return None
    if not _result_ok(previous):
        return None

    run = {field: previous.get(field) for field in _RUN_FIELDS}
    if any(run[field] is None for field in _RUN_FIELDS[:-1]):
        return None
    expected_report = report_path.relative_to(REPO_ROOT).as_posix()
    if previous.get("report_path") != expected_report:
        return None
    if _validate_run_info(run) or _validate_completed_manifest(run):
        return None
    log_path = Path(str(run["log_path"])) if run["log_path"] is not None else None
    audit = _audit_payload(report_path, log_path)
    if audit is None or not audit["ok"]:
        return None

    return {
        "now_utc": now_utc,
        "started_at": None,
        "finished_at": None,
        "duration_seconds": 0.0,
        "returncode": 0,
        "timed_out": False,
        "force_killed": False,
        "parent_interrupted": False,
        "skipped": True,
        "status": "completed",
        **run,
        "context": _context_payload(context),
        "report_path": expected_report,
        "report_exists": True,
        "audit": audit,
        "protocol_errors": [],
        "error": None,
        "phase": None,
    }


def _signal_process_group(
    process: subprocess.Popen[str],
    signal_number: int,
) -> None:
    try:
        os.killpg(process.pid, signal_number)
    except ProcessLookupError:
        return


def _stdout_from_timeout(exc: subprocess.TimeoutExpired) -> str:
    output = exc.stdout if exc.stdout is not None else exc.output
    if isinstance(output, bytes):
        return output.decode("utf-8", errors="replace")
    return output or ""


def _close_process_pipes(process: subprocess.Popen[str]) -> None:
    for name in ("stdout", "stderr"):
        stream = getattr(process, name, None)
        if stream is not None:
            try:
                stream.close()
            except OSError:
                pass


def _force_kill_and_drain(
    process: subprocess.Popen[str],
    *,
    partial_stdout: str = "",
    parent_interrupted: bool = False,
) -> tuple[str, bool, bool]:
    _signal_process_group(process, signal.SIGKILL)
    try:
        stdout, _stderr = process.communicate(timeout=KILL_DRAIN_SECONDS)
        return stdout or partial_stdout, True, parent_interrupted
    except subprocess.TimeoutExpired as exc:
        stdout = _stdout_from_timeout(exc) or partial_stdout
    except KeyboardInterrupt:
        stdout = partial_stdout
        parent_interrupted = True
    _close_process_pipes(process)
    try:
        process.wait(timeout=KILL_DRAIN_SECONDS)
    except KeyboardInterrupt:
        parent_interrupted = True
    except (AttributeError, subprocess.TimeoutExpired):
        pass
    return stdout, True, parent_interrupted


def _interrupt_then_drain(
    process: subprocess.Popen[str],
    *,
    partial_stdout: str = "",
    parent_interrupted: bool = False,
) -> tuple[str, bool, bool]:
    _signal_process_group(process, signal.SIGINT)
    try:
        stdout, _stderr = process.communicate(timeout=INTERRUPT_GRACE_SECONDS)
        return stdout or partial_stdout, False, parent_interrupted
    except subprocess.TimeoutExpired as exc:
        return _force_kill_and_drain(
            process,
            partial_stdout=_stdout_from_timeout(exc) or partial_stdout,
            parent_interrupted=parent_interrupted,
        )
    except KeyboardInterrupt:
        return _force_kill_and_drain(
            process,
            partial_stdout=partial_stdout,
            parent_interrupted=True,
        )


def _communicate(
    process: subprocess.Popen[str],
    timeout_seconds: int,
) -> tuple[str, bool, bool, bool]:
    try:
        stdout, _stderr = process.communicate(timeout=timeout_seconds)
        return stdout or "", False, False, False
    except subprocess.TimeoutExpired as exc:
        stdout, force_killed, parent_interrupted = _interrupt_then_drain(
            process,
            partial_stdout=_stdout_from_timeout(exc),
        )
        return stdout, True, force_killed, parent_interrupted
    except KeyboardInterrupt:
        stdout, force_killed, parent_interrupted = _interrupt_then_drain(
            process,
            parent_interrupted=True,
        )
        return stdout, False, force_killed, parent_interrupted


def _run_one(
    now_utc: str,
    timeout_seconds: int,
    skip_existing_ok: bool,
    historical_results: dict[str, dict[str, object]] | None = None,
) -> dict[str, object]:
    context = _context(now_utc)
    report_path = _report_path(context)
    previous_report_snapshot = _report_snapshot(report_path)
    if skip_existing_ok:
        skipped = _historical_skip(
            now_utc,
            context,
            report_path,
            (
                _load_historical_results()
                if historical_results is None
                else historical_results
            ),
        )
        if skipped is not None:
            return skipped

    env = os.environ.copy()
    env["AGENTLOOM_WEB_SEARCH_NOW_UTC"] = now_utc
    command = [
        sys.executable,
        "-m",
        "src.__main__",
        "run",
        WORKFLOW,
        "--task",
        TASK,
        "--output-format",
        "jsonl",
    ]

    started_at = datetime.now().astimezone().isoformat()
    started_monotonic = time.monotonic()
    process_error: str | None = None
    stdout = ""
    timed_out = False
    force_killed = False
    parent_interrupted = False
    returncode = 1
    try:
        process = subprocess.Popen(
            command,
            cwd=REPO_ROOT,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            start_new_session=True,
        )
        try:
            stdout, timed_out, force_killed, parent_interrupted = _communicate(
                process,
                timeout_seconds,
            )
        except KeyboardInterrupt:
            if process.returncode is None:
                stdout, force_killed, _cleanup_interrupted = _interrupt_then_drain(
                    process,
                    partial_stdout=stdout,
                    parent_interrupted=True,
                )
            parent_interrupted = True
        except BaseException:
            if process.returncode is None:
                _interrupt_then_drain(process, partial_stdout=stdout)
            raise
        returncode = process.returncode if process.returncode is not None else 1
    except OSError as exc:
        process_error = f"cannot start CLI: {exc}"

    finished_at = datetime.now().astimezone().isoformat()
    duration_seconds = round(
        max(0.0, time.monotonic() - started_monotonic),
        6,
    )
    events, protocol_errors = _parse_lifecycle_events(stdout)
    if process_error is not None:
        protocol_errors.append(process_error)
    identity = _run_identity(events)
    terminal = _terminal_event(events)
    terminal_name = terminal.get("event") if terminal is not None else None

    if force_killed:
        status = "killed"
    elif timed_out and terminal_name is None:
        status = "timed_out"
    elif isinstance(terminal_name, str):
        status = terminal_name.removeprefix("run.")
    else:
        status = "protocol_error" if protocol_errors else "process_error"

    log_value = identity.get("log_path")
    log_path = Path(str(log_value)) if log_value is not None else None
    audit = None
    if terminal_name == "run.completed":
        expected_output = report_path.relative_to(REPO_ROOT).as_posix()
        protocol_errors.extend(_validate_completed_manifest(identity))
        if terminal.get("output") != expected_output:
            protocol_errors.append(
                "completed output is not the canonical report path"
            )
        if not report_path.is_file():
            protocol_errors.append("canonical report is missing")
        elif (
            previous_report_snapshot is not None
            and _report_snapshot(report_path) == previous_report_snapshot
        ):
            protocol_errors.append("canonical report was not refreshed")
        audit = _audit_payload(report_path, log_path)

    return {
        "now_utc": now_utc,
        "started_at": started_at,
        "finished_at": finished_at,
        "duration_seconds": duration_seconds,
        "returncode": returncode,
        "timed_out": timed_out,
        "force_killed": force_killed,
        "parent_interrupted": parent_interrupted,
        "skipped": False,
        "status": status,
        **identity,
        "context": _context_payload(context),
        "report_path": report_path.relative_to(REPO_ROOT).as_posix(),
        "report_exists": report_path.is_file(),
        "audit": audit,
        "protocol_errors": protocol_errors,
        "error": terminal.get("error") if terminal is not None else process_error,
        "phase": terminal.get("phase") if terminal is not None else None,
    }


def _write_results(results: list[dict[str, object]]) -> None:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    temporary = MANIFEST_PATH.with_suffix(".tmp")
    temporary.write_text(
        json.dumps(results, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    os.replace(temporary, MANIFEST_PATH)


def _result_ok(result: dict[str, object]) -> bool:
    audit = result.get("audit")
    return bool(
        not result.get("timed_out")
        and not result.get("force_killed")
        and not result.get("parent_interrupted")
        and result.get("returncode") == 0
        and result.get("status") == "completed"
        and isinstance(audit, dict)
        and audit.get("ok")
        and not result.get("protocol_errors")
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--now",
        action="append",
        help="UTC ISO timestamp. Repeatable.",
    )
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--timeout-seconds", type=int, default=900)
    parser.add_argument("--skip-existing-ok", action="store_true")
    args = parser.parse_args(argv)

    now_values = args.now or DEFAULT_NOWS_UTC
    if args.limit is not None:
        now_values = now_values[: args.limit]

    historical_results = (
        _load_historical_results() if args.skip_existing_ok else None
    )
    results: list[dict[str, object]] = []
    try:
        for now_utc in now_values:
            result = _run_one(
                now_utc,
                args.timeout_seconds,
                args.skip_existing_ok,
                historical_results,
            )
            results.append(result)
            _write_results(results)
            if result.get("parent_interrupted"):
                return 130
            status = "SKIP" if result["skipped"] else (
                "PASS" if _result_ok(result) else "FAIL"
            )
            print(
                f"{status} {now_utc} -> "
                f"{result['context']['us_trading_day']} to "
                f"{result['context']['a_share_prediction_date']} "
                f"report={result['report_path']} log={result['log_path']}",
                flush=True,
            )
    except KeyboardInterrupt:
        _write_results(results)
        return 130

    _write_results(results)
    return 0 if all(_result_ok(result) for result in results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
