"""Path helpers for repo_map outputs."""

from __future__ import annotations

import json
import re
from pathlib import Path


def slugify_project_name(raw: str) -> str:
    """Normalize a project name to a skill-safe slug."""
    normalized = re.sub(r"[^a-z0-9]+", "-", raw.lower()).strip("-")
    return normalized or "project"


def project_name_from_meta(scan_meta: dict | None) -> str:
    """Derive the project name from scan metadata."""
    project_path = str((scan_meta or {}).get("project_path", "")).strip()
    return Path(project_path).name if project_path else "project"


def repo_map_skill_name(project_name: str) -> str:
    """Return the generated skill directory name, e.g. dialog-repo-map."""
    return f"{slugify_project_name(project_name)}-repo-map"


def _read_scan_meta(output_dir: Path) -> dict:
    meta_path = output_dir / "data" / "scan_meta.json"
    if not meta_path.exists():
        return {}
    try:
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        return meta if isinstance(meta, dict) else {}
    except (json.JSONDecodeError, OSError):
        return {}


def repo_map_skill_root(
    output_dir: str | Path,
    *,
    project_name: str | None = None,
    scan_meta: dict | None = None,
) -> Path:
    """Return the canonical repo_map skill root under output_dir."""
    out_path = Path(output_dir)
    if not project_name:
        if scan_meta is None:
            scan_meta = _read_scan_meta(out_path)
        project_name = project_name_from_meta(scan_meta)
    return out_path / repo_map_skill_name(project_name)


def repo_map_docs_root(
    output_dir: str | Path,
    *,
    project_name: str | None = None,
    scan_meta: dict | None = None,
) -> Path:
    """Return the canonical repo_map documentation root inside the Skill."""
    return (
        repo_map_skill_root(
            output_dir,
            project_name=project_name,
            scan_meta=scan_meta,
        )
        / "references"
        / "repo_map"
    )
