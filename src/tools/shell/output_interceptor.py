import os
import uuid
from pathlib import Path

from src.lib.runtime import get_current_run_context


class OutputInterceptor:
    """
    OutputInterceptor buffers terminal/shell command output and limits its maximum size.
    It uses a "head/tail buffer" strategy:
    - 50% of the byte budget goes to the beginning (head)
    - 50% of the byte budget goes to the end (tail)
    - The middle is dropped if output exceeds the maximum bytes.

    If output exceeds the preview threshold, it spills the entire unbroken output to disk.
    """

    def __init__(
        self,
        preview_bytes: int = 30000,
        storage_dir: str | Path | None = None,
    ):
        """
        Initializes the output interceptor for shell commands.
        """
        self.preview_bytes = preview_bytes
        self.head_budget = preview_bytes // 2
        self.tail_budget = preview_bytes - self.head_budget

        self.head_buffer = bytearray()
        self.tail_buffer = bytearray()
        self.omitted_bytes = 0
        self.total_bytes = 0

        self.pending_chunks = []
        self.spilled_to_disk = False
        self._disk_spill_unavailable = False

        self._runtime_context = None
        if storage_dir is None:
            runtime_context = get_current_run_context()
            self._runtime_context = runtime_context
            storage_dir = runtime_context.shell_artifacts_dir if runtime_context else None
        self.storage_dir = str(Path(storage_dir).resolve()) if storage_dir else None
        # artifact_path is assigned lazily in _spill_to_disk() — only when we
        # actually need to write to disk. No UUID is generated for small outputs.
        self.artifact_path: str | None = None
        self.file_stream = None

    def write(self, chunk: str) -> None:
        """
        Write a chunk of text. Updates buffers and possibly spills to disk.
        """
        if not chunk:
            return

        chunk_bytes = chunk.encode("utf-8")
        chunk_len = len(chunk_bytes)
        self.total_bytes += chunk_len

        self._add_to_preview_buffers(chunk_bytes)

        if not self.spilled_to_disk:
            self.pending_chunks.append(chunk_bytes)
            if self.total_bytes > self.preview_bytes and not self._disk_spill_unavailable:
                self._spill_to_disk()
        else:
            if self.file_stream:
                self.file_stream.write(chunk_bytes)

    def _add_to_preview_buffers(self, chunk_bytes: bytes) -> None:
        remaining_bytes = chunk_bytes

        # Fill head buffer if there is room
        head_len = len(self.head_buffer)
        if head_len < self.head_budget:
            head_room = self.head_budget - head_len
            if len(remaining_bytes) <= head_room:
                self.head_buffer.extend(remaining_bytes)
                return
            else:
                self.head_buffer.extend(remaining_bytes[:head_room])
                remaining_bytes = remaining_bytes[head_room:]

        self._add_to_tail_buffer(remaining_bytes)

    def _add_to_tail_buffer(self, chunk_bytes: bytes) -> None:
        chunk_len = len(chunk_bytes)
        if self.tail_budget == 0:
            self.omitted_bytes += chunk_len
            return

        # If incoming chunk itself exceeds the full tail budget, keep only the latest subset
        if chunk_len >= self.tail_budget:
            dropped = len(self.tail_buffer) + (chunk_len - self.tail_budget)
            self.omitted_bytes += dropped
            self.tail_buffer = bytearray(chunk_bytes[-self.tail_budget :])
            return

        self.tail_buffer.extend(chunk_bytes)

        # Trim from front if over budget
        if len(self.tail_buffer) > self.tail_budget:
            excess = len(self.tail_buffer) - self.tail_budget
            self.omitted_bytes += excess
            self.tail_buffer = self.tail_buffer[excess:]

    def _spill_to_disk(self) -> None:
        if self.storage_dir is None:
            # Standalone shell helpers have no canonical owner for a raw
            # artifact. Keep the bounded preview and discard the full buffer.
            self.pending_chunks = []
            self._disk_spill_unavailable = True
            return
        try:
            if self._runtime_context is not None:
                fd, artifact_path = self._runtime_context.allocate_artifact(
                    "shell",
                    prefix="cmd-",
                    suffix=".txt",
                )
                self.artifact_path = str(artifact_path)
                self.file_stream = os.fdopen(fd, "wb")
            else:
                os.makedirs(self.storage_dir, exist_ok=True)
                # Explicit standalone storage remains available to low-level callers.
                self.artifact_path = os.path.join(
                    self.storage_dir,
                    f"cmd-{uuid.uuid4().hex}.txt",
                )
                self.file_stream = open(self.artifact_path, "xb")
        except (OSError, RuntimeError):
            self.pending_chunks = []
            self._disk_spill_unavailable = True
            self.artifact_path = None
            self.file_stream = None
            return
        for chunk in self.pending_chunks:
            self.file_stream.write(chunk)
        self.pending_chunks = []
        self.spilled_to_disk = True

    def finalize(self) -> str:
        """
        Finalizes the interceptor, closing file streams and returning the string preview.
        """
        if self.file_stream:
            self.file_stream.close()
            self.file_stream = None

        # Decode head and tail carefully, replacing broken utf-8 sequences
        head_str = self.head_buffer.decode("utf-8", errors="replace")
        tail_str = self.tail_buffer.decode("utf-8", errors="replace")

        if self.omitted_bytes > 0:
            omission = f"\n\n[...{self.omitted_bytes} bytes omitted...]\n"
            if self.spilled_to_disk and self.artifact_path is not None:
                abs_path = os.path.abspath(self.artifact_path)
                omission += (
                    f"<system_notice>\n"
                    f"The command output was too large and has been safely truncated.\n"
                    f"The FULL, unbroken output log has been saved to: {abs_path}\n"
                    f"If you need to analyze the missing middle portions or search for specific errors,\n"
                    f"you MUST use your file reading/grepping tools on that path.\n"
                    f"</system_notice>\n\n"
                )
            return head_str + omission + tail_str
        else:
            return head_str + tail_str
