"""Tests for the read_file tool."""

import os
import unittest
import tempfile
from pathlib import Path

from src.tools.file_ops.read_file import read_file
from src.tools.file_ops._read_file_state import get_read_file_state, FILE_UNCHANGED_STUB


class TestReadFileBasic(unittest.TestCase):
    """Normal functionality path tests."""

    def setUp(self):
        self.test_dir_obj = tempfile.TemporaryDirectory()
        self.test_dir = Path(self.test_dir_obj.name)
        self.state = get_read_file_state()
        self.state.clear()

    def tearDown(self):
        self.test_dir_obj.cleanup()
        self.state.clear()

    def test_read_small_file(self):
        fp = self.test_dir / "hello.txt"
        fp.write_text("line one\nline two\nline three\n")
        result = read_file(str(fp))
        # Should have cat -n line numbers
        self.assertIn("1\t", result)
        self.assertIn("line one", result)
        self.assertIn("line two", result)
        self.assertIn("line three", result)
        # Footer with total lines
        self.assertIn("Total lines: 3", result)

    def test_read_with_offset_limit(self):
        fp = self.test_dir / "lines.txt"
        fp.write_text("\n".join(f"line {i}" for i in range(1, 11)))
        result = read_file(str(fp), offset=3, limit=2)
        self.assertIn("line 3", result)
        self.assertIn("line 4", result)
        self.assertNotIn("line 5", result)
        self.assertIn("Showing: 3-4", result)

    def test_read_offset_beyond_end(self):
        fp = self.test_dir / "short.txt"
        fp.write_text("only one line")
        result = read_file(str(fp), offset=100)
        self.assertIn("beyond the end", result)

    def test_line_number_format(self):
        """Lines should be in cat -n format: number + tab."""
        fp = self.test_dir / "fmt.txt"
        fp.write_text("hello\nworld\n")
        result = read_file(str(fp))
        lines = result.strip().split("\n")
        # First content line should contain line number and tab
        self.assertRegex(lines[0], r"^\s*1\t")

    def test_default_limit_2000(self):
        """Default should read up to 2000 lines."""
        fp = self.test_dir / "big.txt"
        fp.write_text("\n".join(f"line {i}" for i in range(1, 2500)))
        result = read_file(str(fp))
        self.assertIn("Truncated", result)
        self.assertIn("line 1", result)
        self.assertNotIn("line 2001", result)

    def test_result_content_valid(self):
        """Verify actual content is returned, not empty."""
        fp = self.test_dir / "content.py"
        fp.write_text("def hello():\n    return 'world'\n")
        result = read_file(str(fp))
        self.assertIn("def hello():", result)
        self.assertIn("return 'world'", result)
        self.assertGreater(len(result), 20)


class TestReadFileDedup(unittest.TestCase):
    """ReadFileState deduplication tests."""

    def setUp(self):
        self.test_dir_obj = tempfile.TemporaryDirectory()
        self.test_dir = Path(self.test_dir_obj.name)
        self.state = get_read_file_state()
        self.state.clear()

    def tearDown(self):
        self.test_dir_obj.cleanup()
        self.state.clear()

    def test_second_read_returns_stub(self):
        """Second identical read should return dedup stub."""
        fp = self.test_dir / "dedup.txt"
        fp.write_text("content here\n")
        result1 = read_file(str(fp))
        self.assertIn("content here", result1)
        result2 = read_file(str(fp))
        self.assertEqual(result2, FILE_UNCHANGED_STUB)

    def test_modified_file_no_dedup(self):
        """If file is modified between reads, dedup should not trigger."""
        import time
        fp = self.test_dir / "changing.txt"
        fp.write_text("version 1\n")
        read_file(str(fp))
        # Ensure mtime changes (filesystem might have 1s resolution)
        time.sleep(0.05)
        fp.write_text("version 2\n")
        # Force mtime to be different
        import os
        os.utime(fp, (0, 0))
        result2 = read_file(str(fp))
        self.assertIn("version 2", result2)
        self.assertNotEqual(result2, FILE_UNCHANGED_STUB)

    def test_different_range_no_dedup(self):
        """Different offset/limit should not trigger dedup."""
        fp = self.test_dir / "ranges.txt"
        fp.write_text("\n".join(f"line {i}" for i in range(100)))
        read_file(str(fp), offset=1, limit=10)
        result2 = read_file(str(fp), offset=50, limit=10)
        self.assertNotEqual(result2, FILE_UNCHANGED_STUB)
        self.assertIn("line 50", result2)


class TestReadFileErrors(unittest.TestCase):
    """Error and boundary condition tests."""

    def setUp(self):
        self.test_dir_obj = tempfile.TemporaryDirectory()
        self.test_dir = Path(self.test_dir_obj.name)
        self.state = get_read_file_state()
        self.state.clear()

    def tearDown(self):
        self.test_dir_obj.cleanup()
        self.state.clear()

    def test_file_not_found(self):
        with self.assertRaises(FileNotFoundError):
            read_file("/nonexistent/path/file.txt")

    def test_directory_rejected(self):
        with self.assertRaises(ValueError) as ctx:
            read_file(str(self.test_dir))
        self.assertIn("directory", str(ctx.exception).lower())

    def test_empty_path_rejected(self):
        with self.assertRaises(ValueError):
            read_file("")

    def test_invalid_offset(self):
        with self.assertRaises(ValueError):
            read_file("/tmp/any.txt", offset=0)

    def test_invalid_limit(self):
        with self.assertRaises(ValueError):
            read_file("/tmp/any.txt", limit=-1)

    def test_binary_file_rejected(self):
        fp = self.test_dir / "binary.exe"
        fp.write_bytes(b"\x7fELF" + b"\x00" * 100)
        with self.assertRaises(ValueError) as ctx:
            read_file(str(fp))
        self.assertIn("binary", str(ctx.exception).lower())

    def test_empty_file(self):
        """Empty file should return metadata about being empty."""
        fp = self.test_dir / "empty.txt"
        fp.write_text("")
        result = read_file(str(fp))
        self.assertIn("0", result)  # total lines = 0 or empty indicator


class TestReadFileWhitespace(unittest.TestCase):
    """Tests for file_path whitespace handling (fix for LLM-generated paths)."""

    def setUp(self):
        self.test_dir_obj = tempfile.TemporaryDirectory()
        self.test_dir = Path(self.test_dir_obj.name)
        self.state = get_read_file_state()
        self.state.clear()

    def tearDown(self):
        self.test_dir_obj.cleanup()
        self.state.clear()

    def test_leading_space_in_path(self):
        """read_file should strip leading whitespace from file_path."""
        fp = self.test_dir / "test.txt"
        fp.write_text("hello world\n")
        # Simulate LLM passing path with leading space
        result = read_file(" " + str(fp))
        self.assertIn("hello world", result)

    def test_trailing_space_in_path(self):
        """read_file should strip trailing whitespace from file_path."""
        fp = self.test_dir / "test.txt"
        fp.write_text("hello world\n")
        result = read_file(str(fp) + "  ")
        self.assertIn("hello world", result)

    def test_leading_and_trailing_spaces(self):
        """read_file should handle both leading and trailing spaces."""
        fp = self.test_dir / "test.txt"
        fp.write_text("content here\n")
        result = read_file("  " + str(fp) + "  ")
        self.assertIn("content here", result)

    def test_whitespace_only_path_rejected(self):
        """A path of only whitespace should raise ValueError."""
        with self.assertRaises(ValueError):
            read_file("   ")


if __name__ == "__main__":
    unittest.main()
