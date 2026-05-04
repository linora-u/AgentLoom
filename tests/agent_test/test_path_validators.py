"""Tests for path_validators: tool access control via tool_access_control.path_validation."""

import os
from pathlib import Path

import pytest

import src.lib.config.config as config_module
from src.lib.smolagents.hooks.path_validators import (
    DEFAULT_PATH_PARAM_PATTERNS,
    _find_rule_for_tool,
    _normalize_str_list,
    _resolve_path_params,
    validate_workspace_path,
)
from src.lib.smolagents.hooks.types import HookContext


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _patch_config(monkeypatch, raw: dict, root: Path) -> None:
    monkeypatch.setattr(
        config_module, "_ACTIVE_CONFIG",
        config_module.UnifiedConfig(raw, agent_root=root, llm_config=config_module.LLMConfig()),
        raising=True,
    )
    # Ensure workspace module reads from the same config (bypass agent context)
    tac = raw.get("tool_access_control", {})
    monkeypatch.setattr(
        "src.lib.permissions.workspace._resolve_tool_access_control_config",
        lambda: tac,
    )

def _patch_no_agent(monkeypatch) -> None:
    monkeypatch.setattr(
        "src.lib.smolagents.hooks.path_validators.get_current_agent_config",
        lambda: None,
    )

def _make_context(tool_name, tool_input, tool_inputs_schema=None):
    return HookContext(
        session_id="test", cwd=os.getcwd(), hook_event_name="PreToolUse",
        tool_name=tool_name, tool_input=tool_input, tool_inputs_schema=tool_inputs_schema,
    )

def _tac(pv_list):
    """Shorthand: build tool_access_control config."""
    return {"tool_access_control": {"path_validation": pv_list}}


# ===========================================================================
# TestConfigLoading
# ===========================================================================

