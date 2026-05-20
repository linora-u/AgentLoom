"""
Scan & Rank tool: scans a project directory, extracts code tags via RepoMap,
runs PageRank, and serialises results to JSON files.

Called directly by repo_map_app.py (pure Python, no LLM agent needed).

增量更新策略B（参考 aider repomap.py + repo.py 实现）：
- 优先使用 git commit hash 检测变化文件（精确，大仓库性能好）
- 非 git 仓库或 git 命令失败时，fallback 到 mtime 检测（对应 aider 的 mtime 缓存）
- Tags 缓存结构与 aider 一致：{fname: {"mtime": float, "data": [tag, ...]}}
  存储在 tags_cache.json（aider 用 DiskCache/SQLite，我们用 json，零新依赖）
- 修复 refresh="always" → refresh="files"，让 aider 内部 mtime 缓存真正生效
"""

import json
import os
import subprocess
from pathlib import Path
from typing import Optional

from .repomap import RepoMap, MinimalIO, Tag


# Tags 缓存版本，结构变化时递增（对应 aider CACHE_VERSION）
TAGS_CACHE_VERSION = 1

# Default directories to always exclude
DEFAULT_EXCLUDE_DIRS = {
    ".git", ".svn", ".hg",
    "node_modules", "__pycache__", ".venv", "venv", ".env",
    "build", "dist", ".eggs", ".tox", ".mypy_cache", ".pytest_cache",
    ".ruff_cache", "*.egg-info",
    ".idea", ".vscode", ".DS_Store",
    ".repo_map", ".codebase",  # Exclude our own output and CI metadata
}

# Extensions to skip (binary / generated)
SKIP_EXTENSIONS = {
    ".pyc", ".pyo", ".pyd", ".so", ".o", ".a", ".lib", ".dll", ".exe",
    ".jpg", ".jpeg", ".png", ".gif", ".bmp", ".svg", ".ico",
    ".mp3", ".mp4", ".avi", ".mov",
    ".zip", ".tar", ".gz", ".bz2", ".xz", ".7z",
    ".pdf", ".doc", ".docx", ".xls", ".xlsx",
    ".bin", ".dat", ".db", ".sqlite",
    ".min.js", ".min.css",
}


# ─────────────────────────────────────────────────────────────────── #
#  内部辅助函数
# ─────────────────────────────────────────────────────────────────── #

def _should_exclude_dir(dir_name: str, exclude_dirs: set) -> bool:
    """Check whether a directory should be excluded from scanning."""
    return dir_name in exclude_dirs


def _collect_files(project_path: str, exclude_dirs: list) -> list:
    """
    Recursively collect all source files under project_path,
    respecting exclude_dirs (directory name or relative path).
    """
    proj = Path(project_path)
    exclude_set = DEFAULT_EXCLUDE_DIRS | set(exclude_dirs)

    exclude_abs = set()
    for d in exclude_dirs:
        candidate = proj / d
        if candidate.exists():
            exclude_abs.add(str(candidate.resolve()))

    files = []
    for dirpath, dirnames, filenames in os.walk(proj):
        dir_path = Path(dirpath)
        dirnames[:] = [
            d for d in dirnames
            if not _should_exclude_dir(d, exclude_set)
            and str((dir_path / d).resolve()) not in exclude_abs
        ]
        for filename in filenames:
            ext = Path(filename).suffix.lower()
            if ext in SKIP_EXTENSIONS:
                continue
            files.append(str(dir_path / filename))

    return sorted(files)


# ── git 辅助（参考 aider repo.py）──

def _git_run(args: list, cwd: str) -> Optional[str]:
    """运行 git 命令，返回 stdout 字符串，失败返回 None。"""
    try:
        result = subprocess.run(
            ["git"] + args,
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=30,
        )
        if result.returncode == 0:
            return result.stdout.strip()
        return None
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        return None


def _is_git_repo(project_path: str) -> bool:
    """检查是否为 git 仓库（.git 目录存在且 git rev-parse 成功）。"""
    git_dir = Path(project_path) / ".git"
    if not git_dir.exists():
        return False
    return _git_run(["rev-parse", "--git-dir"], project_path) is not None


def _git_head_sha(project_path: str) -> Optional[str]:
    """获取 HEAD commit 完整 SHA（参考 aider get_head_commit_sha()）。"""
    return _git_run(["rev-parse", "HEAD"], project_path)


def _git_changed_files(project_path: str, from_sha: str, to_sha: str) -> set:
    """
    获取两个 commit 之间变更的文件（参考 aider get_dirty_files()）。
    返回相对路径集合。
    """
    changed = set()

    # commit 间差异文件
    diff_out = _git_run(["diff", "--name-only", from_sha, to_sha, "--"], project_path)
    if diff_out:
        changed.update(f for f in diff_out.splitlines() if f)

    # 工作区未暂存（对应 aider: git diff --name-only）
    unstaged = _git_run(["diff", "--name-only"], project_path)
    if unstaged:
        changed.update(f for f in unstaged.splitlines() if f)

    # 暂存区（对应 aider: git diff --name-only --cached）
    staged = _git_run(["diff", "--name-only", "--cached"], project_path)
    if staged:
        changed.update(f for f in staged.splitlines() if f)

    return changed


def _git_deleted_files(project_path: str, from_sha: str, to_sha: str) -> set:
    """获取从 from_sha 到 to_sha 被删除的文件（相对路径）。"""
    out = _git_run(
        ["diff", "--name-only", "--diff-filter=D", from_sha, to_sha, "--"],
        project_path,
    )
    if out:
        return set(f for f in out.splitlines() if f)
    return set()


def _git_untracked_files(project_path: str) -> set:
    """获取 untracked 文件列表（相对路径）。"""
    out = _git_run(["ls-files", "--others", "--exclude-standard"], project_path)
    if out:
        return set(f for f in out.splitlines() if f)
    return set()


# ── Tags 缓存（参考 aider TAGS_CACHE 的 {fname: {mtime, data}} 结构）──

def _load_tags_cache(cache_path: Path) -> dict:
    """
    加载 tags_cache.json。

    对应 aider load_tags_cache()：
    - DiskCache 加载失败 → fallback dict（我们：json 解析失败 → 返回空缓存）
    - version 不匹配 → 清空缓存重建
    """
    if not cache_path.exists():
        return {"cache_version": TAGS_CACHE_VERSION, "project_path": "", "last_scan_commit": None, "entries": {}}
    try:
        data = json.loads(cache_path.read_text(encoding="utf-8"))
        # version 不匹配则清空（参考 aider CACHE_VERSION 机制）
        if data.get("cache_version") != TAGS_CACHE_VERSION:
            print(f"[scan_and_rank] Cache version mismatch, rebuilding...")
            return {"cache_version": TAGS_CACHE_VERSION, "project_path": "", "last_scan_commit": None, "entries": {}}
        return data
    except (json.JSONDecodeError, OSError) as e:
        # 对应 aider tags_cache_error() → fallback dict
        print(f"[scan_and_rank] Cache load error ({e}), falling back to full scan")
        return {"cache_version": TAGS_CACHE_VERSION, "project_path": "", "last_scan_commit": None, "entries": {}}


