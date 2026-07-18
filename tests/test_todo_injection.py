"""Tests for todo sync ActionStep injection after PlanningStep.

Covers:
- _validate_todo_prompts: todo prompt key injection with defaults
- _inject_todo_action_step: tool restriction, isolation, retry, error recovery
- _append_todo_result: result message appended to PlanningStep
- _run_stream integration: 3 states (initial/update/final)
- _read_todo_state_for_planning: read-only label
- Bidirectional type coercion patch
- YAML prompt template validation
"""

import json
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch, PropertyMock

import pytest


# ---------------------------------------------------------------------------
# Helpers to build a minimal mixin instance for testing
# ---------------------------------------------------------------------------

def _make_mixin(**overrides):
    """Create a minimal LoomAgentMixin-like object for unit tests."""
    from src.lib.smolagents.agent.loom_mixin import LoomAgentMixin

    class FakeMixin(LoomAgentMixin):
        pass

    obj = object.__new__(FakeMixin)
    obj.tools = overrides.get("tools", {})
    obj.managed_agents = overrides.get("managed_agents", {})
    obj.planning_interval = overrides.get("planning_interval", 5)
    obj.step_number = overrides.get("step_number", 0)
    obj.prompt_templates = overrides.get("prompt_templates", {"planning": {}})
    obj._hook_manager = overrides.get("hook_manager", MagicMock())
    obj.model = overrides.get("model", MagicMock())

    # Use a real list for memory.steps by default
    memory = overrides.get("memory", None)
    if memory is None:
        memory = MagicMock()
        memory.steps = []
    obj.memory = memory
    return obj


def _make_todo_tool():
    """Create a mock todo_write tool."""
    tool = MagicMock()
    tool.name = "todo_write"
    return tool


def _make_planning_templates():
    """Create standard planning templates for tests."""
    return {
        "planning": {
            "todo_initial": "REGISTER TASKS",
            "todo_update": "UPDATE TASKS",
            "todo_final": "FINALIZE TASKS",
        }
    }


# ===========================================================================
# _validate_todo_prompts tests
# ===========================================================================

class TestValidateTodoPrompts:
    """Test prompt validation logic with fail-fast ValueError."""

    def test_all_keys_present_no_error(self):
        """When all 3 todo keys exist, no error raised."""
        obj = _make_mixin(
            tools={"todo_write": _make_todo_tool()},
            prompt_templates=_make_planning_templates(),
        )
        # Should not raise
        obj._validate_todo_prompts()

    def test_missing_single_key_injects_default(self):
        """Missing one key gets injected with default."""
        obj = _make_mixin(
            tools={"todo_write": _make_todo_tool()},
            prompt_templates={
                "planning": {
                    "todo_initial": "custom initial",
                    "todo_update": "custom update",
                    # todo_final missing
                }
            },
        )
        obj._validate_todo_prompts()
        assert obj.prompt_templates["planning"]["todo_final"]
        # Existing values preserved
        assert obj.prompt_templates["planning"]["todo_initial"] == "custom initial"
        assert obj.prompt_templates["planning"]["todo_update"] == "custom update"

    def test_all_keys_missing_injects_defaults(self):
        """All 3 keys missing → all get injected with defaults."""
        obj = _make_mixin(
            tools={"todo_write": _make_todo_tool()},
            prompt_templates={"planning": {}},
        )
        obj._validate_todo_prompts()
        assert obj.prompt_templates["planning"]["todo_initial"]
        assert obj.prompt_templates["planning"]["todo_update"]
        assert obj.prompt_templates["planning"]["todo_final"]

    def test_empty_string_treated_as_missing_and_injected(self):
        """Empty string values are treated as missing and get injected."""
        obj = _make_mixin(
            tools={"todo_write": _make_todo_tool()},
            prompt_templates={
                "planning": {
                    "todo_initial": "",
                    "todo_update": "valid",
                    "todo_final": "",
                }
            },
        )
        obj._validate_todo_prompts()
        assert obj.prompt_templates["planning"]["todo_initial"]
        assert obj.prompt_templates["planning"]["todo_update"] == "valid"
        assert obj.prompt_templates["planning"]["todo_final"]

    def test_no_todo_write_tool_skips_validation(self):
        """Agent without todo_write skips validation entirely."""
        obj = _make_mixin(
            tools={"other_tool": MagicMock()},
            prompt_templates={"planning": {}},
        )
        # Should not raise
        obj._validate_todo_prompts()

    def test_no_planning_interval_skips_validation(self):
        """Agent with planning_interval=None skips validation."""
        obj = _make_mixin(
            tools={"todo_write": _make_todo_tool()},
            planning_interval=None,
            prompt_templates={"planning": {}},
        )
        # Should not raise
        obj._validate_todo_prompts()


