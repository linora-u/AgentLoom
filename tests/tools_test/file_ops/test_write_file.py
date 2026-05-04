"""Tests for the write_file tool."""

import os
import unittest
import tempfile
from pathlib import Path

from src.tools.file_ops.write_file import write_file
from src.tools.file_ops._read_file_state import get_read_file_state


class TestWriteFileBasic(unittest.TestCase):
    """Normal functionality path tests."""

    def setUp(self):
        self.test_dir_obj = tempfile.TemporaryDirectory()
        self.test_dir = Path(self.test_dir_obj.name)
        self.state = get_read_file_state()
        self.state.clear()

    def tearDown(self):
        self.test_dir_obj.cleanup()
        self.state.clear()

    def test_create_new_file(self):
        fp = self.test_dir / "new.txt"
        result = write_file(str(fp), "hello world")
        self.assertIn("Created", result)
        self.assertEqual(fp.read_text(), "hello world")
        self.assertIn("11 chars", result)

    def test_overwrite_existing_file(self):
        fp = self.test_dir / "exist.txt"
        fp.write_text("old content")
        mtime_ns = os.stat(fp).st_mtime_ns
        self.state.set(fp, "old content", mtime_ns, 1, 2000)
        result = write_file(str(fp), "new content")
        self.assertIn("Updated", result)
        self.assertEqual(fp.read_text(), "new content")

    def test_creates_parent_directories(self):
        fp = self.test_dir / "a" / "b" / "c" / "file.txt"
        result = write_file(str(fp), "nested")
        self.assertIn("Created", result)
        self.assertTrue(fp.exists())
        self.assertEqual(fp.read_text(), "nested")

    def test_preserves_crlf_line_endings(self):
        """When overwriting a CRLF file, preserve line endings."""
        fp = self.test_dir / "crlf.txt"
        fp.write_bytes(b"line1\r\nline2\r\n")
        # Read the file as text for the state cache (Python reads \r\n as \n)
        content_str = fp.read_text(encoding="utf-8")
        mtime_ns = os.stat(fp).st_mtime_ns
        self.state.set(fp, content_str, mtime_ns, 1, 2000)
        result = write_file(str(fp), "new1\nnew2\n")
        self.assertIn("Updated", result)
        raw = fp.read_bytes()
        self.assertIn(b"\r\n", raw)

    def test_result_includes_char_count(self):
        fp = self.test_dir / "count.txt"
        result = write_file(str(fp), "12345")
        self.assertIn("5 chars", result)

    def test_write_empty_content(self):
        fp = self.test_dir / "empty.txt"
        result = write_file(str(fp), "")
        self.assertIn("Created", result)
        self.assertEqual(fp.read_text(), "")


class TestWriteFileErrors(unittest.TestCase):
    """Error and boundary condition tests."""

    def setUp(self):
        self.test_dir_obj = tempfile.TemporaryDirectory()
        self.test_dir = Path(self.test_dir_obj.name)
        self.state = get_read_file_state()
        self.state.clear()

    def tearDown(self):
        self.test_dir_obj.cleanup()
        self.state.clear()

    def test_empty_path_raises(self):
        with self.assertRaises(ValueError):
            write_file("", "content")

    def test_none_content_raises(self):
        with self.assertRaises(ValueError):
            write_file("/tmp/test.txt", None)

    def test_directory_path_raises(self):
        with self.assertRaises(ValueError):
            write_file(str(self.test_dir), "content")

    def test_staleness_check_on_existing(self):
        """Overwriting without reading first should fail."""
        fp = self.test_dir / "unread.txt"
        fp.write_text("existing content")
        result = write_file(str(fp), "new content")
        self.assertIn("not been read", result)

    def test_staleness_externally_modified(self):
        """Overwriting externally modified file should fail."""
        import time
        fp = self.test_dir / "stale.txt"
        fp.write_text("original")
        mtime_ns = os.stat(fp).st_mtime_ns
        self.state.set(fp, "original", mtime_ns, 1, 2000)
        # External modification with forced mtime change
        time.sleep(0.05)
        fp.write_text("externally modified")
        os.utime(fp, (0, 0))  # Force mtime to epoch
        result = write_file(str(fp), "my version")
        self.assertIn("modified since", result)


class TestWriteFileStalenessIntegration(unittest.TestCase):
    """Integration: read_file then write_file should work."""

    def setUp(self):
        self.test_dir_obj = tempfile.TemporaryDirectory()
        self.test_dir = Path(self.test_dir_obj.name)
        self.state = get_read_file_state()
        self.state.clear()

    def tearDown(self):
        self.test_dir_obj.cleanup()
        self.state.clear()

    def test_read_then_write_succeeds(self):
        """Full flow: read file, then write should succeed."""
        from src.tools.file_ops.read_file import read_file
        fp = self.test_dir / "flow.txt"
        fp.write_text("original content\n")
        read_result = read_file(str(fp))
        self.assertIn("original content", read_result)
        write_result = write_file(str(fp), "replaced content\n")
        self.assertIn("Updated", write_result)
        self.assertEqual(fp.read_text(), "replaced content\n")


if __name__ == "__main__":
    unittest.main()
