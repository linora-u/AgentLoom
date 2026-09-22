from __future__ import annotations

import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]


def test_read_only_validation_import_does_not_load_agent_runtime() -> None:
    completed = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "import sys; "
                "import agentloom.application.validation; "
                "assert 'agentloom.application.agent' not in sys.modules; "
                "assert 'litellm' not in sys.modules"
            ),
        ],
        cwd=REPO_ROOT,
        text=True,
        capture_output=True,
        check=False,
        timeout=10,
    )

    assert completed.returncode == 0, completed.stderr


def test_read_only_studio_query_import_does_not_load_model_runtime() -> None:
    completed = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "import sys; "
                "from agentloom.application.studio.query_service import StudioQueryService; "
                "assert StudioQueryService; "
                "assert 'agentloom.application.agent' not in sys.modules; "
                "assert 'litellm' not in sys.modules"
            ),
        ],
        cwd=REPO_ROOT,
        text=True,
        capture_output=True,
        check=False,
        timeout=10,
    )

    assert completed.returncode == 0, completed.stderr
