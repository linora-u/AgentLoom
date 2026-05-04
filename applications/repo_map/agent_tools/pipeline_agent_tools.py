"""
Pipeline Agent Tools for repo_map.

只封装需要控制流处理的操作：
- run_analysis_loop(): Python for 循环调用 dir_architecture_analysis 子 Agent，含断点续传和错误隔离
- prepare_repo_map_skill_workspace(): 组装 skill 工作区（拷贝 repo_map / 生成 manifest / resolver）
- validate_repo_map_skill(): 校验 skill 产物完整性和 frontmatter 规则
- get_analysis_summary(): 读取 progress.json，返回结构化总结报告

不需要封装的操作：
- scan_and_rank()：纯 Python，在 repo_map_app.py 直接调用
- generate_markdown_map()：纯 Python，在 repo_map_app.py 直接调Analysis Loop用

设计原则：
- 子 Agent 通过 YamlAgentFactory.create_agent_as_tool() 懒加载，避免重复初始化
- for 循环在 Python 层实现，比 LLM CodeAct 更可靠，错误处理精确
- 每次迭代立即写回 progress.json，防止进程崩溃丢失进度
- 失败目录记录 error_msg + error_trace，主 Agent 读摘要后决策
"""

from __future__ import annotations

import hashlib
import json
import re
import shutil
import textwrap
import traceback
from pathlib import Path

import yaml

from src.lib.logging import resolve_logger

# yaml 路径（相对于 AGENT_ROOT）
_DIR_ANALYSIS_YAML = "applications/repo_map/workflows/worker_agents/dir_architecture_analysis.yaml"
_SKILL_CONTEXT_FILENAME = "skill_build_context.json"


