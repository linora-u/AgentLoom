"""Tests for subprocess environment filtering (aligned with Claude Code subprocessEnv.ts).

Covers:
- Sensitive variable scrubbing (API keys, cloud secrets, CI tokens)
- Protective variable injection (GIT_EDITOR, AGENT_LOOM)
- Normal variable preservation (HOME, PATH, LANG, user-defined)
"""

import os

import pytest

from src.tools.shell.subprocess_env import build_subprocess_env, _SCRUB_EXACT, _INJECT


# ---------------------------------------------------------------------------
# Sensitive variable filtering — 6 cases
# ---------------------------------------------------------------------------

class TestSensitiveVarFiltering:
    """Sensitive variables must NOT appear in subprocess env."""

    def test_openai_api_key_filtered(self, monkeypatch):
        """18a: OPENAI_API_KEY is removed."""
        monkeypatch.setenv("OPENAI_API_KEY", "sk-test-secret-key")
        env = build_subprocess_env()
        assert "OPENAI_API_KEY" not in env

    def test_anthropic_api_key_filtered(self, monkeypatch):
        """18b: ANTHROPIC_API_KEY is removed."""
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test-key")
        env = build_subprocess_env()
        assert "ANTHROPIC_API_KEY" not in env

    def test_aws_secret_filtered(self, monkeypatch):
        """18c: AWS_SECRET_ACCESS_KEY is removed."""
        monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "wJalrXUtnFEMI/test")
        env = build_subprocess_env()
        assert "AWS_SECRET_ACCESS_KEY" not in env

    def test_ci_token_filtered(self, monkeypatch):
        """18d: ACTIONS_ID_TOKEN_REQUEST_TOKEN is removed."""
        monkeypatch.setenv("ACTIONS_ID_TOKEN_REQUEST_TOKEN", "gha-token-123")
        env = build_subprocess_env()
        assert "ACTIONS_ID_TOKEN_REQUEST_TOKEN" not in env

    def test_multiple_sensitive_vars_all_filtered(self, monkeypatch):
        """18e: Multiple sensitive variables are all removed at once."""
        secrets = {
            "OPENAI_API_KEY": "sk-1",
            "ANTHROPIC_API_KEY": "sk-2",
            "AWS_SECRET_ACCESS_KEY": "secret",
            "AZURE_API_KEY": "azure-key",
            "HF_TOKEN": "hf-token",
        }
        for k, v in secrets.items():
            monkeypatch.setenv(k, v)

        env = build_subprocess_env()
        for k in secrets:
            assert k not in env, f"{k} should have been scrubbed"

    def test_no_sensitive_vars_returns_normal(self, monkeypatch):
        """18f: When no sensitive vars exist, env is returned normally."""
        # Remove any sensitive vars that might exist
        for key in _SCRUB_EXACT:
            monkeypatch.delenv(key, raising=False)

        env = build_subprocess_env()
        # Should still have standard env vars
        assert "PATH" in env or "HOME" in env

    def test_scrub_list_completeness(self):
        """Verify the scrub list contains critical keys."""
        assert "OPENAI_API_KEY" in _SCRUB_EXACT
        assert "ANTHROPIC_API_KEY" in _SCRUB_EXACT
        assert "AWS_SECRET_ACCESS_KEY" in _SCRUB_EXACT
        assert "ACTIONS_ID_TOKEN_REQUEST_TOKEN" in _SCRUB_EXACT
        assert "ACTIONS_RUNTIME_TOKEN" in _SCRUB_EXACT


# ---------------------------------------------------------------------------
# Protective variable injection — 3 cases
# ---------------------------------------------------------------------------

class TestProtectiveVarInjection:
    """Protective variables must be injected into every subprocess env."""

    def test_git_editor_set_to_true(self, monkeypatch):
        """19a: GIT_EDITOR is set to 'true' to prevent interactive editors."""
        monkeypatch.delenv("GIT_EDITOR", raising=False)
        env = build_subprocess_env()
        assert env.get("GIT_EDITOR") == "true"

    def test_agent_loom_flag_set(self, monkeypatch):
        """19b: AGENT_LOOM=1 detection flag is set."""
        monkeypatch.delenv("AGENT_LOOM", raising=False)
        env = build_subprocess_env()
        assert env.get("AGENT_LOOM") == "1"

    def test_git_editor_overrides_user_value(self, monkeypatch):
        """19c: User's GIT_EDITOR=vim is overridden to 'true'."""
        monkeypatch.setenv("GIT_EDITOR", "vim")
        env = build_subprocess_env()
        assert env.get("GIT_EDITOR") == "true"


# ---------------------------------------------------------------------------
# Normal variable preservation — 4 cases
# ---------------------------------------------------------------------------

class TestNormalVarPreservation:
    """Non-sensitive variables must be preserved as-is."""

    def test_home_preserved(self, monkeypatch):
        """20a: HOME is preserved."""
        home = os.environ.get("HOME", "/tmp")
        monkeypatch.setenv("HOME", home)
        env = build_subprocess_env()
        assert env.get("HOME") == home

    def test_path_preserved(self, monkeypatch):
        """20b: PATH is preserved."""
        path_val = os.environ.get("PATH", "/usr/bin")
        monkeypatch.setenv("PATH", path_val)
        env = build_subprocess_env()
        assert env.get("PATH") == path_val

    def test_locale_preserved(self, monkeypatch):
        """20c: LANG locale variable is preserved."""
        monkeypatch.setenv("LANG", "en_US.UTF-8")
        env = build_subprocess_env()
        assert env.get("LANG") == "en_US.UTF-8"

    def test_user_custom_var_preserved(self, monkeypatch):
        """20d: User-defined non-sensitive variables are preserved."""
        monkeypatch.setenv("MY_APP_CONFIG", "custom_value_xyz")
        env = build_subprocess_env()
        assert env.get("MY_APP_CONFIG") == "custom_value_xyz"


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------

class TestSubprocessEnvEdgeCases:
    """Edge cases for environment building."""

    def test_returns_dict_not_environ(self):
        """Result is a plain dict, not os._Environ."""
        env = build_subprocess_env()
        assert isinstance(env, dict)

    def test_inject_constants_match(self):
        """Verify _INJECT has expected keys."""
        assert "GIT_EDITOR" in _INJECT
        assert "AGENT_LOOM" in _INJECT
