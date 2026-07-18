from __future__ import annotations

import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from threading import Barrier, Lock
from unittest.mock import MagicMock, patch

import pytest
from smolagents import LocalPythonExecutor, Tool

from src.lib.smolagents.hooks import HookEvent, HookHandler, HookPlan, HookResult, HookRun
from src.lib.smolagents.hooks.tool_shim import (
    _execute_tool_pipeline,
    clone_tool_for_runtime,
    inject_hooks,
)
from src.lib.smolagents.hooks.types import Blocked, Executed, Failed
from src.lib.smolagents.monkey_patch import install_agentloom_runtime_adapters
from src.lib.smolagents.tools.tools import tool
from src.lib.trusted_memory_evidence import (
    TRUSTED_MEMORY_EVIDENCE_ATTR,
    TRUSTED_MEMORY_EVIDENCE_KIND,
    TRUSTED_MEMORY_EVIDENCE_RESPONSE_KEY,
    TrustedMemoryEvidenceEnvelope,
)
from src.trace import bind_explicit_execution_context, capture_explicit_execution_context


def _tool(name: str, result):
    tool = MagicMock()
    tool.name = name
    tool.inputs = {}
    tool._hooks_injected = False
    tool.forward = MagicMock(return_value=result)
    tool.forward.__name__ = "forward"
    return tool


def _invoke(tool, run: HookRun, *args, **kwargs):
    context = replace(capture_explicit_execution_context(), hook_run=run)
    with bind_explicit_execution_context(context):
        return inject_hooks(tool).forward(*args, **kwargs)


def _write_tool(events: list[str]):
    @tool
    def boundary_write(file_path: str, content: str) -> str:
        """Record a write-like side effect.

        Args:
            file_path: Destination path.
            content: Content to write.
        """

        events.append(f"tool:{file_path}:{content}")
        return "written"

    return boundary_write


def _count_tool(events: list[int]):
    @tool
    def boundary_count(count: int) -> int:
        """Record an integer side effect.

        Args:
            count: Integer to record.
        """

        events.append(count)
        return count

    return boundary_count


def test_tool_wrapper_requires_an_active_hook_run() -> None:
    context = replace(capture_explicit_execution_context(), hook_run=None)

    with bind_explicit_execution_context(context), pytest.raises(RuntimeError, match="HookRun"):
        inject_hooks(_count_tool([])).forward(count=1)


def test_empty_tool_result_is_visible_to_the_model() -> None:
    run = HookRun(HookPlan(), local_run_id="local", root_run_id="root")

    assert _invoke(_tool("empty_tool", ""), run) == "(empty_tool completed with no output)"


def test_context_engine_remains_the_only_large_result_compression_boundary() -> None:
    run = HookRun(HookPlan(), local_run_id="local", root_run_id="root")
    engine = MagicMock()
    engine.compress_tool_result.return_value = "[ContextRef ctx_123] preview"

    with patch(
        "src.lib.context_engine.runtime.get_active_context_engine",
        return_value=engine,
    ):
        result = _invoke(_tool("large_tool", "x" * 60_000), run)

    assert result == "[ContextRef ctx_123] preview"
    engine.compress_tool_result.assert_called_once()


def test_trusted_evidence_is_captured_before_result_compression() -> None:
    fact = "Stable page size is 250."
    seen_response = {}

    def observe(context):
        seen_response.update(context.tool_response or {})
        return HookResult()

    run = HookRun(
        HookPlan((HookHandler(HookEvent.POST_TOOL_USE, "*", observe),)),
        local_run_id="local",
        root_run_id="root",
    )
    tool = _tool("contract_reader", fact + ("x" * 60_000))
    setattr(
        tool,
        TRUSTED_MEMORY_EVIDENCE_ATTR,
        lambda _result: [
            {
                "kind": TRUSTED_MEMORY_EVIDENCE_KIND,
                "scope": "project",
                "source": "contract_reader",
                "text": fact,
            }
        ],
    )
    engine = MagicMock()
    engine.compress_tool_result.return_value = "[ContextRef ctx_123] preview"

    with patch(
        "src.lib.context_engine.runtime.get_active_context_engine",
        return_value=engine,
    ):
        assert _invoke(tool, run) == "[ContextRef ctx_123] preview"

    envelope = seen_response[TRUSTED_MEMORY_EVIDENCE_RESPONSE_KEY]
    assert isinstance(envelope, TrustedMemoryEvidenceEnvelope)
    assert envelope[0]["text"] == fact


