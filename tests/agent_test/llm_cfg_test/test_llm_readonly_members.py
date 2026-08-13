from pathlib import Path

import pytest
from pydantic import ValidationError

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
            "powerful": {
                "model": "openai/powerful",
                "base_url": "https://powerful.example/v1",
                "api_key": "powerful-key",
                "requests_per_minute": 12,
                "temperature": 0.2,
                "context_window": 32768,
                "max_output_tokens": 4096,
                "timeout": 31,
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
    assert set(llm.available_types) == {"powerful", "fast", "summary"}

    powerful = llm.for_type("powerful")
    assert powerful.model == "openai/powerful"
    assert powerful.base_url == "https://powerful.example/v1"
    assert powerful.api_key == "powerful-key"
    assert powerful.temperature == 0.2
    assert powerful.context_window == 32768
    assert powerful.max_output_tokens == 4096
    assert powerful.timeout == 31
    assert powerful.description == "powerful model"


def test_llm_unknown_type_raises(monkeypatch):
    """Explicit unknown model types should raise instead of falling back."""
    raw = {
        "model": {
            "default_model_type": "fast",
            "fast": {
                "model": "openai/fast",
                "temperature": 0.6,
            },
            "summary": {"model": "openai/test-summary"},
        }
    }
    _patch_active_config(monkeypatch, raw)

    with pytest.raises(ValueError) as exc_info:
        config_module.C.llm.for_type("non_existing")
    message = str(exc_info.value)
    assert "config/llm.yaml" in message
    assert "the model call was not started" in message
    assert "Available model types: fast, summary" in message
    assert "model.non_existing.model" in message


def test_llm_no_default_model_type_raises_for_implicit_request(monkeypatch):
    """When default_model_type is not set, implicit model selection fails fast."""
    raw = {
        "model": {
            "powerful": {
                "model": "openai/powerful",
            },
            "summary": {"model": "openai/test-summary"},
        }
    }
    _patch_active_config(monkeypatch, raw)

    assert config_module.C.llm.default_model_type == ""
    with pytest.raises(ValueError) as exc_info:
        config_module.C.llm.for_type(None)
    message = str(exc_info.value)
    assert "model.default_model_type" in message
    assert "the model call was not started" in message
    assert "Agent YAML" in message

    from src.lib.smolagents.models.model_types import ModelTypeManager

    with pytest.raises(ValueError) as exc_info:
        ModelTypeManager.resolve_model_type(None)
    assert "model.default_model_type" in str(exc_info.value)
    assert "the model call was not started" in str(exc_info.value)

    with pytest.raises(ValueError):
        config_module.C.llm.for_type("non_existing")


def test_llm_does_not_require_common(monkeypatch):
    """Model config works without any common type."""
    raw = {
        "model": {
            "default_model_type": "powerful",
            "powerful": {
                "model": "openai/powerful",
                "api_key": "powerful-key",
            },
            "summary": {"model": "openai/test-summary"},
        }
    }
    _patch_active_config(monkeypatch, raw)

    assert set(config_module.C.llm.available_types) == {"powerful", "summary"}
    assert config_module.C.llm.for_type(None).model == "openai/powerful"


def test_llm_common_block_is_ignored(monkeypatch):
    """common is ignored instead of becoming a model type or fallback source."""
    raw = {
        "model": {
            "default_model_type": "powerful",
            "common": {
                "model": "openai/common",
            },
            "powerful": {
                "model": "openai/powerful",
                "base_url": "https://powerful.example/v1",
                "api_key": "powerful-key",
                "requests_per_minute": 12,
                "temperature": 0.4,
            },
            "summary": {"model": "openai/test-summary"},
        }
    }
    _patch_active_config(monkeypatch, raw)

    assert set(config_module.C.llm.available_types) == {"powerful", "summary"}
    powerful = config_module.C.llm.for_type(None)
    assert powerful.base_url == "https://powerful.example/v1"
    assert powerful.api_key == "powerful-key"
    assert powerful.requests_per_minute == 12
    assert powerful.temperature == 0.4

    exported = config_module.C.llm.to_legacy_dict()
    assert "common" not in exported["model"]
    assert config_module.LLMConfig.from_dict(exported).for_type(None).base_url == "https://powerful.example/v1"


def test_llm_legacy_export_does_not_invent_common(monkeypatch):
    """Export should not create a common model type when config did not define it."""
    raw = {
        "model": {
            "default_model_type": "powerful",
            "powerful": {"model": "openai/powerful"},
            "summary": {"model": "openai/test-summary"},
        }
    }
    cfg = config_module.LLMConfig.from_dict(raw)

    exported = cfg.to_legacy_dict()

    assert "common" not in exported["model"]
    assert config_module.LLMConfig.from_dict(exported).for_type(None).model == "openai/powerful"


def test_llm_views_are_readonly(monkeypatch):
    raw = {
        "model": {
            "default_model_type": "powerful",
            "powerful": {
                "model": "openai/powerful",
            },
            "summary": {"model": "openai/test-summary"},
        }
    }
    _patch_active_config(monkeypatch, raw)

    with pytest.raises((ValidationError, AttributeError, TypeError)):
        config_module.C.llm.for_type("powerful").model = "openai/other"


def test_llm_max_tokens_default_is_150000(monkeypatch):
    raw = {
        "model": {
            "default_model_type": "powerful",
            "powerful": {
                "model": "openai/powerful",
            },
            "summary": {"model": "openai/test-summary"},
        }
    }
    _patch_active_config(monkeypatch, raw)

    resolved = config_module.C.llm.for_type("powerful")

    assert resolved.context_window == 150000
    assert resolved.max_output_tokens == 16384


def test_legacy_max_tokens_populates_both_budgets(monkeypatch):
    raw = {
        "model": {
            "default_model_type": "powerful",
            "powerful": {"model": "openai/powerful", "max_tokens": 8192},
            "summary": {"model": "openai/test-summary"},
        }
    }
    _patch_active_config(monkeypatch, raw)

    resolved = config_module.C.llm.for_type("powerful")

    assert resolved.context_window == 8192
    assert resolved.max_output_tokens == 8192


def test_legacy_max_tokens_max_uses_finite_default(monkeypatch):
    raw = {
        "model": {
            "default_model_type": "powerful",
            "powerful": {"model": "openai/powerful", "max_tokens": "max"},
            "summary": {"model": "openai/test-summary"},
        }
    }
    _patch_active_config(monkeypatch, raw)

    resolved = config_module.C.llm.for_type("powerful")

    assert resolved.max_tokens == 150000
    assert resolved.context_window == 150000
    assert resolved.max_output_tokens == 150000
    assert resolved.input_token_limit == 150000


def test_invalid_token_budget_is_rejected(monkeypatch):
    raw = {
        "model": {
            "default_model_type": "powerful",
            "powerful": {
                "model": "openai/powerful",
                "context_window": 4096,
                "max_output_tokens": 8192,
            },
            "summary": {"model": "openai/test-summary"},
        }
    }

    with pytest.raises(ValueError, match="max_output_tokens.*context_window"):
        _patch_active_config(monkeypatch, raw)
