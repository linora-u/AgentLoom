"""Non-configurable file-history protection at the tool execution boundary.

``record_active_file_history`` is the production entry point. ``FileHistoryHook``
is the local backup primitive it invokes; neither is a configurable
``HookPlan`` handler.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from src.lib.logging import get_logger

if TYPE_CHECKING:
    from src.lib.checkpoint.file_history import FileHistoryManager

_logger = get_logger(__name__)


def _file_modifying_specs() -> dict[str, tuple[str, ...]]:
    from src.tools.catalog import list_tool_specs

    # The catalog is the security contract for destructive tools. Falling
    # back to a hand-maintained subset would silently skip newly added tools.
    return {spec.name: spec.path_params for spec in list_tool_specs() if spec.is_destructive and spec.path_params}


class FileHistoryHook:
    """Compatibility adapter for automatic pre-write backup.

    When a file-modifying tool is about to run, this hook extracts
    the target file path and calls ``track_edit()`` on the
    :class:`FileHistoryManager`.

    Supports two calling conventions:

    1. **Hook Runtime integration** (``context`` kwarg or single positional
       ``HookContext``): the manager passes a ``HookContext`` with
       ``.tool_name`` and ``.tool_input``.
    2. **Direct / test invocation** (keyword args ``tool_name`` +
       ``tool_input``): allows easy unit-testing without constructing a
       full ``HookContext``.

    Direct invocation example::

        from src.lib.checkpoint.file_history_hook import FileHistoryHook

        hook = FileHistoryHook(file_history_manager, step_counter_fn)
        hook(tool_name="write_file", tool_input={"file_path": "notes.md"})
    """

    def __init__(
        self,
        fh: FileHistoryManager,
        get_step_number: Any = None,
    ) -> None:
        self._fh = fh
        self._get_step_number = get_step_number

    def __call__(
        self,
        *args: Any,
        context: Any = None,
        event_type: Any = None,
        tool_name: str | None = None,
        tool_input: dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> Any | None:
        """Intercept file-modifying tools and trigger backup.

        Accepts either ``context=HookContext(...)`` or explicit kwargs.
        """
        # Resolve tool_name and tool_input from context or direct args.
        if context is not None:
            tool_name = getattr(context, "tool_name", tool_name)
            tool_input = getattr(context, "tool_input", tool_input)
            if self._get_step_number is None:
                context_step = getattr(context, "step_number", None)
                if context_step is not None:
                    try:
                        step_number = int(context_step)
                    except (TypeError, ValueError):
                        step_number = 0
                else:
                    step_number = 0
            else:
                step_number = self._get_step_number()
        elif args and hasattr(args[0], "tool_name"):
            # Single positional HookContext.
            ctx = args[0]
            tool_name = getattr(ctx, "tool_name", tool_name)
            tool_input = getattr(ctx, "tool_input", tool_input)
            if self._get_step_number is None:
                context_step = getattr(ctx, "step_number", None)
                if context_step is not None:
                    try:
                        step_number = int(context_step)
                    except (TypeError, ValueError):
                        step_number = 0
                else:
                    step_number = 0
            else:
                step_number = self._get_step_number()
        else:
            step_number = self._get_step_number() if self._get_step_number is not None else 0

        if not tool_name or not tool_input:
            return None

        file_specs = _file_modifying_specs()
        path_params = file_specs.get(tool_name)
        if not path_params:
            return None

        # Extract the file path from tool arguments.
        file_path = None
        for param in path_params:
            file_path = tool_input.get(param)
            if file_path:
                break

        if not file_path:
            _logger.debug(
                "FileHistoryHook: no file path found in args for tool %s",
                tool_name,
            )
            return None

        try:
            self._fh.track_edit(str(file_path), step_number)
        except Exception as exc:
            _logger.warning(
                "FileHistoryHook: track_edit failed for %s: %s",
                file_path,
                exc,
            )
            raise

        return None


def record_active_file_history(
    *,
    tool_name: str,
    tool_input: dict[str, Any],
    step_number: int,
) -> None:
    """Record the final validated input at the non-configurable tool boundary.

    This entry point is called directly by the tool runtime after configurable
    ``PreToolUse`` transforms and the core path guard. It deliberately does not
    participate in ``HookPlan`` discovery or ordering.
    """

    from src.lib.checkpoint.coordinator import CheckpointCoordinator

    coordinator = CheckpointCoordinator.current()
    file_history = getattr(coordinator, "_file_history", None) if coordinator else None
    if file_history is None:
        return
    FileHistoryHook(file_history, get_step_number=lambda: step_number)(
        tool_name=tool_name,
        tool_input=dict(tool_input),
    )