# ===========================================================================
# _inject_todo_action_step tests
# ===========================================================================

class TestInjectTodoActionStep:
    """Test the ActionStep injection mechanism."""

    def test_skip_when_no_todo_write(self):
        """No injection when todo_write not in tools."""
        obj = _make_mixin(tools={"other": MagicMock()})
        elements = list(obj._inject_todo_action_step())
        assert elements == []

    def test_skip_when_step_budget_exhausted(self):
        """No injection when step_number >= max_steps - 1."""
        obj = _make_mixin(
            tools={"todo_write": _make_todo_tool()},
            step_number=29,
        )
        elements = list(obj._inject_todo_action_step(max_steps=30))
        assert elements == []

    def test_tool_restriction_applied(self):
        """During injection, self.tools should only contain todo_write."""
        obj = _make_mixin(
            tools={
                "todo_write": _make_todo_tool(),
                "bash": MagicMock(),
                "read_file": MagicMock(),
            },
            prompt_templates=_make_planning_templates(),
        )

        captured_tools = {}

        def fake_step_stream(step):
            # Capture what tools are visible during execution
            captured_tools.update(obj.tools)
            return iter([])

        obj._step_stream = fake_step_stream
        obj._finalize_step = MagicMock()

        list(obj._inject_todo_action_step(todo_state="initial"))

        assert list(captured_tools.keys()) == ["todo_write"]

    def test_tool_restoration_after_success(self):
        """Original tools restored after successful injection."""
        original_tools = {
            "todo_write": _make_todo_tool(),
            "bash": MagicMock(),
        }
        obj = _make_mixin(
            tools=original_tools.copy(),
            prompt_templates=_make_planning_templates(),
        )
        obj._step_stream = MagicMock(return_value=iter([]))
        obj._finalize_step = MagicMock()

        list(obj._inject_todo_action_step())

        assert set(obj.tools.keys()) == {"todo_write", "bash"}

    def test_tool_restoration_after_exception(self):
        """Original tools restored even when all attempts fail."""
        original_tools = {
            "todo_write": _make_todo_tool(),
            "bash": MagicMock(),
        }
        obj = _make_mixin(
            tools=original_tools.copy(),
            prompt_templates=_make_planning_templates(),
        )

        def raise_error(step):
            raise RuntimeError("LLM failed")

        obj._step_stream = raise_error
        obj._finalize_step = MagicMock()

        # Should not raise
        list(obj._inject_todo_action_step())

        # Tools should be restored
        assert set(obj.tools.keys()) == {"todo_write", "bash"}

    def test_prompt_selection_initial(self):
        """todo_state='initial' selects todo_initial prompt."""
        obj = _make_mixin(
            tools={"todo_write": _make_todo_tool()},
            prompt_templates=_make_planning_templates(),
        )
        obj._step_stream = MagicMock(return_value=iter([]))
        obj._finalize_step = MagicMock()

        # Verify prompt is set as system prompt override (not queued as agent context)
        # To verify the correct prompt was selected, capture it during execution
        captured_prompts = []

        def capturing_step_stream(step):
            captured_prompts.append(getattr(obj, '_todo_sys_prompt_override', None))
            return iter([])

        obj._step_stream = capturing_step_stream
        list(obj._inject_todo_action_step(todo_state="initial"))
        assert captured_prompts[0] == "REGISTER TASKS"

    def test_prompt_selection_update(self):
        """todo_state='update' selects todo_update prompt."""
        obj = _make_mixin(
            tools={"todo_write": _make_todo_tool()},
            prompt_templates=_make_planning_templates(),
        )
        obj._step_stream = MagicMock(return_value=iter([]))
        obj._finalize_step = MagicMock()

        captured_prompts = []

        def capturing_step_stream(step):
            captured_prompts.append(getattr(obj, '_todo_sys_prompt_override', None))
            return iter([])

        obj._step_stream = capturing_step_stream
        list(obj._inject_todo_action_step(todo_state="update"))

        assert captured_prompts[0] == "UPDATE TASKS"

    def test_prompt_selection_final(self):
        """todo_state='final' selects todo_final prompt."""
        obj = _make_mixin(
            tools={"todo_write": _make_todo_tool()},
            prompt_templates=_make_planning_templates(),
        )
        obj._step_stream = MagicMock(return_value=iter([]))
        obj._finalize_step = MagicMock()

        captured_prompts = []

        def capturing_step_stream(step):
            captured_prompts.append(getattr(obj, '_todo_sys_prompt_override', None))
            return iter([])

        obj._step_stream = capturing_step_stream
        list(obj._inject_todo_action_step(todo_state="final"))

        assert captured_prompts[0] == "FINALIZE TASKS"

    def test_skip_when_prompt_empty(self):
        """No injection when YAML prompt is empty string."""
        obj = _make_mixin(
            tools={"todo_write": _make_todo_tool()},
            prompt_templates={"planning": {"todo_update": ""}},
        )
        obj._step_stream = MagicMock(return_value=iter([]))
        obj._finalize_step = MagicMock()

        elements = list(obj._inject_todo_action_step(todo_state="update"))
        assert elements == []

    def test_no_hook_manager_still_works(self):
        """Injection proceeds when hook_manager is None."""
        obj = _make_mixin(
            tools={"todo_write": _make_todo_tool()},
            prompt_templates=_make_planning_templates(),
            hook_manager=None,
        )
        obj._step_stream = MagicMock(return_value=iter([]))
        obj._finalize_step = MagicMock()

        with patch("src.lib.smolagents.agent.todo_sync.get_current_hook_manager", return_value=None):
            # Should not raise
            list(obj._inject_todo_action_step())

    def test_managed_agents_cleared_during_injection(self):
        """managed_agents should be empty during injection."""
        obj = _make_mixin(
            tools={"todo_write": _make_todo_tool()},
            managed_agents={"worker": MagicMock()},
            prompt_templates=_make_planning_templates(),
        )

        captured_agents = {}

        def fake_step_stream(step):
            captured_agents.update(obj.managed_agents)
            return iter([])

        obj._step_stream = fake_step_stream
        obj._finalize_step = MagicMock()

        list(obj._inject_todo_action_step())

        assert captured_agents == {}
        # Restored after
        assert "worker" in obj.managed_agents

    def test_no_retry_when_first_succeeds(self):
        """When first LLM attempt succeeds, no retry happens."""
        obj = _make_mixin(
            tools={"todo_write": _make_todo_tool()},
            prompt_templates=_make_planning_templates(),
        )
        obj._step_stream = MagicMock(return_value=iter([]))
        obj._finalize_step = MagicMock()

        # Verify override is set during execution and cleaned up after
        captured_overrides = []

        def capturing_step_stream(step):
            captured_overrides.append(getattr(obj, '_todo_sys_prompt_override', None))
            return iter([])

        obj._step_stream = capturing_step_stream
        list(obj._inject_todo_action_step())

        # Only one attempt (no retry)
        assert len(captured_overrides) == 1
        # Override was set during execution
        assert captured_overrides[0] == "UPDATE TASKS"
        # Override is cleaned up after
        assert obj._todo_sys_prompt_override is None