def test_transformed_unknown_field_is_blocked_before_side_effect() -> None:
    side_effects: list[int] = []
    failure_events: list[dict] = []

    run = HookRun(
        HookPlan(
            (
                HookHandler(
                    HookEvent.PRE_TOOL_USE,
                    "boundary_count",
                    lambda _context: HookResult(
                        decision="modify",
                        modified_input={"unexpected": "injected"},
                    ),
                ),
                HookHandler(
                    HookEvent.POST_TOOL_USE_FAILURE,
                    "boundary_count",
                    lambda context: failure_events.append(context.tool_response or {}),
                ),
            )
        ),
        local_run_id="local",
        root_run_id="root",
    )

    result = _invoke(_count_tool(side_effects), run, count=1)

    assert "unexpected" in result
    assert "not declared" in result
    assert side_effects == []
    assert failure_events == []


def test_transformed_invalid_type_is_blocked_before_side_effect() -> None:
    side_effects: list[int] = []
    run = HookRun(
        HookPlan(
            (
                HookHandler(
                    HookEvent.PRE_TOOL_USE,
                    "boundary_count",
                    lambda _context: HookResult(
                        decision="modify",
                        modified_input={"count": "not-an-integer"},
                    ),
                ),
            )
        ),
        local_run_id="local",
        root_run_id="root",
    )

    result = _invoke(_count_tool(side_effects), run, count=1)

    assert "count" in result
    assert "integer" in result
    assert side_effects == []


def test_pre_allow_cannot_mutate_nested_input_by_reference() -> None:
    observed: list[dict[str, list[int]]] = []

    @tool
    def nested_input_tool(payload: dict[str, list[int]]) -> str:
        """Observe nested input.

        Args:
            payload: Nested values.
        """

        observed.append(payload)
        return "ok"

    def mutate_without_modify(context):
        context.tool_input["payload"]["values"].append(99)
        return HookResult(decision="allow")

    run = HookRun(
        HookPlan((HookHandler(HookEvent.PRE_TOOL_USE, "nested_input_tool", mutate_without_modify),)),
        local_run_id="local",
        root_run_id="root",
    )

    assert _invoke(nested_input_tool, run, payload={"values": [1]}) == "ok"
    assert observed == [{"values": [1]}]


def test_invalid_gate_result_field_fails_closed() -> None:
    side_effects: list[int] = []
    run = HookRun(
        HookPlan(
            (
                HookHandler(
                    HookEvent.PRE_TOOL_USE,
                    "boundary_count",
                    lambda _context: HookResult(agent_context=123),
                ),
            )
        ),
        local_run_id="local",
        root_run_id="root",
    )

    result = _invoke(_count_tool(side_effects), run, count=1)

    assert "agent_context must be a string" in result
    assert side_effects == []


def test_post_observer_cannot_mutate_nested_tool_result_by_reference() -> None:
    @tool
    def nested_result_tool(value: int) -> dict[str, list[int]]:
        """Return a nested result.

        Args:
            value: Result value.
        """

        return {"values": [value]}

    def mutate_observation(context):
        context.tool_response["result"]["values"].append(99)
        return HookResult()

    run = HookRun(
        HookPlan((HookHandler(HookEvent.POST_TOOL_USE, "nested_result_tool", mutate_observation),)),
        local_run_id="local",
        root_run_id="root",
    )

    assert _invoke(nested_result_tool, run, value=1) == {"values": [1]}


