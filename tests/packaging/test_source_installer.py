"""Exercise the source installer with an offline uv executable at its boundary."""

from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys

import pytest


ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def installer(tmp_path: Path):
    tools = tmp_path / "tools"
    tools.mkdir()
    home = tmp_path / "home"
    home.mkdir()
    install_root = tmp_path / "installed environment"
    log = tmp_path / "uv.jsonl"
    interpreter = f"#!{sys.executable}\nimport json, sys\nprint(json.dumps(sys.argv[1:]))\n"
    uv = tools / "uv"
    uv.write_text(
        f"#!{sys.executable}\n"
        "import json, os, pathlib, sys\n"
        "with open(os.environ['INSTALLER_TEST_LOG'], 'a') as log:\n"
        "    log.write(json.dumps(sys.argv[1:]) + '\\n')\n"
        "binary = pathlib.Path(os.environ['UV_PROJECT_ENVIRONMENT']) / 'bin'\n"
        "binary.mkdir(parents=True, exist_ok=True)\n"
        f"(binary / 'python').write_text({interpreter!r})\n"
        "(binary / 'python').chmod(0o755)\n"
    )
    uv.chmod(0o755)
    unrelated_tool = tools / "curl"
    unrelated_tool.write_text("#!/bin/sh\nexit 99\n")
    unrelated_tool.chmod(0o755)
    env = {
        **os.environ,
        "HOME": str(home),
        "PATH": os.pathsep.join((str(tools), "/usr/bin", "/bin")),
        "AGENTLOOM_INSTALL_DIR": str(install_root),
        "INSTALLER_TEST_LOG": str(log),
    }

    def run(*args: str, command: Path = ROOT / "install"):
        return subprocess.run(
            [str(command), *args], cwd=tmp_path, env=env,
            capture_output=True, text=True, timeout=30,
        )

    return run, install_root, log, home


def test_source_install_provides_both_clis_without_environment_activation(installer):
    run, install_root, log, home = installer
    result = run("--no-modify-path")
    assert result.returncode == 0, result.stdout + result.stderr
    for command in ("agentloom", "loom"):
        result = run("run", "application with spaces.yaml", command=install_root / "bin" / command)
        assert result.returncode == 0, result.stdout + result.stderr
        assert json.loads(result.stdout) == ["-I", "-m", "agentloom", "run", "application with spaces.yaml"]
    invocation = json.loads(log.read_text().splitlines()[0])
    assert "--no-editable" in invocation and "--no-dev" in invocation
    assert invocation[invocation.index("--extra") + 1] == "smol"
    assert "code" in invocation
    assert not (home / ".zshrc").exists()


def test_update_preserves_the_selected_runtime_and_installs_its_sdk(installer):
    run, install_root, log, _ = installer
    result = run("--runtime", "pi", "--no-modify-path")
    assert result.returncode == 0, result.stdout + result.stderr
    assert '["-I", "-m", "agentloom", "runtime", "install", "pi"]' in result.stdout
    result = run("update", command=install_root / "bin" / "agentloom")
    assert result.returncode == 0, result.stdout + result.stderr
    assert '["-I", "-m", "agentloom", "runtime", "install", "pi"]' in result.stdout
    for invocation in map(json.loads, log.read_text().splitlines()):
        assert invocation[invocation.index("--extra") + 1] == "pi"
        assert "smol" not in invocation and "code" not in invocation
