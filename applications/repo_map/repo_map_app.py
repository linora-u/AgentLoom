#!/usr/bin/env python3
"""
repo_map_app — AI-powered codebase architecture analysis tool.

Inspired by aider's repo map concept (https://github.com/Aider-AI/aider),
which uses tree-sitter + PageRank to build a concise code map for LLM context.

This tool extends that idea with:
  - Directory-level LLM architecture analysis (aider only does file-level algorithmic mapping)
  - Bottom-Up hierarchical analysis: child directories are analyzed first,
    parent directories reuse children's analysis.md for cross-layer insights
  - Incremental detection: MD5 hash on index.md content (index_md_hash) and
    children's analysis results (children_hash) to skip unchanged directories
  - Crash recovery: in_progress states are auto-reset on restart

Pipeline (3 steps):
  Step 1 — scan_and_rank (pure Python, zero LLM, incremental via git diff / mtime)
    tree-sitter AST parsing → extract definitions & references → PageRank file ranking
    Based on: https://github.com/Aider-AI/aider/blob/main/aider/repomap.py

  Step 2 — generate_markdown_map (pure Python, zero LLM)
    Create directory-mirrored index.md files with file definitions, importance stars,
    and cross-file references. Track index_md_hash for incremental analysis.

  Step 3 — LLM architecture analysis (Agent-driven, per-directory)
    Bottom-Up order (deepest first). Each directory gets a 5-dimension analysis:
    core function, key modules, design patterns, dependencies, notes.
    Parent directories receive children_analyses (full text of child analysis.md)
    for cross-layer architectural reasoning.

  Step 4 — prepare Skill workspace (pure Python)
    Copy repo_map docs and generate manifest/resolver/context.

  Step 5 — write and validate Skill files (pure Python)
    Generate deterministic SKILL.md and examples, then validate the package.

Output structure:
  <output_dir>/
  ├── data/
  │   ├── tags.json              # All extracted code symbols
  │   ├── ranked.json            # PageRank-sorted files
  │   ├── tags_cache.json        # Incremental scan cache
  │   ├── scan_meta.json         # Scan metadata
  │   └── analysis_progress.json # Per-directory status + hashes
  └── repo_map/
      ├── index.md               # Root: top files + directory tree
      ├── analysis.md             # Root architecture analysis
      ├── dependencies.md         # Cross-file dependency graph
      ├── <dir>/index.md          # Per-directory file listing
      └── <dir>/analysis.md       # Per-directory LLM analysis

Usage:
    # Basic: scan project, output to <project>/.repo_map/
    .venv/bin/python applications/repo_map/repo_map_app.py /path/to/project

    # With custom output dir and excluded directories
    .venv/bin/python applications/repo_map/repo_map_app.py /path/to/project \\
        --output_dir /tmp/mymap \\
        --skill_output_dir /path/to/project/.agents/skills \\
        --exclude_dirs vendor \\
        --exclude_dirs third_party
"""

import os
import sys

# Ensure project root is on sys.path.
project_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if project_root not in sys.path:
    sys.path.insert(0, project_root)

import fire
from pathlib import Path

from src.lib.logging import initialize_global_logger_once


# ---------------------------------------------------------------------------
# Parameter validation
# ---------------------------------------------------------------------------

