"""Tests for code_replace tool using real C++ fixture files."""

import pytest

from src.tools.code_editor.code_replace_tool import code_replace


class TestCodeReplaceWithCpp:
    """Test code_replace against real C++ fixture files."""

    def test_replace_exact_in_cpp(self, sample_class_cpp):
        """Replace a function body exactly."""
        result = code_replace(
            sample_class_cpp,
            search_text=(
                "    double add(double a, double b) {\n"
                "        result_ = a + b;\n"
                "        history_count_++;\n"
                "        return result_;\n"
                "    }"
            ),
            replace_text=(
                "    double add(double a, double b) {\n"
                "        result_ = a + b;\n"
                "        history_count_++;\n"
                "        log_operation(\"add\");\n"
                "        return result_;\n"
                "    }"
            ),
        )
        assert "Replaced" in result or "lines changed" in result

        # Verify the file was actually changed
        with open(sample_class_cpp) as f:
            content = f.read()
        assert 'log_operation("add")' in content

    def test_replace_with_indent_shift(self, sample_class_cpp):
        """LLM outputs code without the namespace indentation."""
        result = code_replace(
            sample_class_cpp,
            search_text=(
                "void reset() {\n"
                "    result_ = 0.0;\n"
                "    history_count_ = 0;\n"
                "}"
            ),
            replace_text=(
                "void reset() {\n"
                "    result_ = 0.0;\n"
                "    history_count_ = 0;\n"
                "    last_error_.clear();\n"
                "}"
            ),
        )
        assert "Replaced" in result or "lines changed" in result

        with open(sample_class_cpp) as f:
            content = f.read()
        assert "last_error_.clear()" in content

    def test_replace_linked_list_method(self, sample_algorithm_cpp):
        """Replace a method in the LinkedList class."""
        result = code_replace(
            sample_algorithm_cpp,
            search_text="    int get_size() const { return size_; }",
            replace_text=(
                "    int get_size() const { return size_; }\n"
                "    bool is_empty() const { return size_ == 0; }"
            ),
        )
        assert "Replaced" in result or "lines changed" in result

        with open(sample_algorithm_cpp) as f:
            content = f.read()
        assert "is_empty" in content

    def test_replace_not_found_raises(self, sample_class_cpp):
        """Should raise ValueError with hints when not found."""
        with pytest.raises(ValueError, match="Failed to match"):
            code_replace(
                sample_class_cpp,
                search_text="this_function_does_not_exist_at_all()",
                replace_text="replacement()",
            )

    def test_replace_file_not_found(self, tmp_path):
        with pytest.raises(FileNotFoundError):
            code_replace(
                str(tmp_path / "nonexistent.cpp"),
                search_text="old",
                replace_text="new",
            )

    def test_replace_empty_args(self):
        with pytest.raises(ValueError):
            code_replace("", "old", "new")
        with pytest.raises(ValueError):
            code_replace("/some/file", "", "new")


class TestCodeReplaceEncoding:
    """Test encoding handling."""

    def test_preserves_encoding(self, sample_class_cpp):
        """File should still be readable after replacement."""
        code_replace(
            sample_class_cpp,
            search_text="Calculator() : result_(0.0), history_count_(0) {}",
            replace_text="Calculator() : result_(0.0), history_count_(0), initialized_(true) {}",
        )
        with open(sample_class_cpp, encoding="utf-8") as f:
            content = f.read()
        assert "initialized_(true)" in content
