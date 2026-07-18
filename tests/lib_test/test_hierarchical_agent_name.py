"""Tests for hierarchical runtime agent path in task context.

When a worker agent runs inside a parent agent, the runtime agent path
is built as ``parent/child`` so canonical workspaces nest correctly.
This path is separate from the agent_name (used in logs, checkpoints) which
remains flat.

The hierarchy is built in ``RoleDrivenAgent._execute_agent()`` and propagated
via the ``runtime_agent_path`` ContextVar + ``RUNTIME_AGENT_PATH`` env var.
"""

import pytest

from src.trace.task_context import (
    get_current_agent_name,
    set_current_agent_name,
    clear_current_agent_name,
    get_current_runtime_agent_path,
    set_current_runtime_agent_path,
    clear_current_runtime_agent_path,
    sub_task_context,
)


# ---------------------------------------------------------------------------
# sub_task_context: unchanged behaviour (agent_name stays flat)
# ---------------------------------------------------------------------------

class TestSubTaskContextBasic:
    """Verify sub_task_context sets and restores agent name correctly."""

    def setup_method(self):
        clear_current_agent_name()
        clear_current_runtime_agent_path()

    def teardown_method(self):
        clear_current_agent_name()
        clear_current_runtime_agent_path()

    def test_sets_agent_name(self):
        """sub_task_context should set agent name for the block duration."""
        with sub_task_context("worker_a"):
            assert get_current_agent_name() == "worker_a"

    def test_restores_previous_name(self):
        """Agent name should revert after the block exits."""
        set_current_agent_name("parent")
        with sub_task_context("child"):
            assert get_current_agent_name() == "child"
        assert get_current_agent_name() == "parent"

    def test_clears_when_no_previous(self):
        """Agent name should be cleared if there was none before the block."""
        assert get_current_agent_name() is None
        with sub_task_context("ephemeral"):
            assert get_current_agent_name() == "ephemeral"
        assert get_current_agent_name() is None

    def test_agent_name_remains_flat(self):
        """sub_task_context should NOT build hierarchical agent_name."""
        set_current_agent_name("parent")
        with sub_task_context("child"):
            # agent_name is just the bare child name, NOT "parent/child"
            assert get_current_agent_name() == "child"

    def test_yields_sub_task_id(self):
        """The context manager should yield a non-empty sub-task ID."""
        with sub_task_context("worker") as sub_id:
            assert sub_id
            assert isinstance(sub_id, str)
            assert len(sub_id) > 0


# ---------------------------------------------------------------------------
# Runtime agent path ContextVar
# ---------------------------------------------------------------------------

class TestRuntimeAgentPath:
    """Test the runtime_agent_path ContextVar (separate from agent_name)."""

    def setup_method(self):
        clear_current_agent_name()
        clear_current_runtime_agent_path()

    def teardown_method(self):
        clear_current_agent_name()
        clear_current_runtime_agent_path()

    def test_default_is_none(self):
        """runtime_agent_path should be None by default."""
        assert get_current_runtime_agent_path() is None

    def test_set_and_get(self):
        """Setting runtime_agent_path should be retrievable."""
        set_current_runtime_agent_path("supervisor/worker")
        assert get_current_runtime_agent_path() == "supervisor/worker"

    def test_clear(self):
        """Clearing runtime_agent_path should return None."""
        set_current_runtime_agent_path("some/path")
        clear_current_runtime_agent_path()
        assert get_current_runtime_agent_path() is None

    def test_independent_from_agent_name(self):
        """runtime_agent_path and agent_name should be independent."""
        set_current_agent_name("worker_bare")
        set_current_runtime_agent_path("parent/worker_bare")
        assert get_current_agent_name() == "worker_bare"
        assert get_current_runtime_agent_path() == "parent/worker_bare"


# ---------------------------------------------------------------------------
# Hierarchical runtime path construction (simulating _execute_agent)
# ---------------------------------------------------------------------------

class TestHierarchicalPathConstruction:
    """Simulate the parent->worker flow and verify runtime path logic.

    The hierarchy is built by _execute_agent() which reads the current
    runtime_agent_path (the parent) and prepends it to the worker's own name.
    """

    def setup_method(self):
        clear_current_agent_name()
        clear_current_runtime_agent_path()

    def teardown_method(self):
        clear_current_agent_name()
        clear_current_runtime_agent_path()

    @staticmethod
    def _build_runtime_path(previous_path, child_name):
        """Replicate the logic from _execute_agent()."""
        if previous_path:
            return f"{previous_path}/{child_name}"
        return child_name

    def test_worker_under_parent(self):
        """Worker inside a parent should get 'parent/child' path."""
        path = self._build_runtime_path("supervisor", "worker_a")
        assert path == "supervisor/worker_a"

    def test_no_parent_context(self):
        """Without a parent, worker keeps its bare name."""
        path = self._build_runtime_path(None, "standalone")
        assert path == "standalone"

    def test_empty_parent(self):
        """Empty string parent should not produce '/child'."""
        path = self._build_runtime_path("", "child")
        assert path == "child"

    def test_same_name_still_creates_a_distinct_child_identity(self):
        """A same-named child remains isolated from its parent workspace."""
        path = self._build_runtime_path("agent_x", "agent_x")
        assert path == "agent_x/agent_x"

    def test_three_level_nesting(self):
        """Simulate grandparent->parent->child nesting."""
        # Level 1: supervisor runs
        set_current_runtime_agent_path("supervisor")

        # Level 2: parent worker runs inside supervisor
        parent_path = self._build_runtime_path(
            get_current_runtime_agent_path(), "parent_worker"
        )
        assert parent_path == "supervisor/parent_worker"

        # Simulate _execute_agent setting the path
        set_current_runtime_agent_path(parent_path)

        # Level 3: child worker runs inside parent worker
        child_path = self._build_runtime_path(
            get_current_runtime_agent_path(), "child_worker"
        )
        assert child_path == "supervisor/parent_worker/child_worker"

    def test_context_restoration_after_nested_run(self):
        """After worker completes, parent context should be restored."""
        set_current_runtime_agent_path("supervisor")

        # Simulate worker execution with save/restore (like _execute_agent)
        previous = get_current_runtime_agent_path()
        runtime_path = self._build_runtime_path(previous, "worker")
        set_current_runtime_agent_path(runtime_path)
        assert get_current_runtime_agent_path() == "supervisor/worker"

        # Simulate worker completion and restoration
        set_current_runtime_agent_path(previous)
        assert get_current_runtime_agent_path() == "supervisor"


