from __future__ import annotations

import copy
from pathlib import Path
from uuid import UUID

import pytest
import yaml

import src.lib.config.config as config_module
from src.lib.config.config_validation import RootSettings, validate_system_snapshot
from src.lib.config.model_request_header_profiles import (
    MODEL_REQUEST_HEADER_PROFILES,
)
from src.lib.smolagents.models import model_manager as model_manager_module
from src.lib.smolagents.models import model_types
from src.lib.smolagents.models.request_headers import (
    AGENTLOOM_SESSION_UUID_TOKEN,
    AGENTLOOM_SESSION_TOKEN_TOKEN,
    GENERIC_MODEL_USER_AGENT,
    build_model_request_headers,
    get_system_model_request_headers,
    merge_headers,
)


def _patch_config(monkeypatch, *, system_config: dict, llm_config: dict | None = None) -> None:
    llm_raw = llm_config or {
        "model": {
            "default_model_type": "powerful",
            "powerful": {"model": "openai/test-model"},
            "summary": {"model": "openai/test-summary"},
        }
    }
    monkeypatch.setattr(
        config_module,
        "_ACTIVE_CONFIG",
        config_module.UnifiedConfig(
            copy.deepcopy(system_config),
            agent_root=Path.cwd(),
            llm_config=config_module.LLMConfig.from_dict(copy.deepcopy(llm_raw)),
        ),
        raising=True,
    )


def test_model_request_headers_validation_accepts_supported_profiles() -> None:
    for profile in (
        "agentloom",
        "cline",
        "generic",
        "kimicode",
        "none",
        "openclaw",
        "opencode",
        "roo",
    ):
        validate_system_snapshot(
            {"model_request_headers": {"profile": profile, "headers": {"X-Test": "1"}}},
            "test",
        )


def test_model_request_headers_validation_accepts_configured_custom_profile() -> None:
    validate_system_snapshot(
        {
            "model_request_headers": {
                "profile": "codex",
                "profiles": {
                    "codex": {"headers": {"User-Agent": "configured-agent/1.0"}}
                },
            }
        },
        "test",
    )


def test_model_request_headers_validation_rejects_unknown_profile() -> None:
    with pytest.raises(ValueError, match="model_request_headers.profile"):
        validate_system_snapshot({"model_request_headers": {"profile": "claude-code"}}, "test")


def test_root_settings_default_preserves_legacy_agentloom_profile() -> None:
    settings = RootSettings()
    assert settings.model_request_headers.profile == "agentloom"
    assert settings.model_request_headers.headers == {}


def test_repository_system_yaml_selects_verified_opencode_profile_without_inline_headers() -> None:
    system_yaml = yaml.safe_load(
        (Path(__file__).resolve().parents[2] / "config" / "system.yaml").read_text(
            encoding="utf-8"
        )
    )
    model_request_headers = system_yaml["model_request_headers"]

    assert model_request_headers == {"profile": "opencode", "headers": {}}
    validate_system_snapshot(system_yaml, "config/system.yaml")


def test_custom_profile_can_use_runtime_session_header_placeholder(monkeypatch) -> None:
    _patch_config(
        monkeypatch,
        system_config={
            "system": {"user_agent": "AgentLoom/9.9"},
            "model_request_headers": {
                "profile": "claude_code",
                "profiles": {
                    "claude_code": {
                        "headers": {
                            "User-Agent": "claude-cli/2.1.159 (external, sdk-cli)",
                            "X-App": "cli",
                            "X-Claude-Code-Session-Id": AGENTLOOM_SESSION_UUID_TOKEN,
                        }
                    }
                },
            },
        },
    )

    headers = get_system_model_request_headers()

    assert headers["User-Agent"] == "claude-cli/2.1.159 (external, sdk-cli)"
    assert headers["X-App"] == "cli"
    UUID(headers["X-Claude-Code-Session-Id"])


def test_builtin_profiles_match_agentloom_maintained_defaults(monkeypatch) -> None:
    for profile, expected_headers in MODEL_REQUEST_HEADER_PROFILES.items():
        _patch_config(
            monkeypatch,
            system_config={
                "system": {"user_agent": "AgentLoom/9.9"},
                "model_request_headers": {"profile": profile},
            },
        )
        resolved = get_system_model_request_headers()
        assert resolved["User-Agent"] == expected_headers["User-Agent"]