def _save_tags_cache(cache_path: Path, cache: dict) -> None:
    """
    写回 tags_cache.json。
    注意：aider 用 DiskCache 自动持久化（save_tags_cache 是空函数），我们需要显式写。
    """
    try:
        cache_path.write_text(
            json.dumps(cache, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    except OSError as e:
        print(f"[scan_and_rank] Warning: failed to save tags cache: {e}")


def _get_file_mtime(fname: str) -> Optional[float]:
    """获取文件 mtime（对应 aider get_mtime()）。"""
    try:
        return os.path.getmtime(fname)
    except (FileNotFoundError, OSError):
        return None


# ─────────────────────────────────────────────────────────────────── #
#  Public API
# ─────────────────────────────────────────────────────────────────── #

def scan_and_rank(
    project_path: str,
    output_dir: str,
    exclude_dirs: Optional[list] = None,
    incremental: bool = True,
    force: bool = False,
    map_tokens: int = 4096,
    verbose: bool = False,
) -> str:
    """
    扫描项目目录，提取代码 tags，执行 PageRank 排序，写入 JSON 文件。

    增量更新策略B（参考 aider repomap.py + repo.py）：
    - git 仓库：优先用 git commit hash 差异检测变化文件
      - commit 相同：只检查 untracked 文件的 mtime
      - commit 不同：git diff 获取变更文件 + dirty 文件 + untracked 文件
    - 非 git 仓库 / git 失败：fallback 到 mtime 检测（与 aider mtime 缓存一致）
    - force=True：忽略所有缓存，强制全量扫描

    Args:
        project_path: 项目根目录绝对路径
        output_dir:   输出目录，创建 <output_dir>/data/ 子目录
        exclude_dirs: 要排除的目录名列表，默认排除常见 build/cache 目录
        incremental:  是否启用增量扫描，默认 True
        force:        强制全量重扫，忽略所有缓存，默认 False
        map_tokens:   RepoMap token 限制，控制输出详细度，默认 4096
        verbose:      打印详细进度，默认 False

    Returns:
        摘要字符串，包含文件数、tags 数、输出路径
        增量模式额外包含：changed/cached 文件数
    """
    if exclude_dirs is None:
        exclude_dirs = []

    proj = Path(project_path).resolve()
    out_data = Path(output_dir) / "data"
    out_data.mkdir(parents=True, exist_ok=True)

    cache_path = out_data / "tags_cache.json"

    # ── 1. 收集当前所有文件 ──
    print(f"[scan_and_rank] Collecting files from {proj} ...")
    all_files = _collect_files(str(proj), exclude_dirs)
    print(f"[scan_and_rank] Found {len(all_files)} files")

    if not all_files:
        return f"No source files found in {project_path}"

    # ── 2. 确定需要重新扫描的文件（策略B）──
    all_files_abs = set(all_files)

    if force or not incremental:
        # 全量模式：所有文件都需要重扫
        changed_files = all_files_abs
        cache = {"cache_version": TAGS_CACHE_VERSION, "project_path": str(proj), "last_scan_commit": None, "entries": {}}
        mode = "full"
        print(f"[scan_and_rank] Mode: full scan (force={force})")
    else:
        # 增量模式：加载缓存，检测变化
        cache = _load_tags_cache(cache_path)
        # 缓存的项目路径不同时，清空缓存
        if cache.get("project_path") and cache["project_path"] != str(proj):
            print(f"[scan_and_rank] project_path changed, clearing cache")
            cache = {"cache_version": TAGS_CACHE_VERSION, "project_path": str(proj), "last_scan_commit": None, "entries": {}}

        is_git = _is_git_repo(str(proj))

        if is_git:
            current_sha = _git_head_sha(str(proj))
            last_sha = cache.get("last_scan_commit")

            if current_sha and last_sha and current_sha == last_sha:
                # ── commit 相同：只检查 untracked 文件 ──
                untracked_rel = _git_untracked_files(str(proj))
                changed_files = set()
                for rel in untracked_rel:
                    abs_path = str(proj / rel)
                    if abs_path in all_files_abs:
                        cached_mtime = cache["entries"].get(abs_path, {}).get("mtime")
                        current_mtime = _get_file_mtime(abs_path)
                        if current_mtime != cached_mtime:
                            changed_files.add(abs_path)
                mode = f"git-same-commit ({current_sha[:7]})"

            elif current_sha and last_sha:
                # ── commit 不同：git diff 获取变更文件 ──
                changed_rel = _git_changed_files(str(proj), last_sha, current_sha)
                deleted_rel = _git_deleted_files(str(proj), last_sha, current_sha)
                untracked_rel = _git_untracked_files(str(proj))
                all_changed_rel = changed_rel | untracked_rel

                # 删除文件：从缓存移除
                for rel in deleted_rel:
                    abs_path = str(proj / rel)
                    cache["entries"].pop(abs_path, None)

                # 变更文件转为绝对路径（过滤不在 all_files 中的）
                changed_files = set()
                for rel in all_changed_rel:
                    abs_path = str(proj / rel)
                    if abs_path in all_files_abs:
                        changed_files.add(abs_path)

                mode = f"git-diff ({last_sha[:7]}→{current_sha[:7]})"
            else:
                # git 可用但无法获取 SHA（初始仓库等边界情况）→ mtime fallback
                changed_files = _mtime_changed_files(all_files, cache)
                mode = "mtime-fallback (no sha)"

            cache["last_scan_commit"] = current_sha
        else:
            # ── 非 git 仓库：mtime 全量检测（对应 aider mtime 缓存）──
            changed_files = _mtime_changed_files(all_files, cache)
            mode = "mtime (non-git)"

        # 清理缓存中已不存在的文件
        existing_abs = all_files_abs
        stale = [k for k in cache["entries"] if k not in existing_abs]
        for k in stale:
            del cache["entries"][k]

        print(f"[scan_and_rank] Mode: {mode}, changed: {len(changed_files)}, cached: {len(all_files) - len(changed_files)}")

    # ── 3. 构建 RepoMap（改用 refresh="files"，让 aider 内部 mtime 缓存生效）──
    io = MinimalIO()
    repo_map = RepoMap(
        map_tokens=map_tokens,
        root=str(proj),
        io=io,
        verbose=verbose,
        refresh="files",  # 修复：原来是 "always"，导致 aider mtime 缓存被绕过
    )

    # ── 4. 提取 tags（只重扫变化文件，其他用缓存）──
    print(f"[scan_and_rank] Extracting tags for {len(changed_files)} changed files ...")
    all_tags: list = []
    file_tag_counts: dict = {}

    for fname in all_files:
        rel = os.path.relpath(fname, str(proj))

        if fname in changed_files:
            # 重新提取 tags（参考 aider get_tags() 的缓存更新逻辑）
            tags = repo_map.get_tags(fname, rel)
            current_mtime = _get_file_mtime(fname)
            # 更新缓存（对应 aider: TAGS_CACHE[key] = {"mtime": ..., "data": ...}）
            cache["entries"][fname] = {
                "mtime": current_mtime,
                "tags": [{"rel_fname": t.rel_fname, "fname": t.fname, "line": t.line, "name": t.name, "kind": t.kind} for t in tags],
            }
        else:
            # 使用缓存的 tags（对应 aider: val.get("mtime") == file_mtime → return cached data）
            cached_entry = cache["entries"].get(fname, {})
            cached_tag_dicts = cached_entry.get("tags", [])
            # 转换为 Tag namedtuple 供 get_ranked_tags 使用
            from .repomap import Tag as RepoTag
            tags = [RepoTag(rel_fname=t["rel_fname"], fname=t["fname"], line=t["line"], name=t["name"], kind=t["kind"]) for t in cached_tag_dicts]
            # 同时让 aider 内部缓存感知到这些 tag（写入 TAGS_CACHE 避免内部重新提取）
            current_mtime = _get_file_mtime(fname)
            if current_mtime is not None:
                repo_map.TAGS_CACHE[fname] = {"mtime": current_mtime, "data": tags}

        def_count = sum(1 for t in tags if t.kind == "def")
        file_tag_counts[rel] = def_count
        for t in tags:
            all_tags.append({
                "rel_fname": t.rel_fname,
                "fname": t.fname,
                "line": t.line,
                "name": t.name,
                "kind": t.kind,
            })

    # ── 5. PageRank 排序 ──
    tags_path = out_data / "tags.json"
    ranked_path = out_data / "ranked.json"
    meta_path = out_data / "scan_meta.json"

    # Skip PageRank + file writes when nothing changed and outputs exist.
    # PageRank is non-deterministic (float precision + dict iteration order),
    # so re-running it with identical tags produces slightly different ranks,
    # which would invalidate the incremental analysis hash comparison.
    if not changed_files and ranked_path.exists() and tags_path.exists():
        print("[scan_and_rank] No changed files — reusing existing ranked.json and tags.json")
        ranked_files = json.loads(ranked_path.read_text(encoding="utf-8"))
    else:
        print("[scan_and_rank] Running PageRank ...")
        ranked_tags = repo_map.get_ranked_tags(
            chat_fnames=[],
            other_fnames=all_files,
            mentioned_fnames=set(),
            mentioned_idents=set(),
        )

        ranked_files: list = []
        seen: set = set()
        for i, tag in enumerate(ranked_tags):
            rel = tag[0]
            if rel in seen:
                continue
            seen.add(rel)
            ranked_files.append({
                "rank": i + 1,
                "rel_fname": rel,
                "def_count": file_tag_counts.get(rel, 0),
                "pagerank_position": i + 1,
            })

        # ── 6. 写输出文件 ──
        tags_path.write_text(json.dumps(all_tags, ensure_ascii=False, indent=2), encoding="utf-8")
        ranked_path.write_text(json.dumps(ranked_files, ensure_ascii=False, indent=2), encoding="utf-8")

    dir_structure: dict = {}
    for rf in ranked_files:
        rel = rf["rel_fname"]
        parent = str(Path(rel).parent)
        if parent == ".":
            parent = ""
        dir_structure.setdefault(parent, []).append(rel)

    cache["project_path"] = str(proj)
    meta = {
        "project_path": str(proj),
        "output_dir": str(output_dir),
        "exclude_dirs": exclude_dirs,
        "total_files": len(all_files),
        "total_tags": len(all_tags),
        "total_ranked": len(ranked_files),
        "dir_structure": dir_structure,
        "last_scan_commit": cache.get("last_scan_commit"),
    }
    meta_path.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")

    # ── 7. 写回 tags_cache.json ──
    _save_tags_cache(cache_path, cache)

    # ── 8. 生成摘要 ──
    changed_count = len(changed_files)
    cached_count = len(all_files) - changed_count
    summary = (
        f"Scanned {len(all_files)} files "
        f"({changed_count} changed, {cached_count} cached), "
        f"extracted {len(all_tags)} tags, "
        f"ranked {len(ranked_files)} files.\n"
        f"Output: {out_data}"
    )
    print(f"[scan_and_rank] {summary}")
    return summary


def _mtime_changed_files(all_files: list, cache: dict) -> set:
    """
    通过 mtime 检测变化文件（fallback 路径，对应 aider get_tags() mtime 检测）。
    返回需要重新提取 tags 的文件绝对路径集合。
    """
    changed = set()
    entries = cache.get("entries", {})
    for fname in all_files:
        cached_mtime = entries.get(fname, {}).get("mtime")
        current_mtime = _get_file_mtime(fname)
        if current_mtime is None:
            continue
        # 对应 aider: if val is not None and val.get("mtime") == file_mtime → use cache
        if cached_mtime != current_mtime:
            changed.add(fname)
    return changed
