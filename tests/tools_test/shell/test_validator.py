import pytest

from src.tools.shell import validator as validator_module
from src.tools.shell.validator import analyze_command, validate_command


class _DummyConfig:
    def __init__(self, *, allowed_commands=None, allowed_operators=None):
        self.allowed_commands = allowed_commands
        self.allowed_operators = allowed_operators

    def get_nested(self, *keys, default=None):
        if keys == ("shell_settings", "allowed_commands"):
            if self.allowed_commands is None:
                return default
            return self.allowed_commands
        if keys == ("shell_settings", "allowed_operators"):
            if self.allowed_operators is None:
                return default
            return self.allowed_operators
        return default


def _patch_shell_config(monkeypatch, *, allowed_commands=None, allowed_operators=None):
    monkeypatch.setattr(
        validator_module,
        "C",
        _DummyConfig(
            allowed_commands=allowed_commands,
            allowed_operators=allowed_operators,
        ),
    )


def test_analyze_valid_command():
    names, operators = analyze_command("echo hello")
    assert "echo" in names


def test_analyze_forbidden_operators():
    names, operators = analyze_command("echo hello | grep h")
    assert "echo" in names
    assert "grep" in names
    assert "|" in operators


def test_load_allowed_commands_supports_wildcard_string(monkeypatch):
    _patch_shell_config(monkeypatch, allowed_commands="*")
    assert validator_module.load_allowed_commands() == []


def test_load_allowed_commands_wildcard_in_list_takes_precedence(monkeypatch):
    _patch_shell_config(monkeypatch, allowed_commands=["echo", "*", "rg"])
    assert validator_module.load_allowed_commands() == []


def test_load_allowed_operators_supports_wildcard(monkeypatch):
    _patch_shell_config(monkeypatch, allowed_operators=["|", "*"])
    assert validator_module.load_allowed_operators() == []


def test_validate_commands_allows_any_when_wildcards_configured(monkeypatch):
    _patch_shell_config(monkeypatch, allowed_commands="*", allowed_operators="*")
    validate_command("totally_unknown_cmd --flag | also_unknown_cmd")


def test_load_allowed_commands_still_rejects_non_wildcard_invalid_entry(monkeypatch):
    _patch_shell_config(monkeypatch, allowed_commands=["echo hello"])
    with pytest.raises(ValueError, match="bare command names"):
        validator_module.load_allowed_commands()
