from __future__ import annotations

import threading
from io import StringIO
from pathlib import Path
from typing import Any, TextIO

from rich.console import Console

from src.lib.runtime import RuntimeContext, RuntimeRotatingTextSink


class DualConsole(Console):
    """Rich console with an optional bounded, rotating plain-text file sink."""

    def __init__(
        self,
        log_file_path: str | None,
        *args: Any,
        max_file_bytes: int = 25 * 1024 * 1024,
        backup_count: int = 3,
        console_enabled: bool = True,
        runtime_context: RuntimeContext | None = None,
        **kwargs: Any,
    ) -> None:
        super().__init__(*args, **kwargs)
        self.console_enabled = bool(console_enabled)
        self.max_file_bytes = max(1, int(max_file_bytes))
        self.backup_count = max(0, int(backup_count))
        self.log_file_path = (
            str(Path(log_file_path).expanduser().absolute()) if log_file_path else None
        )
        self.log_file: TextIO | None = None
        self.file_console: Console | None = None
        self._file_sink_closed = False
        self._file_sink_disabled = False
        self._file_lock = threading.RLock()
        self._runtime_sink = None
        if self.log_file_path:
            if runtime_context is None:
                raise ValueError("file logging requires a RuntimeContext")
            self._runtime_sink = RuntimeRotatingTextSink(
                runtime_context,
                Path(self.log_file_path),
                max_file_bytes=self.max_file_bytes,
                backup_count=self.backup_count,
            )

    def _open_file_sink(self) -> bool:
        if self._file_sink_closed or self._file_sink_disabled or not self.log_file_path:
            return False
        return self._runtime_sink is not None

    def _render_plain(self, *args: Any, **kwargs: Any) -> str:
        buffer = StringIO()
        file_console = Console(
            file=buffer,
            width=self.width,
            no_color=True,
            highlight=False,
            force_terminal=False,
        )
        file_console.print(*args, **kwargs)
        return buffer.getvalue()

    def _rollover(self) -> None:
        # Rotation is owned by RuntimeRotatingTextSink so every rename remains
        # anchored to the validated run directory descriptor.
        return None

    def _write_file(self, rendered: str) -> None:
        if not rendered or not self.log_file_path:
            return
        with self._file_lock:
            try:
                if not self._open_file_sink() or self._runtime_sink is None:
                    return
                self._runtime_sink.write(rendered)
                self.log_file = self._runtime_sink.stream
            except (OSError, RuntimeError):
                self._file_sink_disabled = True
                if self._runtime_sink is not None:
                    self._runtime_sink.close()
                    self._runtime_sink = None
                self.log_file = None
                return

    def print(self, *args: Any, **kwargs: Any) -> None:
        if self.console_enabled:
            super().print(*args, **kwargs)
        if self.log_file_path:
            self._write_file(self._render_plain(*args, **kwargs))

    def close_log_file(self) -> None:
        with self._file_lock:
            if self._file_sink_closed:
                return
            if self.log_file is not None:
                self.log_file = None
            if self._runtime_sink is not None:
                try:
                    self._runtime_sink.close()
                except OSError:
                    pass
                self._runtime_sink = None
            self.log_file = None
            self.file_console = None
            self._file_sink_closed = True
