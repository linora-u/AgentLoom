from __future__ import annotations

import os
from pathlib import Path

import pytest

from src.lib.runtime import SecureDirectory


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