# ---------------------------------------------------------------------------
# Full flow: agent_name stays flat, runtime_path is hierarchical
# ---------------------------------------------------------------------------

class TestFullFlowSimulation:
    """Simulate the complete supervisor->worker execution flow."""

    def setup_method(self):
        clear_current_agent_name()
        clear_current_runtime_agent_path()

    def teardown_method(self):
        clear_current_agent_name()
        clear_current_runtime_agent_path()

    def test_agent_name_flat_runtime_path_nested(self):
        """agent_name stays flat; runtime_agent_path is hierarchical."""
        # Supervisor sets both
        set_current_agent_name("ai_check_agent")
        set_current_runtime_agent_path("ai_check_agent")

        # Worker enters (simulating _execute_agent)
        previous_name = get_current_agent_name()
        previous_path = get_current_runtime_agent_path()

        set_current_agent_name("step0_preparation")  # flat!
        runtime_path = f"{previous_path}/step0_preparation"
        set_current_runtime_agent_path(runtime_path)

        assert get_current_agent_name() == "step0_preparation"
        assert get_current_runtime_agent_path() == "ai_check_agent/step0_preparation"

        # Worker exits — restore
        set_current_agent_name(previous_name)
        set_current_runtime_agent_path(previous_path)

        assert get_current_agent_name() == "ai_check_agent"
        assert get_current_runtime_agent_path() == "ai_check_agent"

    def test_multiple_workers_sequential(self):
        """Multiple workers called sequentially should each get correct nesting."""
        set_current_agent_name("supervisor")
        set_current_runtime_agent_path("supervisor")

        for worker_name in ("step0", "step1", "step2"):
            prev_name = get_current_agent_name()
            prev_path = get_current_runtime_agent_path()
            expected_path = f"supervisor/{worker_name}"

            # Worker enters
            set_current_agent_name(worker_name)
            set_current_runtime_agent_path(expected_path)

            assert get_current_agent_name() == worker_name
            assert get_current_runtime_agent_path() == expected_path

            # Worker exits
            set_current_agent_name(prev_name)
            set_current_runtime_agent_path(prev_path)

        assert get_current_agent_name() == "supervisor"
        assert get_current_runtime_agent_path() == "supervisor"

    def test_workspace_path_with_hierarchical_name(self):
        """Verify that Path handles hierarchical agent workspace names."""
        from pathlib import Path
        import tempfile

        agent_path = "ai_check_agent/step0_preparation"

        with tempfile.TemporaryDirectory() as tmpdir:
            workspace_base = Path(tmpdir) / ".agentloom" / "workspaces" / "agents" / "app"
            rd = workspace_base / agent_path
            rd.mkdir(parents=True, exist_ok=True)

            assert rd.exists()
            assert (workspace_base / "ai_check_agent").is_dir()
            assert (workspace_base / "ai_check_agent" / "step0_preparation").is_dir()

            # Verify nested structure, not flat
            flat_path = workspace_base / "ai_check_agent_step0_preparation"
            assert not flat_path.exists()

    def test_deeply_nested_workspace_dir(self):
        """Three-level nesting should create proper directory hierarchy."""
        from pathlib import Path
        import tempfile

        agent_path = "root/parent/child"

        with tempfile.TemporaryDirectory() as tmpdir:
            workspace_base = Path(tmpdir) / ".agentloom" / "workspaces" / "agents" / "app"
            rd = workspace_base / agent_path
            rd.mkdir(parents=True, exist_ok=True)

            assert (workspace_base / "root").is_dir()
            assert (workspace_base / "root" / "parent").is_dir()
            assert (workspace_base / "root" / "parent" / "child").is_dir()


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------

class TestEdgeCases:
    """Edge cases for hierarchical runtime path building."""

    def setup_method(self):
        clear_current_agent_name()
        clear_current_runtime_agent_path()

    def teardown_method(self):
        clear_current_agent_name()
        clear_current_runtime_agent_path()

    def test_exception_in_sub_task_context_restores_name(self):
        """Even on exception, previous agent name should be restored."""
        set_current_agent_name("parent")
        with pytest.raises(ValueError):
            with sub_task_context("child"):
                assert get_current_agent_name() == "child"
                raise ValueError("test error")
        assert get_current_agent_name() == "parent"

    def test_runtime_path_survives_sub_task_context(self):
        """sub_task_context should not touch runtime_agent_path."""
        set_current_runtime_agent_path("parent/child")
        with sub_task_context("whatever"):
            # runtime_agent_path is not managed by sub_task_context
            assert get_current_runtime_agent_path() == "parent/child"
        assert get_current_runtime_agent_path() == "parent/child"

    def test_partial_prefix_match(self):
        """'parent_ext' should not be considered same as 'parent'."""
        path = TestHierarchicalPathConstruction._build_runtime_path(
            "parent_ext", "parent"
        )
        assert path == "parent_ext/parent"
