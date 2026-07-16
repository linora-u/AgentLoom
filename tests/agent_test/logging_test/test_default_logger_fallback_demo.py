from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path


def test_default_logger_fallback_demo_is_console_only_without_runtime_context():
    repo_root = Path(__file__).resolve().parents[3]
    demo_script = repo_root / "applications" / "test_demo" / "test_default_logger_fallback_demo.py"
    env = dict(os.environ)
    env["AGENT_LOOM_DEMO_NO_RUN"] = "1"

    result = subprocess.run(
        [sys.executable, str(demo_script)],
        cwd=repo_root,
        env=env,
        capture_output=True,
        text=True,
        timeout=20,
    )

    assert result.returncode == 0, result.stderr
    assert "log file path: " in result.stdout

    log_file_line = next(line for line in result.stdout.splitlines() if line.startswith("log file path: "))
    assert log_file_line == "log file path: None"
    assert "default logger fallback demo start" in result.stdout
    assert "default logger fallback active: True" in result.stdout
    assert "skip run: true" in result.stdout
