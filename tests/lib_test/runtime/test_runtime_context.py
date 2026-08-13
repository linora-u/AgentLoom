from pathlib import Path

import pytest


def test_runtime_home_builds_run_and_checkpoint_paths(tmp_path: Path) -> None:
    from src.lib.runtime import RuntimeHome

    home = RuntimeHome(tmp_path / ".agentloom")

    context = home.context(
        application_id="web/search",
        task_id="task_123",
        run_id="run_abc",
    )

    assert context.run_dir == tmp_path / ".agentloom" / "runs" / "web" / "search" / "run_abc"
    assert context.log_path == context.run_dir / "logs" / "runtime.log"
    assert context.shell_audit_path == context.run_dir / "audit" / "shell.jsonl"
    assert context.artifacts_dir == context.run_dir / "artifacts"
    assert context.checkpoint_dir == tmp_path / ".agentloom" / "checkpoints" / "web" / "search" / "task_123"
    assert not context.run_dir.exists()
    assert not context.checkpoint_dir.exists()


def test_runtime_context_builds_canonical_agent_workspace_paths(tmp_path: Path) -> None:
    from src.lib.runtime import RuntimeHome

    context = RuntimeHome(tmp_path / ".agentloom").context(
        application_id="web/search",
        task_id="task_123",
        run_id="run_abc",
    )

    agent_root = (
        tmp_path
        / ".agentloom"
        / "workspaces"
        / "agents"
        / "web"
        / "search"
        / "supervisor"
        / "researcher"
    )
    assert context.agent_workspace_root("supervisor/researcher") == agent_root
    assert context.agent_insights_path("supervisor/researcher") == agent_root / "insights.md"
    assert context.agent_task_workspace_dir("supervisor/researcher") == (
        agent_root / "tasks" / "task_123"
    )


def test_agent_insights_are_shared_but_task_workspace_is_isolated(tmp_path: Path) -> None:
    from src.lib.runtime import RuntimeHome

    home = RuntimeHome(tmp_path / ".agentloom")
    first = home.context(application_id="app", task_id="task_1", run_id="run_1")
    second = home.context(application_id="app", task_id="task_2", run_id="run_2")

    assert first.agent_insights_path("supervisor/worker") == second.agent_insights_path(
        "supervisor/worker"
    )
    assert first.agent_task_workspace_dir(
        "supervisor/worker"
    ) != second.agent_task_workspace_dir("supervisor/worker")


def test_agent_workspace_rejects_absolute_and_traversal_paths(tmp_path: Path) -> None:
    from src.lib.runtime import RuntimeHome

    context = RuntimeHome(tmp_path / ".agentloom").context(
        application_id="app",
        task_id="task",
        run_id="run",
    )

    for agent_path in ("", "/supervisor", "../worker", "supervisor/../worker"):
        with pytest.raises(ValueError, match="agent_path"):
            context.agent_workspace_root(agent_path)


def test_prepare_agent_workspace_creates_only_canonical_runtime_directories(
    tmp_path: Path,
) -> None:
    from src.lib.runtime import RuntimeHome

    context = RuntimeHome(tmp_path / ".agentloom").context(
        application_id="app",
        task_id="task",
        run_id="run",
    )

    task_dir = context.prepare_agent_workspace("supervisor/worker")

    assert task_dir == context.agent_task_workspace_dir("supervisor/worker")
    assert task_dir.is_dir()
    assert context.agent_workspace_root("supervisor/worker").is_dir()
    assert not (tmp_path / ".runtime").exists()


def test_runtime_home_resolves_relative_root_against_agent_root(tmp_path: Path) -> None:
    from src.lib.runtime import resolve_runtime_home

    home = resolve_runtime_home(
        {"runtime": {"root_dir": "state/runtime"}},
        agent_root=tmp_path,
    )

    assert home.root_dir == (tmp_path / "state" / "runtime").resolve()


def test_runtime_context_rejects_path_traversal(tmp_path: Path) -> None:
    from src.lib.runtime import RuntimeContext, RuntimeHome

    home = RuntimeHome(tmp_path)
    with pytest.raises(ValueError):
        home.context(application_id="../other", task_id="task", run_id="run")
    with pytest.raises(ValueError):
        home.context(application_id="app", task_id="../task", run_id="run")
    with pytest.raises(ValueError):
        RuntimeContext(tmp_path, "../outside", "task", "run")


def test_runtime_ids_reject_lossy_sanitization_and_cannot_collide(tmp_path: Path) -> None:
    from src.lib.runtime import RuntimeHome

    home = RuntimeHome(tmp_path)
    canonical = home.context(application_id="app", task_id="task_x", run_id="run_x")

    for task_id in ("task@x", "task#x", "中文任务"):
        with pytest.raises(ValueError):
            home.context(application_id="app", task_id=task_id, run_id="run_x")
    for run_id in ("run@x", "run#x", "中文执行"):
        with pytest.raises(ValueError):
            home.context(application_id="app", task_id="task_x", run_id=run_id)

    assert canonical.checkpoint_dir.name == "task_x"


