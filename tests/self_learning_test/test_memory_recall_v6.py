from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from src.extensions.self_learning.persistence.memory_store import MemoryStore


def _config(*, prompt_max_chars: int = 12_000) -> dict:
    return {
        "application_id": "recall_v6_app",
        "self_learning": {
            "memory": {
                "prompt_max_chars": prompt_max_chars,
                "scope_budgets": {"project": 8_000, "application": 6_000},
            },
            "review": {
                "enabled": True,
                "application": {
                    "review_model": "summary",
                    "approval": {"fact": "auto", "experience": "manual"},
                },
                "project": {
                    "review_model": "summary",
                    "approval": {"fact": "manual", "experience": "manual"},
                },
            },
        },
    }


def test_application_memory_reserves_prompt_budget_before_project_memory(
    tmp_path: Path,
) -> None:
    config = _config(prompt_max_chars=95)
    store = MemoryStore(tmp_path / "self_learning.db", agent_config=config)
    store.add("project", "project-" + ("p" * 70))
    store.add(
        "application",
        "application-specific-fact",
        scope_id="recall_v6_app",
    )

    snapshot = store.snapshot_for_prompt(agent_config=config)

    assert "application-specific-fact" in snapshot
    assert "project-" not in snapshot


def test_application_key_overrides_project_key_without_deleting_project(
    tmp_path: Path,
) -> None:
    config = _config()
    store = MemoryStore(tmp_path / "self_learning.db", agent_config=config)
    store.add("project", "Project fallback value.", memory_key="shared:setting")
    store.add(
        "application",
        "Application-specific value.",
        scope_id="recall_v6_app",
        memory_key="shared:setting",
    )

    snapshot = store.snapshot_for_prompt(agent_config=config)

    assert "Application-specific value." in snapshot
    assert "Project fallback value." not in snapshot
    assert [item["content"] for item in store.list("project")] == ["Project fallback value."]


def test_snapshot_loads_only_active_states_and_renders_typed_experience(
    tmp_path: Path,
) -> None:
    config = _config()
    store = MemoryStore(tmp_path / "self_learning.db", agent_config=config)
    active = store.add_typed(
        "application",
        kind="experience",
        memory_key="retry:checkout",
        payload={
            "trigger": "Checkout returns an expired-token error.",
            "symptom": "The first payment call fails.",
            "action": "Refresh the token, then retry once.",
            "verification": "The retry returns a confirmed payment id.",
        },
        scope_id="recall_v6_app",
    )
    store.remove(
        "application",
        str(active["id"]),
        scope_id="recall_v6_app",
    )
    store.add_typed(
        "application",
        kind="experience",
        memory_key="retry:checkout-v2",
        payload={
            "trigger": "Checkout returns an expired-token error.",
            "symptom": "The first payment call fails.",
            "action": "Refresh the token, then retry once.",
            "verification": "The retry returns a confirmed payment id.",
        },
        scope_id="recall_v6_app",
    )

    snapshot = store.snapshot_for_prompt(agent_config=config)

    assert "Trigger: Checkout returns an expired-token error." in snapshot
    assert snapshot.count("Refresh the token, then retry once.") == 1


def test_model_facing_write_submits_candidate_and_never_directly_activates(
    tmp_path: Path,
) -> None:
    config = _config()
    store = MemoryStore(tmp_path / "self_learning.db", agent_config=config)

    result = store.handle_tool_action(
        "add",
        scope="app",
        content="The checkout endpoint requires an idempotency key.",
        root_run_id="root-proposal-1",
        agent_config=config,
    )

    assert result["ok"] is True
    assert result["pending"] is True
    assert result["state"] == "pending_pre_review"
    assert store.list("application", scope_id="recall_v6_app") == []


def test_model_memory_tool_exposes_candidate_only_contract() -> None:
    from src.lib.smolagents.tools.tools import ensure_tool_wrapped
    from src.tools.self_learning.memory_tool import memory

    tool = ensure_tool_wrapped([memory])[0]
    description = " ".join(tool.description.split())

    assert tool.inputs["action"]["enum"] == ["list", "propose"]
    assert tool.inputs["kind"]["enum"] == ["fact", "experience"]
    assert "replace" not in tool.inputs["action"]["enum"]
    assert "cannot activate" in description.casefold()
    assert "candidate" in description.casefold()
    assert "Project promotion" in description


def test_root_snapshot_freezes_memory_mutations_until_the_next_root(
    tmp_path: Path,
) -> None:
    from src.trace import bind_root_run, require_root_run_state

    config = _config()
    db_path = tmp_path / "self_learning.db"
    store = MemoryStore(db_path, agent_config=config)
    original = store.add(
        "project",
        "The root started with the original value.",
        memory_key="root:frozen-value",
    )

    with bind_root_run("root-snapshot-a"):
        first = store.snapshot_for_prompt(
            agent_config=config,
            root_state=require_root_run_state(),
        )
        store.replace(
            "project",
            str(original["id"]),
            "A review changed the value during the root task.",
        )
        worker_view = MemoryStore(db_path, agent_config=config).snapshot_for_prompt(
            agent_config=config,
            root_state=require_root_run_state(),
        )

    with bind_root_run("root-snapshot-b"):
        next_root = store.snapshot_for_prompt(
            agent_config=config,
            root_state=require_root_run_state(),
        )

    assert worker_view == first
    assert "original value" in worker_view
    assert "changed the value" not in worker_view
    assert "changed the value" in next_root
    assert "original value" not in next_root


def test_concurrent_workers_compute_one_shared_root_snapshot(
    tmp_path: Path,
    monkeypatch,
) -> None:
    from src.trace import (
        bind_explicit_execution_context,
        bind_root_run,
        capture_explicit_execution_context,
        require_root_run_state,
    )

    config = _config()
    db_path = tmp_path / "self_learning.db"
    MemoryStore(db_path, agent_config=config).add(
        "project",
        "Concurrent workers must share this snapshot.",
    )
    original_loader = MemoryStore._snapshot_for_prompt_live
    load_count = 0
    load_lock = threading.Lock()

    def counted_loader(self, *, agent_config=None):
        nonlocal load_count
        with load_lock:
            load_count += 1
        return original_loader(self, agent_config=agent_config)

    monkeypatch.setattr(MemoryStore, "_snapshot_for_prompt_live", counted_loader)

    with bind_root_run("root-concurrent-snapshot"):
        execution = capture_explicit_execution_context()

        def load_in_worker() -> str:
            with bind_explicit_execution_context(execution):
                return MemoryStore(db_path, agent_config=config).snapshot_for_prompt(
                    agent_config=config,
                    root_state=require_root_run_state(),
                )

        with ThreadPoolExecutor(max_workers=8) as executor:
            snapshots = list(executor.map(lambda _index: load_in_worker(), range(32)))

    assert load_count == 1
    assert len(set(snapshots)) == 1
    assert "Concurrent workers must share this snapshot." in snapshots[0]
