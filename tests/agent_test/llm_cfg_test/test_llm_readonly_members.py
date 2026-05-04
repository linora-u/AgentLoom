from pydantic import ValidationError
from pathlib import Path

import pytest

import src.lib.config.config as config_module


def _patch_active_config(monkeypatch, raw: dict) -> None:
    monkeypatch.setattr(
        config_module,
        "_ACTIVE_CONFIG",
        config_module.UnifiedConfig(raw, agent_root=Path.cwd(), llm_config=config_module.LLMConfig.from_dict(raw)),
        raising=True,
    )


def test_llm_readonly_members_basic(monkeypatch):
    raw = {
        "model": {
            "default_model_type": "powerful",
            "common": {
                "model": "openai/test-common",
                "base_url": "https://common.example/v1",
                "api_key": "common-key",
                "requests_per_minute": 12,
                "temperature": 0.4,
                "max_tokens": 1024,
                "timeout": 31,
            },
            "powerful": {
                "model": "openai/powerful",
                "temperature": 0.2,
                "description": "powerful model",
            },
            "fast": {
                "model": "openai/fast",
            },
            "summary": {
                "model": "openai/test-summary",
            },
        }
    }
    _patch_active_config(monkeypatch, raw)

    llm = config_module.C.llm

    assert llm.default_model_type == "powerful"
    assert set(llm.available_types) == {"common", "powerful", "fast", "summary"}

    assert llm.common.base_url == "https://common.example/v1"
    assert llm.common.api_key == "common-key"
    assert llm.common.requests_per_minute == 12

    powerful = llm.for_type("powerful")
    assert powerful.model == "openai/powerful"
    assert powerful.base_url == "https://common.example/v1"
    assert powerful.api_key == "common-key"
    assert powerful.temperature == 0.2
    assert powerful.max_tokens == 1024
    assert powerful.timeout == 31
    assert powerful.description == "powerful model"


def test_llm_unknown_type_falls_back_to_default(monkeypatch):
    """for_type() with unknown type should fall back to default_model_type."""
    raw = {
        "model": {
            "default_model_type": "fast",
            "common": {
                "model": "openai/test-common",
                "base_url": "https://common.example/v1",
                "api_key": "common-key",
            },
            "fast": {
                "model": "openai/fast",
                "temperature": 0.6,
            },
            "summary": {"model": "openai/test-summary"},
        }
    }
    _patch_active_config(monkeypatch, raw)

    # for_type() falls back to default_model_type when type not found
    import pytest
    with pytest.raises(ValueError):
        config_module.C.llm.for_type("non_existing")


def test_llm_no_default_model_type_falls_back_to_common(monkeypatch):
    """When default_model_type is not set, it defaults to 'common'."""
    raw = {
        "model": {
            "common": {
                "model": "openai/test-common",
                "base_url": "https://common.example/v1",
                "api_key": "common-key",
                "requests_per_minute": 20,
            },
            "powerful": {
                "model": "openai/powerful",
            },
            "summary": {"model": "openai/test-summary"},
        }
    }
    _patch_active_config(monkeypatch, raw)

    # default_model_type defaults to "common", so unknown type falls back to common
    assert config_module.C.llm.default_model_type == "common"
    import pytest
    with pytest.raises(ValueError):
        config_module.C.llm.for_type("non_existing")


def test_llm_views_are_readonly(monkeypatch):
    raw = {
        "model": {
            "default_model_type": "powerful",
            "common": {
                "model": "openai/test-common",
                "base_url": "https://common.example/v1",
                "api_key": "common-key",
                "requests_per_minute": 10,
            },
            "powerful": {
                "model": "openai/powerful",
            },
            "summary": {"model": "openai/test-summary"},
        }
    }
    _patch_active_config(monkeypatch, raw)

    with pytest.raises((ValidationError, AttributeError, TypeError)):
        config_module.C.llm.common.base_url = "https://new.example/v1"

    with pytest.raises((ValidationError, AttributeError, TypeError)):
        config_module.C.llm.for_type("powerful").model = "openai/other"


def test_llm_max_tokens_default_is_150000(monkeypatch):
    raw = {
        "model": {
            "default_model_type": "powerful",
            "common": {
                "model": "openai/test-common",
            },
            "powerful": {
                "model": "openai/powerful",
            },
            "summary": {"model": "openai/test-summary"},
        }
    }
    _patch_active_config(monkeypatch, raw)

    resolved = config_module.C.llm.for_type("powerful")
