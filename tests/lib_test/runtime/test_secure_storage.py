from __future__ import annotations

import os
import stat
from pathlib import Path

import pytest

from src.lib.runtime import SecureDirectory


def test_atomic_write_fsyncs_file_and_parent_directory(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    storage = SecureDirectory(tmp_path / "state")
    real_fsync = os.fsync
    synced_kinds: list[str] = []

    def record_fsync(fd: int) -> None:
        mode = os.fstat(fd).st_mode
        synced_kinds.append("directory" if stat.S_ISDIR(mode) else "file")
        real_fsync(fd)

    monkeypatch.setattr(os, "fsync", record_fsync)
    try:
        storage.atomic_write_text("jobs.json", '{"version": 1}\n')
    finally:
        storage.close()

    assert synced_kinds == ["file", "directory"]


def test_append_text_retries_regular_file_short_writes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    storage = SecureDirectory(tmp_path / "state")
    real_write = os.write

    def short_write(fd: int, payload) -> int:
        limit = max(1, len(payload) // 2)
        return real_write(fd, payload[:limit])

    monkeypatch.setattr(os, "write", short_write)
    try:
        storage.append_text("events.jsonl", "0123456789\n")
    finally:
        storage.close()

    assert (tmp_path / "state" / "events.jsonl").read_text(encoding="utf-8") == "0123456789\n"


def test_append_text_rejects_zero_byte_write(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    storage = SecureDirectory(tmp_path / "state")
    monkeypatch.setattr(os, "write", lambda _fd, _payload: 0)
    try:
        with pytest.raises(OSError, match="short write"):
            storage.append_text("events.jsonl", "event\n")
    finally:
        storage.close()


def test_copy_streams_large_files_and_retries_short_writes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "source.bin"
    payload = (b"0123456789abcdef" * (200 * 1024)) + b"tail"
    source.write_bytes(payload)
    source.chmod(0o751)
    expected_mtime_ns = 1_700_000_000_123_456_789
    os.utime(source, ns=(expected_mtime_ns, expected_mtime_ns))
    storage = SecureDirectory(tmp_path / "state")
    real_write = os.write

    def short_write(fd: int, data) -> int:
        return real_write(fd, data[: max(1, len(data) // 3)])

    monkeypatch.setattr(os, "write", short_write)
    try:
        storage.copy_from(source, "history/backup.bin")
        restored = tmp_path / "restored.bin"
        storage.copy_to("history/backup.bin", restored)
        assert storage.same_content_as("history/backup.bin", source)
    finally:
        storage.close()

    assert restored.read_bytes() == payload
    assert restored.stat().st_mode & 0o777 == 0o751
    assert restored.stat().st_mtime_ns == expected_mtime_ns


def test_copy_rejects_zero_byte_write_without_publishing_partial_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "source.bin"
    source.write_bytes(b"new payload")
    storage = SecureDirectory(tmp_path / "state")
    monkeypatch.setattr(os, "write", lambda _fd, _payload: 0)
    try:
        with pytest.raises(OSError, match="short write"):
            storage.copy_from(source, "backup.bin")
        assert not (tmp_path / "state" / "backup.bin").exists()
        assert not list((tmp_path / "state").glob(".*.tmp"))
    finally:
        storage.close()


def test_advisory_file_lock_supports_nonblocking_lock_modes(tmp_path: Path) -> None:
    first = SecureDirectory(tmp_path / "state")
    second = SecureDirectory(tmp_path / "state")
    try:
        with first.advisory_file_lock("state.lock", create=True):
            with pytest.raises(BlockingIOError):
                with second.advisory_file_lock(
                    "state.lock",
                    exclusive=True,
                    blocking=False,
                ):
                    pass
    finally:
        first.close()
        second.close()


def test_open_binary_writer_exclusively_creates_and_rejects_symlink_target(
    tmp_path: Path,
) -> None:
    storage = SecureDirectory(tmp_path / "state")
    outside = tmp_path / "outside.log"
    outside.write_bytes(b"sentinel")
    (tmp_path / "state" / "unsafe.log").symlink_to(outside)
    try:
        with storage.open_binary_writer("safe.log", exclusive=True) as handle:
            handle.write(b"safe")
        with pytest.raises(OSError):
            with storage.open_binary_writer("unsafe.log", exclusive=True):
                pass
    finally:
        storage.close()

    assert (tmp_path / "state" / "safe.log").read_bytes() == b"safe"
    assert outside.read_bytes() == b"sentinel"
