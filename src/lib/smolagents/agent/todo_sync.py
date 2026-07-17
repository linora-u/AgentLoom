"""Todo sync mixin for LoomAgent.

Provides isolated todo_write injection with retry logic,
YAML prompt validation, and PlanningStep result annotation.
"""

from typing import Optional

from src.lib.logging import get_logger
from src.trace import get_current_hook_manager, get_current_agent_name
from src.lib.runtime import get_current_run_context


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

    # Default todo prompt sections injected when missing from planning config
    _DEFAULT_TODO_INITIAL = (
        "<tool_restriction>\n"
        "⚠️ CRITICAL: You can ONLY call `todo_write`. ALL other tools are DISABLED.\n"
        "Do NOT call read_file, shell_tool, write_markdown_file, or ANY other tool.\n"
        "The ONLY action you may take is: call `todo_write`.\n"
        "</tool_restriction>\n\n"
        "Register your planned tasks using todo_write. Based on your plan above:\n"
        "- Set the first task as \"in_progress\"\n"
        "- Set remaining tasks as \"pending\"\n"
        "- Use clear, imperative task descriptions\n\n"
        "You MUST register your tasks every time. Do not skip this step.\n"
        "Provide the COMPLETE task list — todo_write replaces the entire list."
    )

    _DEFAULT_TODO_UPDATE = (
        "<tool_restriction>\n"
        "⚠️ CRITICAL: You can ONLY call `todo_write`. ALL other tools are DISABLED.\n"
        "Do NOT call read_file, shell_tool, write_markdown_file, or ANY other tool.\n"
        "The ONLY action you may take is: call `todo_write`.\n"
        "</tool_restriction>\n\n"
        "Update your task list to reflect current progress. Based on your plan review:\n"
        "- Mark completed tasks as \"completed\"\n"
        "- Set your current/next task as \"in_progress\"\n"
        "- Add any newly discovered tasks as \"pending\"\n"
        "- Remove tasks that are no longer relevant\n\n"
        "Provide the COMPLETE updated list — todo_write replaces the entire list, not append."
    )

    _DEFAULT_TODO_FINAL = (
        "<tool_restriction>\n"
        "⚠️ CRITICAL: You can ONLY call `todo_write`. ALL other tools are DISABLED.\n"
        "Do NOT call read_file, shell_tool, write_markdown_file, or ANY other tool.\n"
        "The ONLY action you may take is: call `todo_write`.\n"
        "</tool_restriction>\n\n"
        "Finalize your task list. You are about to deliver the final answer.\n"
        "- Mark all completed tasks as \"completed\"\n"
        "- If any tasks were skipped, mark them as \"completed\" with a note\n"
        "- Ensure the task list accurately reflects what was accomplished\n"
        "- Do NOT pass an empty list. Always include all tasks.\n\n"
        "Provide the COMPLETE finalized list via todo_write."
    )

    def _validate_todo_prompts(self):
        """Ensure todo prompt keys exist in planning config.

        When todo_write is active with planning_interval, injects default
        todo prompt sections if they are missing from the loaded templates.
        """
        if "todo_write" not in getattr(self, "tools", {}):
            return
        if getattr(self, "planning_interval", None) is None:
            return

        planning = self.prompt_templates.get("planning")
        if planning is None:
            return
        if not planning.get("todo_initial"):
            planning["todo_initial"] = self._DEFAULT_TODO_INITIAL
        if not planning.get("todo_update"):
            planning["todo_update"] = self._DEFAULT_TODO_UPDATE
        if not planning.get("todo_final"):
            planning["todo_final"] = self._DEFAULT_TODO_FINAL

    # ------------------------------------------------------------------
    # Todo state helpers
    # ------------------------------------------------------------------

    def _todo_path(self):
        """Resolve this agent's task-scoped todo file from RuntimeContext."""

        from src.trace.task_context import get_current_runtime_agent_path

        agent_path = (
            get_current_runtime_agent_path()
            or get_current_agent_name()
            or getattr(self, "name", None)
            or "default"
        )
        runtime_context = get_current_run_context(required=True)
        runtime_context.prepare_agent_workspace(agent_path)
        return runtime_context.agent_todos_path(agent_path)

    def _reset_todo_file(self) -> None:
        """Create todos.md for the current task if it does not exist.

        The task id, rather than the run id, defines todo isolation.  A resume
        therefore keeps the same todo state while a new task starts clean.
        """
        try:
            todos_file = self._todo_path()
            if not todos_file.exists():
                todos_file.write_text("# Task Progress\n", encoding="utf-8")
            _log = get_logger(__name__)
            _log.debug("Reset todo file: %s", todos_file)
        except Exception as exc:
            _log = get_logger(__name__)
            _log.warning("Failed to reset todo file: %s", exc)

    def _has_incomplete_todos(self) -> bool:
        """Check if there are any pending or in_progress todos on disk."""
        try:
            todos_file = self._todo_path()
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
        """Read the current task's todos.md for planning context injection.

        Uses runtime_agent_path (hierarchical) for path resolution,
        consistent with todo_write tool's persistence path.
        """
        try:
            todos_file = self._todo_path()
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