def _validate_and_prepare(
    project_path: str,
    output_dir: str | None,
    exclude_dirs: list[str] | None,
    skill_output_dir: str | None,
) -> tuple[Path, Path, list[str], Path | None]:
    """
    Validate all inputs, check permissions, return normalised paths.

    Raises:
        FileNotFoundError: project_path does not exist.
        NotADirectoryError: project_path is not a directory.
        PermissionError: insufficient read/write permissions.
        ValueError: exclude_dirs contains invalid entries.
    """
    # 1. project_path
    proj = Path(project_path).resolve()
    if not proj.exists():
        raise FileNotFoundError(f"project_path does not exist: {proj}")
    if not proj.is_dir():
        raise NotADirectoryError(f"project_path is not a directory: {proj}")
    if not os.access(proj, os.R_OK):
        raise PermissionError(f"No read permission on project_path: {proj}")

    # 2. output_dir
    out = Path(output_dir).resolve() if output_dir else proj / ".repo_map"
    out.mkdir(parents=True, exist_ok=True)
    if not os.access(out, os.W_OK):
        raise PermissionError(f"No write permission on output_dir: {out}")

    # 3. skill_output_dir
    skill_out = Path(skill_output_dir).resolve() if skill_output_dir else None
    if skill_out is not None:
        skill_out.mkdir(parents=True, exist_ok=True)
        if not os.access(skill_out, os.W_OK):
            raise PermissionError(f"No write permission on skill_output_dir: {skill_out}")

    # 4. exclude_dirs
    cleaned: list[str] = []
    for raw in (exclude_dirs or []):
        d = raw.strip()
        if not d:
            continue
        p = Path(d)
        if p.is_absolute():
            raise ValueError(
                f"exclude_dirs must be relative paths, not absolute: '{d}'"
            )
        if ".." in p.parts:
            raise ValueError(
                f"exclude_dirs must not contain '..': '{d}'"
            )
        full = proj / d
        if full.exists() and not full.is_dir():
            raise ValueError(
                f"exclude_dirs entry exists but is not a directory: '{d}'"
            )
        cleaned.append(d)

    return proj, out, cleaned, skill_out


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main(
    project_path: str,
    output_dir: str = None,
    skill_output_dir: str = None,
    exclude_dirs: list[str] = None,
    log_to_file: bool = False,
    resume: str | None = None,
) -> None:
    """
    Scan a project and generate AI-readable code map Markdown files.

    The application coordinates:
      - step1: Scan project, extract code tags, run PageRank ranking
      - step2: Generate directory-mirrored Markdown files
      - step3: For-loop per-directory LLM architecture analysis
      - step4: Prepare generated Skill workspace
      - step5: Write and validate deterministic Skill files

    Args:
        project_path: Path to the project directory to scan (required).
        output_dir:   Where to write output files.
                      Defaults to <project_path>/.repo_map
        skill_output_dir:
                      Parent directory where the generated Skill package is written.
                      Defaults to <output_dir>/skills. The package name remains
                      <project-name>-repo-map-navigator.
        exclude_dirs: Directory names/paths to exclude (relative to project_path).
                      Can be specified multiple times:
                        --exclude_dirs vendor --exclude_dirs third_party
        log_to_file:  Deprecated. Kept for CLI compatibility.
        resume:       Deprecated. Incremental progress is driven by
                      analysis_progress.json in output_dir.

    Examples:
        # Basic
        python repo_map_app.py /path/to/my_project

        # With exclusions
        python repo_map_app.py /path/to/my_project \\
            --output_dir /tmp/map \\
            --skill_output_dir /path/to/my_project/.agents/skills \\
            --exclude_dirs vendor \\
            --exclude_dirs build \\
            --exclude_dirs third_party

        # Resume after interruption by rerunning the same command.
    """
    try:
        proj, out, excl, skill_out = _validate_and_prepare(
            project_path,
            output_dir,
            exclude_dirs,
            skill_output_dir,
        )
    except (FileNotFoundError, NotADirectoryError, PermissionError, ValueError) as e:
        print(f"[ERROR] {e}", file=sys.stderr)
        sys.exit(1)

    print(f"[repo_map] Project: {proj}")
    print(f"[repo_map] Output:  {out}")
    if skill_out:
        print(f"[repo_map] Skill output: {skill_out}")
    if excl:
        print(f"[repo_map] Exclude: {excl}")

    if log_to_file:
        print("[repo_map] Note: log_to_file is deprecated in direct orchestration mode.")
    if resume:
        print("[repo_map] Note: --resume is ignored; rerun uses analysis_progress.json.")

    initialize_global_logger_once("repo_map_agent")

    # ── Step 1/5: 扫描项目，提取 tags，PageRank 排序（纯 Python，零 LLM，支持增量） ──
    from applications.repo_map.agent_tools.scan_rank_tool import scan_and_rank
    print("[repo_map] Step 1/5: Scanning project...")
    scan_summary = scan_and_rank(
        project_path=str(proj),
        output_dir=str(out),
        exclude_dirs=excl,
        incremental=True,
    )
    print(f"[repo_map] {scan_summary}")

    # ── Step 2/5: 生成目录镜像 Markdown（纯 Python，零 LLM） ──
    from applications.repo_map.agent_tools.markdown_tool import generate_markdown_map
    print("[repo_map] Step 2/5: Generating Markdown map...")
    md_summary = generate_markdown_map(output_dir=str(out))
    print(f"[repo_map] {md_summary}")

    # 将 output_dir 写入文件，供工具链和增量运行读取。
    os.environ["REPO_MAP_OUTPUT_DIR"] = str(out)
    output_dir_marker = Path(out) / "data" / "output_dir.txt"
    output_dir_marker.write_text(str(out), encoding="utf-8")
    skill_output_dir_marker = Path(out) / "data" / "skill_output_dir.txt"
    if skill_out:
        os.environ["REPO_MAP_SKILL_OUTPUT_DIR"] = str(skill_out)
        skill_output_dir_marker.write_text(str(skill_out), encoding="utf-8")
    elif skill_output_dir_marker.exists():
        skill_output_dir_marker.unlink()

    from applications.repo_map.agent_tools.pipeline_agent_tools import (
        get_analysis_summary,
        prepare_repo_map_skill_workspace,
        run_analysis_loop,
        validate_repo_map_skill,
        write_repo_map_skill_files,
    )

    print("[repo_map] Step 3/5: Running LLM architecture analysis...")
    analysis_summary = run_analysis_loop(output_dir=str(out))
    print(f"[repo_map] {analysis_summary}")

    print("[repo_map] Step 4/5: Preparing Skill workspace...")
    prepare_summary = prepare_repo_map_skill_workspace(
        output_dir=str(out),
        skill_output_dir=str(skill_out) if skill_out else None,
    )
    print(f"[repo_map] {prepare_summary}")

    print("[repo_map] Step 5/5: Writing and validating Skill...")
    write_summary = write_repo_map_skill_files(output_dir=str(out))
    print(f"[repo_map] {write_summary}")
    validate_summary = validate_repo_map_skill(output_dir=str(out))
    print(f"[repo_map] {validate_summary}")

    final_summary = get_analysis_summary(output_dir=str(out))
    print(f"[repo_map] {final_summary}")


if __name__ == "__main__":
    fire.Fire(main)
