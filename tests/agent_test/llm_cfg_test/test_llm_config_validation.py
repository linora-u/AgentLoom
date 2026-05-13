from pathlib import Path

import pytest

from src.lib.config.llm_config import LLMConfig


def _valid_raw() -> dict:
    return {
        "model": {
            "default_model_type": "powerful",
            "common": {
                "model": "openai/test-common",
                "base_url": "https://common.example/v1",
                "api_key": "test-key",
                "requests_per_minute": 12,
            },
            "powerful": {
                "model": "openai/test-powerful",
            },
            "summary": {
                "model": "openai/test-summary",
            },
        }
    }


def test_from_dict_allows_missing_model_block() -> None:
    config = LLMConfig.from_dict({})

    assert config.models == {}
    assert config.default_model_type == ""


def test_from_dict_allows_default_model_type_without_profiles() -> None:
    config = LLMConfig.from_dict({"model": {"default_model_type": "powerful"}})

    assert config.models == {}
    assert config.default_model_type == "powerful"


def test_load_from_yaml_allows_missing_file(tmp_path: Path) -> None:
    config = LLMConfig.load_from_yaml(tmp_path / "missing-llm.yaml")

    assert config.models == {}
    assert config.default_model_type == ""


def test_from_dict_allows_missing_common_when_profiles_are_defined() -> None:
    raw = {
        "model": {
            "powerful": {"model": "openai/test-powerful"},
            "summary": {"model": "openai/test-summary"},
        }
    }

    config = LLMConfig.from_dict(raw)

    assert set(config.available_types) == {"powerful", "summary"}



def test_from_dict_requires_summary_when_profiles_are_defined() -> None:
    raw = {
        "model": {
            "common": {"model": "openai/test-common"},
            "powerful": {"model": "openai/test-powerful"},
        }
    }

    with pytest.raises(ValueError, match="summary"):
        LLMConfig.from_dict(raw)


def test_from_dict_requires_model_field_for_each_profile() -> None:
    raw = {
        "model": {
            "common": {"model": "openai/test-common"},
            "powerful": {"temperature": 0.2},
            "summary": {"model": "openai/test-summary"},
        }
    }

    with pytest.raises(ValueError, match="missing required 'model'"):
        LLMConfig.from_dict(raw)


def test_common_key_is_reserved_and_not_exposed_as_model_type() -> None:
    config = LLMConfig.from_dict(_valid_raw())

    assert "common" not in config.available_types



def test_explicit_unknown_model_type_raises_without_fallback() -> None:
    config = LLMConfig.from_dict(_valid_raw())

    with pytest.raises(ValueError, match="not defined"):
        config.for_type("missing")


@pytest.mark.parametrize("requested_type", [None, ""])
def test_empty_model_type_falls_back_to_default_model_type(requested_type) -> None:
    config = LLMConfig.from_dict(_valid_raw())

    resolved = config.for_type(requested_type)

    assert resolved.model == "openai/test-powerful"
