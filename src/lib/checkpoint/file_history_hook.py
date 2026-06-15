"""
PRE_TOOL_USE hook that triggers file history backup before file-modifying tools.

Registered on the HookManager so that ``track_edit()`` is called
automatically before any tool that might write or modify a file.

The hook accepts either a ``HookContext`` (used by the real HookManager)
or raw ``(event_type, tool_name, tool_input)`` args (for unit-test convenience).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Optional

from src.lib.logging import get_logger

if TYPE_CHECKING:
    from src.lib.checkpoint.file_history import FileHistoryManager

_logger = get_logger(__name__)

# Tools that modify files on disk. Extend this set for custom tools.
FILE_MODIFYING_TOOLS = frozenset({
    "edit_file",
    "write_file",
    "write_markdown_file",
    "create_file",
    "move_file",
    "copy_file",
})

# Tool parameter names that contain file paths.
_PATH_PARAMS = ("file_path", "path", "filePath", "source", "destination")


class FileHistoryHook:
    """PRE_TOOL_USE hook for automatic file backup.

    When a file-modifying tool is about to run, this hook extracts
    the target file path and calls ``track_edit()`` on the
    :class:`FileHistoryManager`.

    Supports two calling conventions:

    1. **HookManager integration** (``context`` kwarg or single positional
       ``HookContext``): the manager passes a ``HookContext`` with
       ``.tool_name`` and ``.tool_input``.
    2. **Direct / test invocation** (keyword args ``tool_name`` +
       ``tool_input``): allows easy unit-testing without constructing a
       full ``HookContext``.

    Registration example::

        from src.lib.checkpoint.file_history_hook import FileHistoryHook

        hook = FileHistoryHook(file_history_manager, step_counter_fn)
        hook_manager.register_hook(HookEvent.PRE_TOOL_USE, "*", hook, source="file_history")
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
    ) -> Optional[Any]:
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

        if tool_name not in FILE_MODIFYING_TOOLS:
            return None

        # Extract the file path from tool arguments.
        file_path = None
        for param in _PATH_PARAMS:
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
                file_path, exc,
            )

        return None
