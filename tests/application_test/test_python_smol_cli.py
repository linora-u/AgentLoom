"""Default native lifecycle through the formal CLI and a real smol runtime."""
import json
import subprocess
import sys
from pathlib import Path

import pytest

from tests.application_test.test_python_process_owner import _alive

pytestmark = pytest.mark.skipif(sys.platform not in {"darwin", "linux"}, reason="managed POSIX execution")


@pytest.mark.parametrize("mode", ["foreground", "background", "timeout", "promotion", "cleanup-failure"])
def test_default_smol_cli_confirms_native_exit_before_terminal_receipt(tmp_path, mode):
    root = Path(__file__).resolve().parents[2]
    process = subprocess.run([sys.executable, "-m", "test_support.managed_smol_cli", str(tmp_path), "default-" + mode],
                             cwd=root, capture_output=True, text=True, timeout=40)
    (tmp_path / "stdout.log").write_text(process.stdout)
    (tmp_path / "stderr.log").write_text(process.stderr)
    evidence = json.loads((tmp_path / "evidence.json").read_text())
    assert "native" in evidence, process.stdout + process.stderr
    assert not _alive(evidence["native"])
    descriptions = list(tmp_path.rglob("native_resources/*/description.json"))
    assert descriptions, "Ordinary CLI execution must acquire the default native owner"
    for description in descriptions:
        anchor = json.loads(description.read_text())["anchor"]
        assert not _alive(anchor)
        assert json.loads(description.with_name("exited.json").read_text())["physical_exit_confirmed"]
    events = [json.loads(line) for line in process.stdout.splitlines()]
    if mode == "cleanup-failure":
        assert process.returncode != 0 and events[-1]["event"] == "run.failed"
        assert "injected-background-close-failure" in " ".join(evidence["runner_error_causes"])
    else:
        assert process.returncode == 0 and events[-1]["event"] == "run.completed", process.stderr
        assert "smol-scope-complete" in process.stdout
    if mode in {"background", "promotion", "cleanup-failure"}:
        assert len(evidence["background_tasks"]) == 1 and evidence["native_alive_before_final"]
    else:
        assert evidence["background_tasks"] == [] and not evidence["native_alive_before_final"]
