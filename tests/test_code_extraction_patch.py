"""
Tests for the greedy code extraction monkeypatch.

Covers:
- Normal code blocks (no nested backticks) – backward-compatible
- Code containing Markdown triple-backtick inside string literals (the bug)
- Multiple code blocks in one response
- Missing closing tag (partial output)
- Code that only passes with non-greedy (greedy candidate is invalid)
- Idempotent patching
- parse_code_blobs wrapper
"""

import ast
import pytest
import sys

from src.lib.smolagents.monkey_patch.code_extraction_patch import (
    _patched_extract_code_from_text,
    _patched_parse_code_blobs,
    _is_valid_python,
    _try_extract_greedy,
    patch_smolagents_code_extraction,
)


MARKDOWN_TAGS = ("```python", "```")


class TestIsValidPython:
    def test_valid(self):
        assert _is_valid_python("x = 1") is True

    def test_invalid(self):
        assert _is_valid_python('x = """unterminated') is False

    def test_empty(self):
        assert _is_valid_python("") is True


class TestNormalCodeBlocks:
    """Backward-compatible: no nested backticks."""

    def test_simple(self):
        text = 'Thoughts: do something\n```python\nx = 1\nprint(x)\n```'
        result = _patched_extract_code_from_text(text, MARKDOWN_TAGS)
        assert result == "x = 1\nprint(x)"

    def test_multiline(self):
        code = "import os\npath = os.getcwd()\nprint(path)"
        text = f"Here is the plan:\n```python\n{code}\n```\nDone."
        result = _patched_extract_code_from_text(text, MARKDOWN_TAGS)
        assert result == code

    def test_no_code_block(self):
        text = "Just some plain text without any code."
        result = _patched_extract_code_from_text(text, MARKDOWN_TAGS)
        assert result is None


class TestNestedBackticks:
    """THE BUG: code containing ``` inside string literals."""

    def test_markdown_in_string_triple_quote(self):
        """Reproduces the actual failure from Step 29 logs."""
        code = '''body = """
## Dependencies

```c
#include "Can.h"
```

Some more text.
"""
write_markdown_file("output.md", body)'''
        text = f"Thoughts: writing deps\n```python\n{code}\n```"
        result = _patched_extract_code_from_text(text, MARKDOWN_TAGS)
        assert result is not None
        # Must be valid Python
        ast.parse(result)
        assert 'write_markdown_file("output.md", body)' in result

    def test_multiple_nested_backticks(self):
        """Multiple ``` inside the code."""
        code = '''content = """
```yaml
key: value
```

```json
{"a": 1}
```
"""
print(content)'''
        text = f"```python\n{code}\n```"
        result = _patched_extract_code_from_text(text, MARKDOWN_TAGS)
        assert result is not None
        ast.parse(result)
        assert "print(content)" in result

    def test_single_backtick_string(self):
        """Backticks in single-quoted strings."""
        code = 'x = "```python\\ncode\\n```"\nprint(x)'
        text = f"```python\n{code}\n```"
        result = _patched_extract_code_from_text(text, MARKDOWN_TAGS)
        assert result is not None
        ast.parse(result)


class TestMultipleCodeBlocks:
    """Multiple code blocks in one LLM response."""

    def test_two_simple_blocks(self):
        text = (
            "First:\n```python\nx = 1\n```\n"
            "Second:\n```python\ny = 2\n```"
        )
        result = _patched_extract_code_from_text(text, MARKDOWN_TAGS)
        assert result is not None
        assert "x = 1" in result
        assert "y = 2" in result


class TestFallbackToNonGreedy:
    """When greedy match fails, fall back to original non-greedy."""

    def test_nongreedy_fallback(self):
        """If all greedy candidates fail ast.parse, non-greedy is tried."""
        # This is a contrived example:  open_tag ... invalid_python ... close_tag
        # where the text between tags is not valid Python.
        # Non-greedy should still return it (smolagents upstream behavior).
        text = "```python\nthis is not python but non-greedy finds it\n```"
        result = _patched_extract_code_from_text(text, MARKDOWN_TAGS)
        # Non-greedy will match it even though it's invalid Python
        assert result is not None
        assert "this is not python" in result


class TestMissingCloseTag:
    """LLM output truncated, no closing ```."""

    def test_no_close_tag(self):
        text = "```python\nx = 1\nprint(x)"
        result = _patched_extract_code_from_text(text, MARKDOWN_TAGS)
        # No closing tag → no match from either strategy
        assert result is None