def test_verified_opencode_profile_uses_current_real_tool_headers(monkeypatch) -> None:
    _patch_config(
        monkeypatch,
        system_config={
            "system": {"user_agent": "AgentLoom/9.9"},
            "model_request_headers": {"profile": "opencode"},
        },
    )

    headers = get_system_model_request_headers()

    assert headers["User-Agent"] == "opencode/1.17.12 ai-sdk/provider-utils/4.0.23 runtime/bun/1.3.14"
    assert headers["X-Session-Affinity"].startswith("ses_")
    assert headers["X-Session-Affinity"] == headers["X-Session-Id"]


def test_verified_cline_profile_uses_current_real_tool_headers(monkeypatch) -> None:
    _patch_config(
        monkeypatch,
        system_config={
            "system": {"user_agent": "AgentLoom/9.9"},
            "model_request_headers": {"profile": "cline"},
        },
    )

    headers = get_system_model_request_headers()

    assert headers["User-Agent"] == (
        "ai-sdk/openai-compatible/2.0.51 "
        "ai-sdk/provider-utils/4.0.30 runtime/bun/1.3.13"
    )


def test_verified_kimicode_profile_uses_current_real_tool_headers(monkeypatch) -> None:
    _patch_config(
        monkeypatch,
        system_config={
            "system": {"user_agent": "AgentLoom/9.9"},
            "model_request_headers": {"profile": "kimicode"},
        },
    )

    headers = get_system_model_request_headers()

    assert headers["User-Agent"] == "kimi-code-cli/0.21.1"
    assert headers["X-Stainless-Lang"] == "js"
    assert headers["X-Stainless-Runtime"] == "node"


def test_verified_openclaw_profile_uses_current_real_tool_headers(monkeypatch) -> None:
    _patch_config(
        monkeypatch,
        system_config={
            "system": {"user_agent": "AgentLoom/9.9"},
            "model_request_headers": {"profile": "openclaw"},
        },
    )

    headers = get_system_model_request_headers()

    assert headers["User-Agent"] == "OpenAI/JS 6.39.1"
    assert headers["X-Stainless-Package-Version"] == "6.39.1"


def test_verified_roo_profile_uses_current_source_verified_headers(monkeypatch) -> None:
    _patch_config(
        monkeypatch,
        system_config={
            "system": {"user_agent": "AgentLoom/9.9"},
            "model_request_headers": {"profile": "roo"},
        },
    )

    headers = get_system_model_request_headers()

    assert headers["HTTP-Referer"] == "https://github.com/RooVetGit/Roo-Cline"
    assert headers["X-Title"] == "Roo Code"
    assert headers["User-Agent"] == "RooCode/3.53.0"


def test_system_generic_profile_hides_agentloom_user_agent(monkeypatch) -> None:
    _patch_config(
        monkeypatch,
        system_config={
            "system": {"user_agent": "AgentLoom/9.9"},
            "model_request_headers": {"profile": "generic", "headers": {"X-Privacy": "on"}},
        },
    )

    headers = get_system_model_request_headers()

    assert headers == {"User-Agent": GENERIC_MODEL_USER_AGENT, "X-Privacy": "on"}
    assert "AgentLoom" not in headers["User-Agent"]


def test_codex_profile_requires_explicit_custom_profile() -> None:
    with pytest.raises(ValueError, match="model_request_headers.profile"):
        validate_system_snapshot(
            {"model_request_headers": {"profile": "codex", "headers": {"X-Privacy": "on"}}},
            "test",
        )


def test_custom_profile_overrides_builtin_profile(monkeypatch) -> None:
    _patch_config(
        monkeypatch,
        system_config={
            "system": {"user_agent": "AgentLoom/9.9"},
            "model_request_headers": {
                "profile": "opencode",
                "profiles": {
                    "opencode": {
                        "headers": {
                            "User-Agent": "custom-opencode/1.0",
                            "X-App": "custom-cli",
                        }
                    }
                },
            },
        },
    )

    assert get_system_model_request_headers() == {
        "User-Agent": "custom-opencode/1.0",
        "X-App": "custom-cli",
    }


