"""LoomAgentMixin — enhancement layer for smolagents Agents.

Provides smart memory compression, progressive error recovery,
before-run callbacks, and canonical Todo state hydration.
"""

import json

from smolagents import LogLevel
from smolagents.models import ChatMessage, MessageRole
from src.lib.logging import get_logger
from src.lib.smolagents.hooks import wrap_in_system_reminder
from src.lib.smolagents.memory.context_compression import ConversationHistoryManager
from src.lib.smolagents.tool_protocol import action_step_to_protocol_messages
from src.trace import (
    get_current_agent_name,
    get_current_hook_run,
    get_current_runtime_agent_path,
)


def append_current_todo_state(messages: list, *, todo_mode: str) -> list:
    """Append the canonical current snapshot to one model input when active."""

    if todo_mode == "off":
        return messages
    from src.lib.todo import get_current_todo_provider

    provider = get_current_todo_provider()
    if provider is None:
        return messages
    agent_path = get_current_runtime_agent_path() or get_current_agent_name() or "default"
    snapshot = provider.load(agent_path)
    if not snapshot["items"]:
        return messages
    serialized = json.dumps(
        snapshot["items"],
        ensure_ascii=False,
        separators=(",", ":"),
    )
    serialized = serialized.replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026")
    reminder = (
        f'<current-todos revision="{snapshot["revision"]}">{serialized}</current-todos>\n'
        "This is the canonical Todo snapshot for the current task and Agent. "
        "Keep it current with todo_write when state changes. Call todo_write "
        "alone and finish the last update before calling final_answer."
    )
    hydrated = list(messages)
    hydrated.append(
        ChatMessage(
            role=MessageRole.SYSTEM,
            content=[
                {
                    "type": "text",
                    "text": wrap_in_system_reminder(reminder),
                }
            ],
        )
    )
    return hydrated


class LoomAgentMixin:
    """Enhancement layer that turns a base smolagents Agent into a Loom Agent.

    Provides:
    - before_run callbacks
    - smart memory compression (ConversationHistoryManager)
    - progressive error recovery guidance
    - canonical Todo snapshot hydration after context compression
    """

    def _init_loom_agent(
        self,
        before_run_callbacks: list | None,
        max_tokens: int | None = None,
        context_window: int | None = None,
        max_output_tokens: int | None = None,
        smart_summary: bool = True,
    ):
        self._before_run_callbacks = before_run_callbacks or []
        self._history_manager = ConversationHistoryManager(
            max_tokens=max_tokens,
            context_window=context_window,
            max_output_tokens=max_output_tokens,
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
        if hasattr(self, "memory"):
            messages = self.memory.system_prompt.to_messages(summary_mode=summary_mode)
            for memory_step in self.memory.steps:
                messages.extend(action_step_to_protocol_messages(memory_step, summary_mode=summary_mode))
        else:
            messages = super().write_memory_to_messages(summary_mode=summary_mode)

        if summary_mode:
            return append_current_todo_state(
                messages,
                todo_mode=getattr(self, "_agent_loom_todo_mode", "auto"),
            )

        hook_run = get_current_hook_run()
        if hook_run is not None:
            hook_run.step_number = getattr(self, "step_number", 0) or 0
            pending_user_messages = hook_run.consume_pending_user_messages()
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

        if hook_run is not None:
            pending_context = hook_run.consume_pending_agent_context()
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
        compressed_messages = append_current_todo_state(
            compressed_messages,
            todo_mode=getattr(self, "_agent_loom_todo_mode", "auto"),
        )

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