# ===========================================================================
# Success path tests
# ===========================================================================

class TestTodoSyncSuccess:
    """Test successful todo sync scenarios."""

    def test_success_on_first_attempt(self):
        """First attempt succeeds: memory clean, result appended."""
        from smolagents.memory import PlanningStep

        plan_step = MagicMock(spec=PlanningStep)
        plan_step.observations = ""

        memory = MagicMock()
        memory.steps = [plan_step]

        obj = _make_mixin(
            tools={"todo_write": _make_todo_tool()},
            prompt_templates=_make_planning_templates(),
            memory=memory,
        )
        obj._step_stream = MagicMock(return_value=iter([]))
        obj._finalize_step = MagicMock()

        list(obj._inject_todo_action_step(todo_state="update"))

        # Memory should be back to just the PlanningStep (intermediate cleared)
        assert len(memory.steps) == 1
        assert isinstance(memory.steps[0], PlanningStep)

        # Result message appended
        assert "updated successfully" in plan_step.observations

    def test_success_on_third_attempt(self):
        """First 2 fail, third succeeds: final memory clean."""
        from smolagents.memory import PlanningStep

        plan_step = MagicMock(spec=PlanningStep)
        plan_step.observations = ""

        memory = MagicMock()
        memory.steps = [plan_step]

        obj = _make_mixin(
            tools={"todo_write": _make_todo_tool()},
            prompt_templates=_make_planning_templates(),
            memory=memory,
        )
        obj._finalize_step = MagicMock()

        call_count = 0

        def step_stream_fail_then_succeed(step):
            nonlocal call_count
            call_count += 1
            if call_count < 3:
                raise RuntimeError("Unknown tool read_file, should be one of: todo_write")
            return iter([])

        obj._step_stream = step_stream_fail_then_succeed

        list(obj._inject_todo_action_step())

        assert call_count == 3
        # Memory should be clean (only original PlanningStep)
        assert len(memory.steps) == 1
        assert "updated successfully" in plan_step.observations

    def test_memory_isolation_after_success(self):
        """After success, len(memory.steps) == original length."""
        from smolagents.memory import PlanningStep

        plan_step = MagicMock(spec=PlanningStep)
        plan_step.observations = ""

        memory = MagicMock()
        memory.steps = [plan_step]

        obj = _make_mixin(
            tools={"todo_write": _make_todo_tool()},
            prompt_templates=_make_planning_templates(),
            memory=memory,
            step_number=5,
        )
        obj._step_stream = MagicMock(return_value=iter([]))
        obj._finalize_step = MagicMock()

        list(obj._inject_todo_action_step())

        assert len(memory.steps) == 1  # Only the original PlanningStep

    def test_step_number_restored_after_success(self):
        """After success, step_number == original value."""
        from smolagents.memory import PlanningStep

        plan_step = MagicMock(spec=PlanningStep)
        plan_step.observations = ""

        memory = MagicMock()
        memory.steps = [plan_step]

        obj = _make_mixin(
            tools={"todo_write": _make_todo_tool()},
            prompt_templates=_make_planning_templates(),
            memory=memory,
            step_number=5,
        )
        obj._step_stream = MagicMock(return_value=iter([]))
        obj._finalize_step = MagicMock()

        list(obj._inject_todo_action_step())

        assert obj.step_number == 5

    def test_result_message_appended_to_observations(self):
        """Success message appended to PlanningStep observations."""
        from smolagents.memory import PlanningStep

        plan_step = MagicMock(spec=PlanningStep)
        plan_step.observations = "Existing observations."

        memory = MagicMock()
        memory.steps = [plan_step]

        obj = _make_mixin(
            tools={"todo_write": _make_todo_tool()},
            prompt_templates=_make_planning_templates(),
            memory=memory,
        )
        obj._step_stream = MagicMock(return_value=iter([]))
        obj._finalize_step = MagicMock()

        list(obj._inject_todo_action_step(todo_state="initial"))

        assert "Existing observations." in plan_step.observations
        assert "updated successfully" in plan_step.observations

    def test_error_feedback_visible_to_next_attempt(self):
        """Failed step error is visible in memory for next attempt."""
        from smolagents.memory import PlanningStep

        plan_step = MagicMock(spec=PlanningStep)
        plan_step.observations = ""

        memory = MagicMock()
        memory.steps = [plan_step]

        obj = _make_mixin(
            tools={"todo_write": _make_todo_tool()},
            prompt_templates=_make_planning_templates(),
            memory=memory,
        )
        obj._finalize_step = MagicMock()

        memory_snapshots = []

        def capturing_step_stream(step):
            # Capture memory state during each attempt
            memory_snapshots.append(len(memory.steps))
            if len(memory_snapshots) == 1:
                raise RuntimeError("Unknown tool bash")
            return iter([])

        obj._step_stream = capturing_step_stream

        list(obj._inject_todo_action_step())

        # First attempt: memory has 1 step (PlanningStep)
        assert memory_snapshots[0] == 1
        # Second attempt: memory has 2 steps (PlanningStep + failed step with error)
        assert memory_snapshots[1] == 2


