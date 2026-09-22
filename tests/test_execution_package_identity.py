from __future__ import annotations

import re
import subprocess
import sys
import textwrap
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_execution_is_the_only_shared_runtime_package() -> None:
    assert (ROOT / "src" / "execution").is_dir()
    assert not (ROOT / "src" / "runtime").exists()

    result = subprocess.run(
        [
            sys.executable,
            "-c",
            textwrap.dedent(
                """
                import importlib.util
                import agentloom.execution

                assert importlib.util.find_spec("agentloom.runtime") is None
                """
            ),
        ],
        cwd=ROOT,
        text=True,
        capture_output=True,
        timeout=60,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_execution_package_has_no_legacy_namespace_imports() -> None:
    offenders = []
    for source_path in (ROOT / "src" / "execution").rglob("*.py"):
        source = source_path.read_text(encoding="utf-8")
        if re.search(r"\bagentloom\.runtime(?:\.|\b)", source):
            offenders.append(str(source_path.relative_to(ROOT)))

    assert offenders == []


def test_execution_package_does_not_import_runtime_adapters() -> None:
    offenders = []
    for source_path in (ROOT / "src" / "execution").rglob("*.py"):
        source = source_path.read_text(encoding="utf-8")
        if re.search(r"\bagentloom\.runtimes(?:\.|\b)", source):
            offenders.append(str(source_path.relative_to(ROOT)))

    assert offenders == []
