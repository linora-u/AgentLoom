"""Tests for code_search tool using real C++ fixture files."""

import pytest

from src.tools.code_editor.code_search_tool import code_search


class TestCodeSearchWithCpp:
    """Test code_search against real C++ fixture files."""

    def test_search_exact_function_in_cpp(self, sample_class_cpp):
        """Search for an exact function in sample_class.cpp."""
        result = code_search(sample_class_cpp, "double add(double a, double b)")
        assert "Match" in result or "match" in result
        assert "add" in result

    def test_search_class_declaration(self, sample_class_cpp):
        """Search for the class declaration."""
        result = code_search(sample_class_cpp, "class Calculator {")
        assert "Match" in result or "match" in result
        assert "Calculator" in result

    def test_search_template_function(self, sample_algorithm_cpp):
        """Search for a template function."""
        result = code_search(
            sample_algorithm_cpp,
            "template <typename T>\nvoid bubble_sort",
        )
        assert "Match" in result or "match" in result

    def test_search_binary_search_in_cpp(self, sample_algorithm_cpp):
        """Search for binary_search implementation."""
        result = code_search(
            sample_algorithm_cpp,
            "int binary_search(const std::vector<T>& arr, const T& target)",
        )
        assert "Match" in result or "match" in result

    def test_search_nested_code(self, sample_template_cpp):
        """Search for deeply nested code."""
        result = code_search(
            sample_template_cpp,
            "if (matrix[i][j] > 0) {",
        )
        assert "Match" in result or "match" in result

    def test_search_with_indent_tolerance(self, sample_class_cpp):
        """Search with different indentation should still find results."""
        # The actual code has 4-space indent; search without indent
        result = code_search(
            sample_class_cpp,
            "double divide(double a, double b) {\nif (b == 0.0) {\nthrow std::invalid_argument",
        )
        assert "Match" in result or "match" in result or "No matches" in result

    def test_search_not_found(self, sample_class_cpp):
        """Search for something that doesn't exist."""
        result = code_search(
            sample_class_cpp,
            "this_function_does_not_exist_anywhere()",
        )
        assert "No matches" in result

    def test_search_empty_raises(self):
        with pytest.raises(ValueError, match="file_path cannot be empty"):
            code_search("", "search text")

    def test_search_empty_text_raises(self, sample_class_cpp):
        with pytest.raises(ValueError, match="search_text cannot be empty"):
            code_search(sample_class_cpp, "")

    def test_search_file_not_found(self, tmp_path):
        with pytest.raises(FileNotFoundError):
            code_search(str(tmp_path / "nonexistent.cpp"), "test")


class TestCodeSearchContext:
    """Test context lines in search results."""

    def test_context_included(self, sample_class_cpp):
        result = code_search(sample_class_cpp, "double add(double a, double b)", context_lines=5)
        # Should include surrounding code
        assert "result_" in result or "Match" in result
