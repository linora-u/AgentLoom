"""Public installation entrypoints cannot execute the withdrawn OpenClaw package."""
from __future__ import annotations

import subprocess
import sys
import types
import urllib.request

import pytest
from agentloom.__main__ import main
from click.testing import CliRunner


@pytest.fixture
def deny_external_execution(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("The installation entrypoint attempted an external operation")

    # Third-party Node/npm and OS/network calls are boundary faults. No real
    # package is imported, installed or executed even against the old code.
    module = types.ModuleType("nodejs_wheel")
    module.node = module.npm = forbidden
    monkeypatch.setitem(sys.modules, "nodejs_wheel", module)
    monkeypatch.setattr(subprocess, "Popen", forbidden)
    monkeypatch.setattr(urllib.request, "urlopen", forbidden)


def test_public_cli_rejects_execution_owner_installation_without_external_operations(deny_external_execution):
    result = CliRunner().invoke(main, ["runtime", "install", "execution-owner"])
    assert result.exit_code == 1
    assert "OpenClaw installation and execution are disabled" in result.output
    assert "source reference" in result.output
    assert not isinstance(result.exception, AssertionError)


def test_direct_installer_rejects_without_external_operations(deny_external_execution):
    from agentloom.execution.durable.openclaw_install import install_openclaw

    with pytest.raises(RuntimeError, match="OpenClaw installation and execution are disabled.*source reference"):
        install_openclaw()