def test_invalid_observer_result_field_is_diagnostic_and_later_observer_runs() -> None:
    observed: list[str] = []
    run = HookRun(
        HookPlan(
            (
                HookHandler(
                    HookEvent.POST_TOOL_USE,
                    "boundary_count",
                    lambda _context: HookResult(telemetry="invalid"),
                    source="invalid-observer",
                ),
                HookHandler(
                    HookEvent.POST_TOOL_USE,
                    "boundary_count",
                    lambda _context: observed.append("later") or HookResult(),
                ),
            )
        ),
        local_run_id="local",
        root_run_id="root",
    )

    assert _invoke(_count_tool([]), run, count=2) == 2
    assert observed == ["later"]


def test_final_input_pipeline_order_is_guard_history_recorder_tool_post() -> None:
    events: list[str] = []
    final_path = "/workspace/final.txt"

    def transform(_context):
        events.append("hook")
        return HookResult(
            decision="modify",
            modified_input={"file_path": final_path, "content": "final"},
        )

    def guard(context):
        events.append(f"guard:{context.tool_input['file_path']}")
        return HookResult()

    def history(*, tool_name, tool_input, step_number):
        events.append(f"history:{tool_name}:{tool_input['file_path']}:{step_number}")

    def recorder(context):
        events.append(f"recorder:{context.tool_input['file_path']}")
        return HookResult()

    def post(context):
        events.append(f"post:{context.tool_input['file_path']}")
        return HookResult()

    run = HookRun(
        HookPlan(
            (
                HookHandler(HookEvent.PRE_TOOL_USE, "boundary_write", transform),
                HookHandler(HookEvent.POST_TOOL_USE, "boundary_write", post),
            )
        ),
        local_run_id="local",
        root_run_id="root",
    )
    run.step_number = 8

    with (
        patch(
            "src.lib.smolagents.hooks.tool_shim.enforce_core_tool_guard",
            side_effect=guard,
        ),
        patch(
            "src.lib.smolagents.hooks.tool_shim.record_active_file_history",
            side_effect=history,
        ),
        patch(
            "src.extensions.self_learning.session_recorder.session_recorder_hook",
            side_effect=recorder,
        ),
    ):
        result = _invoke(
            _write_tool(events),
            run,
            file_path="/workspace/original.txt",
            content="original",
        )

    assert result == "written"
    assert events == [
        "hook",
        f"guard:{final_path}",
        f"history:boundary_write:{final_path}:8",
        f"recorder:{final_path}",
        f"tool:{final_path}:final",
        f"post:{final_path}",
    ]


def test_core_guard_block_is_not_tool_failure_and_has_no_side_effect() -> None:
    side_effects: list[str] = []
    failure_events: list[dict] = []
    guarded_inputs: list[dict] = []
    run = HookRun(
        HookPlan(
            (
                HookHandler(
                    HookEvent.PRE_TOOL_USE,
                    "boundary_write",
                    lambda _context: HookResult(
                        decision="modify",
                        modified_input={"file_path": "/blocked/final.txt"},
                    ),
                ),
                HookHandler(
                    HookEvent.POST_TOOL_USE_FAILURE,
                    "boundary_write",
                    lambda context: failure_events.append(context.tool_response or {}),
                ),
            )
        ),
        local_run_id="local",
        root_run_id="root",
    )

    def guard(context):
        guarded_inputs.append(dict(context.tool_input))
        return HookResult(decision="block", reason="final path denied")

    with (
        patch(
            "src.lib.smolagents.hooks.tool_shim.enforce_core_tool_guard",
            side_effect=guard,
        ),
        patch("src.lib.smolagents.hooks.tool_shim.record_active_file_history") as history,
        patch("src.extensions.self_learning.session_recorder.session_recorder_hook") as recorder,
    ):
        result = _invoke(
            _write_tool(side_effects),
            run,
            file_path="/allowed/original.txt",
            content="data",
        )

    assert result == "final path denied"
    assert guarded_inputs == [{"file_path": "/blocked/final.txt", "content": "data"}]
    assert side_effects == []
    assert failure_events == []
    history.assert_not_called()
    recorder.assert_not_called()


