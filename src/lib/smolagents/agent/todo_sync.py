"""Todo sync mixin for LoomAgent.

Provides isolated todo_write injection with retry logic,
YAML prompt validation, and PlanningStep result annotation.
"""

from pathlib import Path
from typing import Optional

from src.lib.logging import get_logger
from src.trace import get_current_hook_manager, get_current_agent_name
from src.lib.config import C


class TodoSyncMixin:
    """Mixin that adds todo_write sync capabilities to an agent.

    Requires the host class to provide:
    - self.tools: dict of available tools
    - self.managed_agents: dict of managed agents
    - self.step_number: int
    - self.memory.steps: list
    - self.prompt_templates: dict
    - self.planning_interval: int | None
    - self._step_stream(step): generator
    - self._finalize_step(step): method
    - self._hook_manager: HookManager | None
    """

    # Maximum number of retries for todo sync LLM calls.
    MAX_TODO_RETRIES = 4

    # ------------------------------------------------------------------
    # Todo prompt validation
    # ------------------------------------------------------------------

    def _validate_todo_prompts(self):
        """Validate that todo prompt keys exist in YAML config.

        Raises ValueError if any required key is missing when
        todo_write is configured with planning_interval.
        """
        if "todo_write" not in getattr(self, "tools", {}):
            return
        if getattr(self, "planning_interval", None) is None:
            return

        planning = self.prompt_templates.get("planning", {})
        required_keys = ["todo_initial", "todo_update", "todo_final"]
        missing = [k for k in required_keys if not planning.get(k)]
        if missing:
            raise ValueError(
                f"Todo prompt keys missing in YAML planning config: {missing}. "
                "Add them to the agent's prompt YAML under 'planning:' section."
            )

    # ------------------------------------------------------------------
    # Todo state helpers
    # ------------------------------------------------------------------

    def _reset_todo_file(self) -> None:
        """Reset todos.md to empty state at the start of a new run.

        Clears old todo content so each run starts fresh.
        Only called when todo_write is available.
        """
        try:
            from src.trace.task_context import get_current_runtime_agent_path
            agent_path = (
                get_current_runtime_agent_path()
                or get_current_agent_name()
                or getattr(self, "name", None)
                or "default"
            )
            root = Path(C.agent_root).resolve()
            runtime_dir = root / ".runtime" / agent_path
            runtime_dir.mkdir(parents=True, exist_ok=True)
            todos_file = runtime_dir / "todos.md"
            todos_file.write_text("# Task Progress\n", encoding="utf-8")
            _log = get_logger(__name__)
            _log.debug("Reset todo file: %s", todos_file)
        except Exception as exc:
            _log = get_logger(__name__)
            _log.warning("Failed to reset todo file: %s", exc)

    def _has_incomplete_todos(self) -> bool:
        """Check if there are any pending or in_progress todos on disk."""
        try:
            from src.trace.task_context import get_current_runtime_agent_path
            agent_path = get_current_runtime_agent_path() or get_current_agent_name() or getattr(self, "name", None) or "default"
            root = Path(C.agent_root).resolve()
            todos_file = root / ".runtime" / agent_path / "todos.md"
            if not todos_file.exists():
                return False
            content = todos_file.read_text(encoding="utf-8")
            # Unchecked checkboxes indicate pending or in_progress items
            for line in content.splitlines():
                stripped = line.strip()
                if stripped.startswith("- [ ]"):
                    return True
            return False
        except Exception:
            return False

    def _read_todo_state_for_planning(self) -> str:
        """Read .runtime/<agent>/todos.md for planning context injection.

        Uses runtime_agent_path (hierarchical) for path resolution,
        consistent with todo_write tool's persistence path.
        Uses C.agent_root for project root discovery (shared config mechanism).
        """
        try:
            from src.trace.task_context import get_current_runtime_agent_path
            agent_path = get_current_runtime_agent_path() or get_current_agent_name() or getattr(self, 'name', None) or "default"
            root = Path(C.agent_root).resolve()

            todos_file = root / ".runtime" / agent_path / "todos.md"
            if not todos_file.exists():
                return ""
            content = todos_file.read_text(encoding="utf-8").strip()
            if not content or content == "# Task Progress":
                return ""

            # Escape Jinja2 syntax to prevent rendering errors
            safe_content = (
                content
                .replace("{{", "{ {")
                .replace("}}", "} }")
                .replace("{%", "{ %")
                .replace("%}", "% }")
            )
            return (
                "## Current Task Progress (read-only reference)\n"
                "Use this to assess progress. Do not attempt tool calls in this planning phase.\n\n"
                + safe_content
            )
        except Exception:
            return ""

    # ------------------------------------------------------------------
    # Todo sync ActionStep injection
    # ------------------------------------------------------------------

    def _inject_todo_action_step(self, todo_state="update", max_steps=None):
        """Inject an isolated ActionStep with only todo_write tool exposed.

        Runs in a completely isolated environment:
        - Only todo_write tool is available
        - Intermediate failed steps are added to memory for LLM error feedback,
          then atomically cleared after the retry loop completes
        - Retries up to MAX_TODO_RETRIES times with memory-based error feedback
        - No programmatic fallback — if all attempts fail, logs ERROR
        - Only a short result message is appended to the final PlanningStep

        Args:
            todo_state: One of 'initial', 'update', 'final'.
            max_steps: Total step budget; skip injection if near limit.

        Yields ActionStep elements from the injected step. Restores
        original tools, managed_agents, step_number, and memory in a finally block.
        """
        import time as _time
        from smolagents.memory import ActionStep, Timing

        # Guard: todo_write must be available
        if "todo_write" not in getattr(self, "tools", {}):
            return

        # Guard: preserve step budget (need at least 2 steps remaining)
        if max_steps is not None and getattr(self, "step_number", 0) >= max_steps - 1:
            _log = get_logger(__name__)
            _log.debug("Skipping todo injection: step budget exhausted")
            return

        # Select prompt from YAML config (fail-fast validated at init)
        prompt_key = f"todo_{todo_state}"
        prompt = self.prompt_templates.get("planning", {}).get(prompt_key, "")
        if not prompt:
            return

        _log = get_logger(__name__)
        _log.info("Injecting todo sync ActionStep (state=%s)", todo_state)

        # Save original state for isolation
        original_tools = self.tools.copy()
        original_managed = self.managed_agents.copy()
        original_step_number = self.step_number
        memory_start_len = len(self.memory.steps)

        # Restrict to only todo_write
        self.tools = {"todo_write": original_tools["todo_write"]}
        self.managed_agents = {}

        try:
            success = False
            for attempt in range(1, self.MAX_TODO_RETRIES + 1):
                hook_manager = getattr(self, "_hook_manager", None) or get_current_hook_manager()

                # Use the todo prompt as the system prompt override
                # so the LLM only sees todo_write instructions (not the
                # full system prompt with 20+ tool descriptions).
                self._todo_sys_prompt_override = prompt

                try:
                    step = ActionStep(
                        step_number=self.step_number,
                        timing=Timing(start_time=_time.time()),
                    )
                    for output in self._step_stream(step):
                        yield output
                    if hasattr(self, "_finalize_step"):
                        self._finalize_step(step)
                    # Append to memory so next attempt can see context
                    self.memory.steps.append(step)
                    yield step
                    self.step_number += 1
                    success = True
                    _log.info("Todo sync succeeded on attempt %d", attempt)
                    break
                except Exception as exc:
                    _log.debug(
                        "Todo sync attempt %d/%d failed: %s",
                        attempt, self.MAX_TODO_RETRIES, exc,
                    )
                    # Append failed step to memory for error feedback
                    failed_step = ActionStep(
                        step_number=self.step_number,
                        timing=Timing(start_time=_time.time()),
                        error=exc,
                    )
                    self.memory.steps.append(failed_step)
                    self.step_number += 1

            if not success:
                _log.error(
                    "Todo sync failed after %d attempts",
                    self.MAX_TODO_RETRIES,
                )

            # Append short result to the last PlanningStep's observations
            self._append_todo_result(success, todo_state)
        finally:
            # Clear system prompt override
            self._todo_sys_prompt_override = None
            # Atomically clear all intermediate steps from memory
            del self.memory.steps[memory_start_len:]
            # Restore original state
            self.step_number = original_step_number
            self.tools = original_tools
            self.managed_agents = original_managed

    def _append_todo_result(self, success, todo_state):
        """Append a short English result message to the last PlanningStep."""
        from smolagents.memory import PlanningStep

        msg = (
            f"Todo list ({todo_state}) updated successfully."
            if success
            else f"Todo list ({todo_state}) update failed."
        )
        for step in reversed(self.memory.steps):
            if isinstance(step, PlanningStep):
                obs = getattr(step, "observations", None) or ""
                step.observations = f"{obs}\n{msg}".strip() if obs else msg
                break
