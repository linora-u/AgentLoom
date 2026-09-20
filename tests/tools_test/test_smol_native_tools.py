"""Smol's public tool loader and legacy imports share one implementation."""

from importlib import import_module

import pytest

from agentloom.tools.catalog import get_tool_spec
from agentloom.tools.loader import resolve_tool_function


@pytest.mark.parametrize("name", [
    "read_file", "write_file", "edit_file", "list_directory",
    "grep_search", "glob_search", "shell_tool", "check_background_task",
    "kill_background_task", "list_background_tasks",
])
def test_builtin_loader_selects_smol_owned_implementation(name):
    spec = get_tool_spec(name)
    assert spec.implementation.module.startswith("agentloom.adapters.smolagents.tools.")
    assert resolve_tool_function(name).__module__.startswith("agentloom.adapters.smolagents.tools.")


@pytest.mark.parametrize("suffix", [
    "shell.process", "shell.background_task", "shell.shell_audit_log",
    "file_ops._read_file_state", "file_ops.read_file.read_file",
    "file_ops.edit_file.edit_file", "search.grep_tool.grep_tool",
])
def test_legacy_import_keeps_the_same_state_and_function_globals(suffix):
    legacy = import_module(f"agentloom.tools.{suffix}")
    native = import_module(f"agentloom.adapters.smolagents.tools.{suffix}")
    assert legacy is native
