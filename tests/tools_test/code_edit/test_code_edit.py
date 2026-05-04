"""Tests for code_edit tool using real C++ fixture files."""

import pytest

from src.tools.code_editor.code_edit_tool import code_edit


class TestCodeEditSingleBlock:
    """Test single SEARCH/REPLACE block editing."""

    def test_edit_single_block_cpp(self, sample_class_cpp):
        """Apply a single edit to sample_class.cpp."""
        diff = (
            "<<<<<<< SEARCH\n"
            "    double add(double a, double b) {\n"
            "        result_ = a + b;\n"
            "        history_count_++;\n"
            "        return result_;\n"
            "    }\n"
            "=======\n"
            "    double add(double a, double b) {\n"
            "        result_ = a + b;\n"
            "        history_count_++;\n"
            "        log_(\"add\");\n"
            "        return result_;\n"
            "    }\n"
            ">>>>>>> REPLACE\n"
        )
        result = code_edit(sample_class_cpp, diff)
        assert "Successfully applied 1 edit" in result

        with open(sample_class_cpp) as f:
            content = f.read()
        assert 'log_("add")' in content

    def test_edit_with_indent_fix(self, sample_class_cpp):
        """LLM gives code with wrong indentation — engine should fix it."""
        diff = (
            "<<<<<<< SEARCH\n"
            "double multiply(double a, double b) {\n"
            "    result_ = a * b;\n"
            "    history_count_++;\n"
            "    return result_;\n"
            "}\n"
            "=======\n"
            "double multiply(double a, double b) {\n"
            "    result_ = a * b;\n"
            "    history_count_++;\n"
            "    log_(\"multiply\");\n"
            "    return result_;\n"
            "}\n"
            ">>>>>>> REPLACE\n"
        )
        result = code_edit(sample_class_cpp, diff)
        assert "Successfully applied 1 edit" in result

        with open(sample_class_cpp) as f:
            content = f.read()
        assert 'log_("multiply")' in content


class TestCodeEditMultipleBlocks:
    """Test multiple SEARCH/REPLACE blocks in one call."""

    def test_edit_multiple_blocks_cpp(self, sample_class_cpp):
        """Apply two edits to the same file."""
        diff = (
            "<<<<<<< SEARCH\n"
            "    Calculator() : result_(0.0), history_count_(0) {}\n"
            "=======\n"
            "    Calculator() : result_(0.0), history_count_(0), name_(\"default\") {}\n"
            ">>>>>>> REPLACE\n"
            "\n"
            "<<<<<<< SEARCH\n"
            "    ~Calculator() = default;\n"
            "=======\n"
            "    ~Calculator() { /* cleaned up */ }\n"
            ">>>>>>> REPLACE\n"
        )
        result = code_edit(sample_class_cpp, diff)
        assert "Successfully applied 2 edit" in result

        with open(sample_class_cpp) as f:
            content = f.read()
        assert 'name_("default")' in content
        assert "cleaned up" in content


class TestCodeEditNewFile:
    """Test creating new files via code_edit."""

    def test_create_new_file(self, tmp_path):
        """Empty SEARCH + new file → create."""
        new_file = str(tmp_path / "new_module.cpp")
        diff = (
            "<<<<<<< SEARCH\n"
            "=======\n"
            '#include <iostream>\n'
            "\n"
            "int main() {\n"
            '    std::cout << "Hello" << std::endl;\n'
            "    return 0;\n"
            "}\n"
            ">>>>>>> REPLACE\n"
        )
        result = code_edit(new_file, diff)
        assert "Created new file" in result

        with open(new_file) as f:
            content = f.read()
        assert "Hello" in content

    def test_append_to_existing(self, sample_class_cpp):
        """Empty SEARCH + existing file → append."""
        diff = (
            "<<<<<<< SEARCH\n"
            "=======\n"
            "\n// End of file marker\n"
            ">>>>>>> REPLACE\n"
        )
        result = code_edit(sample_class_cpp, diff)
        assert "Successfully applied 1 edit" in result

        with open(sample_class_cpp) as f:
            content = f.read()
        assert "End of file marker" in content


class TestCodeEditErrorHandling:
    """Test error handling and partial failures."""

    def test_partial_failure(self, sample_class_cpp):
        """One block succeeds, one fails → file written with successful edit."""
        diff = (
            "<<<<<<< SEARCH\n"
            "    Calculator() : result_(0.0), history_count_(0) {}\n"
            "=======\n"
            "    Calculator() : result_(0.0), history_count_(0), ok_(true) {}\n"
            ">>>>>>> REPLACE\n"
            "\n"
            "<<<<<<< SEARCH\n"
            "THIS DOES NOT EXIST IN THE FILE AT ALL\n"
            "=======\n"
            "replacement text\n"
            ">>>>>>> REPLACE\n"
        )
        with pytest.raises(ValueError, match="1 SEARCH/REPLACE block.*failed"):
            code_edit(sample_class_cpp, diff)

        # The successful edit should still be applied
        with open(sample_class_cpp) as f:
            content = f.read()
        assert "ok_(true)" in content

    def test_empty_diff_raises(self, sample_class_cpp):
        with pytest.raises(ValueError, match="diff_content cannot be empty"):
            code_edit(sample_class_cpp, "")

    def test_no_blocks_found(self, sample_class_cpp):
        with pytest.raises(ValueError, match="No SEARCH/REPLACE blocks found"):
            code_edit(sample_class_cpp, "just some random text")

    def test_file_not_found_with_search(self, tmp_path):
        diff = (
            "<<<<<<< SEARCH\n"
            "some code\n"
            "=======\n"
            "new code\n"
            ">>>>>>> REPLACE\n"
        )
        with pytest.raises(FileNotFoundError):
            code_edit(str(tmp_path / "nonexistent.cpp"), diff)


class TestCodeEditDotdotdots:
    """Test ellipsis support in SEARCH/REPLACE blocks."""

    def test_dotdotdots_in_cpp(self, sample_class_cpp):
        """Use ... to skip intermediate code."""
        diff = (
            "<<<<<<< SEARCH\n"
            "    double add(double a, double b) {\n"
            "...\n"
            "    }\n"
            "=======\n"
            "    double add(double a, double b) {\n"
            "...\n"
            "    }\n"
            ">>>>>>> REPLACE\n"
        )
        # Dotdotdots with identical search/replace is a no-op
        result = code_edit(sample_class_cpp, diff)
        assert "No changes" in result or "Successfully" in result