# ===========================================================================
# Failure path tests
# ===========================================================================

class TestTodoSyncFailure:
    """Test todo sync failure scenarios."""

    def test_all_attempts_fail(self):
        """MAX_TODO_RETRIES attempts all fail: observations contain 'failed'."""
        from smolagents.memory import PlanningStep

        plan_step = MagicMock(spec=PlanningStep)
        plan_step.observations = ""

        memory = MagicMock()
        memory.steps = [plan_step]

        obj = _make_mixin(
            tools={"todo_write": _make_todo_tool()},
            prompt_templates=_make_planning_templates(),
            memory=memory,
        )
        obj._finalize_step = MagicMock()

        call_count = 0

        def always_fail(step):
            nonlocal call_count
            call_count += 1
            raise RuntimeError("Unknown tool read_file")

        obj._step_stream = always_fail

        list(obj._inject_todo_action_step())

        assert call_count == obj.MAX_TODO_RETRIES
        assert "failed" in plan_step.observations

    def test_all_attempts_fail_error_logged(self):
        """Final failure uses _log.error()."""
        from smolagents.memory import PlanningStep

        plan_step = MagicMock(spec=PlanningStep)
        plan_step.observations = ""

        memory = MagicMock()
        memory.steps = [plan_step]

        obj = _make_mixin(
            tools={"todo_write": _make_todo_tool()},
            prompt_templates=_make_planning_templates(),
            memory=memory,
        )
        obj._finalize_step = MagicMock()

        def always_fail(step):
            raise RuntimeError("Unknown tool")

        obj._step_stream = always_fail

        with patch("src.lib.smolagents.agent.todo_sync.get_logger") as mock_get_logger:
            mock_log = MagicMock()
            mock_get_logger.return_value = mock_log
            list(obj._inject_todo_action_step())
            mock_log.error.assert_called()
            error_msg = mock_log.error.call_args[0][0]
            assert "failed" in error_msg.lower()

    def test_intermediate_failures_debug_logged(self):
        """Intermediate failures use _log.debug()."""
        from smolagents.memory import PlanningStep

        plan_step = MagicMock(spec=PlanningStep)
        plan_step.observations = ""

        memory = MagicMock()
        memory.steps = [plan_step]

        obj = _make_mixin(
            tools={"todo_write": _make_todo_tool()},
            prompt_templates=_make_planning_templates(),
            memory=memory,
        )
        obj._finalize_step = MagicMock()

        call_count = 0

        def fail_then_succeed(step):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise RuntimeError("Unknown tool")
            return iter([])

        obj._step_stream = fail_then_succeed

        with patch("src.lib.smolagents.agent.todo_sync.get_logger") as mock_get_logger:
            mock_log = MagicMock()
            mock_get_logger.return_value = mock_log
            list(obj._inject_todo_action_step())
            mock_log.debug.assert_called()

    def test_memory_isolation_after_failure(self):
        """After all attempts fail, memory.steps restored to original length."""
        from smolagents.memory import PlanningStep

        plan_step = MagicMock(spec=PlanningStep)
        plan_step.observations = ""

        memory = MagicMock()
        memory.steps = [plan_step]

        obj = _make_mixin(
            tools={"todo_write": _make_todo_tool()},
            prompt_templates=_make_planning_templates(),
            memory=memory,
        )
        obj._finalize_step = MagicMock()

        def always_fail(step):
            raise RuntimeError("Unknown tool")

        obj._step_stream = always_fail

        list(obj._inject_todo_action_step())

        assert len(memory.steps) == 1  # Only original PlanningStep

    def test_step_number_restored_after_failure(self):
        """After all attempts fail, step_number restored."""
        from smolagents.memory import PlanningStep

        plan_step = MagicMock(spec=PlanningStep)
        plan_step.observations = ""

        memory = MagicMock()
        memory.steps = [plan_step]

        obj = _make_mixin(
            tools={"todo_write": _make_todo_tool()},
            prompt_templates=_make_planning_templates(),
            memory=memory,
            step_number=10,
        )
        obj._finalize_step = MagicMock()

        def always_fail(step):
            raise RuntimeError("Unknown tool")

        obj._step_stream = always_fail

        list(obj._inject_todo_action_step())

        assert obj.step_number == 10

    def test_tools_restored_after_unexpected_exception(self):
        """Non-standard exception in loop still restores tools."""
        original_tools = {
            "todo_write": _make_todo_tool(),
            "bash": MagicMock(),
        }

        memory = MagicMock()
        memory.steps = []

        obj = _make_mixin(
            tools=original_tools.copy(),
            prompt_templates=_make_planning_templates(),
            memory=memory,
        )
        obj._finalize_step = MagicMock()

        def always_fail(step):
            raise RuntimeError("Unexpected LLM failure")

        obj._step_stream = always_fail

        list(obj._inject_todo_action_step())

        assert set(obj.tools.keys()) == {"todo_write", "bash"}


