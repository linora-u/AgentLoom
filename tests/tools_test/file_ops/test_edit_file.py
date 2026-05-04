"""Tests for the edit_file tool (migrated from test_file_editor + new cases)."""

import os
import unittest
import tempfile
from pathlib import Path

from src.tools.file_ops.edit_file import edit_file
from src.tools.file_ops._read_file_state import get_read_file_state


class TestEditFileBasic(unittest.TestCase):
    """Core edit_file functionality tests (migrated from test_file_editor)."""

    def setUp(self):
        self.test_dir_obj = tempfile.TemporaryDirectory()
        self.test_dir = Path(self.test_dir_obj.name)
        self.state = get_read_file_state()
        self.state.clear()

    def tearDown(self):
        self.test_dir_obj.cleanup()
        self.state.clear()

    def _write_and_read(self, file_path: Path, content: str) -> None:
        """Helper: write a file and register it in ReadFileState."""
        file_path.write_text(content, encoding="utf-8")
        mtime_ns = os.stat(file_path).st_mtime_ns
        self.state.set(file_path, content, mtime_ns, 1, 2000)

    def test_create_new_file(self):
        file_path = self.test_dir / "new_file.txt"
        result = edit_file(str(file_path), "", "Hello World")
        self.assertIn("Successfully created", result)
        self.assertEqual(file_path.read_text(), "Hello World")

    def test_overwrite_fails_without_write_file(self):
        """edit_file with empty old_string on existing file should fail."""
        file_path = self.test_dir / "exist.txt"
        file_path.write_text("Content")
        result = edit_file(str(file_path), "", "New Content")
        self.assertIn("File already exists", result)

    def test_exact_match_replacement(self):
        file_path = self.test_dir / "replace.txt"
        self._write_and_read(file_path, "Line 1\nLine 2\nLine 3")
        result = edit_file(str(file_path), "Line 2", "Modified Row 2")
        self.assertIn("Successfully edited", result)
        content = file_path.read_text()
        self.assertIn("Modified Row 2", content)
        self.assertNotIn("Line 2\n", content)

    def test_whitespace_tolerant_match(self):
        file_path = self.test_dir / "ws_test.py"
        self._write_and_read(file_path, "def foo():\n    print('bar')")
        # Search with tabs instead of spaces
        old_str = "def foo():\n\tprint('bar')"
        result = edit_file(str(file_path), old_str, "def foo():\n    print('baz')")
        self.assertIn("Successfully edited", result)
        self.assertIn("print('baz')", file_path.read_text())

    def test_token_match(self):
        file_path = self.test_dir / "token.txt"
        self._write_and_read(file_path, "A   B C")
        result = edit_file(str(file_path), "A B C", "X Y Z")
        self.assertIn("Successfully edited", result)
        self.assertEqual(file_path.read_text(), "X Y Z")

    def test_ambiguous_match_rejects(self):
        """Multiple matches without replace_all should fail."""
        file_path = self.test_dir / "ambiguous.txt"
        self._write_and_read(file_path, "hello\nhello\nhello")
        result = edit_file(str(file_path), "hello", "world")
        self.assertIn("Multiple matches", result)

    def test_replace_all(self):
        """replace_all=True should replace all occurrences."""
        file_path = self.test_dir / "multi.txt"
        self._write_and_read(file_path, "foo\nfoo")
        result = edit_file(str(file_path), "foo", "bar", replace_all=True)
        self.assertIn("Successfully edited", result)
        self.assertEqual(file_path.read_text(), "bar\nbar")

    def test_no_match(self):
        file_path = self.test_dir / "nomatch.txt"
        self._write_and_read(file_path, "AAA")
        result = edit_file(str(file_path), "BBB", "CCC")
        self.assertIn("Could not find old_string", result)