class TestConfigLoading:
    def test_empty_list_allows_all(self, monkeypatch, tmp_path):
        _patch_config(monkeypatch, _tac([]), tmp_path)
        _patch_no_agent(monkeypatch)
        assert validate_workspace_path(_make_context("read_file", {"file_path": "/etc/passwd"}, {"file_path": {"type": "string"}})).decision == "allow"

    def test_no_path_validation_section_allows_all(self, monkeypatch, tmp_path):
        _patch_config(monkeypatch, {"tool_access_control": {}}, tmp_path)
        _patch_no_agent(monkeypatch)
        assert validate_workspace_path(_make_context("read_file", {"file_path": "/etc/passwd"}, {"file_path": {"type": "string"}})).decision == "allow"

    def test_no_tool_access_control_allows_all(self, monkeypatch, tmp_path):
        _patch_config(monkeypatch, {}, tmp_path)
        _patch_no_agent(monkeypatch)
        assert validate_workspace_path(_make_context("read_file", {"file_path": "/etc/passwd"}, {"file_path": {"type": "string"}})).decision == "allow"

    def test_exclude_paths_in_entry(self, monkeypatch, tmp_path):
        ws = tmp_path / "ws"; secrets = ws / "secrets"; secrets.mkdir(parents=True)
        target = secrets / "key.pem"; target.touch()
        _patch_config(monkeypatch, _tac([{"tools": ["read_file"], "exclude_paths": ["secrets"]}]), ws)
        _patch_no_agent(monkeypatch)
        result = validate_workspace_path(_make_context("read_file", {"file_path": str(target)}, {"file_path": {"type": "string"}}))
        assert result.decision == "block"
        assert "excluded directory" in result.reason

    def test_path_param_patterns_in_entry(self, monkeypatch, tmp_path):
        _patch_config(monkeypatch, _tac([{"tools": ["custom_tool"], "path_param_patterns": ["my_path"]}]), tmp_path)
        _patch_no_agent(monkeypatch)
        assert validate_workspace_path(_make_context("custom_tool", {"my_path": "/etc/passwd"}, {"my_path": {"type": "string"}})).decision == "block"

    def test_default_path_param_patterns_fallback(self, monkeypatch, tmp_path):
        _patch_config(monkeypatch, _tac([{"tools": ["read_file"]}]), tmp_path)
        _patch_no_agent(monkeypatch)
        assert validate_workspace_path(_make_context("read_file", {"file_path": "/etc/passwd"}, {"file_path": {"type": "string"}})).decision == "block"

    def test_tools_list_loaded(self, monkeypatch, tmp_path):
        _patch_config(monkeypatch, _tac([{"tools": ["tool_a", "tool_b"]}]), tmp_path)
        _patch_no_agent(monkeypatch)
        assert validate_workspace_path(_make_context("tool_a", {"file_path": "/etc/passwd"}, {"file_path": {"type": "string"}})).decision == "block"
        assert validate_workspace_path(_make_context("tool_c", {"file_path": "/etc/passwd"}, {"file_path": {"type": "string"}})).decision == "allow"

    def test_entry_exclude_paths_loaded(self, monkeypatch, tmp_path):
        ws = tmp_path / "ws"; build = ws / "build"; build.mkdir(parents=True)
        target = build / "output.js"; target.touch()
        _patch_config(monkeypatch, _tac([{"tools": ["edit_file"], "exclude_paths": ["build"]}]), ws)
        _patch_no_agent(monkeypatch)
        result = validate_workspace_path(_make_context("edit_file", {"file_path": str(target)}, {"file_path": {"type": "string"}}))
        assert result.decision == "block"
        assert "excluded directory" in result.reason

    def test_entry_path_param_patterns_overrides_default(self, monkeypatch, tmp_path):
        """When entry has path_param_patterns, only those are used (not defaults)."""
        _patch_config(monkeypatch, _tac([{"tools": ["move_file"], "path_param_patterns": ["source", "destination"]}]), tmp_path)
        _patch_no_agent(monkeypatch)
        # "source" is in entry patterns -> detected
        assert validate_workspace_path(_make_context("move_file", {"source": "/etc/passwd"}, {"source": {"type": "string"}})).decision == "block"
        # "file_path" is NOT in entry patterns (defaults not used) -> not detected -> allow
        assert validate_workspace_path(_make_context("move_file", {"file_path": "/etc/passwd"}, {"file_path": {"type": "string"}})).decision == "allow"


# ===========================================================================
# TestRuleMatching
# ===========================================================================

class TestRuleMatching:
    def test_tool_in_entry(self):
        assert _find_rule_for_tool("read_file", [{"tools": ["read_file"]}]) is not None

    def test_tool_not_in_any_entry(self):
        assert _find_rule_for_tool("shell_tool", [{"tools": ["read_file"]}]) is None

    def test_tool_matches_first_entry(self):
        r1 = {"tools": ["tool_a"], "exclude_paths": ["dir1"]}
        r2 = {"tools": ["tool_a"], "exclude_paths": ["dir2"]}
        assert _find_rule_for_tool("tool_a", [r1, r2]) is r1

    def test_multiple_tools_in_same_entry(self):
        r = {"tools": ["tool_a", "tool_b", "tool_c"]}
        assert _find_rule_for_tool("tool_a", [r]) is r
        assert _find_rule_for_tool("tool_b", [r]) is r
        assert _find_rule_for_tool("tool_d", [r]) is None

    def test_invalid_entry_skipped(self):
        entries = ["not_a_dict", {"tools": ["tool_a"]}]
        assert _find_rule_for_tool("tool_a", entries) is entries[1]

    def test_empty_list(self):
        assert _find_rule_for_tool("any_tool", []) is None


# ===========================================================================
# TestExcludePaths
# ===========================================================================

