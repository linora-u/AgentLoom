"""Run the supported suite in a clean worktree and reject legacy runtime roots."""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
FORBIDDEN_ROOTS = (".runtime", ".logs")


def _run(command: list[str], *, cwd: Path) -> None:
    subprocess.run(command, cwd=cwd, check=True)


def validate(revision: str) -> dict[str, object]:
    resolved_revision = subprocess.check_output(
        ["git", "rev-parse", revision],
        cwd=ROOT,
        text=True,
    ).strip()
    with tempfile.TemporaryDirectory(prefix="agentloom-clean-checkout-") as temp:
        checkout = Path(temp) / "AgentLoom"
        _run(
            ["git", "worktree", "add", "--detach", str(checkout), resolved_revision],
            cwd=ROOT,
        )
        try:
            shutil.copyfile(
                checkout / "config" / "llm.example.yaml",
                checkout / "config" / "llm.yaml",
            )
            _run(
                [
                    "uv",
                    "sync",
                    "--locked",
                    "--all-groups",
                    "--extra",
                    "smol",
                    "--extra",
                    "code",
                ],
                cwd=checkout,
            )
            _run(
                [
                    "uv",
                    "run",
                    "--locked",
                    "--extra",
                    "smol",
                    "--extra",
                    "code",
                    "loom",
                    "install-runtime",
                    "pi",
                ],
                cwd=checkout,
            )
            suite = subprocess.run(
                [
                    "uv",
                    "run",
                    "--locked",
                    "--extra",
                    "smol",
                    "--extra",
                    "code",
                    "pytest",
                    "tests/",
                    "applications/memory_feature_validation/scripts/test_offline_memory_campaign_contract.py",
                    "applications/memory_feature_validation/scripts/test_memory_review_campaign_capsule.py",
                    "applications/memory_feature_validation/scripts/test_memory_review_campaign_contract.py",
                    "-q",
                ],
                cwd=checkout,
                check=False,
            )
            forbidden = [
                str(checkout / name)
                for name in FORBIDDEN_ROOTS
                if (checkout / name).exists()
            ]
            if forbidden:
                raise AssertionError(
                    "supported suite created forbidden top-level runtime roots: "
                    + ", ".join(forbidden)
                )
            suite.check_returncode()
            return {
                "status": "passed",
                "revision": resolved_revision,
                "forbidden_roots": [],
            }
        finally:
            subprocess.run(
                ["git", "worktree", "remove", "--force", str(checkout)],
                cwd=ROOT,
                check=False,
            )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--revision", default="HEAD")
    args = parser.parse_args()
    print(json.dumps(validate(args.revision), sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
