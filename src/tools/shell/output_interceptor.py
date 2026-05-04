import os
import uuid
import tempfile
from typing import Optional


class OutputInterceptor:
    """
    OutputInterceptor buffers terminal/shell command output and limits its maximum size.
    It uses a "head/tail buffer" strategy:
    - 50% of the byte budget goes to the beginning (head)
    - 50% of the byte budget goes to the end (tail)
    - The middle is dropped if output exceeds the maximum bytes.

    If output exceeds the preview threshold, it spills the entire unbroken output to disk.
    """

    def __init__(self, preview_bytes: int = 30000, storage_dir: Optional[str] = None):
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

        if storage_dir is None:
            storage_dir = os.path.join(os.getcwd(), ".logs", "shell_outputs")
        self.storage_dir = storage_dir
        # artifact_path is assigned lazily in _spill_to_disk() — only when we
        # actually need to write to disk. No UUID is generated for small outputs.
        self.artifact_path: Optional[str] = None
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
            if self.total_bytes > self.preview_bytes:
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
        os.makedirs(self.storage_dir, exist_ok=True)
        # Generate UUID filename only now — when we know we actually need it.
        self.artifact_path = os.path.join(self.storage_dir, f"cmd-{uuid.uuid4().hex}.txt")
        self.file_stream = open(self.artifact_path, "wb")
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
            if self.spilled_to_disk:
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