def test_self_learning_final_input_observer_failure_does_not_block_tool() -> None:
    side_effects: list[int] = []
    run = HookRun(HookPlan(), local_run_id="local", root_run_id="root")

    with patch(
        "src.extensions.self_learning.session_recorder.session_recorder_hook",
        side_effect=RuntimeError("recorder unavailable"),
    ) as recorder:
        result = _invoke(_count_tool(side_effects), run, count="7")

    assert result == 7
    assert side_effects == [7]
    recorder.assert_called_once()
    assert recorder.call_args.args[0].tool_input == {"count": 7}


def test_internal_outcomes_distinguish_executed_blocked_and_failed() -> None:
    executed_tool = _count_tool([])
    run = HookRun(HookPlan(), local_run_id="local", root_run_id="root")
    executed = _execute_tool_pipeline(
        executed_tool,
        executed_tool.forward,
        run,
        args=(),
        kwargs={"count": 1},
    )
    assert isinstance(executed, Executed)
    assert executed.outcome == "executed"

    blocked_run = HookRun(
        HookPlan(
            (
                HookHandler(
                    HookEvent.PRE_TOOL_USE,
                    "boundary_count",
                    lambda _context: HookResult(decision="block", reason="denied"),
                ),
            )
        ),
        local_run_id="local-blocked",
        root_run_id="root",
    )
    blocked = _execute_tool_pipeline(
        executed_tool,
        executed_tool.forward,
        blocked_run,
        args=(),
        kwargs={"count": 2},
    )
    assert isinstance(blocked, Blocked)
    assert blocked.outcome == "blocked"
    assert blocked.reason == "denied"

    @tool
    def boundary_failure(value: int) -> int:
        """Raise from a real tool.

        Args:
            value: Failure marker.
        """

        raise ValueError(f"boom-{value}")

    failed = _execute_tool_pipeline(
        boundary_failure,
        boundary_failure.forward,
        run,
        args=(),
        kwargs={"value": 3},
    )
    assert isinstance(failed, Failed)
    assert failed.outcome == "failed"
    assert isinstance(failed.error, ValueError)


def test_run_owned_trace_preserves_executed_blocked_and_failed() -> None:
    executed_run = HookRun(HookPlan(), local_run_id="executed", root_run_id="root")
    assert _invoke(_count_tool([]), executed_run, count=1) == 1
    executed_trace = executed_run.tool_outcomes_snapshot()
    assert len(executed_trace) == 1
    assert isinstance(executed_trace[0], Executed)
    assert executed_trace[0].outcome == "executed"
    assert executed_trace[0].tool_name == "boundary_count"
    assert executed_trace[0].value is None

    blocked_run = HookRun(
        HookPlan(
            (
                HookHandler(
                    HookEvent.PRE_TOOL_USE,
                    "boundary_count",
                    lambda _context: HookResult(decision="block", reason="policy denied"),
                ),
            )
        ),
        local_run_id="blocked",
        root_run_id="root",
    )
    assert _invoke(_count_tool([]), blocked_run, count=2) == "policy denied"
    blocked_trace = blocked_run.tool_outcomes_snapshot()
    assert len(blocked_trace) == 1
    assert isinstance(blocked_trace[0], Blocked)
    assert blocked_trace[0].outcome == "blocked"
    assert blocked_trace[0].stage == "pre_tool_use"
    assert blocked_trace[0].tool_name == "boundary_count"

    @tool
    def traced_failure(value: int) -> int:
        """Raise from a traced tool.

        Args:
            value: Failure marker.
        """

        raise ValueError(f"trace-boom-{value}")

    failed_run = HookRun(HookPlan(), local_run_id="failed", root_run_id="root")
    with pytest.raises(ValueError, match="trace-boom-3"):
        _invoke(traced_failure, failed_run, value=3)
    failed_trace = failed_run.tool_outcomes_snapshot()
    assert len(failed_trace) == 1
    assert isinstance(failed_trace[0], Failed)
    assert failed_trace[0].outcome == "failed"
    assert failed_trace[0].stage == "tool_execution"
    assert failed_trace[0].tool_name == "traced_failure"