class TestParsedCodeBlobs:
    """Test the parse_code_blobs wrapper."""

    def test_normal(self):
        text = "```python\nresult = 42\n```"
        result = _patched_parse_code_blobs(text, MARKDOWN_TAGS)
        assert result == "result = 42"

    def test_nested_backticks(self):
        code = '''body = """
```c
int x = 0;
```
"""
print(body)'''
        text = f"```python\n{code}\n```"
        result = _patched_parse_code_blobs(text, MARKDOWN_TAGS)
        ast.parse(result)
        assert "print(body)" in result

    def test_raw_code_no_tags(self):
        """LLM outputs raw Python without any tags."""
        text = "x = 1\nprint(x)"
        result = _patched_parse_code_blobs(text, MARKDOWN_TAGS)
        assert result == text

    def test_invalid_no_tags_raises(self):
        text = "this is not code at all, no tags, no python"
        with pytest.raises(ValueError):
            _patched_parse_code_blobs(text, MARKDOWN_TAGS)


class TestPatchIdempotent:
    """patch_smolagents_code_extraction is safe to call multiple times."""

    def test_double_call(self):
        import src.lib.smolagents.monkey_patch.code_extraction_patch as mod
        # Reset the guard
        original = mod._PATCHED
        mod._PATCHED = False
        try:
            patch_smolagents_code_extraction()
            patch_smolagents_code_extraction()  # second call is a no-op
            assert mod._PATCHED is True
        finally:
            mod._PATCHED = original


class TestGreedyExtractDirectly:
    """Directly test _try_extract_greedy edge cases."""

    def test_empty_text(self):
        assert _try_extract_greedy("", "```python", "```") == []

    def test_open_no_close(self):
        assert _try_extract_greedy("```python\nx=1", "```python", "```") == []

    def test_close_before_open(self):
        assert _try_extract_greedy("```\n```python\nx=1\n```", "```python", "```") == ["x=1"]


# =====================================================================
#  XML tag tests — the default: ("<code>", "</code>")
# =====================================================================

XML_TAGS = ("<code>", "</code>")


class TestXmlTagsBasic:
    """Basic extraction with XML tags."""

    def test_simple(self):
        text = "Thoughts: do something\n<code>\nx = 1\nprint(x)\n</code>"
        result = _patched_extract_code_from_text(text, XML_TAGS)
        assert result == "x = 1\nprint(x)"

    def test_multiline(self):
        code = "import os\npath = os.getcwd()\nprint(path)"
        text = f"Here is my plan:\n<code>\n{code}\n</code>\nDone."
        result = _patched_extract_code_from_text(text, XML_TAGS)
        assert result == code

    def test_no_code_block(self):
        text = "Just some plain text."
        result = _patched_extract_code_from_text(text, XML_TAGS)
        assert result is None


class TestXmlTagsNestedBackticks:
    """The whole point: ``` inside code is harmless with XML tags."""

    def test_markdown_backticks_in_string(self):
        """The exact scenario that crashed with markdown tags — now trivial."""
        code = '''body = """
## Dependencies

```c
#include "Can.h"
```

Some more text.
"""
write_markdown_file("output.md", body)'''
        text = f"<code>\n{code}\n</code>"
        result = _patched_extract_code_from_text(text, XML_TAGS)
        assert result is not None
        ast.parse(result)
        assert 'write_markdown_file("output.md", body)' in result

    def test_many_backtick_blocks(self):
        code = '''content = """
```yaml
key: value
```

```json
{"a": 1}
```

```python
x = 1
```
"""
print(content)'''
        text = f"<code>\n{code}\n</code>"
        result = _patched_extract_code_from_text(text, XML_TAGS)
        assert result is not None
        ast.parse(result)
        assert "print(content)" in result


class TestXmlTagsMissingClose:
    """LLM output truncated, no closing </python>."""

    def test_no_close_tag(self):
        text = "<code>\nx = 1\nprint(x)"
        result = _patched_extract_code_from_text(text, XML_TAGS)
        assert result is None


class TestXmlParseCodeBlobs:
    """parse_code_blobs with XML tags."""

    def test_normal(self):
        text = "<code>\nresult = 42\n</code>"
        result = _patched_parse_code_blobs(text, XML_TAGS)
        assert result == "result = 42"

    def test_nested_backticks(self):
        code = '''body = """
```c
int x = 0;
```
"""
print(body)'''
        text = f"<code>\n{code}\n</code>"
        result = _patched_parse_code_blobs(text, XML_TAGS)
        ast.parse(result)
        assert "print(body)" in result

    def test_fallback_to_markdown(self):
        """If LLM outputs ```python instead of <code>, fallback kicks in."""
        text = "```python\nresult = 42\n```"
        result = _patched_parse_code_blobs(text, XML_TAGS)
        # XML extraction finds nothing, but fallback to markdown pattern works
        assert result == "result = 42"

    def test_invalid_no_tags_raises(self):
        text = "this is not code at all"
        with pytest.raises(ValueError):
            _patched_parse_code_blobs(text, XML_TAGS)
