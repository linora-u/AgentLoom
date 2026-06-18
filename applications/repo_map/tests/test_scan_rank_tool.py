"""
测试 scan_rank_tool.py 的增量更新逻辑（无 LLM，纯工具测试）。

覆盖场景：
1. 全量扫描（force=True）：验证输出文件正确生成
2. 增量扫描（git 路径）：commit 未变 → changed=0，cached=N
3. 增量扫描（git 路径）：新增提交后 → changed=M
4. 增量扫描（mtime 路径）：非 git 目录
5. force=True：强制全量，忽略缓存
6. dirty 文件恢复到 HEAD 后，缓存内容哈希仍能发现陈旧 tags
"""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import pytest

# 确保可以导入 repo_map 模块
REPO_ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(REPO_ROOT))

from applications.repo_map.agent_tools.scan_rank_tool import scan_and_rank

FIXTURE_DIR = Path(__file__).parent / "fixtures" / "sample_project"


def _git(args: list, cwd: str) -> str:
    """运行 git 命令辅助函数"""
    result = subprocess.run(
        ["git"] + args, cwd=cwd, capture_output=True, text=True
    )
    return result.stdout.strip()


@pytest.fixture()
def tmp_output(tmp_path):
    """每个测试用独立的输出目录"""
    out = tmp_path / "repo_map_output"
    out.mkdir()
    return out


@pytest.fixture()
def git_project(tmp_path):
    """
    创建一个临时 git 项目，复制 fixture 文件，可在测试中修改和提交。
    避免污染真实的 fixture 目录。
    """
    proj = tmp_path / "project"
    shutil.copytree(FIXTURE_DIR, proj, ignore=shutil.ignore_patterns(".git"))
    subprocess.run(["git", "init"], cwd=str(proj), capture_output=True, check=True)
    subprocess.run(
        ["git", "config", "user.email", "repo-map-test@example.com"],
        cwd=str(proj),
        capture_output=True,
        check=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "Repo Map Test"],
        cwd=str(proj),
        capture_output=True,
        check=True,
    )
    subprocess.run(["git", "add", "."], cwd=str(proj), capture_output=True, check=True)
    commit = subprocess.run(
        ["git", "commit", "-m", "init", "--no-gpg-sign"],
        cwd=str(proj), capture_output=True, text=True
    )
    assert commit.returncode == 0, commit.stderr
    return proj


# ────────────────────────────────────────────────────────────────── #
#  Test 1: 全量扫描输出文件正确
# ────────────────────────────────────────────────────────────────── #

def test_full_scan_creates_output_files(git_project, tmp_output):
    """全量扫描（force=True）应生成所有必要的输出文件"""
    result = scan_and_rank(
        project_path=str(git_project),
        output_dir=str(tmp_output),
        force=True,
    )

    data_dir = tmp_output / "data"
    assert (data_dir / "tags.json").exists(), "tags.json should exist"
    assert (data_dir / "ranked.json").exists(), "ranked.json should exist"
    assert (data_dir / "scan_meta.json").exists(), "scan_meta.json should exist"
    assert (data_dir / "tags_cache.json").exists(), "tags_cache.json should exist"

    # 验证摘要字符串
    assert "files" in result.lower(), f"result should mention files: {result}"
    assert "tags" in result.lower(), f"result should mention tags: {result}"

    # 验证 tags.json 内容
    tags = json.loads((data_dir / "tags.json").read_text())
    assert len(tags) > 0, "should extract at least some tags"

    # 验证 ranked.json 内容
    ranked = json.loads((data_dir / "ranked.json").read_text())
    assert len(ranked) > 0, "should rank at least some files"
    assert all("rank" in r and "rel_fname" in r for r in ranked)

    print(f"[OK] Full scan: {result}")


# ────────────────────────────────────────────────────────────────── #
#  Test 2: 增量扫描（git）—— commit 未变 → changed=0
# ────────────────────────────────────────────────────────────────── #

