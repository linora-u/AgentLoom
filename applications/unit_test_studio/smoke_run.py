#!/usr/bin/env python3
"""Run all five real Workers, then independently execute the generated pytest.

Outputs are written to a fresh controlled directory. Pass --workspace /new/path
for a stable evidence location; pre-existing generated tests are never removed.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path


def main() -> int:
    root = Path(__file__).resolve().parents[2]
    runner = root / "tests/acceptance/existing_application_validation.py"
    return subprocess.call([sys.executable, str(runner), "--case", "unit", *sys.argv[1:]], cwd=root)


if __name__ == "__main__":
    raise SystemExit(main())