# ===========================================================================
# Boundary condition tests
# ===========================================================================

class TestTodoBoundaryConditions:
    """Test edge cases and boundary conditions."""

    def test_max_todo_retries_configurable(self):
        """MAX_TODO_RETRIES can be overridden on the class."""
        from smolagents.memory import PlanningStep

        plan_step = MagicMock(spec=PlanningStep)
        plan_step.observations = ""

        memory = MagicMock()
        memory.steps = [plan_step]

        obj = _make_mixin(
            tools={"todo_write": _make_todo_tool()},
            prompt_templates=_make_planning_templates(),
            memory=memory,
        )
        obj.MAX_TODO_RETRIES = 2
        obj._finalize_step = MagicMock()

        call_count = 0

        def always_fail(step):
            nonlocal call_count
            call_count += 1
            raise RuntimeError("fail")

        obj._step_stream = always_fail

        list(obj._inject_todo_action_step())

        assert call_count == 2  # Not 4

    def test_validate_injects_on_missing_yaml_keys(self):
        """Missing YAML keys get injected with defaults."""
        obj = _make_mixin(
            tools={"todo_write": _make_todo_tool()},
            prompt_templates={"planning": {"todo_initial": "init"}},
        )
        obj._validate_todo_prompts()
        assert obj.prompt_templates["planning"]["todo_update"]
        assert obj.prompt_templates["planning"]["todo_final"]
        assert obj.prompt_templates["planning"]["todo_initial"] == "init"

    def test_validate_injects_on_empty_string_keys(self):
        """Empty string YAML keys get injected with defaults."""
        obj = _make_mixin(
            tools={"todo_write": _make_todo_tool()},
            prompt_templates={
                "planning": {
                    "todo_initial": "init",
                    "todo_update": "",
                    "todo_final": "final",
                }
            },
        )
        obj._validate_todo_prompts()
        assert obj.prompt_templates["planning"]["todo_update"]
        assert obj.prompt_templates["planning"]["todo_initial"] == "init"
        assert obj.prompt_templates["planning"]["todo_final"] == "final"

    def test_empty_memory_steps_no_crash(self):
        """_append_todo_result doesn't crash when no PlanningStep in memory."""
        obj = _make_mixin()
        memory = MagicMock()
        memory.steps = []
        obj.memory = memory
        # Should not raise
        obj._append_todo_result(True, "update")


