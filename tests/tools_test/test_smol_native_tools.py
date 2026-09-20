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


def test_read_cache_is_owned_by_one_agent_instance_and_closed_independently(tmp_path):
    from dataclasses import replace

    from agentloom.runtime import RuntimeHome, bind_run_context
    from agentloom.runtime.resources import close_instance_resources, close_run_resources
    from agentloom.runtime.trace import bind_explicit_execution_context, capture_explicit_execution_context

    path = tmp_path / "shared.txt"
    path.write_text("each worker must see this content\n")
    read = resolve_tool_function("read_file")
    context = RuntimeHome(tmp_path / "runtime").context(application_id="smol", task_id="task", run_id="run")
    parent = capture_explicit_execution_context()
    def worker(identity):
        return bind_explicit_execution_context(replace(parent, agent_id=identity, agent_config={}))

    with bind_run_context(context):
        try:
            with worker("a"):
                assert "each worker" in read(str(path))
                assert "File unchanged" in read(str(path))
            with worker("b"):
                assert "each worker" in read(str(path))
            close_instance_resources("a")
            with worker("b"):
                assert "File unchanged" in read(str(path))
            with worker("a"):
                assert "each worker" in read(str(path))
        finally:
            close_run_resources()
