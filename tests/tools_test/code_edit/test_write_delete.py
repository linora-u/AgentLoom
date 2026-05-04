"""Tests for write_whole_file and delete_file tools."""

import pytest

from src.tools.code_editor import write_whole_file
from src.tools.file_ops.file_manager import delete_file


class TestWriteWholeFile:
    def test_create_new_file(self, tmp_path):
        path = str(tmp_path / "new.cpp")
        result = write_whole_file(path, "#include <iostream>\nint main() {}\n")
        assert "Created new file" in result

        with open(path) as f:
            assert "#include <iostream>" in f.read()

    def test_overwrite_existing(self, sample_class_cpp):
        result = write_whole_file(sample_class_cpp, "// overwritten\n")
        assert "Overwrote file" in result

        with open(sample_class_cpp) as f:
            assert f.read() == "// overwritten\n"

    def test_empty_path_raises(self):
        with pytest.raises(ValueError):
            write_whole_file("", "content")


class TestDeleteFile:
    def test_delete_existing(self, tmp_path):
        path = tmp_path / "to_delete.txt"
        path.write_text("delete me")
        result = delete_file(str(path))
        assert "Deleted" in result
        assert not path.exists()

    def test_delete_nonexistent_raises(self, tmp_path):
        with pytest.raises(FileNotFoundError):
            delete_file(str(tmp_path / "nope.txt"))

    def test_delete_directory_raises(self, tmp_path):
        with pytest.raises(IsADirectoryError):
            delete_file(str(tmp_path))