# ===========================================================================
# _read_todo_state_for_planning: read-only label test
# ===========================================================================

class TestReadTodoStateLabel:
    """Test that _read_todo_state_for_planning uses read-only reference label."""

    def test_read_only_label_in_output(self, tmp_path, monkeypatch):
        """Output should contain 'read-only reference' label."""
        from src.lib.runtime import RuntimeHome, bind_run_context

        obj = _make_mixin()

        # Create a fake todos.md
        runtime_context = RuntimeHome(tmp_path / ".agentloom").context(
            application_id="app", task_id="task", run_id="run"
        )
        agent_dir = runtime_context.agent_task_workspace_dir("test_agent")
        agent_dir.mkdir(parents=True)
        (agent_dir / "todos.md").write_text(
            "# Task Progress\n- [ ] Task 1\n- [x] Task 2\n"
        )

        with bind_run_context(runtime_context):
            with patch(
                "src.lib.smolagents.agent.todo_sync.get_current_agent_name",
                return_value="test_agent",
            ):
                with patch(
                    "src.trace.task_context.get_current_runtime_agent_path",
                    return_value="test_agent",
                ):
                    result = obj._read_todo_state_for_planning()

        assert "read-only reference" in result
        assert "Do not attempt tool calls" in result
        assert "Task 1" in result


