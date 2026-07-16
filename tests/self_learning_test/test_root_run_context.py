"""Root-run ownership and propagation contracts for self-learning."""

from __future__ import annotations

import json
import threading
import tomllib
import uuid
from concurrent.futures import ThreadPoolExecutor
from contextvars import copy_context
from importlib.metadata import version
from pathlib import Path

import pytest
from packaging.requirements import Requirement

from src.lib.smolagents.hooks.hook_manager import HookManager, _context_to_dict
from src.lib.smolagents.hooks.types import HookContext, HookEvent, HookResult
from src.trace import (
    ExplicitExecutionContext,
    MissingRunContextError,
    bind_explicit_execution_context,
    bind_local_run,
    bind_root_run,
    capture_explicit_execution_context,
    get_current_session_run_id,
    require_root_run_id,
)


def test_smolagents_context_patch_dependency_is_exactly_pinned():
    """Private timeout integration must not float beyond the verified version."""
    project_root = Path(__file__).parents[2]
    project = tomllib.loads((project_root / "pyproject.toml").read_text())
    dependency = next(
        Requirement(raw)
        for raw in project["project"]["dependencies"]
        if Requirement(raw).name == "smolagents"
    )

    assert str(dependency.specifier) == "==1.26.0"
    assert version("smolagents") == "1.26.0"


def test_require_root_run_id_fails_closed_without_binding():
    with pytest.raises(MissingRunContextError, match="root run"):
        require_root_run_id()


def test_bind_root_run_owns_outer_binding_and_nested_calls_inherit_it():
    assert get_current_session_run_id() is None

    with bind_root_run("supervisor-run") as outer_owner:
        assert outer_owner is True
        assert require_root_run_id() == "supervisor-run"

        with bind_root_run("worker-local-session") as nested_owner:
            assert nested_owner is False
            assert require_root_run_id() == "supervisor-run"

        assert require_root_run_id() == "supervisor-run"

    assert get_current_session_run_id() is None


def test_copied_worker_context_inherits_root_without_ownership():
    with bind_root_run("supervisor-run"):
        worker_context = copy_context()

        def _worker():
            with bind_root_run("worker-local-session") as owns_root:
                return owns_root, require_root_run_id()

        with ThreadPoolExecutor(max_workers=1) as pool:
            owns_root, root_run_id = pool.submit(worker_context.run, _worker).result()

    assert owns_root is False
    assert root_run_id == "supervisor-run"


@pytest.mark.parametrize("run_id", ["", "   ", None])
def test_bind_root_run_rejects_empty_identity(run_id):
    with pytest.raises(ValueError, match="root run id"):
        with bind_root_run(run_id):
            pass


def test_hook_manager_captures_root_run_before_parallel_dispatch():
    manager = HookManager()
    seen = []

    def _capture(context):
        seen.append(context)
        return HookResult(success=True, decision="allow")

    manager.register_hook(HookEvent.POST_TOOL_USE, "*", _capture)
    with bind_root_run("root-for-hook"):
        manager.trigger_hooks(HookEvent.POST_TOOL_USE, "shell", {"command": "true"})

    assert len(seen) == 1
    assert seen[0].root_run_id == "root-for-hook"
    assert _context_to_dict(seen[0])["root_run_id"] == "root-for-hook"


def test_hook_context_uses_explicit_local_id_and_root_without_manager_fallback():
    manager = HookManager()
    seen = []

    def _capture(context):
        seen.append(context)
        return HookResult(success=True, decision="allow")

    manager.register_hook(HookEvent.POST_TOOL_USE, "*", _capture)
    with bind_local_run("worker-leaf"):
        with bind_root_run("supervisor-root"):
            manager.trigger_hooks(HookEvent.POST_TOOL_USE, "shell", {})

    assert len(seen) == 1
    assert seen[0].session_id == "worker-leaf"
    assert seen[0].root_run_id == "supervisor-root"


def test_unbound_builtin_recorder_does_not_create_persistent_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    """A manager construction UUID is not a run identity."""
    from src.lib.smolagents.hooks import register_builtin_hooks

    state_root = tmp_path / ".agentloom"
    monkeypatch.setenv("AGENTLOOM_SELF_LEARNING_ROOT", str(state_root))
    manager = register_builtin_hooks(HookManager())
    seen: list[HookContext] = []
    manager.register_hook(
        HookEvent.POST_TOOL_USE,
        "*",
        lambda context: seen.append(context)
        or HookResult(success=True, decision="allow"),
    )

    manager.trigger_hooks(
        HookEvent.POST_TOOL_USE,
        "shell",
        {"command": "true"},
        tool_response={"result": "ok"},
    )

    assert len(seen) == 1
    assert seen[0].session_id == ""
    assert seen[0].root_run_id is None
    assert not (state_root / "self_learning.db").exists()
    assert not (state_root / "sessions" / "events").exists()


