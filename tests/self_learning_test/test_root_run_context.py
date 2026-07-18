"""Root-run ownership and Hook Run propagation contracts."""

from __future__ import annotations

import json
import tomllib
import uuid
from concurrent.futures import ThreadPoolExecutor
from contextvars import copy_context
from importlib.metadata import version
from pathlib import Path

import pytest
from packaging.requirements import Requirement

from src.lib.smolagents.hooks import HookEvent, HookHandler, HookPlan, HookResult, HookRun
from src.trace import (
    MissingRunContextError,
    bind_local_run,
    bind_root_run,
    get_current_session_run_id,
    require_root_run_id,
)


def test_smolagents_context_patch_dependency_is_exactly_pinned() -> None:
    project_root = Path(__file__).parents[2]
    project = tomllib.loads((project_root / "pyproject.toml").read_text())
    dependency = next(
        Requirement(raw) for raw in project["project"]["dependencies"] if Requirement(raw).name == "smolagents"
    )

    assert str(dependency.specifier) == "==1.26.0"
    assert version("smolagents") == "1.26.0"


def test_require_root_run_id_fails_closed_without_binding() -> None:
    with pytest.raises(MissingRunContextError, match="root run"):
        require_root_run_id()


def test_bind_root_run_owns_outer_binding_and_nested_calls_inherit_it() -> None:
    assert get_current_session_run_id() is None

    with bind_root_run("supervisor-run") as outer_owner:
        assert outer_owner is True
        with bind_root_run("worker-local-session") as nested_owner:
            assert nested_owner is False
            assert require_root_run_id() == "supervisor-run"

    assert get_current_session_run_id() is None


def test_copied_worker_context_inherits_root_without_ownership() -> None:
    with bind_root_run("supervisor-run"):
        worker_context = copy_context()

        def worker():
            with bind_root_run("worker-local-session") as owns_root:
                return owns_root, require_root_run_id()

        with ThreadPoolExecutor(max_workers=1) as pool:
            owns_root, root_run_id = pool.submit(worker_context.run, worker).result()

    assert owns_root is False
    assert root_run_id == "supervisor-run"


@pytest.mark.parametrize("run_id", ["", "   ", None])
def test_bind_root_run_rejects_empty_identity(run_id) -> None:
    with pytest.raises(ValueError, match="root run id"):
        with bind_root_run(run_id):
            pass


def test_hook_run_uses_explicit_local_and_root_identity() -> None:
    seen = []

    def capture(context):
        seen.append((context.local_run_id, context.root_run_id))
        return HookResult()

    with bind_local_run("worker-local"), bind_root_run("supervisor-root"):
        run = HookRun(
            HookPlan((HookHandler(HookEvent.POST_TOOL_USE, "*", capture),)),
            local_run_id="worker-local",
            root_run_id=require_root_run_id(),
        )
        run.dispatch(HookEvent.POST_TOOL_USE, "shell", {"command": "true"})

    assert seen == [("worker-local", "supervisor-root")]


