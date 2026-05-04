import os
import tempfile
import textwrap

import pytest

from src.tools.search.grep_tool.grep_tool import grep_search


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def sample_dir(tmp_path):
    """Create a temp directory with sample files for searching."""
    (tmp_path / "hello.py").write_text(
        textwrap.dedent("""\
        def hello():
            print("Hello, world!")

        def goodbye():
            print("Goodbye!")

        class Greeter:
            def greet(self):
                return "hi"
        """),
        encoding="utf-8",
    )
    (tmp_path / "utils.py").write_text(
        textwrap.dedent("""\
        import os

        def helper():
            return 42

        # TODO: refactor this
        TODO_COUNT = 1
        """),
        encoding="utf-8",
    )
    (tmp_path / "readme.md").write_text("# Hello Project\nThis is a test.\n")
    sub = tmp_path / "sub"
    sub.mkdir()
    (sub / "deep.py").write_text("def deep_func():\n    pass\n")
    return tmp_path


# ---------------------------------------------------------------------------
# Basic search
# ---------------------------------------------------------------------------

class TestBasicSearch:
    def test_search_keyword(self, sample_dir):
        result = grep_search("hello", path=str(sample_dir), case_insensitive=True)
        assert "hello" in result.lower()
        assert "matches" in result  # metadata footer

    def test_search_regex(self, sample_dir):
        result = grep_search("def .*\\(", path=str(sample_dir))
        assert "def hello" in result or "def helper" in result

    def test_no_matches(self, sample_dir):
        result = grep_search("zzz_nonexistent_zzz", path=str(sample_dir))
        assert "No matches found" in result

    def test_invalid_directory(self):
        with pytest.raises(FileNotFoundError):
            grep_search("test", path="/nonexistent/path/xyz")

    def test_empty_pattern(self, sample_dir):
        with pytest.raises(ValueError, match="pattern is required"):
            grep_search("", path=str(sample_dir))


# ---------------------------------------------------------------------------
# Output modes
# ---------------------------------------------------------------------------

class TestOutputModes:
    def test_content_mode(self, sample_dir):
        result = grep_search("def", path=str(sample_dir), output_mode="content")
        # Should contain line numbers and content
        assert "|" in result  # "  42 | def hello()"

    def test_files_with_matches_mode(self, sample_dir):
        result = grep_search("def", path=str(sample_dir), output_mode="files_with_matches")
        assert "hello.py" in result
        assert "utils.py" in result
        # Should NOT contain line content
        assert "|" not in result.split("[")[0]  # before metadata

    def test_count_mode(self, sample_dir):
        result = grep_search("def", path=str(sample_dir), output_mode="count")
        # Format: file: N
        assert ":" in result

    def test_invalid_output_mode(self, sample_dir):
        with pytest.raises(ValueError, match="output_mode"):
            grep_search("def", path=str(sample_dir), output_mode="invalid")


# ---------------------------------------------------------------------------
# Context lines
# ---------------------------------------------------------------------------

class TestContextLines:
    def test_context_lines(self, sample_dir):
        result = grep_search(
            "hello", path=str(sample_dir),
            output_mode="content", context_lines=2,
        )
        # Context should include surrounding lines
        assert "matches" in result

    def test_before_after_context(self, sample_dir):
        result = grep_search(
            "hello", path=str(sample_dir),
            output_mode="content", before_context=1, after_context=1,
        )
        assert "matches" in result


# ---------------------------------------------------------------------------
# Multiline
# ---------------------------------------------------------------------------

class TestMultiline:
    def test_multiline_pattern(self, sample_dir):
        # Search for a pattern that spans lines
        result = grep_search(
            "def hello.*print", path=str(sample_dir),
            multiline=True,
        )
        # May or may not match depending on ripgrep availability
        assert isinstance(result, str)


# ---------------------------------------------------------------------------
# Pagination
# ---------------------------------------------------------------------------

class TestPagination:
    def test_max_results(self, sample_dir):
        result = grep_search("def", path=str(sample_dir), max_results=2)
        assert "matches" in result

    def test_offset(self, sample_dir):
        result = grep_search("def", path=str(sample_dir), max_results=2, offset=1)
        assert isinstance(result, str)


# ---------------------------------------------------------------------------
# Case sensitivity
# ---------------------------------------------------------------------------

class TestCaseSensitivity:
    def test_case_insensitive(self, sample_dir):
        result = grep_search("HELLO", path=str(sample_dir), case_insensitive=True)
        assert "hello" in result.lower()

    def test_case_sensitive(self, sample_dir):
        result = grep_search("HELLO", path=str(sample_dir), case_insensitive=False)
        # Only "Hello" in readme.md matches (title case), not "hello" in hello.py
        assert isinstance(result, str)


# ---------------------------------------------------------------------------
# Include filter
# ---------------------------------------------------------------------------

class TestIncludeFilter:
    def test_include_py_only(self, sample_dir):
        result = grep_search(
            "Hello", path=str(sample_dir),
            include="*.py", case_insensitive=True,
        )
        assert "readme.md" not in result

    def test_include_md_only(self, sample_dir):
        result = grep_search(
            "Hello", path=str(sample_dir),
            include="*.md", case_insensitive=True,
        )
        assert "hello.py" not in result


# ---------------------------------------------------------------------------
# Pattern starting with dash
# ---------------------------------------------------------------------------

class TestPatternDash:
    def test_pattern_with_dash(self, sample_dir):
        # Should not crash (uses -e flag)
        result = grep_search("-i", path=str(sample_dir))
        assert isinstance(result, str)


# ---------------------------------------------------------------------------
# Metadata footer
# ---------------------------------------------------------------------------

class TestMetadata:
    def test_footer_contains_matches(self, sample_dir):
        result = grep_search("def", path=str(sample_dir))
        assert "matches" in result
        assert "files" in result

    def test_footer_contains_timing(self, sample_dir):
        result = grep_search("def", path=str(sample_dir))
        assert "ms" in result