def test_parallel_python_hooks_rebind_full_explicit_context_for_agent_policy():
    """Pool dispatch plus the raw-hook timeout thread must preserve policy context."""
    from src.lib.smolagents.hooks.path_validators import validate_workspace_path

    manager = HookManager()
    seen = []

    def _capture_context(context):
        seen.append(capture_explicit_execution_context())
        return HookResult(success=True, decision="allow")

    manager.register_hook(HookEvent.PRE_TOOL_USE, "*", validate_workspace_path)
    # A second hook forces HookManager through its ThreadPoolExecutor path;
    # each raw Python hook then crosses the daemon timeout-thread boundary.
    manager.register_hook(HookEvent.PRE_TOOL_USE, "*", _capture_context)
    explicit = ExplicitExecutionContext(
        task_id="policy-task",
        sub_task_id="policy-subtask",
        agent_id="policy-agent-id",
        agent_name="policy-agent",
        agent_config={
            "tool_access_control": {
                "path_validation": [
                    {"tools": ["read_file"], "exclude_paths": ["*"]}
                ]
            }
        },
        skills_manager=None,
        hook_manager=manager,
        runtime_agent_path="policy-agent",
        root_run_id="policy-root",
        local_run_id="policy-leaf",
    )

    with bind_explicit_execution_context(explicit):
        result = manager.trigger_hooks(
            HookEvent.PRE_TOOL_USE,
            "read_file",
            {"file_path": "/tmp/must-be-denied"},
            tool_inputs_schema={"file_path": {"type": "string"}},
        )

    assert result.decision == "block"
    assert len(seen) == 1
    assert seen[0] == explicit


def test_local_python_executor_propagates_full_context_across_interleaved_runs():
    # Importing BaseAgent installs the LocalPythonExecutor ContextVar adapter.
    from smolagents import Tool
    from smolagents.local_python_executor import LocalPythonExecutor

    import src.lib.smolagents.agent.base_agent  # noqa: F401
    from src.lib.smolagents.hooks.tool_shim import inject_hooks

    execution_barrier = threading.Barrier(2)
    hook_contexts: dict[str, list[HookContext]] = {"A": [], "B": []}

    class _ProbeTool(Tool):
        name = "context_probe"
        description = "Return the explicit AgentLoom execution context."
        inputs = {"label": {"type": "string", "description": "Run label"}}
        output_type = "string"

        def forward(self, label: str) -> str:
            execution_barrier.wait(timeout=5)
            current = capture_explicit_execution_context()
            return json.dumps(
                {
                    "label": label,
                    "task_id": current.task_id,
                    "sub_task_id": current.sub_task_id,
                    "agent_name": current.agent_name,
                    "agent_config": current.agent_config,
                    "root_run_id": current.root_run_id,
                    "local_run_id": current.local_run_id,
                    "hook_manager_matches": current.hook_manager is managers[label],
                }
            )

    managers = {"A": HookManager(), "B": HookManager()}
    for label, manager in managers.items():
        manager.register_hook(
            HookEvent.PRE_TOOL_USE,
            "context_probe",
            lambda context, current_label=label: hook_contexts[current_label].append(context)
            or HookResult(success=True, decision="allow"),
        )

    def _run(label: str) -> dict:
        explicit = ExplicitExecutionContext(
            task_id=f"task-{label}",
            sub_task_id=f"subtask-{label}",
            agent_id=f"agent-id-{label}",
            agent_name=f"agent-{label}",
            agent_config={"label": label},
            skills_manager=None,
            hook_manager=managers[label],
            runtime_agent_path=f"supervisor/agent-{label}",
            root_run_id=f"root-{label}",
            local_run_id=f"leaf-{label}",
        )
        with bind_explicit_execution_context(explicit):
            tool = inject_hooks(_ProbeTool())
            executor = LocalPythonExecutor([], timeout_seconds=10)
            executor.send_tools({tool.name: tool})
            output = executor(f'context_probe(label="{label}")').output
            return json.loads(output)

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = {
            label: future.result(timeout=15)
            for label, future in {
                label: pool.submit(_run, label) for label in ("A", "B")
            }.items()
        }

    for label in ("A", "B"):
        assert results[label] == {
            "label": label,
            "task_id": f"task-{label}",
            "sub_task_id": f"subtask-{label}",
            "agent_name": f"agent-{label}",
            "agent_config": {"label": label},
            "root_run_id": f"root-{label}",
            "local_run_id": f"leaf-{label}",
            "hook_manager_matches": True,
        }
        assert len(hook_contexts[label]) == 1
        assert hook_contexts[label][0].session_id == f"leaf-{label}"
        assert hook_contexts[label][0].root_run_id == f"root-{label}"
        assert hook_contexts[label][0].task_id == f"task-{label}"
        assert hook_contexts[label][0].sub_task_id == f"subtask-{label}"
        assert hook_contexts[label][0].agent_name == f"agent-{label}"
        assert hook_contexts[label][0].agent_config == {"label": label}


