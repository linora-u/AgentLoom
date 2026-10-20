import pytest
from pydantic import ValidationError

from agentloom.config.config_validation import RootSettings


def test_runtime_trace_modes_are_explicit_and_full_by_default():
    assert RootSettings().runtime.trace.mode == "full"
    assert RootSettings.model_validate(
        {"runtime": {"trace": {"mode": "off"}}}
    ).runtime.trace.mode == "off"
    assert RootSettings.model_validate(
        {"runtime": {"trace": {"mode": False}}}
    ).runtime.trace.mode == "off"


def test_runtime_trace_rejects_unknown_modes_and_keys():
    with pytest.raises(ValidationError):
        RootSettings.model_validate({"runtime": {"trace": {"mode": "summary"}}})
    with pytest.raises(ValidationError):
        RootSettings.model_validate({"runtime": {"trace": {"mode": True}}})
    with pytest.raises(ValidationError):
        RootSettings.model_validate(
            {"runtime": {"trace": {"mode": "off", "delete_later": True}}}
        )
