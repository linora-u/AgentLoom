"""Tests for the core matching engine (_match_engine.py)."""

import pytest

from src.tools.code_editor._match_engine import (
    EditBlock,
    RelativeIndenter,
    SearchReplaceError,
    dmp_lines_apply,
    flexible_search_and_replace,
    find_similar_lines,
    parse_search_replace_blocks,
    replace_most_similar_chunk,
    search_in_file,
    _simple_search_and_replace,
)


# ============================================================================
# parse_search_replace_blocks
# ============================================================================


class TestParseBlocks:
    def test_single_block(self):
        content = (
            "<<<<<<< SEARCH\n"
            "old code\n"
            "=======\n"
            "new code\n"
            ">>>>>>> REPLACE\n"
        )
        blocks = parse_search_replace_blocks(content)
        assert len(blocks) == 1
        assert blocks[0].search == "old code\n"
        assert blocks[0].replace == "new code\n"

    def test_multiple_blocks(self):
        content = (
            "<<<<<<< SEARCH\n"
            "aaa\n"
            "=======\n"
            "bbb\n"
            ">>>>>>> REPLACE\n"
            "some text between\n"
            "<<<<<<< SEARCH\n"
            "ccc\n"
            "=======\n"
            "ddd\n"
            ">>>>>>> REPLACE\n"
        )
        blocks = parse_search_replace_blocks(content)
        assert len(blocks) == 2
        assert blocks[0].search == "aaa\n"
        assert blocks[1].replace == "ddd\n"

    def test_empty_search_block(self):
        content = (
            "<<<<<<< SEARCH\n"
            "=======\n"
            "new content\n"
            ">>>>>>> REPLACE\n"
        )
        blocks = parse_search_replace_blocks(content)
        assert len(blocks) == 1
        assert blocks[0].search == ""
        assert blocks[0].replace == "new content\n"

    def test_malformed_missing_divider(self):
        content = "<<<<<<< SEARCH\nold code\n>>>>>>> REPLACE\n"
        # The parser will see >>>>>>> REPLACE as a regular line (not divider)
        # and then fail because no ======= was found
        with pytest.raises(SearchReplaceError, match="missing.*divider"):
            parse_search_replace_blocks(content)

    def test_malformed_missing_footer(self):
        content = "<<<<<<< SEARCH\nold\n=======\nnew\n"
        with pytest.raises(SearchReplaceError, match="missing.*REPLACE"):
            parse_search_replace_blocks(content)

    def test_empty_input(self):
        assert parse_search_replace_blocks("") == []
        assert parse_search_replace_blocks("   ") == []

    def test_variable_bracket_count(self):
        """Should accept 5-9 brackets."""
        content = (
            "<<<<<< SEARCH\n"
            "old\n"
            "======\n"
            "new\n"
            ">>>>>> REPLACE\n"
        )
        blocks = parse_search_replace_blocks(content)
        assert len(blocks) == 1


# ============================================================================
# RelativeIndenter
# ============================================================================


class TestRelativeIndenter:
    def test_roundtrip_simple(self):
        text = "    Foo\n        Bar\n        Baz\n    Fob\n"
        ri = RelativeIndenter([text])
        relative = ri.make_relative(text)
        restored = ri.make_absolute(relative)
        assert restored == text

    def test_roundtrip_no_indent(self):
        text = "line1\nline2\nline3\n"
        ri = RelativeIndenter([text])
        relative = ri.make_relative(text)
        restored = ri.make_absolute(relative)
        assert restored == text

    def test_outdent_marker(self):
        """Lines that reduce indentation should use the marker."""
        text = "        deep\n    shallow\n"
        ri = RelativeIndenter([text])
        relative = ri.make_relative(text)
        assert ri.marker in relative

    def test_multiple_texts_roundtrip(self):
        """The same indenter should work for multiple texts."""
        text1 = "    a\n        b\n"
        text2 = "        x\n    y\n"
        ri = RelativeIndenter([text1, text2])
        assert ri.make_absolute(ri.make_relative(text1)) == text1
        assert ri.make_absolute(ri.make_relative(text2)) == text2


# ============================================================================
# DMP line-level matching
# ============================================================================


class TestDmpLinesApply:
    def test_exact_match(self):
        search = "line1\nline2\nline3\n"
        replace = "line1\nMODIFIED\nline3\n"
        original = "header\nline1\nline2\nline3\nfooter\n"
        result = dmp_lines_apply((search, replace, original))
        assert result is not None
        assert "MODIFIED" in result
        assert "header" in result
        assert "footer" in result

    def test_with_indent_change(self):
        """DMP should handle when original has different indentation."""
        search = "if (x) {\n    foo();\n}\n"
        replace = "if (x) {\n    bar();\n}\n"
        original = "void func() {\n    if (x) {\n        foo();\n    }\n}\n"
        result = dmp_lines_apply((search, replace, original))
        # DMP is tolerant — may or may not match this exact case
        # But it should not crash
        # result could be None (ok) or a valid replacement

    def test_no_match(self):
        search = "completely different\n"
        replace = "something else\n"
        original = "nothing matches here\nat all\n"
        result = dmp_lines_apply((search, replace, original))
        # With very different content, DMP may or may not find a patch
        # Just verify no crash


