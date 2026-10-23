"""File rewind remains useful inside the Harness metadata-denial boundary."""
import os
import stat

from agentloom.execution.checkpoint.file_history import FileHistoryManager
from agentloom.execution.storage import bind_file_copy_metadata


def test_content_only_history_restores_bytes_and_creation_mode(tmp_path):
    target = tmp_path / "skill.py"
    target.write_bytes(b"original\x00bytes\n")
    target.chmod(0o755)
    with bind_file_copy_metadata(False):
        history = FileHistoryManager(tmp_path / "history")
    history.track_edit(str(target), 1)
    target.write_text("changed")
    assert history.rewind_to_step(1) == [str(target)]
    assert target.read_bytes() == b"original\x00bytes\n"
    assert stat.S_IMODE(target.stat().st_mode) == 0o755


def test_ordinary_history_retains_copy2_timestamps(tmp_path):
    target = tmp_path / "prompt.md"
    target.write_text("original")
    stamp = 1_700_000_000_123_456_789
    os.utime(target, ns=(stamp, stamp))
    history = FileHistoryManager(tmp_path / "history")
    history.track_edit(str(target), 1)
    target.write_text("changed")
    assert history.rewind_to_step(1) == [str(target)]
    assert target.read_text() == "original"
    assert target.stat().st_mtime_ns == stamp