def test_typed_outcome_is_recorded_before_post_observers() -> None:
    observed: list[str] = []
    run: HookRun

    def post(_context):
        observed.append(run.tool_outcomes_snapshot()[-1].outcome)
        return HookResult()

    run = HookRun(
        HookPlan((HookHandler(HookEvent.POST_TOOL_USE, "boundary_count", post),)),
        local_run_id="local",
        root_run_id="root",
    )

    assert _invoke(_count_tool([]), run, count=1) == 1
    assert observed == ["executed"]


def test_failed_outcome_is_recorded_before_failure_observers() -> None:
    observed: list[str] = []
    run: HookRun

    @tool
    def preobserved_failure(value: int) -> int:
        """Fail after crossing the tool boundary.

        Args:
            value: Failure marker.
        """

        raise ValueError(str(value))

    def failure(_context):
        observed.append(run.tool_outcomes_snapshot()[-1].outcome)
        return HookResult()

    run = HookRun(
        HookPlan((HookHandler(HookEvent.POST_TOOL_USE_FAILURE, "preobserved_failure", failure),)),
        local_run_id="local",
        root_run_id="root",
    )

    with pytest.raises(ValueError, match="4"):
        _invoke(preobserved_failure, run, value=4)
    assert observed == ["failed"]


def test_run_owned_outcome_snapshot_cannot_mutate_recorded_tool_input() -> None:
    @tool
    def payload_tool(payload: dict[str, list[int]]) -> str:
        """Consume a nested payload.

        Args:
            payload: Nested numbers.
        """

        return "unexpected"

    run = HookRun(
        HookPlan(
            (
                HookHandler(
                    HookEvent.PRE_TOOL_USE,
                    "payload_tool",
                    lambda _context: HookResult(decision="block", reason="nested denied"),
                ),
            )
        ),
        local_run_id="local",
        root_run_id="root",
    )
    payload = {"numbers": [1]}

    assert _invoke(payload_tool, run, payload=payload) == "nested denied"
    payload["numbers"].append(2)

    first_snapshot = run.tool_outcomes_snapshot()
    assert first_snapshot[0].tool_input == {"payload": {"numbers": [1]}}
    first_snapshot[0].tool_input["payload"]["numbers"].append(3)
    assert run.tool_outcomes_snapshot()[0].tool_input == {"payload": {"numbers": [1]}}


def test_run_owned_outcome_trace_is_bounded() -> None:
    run = HookRun(HookPlan(), local_run_id="local", root_run_id="root")

    for index in range(1005):
        run.record_tool_outcome(Blocked({"index": index}, "denied", f"stage-{index}"))

    snapshot = run.tool_outcomes_snapshot()
    assert len(snapshot) == 1000
    assert snapshot[0].stage == "stage-5"
    assert snapshot[-1].stage == "stage-1004"


def test_run_owned_outcome_trace_summarizes_oversized_payloads_and_text() -> None:
    run = HookRun(HookPlan(), local_run_id="local", root_run_id="root")

    run.record_tool_outcome(Blocked({"blob": "x" * 100_000}, "r" * 100_000, "guard"))

    traced = run.tool_outcomes_snapshot()[0]
    assert traced.tool_input["_trace_input"] == "truncated"
    assert traced.tool_input["original_bytes"] > 16 * 1024
    assert len(traced.reason) < 5000
    assert traced.reason.endswith("[truncated]")


def test_run_owned_effect_queues_enforce_item_and_total_byte_budgets() -> None:
    run = HookRun(HookPlan(), local_run_id="local", root_run_id="root")

    run.queue_agent_context("x" * 100_000)
    contexts = run.consume_pending_agent_context()
    assert len(contexts) == 1
    assert contexts[0].endswith("[truncated]")
    assert len(contexts[0].encode("utf-8")) <= 16 * 1024

    for index in range(100):
        run.queue_user_message(f"{index}:" + ("y" * 10_000))
    messages = run.consume_pending_user_messages()
    assert len(messages) < 100
    assert sum(len(message.encode("utf-8")) for message in messages) <= 256 * 1024


