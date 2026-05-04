from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path


def test_default_logger_fallback_demo_script_runs_and_writes_log():
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
    log_path = Path(log_file_line.split("log file path: ", 1)[1].strip())
    assert log_path.exists()

    content = log_path.read_text(encoding="utf-8")
    assert "[INFO]" in content
    assert "default logger fallback demo start" in content
    assert "default logger fallback active: True" in result.stdout
    assert "skip run: true" in result.stdout
