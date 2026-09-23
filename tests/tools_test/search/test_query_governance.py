"""Query exclusion policy at real smol executor entry points."""
from importlib import import_module
import pytest
from agentloom.config.config import bind_config, load_project_config
from agentloom.execution.trace.task_context import set_current_agent_config, clear_current_agent_config

@pytest.mark.parametrize("backend", ["rg", "python"])
@pytest.mark.parametrize("tool", ["grep", "glob"])
def test_absolute_exclusion_and_include_cannot_reintroduce_secret(tmp_path, backend, tool):
    module = import_module(f"agentloom.runtimes.smolagents.tools.search.{tool}_tool.{tool}_tool")
    if backend == "rg" and not module._RG_PATH:
        pytest.skip("ripgrep unavailable")
    (tmp_path / "secrets").mkdir()
    (tmp_path / "secrets" / "hidden.txt").write_text("MATCH_SECRET")
    (tmp_path / "public.txt").write_text("MATCH_PUBLIC")
    # Bind an actual per-agent policy, not patched authorization internals.
    set_current_agent_config({"tool_access_control": {"path_validation": [{"tools": [tool + "_search"], "exclude_paths": [str(tmp_path / "secrets")]}]}})
    original = module._RG_PATH
    try:
        if backend == "python":
            module._RG_PATH = None
        result = module.grep_search("MATCH", path=str(tmp_path), include="*.txt") if tool == "grep" else module.glob_search("**/*.txt", path=str(tmp_path))
        assert "public.txt" in result
        assert "hidden.txt" not in result and "MATCH_SECRET" not in result
    finally:
        module._RG_PATH = original
        clear_current_agent_config()

@pytest.mark.parametrize("tool", ["grep", "glob"])
def test_python_search_cannot_follow_alias_into_excluded_directory(tmp_path, tool):
    module = import_module(f"agentloom.runtimes.smolagents.tools.search.{tool}_tool.{tool}_tool")
    (tmp_path / "secrets").mkdir()
    secret = tmp_path / "secrets" / "hidden.txt"
    secret.write_text("MATCH_SECRET")
    (tmp_path / "alias.txt").symlink_to(secret)
    (tmp_path / "public.txt").write_text("MATCH_PUBLIC")
    set_current_agent_config({"tool_access_control": {"path_validation": [{"tools": [tool + "_search"], "exclude_paths": [str(tmp_path / "secrets")]}]}})
    original = module._RG_PATH
    try:
        module._RG_PATH = None
        result = module.grep_search("MATCH", path=str(tmp_path)) if tool == "grep" else module.glob_search("**/*.txt", path=str(tmp_path))
        assert "public.txt" in result
        assert "alias.txt" not in result and "MATCH_SECRET" not in result
    finally:
        module._RG_PATH = original
        clear_current_agent_config()


@pytest.mark.parametrize("backend", ["rg", "python"])
@pytest.mark.parametrize("absolute", [True, False])
def test_exclusion_alias_protects_canonical_directory(tmp_path, backend, absolute):
    module = import_module("agentloom.runtimes.smolagents.tools.search.grep_tool.grep_tool")
    if backend == "rg" and not module._RG_PATH:
        pytest.skip("ripgrep unavailable")
    (tmp_path / "secrets").mkdir()
    (tmp_path / "secrets" / "hidden.txt").write_text("MATCH_SECRET")
    (tmp_path / "private-alias").symlink_to(tmp_path / "secrets", target_is_directory=True)
    (tmp_path / "public.txt").write_text("MATCH_PUBLIC")
    set_current_agent_config({"tool_access_control": {"path_validation": [{"tools": ["grep_search"], "exclude_paths": [str(tmp_path / "private-alias") if absolute else "private-alias"]}]}})
    original = module._RG_PATH
    try:
        if backend == "python":
            module._RG_PATH = None
        result = module.grep_search("MATCH", path=str(tmp_path))
        assert "MATCH_PUBLIC" in result and "MATCH_SECRET" not in result
    finally:
        module._RG_PATH = original
        clear_current_agent_config()
