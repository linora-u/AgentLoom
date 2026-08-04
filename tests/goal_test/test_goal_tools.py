import json
from dataclasses import replace

import pytest

from src.lib.goal import GoalState
from src.lib.goal.provider import GoalStateProvider, bind_goal_state_provider
from src.lib.smolagents.hooks.runtime import HookPlan, HookRun
from src.tools.goal import get_goal, update_goal
from src.trace import (
    bind_explicit_execution_context,
    bind_local_run,
    bind_root_run,
    capture_explicit_execution_context,
)


def _provider() -> GoalStateProvider:
    return GoalStateProvider(
        GoalState.create(
            objective="Ship Goal mode.",
            objective_fingerprint="abc123",
            token_budget=None,
        )
    )


def _hook(*, parent=None) -> HookRun:
    return HookRun(
        HookPlan(),
        local_run_id="root" if parent is None else "worker",
        root_run_id="root",
        parent=parent,
        agent_config={"goal": {"enabled": True}},
    )


def test_root_supervisor_can_read_and_complete_goal():
    provider = _provider()
    with bind_local_run("root"), bind_root_run("root"), bind_goal_state_provider(provider):
        context = replace(
            capture_explicit_execution_context(),
            hook_run=_hook(),
            agent_config={"goal": {"enabled": True}},
        )
        with bind_explicit_execution_context(context):
            initial = json.loads(get_goal.forward())
            completed = json.loads(
                update_goal.forward(status="complete", evidence="Tests passed.")
            )
            assert provider.assert_request_allowed(
                local_run_id="root",
                allow_completion_settlement=True,
            ) is True

    assert initial["status"] == "active"
    assert completed["status"] == "complete"
    assert completed["evidence"] == "Tests passed."


def test_update_goal_requires_complete_and_evidence():
    provider = _provider()
    with bind_local_run("root"), bind_root_run("root"), bind_goal_state_provider(provider):
        context = replace(capture_explicit_execution_context(), hook_run=_hook())
        with bind_explicit_execution_context(context):
            with pytest.raises(ValueError, match="must be 'complete'"):
                update_goal.forward(status="active", evidence="no")
            with pytest.raises(ValueError, match="evidence"):
                update_goal.forward(status="complete", evidence="  ")


def test_worker_cannot_call_goal_tools_even_when_provider_is_inherited():
    provider = _provider()
    parent = _hook()
    with bind_local_run("worker"), bind_root_run("root"), bind_goal_state_provider(provider):
        context = replace(
            capture_explicit_execution_context(),
            hook_run=_hook(parent=parent),
            agent_config={"goal": {"enabled": True}},
        )
        with bind_explicit_execution_context(context):
            with pytest.raises(PermissionError, match="root Supervisor"):
                get_goal.forward()
