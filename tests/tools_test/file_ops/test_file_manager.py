"""Tests for file_manager tools (delete / move / rename / copy)."""

import pytest

from src.tools.file_ops.file_manager import (
    copy_file,
    delete_file,
    move_file,
    rename_file,
)


# =========================================================================
# delete_file
# =========================================================================

class TestDeleteFile:
    def test_delete_existing_file(self, tmp_path):
        f = tmp_path / "to_delete.txt"
        f.write_text("bye")
        result = delete_file(str(f))
        assert "Deleted file" in result
        assert not f.exists()

    def test_delete_nonexistent_raises(self, tmp_path):
        with pytest.raises(FileNotFoundError):
            delete_file(str(tmp_path / "nope.txt"))

    def test_delete_nonexistent_missing_ok(self, tmp_path):
        result = delete_file(str(tmp_path / "nope.txt"), missing_ok=True)
        assert "skipped" in result.lower()

    def test_delete_directory_raises_without_force(self, tmp_path):
        d = tmp_path / "subdir"
        d.mkdir()
        with pytest.raises(IsADirectoryError):
            delete_file(str(d))

    def test_delete_directory_with_force(self, tmp_path):
        d = tmp_path / "subdir"
        d.mkdir()
        (d / "a.txt").write_text("hello")
        result = delete_file(str(d), force=True)
        assert "Deleted directory" in result
        assert not d.exists()

    def test_delete_empty_path_raises(self):
        with pytest.raises(ValueError):
            delete_file("")


# =========================================================================
# move_file
# =========================================================================

class TestMoveFile:
    def test_move_file(self, tmp_path):
        src = tmp_path / "a.txt"
        src.write_text("content")
        dst = tmp_path / "b.txt"
        result = move_file(str(src), str(dst))
        assert "Moved" in result
        assert not src.exists()
        assert dst.read_text() == "content"

    def test_move_creates_parent_dirs(self, tmp_path):
        src = tmp_path / "a.txt"
        src.write_text("hi")
        dst = tmp_path / "deep" / "nested" / "b.txt"
        result = move_file(str(src), str(dst))
        assert "Moved" in result
        assert dst.exists()

    def test_move_overwrite_false_raises(self, tmp_path):
        src = tmp_path / "a.txt"
        src.write_text("src")
        dst = tmp_path / "b.txt"
        dst.write_text("dst")
        with pytest.raises(FileExistsError):
            move_file(str(src), str(dst))

    def test_move_overwrite_true(self, tmp_path):
        src = tmp_path / "a.txt"
        src.write_text("new")
        dst = tmp_path / "b.txt"
        dst.write_text("old")
        move_file(str(src), str(dst), overwrite=True)
        assert dst.read_text() == "new"
        assert not src.exists()

    def test_move_nonexistent_raises(self, tmp_path):
        with pytest.raises(FileNotFoundError):
            move_file(str(tmp_path / "nope"), str(tmp_path / "dst"))


# =========================================================================
# rename_file
# =========================================================================

class TestRenameFile:
    def test_rename(self, tmp_path):
        f = tmp_path / "old.txt"
        f.write_text("data")
        result = rename_file(str(f), "new.txt")
        assert "Moved" in result
        assert not f.exists()
        assert (tmp_path / "new.txt").read_text() == "data"

    def test_rename_with_separator_raises(self, tmp_path):
        f = tmp_path / "old.txt"
        f.write_text("data")
        with pytest.raises(ValueError, match="separator"):
            rename_file(str(f), "sub/new.txt")

    def test_rename_empty_name_raises(self, tmp_path):
        with pytest.raises(ValueError):
            rename_file(str(tmp_path / "a.txt"), "")


# =========================================================================
# copy_file
# =========================================================================

class TestCopyFile:
    def test_copy_file(self, tmp_path):
        src = tmp_path / "a.txt"
        src.write_text("hello")
        dst = tmp_path / "b.txt"
        result = copy_file(str(src), str(dst))
        assert "Copied file" in result
        assert src.exists()  # original still there
        assert dst.read_text() == "hello"

    def test_copy_directory(self, tmp_path):
        src = tmp_path / "dir"
        src.mkdir()
        (src / "f.txt").write_text("x")
        dst = tmp_path / "dir_copy"
        result = copy_file(str(src), str(dst))
        assert "Copied directory" in result
        assert (dst / "f.txt").read_text() == "x"

    def test_copy_creates_parent_dirs(self, tmp_path):
        src = tmp_path / "a.txt"
        src.write_text("data")
        dst = tmp_path / "deep" / "nested" / "a.txt"
        copy_file(str(src), str(dst))
        assert dst.read_text() == "data"

    def test_copy_overwrite_false_raises(self, tmp_path):
        src = tmp_path / "a.txt"
        src.write_text("src")
        dst = tmp_path / "b.txt"
        dst.write_text("dst")
        with pytest.raises(FileExistsError):
            copy_file(str(src), str(dst))

    def test_copy_overwrite_true(self, tmp_path):
        src = tmp_path / "a.txt"
        src.write_text("new")
        dst = tmp_path / "b.txt"
        dst.write_text("old")
        copy_file(str(src), str(dst), overwrite=True)
        assert dst.read_text() == "new"

    def test_copy_nonexistent_raises(self, tmp_path):
        with pytest.raises(FileNotFoundError):
            copy_file(str(tmp_path / "nope"), str(tmp_path / "dst"))