def test_shared_tool_source_gets_isolated_runtime_context_for_interleaved_runs():
    from smolagents import Tool

    from src.lib.smolagents.hooks.tool_shim import (
        clone_tool_for_runtime,
        inject_hooks,
    )

    call_barrier = threading.Barrier(2)

    class _SharedProbe(Tool):
        name = "shared_context_probe"
        description = "Return the current run identity."
        inputs = {}
        output_type = "string"

        def forward(self) -> str:
            call_barrier.wait(timeout=5)
            current = capture_explicit_execution_context()
            return json.dumps(
                {
                    "task_id": current.task_id,
                    "root_run_id": current.root_run_id,
                    "local_run_id": current.local_run_id,
                    "agent_name": current.agent_name,
                }
            )

    shared_source = _SharedProbe()
    prepared_barrier = threading.Barrier(2)

    class _AllowHooks:
        def trigger_hooks(self, *_args, **_kwargs):
            return HookResult(success=True, decision="allow")

        def flush_user_messages(self):
            return []

    def _run(label: str) -> dict:
        explicit = ExplicitExecutionContext(
            task_id=f"task-{label}",
            sub_task_id=f"subtask-{label}",
            agent_id=f"agent-id-{label}",
            agent_name=f"agent-{label}",
            agent_config={"label": label},
            skills_manager=None,
            hook_manager=_AllowHooks(),
            runtime_agent_path=f"agent-{label}",
            root_run_id=f"root-{label}",
            local_run_id=f"leaf-{label}",
        )
        with bind_explicit_execution_context(explicit):
            runtime_tool = inject_hooks(clone_tool_for_runtime(shared_source))
        prepared_barrier.wait(timeout=5)
        # A custom executor deliberately strips ContextVars.  Each runtime
        # clone must restore its own immutable snapshot, not whichever run
        # most recently prepared the shared source Tool.
        with ThreadPoolExecutor(max_workers=1) as executor:
            return json.loads(executor.submit(runtime_tool.forward).result(timeout=10))

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = {label: pool.submit(_run, label) for label in ("A", "B")}
        results = {label: future.result(timeout=15) for label, future in futures.items()}

    assert results == {
        "A": {
            "task_id": "task-A",
            "root_run_id": "root-A",
            "local_run_id": "leaf-A",
            "agent_name": "agent-A",
        },
        "B": {
            "task_id": "task-B",
            "root_run_id": "root-B",
            "local_run_id": "leaf-B",
            "agent_name": "agent-B",
        },
    }


def test_session_search_excludes_explicit_root_not_global_hook_manager(monkeypatch):
    captured = {}

    class _FakeIndex:
        def search(self, query, **kwargs):
            captured["query"] = query
            captured.update(kwargs)
            return []

    class _WrongManager:
        _session_id = "wrong-worker-session"

    monkeypatch.setattr(
        "src.tools.self_learning.session_tools.SessionIndex", lambda: _FakeIndex()
    )
    monkeypatch.setattr("src.trace.get_current_hook_manager", lambda: _WrongManager())

    from src.tools.self_learning.session_tools import session_search

    with bind_root_run("real-root-run"):
        payload = json.loads(session_search("needle", scope="all"))

    assert payload["ok"] is True
    assert captured["exclude_run_id"] == "real-root-run"


def test_canonical_event_v3_carries_explicit_root_run_id():
    from src.extensions.self_learning.session_recorder import event_from_hook_context

    event = event_from_hook_context(
        HookContext(
            session_id="worker-leaf-run",
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


def test_application_disable_prevents_session_recorder_write(monkeypatch):
    from src.extensions.self_learning.session_recorder import SessionRecorder

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
            session_id="disabled-run",
            root_run_id="disabled-run",
            cwd="/tmp",
            hook_event_name="SessionStart",
            tool_name="",
            tool_input={},
            agent_config={"self_learning": {"enabled": False}},
        )
    )

    assert result.success is True


