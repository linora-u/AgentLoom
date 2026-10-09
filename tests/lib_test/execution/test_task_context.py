"""Subtask identities stay local even when threads/tasks enter and exit out of order."""

from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
from contextvars import copy_context
from dataclasses import replace
from importlib import import_module
from threading import Barrier, Event

import pytest
from agentloom.execution.trace.task_context import (
    bind_explicit_execution_context,
    capture_explicit_execution_context,
    get_current_agent_id,
    get_current_agent_name,
    get_current_sub_task_id,
    sub_task_context,
)

# The package exports a same-named context manager; import the actual module.
contexts = import_module("agentloom.execution.trace.task_context")
FALLBACKS = (
    "_global_sub_task_id_fallback", "_global_agent_id_fallback", "_global_agent_name_fallback",
)


def identity():
    execution = capture_explicit_execution_context()
    return execution.sub_task_id, execution.agent_id, execution.agent_name


def legacy_identity():
    return tuple(getattr(contexts, name) for name in FALLBACKS)


@pytest.fixture(autouse=True)
def isolated_parent(monkeypatch):
    # Establish the starting conditions, but never clean state before asserting
    # the real post-exit invariants in the body of each test.
    for name in FALLBACKS:
        monkeypatch.setattr(contexts, name, None)
    parent = replace(
        capture_explicit_execution_context(),
        sub_task_id=None, agent_id=None, agent_name=None,
    )
    with bind_explicit_execution_context(parent):
        yield parent


def test_nested_contexts_restore_exact_parent_and_preserve_other_run_fields(isolated_parent):
    parent = replace(
        isolated_parent, task_id="task-parent", sub_task_id="sub-parent",
        agent_id="instance-parent", agent_name="parent", root_run_id="root-parent",
        local_run_id="run-parent", agent_config={"trusted": True},
        runtime_agent_path="parent", hook_run=object(), skill_catalog=object(),
    )
    with bind_explicit_execution_context(parent):
        with sub_task_context("outer", "sub-outer", agent_id="instance-outer") as yielded:
            outer = capture_explicit_execution_context()
            assert yielded == "sub-outer"
            assert outer == replace(parent, sub_task_id=yielded, agent_id="instance-outer", agent_name="outer")
            assert outer.root_run_state is parent.root_run_state
            with sub_task_context("inner", "sub-inner", agent_id="instance-inner"):
                assert identity() == ("sub-inner", "instance-inner", "inner")
            assert capture_explicit_execution_context() == outer
        assert capture_explicit_execution_context() == parent
    assert legacy_identity() == (None, None, None)


def test_generated_subtask_id_and_default_agent_id_are_preserved(isolated_parent):
    with sub_task_context("worker") as yielded:
        assert isinstance(yielded, str) and yielded
        assert identity() == (yielded, "worker", "worker")
        assert (get_current_sub_task_id(), get_current_agent_id(), get_current_agent_name()) == identity()
    assert capture_explicit_execution_context() == isolated_parent


@pytest.mark.parametrize("parent_values", [
    (None, None, None), ("parent-sub", None, None),
    (None, "parent-instance", "parent"), ("parent-sub", "parent-instance", "parent"),
])
@pytest.mark.parametrize("error_type", [None, RuntimeError, KeyboardInterrupt, asyncio.CancelledError])
def test_foreign_fallback_never_becomes_explicit_parent(monkeypatch, isolated_parent, parent_values, error_type):
    foreign = ("other-sub", "other-instance", "other-agent")
    for name, value in zip(FALLBACKS, foreign, strict=True):
        monkeypatch.setattr(contexts, name, value)
    parent = replace(isolated_parent, sub_task_id=parent_values[0],
                     agent_id=parent_values[1], agent_name=parent_values[2])

    def invoke():
        with sub_task_context("worker", "worker-sub", agent_id="worker-instance"):
            assert identity() == ("worker-sub", "worker-instance", "worker")
            assert legacy_identity() == foreign
            if error_type is not None:
                raise error_type("interrupted")

    with bind_explicit_execution_context(parent):
        if error_type is None:
            invoke()
        else:
            with pytest.raises(error_type):
                invoke()
        assert capture_explicit_execution_context() == parent
    assert legacy_identity() == foreign


