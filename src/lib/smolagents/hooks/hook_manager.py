"""Hook manager: central coordination point for the hook system.

Provides hook registration, pattern matching, **true parallel** execution
via ``ThreadPoolExecutor``, timeout enforcement pushed down to each
executor, permission precedence aggregation (deny > allow > passthrough),
deduplication, once-flag auto-removal, thread safety, a global enable /
disable switch, and a config bridge that converts YAML-defined
``HookCommand`` objects into executable callables.

Aligned with the upstream ``executeHooks()`` engine.
"""

from __future__ import annotations

import inspect
import os
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextvars import copy_context
from threading import Event, RLock, Thread
from typing import Any, Callable, Dict, List, Optional

from src.lib.logging import get_logger

from .hook_helpers import hook_dedup_key, matches_pattern
from .types import (
    AggregatedHookResult,
    AgentHook,
    CommandHook,
    HookCommand,
    HookContext,
    HookEvent,
    HookResult,
    HttpHook,
    PromptHook,
)

logger = get_logger(__name__)

# Maximum number of pending messages before the oldest are dropped.
_MAX_PENDING_ITEMS: int = 1000


def wrap_in_system_reminder(content: str) -> str:
    """Wrap content in <system-reminder> tags for priority signaling.

    LLMs treat XML-tagged content as higher-priority instructions.
    This wrapper is idempotent: already-wrapped content is returned as-is.
    Empty/whitespace-only input returns an empty string.
    """
    stripped = content.strip()
    if not stripped:
        return ""
    if stripped.startswith("<system-reminder>"):
        return stripped
    return f"<system-reminder>\n{stripped}\n</system-reminder>"


def _context_to_dict(ctx: HookContext) -> Dict[str, Any]:
    """Convert a ``HookContext`` to a plain dict for executor consumption."""
    return {
        "session_id": ctx.session_id,
        "root_run_id": ctx.root_run_id,
        "cwd": ctx.cwd,
        "hook_event_name": ctx.hook_event_name,
        "tool_name": ctx.tool_name,
        "tool_call_id": ctx.tool_call_id,
        "tool_input": ctx.tool_input or {},
        "tool_response": ctx.tool_response,
        "tool_inputs_schema": ctx.tool_inputs_schema,
        "step_number": ctx.step_number,
        "task_id": ctx.task_id,
        "sub_task_id": ctx.sub_task_id,
        "agent_name": ctx.agent_name,
        "agent_config": ctx.agent_config or {},
    }

# Default per-hook timeout (seconds) when none is specified.
_DEFAULT_HOOK_TIMEOUT: float = 20.0

# Maximum parallel workers for hook execution.
_MAX_HOOK_WORKERS: int = 8

# Default timeouts per hook type (seconds), aligned with upstream.
_TYPE_DEFAULT_TIMEOUTS: Dict[str, float] = {
    "command": 600.0,
    "prompt": 30.0,
    "http": 600.0,
    "agent": 60.0,
}


