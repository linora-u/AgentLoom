"""Tests for the multi-edit edit_file tool."""

import os
import tempfile
import time
import unittest
from pathlib import Path

from src.tools.file_ops._read_file_state import get_read_file_state
from src.tools.file_ops.edit_file import edit_file


class EditFileTestCase(unittest.TestCase):
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


class TestEditFileBasic(EditFileTestCase):
    def test_exact_match_replacement(self):
        file_path = self.test_dir / "replace.txt"
        self._write_and_read(file_path, "Line 1\nLine 2\nLine 3")
        result = edit_file(
            str(file_path),
            [{"old_text": "Line 2", "new_text": "Modified Row 2"}],
        )
        self.assertIn("Successfully edited", result)
        self.assertIn("Modified Row 2", file_path.read_text())
        self.assertNotIn("Line 2\n", file_path.read_text())

    def test_multiple_edits_apply_against_original_file(self):
        file_path = self.test_dir / "multi.txt"
        self._write_and_read(file_path, "alpha\nbeta\ngamma\n")
        result = edit_file(
            str(file_path),
            [
                {"old_text": "alpha", "new_text": "ALPHA"},
                {"old_text": "gamma", "new_text": "GAMMA"},
            ],
        )
        self.assertIn("(2 edits)", result)
        self.assertEqual(file_path.read_text(), "ALPHA\nbeta\nGAMMA\n")

    def test_whitespace_tolerant_match(self):
        file_path = self.test_dir / "ws_test.py"
        self._write_and_read(file_path, "def foo():\n    print('bar')")
        result = edit_file(
            str(file_path),
            [{"old_text": "def foo():\n\tprint('bar')", "new_text": "def foo():\n    print('baz')"}],
        )
        self.assertIn("Successfully edited", result)
        self.assertIn("whitespace-tolerant", result)
        self.assertIn("print('baz')", file_path.read_text())

    def test_token_match(self):
        file_path = self.test_dir / "token.txt"
        self._write_and_read(file_path, "A   B C")
        result = edit_file(str(file_path), [{"old_text": "A B C", "new_text": "X Y Z"}])
        self.assertIn("Successfully edited", result)
        self.assertEqual(file_path.read_text(), "X Y Z")

    def test_quote_normalized_match(self):
        file_path = self.test_dir / "quotes.md"
        self._write_and_read(file_path, "She said \u201chello\u201d")
        result = edit_file(
            str(file_path),
            [{"old_text": 'She said "hello"', "new_text": 'She said "goodbye"'}],
        )
        self.assertIn("Successfully edited", result)


class TestEditFileValidation(EditFileTestCase):
    def test_empty_edits_rejected(self):
        file_path = self.test_dir / "empty-edits.txt"
        self._write_and_read(file_path, "hello")
        result = edit_file(str(file_path), [])
        self.assertIn("edits must be a non-empty list", result)

    def test_duplicate_old_text_rejected(self):
        file_path = self.test_dir / "duplicate.txt"
        self._write_and_read(file_path, "a\nb\n")
        result = edit_file(
            str(file_path),
            [
                {"old_text": "a", "new_text": "A"},
                {"old_text": "a", "new_text": "AA"},
            ],
        )
        self.assertIn("Duplicate old_text", result)

    def test_overlapping_edits_rejected(self):
        file_path = self.test_dir / "overlap.txt"
        self._write_and_read(file_path, "abc")
        result = edit_file(
            str(file_path),
            [
                {"old_text": "ab", "new_text": "AB"},
                {"old_text": "bc", "new_text": "BC"},
            ],
        )
        self.assertIn("Overlapping edits", result)

    def test_ambiguous_match_rejects(self):
        file_path = self.test_dir / "ambiguous.txt"
        self._write_and_read(file_path, "hello\nhello\nhello")
        result = edit_file(str(file_path), [{"old_text": "hello", "new_text": "world"}])
        self.assertIn("Multiple exact matches", result)

    def test_no_match(self):
        file_path = self.test_dir / "nomatch.txt"
        self._write_and_read(file_path, "AAA")
        result = edit_file(str(file_path), [{"old_text": "BBB", "new_text": "CCC"}])
        self.assertIn("Could not find old_text", result)

    def test_same_string_rejected(self):
        file_path = self.test_dir / "same.txt"
        self._write_and_read(file_path, "hello world")
        result = edit_file(str(file_path), [{"old_text": "hello", "new_text": "hello"}])
        self.assertIn("identical", result)

    def test_file_not_exists(self):
        result = edit_file("/nonexistent/path/file.txt", [{"old_text": "old", "new_text": "new"}])
        self.assertIn("does not exist", result)

    def test_directory_path_rejected(self):
        result = edit_file(str(self.test_dir), [{"old_text": "old", "new_text": "new"}])
        self.assertIn("directory", result)


class TestEditFileStateAndEncoding(EditFileTestCase):
    def test_staleness_check(self):
        file_path = self.test_dir / "stale.txt"
        self._write_and_read(file_path, "original")
        time.sleep(0.05)
        file_path.write_text("modified externally")
        os.utime(file_path, (0, 0))
        result = edit_file(str(file_path), [{"old_text": "original", "new_text": "new"}])
        self.assertIn("modified since", result)

    def test_staleness_check_not_read(self):
        file_path = self.test_dir / "unread.txt"
        file_path.write_text("content")
        result = edit_file(str(file_path), [{"old_text": "content", "new_text": "new"}])
        self.assertIn("not been read", result)

    def test_preserves_line_endings(self):
        file_path = self.test_dir / "crlf.txt"
        file_path.write_bytes(b"line1\r\nline2\r\nline3")
        content_str = file_path.read_text(encoding="utf-8")
        self.state.set(file_path, content_str, os.stat(file_path).st_mtime_ns, 1, 2000)
        result = edit_file(str(file_path), [{"old_text": "line2", "new_text": "modified"}])
        self.assertIn("Successfully edited", result)
        self.assertIn(b"\r\n", file_path.read_bytes())


if __name__ == "__main__":
    unittest.main()
