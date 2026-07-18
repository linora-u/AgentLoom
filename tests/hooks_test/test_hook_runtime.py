from __future__ import annotations

import shlex
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path

import pytest

from src.lib.runtime import process as process_runtime
from src.lib.smolagents.hooks import (
    HOOK_EVENT_NAMES,
    HookConfigLayer,
    HookEvent,
    HookHandler,
    HookPlan,
    HookPlanCompiler,
    HookResult,
    HookRun,
)
from src.lib.smolagents.hooks.tool_shim import inject_hooks
from src.lib.smolagents.tools.tools import tool
from src.trace import bind_explicit_execution_context, capture_explicit_execution_context


def _run(*handlers: HookHandler) -> HookRun:
    return HookRun(
        HookPlan(tuple(handlers)),
        local_run_id="local-run",
        root_run_id="root-run",
    )


def test_event_catalog_contains_only_reachable_events() -> None:
    assert HOOK_EVENT_NAMES == [
        "PreToolUse",
        "PostToolUse",
        "PostToolUseFailure",
        "SessionStart",
        "SessionEnd",
        "Stop",
        "StopFailure",
        "SubagentStart",
        "SubagentStop",
        "TaskCreated",
        "TaskCompleted",
    ]


def test_pre_tool_handlers_transform_input_in_registration_order() -> None:
    seen: list[dict[str, int]] = []

    def add_a(context):
        return HookResult(decision="modify", modified_input={"a": 10})

    def derive_b(context):
        seen.append(dict(context.tool_input))
        return HookResult(
            decision="modify",
            modified_input={"a": 99, "b": context.tool_input["a"] + 1},
        )

    run = _run(
        HookHandler(HookEvent.PRE_TOOL_USE, "*", add_a, source="first"),
        HookHandler(HookEvent.PRE_TOOL_USE, "*", derive_b, source="second"),
    )

    result = run.dispatch(HookEvent.PRE_TOOL_USE, "demo", {"seed": 1})

    assert seen == [{"seed": 1, "a": 10}]
    assert result.decision == "modify"
    assert result.modified_input == {"seed": 1, "a": 99, "b": 11}


def test_gate_block_short_circuits_later_handlers() -> None:
    called: list[str] = []

    def block(_context):
        return HookResult(decision="block", reason="denied")

    def too_late(_context):
        called.append("too-late")
        return HookResult()

    run = _run(
        HookHandler(HookEvent.PRE_TOOL_USE, "*", block),
        HookHandler(HookEvent.PRE_TOOL_USE, "*", too_late),
    )

    result = run.dispatch(HookEvent.PRE_TOOL_USE, "demo", {})

    assert result.decision == "block"
    assert result.reason == "denied"
    assert called == []


def test_gate_exception_fails_closed_but_observer_exception_fails_open() -> None:
    observed: list[str] = []

    def explode(_context):
        raise RuntimeError("broken")

    def observe(_context):
        observed.append("continued")
        return HookResult(agent_context="next turn", user_message="visible")

    gate = _run(HookHandler(HookEvent.PRE_TOOL_USE, "*", explode, source="gate"))
    gate_result = gate.dispatch(HookEvent.PRE_TOOL_USE, "demo", {})

    observer = _run(
        HookHandler(HookEvent.POST_TOOL_USE, "*", explode, source="broken-observer"),
        HookHandler(HookEvent.POST_TOOL_USE, "*", observe, source="observer"),
    )
    observer_result = observer.dispatch(
        HookEvent.POST_TOOL_USE,
        "demo",
        {},
        tool_response={"result": "ok"},
    )

    assert gate_result.decision == "block"
    assert "gate" in (gate_result.reason or "")
    assert observer_result.decision == "allow"
    assert observed == ["continued"]
    assert observer.consume_pending_agent_context() == ["next turn"]
    assert observer.consume_pending_user_messages() == ["visible"]


def test_observer_cannot_block_or_modify_completed_work() -> None:
    run = _run(
        HookHandler(
            HookEvent.POST_TOOL_USE,
            "*",
            lambda _context: HookResult(
                decision="modify",
                modified_input={"hidden": True},
            ),
            source="invalid-observer",
        )
    )

    result = run.dispatch(
        HookEvent.POST_TOOL_USE,
        "demo",
        {},
        tool_response={"result": "original"},
    )

    assert result.decision == "allow"
    assert result.modified_input is None
    assert result.telemetry["hook_errors"][0]["source"] == "invalid-observer"


