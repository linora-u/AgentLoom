#!/usr/bin/env python3
"""
LLM integration smoke test for repo_map_app on fixture sample_project.

Output is intentionally written inside repository (.repo_map/) so it can be
inspected manually and deleted by developers.
"""

from __future__ import annotations

import shutil
import sys
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[3]
FIXTURE_PROJECT = (
    REPO_ROOT / "applications" / "repo_map" / "tests" / "fixtures" / "sample_project"
)
OUTPUT_DIR = REPO_ROOT / ".repo_map" / "repo_map_fixture_smoke"


def _assert_required_outputs(output_dir: Path) -> None:
    skill_root = output_dir / "sample-project-repo-map"
    required_paths = [
        output_dir / "data" / "analysis_progress.json",
        skill_root,
        skill_root / "SKILL.md",
        skill_root / "references" / "repo_map" / "index.md",
        skill_root / "references" / "repo_map" / "dependencies.md",
        skill_root / "references" / "manifest.jsonl",
        skill_root / "scripts" / "resolve_repo_map_docs.py",
        skill_root / "assets" / "examples",
        skill_root / "agents" / "openai.yaml",
    ]
    missing = [str(p) for p in required_paths if not p.exists()]
    if missing:
        raise RuntimeError(
            "repo_map fixture smoke test failed, missing outputs:\n" + "\n".join(missing)
        )


def main() -> int:
    sys.path.insert(0, str(REPO_ROOT))
    from applications.repo_map.repo_map_app import main as repo_map_main

    if OUTPUT_DIR.exists():
        shutil.rmtree(OUTPUT_DIR)

    print(f"[run_demo] fixture project: {FIXTURE_PROJECT}")
    print(f"[run_demo] output dir:      {OUTPUT_DIR}")
    repo_map_main(
        project_path=str(FIXTURE_PROJECT),
        output_dir=str(OUTPUT_DIR),
        exclude_dirs=None,
    )

    _assert_required_outputs(OUTPUT_DIR)
    print("[run_demo] PASS: fixture smoke output + skill artifacts generated.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