class TestExcludePaths:
    def test_entry_exclude_blocks(self, monkeypatch, tmp_path):
        ws = tmp_path / "ws"; git = ws / ".git"; git.mkdir(parents=True)
        target = git / "config"; target.touch()
        _patch_config(monkeypatch, _tac([{"tools": ["read_file"], "exclude_paths": [".git"]}]), ws)
        _patch_no_agent(monkeypatch)
        result = validate_workspace_path(_make_context("read_file", {"file_path": str(target)}, {"file_path": {"type": "string"}}))
        assert result.decision == "block"
        assert ".git" in result.reason

    def test_no_exclude_allows_within_workspace(self, monkeypatch, tmp_path):
        ws = tmp_path / "ws"; ws.mkdir()
        target = ws / "test.txt"; target.touch()
        _patch_config(monkeypatch, _tac([{"tools": ["read_file"]}]), ws)
        _patch_no_agent(monkeypatch)
        assert validate_workspace_path(_make_context("read_file", {"file_path": str(target)}, {"file_path": {"type": "string"}})).decision == "allow"

    def test_slash_exclude_blocks_everything(self, monkeypatch, tmp_path):
        ws = tmp_path / "ws"; ws.mkdir(parents=True)
        target = ws / "test.txt"; target.touch()
        _patch_config(monkeypatch, _tac([{"tools": ["read_file"], "exclude_paths": ["/"]}]), ws)
        _patch_no_agent(monkeypatch)
        assert validate_workspace_path(_make_context("read_file", {"file_path": str(target)}, {"file_path": {"type": "string"}})).decision == "block"

    def test_different_entries_different_excludes(self, monkeypatch, tmp_path):
        ws = tmp_path / "ws"; build = ws / "build"; build.mkdir(parents=True)
        target = build / "out.js"; target.touch()
        _patch_config(monkeypatch, _tac([
            {"tools": ["read_file"], "exclude_paths": [".git"]},
            {"tools": ["edit_file"], "exclude_paths": ["build"]},
        ]), ws)
        _patch_no_agent(monkeypatch)
        # read_file_content excludes .git but NOT build -> allow
        assert validate_workspace_path(_make_context("read_file", {"file_path": str(target)}, {"file_path": {"type": "string"}})).decision == "allow"
        # edit_file_content excludes build -> block
        assert validate_workspace_path(_make_context("edit_file", {"file_path": str(target)}, {"file_path": {"type": "string"}})).decision == "block"


# ===========================================================================
# TestPathParamPatterns
# ===========================================================================

class TestPathParamPatterns:
    def test_default_fallback(self):
        result = _resolve_path_params({"file_path": {"type": "string"}, "encoding": {"type": "string"}}, DEFAULT_PATH_PARAM_PATTERNS)
        assert "file_path" in result
        assert "encoding" not in result

    def test_custom_patterns(self):
        result = _resolve_path_params({"source": {"type": "string"}, "dest": {"type": "string"}}, ["source", "dest"])
        assert result == ["source", "dest"]

    def test_no_schema_returns_empty(self):
        assert _resolve_path_params(None, DEFAULT_PATH_PARAM_PATTERNS) == []

    def test_empty_patterns_returns_empty(self):
        assert _resolve_path_params({"file_path": {"type": "string"}}, []) == []

    def test_entry_with_custom_patterns_ignores_defaults(self, monkeypatch, tmp_path):
        _patch_config(monkeypatch, _tac([{"tools": ["move_file"], "path_param_patterns": ["source"]}]), tmp_path)
        _patch_no_agent(monkeypatch)
        # "source" detected -> block
        assert validate_workspace_path(_make_context("move_file", {"source": "/etc/passwd"}, {"source": {"type": "string"}})).decision == "block"
        # "file_path" NOT detected (not in entry's patterns) -> allow
        assert validate_workspace_path(_make_context("move_file", {"file_path": "/etc/passwd"}, {"file_path": {"type": "string"}})).decision == "allow"


# ===========================================================================
# TestNormalizeStrList
# ===========================================================================

