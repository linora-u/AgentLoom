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
        --exclude_dirs vendor \\
        --exclude_dirs third_party
"""

import json
import os
import sys

# Ensure project root is on sys.path.
project_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if project_root not in sys.path:
    sys.path.insert(0, project_root)

import fire
from pathlib import Path

from src.runner import run_app


# ---------------------------------------------------------------------------
# Parameter validation
# ---------------------------------------------------------------------------

def _validate_and_prepare(
    project_path: str,
    output_dir: str | None,
    exclude_dirs: list[str] | None,
) -> tuple[Path, Path, list[str]]:
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

    # 3. exclude_dirs
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

    return proj, out, cleaned


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main(
    project_path: str,
    output_dir: str = None,
    exclude_dirs: list[str] = None,
    log_to_file: bool = False,
    resume: str | None = None,
) -> None:
    """
    Scan a project and generate AI-readable code map Markdown files.

    The Supervisor Agent coordinates:
      - step1: Scan project, extract code tags, run PageRank ranking
      - step2: Generate directory-mirrored Markdown files
      - step3: For-loop per-directory LLM architecture analysis

    Args:
        project_path: Path to the project directory to scan (required).
        output_dir:   Where to write output files.
                      Defaults to <project_path>/.repo_map
        exclude_dirs: Directory names/paths to exclude (relative to project_path).
                      Can be specified multiple times:
                        --exclude_dirs vendor --exclude_dirs third_party
        log_to_file:  Write logs to .logs/ directory. Default False.
        resume:       Resume from a checkpoint task ID. If provided, skips
                      step 1/2 and goes straight to LLM analysis with saved state.
                      Use ``loom list-tasks`` to find resumable task IDs.

    Examples:
        # Basic
        python repo_map_app.py /path/to/my_project

        # With exclusions
        python repo_map_app.py /path/to/my_project \\
            --output_dir /tmp/map \\
            --exclude_dirs vendor \\
            --exclude_dirs build \\
            --exclude_dirs third_party

        # Resume after interruption
        python repo_map_app.py /path/to/my_project --resume task_xxx
    """
    try:
        proj, out, excl = _validate_and_prepare(project_path, output_dir, exclude_dirs)
    except (FileNotFoundError, NotADirectoryError, PermissionError, ValueError) as e:
        print(f"[ERROR] {e}", file=sys.stderr)
        sys.exit(1)

    print(f"[repo_map] Project: {proj}")
    print(f"[repo_map] Output:  {out}")
    if excl:
        print(f"[repo_map] Exclude: {excl}")

    # ── Step 1/3: 扫描项目，提取 tags，PageRank 排序（纯 Python，零 LLM，支持增量） ──
    from applications.repo_map.agent_tools.scan_rank_tool import scan_and_rank
    print("[repo_map] Step 1/3: Scanning project...")
    scan_summary = scan_and_rank(
        project_path=str(proj),
        output_dir=str(out),
        exclude_dirs=excl,
        incremental=True,
    )
    print(f"[repo_map] {scan_summary}")

    # ── Step 2/3: 生成目录镜像 Markdown（纯 Python，零 LLM） ──
    from applications.repo_map.agent_tools.markdown_tool import generate_markdown_map
    print("[repo_map] Step 2/3: Generating Markdown map...")
    md_summary = generate_markdown_map(output_dir=str(out))
    print(f"[repo_map] {md_summary}")

    # ── Step 3/3: LLM 架构分析（启动 Agent，只做需要 LLM 的工作） ──
    # 将 output_dir 写入文件，供 tool_call 模式的 supervisor 读取
    os.environ["REPO_MAP_OUTPUT_DIR"] = str(out)
    output_dir_marker = Path(out) / "data" / "output_dir.txt"
    output_dir_marker.write_text(str(out), encoding="utf-8")

    print("[repo_map] Step 3/3: Running LLM architecture analysis...")

    # Build task_override with output_dir embedded (tool_call agents can't read env vars)
    task_with_output_dir = (
        f"执行 repo_map 架构分析工作流。output_dir={out}\n\n"
        f"请严格按照 workflow 中的步骤顺序执行，所有工具调用的 output_dir 参数值为：{out}"
    )
    run_app(
        "applications/repo_map/workflows/repo_map_agent.yaml",
        log_to_file=log_to_file,
        resume_task_id=resume,
        task_override=task_with_output_dir,
    )


if __name__ == "__main__":
    fire.Fire(main)
