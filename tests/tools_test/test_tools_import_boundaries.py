"""Package import boundaries that must work in a fresh interpreter."""

from __future__ import annotations

import subprocess
import sys


def test_shell_audit_module_imports_without_preloading_agent_packages() -> None:
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "from src.tools.shell.shell_audit_log import ShellAuditLogger",
        ],
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr
