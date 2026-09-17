"""Run genuine text/JSON/multi-worker ContextEngine acceptance in new directories.

The shared validator correlates source payload, ContextRef, retrieval event and
actual tool return. It never clears an existing runtime or accepts PASS text alone.
"""
from __future__ import annotations

import argparse
import subprocess
import sys
import tempfile
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--case", choices=["all", "text", "json", "multi"], default="all")
    parser.add_argument("--workspace", type=Path)
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[3]
    runner = root / "tests/acceptance/existing_application_validation.py"
    evidence = args.workspace or Path(tempfile.mkdtemp(prefix="agentloom-context-"))
    codes = []
    for case in ["text", "json", "multi"] if args.case == "all" else [args.case]:
        codes.append(subprocess.call([sys.executable, str(runner), "--case", f"context_{case}",
                                      "--workspace", str(evidence / case)], cwd=root))
    return int(any(codes))


if __name__ == "__main__":
    raise SystemExit(main())
