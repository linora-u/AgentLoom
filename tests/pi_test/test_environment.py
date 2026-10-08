"""Pi's narrow Node environment exceptions must not weaken subprocess policy."""
import os

import pytest

from agentloom.runtimes.pi.environment import build_pi_subprocess_env


@pytest.mark.parametrize("parent_settings", [
    {},
    {"NODE_USE_ENV_PROXY": "0"},
    {"NODE_EXTRA_CA_CERTS": "/tmp/custom CA.pem"},
    {"NODE_USE_ENV_PROXY": "1", "NODE_EXTRA_CA_CERTS": "/tmp/custom CA.pem"},
    {"NODE_USE_ENV_PROXY": "", "NODE_EXTRA_CA_CERTS": ""},
])
def test_pi_env_inherits_only_explicit_proxy_ca_settings(monkeypatch, parent_settings):
    for name in ("NODE_USE_ENV_PROXY", "NODE_EXTRA_CA_CERTS"):
        monkeypatch.delenv(name, raising=False)
    for name, value in parent_settings.items():
        monkeypatch.setenv(name, value)
    for name in ("NODE_OPTIONS", "NODE_PATH", "NODE_TLS_REJECT_UNAUTHORIZED",
                 "PI_CODING_AGENT_DIR", "PI_TEST_SETTING"):
        monkeypatch.setenv(name, "must-be-removed")
    monkeypatch.setenv("OPENAI_API_KEY", "must-be-removed")
    monkeypatch.setenv("HTTPS_PROXY", "http://proxy.example:8080")
    monkeypatch.setenv("HTTP_PROXY", "http://proxy.example:8081")
    monkeypatch.setenv("NO_PROXY", "localhost,127.0.0.1")
    monkeypatch.setenv("APP_SETTING", "unchanged")
    before = os.environ.copy()

    env = build_pi_subprocess_env()

    assert {k: v for k, v in env.items() if k.startswith(("PI_", "NODE_"))} == parent_settings
    assert "OPENAI_API_KEY" not in env
    for name in ("HTTPS_PROXY", "HTTP_PROXY", "NO_PROXY", "APP_SETTING"):
        assert env[name] == before[name]
    assert os.environ == before
