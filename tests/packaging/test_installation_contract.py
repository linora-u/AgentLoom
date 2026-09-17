from __future__ import annotations

import subprocess
import sys
from pathlib import Path


def test_installed_package_executes_external_generated_application(tmp_path: Path) -> None:
    result = subprocess.run(
        [sys.executable, str(Path(__file__).with_name('installation_probe.py')),
         '--workspace', str(tmp_path / 'probe')],
        cwd=tmp_path, text=True, capture_output=True, timeout=120,
    )
    assert result.returncode == 0, result.stdout + result.stderr