class TestNormalizeStrList:
    def test_list_input(self):
        assert _normalize_str_list(["a", "b"]) == ["a", "b"]
    def test_string_input(self):
        assert _normalize_str_list("single") == ["single"]
    def test_empty_string(self):
        assert _normalize_str_list("  ") == []
    def test_none_with_default(self):
        assert _normalize_str_list(None, default=["x"]) == ["x"]
    def test_none_without_default(self):
        assert _normalize_str_list(None) == []
    def test_filters_non_strings(self):
        assert _normalize_str_list(["a", 123, "b", None]) == ["a", "b"]
    def test_filters_empty_strings(self):
        assert _normalize_str_list(["a", "", "  ", "b"]) == ["a", "b"]


# ===========================================================================
# TestValidateWorkspacePath — end-to-end
# ===========================================================================

class TestValidateWorkspacePath:
    def test_path_inside_workspace_allowed(self, monkeypatch, tmp_path):
        ws = tmp_path / "ws"; ws.mkdir()
        target = ws / "test.txt"; target.touch()
        _patch_config(monkeypatch, _tac([{"tools": ["read_file"]}]), ws)
        _patch_no_agent(monkeypatch)
        assert validate_workspace_path(_make_context("read_file", {"file_path": str(target)}, {"file_path": {"type": "string"}})).decision == "allow"

    def test_path_outside_workspace_blocked(self, monkeypatch, tmp_path):
        ws = tmp_path / "ws"; ws.mkdir()
        _patch_config(monkeypatch, _tac([{"tools": ["read_file"]}]), ws)
        _patch_no_agent(monkeypatch)
        result = validate_workspace_path(_make_context("read_file", {"file_path": "/etc/passwd"}, {"file_path": {"type": "string"}}))
        assert result.decision == "block"
        assert "outside" in result.reason.lower()

    def test_file_uri_prefix_stripped(self, monkeypatch, tmp_path):
        ws = tmp_path / "ws"; ws.mkdir()
        target = ws / "test.txt"; target.touch()
        _patch_config(monkeypatch, _tac([{"tools": ["read_file"]}]), ws)
        _patch_no_agent(monkeypatch)
        assert validate_workspace_path(_make_context("read_file", {"file_path": f"file://{target}"}, {"file_path": {"type": "string"}})).decision == "allow"

    def test_relative_path_resolved(self, monkeypatch, tmp_path):
        ws = tmp_path / "ws"; ws.mkdir()
        _patch_config(monkeypatch, _tac([{"tools": ["read_file"]}]), ws)
        _patch_no_agent(monkeypatch)
        assert validate_workspace_path(_make_context("read_file", {"file_path": "../../etc/passwd"}, {"file_path": {"type": "string"}})).decision == "block"

    def test_list_type_path_param(self, monkeypatch, tmp_path):
        ws = tmp_path / "ws"; ws.mkdir()
        good = ws / "ok.txt"; good.touch()
        _patch_config(monkeypatch, _tac([{"tools": ["batch_tool"]}]), ws)
        _patch_no_agent(monkeypatch)
        result = validate_workspace_path(_make_context("batch_tool", {"file_paths": [str(good), "/etc/passwd"]}, {"file_paths": {"type": "array"}}))
        assert result.decision == "block"

    def test_tool_not_in_entries_allows(self, monkeypatch, tmp_path):
        _patch_config(monkeypatch, _tac([{"tools": ["edit_file"]}]), tmp_path)
        _patch_no_agent(monkeypatch)
        assert validate_workspace_path(_make_context("shell_tool", {"file_path": "/etc/passwd"}, {"file_path": {"type": "string"}})).decision == "allow"

    def test_no_path_values_in_input_allows(self, monkeypatch, tmp_path):
        _patch_config(monkeypatch, _tac([{"tools": ["shell_tool"]}]), tmp_path)
        _patch_no_agent(monkeypatch)
        assert validate_workspace_path(_make_context("shell_tool", {"command": "echo hi"}, {"command": {"type": "string"}})).decision == "allow"


# ===========================================================================
# T7: Multi-rule precedence — first matching rule wins
# ===========================================================================

