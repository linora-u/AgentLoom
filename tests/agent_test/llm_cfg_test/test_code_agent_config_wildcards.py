import builtins

import pytest

import src.lib.config.config as config_module


class _DummyConfigProxy:
    def __init__(self, code_agent_cfg):
        self._code_agent_cfg = code_agent_cfg

    def get(self, key, default=None):
        if key == "code_agent":
            return self._code_agent_cfg
        return default


def _patch_code_agent_cfg(monkeypatch, code_agent_cfg):
    monkeypatch.setattr(config_module, "C", _DummyConfigProxy(code_agent_cfg))


def test_additional_authorized_imports_supports_wildcard_string(monkeypatch):
    _patch_code_agent_cfg(monkeypatch, {"additional_authorized_imports": "*"})

    result = config_module.get_code_agent_config()

    assert result["additional_authorized_imports"] == ["*"]


def test_additional_authorized_imports_wildcard_in_list_takes_precedence(monkeypatch):
    _patch_code_agent_cfg(
        monkeypatch,
        {"additional_authorized_imports": ["json", "*", "os"]},
    )

    result = config_module.get_code_agent_config()

    assert result["additional_authorized_imports"] == ["*"]


def test_additional_functions_supports_wildcard_string(monkeypatch):
    _patch_code_agent_cfg(monkeypatch, {"additional_functions": "*"})

    result = config_module.get_code_agent_config()
    funcs = result["additional_functions"]

    assert funcs["open"] is builtins.open
    assert funcs["len"] is builtins.len
    assert funcs["print"] is builtins.print
    assert funcs["list"] is builtins.list


def test_additional_functions_wildcard_list_takes_precedence(monkeypatch):
    _patch_code_agent_cfg(
        monkeypatch,
        {"additional_functions": ["*", "not_a_builtin"]},
    )

    result = config_module.get_code_agent_config()

    assert "not_a_builtin" not in result["additional_functions"]
    assert result["additional_functions"]["open"] is builtins.open


def test_additional_functions_still_raises_for_invalid_explicit_name(monkeypatch):
    _patch_code_agent_cfg(monkeypatch, {"additional_functions": ["definitely_not_builtin"]})

    with pytest.raises(AttributeError, match="definitely_not_builtin"):
        config_module.get_code_agent_config()