def test_session_tools_fail_closed_before_reading_without_root_context(monkeypatch):
    from src.tools.self_learning.session_tools import session_scroll, session_search

    class _MustNotRead:
        def search(self, *_args, **_kwargs):
            raise AssertionError("search must not read without a root context")

        def scroll(self, *_args, **_kwargs):
            raise AssertionError("scroll must not read without a root context")

    monkeypatch.setattr(
        "src.tools.self_learning.session_tools.SessionIndex", lambda: _MustNotRead()
    )

    assert json.loads(session_search("needle"))["error"] == "missing_run_context"
    assert json.loads(session_scroll("old-run", 1))["error"] == "missing_run_context"


def test_disabled_session_tools_fail_closed_before_runtime_or_state_access(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / ".agentloom"
    monkeypatch.setenv("AGENTLOOM_SELF_LEARNING_ROOT", str(root))
    monkeypatch.setattr(
        "src.extensions.self_learning.paths.config_bool",
        lambda name, default=True: False if name == "enabled" else default,
    )
    monkeypatch.setattr(
        "src.extensions.self_learning.paths.self_learning_enabled",
        lambda _agent_config=None: False,
    )

    from src.tools.self_learning import memory_tool, session_tools

    def _must_not_initialize(*_args, **_kwargs):
        raise AssertionError("disabled session tools must not initialize runtime or state")

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
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    root = tmp_path / ".agentloom"
    monkeypatch.setenv("AGENTLOOM_SELF_LEARNING_ROOT", str(root))
    from src.tools.self_learning.memory_tool import memory

    result = json.loads(memory("list", scope="project"))

    assert result["error"] == "missing_run_context"
    assert not root.exists()


def test_tool_wrapper_propagates_and_refreshes_root_across_executor_thread(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setenv(
        "AGENTLOOM_SELF_LEARNING_ROOT", str(tmp_path / ".agentloom")
    )
    from src.extensions.self_learning.memory_store import MemoryStore
    from src.lib.smolagents.hooks.tool_shim import inject_hooks
    from src.lib.smolagents.tools.tools import ensure_tool_wrapped
    from src.tools.self_learning import memory_tool
    from src.tools.self_learning.memory_tool import memory

    class _AllowHooks:
        def trigger_hooks(self, *_args, **_kwargs):
            return HookResult(success=True, decision="allow")

        def flush_user_messages(self):
            return []

    monkeypatch.setattr(
        "src.lib.smolagents.hooks.tool_shim._resolve_hook_manager",
        lambda: _AllowHooks(),
    )
    monkeypatch.setattr(
        memory_tool,
        "_current_agent_config",
        lambda: {
            "application_id": "root_context_test",
            "self_learning": {"enabled": True, "memory": {"write_approval": False}},
        },
    )
    wrapped = ensure_tool_wrapped([memory])[0]
    with bind_root_run("root-tool-a"):
        inject_hooks(wrapped)
    with ThreadPoolExecutor(max_workers=1) as executor:
        first = json.loads(
            executor.submit(
                wrapped.forward,
                action="add",
                scope="project",
                content="first threaded durable fact",
            ).result()
        )
    assert first["ok"] is True

    # Cached tools are re-injected by each runtime build; that refresh must
    # replace the previous root rather than leak it into a later run.
    with bind_root_run("root-tool-b"):
        inject_hooks(wrapped)
    with ThreadPoolExecutor(max_workers=1) as executor:
        second = json.loads(
            executor.submit(
                wrapped.forward,
                action="add",
                scope="project",
                content="second threaded durable fact",
            ).result()
        )
    assert second["ok"] is True

    store = MemoryStore()
    assert [item["content"] for item in store.list("project")] == [
        "first threaded durable fact",
        "second threaded durable fact",
    ]

    # Preparing the same cached tool without a binding must clear, rather than
    # retain, root B.  The executor thread then fails closed and writes nothing.
    inject_hooks(wrapped)
    with ThreadPoolExecutor(max_workers=1) as executor:
        missing = json.loads(
            executor.submit(
                wrapped.forward,
                action="add",
                scope="project",
                content="must not inherit a stale root",
            ).result()
        )
    assert missing["error"] == "missing_run_context"
    assert [item["content"] for item in store.list("project")] == [
        "first threaded durable fact",
        "second threaded durable fact",
    ]


def test_session_tools_exclude_every_leaf_of_current_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setenv("AGENTLOOM_SELF_LEARNING_ROOT", str(tmp_path / ".agentloom"))
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