class HookManager:
    """Central manager for all hook operations (Singleton).

    Thread-safe via ``_data_lock`` (RLock) protecting all mutable state.
    Global enable/disable via ``_hooks_enabled``.
    """

    _instance: Optional["HookManager"] = None
    _lock = RLock()

    def __init__(self) -> None:
        self._data_lock = RLock()
        self.hooks: Dict[HookEvent, List[Dict[str, Any]]] = {
            e: [] for e in HookEvent
        }
        self._session_id = str(uuid.uuid4())
        self._pending_agent_context: List[str] = []
        self._pending_user_messages: List[str] = []
        self._user_message_sink: Optional[Callable[[str], None]] = None
        self._hooks_enabled: bool = True
        self._hook_metrics: Dict[str, List[float]] = {}
        self._config_manager: Optional[Any] = None  # HooksConfigManager
        # Updated by the owning agent each step via write_memory_to_messages().
        # Per-agent instance — no lock needed (see design.md S2.0).
        self.step_number: int = 0
        logger.debug("HookManager initialized")

    # -----------------------------------------------------------------
    # Singleton
    # -----------------------------------------------------------------

    @classmethod
    def get_instance(cls) -> "HookManager":
        """Get the singleton instance (double-checked locking)."""
        if cls._instance is None:
            with cls._lock:
                if cls._instance is None:
                    cls._instance = cls()
        return cls._instance

    # -----------------------------------------------------------------
    # Global enable / disable
    # -----------------------------------------------------------------

    def disable_hooks(self) -> None:
        """Disable all hook execution globally."""
        with self._data_lock:
            self._hooks_enabled = False
        logger.info("Hooks globally disabled")

    def enable_hooks(self) -> None:
        """Re-enable hook execution globally."""
        with self._data_lock:
            self._hooks_enabled = True
        logger.info("Hooks globally enabled")

    @property
    def hooks_enabled(self) -> bool:
        with self._data_lock:
            return self._hooks_enabled

    # -----------------------------------------------------------------
    # Registration
    # -----------------------------------------------------------------

    def register_hook(
        self,
        event: HookEvent,
        pattern: str,
        func: Callable,
        timeout: float = _DEFAULT_HOOK_TIMEOUT,
        allow_duplicates: bool = True,
        once: bool = False,
        source: str = "",
        must_complete: bool = False,
    ) -> None:
        """Register a Python function as a hook.

        The must_complete flag is reserved for framework lifecycle effects
        whose caller cannot safely continue after a timeout. Ordinary hooks
        should retain the bounded timeout default.

        Raises TypeError if func is not callable.
        """
        if not callable(func):
            logger.error(
                "Hook registration failed: func is not callable (%s)",
                type(func).__name__,
            )
            raise TypeError(
                f"Hook function must be callable, got {type(func).__name__}"
            )

        entry: Dict[str, Any] = {
            "pattern": pattern,
            "func": func,
            "timeout": timeout,
            "once": once,
            "source": source,
            "must_complete": bool(must_complete),
        }

        with self._data_lock:
            if not allow_duplicates:
                dedup = hook_dedup_key(func, source, None)  # type: ignore[arg-type]
                for existing in self.hooks.get(event, []):
                    existing_dedup = hook_dedup_key(
                        existing["func"], existing.get("source", ""), None  # type: ignore[arg-type]
                    )
                    if dedup == existing_dedup:
                        logger.debug(
                            "Skipping duplicate hook for %s pattern=%s",
                            event.value, pattern,
                        )
                        return

            self.hooks[event].append(entry)

        logger.debug("Registered hook for %s with pattern %s", event.value, pattern)

    def remove_hook(self, event: HookEvent, func: Callable) -> bool:
        """Remove a specific hook function from an event.  Returns True if found."""
        with self._data_lock:
            hooks = self.hooks.get(event, [])
            for i, entry in enumerate(hooks):
                if entry["func"] is func:
                    hooks.pop(i)
                    logger.debug("Removed hook for %s", event.value)
                    return True
        return False

    def get_registered_hooks(self, event: Optional[HookEvent] = None) -> List[Dict[str, Any]]:
        """Return a copy of registered hooks (for debugging)."""
        with self._data_lock:
            if event is not None:
                return list(self.hooks.get(event, []))
            result = {}
            for ev, hooks_list in self.hooks.items():
                if hooks_list:
                    result[ev] = list(hooks_list)
            return result  # type: ignore[return-value]

    def clear_hooks(self) -> None:
        """Remove all registered hooks."""
        with self._data_lock:
            self.hooks = {e: [] for e in HookEvent}
        logger.debug("All hooks cleared")

    # -----------------------------------------------------------------
    # Message queue (with bounded size)
    # -----------------------------------------------------------------

    @staticmethod
    def _append_text(existing: Optional[str], extra: Optional[str]) -> Optional[str]:
        if extra is None:
            return existing
        extra = str(extra).strip()
        if not extra:
            return existing
        if existing is None or not str(existing).strip():
            return extra
        return f"{existing}\n{extra}"

    def set_user_message_sink(self, sink: Optional[Callable[[str], None]]) -> None:
        self._user_message_sink = sink

    @staticmethod
    def _preview_hook_text(result: HookResult) -> Optional[str]:
        for value in (result.reason, result.user_message, result.agent_context):
            if value is None:
                continue
            normalized = str(value).strip()
            if normalized:
                return normalized
        return None

    def queue_agent_context(self, text: Optional[str]) -> None:
        if text is None:
            return
        normalized = str(text).strip()
        if not normalized:
            return
        with self._data_lock:
            if len(self._pending_agent_context) >= _MAX_PENDING_ITEMS:
                self._pending_agent_context.pop(0)
                logger.warning("Pending agent context queue overflow, dropping oldest")
            self._pending_agent_context.append(normalized)

    def queue_user_message(self, text: Optional[str]) -> None:
        if text is None:
            return
        normalized = str(text).strip()
        if not normalized:
            return
        with self._data_lock:
            if len(self._pending_user_messages) >= _MAX_PENDING_ITEMS:
                self._pending_user_messages.pop(0)
                logger.warning("Pending user message queue overflow, dropping oldest")
            self._pending_user_messages.append(normalized)

    def consume_pending_agent_context(self) -> List[str]:
        with self._data_lock:
            values = list(self._pending_agent_context)
            self._pending_agent_context.clear()
        return values

    def consume_pending_user_messages(self) -> List[str]:
        with self._data_lock:
            values = list(self._pending_user_messages)
            self._pending_user_messages.clear()
        return values

    def flush_user_messages(self) -> List[str]:
        messages = self.consume_pending_user_messages()
        if not messages:
            return []

        sink = self._user_message_sink
        if sink is not None:
            for message in messages:
                try:
                    sink(message)
                except Exception as exc:
                    logger.warning("Failed to deliver hook user message to sink: %s", exc)
        else:
            for message in messages:
                logger.info("Hook user message: %s", message)
        return messages

    # -----------------------------------------------------------------
    # Stop-check builder
    # -----------------------------------------------------------------

    def build_stop_check(self) -> Callable[[Any, Any], bool]:
        def _check(final_answer: Any, memory: Any, **_kwargs: Any) -> bool:
            memory_steps = getattr(memory, "steps", None)
            tool_response = None
            if isinstance(memory_steps, list):
                tool_response = {"memory_steps": len(memory_steps)}

            result = self.trigger_hooks(
                HookEvent.STOP,
                "final_answer",
                {"final_answer": final_answer},
                tool_response=tool_response,
            )
            self.flush_user_messages()
            if result.should_block():
                raise AssertionError(result.reason or "Stop hook blocked final answer")
            return True

        return _check

    # -----------------------------------------------------------------
    # Metrics
    # -----------------------------------------------------------------

    def _record_metric(self, name: str, duration: float) -> None:
        with self._data_lock:
            if name not in self._hook_metrics:
                self._hook_metrics[name] = []
            metrics = self._hook_metrics[name]
            metrics.append(duration)
            # Keep last 1000 entries
            if len(metrics) > 1000:
                self._hook_metrics[name] = metrics[-100:]

    def get_hook_metrics(self) -> Dict[str, Dict[str, float]]:
        """Return execution time statistics for all hooks."""
        with self._data_lock:
            result = {}
            for name, times in self._hook_metrics.items():
                if times:
                    result[name] = {
                        "calls": len(times),
                        "total_time": sum(times),
                        "avg_time": sum(times) / len(times),
                        "max_time": max(times),
                        "min_time": min(times),
                    }
            return result

    # -----------------------------------------------------------------
    # Config manager injection
    # -----------------------------------------------------------------

    def set_config_manager(self, cm: Any) -> None:
        """Inject a ``HooksConfigManager`` for YAML-based hook resolution."""
        with self._data_lock:
            self._config_manager = cm
        logger.debug("HooksConfigManager injected into HookManager")

    # -----------------------------------------------------------------
    # Core execution engine
    # -----------------------------------------------------------------

    def _execute_single_hook(
        self,
        hook_info: Dict[str, Any],
        context: HookContext,
        execution_context: Any = None,
    ) -> Optional[HookResult]:
        """Execute a single hook with timeout enforcement.

        Config-sourced hooks (command/prompt/http/agent executors) handle
        their own timeouts internally. Raw Python function hooks normally run
        in a daemon thread with a bounded wait as a safety net. A framework
        lifecycle hook may declare must_complete when returning before its
        side effect commits would violate a caller invariant.
        """
        func = hook_info["func"]
        timeout = hook_info.get("timeout", _DEFAULT_HOOK_TIMEOUT)
        must_complete = bool(hook_info.get("must_complete", False))
        func_name = getattr(func, "__name__", repr(func))
        source = hook_info.get("source", "")

        start_time = time.monotonic()

        def _invoke() -> Optional[HookResult]:
            try:
                sig = inspect.signature(func)
                if "context" in sig.parameters:
                    return func(context=context)
                elif len(sig.parameters) >= 1:
                    return func(context)
                else:
                    logger.warning("Skipping hook %s: Invalid signature", func_name)
                    return None
            except Exception as exc:
                logger.error("Error executing hook %s: %s", func_name, exc)
                return HookResult(
                    success=False,
                    decision="allow",
                    outcome="non_blocking_error",
                    reason=f"Hook {func_name} raised: {exc}",
                )

        # Config-sourced executors manage their own timeout (litellm
        # timeout, Popen Timer, httpx timeout). Raw Python function
        # hooks have no internal timeout mechanism, so we enforce one via
        # a daemon worker thread plus a bounded wait.
        if source.startswith("config:") or must_complete:
            # Config executors own their timeout. A must-complete framework
            # hook intentionally has no detached timeout boundary.
            res = _invoke()
        else:
            # Python function hook -- run in a daemon thread so timed-out
            # hooks do not block interpreter shutdown during test teardown.
            result_holder: List[Optional[HookResult]] = [None]
            completed = Event()

            def _run_in_worker() -> None:
                try:
                    if execution_context is None:
                        result_holder[0] = _invoke()
                    else:
                        from src.trace import bind_explicit_execution_context

                        # ``Thread`` does not copy ContextVars. Rebind the
                        # immutable caller snapshot so Python hooks observe the
                        # same run/config identity as their HookContext.
                        with bind_explicit_execution_context(execution_context):
                            result_holder[0] = _invoke()
                finally:
                    completed.set()

            worker_context = copy_context()
            worker = Thread(
                target=worker_context.run,
                args=(_run_in_worker,),
                name=f"hook-timeout-{func_name}-{uuid.uuid4().hex[:8]}",
                daemon=True,
            )
            worker.start()

            if not completed.wait(timeout=timeout):
                logger.warning(
                    "Hook %s timed out after %.1fs, skipping",
                    func_name, timeout,
                )
                return HookResult(
                    success=False,
                    decision="allow",
                    outcome="cancelled",
                    reason=f"Hook timed out after {timeout}s",
                )

            res = result_holder[0]

        duration = time.monotonic() - start_time
        self._record_metric(func_name, duration)

        if isinstance(res, HookResult):
            preview = self._preview_hook_text(res)
            if preview and len(preview) > 120:
                preview = preview[:120] + "..."
            logger.info(
                "Hook executed: func=%s success=%s duration=%.2fs message=%r",
                func_name, res.success, duration, preview,
            )
            return res

        return None

    def _execute_single_hook_in_context(
        self,
        hook_info: Dict[str, Any],
        context: HookContext,
        execution_context: Any,
    ) -> Optional[HookResult]:
        """Execute one hook under the caller's explicit runtime context.

        ``trigger_hooks`` may first cross a ThreadPoolExecutor boundary and raw
        Python hooks then cross a second daemon-thread boundary for timeout
        enforcement. This outer binding covers the pool/config-hook boundary;
        ``_execute_single_hook`` rebinds once more inside the daemon worker.
        """
        if execution_context is None:
            return self._execute_single_hook(
                hook_info,
                context,
                execution_context=None,
            )

        from src.trace import bind_explicit_execution_context

        with bind_explicit_execution_context(execution_context):
            return self._execute_single_hook(
                hook_info,
                context,
                execution_context=execution_context,
            )

    # -----------------------------------------------------------------
    # Config hook bridge: HookCommand -> Callable
    # -----------------------------------------------------------------

    def _get_config_hooks(
        self,
        event: HookEvent,
        tool_name: str,
    ) -> List[Dict[str, Any]]:
        """Bridge: resolve matching config hooks into executable entries.

        Reads from the injected ``HooksConfigManager``, applies matcher
        pattern and if-condition filtering, and wraps each ``HookCommand``
        into a ``Callable[[HookContext], HookResult]``.
        """
        cm = self._config_manager
        if cm is None or not getattr(cm, "enabled", True):
            return []

        try:
            snapshot = cm.snapshot()
        except Exception:
            return []

        matchers = snapshot.get_matchers(event.value)
        if not matchers:
            return []

        result: List[Dict[str, Any]] = []
        for matcher in matchers:
            if not matches_pattern(tool_name, matcher.matcher):
                continue
            for hook_cmd in matcher.hooks:
                # if-condition filtering (simple tool-name match for now)
                if_cond = getattr(hook_cmd, "if_condition", None)
                if if_cond and tool_name not in if_cond:
                    continue

                executor = self._build_executor(hook_cmd)
                if executor is None:
                    continue

                hook_type = getattr(hook_cmd, "type", "command")
                result.append({
                    "pattern": matcher.matcher or "*",
                    "func": executor,
                    "timeout": getattr(hook_cmd, "timeout", None) or _TYPE_DEFAULT_TIMEOUTS.get(hook_type, _DEFAULT_HOOK_TIMEOUT),
                    "once": getattr(hook_cmd, "once", False),
                    "source": f"config:{hook_type}",
                })

        return result

    @staticmethod
    def _build_executor(hook_cmd: HookCommand) -> Optional[Callable]:
        """Convert a declarative ``HookCommand`` into an executable callable.

        Each returned callable has the signature
        ``(context: HookContext) -> HookResult``.
        """
        if isinstance(hook_cmd, CommandHook):
            def _cmd_executor(context: HookContext) -> HookResult:
                from .exec_command_hook import exec_command_hook
                from .async_hook_registry import AsyncHookRegistry
                return exec_command_hook(
                    hook_cmd,
                    _context_to_dict(context),
                    cwd=context.cwd,
                    timeout=hook_cmd.timeout,
                    async_registry=AsyncHookRegistry.get_instance(),
                )
            return _cmd_executor

        if isinstance(hook_cmd, PromptHook):
            def _prompt_executor(context: HookContext) -> HookResult:
                from .exec_prompt_hook import exec_prompt_hook
                return exec_prompt_hook(
                    hook_cmd,
                    _context_to_dict(context),
                    timeout=hook_cmd.timeout,
                )
            return _prompt_executor

        if isinstance(hook_cmd, HttpHook):
            def _http_executor(context: HookContext) -> HookResult:
                from .exec_http_hook import exec_http_hook
                return exec_http_hook(
                    hook_cmd,
                    _context_to_dict(context),
                    timeout=hook_cmd.timeout,
                )
            return _http_executor

        if isinstance(hook_cmd, AgentHook):
            def _agent_executor(context: HookContext) -> HookResult:
                from .exec_agent_hook import exec_agent_hook
                return exec_agent_hook(
                    hook_cmd,
                    _context_to_dict(context),
                    timeout=hook_cmd.timeout,
                )
            return _agent_executor

        logger.warning("Unknown hook command type: %s", type(hook_cmd).__name__)
        return None

    # -----------------------------------------------------------------
    # Trigger hooks (parallel execution)
    # -----------------------------------------------------------------

    def trigger_hooks(
        self,
        event: HookEvent,
        tool_name: str,
        tool_input: Dict[str, Any],
        tool_response: Optional[Dict[str, Any]] = None,
        tool_inputs_schema: Optional[Dict[str, Any]] = None,
        tool_call_id: Optional[str] = None,
    ) -> HookResult:
        """Trigger all matching hooks for an event.

        Merges **function hooks** (registered via ``register_hook()``) and
        **config hooks** (loaded from YAML via ``HooksConfigManager``),
        then executes them in **parallel** via ``ThreadPoolExecutor``.

        Permission precedence aggregation: deny > allow > passthrough.
        Once-flag hooks are auto-removed after execution.
        Returns a single merged ``HookResult``.
        """
        # Global disable check
        if not self.hooks_enabled:
            return HookResult(success=True, decision="allow")

        execution_context = None
        try:
            from src.trace import capture_explicit_execution_context

            execution_context = capture_explicit_execution_context()
            task_id = execution_context.task_id
            sub_task_id = execution_context.sub_task_id
            agent_name = execution_context.agent_name
            agent_config = execution_context.agent_config
            root_run_id = execution_context.root_run_id
            local_run_id = execution_context.local_run_id
        except Exception:
            task_id = None
            sub_task_id = None
            agent_name = None
            agent_config = None
            root_run_id = None
            local_run_id = None

        context = HookContext(
            # BaseAgent binds a fresh local id before dispatch. A manager's
            # construction UUID is not a run identity, so direct/unbound calls
            # stay explicitly empty and persistence can fail closed.
            session_id=local_run_id or "",
            cwd=os.getcwd(),
            hook_event_name=event.value,
            tool_name=tool_name,
            tool_input=tool_input,
            tool_call_id=tool_call_id,
            root_run_id=root_run_id,
            tool_response=tool_response,
            tool_inputs_schema=tool_inputs_schema,
            step_number=self.step_number,
            task_id=task_id,
            sub_task_id=sub_task_id,
            agent_name=agent_name,
            agent_config=agent_config,
        )

        final_result = HookResult(success=True, decision="allow")

        # --- Collect from both pipelines ---
        with self._data_lock:
            func_hooks = list(self.hooks.get(event, []))

        config_hooks = self._get_config_hooks(event, tool_name)

        # --- Pattern-filter function hooks ---
        matched_func_hooks: List[Dict[str, Any]] = []
        for hook_info in func_hooks:
            pattern = hook_info["pattern"]
            if matches_pattern(tool_name, pattern):
                matched_func_hooks.append(hook_info)

        all_hooks = matched_func_hooks + config_hooks

        if not all_hooks:
            return final_result

        # --- Log matched hooks ---
        for h in all_hooks:
            logger.info(
                "Hook matched: pattern='%s' tool='%s' event=%s source=%s",
                h.get("pattern", "*"), tool_name, event.value,
                h.get("source", "function"),
            )

        once_to_remove: List[Dict[str, Any]] = []
        for h in all_hooks:
            if h.get("once"):
                once_to_remove.append(h)

        # --- Execute: parallel for multiple hooks, direct for single ---
        if len(all_hooks) == 1:
            results = [
                self._execute_single_hook_in_context(
                    all_hooks[0],
                    context,
                    execution_context,
                )
            ]
        else:
            worker_count = min(len(all_hooks), _MAX_HOOK_WORKERS)
            results = [None] * len(all_hooks)
            with ThreadPoolExecutor(max_workers=worker_count) as pool:
                future_map = {
                    pool.submit(
                        copy_context().run,
                        self._execute_single_hook_in_context,
                        h,
                        context,
                        execution_context,
                    ): idx
                    for idx, h in enumerate(all_hooks)
                }
                for future in as_completed(future_map):
                    idx = future_map[future]
                    try:
                        results[idx] = future.result()
                    except Exception as exc:
                        logger.error("Hook future raised: %s", exc)
                        results[idx] = None

        # --- Merge results ---
        for res in results:
            if res is None:
                continue

            if not res.success:
                final_result.success = False

            # Queue context / messages
            effective_context = res.additional_context or res.agent_context
            if effective_context:
                self.queue_agent_context(effective_context)
                final_result.agent_context = self._append_text(
                    final_result.agent_context, effective_context,
                )
                final_result.additional_context = self._append_text(
                    final_result.additional_context, effective_context,
                )

            if res.user_message:
                self.queue_user_message(res.user_message)
                final_result.user_message = self._append_text(
                    final_result.user_message, res.user_message,
                )

            if res.reason:
                final_result.reason = self._append_text(final_result.reason, res.reason)

            if res.telemetry:
                final_result.telemetry.update(res.telemetry)

            # Permission precedence: deny wins over everything
            if res.should_block():
                logger.warning(
                    "Hook blocked action: %s",
                    res.reason or "blocked without reason",
                )
                final_result.decision = "block"
                final_result.outcome = "blocking"
                final_result.permission_behavior = "deny"
                final_result.prevent_continuation = True
                final_result.blocking_error = res.blocking_error
                final_result.stop_reason = res.stop_reason or res.reason

            # Modify: accumulate input/response changes
            elif res.decision == "modify":
                effective_input = res.updated_input or res.modified_input
                if effective_input:
                    merged = dict(tool_input) if isinstance(tool_input, dict) else {}
                    merged.update(effective_input)
                    context.tool_input = merged
                    tool_input = merged
                    final_result.modified_input = merged
                    final_result.updated_input = merged
                    if final_result.decision != "block":
                        final_result.decision = "modify"

                effective_response = res.updated_response or res.modified_response
                if effective_response:
                    final_result.modified_response = effective_response
                    final_result.updated_response = effective_response

        # Remove once-hooks after execution
        self._remove_once_hooks(event, once_to_remove)

        return final_result

    def _remove_once_hooks(
        self,
        event: HookEvent,
        to_remove: List[Dict[str, Any]],
    ) -> None:
        """Remove once-flagged hooks after execution."""
        if not to_remove:
            return
        with self._data_lock:
            hooks = self.hooks.get(event, [])
            for entry in to_remove:
                try:
                    hooks.remove(entry)
                    logger.debug("Removed once-hook for %s", event.value)
                except ValueError:
                    pass
