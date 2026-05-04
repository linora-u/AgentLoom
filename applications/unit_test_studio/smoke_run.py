#!/usr/bin/env python3
"""
Real LLM smoke run for Unit Test Studio.

This script intentionally runs the full supervisor workflow against fixture code,
then verifies generated pytest files under `applications/unit_test_studio/test/generated/`.
"""

from __future__ import annotations

import shutil
import sys
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]
APP_ROOT = Path(__file__).resolve().parent
FIXTURE_ROOT = APP_ROOT / "test" / "fixtures" / "sample_project"
GENERATED_ROOT = APP_ROOT / "test" / "generated"


def _assert_generated_outputs(output_dir: Path) -> None:
    generated_files = sorted(output_dir.glob("test_*.py"))
    if not generated_files:
        raise RuntimeError(
            f"No generated test files were found in {output_dir}. "
            "Expected at least one file from the real LLM run."
        )

    for file_path in generated_files:
        text = file_path.read_text(encoding="utf-8")
        if "import pytest" not in text:
            raise RuntimeError(f"Missing `import pytest` in generated file: {file_path}")
        if "@pytest.mark.parametrize" not in text:
            raise RuntimeError(
                f"Missing `@pytest.mark.parametrize` in generated file: {file_path}"
            )


def main() -> int:
    sys.path.insert(0, str(REPO_ROOT))
    from applications.unit_test_studio.studio_runner import run_unit_test_studio

    if GENERATED_ROOT.exists():
        shutil.rmtree(GENERATED_ROOT)
    GENERATED_ROOT.mkdir(parents=True, exist_ok=True)

    targets = (
        "test/fixtures/sample_project/src/text_pipeline.py:normalize_user_message,"
        "test/fixtures/sample_project/src/text_pipeline.py:extract_keywords"
    )

    print(f"[unit_test_studio.smoke_run] app root: {APP_ROOT}")
    print(f"[unit_test_studio.smoke_run] fixture root: {FIXTURE_ROOT}")
    print(f"[unit_test_studio.smoke_run] output root: {GENERATED_ROOT}")
    print(f"[unit_test_studio.smoke_run] targets: {targets}")

    report = run_unit_test_studio(
        target_path=str(APP_ROOT),
        targets=targets,
        output_dir="test/generated",
    )
    print("[unit_test_studio.smoke_run] report preview:")
    print(report[:1200])

    _assert_generated_outputs(GENERATED_ROOT)
    print("[unit_test_studio.smoke_run] PASS: generated test files validated.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
