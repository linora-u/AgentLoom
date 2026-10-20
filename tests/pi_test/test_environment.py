"""Pi's narrow Node environment exceptions must not weaken subprocess policy."""
import os

import pytest

from agentloom.runtimes.pi.environment import build_pi_subprocess_env
from agentloom.execution.subprocess_env import build_subprocess_env


@pytest.mark.parametrize("builder", [build_subprocess_env, build_pi_subprocess_env])
def test_news_credentials_are_filtered_without_changing_proxy_or_parent(monkeypatch, builder):
    credentials = {"ANYSEARCH_AUTH_PRIMARY": "test-primary", "ANYSEARCH_AUTH_BACKUP_1": "test-backup",
                   "ANYSEARCH_AUTH_BACKUP_10": "test-backup-10", "NEWS_LLM_API_KEY": "test-model-key"}
    for name, value in credentials.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setenv("HTTPS_PROXY", "http://proxy.example:8080")
    monkeypatch.setenv("SSL_CERT_FILE", "/tmp/cloud-ca.pem")
    monkeypatch.setenv("NEWS_LLM_BASE_URL", "https://llm.example/v1")
    before = os.environ.copy()
    env = builder()
    assert not credentials.keys() & env.keys()
    for name in ("HTTPS_PROXY", "SSL_CERT_FILE", "NEWS_LLM_BASE_URL"):
        assert env[name] == before[name]
    assert os.environ == before


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

    expected = {"NODE_USE_ENV_PROXY": "1", **parent_settings}
    assert {k: v for k, v in env.items() if k.startswith(("PI_", "NODE_"))} == expected
    assert "OPENAI_API_KEY" not in env
    for name in ("HTTPS_PROXY", "HTTP_PROXY", "NO_PROXY", "APP_SETTING"):
        assert env[name] == before[name]
    assert os.environ == before


def test_pi_env_leaves_direct_network_unchanged(monkeypatch):
    for name in ("HTTPS_PROXY", "HTTP_PROXY", "https_proxy", "http_proxy", "NODE_USE_ENV_PROXY"):
        monkeypatch.delenv(name, raising=False)
    assert "NODE_USE_ENV_PROXY" not in build_pi_subprocess_env()


def test_pi_env_supports_lowercase_proxy(monkeypatch):
    monkeypatch.delenv("NODE_USE_ENV_PROXY", raising=False)
    monkeypatch.setenv("https_proxy", "http://proxy.example:8080")
    assert build_pi_subprocess_env()["NODE_USE_ENV_PROXY"] == "1"


@pytest.mark.parametrize("version, supported", [
    ("v22.19.0", False), ("v22.20.0", False), ("v22.21.0", True),
    ("v23.11.0", False), ("v24.0.0", True), ("v25.0.0", True),
])
def test_find_node_requires_proxy_aware_version_only_when_enabled(monkeypatch, version, supported):
    from types import SimpleNamespace
    from agentloom.runtimes.pi import install

    monkeypatch.setattr(install.os, "get_exec_path", lambda env: ["/fixture"])
    monkeypatch.setattr(install.shutil, "which", lambda name, path: "/fixture/node")
    monkeypatch.setattr(install.subprocess, "run", lambda *a, **kw: SimpleNamespace(stdout=version))
    assert install.find_node({}) == "/fixture/node"
    if supported:
        assert install.find_node({"NODE_USE_ENV_PROXY": "1"}) == "/fixture/node"
    else:
        with pytest.raises(RuntimeError, match="environment proxy mode"):
            install.find_node({"NODE_USE_ENV_PROXY": "1"})
