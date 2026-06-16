"""LoomAgentMixin — enhancement layer for smolagents Agents.

Provides smart memory compression, progressive error recovery,
before-run callbacks, and todo sync injection via TodoSyncMixin.
"""

from typing import Optional

from smolagents import LogLevel
from smolagents.models import ChatMessage, MessageRole

from src.lib.smolagents.memory.context_compression import ConversationHistoryManager
from src.lib.logging import get_logger
from src.trace import get_current_hook_manager
from src.lib.smolagents.hooks.hook_manager import wrap_in_system_reminder
from src.lib.smolagents.agent.todo_sync import TodoSyncMixin


class LoomAgentMixin(TodoSyncMixin):
    """Enhancement layer that turns a base smolagents Agent into a Loom Agent.

    Provides:
    - before_run callbacks
    - smart memory compression (ConversationHistoryManager)
    - progressive error recovery guidance
    - todo sync injection into the planning loop (via TodoSyncMixin)
    """

    def _init_loom_agent(
        self,
        before_run_callbacks: Optional[list],
        max_tokens: Optional[int] = None,
        smart_summary: bool = True,
    ):
        self._before_run_callbacks = before_run_callbacks or []
        self._history_manager = ConversationHistoryManager(
            max_tokens=max_tokens,
            smart_summary=smart_summary,
        )
        self._agent_loom_supports_reset_false_task_step_control = True

    def run(self, task: str, *args, **kwargs):
        skip_task_step_on_reset_false = kwargs.pop("_skip_task_step_on_reset_false", True)
        for callback in self._before_run_callbacks:
            task = callback(self, task, *args, **kwargs)

        if getattr(self, "python_executor", None) is not None and hasattr(self, "state"):
            self.state["task"] = task

        # Determine if we are resetting memory
        reset = kwargs.get("reset", True)
        if len(args) >= 2:
            reset = args[1] # stream is args[0], reset is args[1]

        if (
            skip_task_step_on_reset_false
            and not reset
            and hasattr(self, "memory")
            and hasattr(self.memory, "steps")
        ):
            original_steps = self.memory.steps

            class _InterceptTaskStepList(list):
                def __init__(self, original_list):
                    super().__init__(original_list)
                    self._original = original_list
                    self._skipped = False

                def append(self, item):
                    if not self._skipped and type(item).__name__ == "TaskStep":
                        self._skipped = True
                        return
                    super().append(item)
                    self._original.append(item)

            self.memory.steps = _InterceptTaskStepList(original_steps)
            try:
                return super().run(task, *args, **kwargs)
            finally:
                self.memory.steps = original_steps

        return super().run(task, *args, **kwargs)

    def write_memory_to_messages(self, summary_mode: bool = False):
        """
        Write memory into message list with smart compression and state persistence.
        """
        messages = super().write_memory_to_messages(summary_mode=summary_mode)

        if summary_mode:
            return messages

        # ── Todo injection: replace system prompt with todo-only prompt ──
        # When _todo_sys_prompt_override is set (by TodoSyncMixin during
        # _inject_todo_action_step), replace the first SYSTEM message so the
        # LLM only sees todo_write instructions instead of the full system
        # prompt with all tool descriptions.  The original
        # self.memory.system_prompt is never modified.
        todo_override = getattr(self, "_todo_sys_prompt_override", None)
        if todo_override:
            for i, msg in enumerate(messages):
                if msg.role == MessageRole.SYSTEM:
                    messages[i] = ChatMessage(
                        role=MessageRole.SYSTEM,
                        content=[{"type": "text", "text": todo_override}],
                    )
                    break

        hook_manager = getattr(self, "_hook_manager", None) or get_current_hook_manager()
        # Sync step_number to hook_manager so hook scripts can read it
        # via $STEP_NUMBER env var.  Per-agent instance — no lock needed.
        if hook_manager is not None:
            hook_manager.step_number = getattr(self, "step_number", 0) or 0
        if hook_manager is not None:
            pending_user_messages = hook_manager.consume_pending_user_messages()
            if pending_user_messages:
                agent_logger = getattr(self, "logger", None)
                for message in pending_user_messages:
                    rendered = f"[hook] {message}"
                    if agent_logger is not None and hasattr(agent_logger, "log"):
                        agent_logger.log(rendered, level=LogLevel.INFO)

        self._history_manager.sync_from_messages(messages)

        model_id = getattr(self.model, "model_id", None)
        compressed_messages = self._history_manager.get_compressed_messages(
            model_id=model_id,
            step=getattr(self, 'step_number', None),
        )

        if hook_manager is not None:
            pending_context = hook_manager.consume_pending_agent_context()
            if pending_context:
                combined_context = "\n\n".join(item for item in pending_context if item)
                if combined_context:
                    compressed_messages = list(compressed_messages)
                    compressed_messages.append(
                        ChatMessage(
                            role=MessageRole.SYSTEM,
                            content=[
                                {
                                    "type": "text",
                                    "text": wrap_in_system_reminder(combined_context),
                                }
                            ],
                        )
                    )

        # Progressive error recovery: consolidate consecutive error messages and
        # replace generic "Now let's retry…" suffixes with targeted guidance.
        compressed_messages = self._consolidate_error_messages(compressed_messages)

        # ── Todo injection: strip conversation history for todo-only prompt ──
        # When _todo_sys_prompt_override is set (by TodoSyncMixin during
        # _inject_todo_action_step), replace the SYSTEM message AND strip ALL
        # conversation history so the LLM only sees the todo-only system prompt.
        # This prevents the LLM from being influenced by previous tool calls
        # (shell_tool, read_file, glob_search, etc.) in the conversation history
        # or the planning step text. The LLM needs ONLY the todo instructions.
        # Applied on compressed_messages (the final output) because the history
        # manager's sync_from_messages only appends and never re-syncs index 0.
        # The original self.memory.system_prompt is never modified.
        todo_override = getattr(self, "_todo_sys_prompt_override", None)
        if todo_override:
            # Build a context-rich user message so the LLM knows what
            # tasks exist and what the current plan is, without exposing
            # the full conversation history with tool call patterns.
            user_parts = []

            # 1. Include the last planning step text (plan review).
            #    This gives context about what was accomplished and what
            #    the current plan is.  PlanningStep messages are text-only
            #    and don't contain tool call patterns.
            from smolagents.memory import PlanningStep
            for step in reversed(getattr(getattr(self, "memory", None), "steps", [])):
                if isinstance(step, PlanningStep) and getattr(step, "model_output", None):
                    user_parts.append("## Current Plan\n" + step.model_output.strip())
                    break

            # 2. Include current todo state from the todos.md file.
            todo_state = self._read_todo_state_for_planning()
            if todo_state:
                user_parts.append(todo_state)

            user_parts.append("Please call todo_write now to update the task list.")

            compressed_messages = [
                ChatMessage(
                    role=MessageRole.SYSTEM,
                    content=[{"type": "text", "text": todo_override}],
                ),
                ChatMessage(
                    role=MessageRole.USER,
                    content=[{"type": "text", "text": "\n\n".join(user_parts)}],
                ),
            ]

        return compressed_messages

    def _consolidate_error_messages(self, messages: list) -> list:
        """Replace generic error suffixes with progressive recovery guidance.

        Reuses the memory.steps traversal pattern (from the former circuit-breaker)
        to count consecutive AgentParsingError steps, extract error metadata from
        self.tools / step.model_output / step.error, and delegate to
        ``error_recovery.consolidate_error_messages()`` +
        ``error_recovery.build_recovery_message()`` for the actual replacement.
        """
        from smolagents.agents import AgentParsingError
        from smolagents.memory import ActionStep

        try:
            steps = getattr(getattr(self, "memory", None), "steps", None)
            if not steps:
                return messages

            # Count consecutive errors from the end (same pattern as old breaker)
            consecutive = 0
            latest_error_step: ActionStep | None = None
            for step in reversed(steps):
                if not isinstance(step, ActionStep):
                    continue
                if step.error is not None and isinstance(step.error, AgentParsingError):
                    consecutive += 1
                    if latest_error_step is None:
                        latest_error_step = step
                else:
                    break

            if consecutive == 0:
                return messages

            # Gather data from smolagents runtime
            from src.lib.smolagents.error_recovery import (
                build_recovery_message,
                consolidate_error_messages,
                extract_category_from_error,
                extract_tool_info,
                classify_parse_error,
            )

            # Extract error category from [CATEGORY:...] tag in error message
            error_str = str(latest_error_step.error) if latest_error_step and latest_error_step.error else ""
            error_category = extract_category_from_error(error_str)

            # Extract tool info from error message (failures) and raw LLM output
            raw_text = getattr(latest_error_step, "model_output", None) if latest_error_step else None

            # Gather available tool names and descriptions
            tools = getattr(self, "tools", {})
            tool_names = list(tools.keys()) if tools else []
            tool_descriptions = {}
            for name, tool in tools.items():
                desc = getattr(tool, "description", "")
                if desc:
                    # Truncate long descriptions to first sentence
                    first_line = desc.split("\n")[0][:80]
                    tool_descriptions[name] = first_line

            # Extract partial tool name for category-aware L1 guidance
            partial_tool_name = extract_tool_info(
                raw_text=raw_text,
                available_tool_names=tool_names if tool_names else None,
            )

            # Build recovery message
            last_snippet = raw_text[:300] if raw_text else None
            recovery_msg = build_recovery_message(
                consecutive_errors=consecutive,
                error_category=error_category,
                last_output_snippet=last_snippet,
                available_tool_names=tool_names,
                tool_descriptions=tool_descriptions,
                partial_tool_name=partial_tool_name,
            )

            # Consolidate error messages in the message list
            consolidated = consolidate_error_messages(
                messages=messages,
                consecutive_error_count=consecutive,
                recovery_message=recovery_msg,
                max_full_errors=1,
            )

            if consolidated is not messages:
                _log = get_logger(__name__)
                _log.info(
                    "Injected Level %s recovery guidance (%d consecutive parse failures)",
                    min(consecutive, 4),
                    consecutive,
                )
                _log.debug("Recovery message sent to LLM: %s", recovery_msg)

            return consolidated
        except Exception as exc:
            _log = get_logger(__name__)
            _log.warning("Error recovery consolidation failed (safe fallback): %s", exc)
            return messages

    # ------------------------------------------------------------------
    # Final planning step: review todo completeness after final_answer
    # ------------------------------------------------------------------

    def _run_stream(self, task, max_steps, images=None):
        """Override: inject todo sync ActionStep after each PlanningStep.

        When planning_interval is configured and todo_write is available:
        - After initial PlanningStep: inject with todo_state='initial'
        - After update PlanningStep: inject with todo_state='update'
        - After final planning step: inject with todo_state='final'

        Also preserves the existing final planning step logic for
        reviewing todo completeness after final_answer.
        """
        import time as _time
        from smolagents.memory import PlanningStep

        # Guard: ensure final planning runs at most once per run
        self._final_planning_done = False

        # Validate todo prompts at the start of a run
        self._validate_todo_prompts()

        # Reset todo file at the start of a new run (fresh start)
        if "todo_write" in getattr(self, "tools", {}):
            self._reset_todo_file()

        _planning_step_count = 0
        final_answer_step = None
        for element in super()._run_stream(task, max_steps, images=images):
            # Capture FinalAnswerStep but don't yield it yet
            if type(element).__name__ == "FinalAnswerStep":
                final_answer_step = element
                continue
            yield element

            # After every PlanningStep, inject a todo sync ActionStep
            if isinstance(element, PlanningStep) and "todo_write" in getattr(self, "tools", {}):
                todo_state = "initial" if _planning_step_count == 0 else "update"
                _planning_step_count += 1
                yield from self._inject_todo_action_step(
                    todo_state=todo_state, max_steps=max_steps
                )

        # Run ONE final planning step if:
        # 1. planning_interval is configured
        # 2. There are incomplete todos on disk
        # 3. Guard flag has not been set (at-most-once)
        if (
            not self._final_planning_done
            and getattr(self, "planning_interval", None) is not None
            and self._has_incomplete_todos()
        ):
            self._final_planning_done = True  # Set BEFORE execution to prevent re-entry
            _log = get_logger(__name__)
            _log.info("Final planning step: reviewing todo completeness")
            try:
                planning_start = _time.time()
                planning_step = None
                for elem in self._generate_planning_step(
                    task, is_first_step=False, step=getattr(self, "step_number", 0)
                ):
                    yield elem
                    planning_step = elem
                if isinstance(planning_step, PlanningStep):
                    from smolagents.memory import Timing
                    planning_step.timing = Timing(
                        start_time=planning_start,
                        end_time=_time.time(),
                    )
                    if hasattr(self, "_finalize_step"):
                        self._finalize_step(planning_step)
                    self.memory.steps.append(planning_step)
                # Inject final todo sync after the final planning step
                yield from self._inject_todo_action_step(
                    todo_state="final", max_steps=max_steps
                )
            except Exception as exc:
                _log = get_logger(__name__)
                _log.warning("Final planning step failed (non-fatal): %s", exc)

        # Now yield the final answer step
        if final_answer_step is not None:
            yield final_answer_step

    # ------------------------------------------------------------------
    # Planning step override: inject todo state into planning context
    # ------------------------------------------------------------------

    def _generate_planning_step(self, task, is_first_step: bool, step: int):
        """Override: inject current todo state into planning context.

        Reads .runtime/<agent>/todos.md and appends its content to
        update_plan_pre_messages BEFORE Jinja2 rendering.  The LLM sees
        the current task list in the SYSTEM message during planning.
        """
        if not is_first_step:
            todo_state = self._read_todo_state_for_planning()
            if todo_state:
                original_pre = self.prompt_templates["planning"]["update_plan_pre_messages"]
                self.prompt_templates["planning"]["update_plan_pre_messages"] = (
                    original_pre + "\n\n" + todo_state
                )
                try:
                    yield from super()._generate_planning_step(task, is_first_step, step)
                finally:
                    self.prompt_templates["planning"]["update_plan_pre_messages"] = original_pre
                return
        # Fallback: first step or no todo state — delegate to parent unchanged
        yield from super()._generate_planning_step(task, is_first_step, step)
