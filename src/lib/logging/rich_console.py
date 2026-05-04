from rich.console import Console
from pathlib import Path

class DualConsole(Console):
    """Dual-output console: output to terminal and file at the same time."""
    
    def __init__(self, log_file_path, *args, **kwargs):
        # Initialize parent Console (for terminal output).
        super().__init__(*args, **kwargs)
        # Do not reuse Console internals like `_closed`; keep our own file sink state.
        self._file_sink_closed = False
        self._file_sink_disabled = False

        # File sink is lazily opened on first write to avoid eager empty-file creation.
        self.log_file = None
        self.file_console = None
        if log_file_path:
            resolved_path = Path(log_file_path).expanduser()
            self.log_file_path = str(resolved_path.resolve())
        else:
            self.log_file_path = None

    def _ensure_file_sink_open(self) -> bool:
        if self._file_sink_closed or self._file_sink_disabled:
            return False
        if self.log_file and self.file_console:
            return True
        if not self.log_file_path:
            return False

        try:
            resolved_path = Path(self.log_file_path).expanduser()
            resolved_path.parent.mkdir(parents=True, exist_ok=True)
            self.log_file = open(resolved_path, "a", encoding="utf-8")
            self.file_console = Console(file=self.log_file, width=self.width, no_color=True, highlight=False)
            return True
        except Exception:
            # Keep terminal logging healthy even when file sink is not writable.
            self._file_sink_disabled = True
            self.log_file = None
            self.file_console = None
            return False

    def print(self, *args, **kwargs):
        # Output to terminal (call parent method).
        super().print(*args, **kwargs)
        # Output to file.
        if not self._ensure_file_sink_open():
            return
        try:
            self.file_console.print(*args, **kwargs)
            # Force flush buffer.
            self.log_file.flush()
        except Exception:
            self._file_sink_disabled = True
            if self.log_file:
                try:
                    self.log_file.close()
                except Exception:
                    pass
            self.log_file = None
            self.file_console = None

    def close_log_file(self):
        """Close file sink explicitly when caller needs deterministic cleanup."""
        if self._file_sink_closed:
            return
        if self.log_file:
            try:
                self.log_file.close()
            except Exception:
                pass
        self.log_file = None
        self.file_console = None
        self._file_sink_closed = True