def test_stateful_tool_runtime_clones_do_not_share_mutable_state() -> None:
    rendezvous = Barrier(2)

    class StatefulTool(Tool):
        name = "stateful_tool"
        description = "Track per-runtime calls."
        inputs = {"label": {"type": "string", "description": "Run label"}}
        output_type = "string"

        def __init__(self):
            self.is_initialized = True
            self.calls: list[str] = []

        def forward(self, label: str) -> str:
            rendezvous.wait(timeout=3)
            self.calls.append(label)
            return label

    definition = StatefulTool()
    runtime_tools = [inject_hooks(clone_tool_for_runtime(definition)) for _ in range(2)]
    runs = [HookRun(HookPlan(), local_run_id=f"local-{label}", root_run_id=f"root-{label}") for label in "AB"]

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [
            pool.submit(_invoke, runtime_tool, run, label=label)
            for runtime_tool, run, label in zip(runtime_tools, runs, "AB", strict=True)
        ]
        assert [future.result(timeout=5) for future in futures] == ["A", "B"]

    assert definition.calls == []
    assert runtime_tools[0].calls == ["A"]
    assert runtime_tools[1].calls == ["B"]


def test_uncloneable_stateful_tool_requires_explicit_runtime_factory() -> None:
    class UncloneableTool(Tool):
        name = "uncloneable_tool"
        description = "Owns a process lock."
        inputs = {}
        output_type = "string"

        def __init__(self):
            self.is_initialized = True
            self.lock = Lock()

        def forward(self) -> str:
            return "ok"

    with pytest.raises(RuntimeError, match=r"implement clone_for_runtime\(\)"):
        clone_tool_for_runtime(UncloneableTool())


def test_local_python_executor_timeout_thread_keeps_concurrent_run_identity_isolated() -> None:
    install_agentloom_runtime_adapters()
    rendezvous = Barrier(2)
    observations: dict[str, dict[str, object]] = {}

    @tool
    def shared_identity_tool(label: str) -> str:
        """Return a run label through a shared tool definition.

        Args:
            label: Run label.
        """

        return label

    shared_tool = inject_hooks(shared_identity_tool)

    def execute(label: str):
        def observe(context):
            active = capture_explicit_execution_context()
            observations[label] = {
                "context_task": context.task_id,
                "context_agent": context.agent_name,
                "context_root": context.root_run_id,
                "context_local": context.local_run_id,
                "active_task": active.task_id,
                "active_agent": active.agent_name,
                "active_root": active.root_run_id,
                "active_local": active.local_run_id,
                "active_hook_run": active.hook_run,
            }
            rendezvous.wait(timeout=3)
            return HookResult(agent_context=f"context-{label}")

        run = HookRun(
            HookPlan((HookHandler(HookEvent.PRE_TOOL_USE, "shared_identity_tool", observe),)),
            local_run_id=f"local-{label}",
            root_run_id=f"root-{label}",
        )
        current = capture_explicit_execution_context()
        explicit = replace(
            current,
            task_id=f"task-{label}",
            agent_name=f"agent-{label}",
            hook_run=run,
            root_run_id=f"root-{label}",
            local_run_id=f"local-{label}",
        )
        with bind_explicit_execution_context(explicit):
            executor = LocalPythonExecutor([], timeout_seconds=5)
            executor.send_tools({"shared_identity_tool": shared_tool})
            result = executor(f'shared_identity_tool(label="{label}")')
        return result.output, run, run.tool_outcomes_snapshot()

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = {label: pool.submit(execute, label) for label in ("A", "B")}
        results = {label: future.result(timeout=10) for label, future in futures.items()}

    for label in ("A", "B"):
        value, run, trace = results[label]
        assert value == label
        assert observations[label] == {
            "context_task": f"task-{label}",
            "context_agent": f"agent-{label}",
            "context_root": f"root-{label}",
            "context_local": f"local-{label}",
            "active_task": f"task-{label}",
            "active_agent": f"agent-{label}",
            "active_root": f"root-{label}",
            "active_local": f"local-{label}",
            "active_hook_run": run,
        }
        assert run.consume_pending_agent_context() == [f"context-{label}"]
        assert len(trace) == 1
        assert isinstance(trace[0], Executed)
        assert trace[0].tool_input == {"label": label}