class TestMultiRulePrecedence:
    """When a tool matches multiple rules, the first match is used."""

    def test_first_rule_excludes_second_allows(self, monkeypatch, tmp_path):
        """Tool in two rules: first rule has exclude_paths, second doesn't.
        First match wins → path is blocked."""
        ws = tmp_path / "ws"
        ws.mkdir()
        secret = ws / "secrets" / "key.txt"
        secret.parent.mkdir(parents=True)
        secret.write_text("secret")

        # Rule 1: read_file_content with exclude "secrets"
        # Rule 2: read_file_content with no excludes
        _patch_config(monkeypatch, _tac([
            {"tools": ["read_file"], "exclude_paths": ["secrets"]},
            {"tools": ["read_file"], "exclude_paths": []},
        ]), ws)
        _patch_no_agent(monkeypatch)

        result = validate_workspace_path(_make_context(
            "read_file",
            {"file_path": str(secret)},
            {"file_path": {"type": "string"}},
        ))
        assert result.decision == "block"
        assert "excluded" in result.reason.lower()

    def test_tool_in_two_rules_excludes_union(self, monkeypatch, tmp_path):
        """Multi-rule: exclude_paths from all matching rules are merged (union)."""
        ws = tmp_path / "ws"
        ws.mkdir()
        build_file = ws / "build" / "out.js"
        build_file.parent.mkdir(parents=True)
        build_file.write_text("code")

        # Rule 1: broad rule without "build" in excludes
        # Rule 2: specific rule with "build" in excludes
        # Union semantics: exclude_paths = [] + ["build"] = ["build"]
        _patch_config(monkeypatch, _tac([
            {"tools": ["read_file"], "exclude_paths": []},
            {"tools": ["read_file"], "exclude_paths": ["build"]},
        ]), ws)
        _patch_no_agent(monkeypatch)

        result = validate_workspace_path(_make_context(
            "read_file",
            {"file_path": str(build_file)},
            {"file_path": {"type": "string"}},
        ))
        # Union: "build" is excluded across all matching rules → block
        assert result.decision == "block"
        assert "excluded" in result.reason.lower()


# ===========================================================================
# T13: Relative path escape with ../
# ===========================================================================

class TestRelativePathEscape:
    """Paths using ../ to escape workspace boundary should be blocked."""

    def test_triple_dot_dot_escape_blocked(self, monkeypatch, tmp_path):
        ws = tmp_path / "ws"
        ws.mkdir()
        _patch_config(monkeypatch, _tac([{"tools": ["read_file"]}]), ws)
        _patch_no_agent(monkeypatch)
        result = validate_workspace_path(_make_context(
            "read_file",
            {"file_path": "../../../etc/passwd"},
            {"file_path": {"type": "string"}},
        ))
        assert result.decision == "block"

    def test_single_dot_dot_escape_blocked(self, monkeypatch, tmp_path):
        ws = tmp_path / "ws"
        ws.mkdir()
        _patch_config(monkeypatch, _tac([{"tools": ["read_file"]}]), ws)
        _patch_no_agent(monkeypatch)
        result = validate_workspace_path(_make_context(
            "read_file",
            {"file_path": "../sibling/file.txt"},
            {"file_path": {"type": "string"}},
        ))
        assert result.decision == "block"

    def test_dot_dot_within_workspace_allowed(self, monkeypatch, tmp_path):
        """../that resolves back into workspace should be allowed."""
        ws = tmp_path / "ws"
        (ws / "src" / "sub").mkdir(parents=True)
        target = ws / "src" / "main.py"
        target.write_text("code")

        _patch_config(monkeypatch, _tac([{"tools": ["read_file"]}]), ws)
        _patch_no_agent(monkeypatch)
        # "src/sub/../main.py" resolves to "src/main.py" which is inside workspace
        result = validate_workspace_path(_make_context(
            "read_file",
            {"file_path": str(ws / "src" / "sub" / ".." / "main.py")},
            {"file_path": {"type": "string"}},
        ))
        assert result.decision == "allow"