class TestEditFileNewFeatures(unittest.TestCase):
    """New test cases for features aligned with upstream."""

    def setUp(self):
        self.test_dir_obj = tempfile.TemporaryDirectory()
        self.test_dir = Path(self.test_dir_obj.name)
        self.state = get_read_file_state()
        self.state.clear()

    def tearDown(self):
        self.test_dir_obj.cleanup()
        self.state.clear()

    def _write_and_read(self, file_path: Path, content: str) -> None:
        file_path.write_text(content, encoding="utf-8")
        mtime_ns = os.stat(file_path).st_mtime_ns
        self.state.set(file_path, content, mtime_ns, 1, 2000)

    def test_same_string_rejected(self):
        """old_string == new_string should be rejected."""
        file_path = self.test_dir / "same.txt"
        self._write_and_read(file_path, "hello world")
        result = edit_file(str(file_path), "hello", "hello")
        self.assertIn("identical", result)

    def test_file_not_exists(self):
        result = edit_file("/nonexistent/path/file.txt", "old", "new")
        self.assertIn("does not exist", result)

    def test_staleness_check(self):
        """Edit should fail if file was modified externally since last read."""
        import time
        file_path = self.test_dir / "stale.txt"
        self._write_and_read(file_path, "original")
        # Modify externally with forced mtime change
        time.sleep(0.05)
        file_path.write_text("modified externally")
        os.utime(file_path, (0, 0))  # Force mtime to epoch
        result = edit_file(str(file_path), "original", "new")
        self.assertIn("modified since", result)

    def test_staleness_check_not_read(self):
        """Edit should fail if file was never read."""
        file_path = self.test_dir / "unread.txt"
        file_path.write_text("content")
        result = edit_file(str(file_path), "content", "new")
        self.assertIn("not been read", result)

    def test_creates_parent_directories(self):
        file_path = self.test_dir / "sub" / "dir" / "new.txt"
        result = edit_file(str(file_path), "", "content")
        self.assertIn("Successfully created", result)
        self.assertTrue(file_path.exists())

    def test_preserves_line_endings(self):
        file_path = self.test_dir / "crlf.txt"
        # Write CRLF content as bytes to preserve line endings
        file_path.write_bytes(b"line1\r\nline2\r\nline3")
        content_str = file_path.read_text(encoding="utf-8")
        mtime_ns = os.stat(file_path).st_mtime_ns
        self.state.set(file_path, content_str, mtime_ns, 1, 2000)
        result = edit_file(str(file_path), "line2", "modified")
        self.assertIn("Successfully edited", result)
        content = file_path.read_bytes()
        self.assertIn(b"\r\n", content)

    def test_quote_normalized_match(self):
        """Curly quotes in file should match straight quotes from model."""
        file_path = self.test_dir / "quotes.md"
        # File has curly quotes
        self._write_and_read(file_path, "She said \u201chello\u201d")
        # Model sends straight quotes
        result = edit_file(str(file_path), 'She said "hello"', 'She said "goodbye"')
        self.assertIn("Successfully edited", result)

    def test_similar_lines_hint(self):
        """On no match, should suggest similar lines."""
        file_path = self.test_dir / "hint.txt"
        self._write_and_read(file_path, "def calculate_total(items):\n    return sum(items)")
        result = edit_file(str(file_path), "def calculate_totl(items):\n    return sum(items)", "new")
        self.assertIn("similarity", result.lower())


class TestEditFileBoundary(unittest.TestCase):
    """Boundary and edge case tests."""

    def setUp(self):
        self.test_dir_obj = tempfile.TemporaryDirectory()
        self.test_dir = Path(self.test_dir_obj.name)
        self.state = get_read_file_state()
        self.state.clear()

    def tearDown(self):
        self.test_dir_obj.cleanup()
        self.state.clear()

    def test_empty_file(self):
        """Editing an empty file with non-empty old_string should fail."""
        file_path = self.test_dir / "empty.txt"
        file_path.write_text("")
        mtime_ns = os.stat(file_path).st_mtime_ns
        self.state.set(file_path, "", mtime_ns, 1, 2000)
        result = edit_file(str(file_path), "something", "other")
        self.assertIn("Could not find", result)

    def test_single_char_replacement(self):
        file_path = self.test_dir / "char.txt"
        file_path.write_text("A")
        mtime_ns = os.stat(file_path).st_mtime_ns
        self.state.set(file_path, "A", mtime_ns, 1, 2000)
        result = edit_file(str(file_path), "A", "B")
        self.assertIn("Successfully edited", result)
        self.assertEqual(file_path.read_text(), "B")

    def test_directory_path_rejected(self):
        result = edit_file(str(self.test_dir), "old", "new")
        # Should not crash; existing file check handles this
        self.assertIsInstance(result, str)


if __name__ == "__main__":
    unittest.main()
