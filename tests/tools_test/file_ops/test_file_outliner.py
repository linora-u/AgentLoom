"""Regression tests for the file outliner fallback parser."""

import subprocess
import sys

import pytest
from agentloom.tools.file_ops.file_outliner import (
    _FALLBACK_ARROW_PREFIX,
    _has_fallback_arrow_suffix,
    _regex_fallback_outline,
)


@pytest.mark.parametrize(
    "line,expected",
    [
        ("const plain = value => value", True),
        ("export const typed = (value: number): number => value", True),
        ("const defaulted = (value = 1) => value", True),
        ("const nested_arrow = (value => value)", True),
        ("const chained = value = result => result", False),
        ("const inner_default = (value = fallback => fallback)", False),
        ("const outer_arrow = (value = fallback => fallback) => value", True),
    ],
)
def test_fallback_arrow_suffix_preserves_matching_boundaries(line, expected):
    match = _FALLBACK_ARROW_PREFIX.match(line)
    assert match is not None
    assert _has_fallback_arrow_suffix(line, match.end()) is expected


def test_fallback_outline_extracts_arrow_function_names():
    outline = _regex_fallback_outline(
        [
            "const plain = value => value",
            "export const defaulted = (value = 1) => value",
            "const chained = value = result => result",
        ],
        detail_level="brief",
        include_line_numbers=True,
        max_items=10,
    )

    rendered = "\n".join(outline)
    assert "plain" in rendered
    assert "defaulted" in rendered
    assert "chained" not in rendered


def test_fallback_arrow_scan_does_not_backtrack_exponentially():
    script = """
from agentloom.tools.file_ops.file_outliner import (
    _FALLBACK_ARROW_PREFIX,
    _has_fallback_arrow_suffix,
)

line = "let zero = " + "()" * 100_000
match = _FALLBACK_ARROW_PREFIX.match(line)
assert match is not None
assert not _has_fallback_arrow_suffix(line, match.end())
"""
    subprocess.run(
        [sys.executable, "-c", script],
        check=True,
        capture_output=True,
        text=True,
        timeout=3,
    )
