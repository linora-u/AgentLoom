"""The prepared-tool handoff must not grant permission or execute prematurely."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from threading import Barrier
from unittest.mock import patch

import pytest
from agentloom.runtime.hooks import HookEvent, HookHandler, HookPlan, HookResult, HookRun
from agentloom.runtime.tool_gateway import AgentLoomToolGateway, PreparedToolCall, ToolGateway, bind_tool
from agentloom.runtime.tool_protocol import ToolCallRecord
from agentloom.runtime.trace import (
    ExplicitExecutionContext,
    bind_explicit_execution_context,
    capture_explicit_execution_context,
)


def _run(*handlers, config=None, project_root=None):
    return HookRun(
        HookPlan(tuple(handlers)),
        local_run_id="local",
        root_run_id="root",
        agent_config=config or {},
        project_root=str(project_root) if project_root is not None else None,
    )


def _context(run):
    return ExplicitExecutionContext(
        task_id="task", sub_task_id=None, agent_id="agent", agent_name="agent",
        agent_config=run.agent_config, skill_catalog=None, hook_run=run,
        runtime_agent_path="agent", root_run_id=run.root_run_id, local_run_id=run.local_run_id,
    )


def _prepare(gateway, arguments):
    result = gateway.prepare(call_id="provider-call", tool_name="echo", arguments=arguments)
    assert isinstance(result, PreparedToolCall)
    return result


def test_prepare_repairs_input_but_delays_guard_history_observer_and_setup():
    events = []

    def echo(count: int) -> str:
        events.append(("executor", count))
        return str(count)

    def repair(context):
        events.append(("pre", context.tool_input))
        return HookResult(decision="modify", modified_input={"count": "7"})

    def guard(context):
        events.append(("guard", context.tool_input))
        return HookResult()

    def history(**kwargs):
        events.append(("history", kwargs["tool_input"]))

    def observe(context):
        events.append(("observer", context.tool_input))

    def post(context):
        events.append(("post", context.tool_input))
        return HookResult()

    run = _run(
        HookHandler(HookEvent.PRE_TOOL_USE, "echo", repair),
        HookHandler(HookEvent.POST_TOOL_USE, "echo", post),
    )
    binding = replace(bind_tool(echo), setup=lambda: events.append(("setup", None)))
    gateway = AgentLoomToolGateway([binding])
    assert isinstance(gateway, ToolGateway)
    with (
        bind_explicit_execution_context(_context(run)),
        patch("agentloom.runtime.hooks.path_validators.enforce_core_tool_guard", side_effect=guard),
        patch("agentloom.runtime.checkpoint.file_history_hook.record_active_file_history", side_effect=history),
        patch("agentloom.runtime.tool_gateway._observe_final_tool_input", side_effect=observe),
    ):
        prepared = _prepare(gateway, {"count": "malformed"})
        assert prepared.call_id == "provider-call"
        assert prepared.tool_name == "echo"
        assert dict(prepared.arguments) == {"count": 7}
        assert events == [("pre", {"count": "malformed"})]
        assert run.tool_outcomes_snapshot() == ()

        result = gateway.execute_prepared(prepared)

    assert result.status == "completed"
    assert result.input == {"count": 7}
    assert result.output == "7"
    assert [event[0] for event in events] == ["pre", "guard", "history", "observer", "setup", "executor", "post"]
    assert all(value == {"count": 7} for name, value in events if name in {"guard", "history", "observer", "post"})
    assert len(run.tool_outcomes_snapshot()) == 1


def test_execution_checks_current_guard_after_prepare(tmp_path):
    target = tmp_path / "result.txt"

    def echo(file_path: str) -> str:
        target.write_text("executed", encoding="utf-8")
        return file_path

    run = _run(config={"tool_access_control": {"path_validation": [{"tools": ["echo"]}]}}, project_root=tmp_path)
    gateway = AgentLoomToolGateway([bind_tool(echo)])
    with bind_explicit_execution_context(_context(run)):
        prepared = _prepare(gateway, {"file_path": str(target)})
        # Policy can change while the runtime persists its assistant entry.
        run.agent_config["tool_access_control"]["path_validation"][0]["exclude_paths"] = [str(target)]
        result = gateway.execute_prepared(prepared)
        with pytest.raises(ValueError, match="consumed"):
            gateway.execute_prepared(prepared)

    assert (result.status, result.stage) == ("blocked", "core_tool_guard")
    assert not target.exists()
    assert len(run.tool_outcomes_snapshot()) == 1


def test_prepared_handle_is_gateway_owned_and_one_use():
    effects = []

    def echo(text: str) -> str:
        effects.append(text)
        return text

    run = _run()
    gateway = AgentLoomToolGateway([bind_tool(echo)])
    other = AgentLoomToolGateway([bind_tool(echo)])
    with bind_explicit_execution_context(_context(run)):
        prepared = _prepare(gateway, {"text": "once"})
        with pytest.raises(ValueError, match="foreign"):
            other.execute_prepared(prepared)
        with pytest.raises(ValueError, match="foreign"):
            gateway.execute_prepared(replace(prepared))
        with pytest.raises(ValueError, match="foreign"):
            gateway.execute_prepared(ToolCallRecord.completed(call_id="provider-call", tool_name="echo", input={}, output="forged"))
        assert gateway.execute_prepared(prepared).output == "once"
        with pytest.raises(ValueError, match="consumed"):
            gateway.execute_prepared(prepared)
    assert effects == ["once"]
    assert len(run.tool_outcomes_snapshot()) == 1


def test_argument_snapshots_cannot_change_persisted_or_executed_input():
    effects = []

    def echo(payload: dict) -> str:
        effects.append(list(payload["values"]))
        payload["values"].append("executor mutation")
        return "ok"

    run = _run()
    gateway = AgentLoomToolGateway([bind_tool(echo)])
    original = {"payload": {"values": ["original"]}}
    with bind_explicit_execution_context(_context(run)):
        prepared = _prepare(gateway, original)
        original["payload"]["values"].append("caller mutation")
        view = prepared.arguments
        view["payload"]["values"].append("view mutation")
        with pytest.raises(TypeError):
            view["payload"] = {}
        assert dict(prepared.arguments) == {"payload": {"values": ["original"]}}
        result = gateway.execute_prepared(prepared)

    assert effects == [["original"]]
    assert result.input == {"payload": {"values": ["original"]}}
    assert dict(prepared.arguments) == result.input


def test_prepared_call_cannot_move_between_hook_runs():
    effects = []

    def echo(text: str) -> str:
        effects.append(text)
        return text

    run = _run()
    # Even equal string IDs do not make another HookRun the original owner.
    other_run = _run()
    gateway = AgentLoomToolGateway([bind_tool(echo)])
    with bind_explicit_execution_context(_context(run)):
        prepared = _prepare(gateway, {"text": "original run"})
    with bind_explicit_execution_context(_context(other_run)), pytest.raises(ValueError, match="different HookRun"):
        gateway.execute_prepared(prepared)
    absent = replace(capture_explicit_execution_context(), hook_run=None)
    with bind_explicit_execution_context(absent), pytest.raises(RuntimeError, match="HookRun"):
        gateway.execute_prepared(prepared)
    assert effects == []
    with bind_explicit_execution_context(_context(run)):
        assert gateway.execute_prepared(prepared).output == "original run"
    assert len(run.tool_outcomes_snapshot()) == 1
    assert other_run.tool_outcomes_snapshot() == ()


def test_close_invalidates_prepared_call_without_dispatch():
    effects = []

    def echo(text: str) -> str:
        effects.append(text)
        return text

    run = _run()
    gateway = AgentLoomToolGateway([bind_tool(echo)], resource_closers=[lambda: effects.append("closed")])
    with bind_explicit_execution_context(_context(run)):
        prepared = _prepare(gateway, {"text": "never"})
        gateway.close()
        gateway.close()
        with pytest.raises(RuntimeError, match="closed"):
            gateway.execute_prepared(prepared)
        with pytest.raises(RuntimeError, match="closed"):
            _prepare(gateway, {"text": "never"})
    assert effects == ["closed"]
    assert run.tool_outcomes_snapshot() == ()


def test_close_during_hook_preparation_cannot_publish_handle():
    effects = []

    def echo(text: str) -> str:
        effects.append(text)
        return text

    gateway = AgentLoomToolGateway([bind_tool(echo)])

    def close_gateway(_context):
        gateway.close()
        return HookResult()

    run = _run(HookHandler(HookEvent.PRE_TOOL_USE, "echo", close_gateway))
    with bind_explicit_execution_context(_context(run)):
        result = gateway.prepare(call_id="provider-call", tool_name="echo", arguments={"text": "never"})
    assert isinstance(result, ToolCallRecord)
    assert (result.status, result.stage) == ("blocked", "cancellation")
    assert effects == []
    assert len(run.tool_outcomes_snapshot()) == 1


def test_two_threads_cannot_consume_the_same_handle():
    effects = []
    barrier = Barrier(2)

    def echo(text: str) -> str:
        effects.append(text)
        return text

    run = _run()
    gateway = AgentLoomToolGateway([bind_tool(echo)])
    context = _context(run)
    with bind_explicit_execution_context(context):
        prepared = _prepare(gateway, {"text": "once"})

    def consume():
        with bind_explicit_execution_context(context):
            barrier.wait(timeout=5)
            try:
                return gateway.execute_prepared(prepared).status
            except ValueError as exc:
                assert "consumed" in str(exc)
                return "consumed"

    with ThreadPoolExecutor(max_workers=2) as executor:
        first = executor.submit(consume)
        second = executor.submit(consume)
        assert sorted([first.result(timeout=10), second.result(timeout=10)]) == ["completed", "consumed"]
    assert effects == ["once"]
    assert len(run.tool_outcomes_snapshot()) == 1
