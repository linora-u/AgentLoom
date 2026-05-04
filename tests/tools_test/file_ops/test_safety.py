"""Tests for the _safety module."""

import os
import unittest
import tempfile
from pathlib import Path

from src.tools.file_ops._safety import (
    BLOCKED_DEVICE_PATHS,
    DEFAULT_READ_LIMIT,
    MAX_EDIT_FILE_SIZE,
    MAX_READ_SIZE_BYTES,
    is_binary_file,
    is_device_file,
    normalize_path,
)


class TestIsDeviceFile(unittest.TestCase):
    """Device file detection tests."""

    def test_blocked_paths(self):
        for path in BLOCKED_DEVICE_PATHS:
            self.assertTrue(is_device_file(path), f"Expected {path} to be blocked")

    def test_proc_fd_pattern(self):
        self.assertTrue(is_device_file("/proc/1234/fd/0"))
        self.assertTrue(is_device_file("/proc/1/fd/1"))
        self.assertTrue(is_device_file("/proc/99/fd/2"))

    def test_normal_file_not_blocked(self):
        self.assertFalse(is_device_file("/tmp/hello.txt"))
        self.assertFalse(is_device_file("/home/user/code.py"))

    def test_proc_fd_non_stdio(self):
        """Non-stdio fd should not be blocked."""
        self.assertFalse(is_device_file("/proc/1234/fd/3"))
        self.assertFalse(is_device_file("/proc/1234/fd/100"))


class TestIsBinaryFile(unittest.TestCase):
    """Binary file detection tests."""

    def setUp(self):
        self.test_dir_obj = tempfile.TemporaryDirectory()
        self.test_dir = Path(self.test_dir_obj.name)

    def tearDown(self):
        self.test_dir_obj.cleanup()

    def test_elf_binary(self):
        fp = self.test_dir / "test.bin"
        fp.write_bytes(b"\x7fELF" + b"\x00" * 100)
        self.assertTrue(is_binary_file(fp))

    def test_zip_binary(self):
        fp = self.test_dir / "test.zip"
        fp.write_bytes(b"PK\x03\x04" + b"\x00" * 100)
        self.assertTrue(is_binary_file(fp))

    def test_png_binary(self):
        fp = self.test_dir / "image.png"
        fp.write_bytes(b"\x89PNG" + b"\x00" * 100)
        self.assertTrue(is_binary_file(fp))

    def test_text_file_not_binary(self):
        fp = self.test_dir / "text.txt"
        fp.write_text("Hello world\nThis is a text file.\n")
        self.assertFalse(is_binary_file(fp))

    def test_python_file_not_binary(self):
        fp = self.test_dir / "script.py"
        fp.write_text("#!/usr/bin/env python3\nprint('hello')\n")
        self.assertFalse(is_binary_file(fp))

    def test_binary_extension_fast_path(self):
        fp = self.test_dir / "test.exe"
        fp.write_bytes(b"not actually an exe but extension triggers")
        self.assertTrue(is_binary_file(fp))

    def test_nonexistent_file(self):
        self.assertFalse(is_binary_file("/nonexistent/file.bin"))

    def test_null_bytes_detection(self):
        """Files with null bytes in first 8KB should be detected as binary."""
        fp = self.test_dir / "nulls.dat"
        fp.write_bytes(b"some text\x00more text")
        self.assertTrue(is_binary_file(fp))


class TestNormalizePath(unittest.TestCase):
    """Path normalization tests."""

    def test_home_expansion(self):
        result = normalize_path("~/test.txt")
        self.assertTrue(result.is_absolute())
        self.assertNotIn("~", str(result))

    def test_relative_resolution(self):
        result = normalize_path("./test.txt")
        self.assertTrue(result.is_absolute())

    def test_absolute_unchanged(self):
        result = normalize_path("/tmp/test.txt")
        self.assertEqual(str(result), "/tmp/test.txt")

    def test_dotdot_resolution(self):
        result = normalize_path("/tmp/a/../b/test.txt")
        self.assertEqual(str(result), "/tmp/b/test.txt")


class TestConstants(unittest.TestCase):
    """Verify constant values."""

    def test_max_read_size(self):
        self.assertEqual(MAX_READ_SIZE_BYTES, 256 * 1024)

    def test_max_edit_file_size(self):
        self.assertEqual(MAX_EDIT_FILE_SIZE, 1 * 1024 * 1024 * 1024)

    def test_default_read_limit(self):
        self.assertEqual(DEFAULT_READ_LIMIT, 2000)


if __name__ == "__main__":
    unittest.main()
