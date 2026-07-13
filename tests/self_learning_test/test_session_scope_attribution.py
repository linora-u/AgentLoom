"""Worker session-memory attribution: session scope follows the top-level run."""

from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from contextvars import copy_context
from pathlib import Path

import pytest

from src.extensions.self_learning.memory_store import MemoryStore, current_session_run_id
from src.trace import (
    bind_root_run,
    clear_current_session_run_id,
    get_current_session_run_id,
    set_current_session_run_id,
)


class _FakeManager:
    def __init__(self, session_id: str):
        self._session_id = session_id


@pytest.fixture(autouse=True)
def _clean_session_run_id():
    clear_current_session_run_id()
    yield
    clear_current_session_run_id()


def test_current_session_run_id_prefers_top_level_contextvar(monkeypatch: pytest.MonkeyPatch):
    set_current_session_run_id("supervisor_run")
    monkeypatch.setattr(
        "src.trace.get_current_hook_manager", lambda: _FakeManager("worker_uuid")
    )
    assert current_session_run_id() == "supervisor_run"


def test_current_session_run_id_ignores_global_hook_manager(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(
        "src.trace.get_current_hook_manager", lambda: _FakeManager("worker_uuid")
    )
    assert current_session_run_id() == ""


def test_worker_session_note_lands_in_supervisor_scope(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    store = MemoryStore(tmp_path / ".agentloom" / "self_learning.db")
    # Supervisor published its session id; the worker context has a different
    # hook manager, exactly like a delegated worker run.
    set_current_session_run_id("sup_run")
    monkeypatch.setattr(
        "src.trace.get_current_hook_manager", lambda: _FakeManager("worker_uuid")
    )
    result = store.handle_tool_action("add", scope="session", content="worker learned a durable fact")
    assert result["scope_id"] == "sup_run"
    # The supervisor's SessionEnd distillation consumes it.
    distilled = store.distill_session("sup_run")
    assert distilled["distilled"] == 1
    assert store.list("session", scope_id="sup_run") == []


def test_worker_thread_sees_session_id_via_copied_context():
    """Parallel workers are submitted through copy_context().run (see
    ParallelAgentExecutor), which carries the supervisor's session run id."""
    set_current_session_run_id("threaded_run")
    ctx = copy_context()
    with ThreadPoolExecutor(max_workers=1) as pool:
        seen = pool.submit(ctx.run, get_current_session_run_id).result()
    assert seen == "threaded_run"


def test_concurrent_runs_in_one_process_stay_isolated():
    """Audit counterexample: the old process-global fallback leaked run A's
    session id into a concurrently running run B."""
    set_current_session_run_id("run_a")

    def run_b():
        # A fresh thread with NO copied context is a new top-level run: it
        # must not observe run A, and setting its own id must not leak back.
        assert get_current_session_run_id() is None
        set_current_session_run_id("run_b")
        return get_current_session_run_id()

    with ThreadPoolExecutor(max_workers=1) as pool:
        assert pool.submit(run_b).result() == "run_b"
    assert get_current_session_run_id() == "run_a"


def test_session_run_id_save_and_restore_cycle():
    set_current_session_run_id("outer")
    previous = get_current_session_run_id()
    set_current_session_run_id("inner")
    assert get_current_session_run_id() == "inner"
    # Mirror base_agent's finally block.
    if previous is not None:
        set_current_session_run_id(previous)
    else:
        clear_current_session_run_id()
    assert get_current_session_run_id() == "outer"


def test_model_session_actions_ignore_untrusted_scope_id_but_direct_api_does_not(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    root = tmp_path / ".agentloom"
    monkeypatch.setenv("AGENTLOOM_SELF_LEARNING_ROOT", str(root))

    from src.tools.self_learning.memory_tool import memory

    store = MemoryStore()
    other = store.add(
        "session",
        "other run private note",
        proposal=False,
        source="cli",
        scope_id="root-b",
    )

    with bind_root_run("root-a"):
        added = json.loads(
            memory(
                "add",
                scope="session",
                scope_id="root-b",
                content="root A add",
            )
        )
        assert added["scope_id"] == "root-a"

        listed = json.loads(memory("list", scope="session", scope_id="root-b"))
        assert [item["content"] for item in listed["items"]] == ["root A add"]

        replaced = json.loads(
            memory(
                "replace",
                scope="session",
                scope_id="root-b",
                target=str(added["id"]),
                content="root A replacement",
            )
        )
        assert replaced["ok"] is True

        removable = json.loads(
            memory(
                "add",
                scope="session",
                scope_id="root-b",
                content="root A removable",
            )
        )
        removed = json.loads(
            memory(
                "remove",
                scope="session",
                scope_id="root-b",
                target=str(removable["id"]),
            )
        )
        assert removed["ok"] is True

        batched = json.loads(
            memory(
                "batch",
                scope="session",
                scope_id="root-b",
                operations=json.dumps(
                    [{"action": "add", "content": "root A batch note"}]
                ),
            )
        )
        assert batched["ok"] is True
        assert batched["scope_id"] == "root-a"

        # Even an exact id from another run cannot be targeted through a
        # forged scope id.
        with pytest.raises(KeyError, match="Memory target not found"):
            memory(
                "replace",
                scope="session",
                scope_id="root-b",
                target=str(other["id"]),
                content="must not cross runs",
            )

    assert [
        item["content"] for item in store.list("session", scope_id="root-b")
    ] == ["other run private note"]
    assert sorted(
        item["content"] for item in store.list("session", scope_id="root-a")
    ) == ["root A batch note", "root A replacement"]

    # CLI/maintenance APIs are intentionally explicit and remain capable of
    # selecting a session bucket without model-facing coercion.
    explicit = store.add(
        "session",
        "explicit maintenance note",
        proposal=False,
        source="cli",
        scope_id="root-b",
    )
    assert explicit["scope_id"] == "root-b"