def _save_progress(progress_file: Path, progress: dict) -> None:
    """将 progress 写回 JSON 文件（每次迭代后立即调用，防崩溃）"""
    progress_file.write_text(
        json.dumps(progress, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


# ─────────────────────────────────────────────────────────────────── #
#  Hierarchy helpers
# ─────────────────────────────────────────────────────────────────── #


def _dir_depth(dir_path: str) -> int:
    """返回目录深度。(root) 视为 -1 以确保排在所有目录之后。"""
    if dir_path == "(root)":
        return -1
    return dir_path.count("/")


def _sort_bottom_up(progress: dict) -> list[str]:
    """
    按 Bottom-Up 顺序排序目录：深层目录优先，同深度按 rank 升序。

    确保子目录在父目录之前被分析，使父目录可以复用子目录的 analysis 结果。
    (root) 排在最后（depth = -1）。
    """
    return sorted(
        progress.keys(),
        key=lambda d: (-_dir_depth(d), progress[d].get("rank", 9999)),
    )


def _get_direct_children(dir_path: str, all_dirs: list[str]) -> list[str]:
    """
    获取某目录在 progress 中的直接子目录列表。

    判断标准：child 以 dir_path + "/" 开头，且 depth 恰好多 1。
    对于 (root)，直接子目录是所有 depth=0 的目录。
    """
    if dir_path == "(root)":
        return [d for d in all_dirs if d != "(root)" and "/" not in d]
    target_depth = dir_path.count("/") + 1
    prefix = dir_path + "/"
    return [d for d in all_dirs if d.startswith(prefix) and d.count("/") == target_depth]


def _collect_children_analyses(
    dir_path: str,
    progress: dict,
    out_path: Path,
) -> str:
    """
    收集某目录所有直接子目录的 analysis.md 全文，拼接为一个字符串。

    只收集 status=completed 且 analysis.md 文件存在的子目录。
    返回空字符串表示无子目录分析（叶子目录或子目录均未完成）。
    """
    all_dirs = list(progress.keys())
    children = _get_direct_children(dir_path, all_dirs)
    if not children:
        return ""

    parts = []
    for child in sorted(children):
        entry = progress.get(child, {})
        if entry.get("status") != "completed":
            continue
        # 定位子目录的 analysis.md
        if child == "(root)":
            analysis_file = out_path / "repo_map" / "analysis.md"
        else:
            analysis_file = out_path / "repo_map" / child / "analysis.md"
        if analysis_file.exists():
            content = analysis_file.read_text(encoding="utf-8").strip()
            if content:
                parts.append(content)

    return "\n\n---\n\n".join(parts)


def _compute_children_hash(
    dir_path: str,
    progress: dict,
    out_path: Path,
) -> str:
    """
    计算某目录所有直接子目录 analysis.md 内容的组合 MD5 hash。

    用于增量检测：子目录分析结果变化时，父目录需要重新分析。
    叶子目录（无子目录）返回空字符串。
    """
    all_dirs = list(progress.keys())
    children = _get_direct_children(dir_path, all_dirs)
    if not children:
        return ""

    h = hashlib.md5()
    for child in sorted(children):  # 排序保证确定性
        if child == "(root)":
            analysis_file = out_path / "repo_map" / "analysis.md"
        else:
            analysis_file = out_path / "repo_map" / child / "analysis.md"
        if analysis_file.exists():
            content = analysis_file.read_bytes()
            h.update(child.encode("utf-8"))
            h.update(content)
        else:
            # 文件不存在也参与 hash（区分"无文件"和"空文件"）
            h.update(child.encode("utf-8"))
            h.update(b"__MISSING__")
    return h.hexdigest()


def _group_by_depth(dirs_sorted: list[str], progress: dict) -> list[tuple[int, list[str]]]:
    """
    Group directories by depth for parallel execution.

    Returns list of (depth, [dir_paths]) tuples, deepest first.
    Within each group, directories are independent and can be analysed in parallel.
    Groups must be processed sequentially (children before parents).
    """
    from collections import defaultdict
    groups: dict[int, list[str]] = defaultdict(list)
    for d in dirs_sorted:
        depth = _dir_depth(d)
        groups[depth].append(d)
    # Sort by depth descending (deepest first, root last)
    return sorted(groups.items(), key=lambda x: -x[0])


# ─────────────────────────────────────────────────────────────────── #
#  Skill packaging helpers
# ─────────────────────────────────────────────────────────────────── #


def _slugify_name(raw: str) -> str:
    """Normalize project name to skill-safe slug (lowercase letters/digits/hyphen)."""
    normalized = re.sub(r"[^a-z0-9]+", "-", raw.lower()).strip("-")
    return normalized or "project"


def _read_json_file(path: Path, default: dict | None = None) -> dict:
    if not path.exists():
        return default or {}
    try:
        obj = json.loads(path.read_text(encoding="utf-8"))
        return obj if isinstance(obj, dict) else (default or {})
    except (json.JSONDecodeError, OSError):
        return default or {}


def _copy_repo_map_tree(repo_map_src: Path, repo_map_dst: Path) -> int:
    """
    Full-copy repo_map tree into skill references path.

    Returns:
        Number of files copied.
    """
    if repo_map_dst.exists():
        shutil.rmtree(repo_map_dst)
    repo_map_dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(repo_map_src, repo_map_dst)
    return sum(1 for p in repo_map_dst.rglob("*") if p.is_file())


def _write_manifest_jsonl(progress: dict, manifest_path: Path) -> int:
    """
    Write routing manifest for repo_map docs.

    Each line contains:
      - dir_path
      - index_path
      - analysis_path
      - rank
      - file_count
    """
    entries: list[dict] = []
    for dir_path, entry in sorted(
        progress.items(),
        key=lambda item: (item[1].get("rank", 999999), item[0]),
    ):
        if dir_path == "(root)":
            index_rel = "references/repo_map/index.md"
            analysis_rel = "references/repo_map/analysis.md"
        else:
            index_rel = f"references/repo_map/{dir_path}/index.md"
            analysis_rel = f"references/repo_map/{dir_path}/analysis.md"

        entries.append(
            {
                "dir_path": dir_path,
                "index_path": index_rel,
                "analysis_path": analysis_rel,
                "rank": entry.get("rank"),
                "file_count": entry.get("file_count"),
            }
        )

    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    with manifest_path.open("w", encoding="utf-8") as f:
        for item in entries:
            f.write(json.dumps(item, ensure_ascii=False) + "\n")
    return len(entries)


def _write_resolver_script(script_path: Path) -> None:
    """Generate helper script for exact/fallback repo_map doc resolution."""
    script = textwrap.dedent(
        """\
        #!/usr/bin/env python3
        \"\"\"Resolve repo_map docs for a given source file path.\"\"\"

        from __future__ import annotations

        import argparse
        import json
        from pathlib import Path


        def _normalize_relative_dir(source_path: str, source_root: str = "") -> Path:
            src = Path(source_path)
            rel = src

            if source_root:
                root = Path(source_root)
                try:
                    rel = src.resolve().relative_to(root.resolve())
                except Exception:
                    rel = src

            if rel.suffix:
                rel = rel.parent
            if str(rel) == ".":
                return Path("")
            return rel


        def _candidate_dirs(rel_dir: Path) -> list[str]:
            current = rel_dir
            dirs: list[str] = []
            while True:
                current_str = "" if str(current) in ("", ".") else current.as_posix()
                dirs.append(current_str if current_str else "(root)")
                if not current_str:
                    break
                parent = current.parent
                current = Path("") if str(parent) == "." else parent
            return dirs


        def resolve_repo_map_docs(
            source_path: str,
            source_root: str = "",
            repo_map_ref_root: str = "references/repo_map",
        ) -> list[dict]:
            rel_dir = _normalize_relative_dir(source_path=source_path, source_root=source_root)
            repo_root = Path(repo_map_ref_root)
            resolved: list[dict] = []

            for dir_key in _candidate_dirs(rel_dir):
                if dir_key == "(root)":
                    index_path = repo_root / "index.md"
                    analysis_path = repo_root / "analysis.md"
                else:
                    index_path = repo_root / dir_key / "index.md"
                    analysis_path = repo_root / dir_key / "analysis.md"
                if index_path.exists() or analysis_path.exists():
                    resolved.append(
                        {
                            "dir_path": dir_key,
                            "index_path": str(index_path),
                            "analysis_path": str(analysis_path),
                            "index_exists": index_path.exists(),
                            "analysis_exists": analysis_path.exists(),
                        }
                    )

            if not resolved:
                index_path = repo_root / "index.md"
                analysis_path = repo_root / "analysis.md"
                resolved.append(
                    {
                        "dir_path": "(root)",
                        "index_path": str(index_path),
                        "analysis_path": str(analysis_path),
                        "index_exists": index_path.exists(),
                        "analysis_exists": analysis_path.exists(),
                    }
                )

            return resolved


        def main() -> None:
            parser = argparse.ArgumentParser(description="Resolve repo_map docs for a source path")
            parser.add_argument("source_path", help="Source file path to resolve")
            parser.add_argument("--source-root", default="", help="Optional source root for relative conversion")
            parser.add_argument(
                "--repo-map-ref-root",
                default="references/repo_map",
                help="repo_map reference root inside skill package",
            )
            args = parser.parse_args()

            docs = resolve_repo_map_docs(
                source_path=args.source_path,
                source_root=args.source_root,
                repo_map_ref_root=args.repo_map_ref_root,
            )
            output = {
                "docs": docs,
                "dependencies_path": str(Path(args.repo_map_ref_root) / "dependencies.md"),
            }
            print(json.dumps(output, ensure_ascii=False, indent=2))


        if __name__ == "__main__":
            main()
        """
    )
    script_path.parent.mkdir(parents=True, exist_ok=True)
    script_path.write_text(script, encoding="utf-8")


def _build_skill_context(out_path: Path, scan_meta: dict) -> dict:
    """Build deterministic skill metadata from output_dir + scan_meta."""
    project_path = str(scan_meta.get("project_path", "")).strip()
    project_name = Path(project_path).name if project_path else "project"
    skill_name = f"{_slugify_name(project_name)}-repo-map-navigator"
    skill_root = out_path / "skills" / skill_name
    description = (
        f"Use when reading or changing code under {project_name} and you need "
        "repo_map routing from source paths to index.md/analysis.md, with "
        "parent/root fallback and cross-module dependency lookup."
    )

    return {
        "output_dir": str(out_path),
        "project_name": project_name,
        "source_root": project_path or "<unknown>",
        "skill_name": skill_name,
        "description": description,
        "skill_description": description,
        "skill_root": str(skill_root),
        "repo_map_ref_root": "references/repo_map",
        "manifest_rel_path": "references/manifest.jsonl",
        "resolver_script_rel_path": "scripts/resolve_repo_map_docs.py",
        "examples_rel_root": "assets/examples",
    }


def _get_skill_context_path(output_dir: str | Path) -> Path:
    return Path(output_dir) / "data" / _SKILL_CONTEXT_FILENAME


def _load_skill_context(output_dir: str) -> dict:
    context_path = _get_skill_context_path(output_dir)
    if not context_path.exists():
        raise FileNotFoundError(
            f"skill context not found at {context_path}. "
            "Run prepare_repo_map_skill_workspace() first."
        )
    context = _read_json_file(context_path, default={})
    if not context:
        raise RuntimeError(f"skill context is empty or invalid: {context_path}")
    return context


def _validate_skill_frontmatter(skill_md_text: str) -> None:
    """Ensure SKILL.md frontmatter only contains name + description."""
    parts = skill_md_text.split("---", 2)
    if len(parts) != 3:
        raise ValueError("SKILL.md must contain YAML frontmatter delimited by ---")
    frontmatter_obj = yaml.safe_load(parts[1])
    if not isinstance(frontmatter_obj, dict):
        raise ValueError("SKILL.md frontmatter must be a YAML mapping")
    keys = set(frontmatter_obj.keys())
    if keys != {"name", "description"}:
        raise ValueError(
            f"SKILL.md frontmatter must contain only name/description, got keys: {sorted(keys)}"
        )


# ─────────────────────────────────────────────────────────────────── #
#  Public API
# ─────────────────────────────────────────────────────────────────── #


def _process_batch_results(
    tasks: list[dict],
    results: list,
    progress: dict,
    progress_file: Path,
    stats: dict,
    logger,
    total: int,
) -> None:
    """
    统一处理 tool.batch() 返回的 TaskResult 列表。

    对每个 result：写 analysis.md（completed 时）、更新 progress 状态、更新统计、记日志。
    """
    task_lookup = {t["dir_path"]: t for t in tasks}

    for r in results:
        dir_path = r.task_id
        task = task_lookup.get(dir_path)
        if task is None:
            continue

        entry = progress[dir_path]
        analysis_file = Path(task["_analysis_file"])

        if r.status == "completed":
            analysis_file.parent.mkdir(parents=True, exist_ok=True)
            analysis_file.write_text(
                str(r.result) if r.result else "",
                encoding="utf-8",
            )
            entry["status"] = "completed"
            entry["output"] = str(analysis_file)
            entry["children_hash"] = task["_children_hash"]
            entry.pop("error_msg", None)
            entry.pop("error_trace", None)
            stats["completed"] += 1
            logger.info(
                f"[Analysis Loop] 完成: {dir_path} → {analysis_file} "
                f"({r.duration_seconds:.1f}s)"
            )
        else:
            entry["status"] = "failed"
            entry["error_msg"] = r.error or "unknown error"
            if r.error_trace:
                entry["error_trace"] = r.error_trace
            stats["failed"] += 1
            logger.error(f"[Analysis Loop] 失败: {dir_path} — {r.error}")

    _save_progress(progress_file, progress)

    overall_completed = sum(1 for v in progress.values() if v["status"] == "completed")
    pct = overall_completed / total * 100 if total else 0
    logger.info(f"[Analysis Loop] 总进度: {overall_completed}/{total} ({pct:.1f}%)")


def run_analysis_loop(
    output_dir: str,
    retry_failed: bool = False,
) -> str:
    """
    对每个目录调用 dir_architecture_analysis 子 Agent 进行架构分析。

    并发度由 Worker YAML 中的 ``concurrency`` 字段决定（默认 auto），
    框架通过 ``tool.batch()`` 自动并行执行，应用层无需手动管理线程。

    特性：
    - Bottom-Up 层级分析：深层目录先分析，父目录复用子目录的 analysis.md
    - children_hash 增量检测：子目录分析结果变化时，父目录自动重新分析
    - 断点续传：重置上次崩溃遗留的 in_progress 状态为 pending，从失败处重新开始
    - 错误隔离：单个目录失败不影响其他目录，失败信息记录到 progress.json
    - 立即持久化：每组完成后写回 progress.json，防止进程崩溃丢失进度
    - 返回摘要：包含完成/失败/跳过数量和失败目录的错误原因，供主 Agent 决策

    Args:
        output_dir:    输出目录，需包含 data/analysis_progress.json
        retry_failed:  是否重试之前失败的目录（默认 False，保留失败记录供人工检查）

    Returns:
        摘要字符串，如 "Analysis: 15 completed, 1 failed, 3 skipped."
        若有失败，摘要中包含失败目录和错误原因
    """
    progress_file = Path(output_dir) / "data" / "analysis_progress.json"

    if not progress_file.exists():
        raise FileNotFoundError(
            f"analysis_progress.json not found at {progress_file}. "
            "Run generate_markdown_map() first."
        )

    # 加载进度文件
    try:
        progress = json.loads(progress_file.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as e:
        raise RuntimeError(f"Failed to load progress file {progress_file}: {e}") from e

    logger = resolve_logger(None, "repo_map_agent")

    # create_agent_as_tool 内置缓存，同一 YAML 文件只创建一次
    # logger=None 让框架使用全局 AgentLogger（由 runner 初始化）
    from src.lib.smolagents.agent.yaml_agent_factory import YamlAgentFactory
    tool = YamlAgentFactory.create_agent_as_tool(_DIR_ANALYSIS_YAML)
    if tool is None:
        raise RuntimeError(f"Failed to create agent tool from {_DIR_ANALYSIS_YAML}")

    # ── 统计初始状态 ──
    total = len(progress)
    initial_completed = sum(1 for v in progress.values() if v["status"] == "completed")
    initial_failed = sum(1 for v in progress.values() if v["status"] == "failed")
    initial_pending = sum(1 for v in progress.values() if v["status"] == "pending")
    initial_in_progress = sum(1 for v in progress.values() if v["status"] == "in_progress")
    logger.info(
        f"[Analysis Loop] 启动 | 总计: {total} 目录 | 已完成: {initial_completed} | "
        f"待处理: {initial_pending} | 失败: {initial_failed} | 处理中(崩溃残留): {initial_in_progress}"
    )

    # ── 断点续传：重置上次崩溃遗留的 in_progress ──
    reset_count = 0
    for d in progress:
        if progress[d]["status"] == "in_progress":
            progress[d]["status"] = "pending"
            reset_count += 1
    if reset_count:
        _save_progress(progress_file, progress)
        logger.warning(f"[Analysis Loop] 崩溃恢复: 重置 {reset_count} 个 in_progress → pending")

    # ── 可选：重试失败的目录 ──
    if retry_failed:
        retry_count = 0
        for d in progress:
            if progress[d]["status"] == "failed":
                progress[d]["status"] = "pending"
                progress[d].pop("error_msg", None)
                progress[d].pop("error_trace", None)
                retry_count += 1
        if retry_count:
            _save_progress(progress_file, progress)
            logger.info(f"[Analysis Loop] 重试模式: 重置 {retry_count} 个 failed → pending")

    # ── Bottom-Up 排序 + 按深度分组 ──
    dirs_sorted = _sort_bottom_up(progress)
    to_process = sum(1 for d in dirs_sorted if progress[d]["status"] == "pending")
    logger.info(f"[Analysis Loop] 本轮预估需处理: {to_process} / {total} 目录 (排序: Bottom-Up)")

    stats = {"completed": 0, "failed": 0, "skipped": 0, "invalidated": 0}
    out_path = Path(output_dir)
    depth_groups = _group_by_depth(dirs_sorted, progress)

    for depth, group_dirs in depth_groups:
        # ── Phase A: 准备 task 列表（确定性操作，串行）──
        batch_tasks = []
        for dir_path in group_dirs:
            entry = progress[dir_path]
            status = entry["status"]

            # children_hash 增量检测
            current_children_hash = _compute_children_hash(dir_path, progress, out_path)
            stored_children_hash = entry.get("children_hash", "")

            if (
                status == "completed"
                and current_children_hash
                and current_children_hash != stored_children_hash
            ):
                entry["status"] = "pending"
                status = "pending"
                stats["invalidated"] += 1
                _save_progress(progress_file, progress)
                logger.info(
                    f"[Analysis Loop] 子目录变化检测: {dir_path} children_hash 变更 "
                    f"({stored_children_hash[:8]}.. → {current_children_hash[:8]}..) → 重置为 pending"
                )

            if status in ("completed", "failed"):
                stats["skipped"] += 1
                continue

            # 读 index.md
            if dir_path == "(root)":
                index_file = out_path / "repo_map" / "index.md"
                analysis_file = out_path / "repo_map" / "analysis.md"
            else:
                index_file = out_path / "repo_map" / dir_path / "index.md"
                analysis_file = out_path / "repo_map" / dir_path / "analysis.md"

            if not index_file.exists():
                entry["status"] = "failed"
                entry["error_msg"] = f"index.md not found: {index_file}"
                stats["failed"] += 1
                _save_progress(progress_file, progress)
                logger.warning(f"[Analysis Loop] 跳过 (无 index.md): {dir_path}")
                continue

            index_content = index_file.read_text(encoding="utf-8").strip()
            if not index_content:
                entry["status"] = "failed"
                entry["error_msg"] = f"index.md is empty: {index_file}"
                stats["failed"] += 1
                _save_progress(progress_file, progress)
                logger.warning(f"[Analysis Loop] 跳过 (空 index.md): {dir_path}")
                continue

            children_analyses = _collect_children_analyses(dir_path, progress, out_path)

            batch_tasks.append({
                "dir_path": dir_path,
                "index_content": index_content,
                "children_analyses": children_analyses,
                "_analysis_file": str(analysis_file),
                "_children_hash": current_children_hash,
            })

        if not batch_tasks:
            continue

        # ── Phase B: 执行分析 — tool.batch() 自动处理并发 ──
        logger.info(
            f"[Analysis Loop] 执行: {len(batch_tasks)} 个目录 (depth={depth})"
        )

        # Mark all as in_progress
        for t in batch_tasks:
            progress[t["dir_path"]]["status"] = "in_progress"
        _save_progress(progress_file, progress)

        if hasattr(tool, 'batch'):
            results = tool.batch(batch_tasks)
        else:
            # Fallback: 逐个调用（测试中 tool 可能无 batch 方法）
            from src.lib.concurrency.models import TaskResult
            import time as _time
            results = []
            for t in batch_tasks:
                _start = _time.monotonic()
                try:
                    res = tool(
                        dir_path=t["dir_path"],
                        index_content=t["index_content"],
                        children_analyses=t["children_analyses"],
                    )
                    results.append(TaskResult(
                        task_id=t["dir_path"], status="completed",
                        result=res, duration_seconds=_time.monotonic() - _start,
                    ))
                except Exception as e:
                    results.append(TaskResult(
                        task_id=t["dir_path"], status="failed",
                        error=str(e), error_trace=traceback.format_exc(),
                        duration_seconds=_time.monotonic() - _start,
                    ))

        # ── Phase C: 统一后处理 ──
        _process_batch_results(
            batch_tasks, results, progress, progress_file, stats, logger, total,
        )

    # ── 生成摘要 ──
    final_completed = sum(1 for v in progress.values() if v["status"] == "completed")
    final_pending = sum(1 for v in progress.values() if v["status"] == "pending")
    final_failed = sum(1 for v in progress.values() if v["status"] == "failed")
    pct = final_completed / total * 100 if total else 0
    logger.info(
        f"[Analysis Loop] 完成 | 本轮: +{stats['completed']} 完成, {stats['failed']} 失败, "
        f"{stats['skipped']} 跳过 | 累计: {final_completed}/{total} 已完成 ({pct:.1f}%), "
        f"{final_pending} 待处理, {final_failed} 失败"
        + (f", {stats['invalidated']} 因子目录变化重分析" if stats['invalidated'] else "")
    )

    summary_lines = [
        f"Analysis loop complete: "
        f"{stats['completed']} completed, "
        f"{stats['failed']} failed, "
        f"{stats['skipped']} skipped. "
        f"(Overall: {final_completed}/{total} done, {final_pending} pending, {final_failed} failed)"
    ]

    failed_dirs = [d for d, v in progress.items() if v["status"] == "failed"]
    if failed_dirs:
        summary_lines.append(
            f"\nFailed directories ({len(failed_dirs)}) — "
            "see data/analysis_progress.json for full error_trace:"
        )
        for d in failed_dirs:
            err = progress[d].get("error_msg", "unknown error")
            err_short = err[:200] + "..." if len(err) > 200 else err
            summary_lines.append(f"  - {d}: {err_short}")
        summary_lines.append(
            "\nTo retry failed directories, call run_analysis_loop(retry_failed=True)."
        )

    return "\n".join(summary_lines)


def prepare_repo_map_skill_workspace(output_dir: str) -> str:
    """
    Prepare deterministic skill workspace under <output_dir>/skills/.

    This tool only does deterministic file preparation:
      1) Build skill name/path metadata
      2) Full-copy <output_dir>/repo_map to references/repo_map
      3) Generate references/manifest.jsonl
      4) Generate scripts/resolve_repo_map_docs.py
      5) Persist context into data/skill_build_context.json

    Args:
        output_dir: Repo map output directory (contains data/ and repo_map/)

    Returns:
        Summary string with skill root path and artifact counts.
    """
    out_path = Path(output_dir)
    data_dir = out_path / "data"
    repo_map_dir = out_path / "repo_map"
    progress_file = data_dir / "analysis_progress.json"
    scan_meta_file = data_dir / "scan_meta.json"

    if not repo_map_dir.exists():
        raise FileNotFoundError(
            f"repo_map directory not found at {repo_map_dir}. Run generate_markdown_map() first."
        )
    if not progress_file.exists():
        raise FileNotFoundError(
            f"analysis_progress.json not found at {progress_file}. Run generate_markdown_map() first."
        )

    progress = _read_json_file(progress_file, default={})
    if not isinstance(progress, dict) or not progress:
        raise RuntimeError(f"analysis_progress.json is empty or invalid: {progress_file}")

    scan_meta = _read_json_file(scan_meta_file, default={})
    context = _build_skill_context(out_path=out_path, scan_meta=scan_meta)

    skill_root = Path(context["skill_root"])
    references_dir = skill_root / "references"
    scripts_dir = skill_root / "scripts"
    examples_dir = skill_root / "assets" / "examples"

    references_dir.mkdir(parents=True, exist_ok=True)
    scripts_dir.mkdir(parents=True, exist_ok=True)
    examples_dir.mkdir(parents=True, exist_ok=True)

    copied_files = _copy_repo_map_tree(
        repo_map_src=repo_map_dir,
        repo_map_dst=references_dir / "repo_map",
    )
    manifest_count = _write_manifest_jsonl(
        progress=progress,
        manifest_path=references_dir / "manifest.jsonl",
    )
    _write_resolver_script(scripts_dir / "resolve_repo_map_docs.py")

    context["copied_files"] = copied_files
    context["manifest_entries"] = manifest_count
    context_path = _get_skill_context_path(out_path)
    context_path.parent.mkdir(parents=True, exist_ok=True)
    context_path.write_text(
        json.dumps(context, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    logger = resolve_logger(None, "repo_map_agent")
    logger.info(
        "[Skill Workspace] prepared | "
        f"skill={skill_root} | copied_files={copied_files} | manifest_entries={manifest_count}"
    )

    lines = [
        "Skill workspace prepared:",
        f"Skill root: {skill_root}",
        f"Copied files: {copied_files}",
        f"Manifest entries: {manifest_count}",
        f"Context file: {context_path}",
    ]
    return "\n".join(lines)


def validate_repo_map_skill(output_dir: str) -> str:
    """
    Validate generated skill artifacts under <output_dir>/skills/.

    Validation checks:
      - Required files/directories exist
      - SKILL.md frontmatter contains only name/description
      - manifest entry count == analysis_progress directory count
      - assets/examples contains at least one markdown example

    Args:
        output_dir: Repo map output directory containing data/skill_build_context.json

    Returns:
        Summary string when all validation checks pass.
    """
    out_path = Path(output_dir)
    context = _load_skill_context(output_dir)

    skill_root = Path(str(context.get("skill_root", "")).strip())
    if not skill_root:
        raise RuntimeError("skill_root missing in skill_build_context.json")

    manifest_rel = str(context.get("manifest_rel_path", "references/manifest.jsonl"))
    resolver_rel = str(
        context.get("resolver_script_rel_path", "scripts/resolve_repo_map_docs.py")
    )
    repo_map_ref_root = str(context.get("repo_map_ref_root", "references/repo_map"))
    examples_rel_root = str(context.get("examples_rel_root", "assets/examples"))

    required_paths = [
        skill_root / "SKILL.md",
        skill_root / manifest_rel,
        skill_root / resolver_rel,
        skill_root / repo_map_ref_root / "index.md",
        skill_root / repo_map_ref_root / "dependencies.md",
    ]
    missing = [str(p) for p in required_paths if not p.exists()]
    if missing:
        raise FileNotFoundError("Missing required skill artifacts:\n" + "\n".join(missing))

    skill_md_text = (skill_root / "SKILL.md").read_text(encoding="utf-8")
    _validate_skill_frontmatter(skill_md_text)

    progress_file = out_path / "data" / "analysis_progress.json"
    progress = _read_json_file(progress_file, default={})
    if not isinstance(progress, dict):
        raise RuntimeError(f"Invalid analysis_progress.json: {progress_file}")

    manifest_path = skill_root / manifest_rel
    manifest_lines = [
        line.strip()
        for line in manifest_path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    if len(manifest_lines) != len(progress):
        raise ValueError(
            "Manifest entry count mismatch: "
            f"manifest={len(manifest_lines)} vs progress={len(progress)}"
        )
    for i, line in enumerate(manifest_lines):
        item = json.loads(line)
        for key in ("dir_path", "index_path", "analysis_path"):
            if key not in item:
                raise ValueError(f"manifest line {i + 1} missing required key: {key}")

    examples_dir = skill_root / examples_rel_root
    example_markdowns = sorted(examples_dir.glob("*.md")) if examples_dir.exists() else []
    if not example_markdowns:
        raise ValueError(f"No markdown examples found under {examples_dir}")

    lines = [
        "Skill validation passed:",
        f"Skill root: {skill_root}",
        f"Manifest entries: {len(manifest_lines)}",
        f"Examples markdown files: {len(example_markdowns)}",
    ]
    return "\n".join(lines)


def get_analysis_summary(output_dir: str) -> str:
    """
    读取 analysis_progress.json，返回结构化的最终总结报告。

    包含：完成/失败/总数统计、失败目录和错误原因、输出文件路径。
    供主 Agent 在所有步骤完成后调用，获取最终状态和交付物位置。

    Args:
        output_dir: 输出目录

    Returns:
        格式化的总结报告字符串
    """
    progress_file = Path(output_dir) / "data" / "analysis_progress.json"

    if not progress_file.exists():
        return f"No analysis_progress.json found at {output_dir}/data/. Pipeline may not have run."

    progress = json.loads(progress_file.read_text(encoding="utf-8"))
    total = len(progress)
    completed = sum(1 for v in progress.values() if v["status"] == "completed")
    failed = sum(1 for v in progress.values() if v["status"] == "failed")
    pending = sum(1 for v in progress.values() if v["status"] == "pending")

    lines = [
        "=" * 50,
        "Repo Map Generation Summary",
        "=" * 50,
        f"Total directories : {total}",
        f"Completed         : {completed}",
        f"Failed            : {failed}",
        f"Pending (unrun)   : {pending}",
        "",
        "Output location:",
        f"  {output_dir}/repo_map/index.md         (project overview)",
        f"  {output_dir}/repo_map/dependencies.md  (dependency graph)",
        f"  {output_dir}/data/analysis_progress.json (per-dir status)",
    ]

    failed_dirs = [(d, v) for d, v in progress.items() if v["status"] == "failed"]
    if failed_dirs:
        lines += ["", f"WARNING: {len(failed_dirs)} failed directories:"]
        for d, v in failed_dirs:
            err = v.get("error_msg", "unknown")[:150]
            lines.append(f"  - {d}: {err}")
        lines.append("To retry: call run_analysis_loop(output_dir, retry_failed=True)")
    else:
        lines.append("\nAll directories analyzed successfully!")

    return "\n".join(lines)