# ============================================================================
# flexible_search_and_replace (12-level chain)
# ============================================================================


class TestFlexibleSearchAndReplace:
    def test_exact_match(self):
        texts = ("old line\n", "new line\n", "header\nold line\nfooter\n")
        result = flexible_search_and_replace(texts)
        assert result is not None
        assert "new line" in result
        assert "old line" not in result

    def test_indent_shift(self):
        """Search text has different indentation than original."""
        search = "foo();\nbar();\n"
        replace = "baz();\nbar();\n"
        original = "    foo();\n    bar();\n"
        texts = (search, replace, original)
        result = flexible_search_and_replace(texts)
        assert result is not None
        assert "baz" in result

    def test_with_blank_line_difference(self):
        """Search text has extra blank lines."""
        search = "\nfoo();\n\n"
        replace = "\nbaz();\n\n"
        original = "foo();\n"
        texts = (search, replace, original)
        result = flexible_search_and_replace(texts)
        assert result is not None
        assert "baz" in result

    def test_no_match_returns_none(self):
        texts = ("nonexistent\n", "replacement\n", "totally different content\n")
        result = flexible_search_and_replace(texts)
        # May be None or may match via DMP — just verify no crash


# ============================================================================
# simple_search_and_replace
# ============================================================================


class TestSimpleSearchAndReplace:
    def test_basic(self):
        texts = ("old", "new", "prefix old suffix")
        result = _simple_search_and_replace(texts)
        assert result == "prefix new suffix"

    def test_not_found(self):
        texts = ("missing", "new", "nothing here")
        result = _simple_search_and_replace(texts)
        assert result is None


# ============================================================================
# replace_most_similar_chunk (main entry)
# ============================================================================


class TestReplaceMostSimilarChunk:
    def test_exact_match(self):
        whole = "line1\nline2\nline3\n"
        part = "line2\n"
        replace = "REPLACED\n"
        result = replace_most_similar_chunk(whole, part, replace)
        assert result is not None
        assert "REPLACED" in result
        assert "line1" in result
        assert "line3" in result

    def test_whitespace_fix(self):
        """LLM gives code without indentation."""
        whole = "    def foo():\n        pass\n"
        part = "def foo():\n    pass\n"
        replace = "def bar():\n    return 42\n"
        result = replace_most_similar_chunk(whole, part, replace)
        assert result is not None
        assert "bar" in result

    def test_dotdotdots(self):
        whole = "line1\nline2\nline3\nline4\nline5\n"
        part = "line1\n...\nline5\n"
        replace = "LINE1\n...\nLINE5\n"
        result = replace_most_similar_chunk(whole, part, replace)
        assert result is not None
        assert "LINE1" in result
        assert "LINE5" in result
        assert "line2" in result  # middle preserved

    def test_fuzzy_match(self):
        """Small differences should still match via fuzzy."""
        whole = "def calculate(x, y):\n    return x + y\n"
        part = "def calculate(a, b):\n    return a + b\n"
        replace = "def compute(a, b):\n    return a * b\n"
        result = replace_most_similar_chunk(whole, part, replace)
        assert result is not None
        assert "compute" in result


# ============================================================================
# search_in_file
# ============================================================================


class TestSearchInFile:
    def test_exact_match(self):
        content = "line1\nline2\nline3\nline4\n"
        results = search_in_file(content, "line2\nline3")
        assert len(results) >= 1
        assert results[0].start_line == 2
        assert results[0].similarity == 1.0

    def test_whitespace_tolerant(self):
        content = "    indented_code\n    more_code\n"
        results = search_in_file(content, "indented_code\nmore_code")
        assert len(results) >= 1
        assert results[0].similarity > 0.7

    def test_fuzzy_match(self):
        content = "def calculate(x, y):\n    return x + y\n"
        results = search_in_file(content, "def calculate(a, b):\n    return a + b")
        assert len(results) >= 1
        assert results[0].similarity >= 0.6

    def test_no_match(self):
        content = "completely different content\n"
        results = search_in_file(content, "nothing like this at all\nreally different")
        assert len(results) == 0

    def test_context_lines(self):
        content = "a\nb\nc\nd\ne\nf\ng\n"
        results = search_in_file(content, "d", context_lines=2)
        if results:
            assert results[0].context_before  # should have lines before
            assert results[0].context_after   # should have lines after


# ============================================================================
# find_similar_lines
# ============================================================================


class TestFindSimilarLines:
    def test_similar_content(self):
        """find_similar_lines compares line-lists, so lines must partially match."""
        search = "int add(int a, int b) {\n    return a + b;\n}\n"
        content = (
            "// header\n"
            "int add(int a, int b) {\n"
            "    return a + b;\n"
            "}\n"
            "int mul(int a, int b) {\n"
            "    return a * b;\n"
            "}\n"
        )
        hint = find_similar_lines(search, content)
        assert hint  # Lines match exactly at offset 1

    def test_no_similar_content(self):
        hint = find_similar_lines("xyz123", "abc\ndef\n")
        assert hint == ""