def test_local_python_executor_timeout_leaves_no_delayed_hook_or_tool_effect_after_return() -> None:
    install_agentloom_runtime_adapters()
    effects: list[str] = []

    @tool
    def slow_shared_tool(label: str) -> str:
        """Complete after the executor timeout threshold.

        Args:
            label: Effect label.
        """

        time.sleep(0.15)
        effects.append(f"tool-{label}")
        return label

    def observe(_context):
        effects.append("post")
        return HookResult()

    run = HookRun(
        HookPlan((HookHandler(HookEvent.POST_TOOL_USE, "slow_shared_tool", observe),)),
        local_run_id="timed",
        root_run_id="timed",
    )
    current = capture_explicit_execution_context()
    explicit = replace(
        current,
        hook_run=run,
        root_run_id="timed",
        local_run_id="timed",
    )
    executor = LocalPythonExecutor([], timeout_seconds=0.05)
    executor.send_tools({"slow_shared_tool": inject_hooks(slow_shared_tool)})

    started = time.monotonic()
    with bind_explicit_execution_context(explicit), pytest.raises(Exception, match="exceeded"):
        executor('slow_shared_tool(label="A")')
    elapsed = time.monotonic() - started
    effects_at_return = list(effects)
    time.sleep(0.2)

    # smolagents cannot kill its Python timeout thread and waits for shutdown;
    # the Hook Runtime does not promise cancellation of trusted Python tools.
    # The invariant we can enforce is that no old-run effect occurs later.
    assert elapsed >= 0.15
    assert effects_at_return == ["tool-A", "post"]
    assert effects == effects_at_return


def test_real_tool_failure_still_dispatches_post_tool_use_failure() -> None:
    failures: list[dict] = []

    @tool
    def boundary_failure(value: int) -> int:
        """Raise from a real tool.

        Args:
            value: Failure marker.
        """

        raise ValueError(f"boom-{value}")

    run = HookRun(
        HookPlan(
            (
                HookHandler(
                    HookEvent.POST_TOOL_USE_FAILURE,
                    "boundary_failure",
                    lambda context: failures.append(context.tool_response or {}),
                ),
            )
        ),
        local_run_id="local",
        root_run_id="root",
    )

    with pytest.raises(ValueError, match="boom-5"):
        _invoke(boundary_failure, run, value=5)

    assert failures == [{"error": "boom-5", "error_type": "ValueError"}]


def test_failure_observer_framework_error_does_not_replace_tool_error(monkeypatch) -> None:
    @tool
    def boundary_failure(value: int) -> int:
        """Raise from a real tool.

        Args:
            value: Failure marker.
        """

        raise ValueError(f"original-{value}")

    run = HookRun(HookPlan(), local_run_id="local", root_run_id="root")
    original_dispatch = run.dispatch

    def dispatch(event, *args, **kwargs):
        if event is HookEvent.POST_TOOL_USE_FAILURE:
            raise RuntimeError("observer infrastructure unavailable")
        return original_dispatch(event, *args, **kwargs)

    monkeypatch.setattr(run, "dispatch", dispatch)

    with pytest.raises(ValueError, match="original-9"):
        _invoke(boundary_failure, run, value=9)

    trace = run.tool_outcomes_snapshot()
    assert len(trace) == 1
    assert isinstance(trace[0], Failed)
    assert str(trace[0].error) == "ValueError: original-9"