def test_incremental_git_no_changes(git_project, tmp_output):
    """相同 commit、无新文件时，增量扫描 changed=0"""
    # 第一次全量
    result1 = scan_and_rank(
        project_path=str(git_project),
        output_dir=str(tmp_output),
        force=True,
    )
    assert "files" in result1.lower()

    # 第二次增量（commit 未变，无 untracked）
    result2 = scan_and_rank(
        project_path=str(git_project),
        output_dir=str(tmp_output),
        incremental=True,
        force=False,
    )

    assert "0 changed" in result2, f"Expected '0 changed', got: {result2}"
    print(f"[OK] Incremental (no changes): {result2}")


# ────────────────────────────────────────────────────────────────── #
#  Test 3: 增量扫描（git）—— 新增提交后 → changed=N
# ────────────────────────────────────────────────────────────────── #

def test_incremental_git_after_commit(git_project, tmp_output):
    """新增提交后，增量扫描应检测到变更文件"""
    # 第一次全量
    scan_and_rank(
        project_path=str(git_project),
        output_dir=str(tmp_output),
        force=True,
    )

    # 修改一个文件并提交
    modified_file = git_project / "models" / "user.py"
    original = modified_file.read_text()
    modified_file.write_text(original + "\n\ndef new_function():\n    return 42\n")

    subprocess.run(["git", "add", "models/user.py"], cwd=str(git_project), capture_output=True)
    subprocess.run(
        ["git", "commit", "-m", "add new_function", "--no-gpg-sign"],
        cwd=str(git_project), capture_output=True
    )

    # 增量扫描
    result = scan_and_rank(
        project_path=str(git_project),
        output_dir=str(tmp_output),
        incremental=True,
        force=False,
    )

    # 应该检测到至少 1 个变化文件（utils.py）
    assert "0 changed" not in result, f"Should detect changes, got: {result}"
    print(f"[OK] Incremental (after commit): {result}")

    # 验证 tags_cache.json 更新了 last_scan_commit
    cache = json.loads((tmp_output / "data" / "tags_cache.json").read_text())
    current_sha = _git(["rev-parse", "HEAD"], str(git_project))
    assert cache.get("last_scan_commit") == current_sha, \
        f"Cache SHA mismatch: {cache.get('last_scan_commit')} != {current_sha}"


# ────────────────────────────────────────────────────────────────── #
#  Test 4: 增量扫描（mtime fallback）—— 非 git 目录
# ────────────────────────────────────────────────────────────────── #

def test_incremental_mtime_non_git(tmp_path):
    """非 git 目录使用 mtime 增量检测"""
    # 创建一个非 git 目录
    proj = tmp_path / "non_git_project"
    proj.mkdir()
    (proj / "foo.py").write_text("def foo(): return 1\n")
    (proj / "bar.py").write_text("def bar(): return 2\n")

    out = tmp_path / "output"
    out.mkdir()

    # 第一次全量
    result1 = scan_and_rank(str(proj), str(out), force=True)
    assert "files" in result1.lower()

    # 第二次增量（文件未修改）
    result2 = scan_and_rank(str(proj), str(out), incremental=True)
    assert "0 changed" in result2, f"Expected '0 changed' for unchanged files, got: {result2}"
    print(f"[OK] Incremental mtime (no change): {result2}")

    # 修改一个文件的 mtime
    foo = proj / "foo.py"
    foo.write_text("def foo(): return 99\ndef extra(): pass\n")

    # 第三次增量（foo.py 变化）
    result3 = scan_and_rank(str(proj), str(out), incremental=True)
    assert "0 changed" not in result3, f"Should detect change in foo.py, got: {result3}"
    print(f"[OK] Incremental mtime (one change): {result3}")


# ────────────────────────────────────────────────────────────────── #
#  Test 5: force=True 强制全量
# ────────────────────────────────────────────────────────────────── #