def test_modified_input_requires_pre_tool_modify_decision() -> None:
    run = _run(
        HookHandler(
            HookEvent.PRE_TOOL_USE,
            "*",
            lambda _context: HookResult(
                decision="allow",
                modified_input={"hidden": True},
            ),
            source="invalid-gate",
        )
    )

    result = run.dispatch(HookEvent.PRE_TOOL_USE, "demo", {})

    assert result.decision == "block"
    assert "decision=modify" in (result.reason or "")


def _add_tool():
    @tool
    def add(a: int, b: int) -> int:
        """Add two integers.

        Args:
            a: First integer.
            b: Second integer.
        """

        return a + b

    return add


def _bind(run: HookRun):
    current = capture_explicit_execution_context()
    return bind_explicit_execution_context(replace(current, hook_run=run))


def test_hooked_tool_requires_explicit_hook_run() -> None:
    add = inject_hooks(_add_tool())

    with pytest.raises(RuntimeError, match="HookRun"):
        add(a=1, b=2)


def test_real_tool_wrapper_applies_transform_and_queues_observer_effect() -> None:
    delivered: list[str] = []
    run = _run(
        HookHandler(
            HookEvent.PRE_TOOL_USE,
            "add",
            lambda _context: HookResult(
                decision="modify",
                modified_input={"a": 10, "b": 20},
            ),
        ),
        HookHandler(
            HookEvent.POST_TOOL_USE,
            "add",
            lambda _context: HookResult(
                agent_context="calculation complete",
                user_message="calculation visible",
            ),
        ),
    )
    run.set_user_message_sink(delivered.append)
    add = inject_hooks(_add_tool())

    with _bind(run):
        result = add(a=1, b=2)

    assert result == 30
    assert run.consume_pending_agent_context() == ["calculation complete"]
    assert delivered == ["calculation visible"]
    assert run.consume_pending_user_messages() == []


def test_concurrent_tool_calls_do_not_share_hook_effect_queues() -> None:
    add = inject_hooks(_add_tool())

    def invoke(label: str) -> tuple[int, list[str]]:
        run = _run(
            HookHandler(
                HookEvent.POST_TOOL_USE,
                "add",
                lambda _context: HookResult(agent_context=label),
            )
        )
        with _bind(run):
            value = add(a=1, b=2)
        return value, run.consume_pending_agent_context()

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(invoke, ("run-a", "run-b")))

    assert sorted(results) == [(3, ["run-a"]), (3, ["run-b"])]


def _compiled_shell_handler(
    tmp_path: Path,
    command: str,
    *,
    hook_id: str,
    timeout: float,
) -> HookHandler:
    layer = HookConfigLayer(
        name="test",
        config={
            "hooks": {
                "PreToolUse": [
                    {
                        "id": hook_id,
                        "command": command,
                        "timeout": timeout,
                    }
                ]
            }
        },
        agent_root=tmp_path,
        source_path=tmp_path / "agent.yaml",
        priority=0,
    )
    return HookPlanCompiler().compile([layer]).handlers[0]


def test_shell_timeout_kills_descendants_before_they_can_write(tmp_path: Path) -> None:
    sentinel = tmp_path / "late-side-effect"
    command = f"(sleep 0.4; printf late > {sentinel}) & sleep 5"
    shell_handler = _compiled_shell_handler(
        tmp_path,
        command,
        hook_id="timeout-fixture",
        timeout=0.1,
    )
    run = _run(shell_handler)

    result = run.dispatch(HookEvent.PRE_TOOL_USE, "demo", {})
    time.sleep(0.6)

    assert result.decision == "block"
    assert "timed out" in (result.reason or "")
    assert not sentinel.exists()


