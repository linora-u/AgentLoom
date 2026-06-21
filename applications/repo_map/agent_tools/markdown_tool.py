"""
Markdown generation tool: wraps renderer.render_directory_map() for agent use.
"""

import os
from pathlib import Path

from .renderer import render_directory_map


def generate_markdown_map(
    output_dir: str,
) -> str:
    """
    Generate directory-mirrored Markdown code map files from scan results.

    Reads tags.json, ranked.json, and scan_meta.json from <output_dir>/data/,
    then creates a Markdown file for every directory in the scanned project,
    mirroring the project directory structure under <output_dir>/<project-name>-repo-map/.

    Each directory gets an index.md containing:
    - All file definitions (functions, classes, structs) with line numbers
    - File importance (PageRank stars)
    - Cross-file reference information

    Also creates:
    - <output_dir>/<project-name>-repo-map/references/repo_map/index.md — project overview
    - <output_dir>/<project-name>-repo-map/references/repo_map/dependencies.md — dependency relationships
    - <output_dir>/data/analysis_progress.json — per-directory status for step3

    Args:
        output_dir: The output directory (same as passed to scan_and_rank).
                    Must contain data/tags.json and data/ranked.json.

    Returns:
        A summary of generated files, e.g.:
        "Generated 18 files:
          /path/.repo_map/my-project-repo-map/references/repo_map/index.md
          /path/.repo_map/my-project-repo-map/references/repo_map/src/index.md
          ..."

    Raises:
        FileNotFoundError: If tags.json or ranked.json are missing.
    """
    data_dir = Path(output_dir) / "data"
    ranked_file = data_dir / "ranked.json"
    tags_file = data_dir / "tags.json"

    if not ranked_file.exists():
        raise FileNotFoundError(
            f"ranked.json not found at {ranked_file}. "
            "Run scan_and_rank() first."
        )
    if not tags_file.exists():
        raise FileNotFoundError(
            f"tags.json not found at {tags_file}. "
            "Run scan_and_rank() first."
        )

    return render_directory_map(
        ranked_file=str(ranked_file),
        tags_file=str(tags_file),
        output_dir=output_dir,
    )