def test_external_workflow_application_id_is_stable_and_collision_safe(tmp_path: Path) -> None:
    from src.lib.runtime import resolve_application_id

    first = tmp_path / "one" / "agent.yaml"
    second = tmp_path / "two" / "agent.yaml"

    assert resolve_application_id({}, first) == resolve_application_id({}, first)
    assert resolve_application_id({}, first) != resolve_application_id({}, second)


def test_unicode_application_ids_use_stable_portable_components() -> None:
    from src.lib.runtime import safe_application_id

    first = safe_application_id("中文应用")
    second = safe_application_id("另一个应用")

    assert first.startswith("app-")
    assert first == safe_application_id("中文应用")
    assert first != second
    assert first.isascii()


def test_unicode_external_workflow_name_falls_back_to_safe_name_and_hash(tmp_path: Path) -> None:
    from src.lib.runtime import fallback_application_id

    workflow = tmp_path / "测试.yaml"
    application_id = fallback_application_id(workflow, name_hint="中文名字")

    assert application_id.startswith("external-")
    assert application_id == fallback_application_id(workflow, name_hint="中文名字")
    assert application_id.isascii()


def test_generated_attempt_ids_do_not_collide_within_the_same_clock_tick() -> None:
    from src.lib.runtime import generate_runtime_id

    generated = {generate_runtime_id("run") for _ in range(100)}

    assert len(generated) == 100


def test_failed_root_memory_read_freezes_an_empty_snapshot() -> None:
    from src.lib.runtime import RootRunState

    state = RootRunState("root-memory-read-failed")
    calls = 0

    def fail_initial_read() -> str:
        nonlocal calls
        calls += 1
        raise RuntimeError("database unavailable")

    with pytest.raises(RuntimeError, match="database unavailable"):
        state.get_or_create_memory_snapshot(fail_initial_read)

    assert state.get_or_create_memory_snapshot(lambda: "late memory") == ""
    assert calls == 1


def test_artifact_allocator_rejects_cross_run_symlink(tmp_path: Path) -> None:
    from src.lib.runtime import RuntimeHome

    home = RuntimeHome(tmp_path / ".agentloom")
    first = home.context(application_id="first", task_id="task", run_id="run")
    second = home.context(application_id="second", task_id="task", run_id="run")
    first.prepare_run()
    second.prepare_run()
    first.shell_artifacts_dir.rmdir()
    first.shell_artifacts_dir.symlink_to(
        second.shell_artifacts_dir,
        target_is_directory=True,
    )

    with pytest.raises(RuntimeError, match="safe directory"):
        first.allocate_artifact("shell", prefix="cmd-", suffix=".txt")

    assert list(second.shell_artifacts_dir.iterdir()) == []


def test_bound_run_home_owns_self_learning_state(tmp_path: Path, monkeypatch) -> None:
    from src.extensions.self_learning.paths import self_learning_root
    from src.lib.runtime import RuntimeHome, bind_run_context

    context = RuntimeHome(tmp_path / "canonical").context(
        application_id="app",
        task_id="task",
        run_id="run",
    )
    monkeypatch.setenv("AGENTLOOM_SELF_LEARNING_ROOT", str(tmp_path / "legacy-override"))

    with bind_run_context(context):
        assert self_learning_root() == context.root_dir


def test_legacy_self_learning_env_cannot_split_unbound_runtime_root(
    tmp_path: Path,
    monkeypatch,
) -> None:
    from src.extensions.self_learning import paths

    configured_root = tmp_path / "canonical"
    monkeypatch.setenv("AGENTLOOM_SELF_LEARNING_ROOT", str(tmp_path / "legacy"))
    monkeypatch.setattr(
        paths,
        "_runtime_config_section",
        lambda: {"root_dir": str(configured_root)},
    )

    assert paths.self_learning_root() == configured_root


def test_canonical_runtime_env_moves_every_runtime_consumer_together(
    tmp_path: Path,
    monkeypatch,
) -> None:
    from src.extensions.self_learning.paths import self_learning_root
    from src.lib.runtime import resolve_runtime_home

    override = tmp_path / "isolated-runtime"
    monkeypatch.setenv("AGENTLOOM_RUNTIME_ROOT", str(override))

    home = resolve_runtime_home(
        {"runtime": {"root_dir": str(tmp_path / "configured")}},
        agent_root=tmp_path,
    )

    assert home.root_dir == override
    assert self_learning_root() == override