# ===========================================================================
# Strict type coercion tests
# ===========================================================================

class TestBidirectionalCoercion:
    """Test schema-bound one-directional coercion."""

    def _make_tool_with_input(self, key, expected_type):
        """Create a mock tool with a specific input type."""
        tool = MagicMock()
        tool.inputs = {key: {"type": expected_type}}
        return tool

    def test_list_to_string_is_not_coerced(self):
        """Array value is not coerced when expected type is string."""
        from src.lib.smolagents.agent.tool_argument_coercion import coerce_tool_arguments

        tool = self._make_tool_with_input("todos", "string")
        args = {"todos": [{"content": "Task A", "status": "pending"}]}
        coerce_tool_arguments(tool, args)
        assert args["todos"] == [{"content": "Task A", "status": "pending"}]

    def test_dict_to_string_is_not_coerced(self):
        """Dict value is not coerced when expected type is string."""
        from src.lib.smolagents.agent.tool_argument_coercion import coerce_tool_arguments

        tool = self._make_tool_with_input("config", "string")
        args = {"config": {"key": "value"}}
        coerce_tool_arguments(tool, args)
        assert args["config"] == {"key": "value"}

    def test_string_to_array_coercion(self):
        """Existing: string value parsed to list when expected type is array."""
        from src.lib.smolagents.agent.tool_argument_coercion import coerce_tool_arguments

        tool = self._make_tool_with_input("items", "array")
        args = {"items": '[1, 2, 3]'}
        coerce_tool_arguments(tool, args)
        assert args["items"] == [1, 2, 3]

    def test_string_to_object_coercion(self):
        """Existing: string value parsed to dict when expected type is object."""
        from src.lib.smolagents.agent.tool_argument_coercion import coerce_tool_arguments

        tool = self._make_tool_with_input("data", "object")
        args = {"data": '{"a": 1}'}
        coerce_tool_arguments(tool, args)
        assert args["data"] == {"a": 1}

    def test_double_serialized_json_coercion(self):
        """Double-serialized JSON string correctly parsed to list."""
        from src.lib.smolagents.agent.tool_argument_coercion import coerce_tool_arguments

        tool = self._make_tool_with_input("sections", "array")
        # LLM wraps JSON in an extra string layer
        inner_json = json.dumps([{"heading": "Intro", "body": "Hello"}])
        double_serialized = json.dumps(inner_json)
        args = {"sections": double_serialized}
        coerce_tool_arguments(tool, args)
        assert isinstance(args["sections"], list)
        assert args["sections"][0]["heading"] == "Intro"

    def test_no_double_coercion(self):
        """Already correct type should not be coerced."""
        from src.lib.smolagents.agent.tool_argument_coercion import coerce_tool_arguments

        tool = self._make_tool_with_input("name", "string")
        args = {"name": "already a string"}
        coerce_tool_arguments(tool, args)
        assert args["name"] == "already a string"

    def test_array_stays_as_array(self):
        """Array value stays when expected type is array."""
        from src.lib.smolagents.agent.tool_argument_coercion import coerce_tool_arguments

        tool = self._make_tool_with_input("items", "array")
        original = [1, 2, 3]
        args = {"items": original}
        coerce_tool_arguments(tool, args)
        assert args["items"] is original

    def test_unknown_key_ignored(self):
        """Keys not in tool.inputs are skipped."""
        from src.lib.smolagents.agent.tool_argument_coercion import coerce_tool_arguments

        tool = self._make_tool_with_input("known", "string")
        args = {"known": "ok", "unknown": [1, 2]}
        coerce_tool_arguments(tool, args)
        assert args["unknown"] == [1, 2]  # untouched