def test_force_full_scan(git_project, tmp_output):
    """force=True 应强制全量扫描，忽略缓存"""
    # 第一次全量建立缓存
    scan_and_rank(str(git_project), str(tmp_output), force=True)

    # 第二次 force=True 仍然全量
    result = scan_and_rank(str(git_project), str(tmp_output), force=True)
    # force 模式下 changed 等于总文件数（不是 0）
    assert "0 changed" not in result, f"force=True should rescan all, got: {result}"
    print(f"[OK] Force full scan: {result}")


# ────────────────────────────────────────────────────────────────── #
#  Test 6: tags_cache.json 结构正确
# ────────────────────────────────────────────────────────────────── #

def test_tags_cache_structure(git_project, tmp_output):
    """验证 tags_cache.json 的数据结构符合 aider 设计"""
    scan_and_rank(str(git_project), str(tmp_output), force=True)

    cache_path = tmp_output / "data" / "tags_cache.json"
    assert cache_path.exists()

    cache = json.loads(cache_path.read_text())
    assert cache.get("cache_version") == 2, "cache_version should be 2"
    assert "project_path" in cache
    assert "last_scan_commit" in cache
    assert "entries" in cache
    assert isinstance(cache["entries"], dict)

    # 每个 entry 有 mtime 和 tags
    for fname, entry in cache["entries"].items():
        assert "mtime" in entry, f"entry {fname} missing mtime"
        assert "content_hash" in entry, f"entry {fname} missing content_hash"
        assert "tags" in entry, f"entry {fname} missing tags"
        assert isinstance(entry["tags"], list)

    print(f"[OK] tags_cache.json structure: {len(cache['entries'])} entries")


# ────────────────────────────────────────────────────────────────── #
#  Test 7: dirty 文件（工作区未提交改动）被检测
# ────────────────────────────────────────────────────────────────── #

def test_incremental_git_dirty_files(git_project, tmp_output):
    """工作区有未提交改动时，增量扫描应检测到 dirty 文件"""
    # 第一次全量
    scan_and_rank(str(git_project), str(tmp_output), force=True)

    # 修改文件但不提交（dirty）
    dirty_file = git_project / "models" / "user.py"
    dirty_file.write_text(
        dirty_file.read_text() + "\n\nclass Extra: pass\n"
    )

    # 增量扫描应检测到 dirty 文件
    result = scan_and_rank(str(git_project), str(tmp_output), incremental=True)
    assert "0 changed" not in result, f"Should detect dirty file, got: {result}"
    print(f"[OK] Dirty file detected: {result}")


def test_incremental_git_restored_dirty_file_invalidates_cached_tags(git_project, tmp_output):
    """dirty 内容被扫描后恢复到 HEAD，也应刷新陈旧缓存 tags"""
    scan_and_rank(str(git_project), str(tmp_output), force=True)

    dirty_file = git_project / "models" / "user.py"
    original = dirty_file.read_text()
    dirty_file.write_text(
        original + "\n\ndef transient_probe():\n    return 'repo-map-probe'\n"
    )

    result_dirty = scan_and_rank(str(git_project), str(tmp_output), incremental=True)
    assert "0 changed" not in result_dirty, f"Should detect dirty file, got: {result_dirty}"

    tags_path = tmp_output / "data" / "tags.json"
    tags = json.loads(tags_path.read_text())
    assert any(t["name"] == "transient_probe" for t in tags)

    dirty_file.write_text(original)
    assert _git(["status", "--short", "--", "models/user.py"], str(git_project)) == ""

    result_restored = scan_and_rank(str(git_project), str(tmp_output), incremental=True)
    assert "0 changed" not in result_restored, (
        "Clean working tree must still invalidate tags cached from transient dirty content, "
        f"got: {result_restored}"
    )

    tags = json.loads(tags_path.read_text())
    assert not any(t["name"] == "transient_probe" for t in tags)