def test_shell_timeout_terminates_group_before_global_process_scan(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sentinel = tmp_path / "late-side-effect-during-scan"
    command = f"(sleep 0.4; printf late > {sentinel}) & sleep 5"
    shell_handler = _compiled_shell_handler(
        tmp_path,
        command,
        hook_id="slow-process-scan-timeout-fixture",
        timeout=0.1,
    )
    original_marked_processes = process_runtime._marked_processes
    calls = 0

    def delayed_first_scan(token: str, *, exclude: set[int]):
        nonlocal calls
        calls += 1
        if calls == 1:
            time.sleep(0.45)
        return original_marked_processes(token, exclude=exclude)

    monkeypatch.setattr(process_runtime, "_marked_processes", delayed_first_scan)
    run = _run(shell_handler)

    result = run.dispatch(HookEvent.PRE_TOOL_USE, "demo", {})
    time.sleep(0.6)

    assert calls > 0
    assert result.decision == "block"
    assert "timed out" in (result.reason or "")
    assert not sentinel.exists()


def test_shell_timeout_kills_descendant_that_escapes_process_group(tmp_path: Path) -> None:
    sentinel = tmp_path / "escaped-side-effect"
    escaped_script = (
        "import pathlib,time; "
        "time.sleep(0.4); "
        f"pathlib.Path({str(sentinel)!r}).write_text('late')"
    )
    parent_script = (
        "import subprocess,sys,time; "
        f"subprocess.Popen([sys.executable, '-c', {escaped_script!r}], start_new_session=True); "
        "time.sleep(5)"
    )
    command = f"{shlex.quote(sys.executable)} -c {shlex.quote(parent_script)}"
    shell_handler = _compiled_shell_handler(
        tmp_path,
        command,
        hook_id="escaped-timeout-fixture",
        timeout=0.1,
    )
    run = _run(shell_handler)

    result = run.dispatch(HookEvent.PRE_TOOL_USE, "demo", {})
    time.sleep(0.6)

    assert result.decision == "block"
    assert "timed out" in (result.reason or "")
    assert not sentinel.exists()


def test_shell_timeout_tracks_escapee_descendants_spawned_during_cleanup(tmp_path: Path) -> None:
    sentinel = tmp_path / "cleanup-side-effect"
    escaped_script = (
        "import pathlib,time; "
        "time.sleep(0.25); "
        f"pathlib.Path({str(sentinel)!r}).write_text('late')"
    )
    escapee_script = (
        "import signal,subprocess,sys,threading,time; "
        "signal.signal(signal.SIGTERM, signal.SIG_IGN); "
        "threading.Timer("
        "0.12, "
        f"lambda: subprocess.Popen([sys.executable, '-c', {escaped_script!r}], "
        "start_new_session=True)"
        ").start(); "
        "time.sleep(5)"
    )
    parent_script = (
        "import subprocess,sys,time; "
        f"subprocess.Popen([sys.executable, '-c', {escapee_script!r}], "
        "start_new_session=True); "
        "time.sleep(5)"
    )
    command = f"{shlex.quote(sys.executable)} -c {shlex.quote(parent_script)}"
    shell_handler = _compiled_shell_handler(
        tmp_path,
        command,
        hook_id="term-grace-timeout-fixture",
        timeout=0.05,
    )
    run = _run(shell_handler)

    result = run.dispatch(HookEvent.PRE_TOOL_USE, "demo", {})
    time.sleep(0.7)

    assert result.decision == "block"
    assert "timed out" in (result.reason or "")
    assert not sentinel.exists()


def test_shell_timeout_kills_reparented_double_fork_descendant(tmp_path: Path) -> None:
    sentinel = tmp_path / "double-fork-side-effect"
    final_script = (
        "import pathlib,time; "
        "time.sleep(0.4); "
        f"pathlib.Path({str(sentinel)!r}).write_text('late')"
    )
    launcher_script = (
        "import subprocess,sys; "
        f"subprocess.Popen([sys.executable, '-c', {final_script!r}], "
        "start_new_session=True)"
    )
    parent_script = (
        "import subprocess,sys,time; "
        f"subprocess.Popen([sys.executable, '-c', {launcher_script!r}], "
        "start_new_session=True); "
        "time.sleep(5)"
    )
    command = f"{shlex.quote(sys.executable)} -c {shlex.quote(parent_script)}"
    shell_handler = _compiled_shell_handler(
        tmp_path,
        command,
        hook_id="double-fork-timeout-fixture",
        timeout=0.1,
    )
    run = _run(shell_handler)

    result = run.dispatch(HookEvent.PRE_TOOL_USE, "demo", {})
    time.sleep(0.7)

    assert result.decision == "block"
    assert "timed out" in (result.reason or "")
    assert not sentinel.exists()