@pytest.mark.parametrize("first_exit", ["A", "B"])
@pytest.mark.parametrize("inherit_parent", [False, True])
def test_overlapping_threads_restore_their_own_parent(first_exit, inherit_parent, isolated_parent):
    entered = {name: Event() for name in ("A", "B")}
    release = {name: Event() for name in ("A", "B")}
    parent = replace(isolated_parent, task_id="parent-task", agent_id="parent-instance",
                     agent_name="parent", root_run_id="parent-root", local_run_id="parent-run")

    def run(name):
        before = capture_explicit_execution_context()
        with sub_task_context(name, f"sub-{name}", agent_id=f"instance-{name}"):
            entered[name].set()
            assert release[name].wait(5), "worker release timed out"
            inside = identity()
        return before, inside, capture_explicit_execution_context()

    with bind_explicit_execution_context(parent), ThreadPoolExecutor(max_workers=2) as pool:
        futures = {}
        try:
            for name in ("A", "B"):
                futures[name] = (pool.submit(copy_context().run, run, name)
                                 if inherit_parent else pool.submit(run, name))
                assert entered[name].wait(5), "worker entry timed out"
            release[first_exit].set()
            first_result = futures[first_exit].result(timeout=5)
            last = "B" if first_exit == "A" else "A"
            release[last].set()
            last_result = futures[last].result(timeout=5)
        finally:
            for event in release.values():
                event.set()
        for name, (before, inside, after) in ((first_exit, first_result), (last, last_result)):
            assert inside == (f"sub-{name}", f"instance-{name}", name)
            assert after == before
            if inherit_parent:
                assert before == parent
            else:
                assert (before.sub_task_id, before.agent_id, before.agent_name) == (None, None, None)
        assert legacy_identity() == (None, None, None)
        with sub_task_context("serial", "serial-sub", agent_id="serial-instance"):
            assert identity() == ("serial-sub", "serial-instance", "serial")
        assert capture_explicit_execution_context() == parent


@pytest.mark.parametrize("workers", [2, 8, 20])
@pytest.mark.parametrize("interrupted", [False, True])
def test_reused_pool_preserves_context_under_repeated_contention(workers, interrupted, isolated_parent):
    barrier = Barrier(workers)

    def run(index):
        before = capture_explicit_execution_context()
        try:
            with sub_task_context(f"worker-{index}", f"sub-{index}", agent_id=f"instance-{index}"):
                barrier.wait(timeout=5)
                assert identity() == (f"sub-{index}", f"instance-{index}", f"worker-{index}")
                if interrupted and index % 2:
                    raise RuntimeError("simulated worker failure")
        except RuntimeError:
            assert interrupted and index % 2
        return before, capture_explicit_execution_context()

    with ThreadPoolExecutor(max_workers=workers) as pool:
        for round_number in range(25):
            parent = replace(isolated_parent, task_id=f"task-{round_number}",
                             root_run_id=f"root-{round_number}", local_run_id=f"run-{round_number}")
            with bind_explicit_execution_context(parent):
                futures = [pool.submit(copy_context().run, run, round_number * workers + index)
                           for index in range(workers)]
                for future in futures:
                    before, after = future.result(timeout=10)
                    assert before == after == parent
                    assert after.root_run_state is parent.root_run_state
                assert capture_explicit_execution_context() == parent
            assert legacy_identity() == (None, None, None)


@pytest.mark.parametrize("cancelled", [None, "A", "B"])
def test_async_tasks_restore_context_on_normal_exit_or_cancellation(cancelled, isolated_parent):
    async def scenario():
        entered = {name: asyncio.Event() for name in ("A", "B")}
        release = {name: asyncio.Event() for name in ("A", "B")}
        snapshots = {}

        async def run(name):
            before = capture_explicit_execution_context()
            try:
                with sub_task_context(name, f"sub-{name}", agent_id=f"instance-{name}"):
                    entered[name].set()
                    await release[name].wait()
                    assert identity() == (f"sub-{name}", f"instance-{name}", name)
            finally:
                snapshots[name] = before, capture_explicit_execution_context()

        tasks = {}
        try:
            for name in ("A", "B"):
                tasks[name] = asyncio.create_task(run(name))
                await asyncio.wait_for(entered[name].wait(), timeout=5)
            first = cancelled or "A"
            if cancelled:
                tasks[first].cancel()
                with pytest.raises(asyncio.CancelledError):
                    await tasks[first]
            else:
                release[first].set()
                await asyncio.wait_for(tasks[first], timeout=5)
            last = "B" if first == "A" else "A"
            release[last].set()
            await asyncio.wait_for(tasks[last], timeout=5)
        finally:
            for task in tasks.values():
                if not task.done():
                    task.cancel()
            await asyncio.gather(*tasks.values(), return_exceptions=True)
        assert all(before == after == isolated_parent for before, after in snapshots.values())
        assert legacy_identity() == (None, None, None)

    asyncio.run(scenario())
    assert capture_explicit_execution_context() == isolated_parent