def test_session_search_excludes_explicit_root(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured = {}

    class _FakeIndex:
        def search(self, query, **kwargs):
            captured["query"] = query
            captured.update(kwargs)
            return []

    monkeypatch.setattr(
        "src.tools.self_learning.session_tools.SessionIndex",
        lambda: _FakeIndex(),
    )

    from src.tools.self_learning.session_tools import session_search

    with bind_root_run("real-root-run"):
        payload = json.loads(session_search("needle", scope="all"))

    assert payload["ok"] is True
    assert captured["exclude_run_id"] == "real-root-run"


def test_canonical_event_carries_hook_run_local_and_root_ids() -> None:
    from src.extensions.self_learning.session_recorder import event_from_hook_context
    from src.lib.smolagents.hooks import HookContext

    event = event_from_hook_context(
        HookContext(
            local_run_id="worker-leaf-run",
            root_run_id="supervisor-root-run",
            cwd="/tmp",
            hook_event_name="TaskCompleted",
            tool_name="",
            tool_input={"task_id": "worker-task"},
        )
    )

    assert event is not None
    assert event.schema_version == 3
    assert event.run_id == "worker-leaf-run"
    assert event.root_run_id == "supervisor-root-run"
    assert event.to_record()["root_run_id"] == "supervisor-root-run"


def test_application_disable_prevents_session_recorder_write(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from src.extensions.self_learning.session_recorder import SessionRecorder
    from src.lib.smolagents.hooks import HookContext

    recorder = SessionRecorder()
    monkeypatch.setattr(
        recorder,
        "append",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("disabled Application must not record history")
        ),
    )

    result = recorder.record_hook(
        HookContext(
            local_run_id="disabled-leaf",
            root_run_id="disabled-root",
            cwd="/tmp",
            hook_event_name="SessionStart",
            tool_name="",
            tool_input={},
            agent_config={"self_learning": {"enabled": False}},
        )
    )

    assert result.decision == "allow"


def test_session_tools_fail_closed_before_reading_without_root_context(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from src.tools.self_learning.session_tools import session_scroll, session_search

    class _MustNotRead:
        def search(self, *_args, **_kwargs):
            raise AssertionError("search must not read without a root context")

        def scroll(self, *_args, **_kwargs):
            raise AssertionError("scroll must not read without a root context")

    monkeypatch.setattr(
        "src.tools.self_learning.session_tools.SessionIndex",
        lambda: _MustNotRead(),
    )

    assert json.loads(session_search("needle"))["error"] == "missing_run_context"
    assert json.loads(session_scroll("old-run", 1))["error"] == "missing_run_context"


def test_disabled_self_learning_tools_do_not_initialize_runtime_or_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / ".agentloom"
    monkeypatch.setenv("AGENTLOOM_RUNTIME_ROOT", str(root))
    monkeypatch.setattr(
        "src.extensions.self_learning.paths.self_learning_enabled",
        lambda _agent_config=None: False,
    )

    from src.tools.self_learning import memory_tool, session_tools

    def _must_not_initialize(*_args, **_kwargs):
        raise AssertionError("disabled tools must not initialize runtime or state")

    monkeypatch.setattr(session_tools, "_current_run_id", _must_not_initialize)
    monkeypatch.setattr(session_tools, "SessionIndex", _must_not_initialize)
    monkeypatch.setattr(memory_tool, "current_session_run_id", _must_not_initialize)
    monkeypatch.setattr(memory_tool, "MemoryStore", _must_not_initialize)
    monkeypatch.setattr(
        memory_tool,
        "_current_agent_config",
        lambda: {"self_learning": {"enabled": False}},
    )

    session_expected = {"ok": False, "error": "self_learning is disabled in config"}
    assert json.loads(session_tools.session_search("historical secret")) == session_expected
    assert json.loads(session_tools.session_scroll("historical-run", 1)) == session_expected
    assert json.loads(
        memory_tool.memory(action="add", scope="project", content="must not persist")
    ) == {"ok": False, "error": "self_learning_disabled"}
    assert not root.exists()


def test_memory_tool_fails_before_creating_state_without_root_context(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / ".agentloom"
    monkeypatch.setenv("AGENTLOOM_RUNTIME_ROOT", str(root))
    from src.tools.self_learning.memory_tool import memory

    result = json.loads(memory("list", scope="project"))

    assert result["error"] == "missing_run_context"
    assert not root.exists()


def test_session_tools_exclude_every_leaf_of_current_root(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("AGENTLOOM_RUNTIME_ROOT", str(tmp_path / ".agentloom"))
    from src.extensions.self_learning.event_schema import CanonicalSessionEvent, now_iso
    from src.extensions.self_learning.ledger import SelfLearningLedger
    from src.tools.self_learning.session_tools import session_scroll, session_search

    ledger = SelfLearningLedger()
    for run_id, root_run_id in (
        ("current-worker-leaf", "current-root"),
        ("prior-worker-leaf", "prior-root"),
    ):
        ledger.append_event(
            CanonicalSessionEvent(
                event_id=uuid.uuid4().hex,
                run_id=run_id,
                root_run_id=root_run_id,
                event_type="tool_result",
                content="ROOT_ISOLATION_MARKER",
                content_text="ROOT_ISOLATION_MARKER",
                created_at=now_iso(),
            )
        )

    with bind_root_run("current-root"):
        payload = json.loads(session_search("ROOT_ISOLATION_MARKER", scope="all"))
        assert [item["run_id"] for item in payload["results"]] == ["prior-worker-leaf"]
        with pytest.raises(ValueError, match="current root"):
            session_scroll("current-worker-leaf", 1)
