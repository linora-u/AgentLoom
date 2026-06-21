"""
Renderer: converts ranked tags data into a directory-mirrored Markdown structure.

Called by markdown_tool.generate_markdown_map().
"""

import hashlib
import json
import shutil
from collections import defaultdict
from pathlib import Path
from typing import Optional

from .paths import repo_map_docs_root, repo_map_skill_root


IMPORTANCE_STARS = ["", "★", "★★", "★★★", "★★★★", "★★★★★"]


def _migrate_legacy_analysis_files(legacy_root: Path, docs_root: Path) -> int:
    """Copy existing analysis.md files from a previous docs root to the canonical root."""
    if not legacy_root.exists() or legacy_root.resolve() == docs_root.resolve():
        return 0

    migrated = 0
    for legacy_analysis in legacy_root.rglob("analysis.md"):
        try:
            legacy_analysis.relative_to(docs_root)
            continue
        except ValueError:
            pass
        rel = legacy_analysis.relative_to(legacy_root)
        target = docs_root / rel
        if target.exists():
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(legacy_analysis, target)
        migrated += 1
    return migrated


def _cleanup_flat_skill_docs(skill_root: Path, docs_root: Path, dir_keys: list[str]) -> None:
    """Remove docs from the previous flat Skill layout after migration."""
    if not skill_root.exists():
        return

    for filename in ("index.md", "analysis.md", "dependencies.md"):
        path = skill_root / filename
        if path.exists():
            path.unlink()

    first_level_dirs = {
        Path(dir_key).parts[0]
        for dir_key in dir_keys
        if dir_key and dir_key != "(root)" and Path(dir_key).parts
    }
    reserved = {"references", "scripts", "assets", "agents"}
    for dirname in first_level_dirs:
        if dirname in reserved:
            continue
        path = skill_root / dirname
        if path.exists() and path.is_dir() and path.resolve() != docs_root.resolve():
            shutil.rmtree(path)

    old_meta = skill_root / "_repo_map"
    if old_meta.exists():
        shutil.rmtree(old_meta)


def _stars(rank: int, total: int) -> str:
    """Convert rank position to star rating."""
    if total == 0:
        return ""
    pct = 1.0 - (rank - 1) / total
    idx = max(1, min(5, int(pct * 5) + 1))
    return IMPORTANCE_STARS[idx]


def _safe_dir_name(rel_dir: str) -> str:
    """Convert relative directory path to a safe display name."""
    if not rel_dir:
        return "(root)"
    return rel_dir.replace("\\", "/")


