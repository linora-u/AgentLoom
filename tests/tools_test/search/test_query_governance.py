"""Query exclusion policy at real smol executor entry points."""
from importlib import import_module
import pytest
from agentloom.configuration.config import bind_config, load_project_config
from agentloom.runtime.trace.task_context import set_current_agent_config, clear_current_agent_config

@pytest.mark.parametrize("backend", ["rg", "python"])
@pytest.mark.parametrize("tool", ["grep", "glob"])
def test_absolute_exclusion_and_include_cannot_reintroduce_secret(tmp_path, backend, tool):
    module = import_module(f"agentloom.adapters.smolagents.tools.search.{tool}_tool.{tool}_tool")
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