# ===========================================================================
# YAML prompt template loading tests
# ===========================================================================

class TestYamlTodoPrompts:
    """Test that todo prompt keys are present in all YAML template files."""

    @pytest.fixture(params=[
        "src/lib/smolagents/prompts/structured_code_agent.example.yaml",
        "src/lib/smolagents/prompts/toolcalling_agent.example.yaml",
        "src/lib/smolagents/prompts/openai/toolcalling_agent.example.yaml",
        "src/lib/smolagents/prompts/anthropic/toolcalling_agent.example.yaml",
        "src/lib/smolagents/prompts/gemini/toolcalling_agent.example.yaml",
    ])
    def yaml_path(self, request):
        """Return path to each YAML template file."""
        import yaml
        root = Path(__file__).parent.parent
        path = root / request.param
        if not path.exists():
            pytest.skip(f"YAML file not found: {path}")
        return path

    def test_todo_keys_exist(self, yaml_path):
        """All 3 todo keys should exist in planning section."""
        import yaml
        data = yaml.safe_load(yaml_path.read_text())
        planning = data.get("planning", {})
        assert "todo_initial" in planning, f"Missing todo_initial in {yaml_path.name}"
        assert "todo_update" in planning, f"Missing todo_update in {yaml_path.name}"
        assert "todo_final" in planning, f"Missing todo_final in {yaml_path.name}"

    def test_todo_prompts_non_empty(self, yaml_path):
        """Todo prompts should be non-empty strings."""
        import yaml
        data = yaml.safe_load(yaml_path.read_text())
        planning = data.get("planning", {})
        for key in ("todo_initial", "todo_update", "todo_final"):
            assert planning[key].strip(), f"Empty {key} in {yaml_path.name}"

    def test_todo_prompts_contain_todo_write(self, yaml_path):
        """Todo prompts should mention todo_write tool."""
        import yaml
        data = yaml.safe_load(yaml_path.read_text())
        planning = data.get("planning", {})
        for key in ("todo_initial", "todo_update", "todo_final"):
            assert "todo_write" in planning[key], f"{key} missing todo_write in {yaml_path.name}"

    def test_initial_plan_no_direct_todo_call(self, yaml_path):
        """initial_plan should NOT tell LLM to directly call todo_write."""
        import yaml
        data = yaml.safe_load(yaml_path.read_text())
        initial = data["planning"]["initial_plan"]
        assert "FIRST action" not in initial
        assert "must be calling todo_write" not in initial

    def test_update_plan_no_direct_todo_call(self, yaml_path):
        """update_plan_post_messages should NOT tell LLM to call todo_write."""
        import yaml
        data = yaml.safe_load(yaml_path.read_text())
        update = data["planning"]["update_plan_post_messages"]
        assert "update it via todo_write" not in update

    def test_system_prompt_has_task_tracking(self, yaml_path):
        """system_prompt (or system_prompt_extra) should contain Task Tracking section."""
        import yaml
        data = yaml.safe_load(yaml_path.read_text())
        system = data.get("system_prompt") or data.get("system_prompt_extra", "")
        assert "Task Tracking" in system
        assert "todo_write" in system