def render_directory_map(
    ranked_file: str,
    tags_file: str,
    output_dir: str,
) -> str:
    """
    Read ranked.json + tags.json and generate Markdown files mirroring
    the project directory structure.

    For each directory that contains source files, creates:
      <output_dir>/<project-name>-repo-map/references/repo_map/<rel_dir>/index.md

    Also creates:
      <output_dir>/<project-name>-repo-map/references/repo_map/index.md   — project-level overview
      <output_dir>/<project-name>-repo-map/references/repo_map/dependencies.md — top cross-file dependencies

    Args:
        ranked_file: Path to ranked.json produced by scan_rank_tool.
        tags_file:   Path to tags.json produced by scan_rank_tool.
        output_dir:  Root output directory.

    Returns:
        Summary string listing all generated files.
    """
    ranked_path = Path(ranked_file)
    tags_path = Path(tags_file)
    meta_path = ranked_path.parent / "scan_meta.json"

    ranked: list[dict] = json.loads(ranked_path.read_text(encoding="utf-8"))
    all_tags: list[dict] = json.loads(tags_path.read_text(encoding="utf-8"))
    meta: dict = json.loads(meta_path.read_text(encoding="utf-8")) if meta_path.exists() else {}

    out_root = Path(output_dir)
    skill_root = repo_map_skill_root(out_root, scan_meta=meta)
    out_map = repo_map_docs_root(out_root, scan_meta=meta)
    legacy_out_map = out_root / "repo_map"
    out_map.mkdir(parents=True, exist_ok=True)

    total = len(ranked)
    project_name = Path(meta.get("project_path", "project")).name

    # Index tags by file
    file_defs: dict[str, list[dict]] = defaultdict(list)
    file_refs: dict[str, set] = defaultdict(set)
    for t in all_tags:
        if t["kind"] == "def":
            file_defs[t["rel_fname"]].append(t)
        elif t["kind"] == "ref" and t["line"] >= 0:
            file_refs[t["rel_fname"]].add(t["name"])

    # Build definition → file mapping for "referenced by" info
    def_to_files: dict[str, list[str]] = defaultdict(list)
    for t in all_tags:
        if t["kind"] == "def":
            def_to_files[t["name"]].append(t["rel_fname"])

    # Group ranked files by directory
    dir_files: dict[str, list[dict]] = defaultdict(list)
    for rf in ranked:
        rel = rf["rel_fname"]
        parent = str(Path(rel).parent)
        if parent == ".":
            parent = ""
        dir_files[parent].append(rf)

    generated_files: list[str] = []
    _index_md_hashes: dict[str, str] = {}  # dir_key -> md5 of index.md content

    # ------------------------------------------------------------------ #
    # 1. Per-directory index.md
    # ------------------------------------------------------------------ #
    for rel_dir, dir_ranked_files in sorted(dir_files.items()):
        # Stable sort: by rank first, then by filename for deterministic output
        dir_ranked_files = sorted(dir_ranked_files, key=lambda rf: (rf["rank"], rf["rel_fname"]))
        dir_out = out_map / rel_dir if rel_dir else out_map
        dir_out.mkdir(parents=True, exist_ok=True)
        md_path = dir_out / "index.md"

        lines: list[str] = []
        dir_display = _safe_dir_name(rel_dir) if rel_dir else f"{project_name}/ (root)"
        lines.append(f"# {dir_display}\n")

        # Count unique definitions (excluding duplicates and package/namespace declarations at line 0)
        total_defs = 0
        for rf in dir_ranked_files:
            _rel = rf["rel_fname"]
            _defs = file_defs.get(_rel, [])
            _parent = Path(_rel).parent.name if Path(_rel).parent.name else ""
            _seen_count: set[tuple[str, int]] = set()
            for _d in _defs:
                _k = (_d["name"], _d["line"])
                if _k in _seen_count:
                    continue
                # Skip package/namespace declarations at line 0
                # (e.g. Go package declarations, C++ namespace declarations)
                if _d["line"] == 0 and _d["name"] == _parent:
                    continue
                _seen_count.add(_k)
            total_defs += len(_seen_count)
        lines.append(f"- 文件数: {len(dir_ranked_files)}")
        lines.append(f"- 总定义数: {total_defs}\n")

        lines.append("## 文件列表（按重要性排序）\n")

        for rf in dir_ranked_files:
            rel = rf["rel_fname"]
            stars = _stars(rf["rank"], total)
            defs = file_defs.get(rel, [])

            lines.append(f"### {Path(rel).name} {stars}")
            lines.append(f"*{rel}*\n")

            if defs:
                # Deduplicate: same (name, line) can be captured by multiple
                # tree-sitter queries (e.g. type_spec + type_decl in Go, class + method in Python)
                seen: set[tuple[str, int]] = set()
                # Skip package/namespace declarations at line 0 whose name
                # matches the directory basename (Go package, C++ namespace convention)
                parent_name = Path(rel).parent.name if Path(rel).parent.name else ""
                for d in sorted(defs, key=lambda x: x["line"]):
                    key = (d["name"], d["line"])
                    if key in seen:
                        continue
                    seen.add(key)
                    # Skip package/namespace declarations (line 0, name = parent dir name)
                    if d["line"] == 0 and d["name"] == parent_name:
                        continue
                    lines.append(f"- `{d['name']}` (line {d['line'] + 1})")
            else:
                lines.append("- *(no definitions extracted)*")

            # "referenced by" — find files that reference symbols from this file
            our_def_names = {d["name"] for d in defs}
            referencing_files: set[str] = set()
            for sym in our_def_names:
                for other_rel in def_to_files.get(sym, []):
                    if other_rel != rel:
                        referencing_files.add(other_rel)
            # Also check files that import symbols defined here
            for other_rel, other_refs in file_refs.items():
                if other_rel != rel and our_def_names & other_refs:
                    referencing_files.add(other_rel)

            if referencing_files:
                # Deduplicate and show parent dir for ambiguous filenames
                seen_display: dict[str, str] = {}  # display_name -> rel_path
                for f in sorted(referencing_files):
                    fname = Path(f).name
                    if fname in seen_display:
                        # Ambiguous: replace with dir/filename for both
                        prev = seen_display[fname]
                        if "/" not in prev:
                            # Upgrade previous entry to include parent
                            old_key = fname
                            new_key = str(Path(prev).parent / fname) if "/" in prev else prev
                            seen_display[old_key] = prev  # keep the full path
                        seen_display[f] = f  # store full path for this one too
                    else:
                        seen_display[fname] = f

                # Build display: use short name if unique, parent/name if ambiguous
                name_count: dict[str, int] = defaultdict(int)
                for f in referencing_files:
                    name_count[Path(f).name] += 1

                sample_files = sorted(referencing_files)[:5]
                more = len(referencing_files) - len(sample_files)
                display_parts = []
                for f in sample_files:
                    fname = Path(f).name
                    if name_count[fname] > 1:
                        # Show parent_dir/filename for disambiguation
                        parent = Path(f).parent.name
                        display_parts.append(f"`{parent}/{fname}`")
                    else:
                        display_parts.append(f"`{fname}`")
                refs_str = ", ".join(display_parts)
                if more > 0:
                    refs_str += f" ... +{more} more"
                lines.append(f"\n*被引用于*: {refs_str}")

            lines.append("")

        md_content = "\n".join(lines)
        md_path.write_text(md_content, encoding="utf-8")
        generated_files.append(str(md_path))
        # Track index.md content hash for incremental analysis detection
        _dir_key = rel_dir if rel_dir else "(root)"
        _index_md_hashes[_dir_key] = hashlib.md5(md_content.encode("utf-8")).hexdigest()

    # ------------------------------------------------------------------ #
    # 2. Root index.md — project overview with PageRank top-20
    # ------------------------------------------------------------------ #
    root_index = out_map / "index.md"
    root_lines: list[str] = []
    root_lines.append(f"# Repo Map: {project_name}\n")

    root_lines.append("## 项目概览\n")
    root_lines.append(f"- 文件数: {meta.get('total_files', total)}")
    root_lines.append(f"- 总 tags: {meta.get('total_tags', len(all_tags))}")
    root_lines.append(f"- 目录数: {len(dir_files)}")
    exclude = meta.get("exclude_dirs", [])
    if exclude:
        root_lines.append(f"- 排除目录: {', '.join(exclude)}")
    root_lines.append("")

    root_lines.append("## 关键模块（PageRank 排序 top-20）\n")
    root_lines.append("| # | 文件 | 定义数 | 重要性 |")
    root_lines.append("|---|------|--------|--------|")
    for rf in ranked[:20]:
        stars = _stars(rf["rank"], total)
        def_count = len(file_defs.get(rf["rel_fname"], []))
        root_lines.append(
            f"| {rf['rank']} | `{rf['rel_fname']}` | {def_count} | {stars} |"
        )
    root_lines.append("")

    root_lines.append("## 目录结构\n")
    for rel_dir in sorted(dir_files.keys()):
        count = len(dir_files[rel_dir])
        indent = "  " * rel_dir.count("/") if rel_dir else ""
        display = _safe_dir_name(rel_dir) if rel_dir else "(root)"
        dir_link = (rel_dir + "/index.md") if rel_dir else "index.md"
        root_lines.append(f"{indent}- [{display}/]({dir_link}) — {count} 文件")
    root_lines.append("")

    root_index.write_text("\n".join(root_lines), encoding="utf-8")
    generated_files.insert(0, str(root_index))

    # ------------------------------------------------------------------ #
    # 3. dependencies.md — package-level dependency graph
    # ------------------------------------------------------------------ #
    deps_path = out_map / "dependencies.md"
    deps_lines: list[str] = []
    deps_lines.append("# 跨模块依赖关系\n")
    deps_lines.append("*基于符号定义/引用关系生成，按目录（模块）聚合*\n")

    # Build package-level dependency: pkg_a -> pkg_b means files in pkg_a
    # reference symbols defined in pkg_b
    pkg_deps: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    for rf in ranked:
        rel = rf["rel_fname"]
        defs = file_defs.get(rel, [])
        if not defs:
            continue
        our_names = {d["name"] for d in defs}
        src_pkg = str(Path(rel).parent) if str(Path(rel).parent) != "." else "(root)"
        for other_rel, other_refs in file_refs.items():
            if other_rel == rel:
                continue
            cnt = len(our_names & other_refs)
            if cnt > 0:
                caller_pkg = str(Path(other_rel).parent) if str(Path(other_rel).parent) != "." else "(root)"
                if caller_pkg != src_pkg:  # Only cross-module deps
                    pkg_deps[src_pkg][caller_pkg] += cnt

    # Sort packages by total incoming references (most depended-on first)
    pkg_incoming = sorted(
        pkg_deps.items(),
        key=lambda x: sum(x[1].values()),
        reverse=True,
    )

    deps_lines.append("## 核心模块依赖（被依赖最多）\n")
    deps_lines.append("| 模块 | 被引用次数 | 主要消费者 |")
    deps_lines.append("|------|-----------|-----------|")
    for src_pkg, consumers in pkg_incoming[:20]:
        total_refs = sum(consumers.values())
        top_consumers = sorted(consumers.items(), key=lambda x: -x[1])[:4]
        consumer_str = ", ".join(f"`{p}` ({n})" for p, n in top_consumers)
        deps_lines.append(f"| `{src_pkg}` | {total_refs} | {consumer_str} |")

    deps_lines.append("")

    # Show key file-level dependencies for top-10 files
    deps_lines.append("## 核心文件依赖（top-10 高引用文件）\n")
    for rf in ranked[:10]:
        rel = rf["rel_fname"]
        defs = file_defs.get(rel, [])
        if not defs:
            continue
        our_names = {d["name"] for d in defs}
        callers: dict[str, int] = defaultdict(int)
        for other_rel, other_refs in file_refs.items():
            if other_rel == rel:
                continue
            cnt = len(our_names & other_refs)
            if cnt > 0:
                callers[other_rel] += cnt
        if callers:
            total_refs = sum(callers.values())
            top_callers = sorted(callers.items(), key=lambda x: -x[1])[:4]
            # Use parent/filename for disambiguation
            caller_parts = []
            for c, n in top_callers:
                cname = Path(c).name
                cparent = Path(c).parent.name
                caller_parts.append(f"`{cparent}/{cname}` ({n})")
            caller_str = ", ".join(caller_parts)
            deps_lines.append(f"- **`{rel}`** ({total_refs} refs) ← {caller_str}")

    deps_path.write_text("\n".join(deps_lines), encoding="utf-8")
    generated_files.append(str(deps_path))

    migrated_analyses = _migrate_legacy_analysis_files(legacy_out_map, out_map)
    migrated_analyses += _migrate_legacy_analysis_files(skill_root, out_map)
    if legacy_out_map.exists() and legacy_out_map.resolve() != out_map.resolve():
        shutil.rmtree(legacy_out_map)
    _cleanup_flat_skill_docs(
        skill_root=skill_root,
        docs_root=out_map,
        dir_keys=[rel_dir if rel_dir else "(root)" for rel_dir in dir_files.keys()],
    )
    if migrated_analyses:
        print(f"[renderer] Migrated {migrated_analyses} legacy analysis.md files into {out_map}")

    # ------------------------------------------------------------------ #
    # 4. analysis_progress.json — state for step3 for loop
    # ------------------------------------------------------------------ #
    data_dir = Path(output_dir) / "data"

    # ── Incremental: load old progress to preserve completed analyses ──
    progress_path = data_dir / "analysis_progress.json"
    old_progress: dict[str, dict] = {}
    if progress_path.exists():
        try:
            old_progress = json.loads(progress_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            old_progress = {}

    progress: dict[str, dict] = {}
    reused, invalidated = 0, 0
    for rel_dir in sorted(dir_files.keys()):
        dir_key = rel_dir if rel_dir else "(root)"
        md_rel = (rel_dir + "/index.md") if rel_dir else "index.md"
        md_abs = str(out_map / rel_dir / "index.md") if rel_dir else str(out_map / "index.md")
        analysis_abs = (
            str(out_map / rel_dir / "analysis.md")
            if rel_dir
            else str(out_map / "analysis.md")
        )
        best_rank = min(rf["rank"] for rf in dir_files[rel_dir])
        new_hash = _index_md_hashes.get(dir_key, "")

        old_entry = old_progress.get(dir_key)
        if (
            old_entry
            and old_entry.get("status") == "completed"
            and old_entry.get("index_md_hash")
            and old_entry["index_md_hash"] == new_hash
        ):
            # Source unchanged — preserve completed analysis
            progress[dir_key] = {
                **old_entry,
                "md_file": md_abs,
                "md_rel": md_rel,
                "output": analysis_abs,
                "rank": best_rank,
                "file_count": len(dir_files[rel_dir]),
                "index_md_hash": new_hash,
            }
            reused += 1
        else:
            if old_entry and old_entry.get("status") == "completed":
                invalidated += 1
            progress[dir_key] = {
                "status": "pending",
                "md_file": md_abs,
                "md_rel": md_rel,
                "output": None,
                "rank": best_rank,
                "file_count": len(dir_files[rel_dir]),
                "index_md_hash": new_hash,
            }

    if reused or invalidated:
        print(f"[renderer] Incremental: {reused} dirs reused (unchanged), {invalidated} dirs invalidated (source changed)")

    progress_path.write_text(json.dumps(progress, ensure_ascii=False, indent=2), encoding="utf-8")
    generated_files.append(str(progress_path))

    summary = (
        f"Generated {len(generated_files)} files:\n"
        + "\n".join(f"  {f}" for f in generated_files[:10])
        + (f"\n  ... and {len(generated_files) - 10} more" if len(generated_files) > 10 else "")
    )
    return summary