def test_profile_none_sends_only_explicit_system_headers(monkeypatch) -> None:
    _patch_config(
        monkeypatch,
        system_config={
            "system": {"user_agent": "AgentLoom/9.9"},
            "model_request_headers": {"profile": "none", "headers": {"X-Privacy": "on"}},
        },
    )

    assert get_system_model_request_headers() == {"X-Privacy": "on"}


def test_runtime_session_header_placeholder_is_stable_uuid() -> None:
    first = merge_headers({"X-Claude-Code-Session-Id": "${agentloom.session_uuid}"})
    second = merge_headers({"X-Claude-Code-Session-Id": "${agentloom.session_uuid}"})

    assert first == second
    UUID(first["X-Claude-Code-Session-Id"])


def test_runtime_session_token_placeholder_is_stable_session_id() -> None:
    first = merge_headers({"X-Session-Id": AGENTLOOM_SESSION_TOKEN_TOKEN})
    second = merge_headers({"X-Session-Id": AGENTLOOM_SESSION_TOKEN_TOKEN})

    assert first == second
    assert first["X-Session-Id"].startswith("ses_")


def test_merge_headers_uses_case_insensitive_override() -> None:
    headers = merge_headers(
        {"User-Agent": "system", "X-Trace": "system"},
        {"user-agent": "model", "X-Model": "yes"},
    )

    assert headers == {"X-Trace": "system", "user-agent": "model", "X-Model": "yes"}
    assert "User-Agent" not in headers


def test_model_headers_override_system_headers(monkeypatch) -> None:
    _patch_config(
        monkeypatch,
        system_config={
            "system": {"user_agent": "AgentLoom/9.9"},
            "model_request_headers": {"profile": "generic", "headers": {"X-Privacy": "on"}},
        },
    )

    headers = build_model_request_headers(
        {"user-agent": "model-client/2.0", "X-Model": "powerful"}
    )

    assert headers == {
        "X-Privacy": "on",
        "user-agent": "model-client/2.0",
        "X-Model": "powerful",
    }


def test_model_manager_litellm_config_merges_system_and_model_headers(monkeypatch) -> None:
    _patch_config(
        monkeypatch,
        system_config={
            "system": {"user_agent": "AgentLoom/9.9"},
            "model_request_headers": {"profile": "generic", "headers": {"X-Privacy": "on"}},
        },
        llm_config={
            "model": {
                "default_model_type": "powerful",
                "powerful": {
                    "model": "openai/test-model",
                    "base_url": "https://example.test/v1",
                    "api_key": "key",
                    "extra_headers": {
                        "user-agent": "model-client/2.0",
                        "X-Model": "powerful",
                    },
                },
                "summary": {"model": "openai/test-summary"},
            }
        },
    )

    manager = model_manager_module.ModelManager()
    params = manager.get_litellm_config(model_types.ModelType("powerful"), model_cache=False)

    assert params["extra_headers"] == {
        "X-Privacy": "on",
        "user-agent": "model-client/2.0",
        "X-Model": "powerful",
    }


def test_smolagents_model_receives_merged_request_headers(monkeypatch) -> None:
    _patch_config(
        monkeypatch,
        system_config={
            "system": {"user_agent": "AgentLoom/9.9"},
            "model_request_headers": {"profile": "generic", "headers": {"X-Privacy": "on"}},
        },
        llm_config={
            "model": {
                "default_model_type": "powerful",
                "powerful": {
                    "model": "openai/test-model",
                    "extra_headers": {"X-Model": "powerful"},
                    "requests_per_minute": 999999,
                },
                "summary": {"model": "openai/test-summary"},
            }
        },
    )

    manager = model_manager_module.ModelManager()
    model = manager.get_smolagents_model(model_types.ModelType("powerful"), model_cache=False)

    assert model.kwargs["extra_headers"] == {
        "User-Agent": GENERIC_MODEL_USER_AGENT,
        "X-Privacy": "on",
        "X-Model": "powerful",
    }
