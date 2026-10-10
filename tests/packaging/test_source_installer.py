"""Exercise the source installer with an offline uv executable at its boundary."""

from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

import pytest


ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def installer(tmp_path: Path):
    checkout = tmp_path / "source checkout"
    checkout.mkdir()
    source_installer = checkout / "install"
    shutil.copy2(ROOT / "install", source_installer)
    shutil.copy2(ROOT / ".python-version", checkout / ".python-version")
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
    env.pop("UV_PROJECT_ENVIRONMENT", None)
    env.pop("UV_PYTHON", None)

    def run(*args: str, command: Path = source_installer,
            install_dir: Path | None = install_root, python: str | None = None):
        invocation_env = dict(env)
        if install_dir is None:
            invocation_env.pop("AGENTLOOM_INSTALL_DIR", None)
        else:
            invocation_env["AGENTLOOM_INSTALL_DIR"] = str(install_dir)
        if python is not None:
            invocation_env["UV_PYTHON"] = python
        return subprocess.run(
            [str(command), *args], cwd=tmp_path, env=invocation_env,
            capture_output=True, text=True, timeout=30,
        )

    return run, install_root, log, home, checkout


def test_source_install_provides_both_clis_without_environment_activation(installer):
    run, install_root, log, home, _ = installer
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
    assert "--inexact" not in invocation
    assert (install_root / "python-environment").read_text().strip() == str(install_root / "venv")
    assert not (home / ".zshrc").exists()


def test_update_preserves_the_selected_runtime_and_installs_its_sdk(installer):
    run, install_root, log, _, _ = installer
    result = run("--runtime", "pi", "--no-modify-path")
    assert result.returncode == 0, result.stdout + result.stderr
    assert '["-I", "-m", "agentloom", "runtime", "install", "pi"]' in result.stdout
    result = run("update", command=install_root / "bin" / "agentloom")
    assert result.returncode == 0, result.stdout + result.stderr
    assert '["-I", "-m", "agentloom", "runtime", "install", "pi"]' in result.stdout
    for invocation in map(json.loads, log.read_text().splitlines()):
        assert invocation[invocation.index("--extra") + 1] == "pi"
        assert "smol" not in invocation and "code" not in invocation


def test_default_install_uses_checkout_environment_when_called_elsewhere(installer):
    run, _, log, home, checkout = installer
    result = run("--no-modify-path", install_dir=None)
    assert result.returncode == 0, result.stdout + result.stderr
    install_root = checkout / ".agentloom"
    assert (checkout / ".venv/bin/python").is_file()
    assert (install_root / "python-environment").read_text().strip() == str(checkout / ".venv")
    assert (install_root / "source-root").read_text().strip() == str(checkout)
    assert not (home / ".agentloom").exists()
    assert not (checkout.parent / ".venv").exists()
    assert "--inexact" in json.loads(log.read_text().splitlines()[0])
    invocation = json.loads(log.read_text().splitlines()[0])
    assert "--managed-python" in invocation
    assert invocation[invocation.index("--python") + 1] == (checkout / ".python-version").read_text().strip()
    for name in ("agentloom", "loom"):
        result = run("--help", command=install_root / "bin" / name, install_dir=None)
        assert result.returncode == 0, result.stdout + result.stderr
        assert json.loads(result.stdout) == ["-I", "-m", "agentloom", "--help"]


def test_default_update_reuses_checkout_environment_and_runtime(installer):
    run, _, log, _, checkout = installer
    result = run("--runtime", "pi", "--no-modify-path", install_dir=None)
    assert result.returncode == 0, result.stdout + result.stderr
    install_root = checkout / ".agentloom"
    result = run("update", command=install_root / "bin" / "loom", install_dir=None)
    assert result.returncode == 0, result.stdout + result.stderr
    assert '["-I", "-m", "agentloom", "runtime", "install", "pi"]' in result.stdout
    assert (install_root / "python-environment").read_text().strip() == str(checkout / ".venv")
    assert not (install_root / "venv").exists()
    invocations = [json.loads(line) for line in log.read_text().splitlines()]
    assert len(invocations) == 2
    assert all(command[command.index("--extra") + 1] == "pi" for command in invocations)
    assert all("--inexact" in command for command in invocations)


def test_explicit_python_selection_is_left_to_uv(installer):
    run, _, log, _, _ = installer
    result = run("--no-modify-path", python="/explicit/python")
    assert result.returncode == 0, result.stdout + result.stderr
    invocation = json.loads(log.read_text().splitlines()[0])
    assert "--managed-python" not in invocation
    assert "--python" not in invocation
