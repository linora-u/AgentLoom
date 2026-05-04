from __future__ import annotations

import subprocess
from pathlib import Path


def _tracked_files(repo_root: Path) -> list[str]:
    result = subprocess.run(
        ["git", "ls-files"],
        cwd=repo_root,
        check=True,
        capture_output=True,
        text=True,
    )
    return [line.strip() for line in result.stdout.splitlines() if line.strip()]


def test_legacy_app_reference_is_scoped_to_its_own_directory():
    repo_root = Path(__file__).resolve().parents[3]
    files = _tracked_files(repo_root)

    token = "haloos" + "_unit_test"
    allowed_prefix = f"applications/{token}/"

    violations: list[str] = []
    for rel in files:
        file_path = repo_root / rel
        if not file_path.exists():
            # Renames can leave stale index paths before staging; skip missing files.
            continue

        if token in rel and not rel.startswith(allowed_prefix):
            violations.append(f"path::{rel}")

        if rel.startswith(allowed_prefix):
            continue

        try:
            content = file_path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue

        if token in content:
            violations.append(f"content::{rel}")

    assert not violations, "Found external legacy references:\n" + "\n".join(sorted(violations))
