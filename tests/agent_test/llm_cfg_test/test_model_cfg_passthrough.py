import copy
from pathlib import Path

import src.lib.config.config as config_module
from src.lib.smolagents.models import model_manager as model_manager_module
from src.lib.smolagents.models import model_types


def _patch_yaml_config(monkeypatch, config: dict) -> dict:
    patched = copy.deepcopy(config)
    monkeypatch.setattr(
        config_module,
        "_ACTIVE_CONFIG",
        config_module.UnifiedConfig(patched, agent_root=Path.cwd(), llm_config=config_module.LLMConfig.from_dict(patched)),
        raising=True,
    )
    return patched


def _base_model_config() -> dict:
    return {
        "langfuse": {
            "enabled": True,
            "host": "https://langfuse.example",
            "public_key": "pk-example",
            "secret_key": "sk-secret",
        },
        "model": {
            "default_model_type": "powerful",
            "common": {
                "model": "openai/test-common",
                "base_url": "https://example.test/v1",
                "api_key": "key-common",
                "temperature": 0.2,
                "max_tokens": 1024,
                "timeout": 30,
                "num_retries": 7,
                "retry_delay": 2.5,
                "max_retry_delay": 20.0,
                "extra_headers": {"X-Common": "1"},
                "requests_per_minute": 11,
            },
            "powerful": {
                "model": "openai/test-model",
                "description": "Powerful model for complex tasks",
                "timeout": 60,
            },
            "fast": {
                "model": "openai/fast-model",
            },
            "summary": {
                "model": "openai/test-summary",
            },
        },
    }


def test_llm_member_access_and_model_config_fields(monkeypatch):
    _patch_yaml_config(monkeypatch, _base_model_config())

    llm = config_module.C.llm
    cfg = model_types.ModelTypeManager.get_llm_config(model_types.ModelType("powerful"))

    assert llm.default_model_type == "powerful"
    assert set(llm.available_types) == {"common", "powerful", "fast", "summary"}
    assert llm.common.base_url == "https://example.test/v1"
    assert llm.common.requests_per_minute == 11
    assert llm.for_type("powerful").model == "openai/test-model"

    assert cfg.model_id == "openai/test-model"
    assert cfg.base_url == "https://example.test/v1"
    assert cfg.api_key == "key-common"
    assert cfg.timeout == 60
    assert cfg.num_retries == 7
    assert cfg.retry_delay == 2.5
    assert cfg.max_retry_delay == 20.0
    assert cfg.description == "Powerful model for complex tasks"
    assert cfg.extra_headers == {"X-Common": "1"}


def test_model_config_specific_extra_headers_override_common(monkeypatch):
    config = _base_model_config()
    config["model"]["powerful"]["extra_headers"] = {"X-Specific": "yes"}
    _patch_yaml_config(monkeypatch, config)

    cfg = model_types.ModelTypeManager.get_llm_config(model_types.ModelType("powerful"))

    assert cfg.extra_headers == {"X-Specific": "yes"}


def test_model_manager_litellm_config_contains_passthrough_fields(monkeypatch):
    _patch_yaml_config(monkeypatch, _base_model_config())

    manager = model_manager_module.ModelManager()
    params = manager.get_litellm_config(
        model_type=model_types.ModelType("powerful"),
        model_builder=None,
        model_cache=False,
    )

    assert params["model"] == "openai/test-model"
    assert params["timeout"] == 60
    assert params["num_retries"] == 7
    assert params["retry_delay"] == 2.5
    assert params["max_retry_delay"] == 20.0
    assert params["extra_headers"] == {"X-Common": "1"}
    assert params["api_base"] == "https://example.test/v1"
    assert params["api_key"] == "key-common"


def test_langfuse_private_key_fallback(monkeypatch):
    _patch_yaml_config(monkeypatch, _base_model_config())

    assert config_module.C.llm.langfuse.enabled is True
    assert config_module.C.llm.langfuse.host == "https://langfuse.example"
    assert config_module.C.llm.langfuse.public_key == "pk-example"
    assert config_module.C.llm.langfuse.private_key == "sk-secret"
