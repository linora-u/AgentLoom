"""Import boundaries for the built-in tool catalog and implementation loader."""

from __future__ import annotations

import inspect
import json
import subprocess
import sys
from pathlib import Path
from textwrap import dedent


def _run_in_fresh_interpreter(source: str) -> dict[str, object]:
    result = subprocess.run(
        [sys.executable, "-c", dedent(source)],
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def test_tools_package_is_not_a_public_export_facade() -> None:
    state = _run_in_fresh_interpreter(
        """
        import json
        import sys
        import agentloom.tools as tools

        implementation_prefixes = (
            "agentloom.runtimes.smolagents.tools",
            "agentloom.tools.context",
            "agentloom.tools.file_ops",
            "agentloom.tools.search",
            "agentloom.tools.self_learning",
            "agentloom.runtimes.smolagents.tools.shell",
            "agentloom.tools.skills",
            "agentloom.runtimes.smolagents.tools.todo",
        )
        loaded = sorted(
            name
            for name in sys.modules
            if name.startswith(implementation_prefixes)
        )
        leaked_exports = sorted(
            name
            for name in (
                "shell_tool",
                "read_file",
                "get_tool_spec",
                "resolve_tool_function",
                "todo_write",
            )
            if hasattr(tools, name)
        )
        print(json.dumps({"loaded": loaded, "leaked_exports": leaked_exports}))
        """
    )

    assert state == {"loaded": [], "leaked_exports": []}


def test_catalog_metadata_does_not_load_tool_implementations() -> None:
    state = _run_in_fresh_interpreter(
        """
        import json
        import sys
        from agentloom.tools.catalog import list_tool_specs

        specs = list_tool_specs()
        implementation_prefixes = (
            "agentloom.runtimes.smolagents.tools",
            "agentloom.tools.context",
            "agentloom.tools.file_ops",
            "agentloom.tools.search",
            "agentloom.tools.self_learning",
            "agentloom.runtimes.smolagents.tools.shell",
            "agentloom.tools.skills",
            "agentloom.runtimes.smolagents.tools.todo",
        )
        loaded = sorted(
            name
            for name in sys.modules
            if name.startswith(implementation_prefixes)
        )
        print(json.dumps({
            "count": len(specs),
            "has_implementation_refs": all(bool(spec.implementation.module) for spec in specs),
            "has_callable_field": any(hasattr(spec, "function") for spec in specs),
            "loaded": loaded,
        }))
        """
    )

    assert state["count"] >= 20
    assert state["has_implementation_refs"] is True
    assert state["has_callable_field"] is False
    assert state["loaded"] == []


def test_loader_imports_only_the_selected_tool_implementation() -> None:
    state = _run_in_fresh_interpreter(
        """
        import json
        import sys
        from agentloom.tools.loader import resolve_tool_function

        resolved = resolve_tool_function("grep_search")
        sibling_prefixes = (
            "agentloom.tools.search.ast_grep_tool",
            "agentloom.runtimes.smolagents.tools.search.glob_tool",
            "agentloom.runtimes.smolagents.tools.search.glob_tool",
            "agentloom.tools.search.lsp_tool",
        )
        loaded_siblings = sorted(
            prefix
            for prefix in sibling_prefixes
            if any(name == prefix or name.startswith(prefix + ".") for name in sys.modules)
        )
        print(json.dumps({
            "name": resolved.__name__,
            "module": resolved.__module__,
            "loaded_siblings": loaded_siblings,
        }))
        """
    )

    assert state == {
        "name": "grep_search",
        "module": "agentloom.runtimes.smolagents.tools.search.grep_tool.grep_tool",
        "loaded_siblings": [],
    }


def test_lazy_group_exports_survive_same_named_submodule_imports() -> None:
    state = _run_in_fresh_interpreter(
        """
        import importlib
        import json

        importlib.import_module("agentloom.runtimes.smolagents.tools.shell.shell_tool")
        importlib.import_module("agentloom.runtimes.smolagents.tools.file_ops.read_file")

        from agentloom.runtimes.smolagents.tools.file_ops import read_file
        from agentloom.runtimes.smolagents.tools.shell import shell_tool

        print(json.dumps({
            "read_file_callable": callable(read_file),
            "shell_tool_callable": callable(shell_tool),
        }))
        """
    )

    assert state == {
        "read_file_callable": True,
        "shell_tool_callable": True,
    }


def test_catalog_fixed_arg_contract_matches_implementations() -> None:
    from agentloom.tools.catalog import list_tool_specs
    from agentloom.tools.loader import resolve_tool_function

    for spec in list_tool_specs():
        parameters = inspect.signature(resolve_tool_function(spec.name)).parameters
        keyword_names = tuple(
            name
            for name, parameter in parameters.items()
            if parameter.kind
            in (inspect.Parameter.POSITIONAL_OR_KEYWORD, inspect.Parameter.KEYWORD_ONLY)
        )
        accepts_extra = any(
            parameter.kind == inspect.Parameter.VAR_KEYWORD
            for parameter in parameters.values()
        )
        if spec.provider == "pi":
            # Pi's Python declaration publishes the official schema. Fixed argument
            # projection is not yet enabled for its native executor.
            assert spec.fixed_arg_names == ()
            declared = resolve_tool_function(spec.name)._agentloom_tool_definition
            assert keyword_names == tuple(declared.parameters["properties"])
        else:
            assert spec.fixed_arg_names == keyword_names, spec.name
        assert spec.accepts_extra_fixed_args is accepts_extra, spec.name


def test_tui_definition_validation_does_not_load_implementations() -> None:
    state = _run_in_fresh_interpreter(
        """
        import json
        import sys
        from pathlib import Path
        from agentloom.application.definition import validate_agent_definition

        valid_errors = validate_agent_definition(
            Path("."),
            "applications/example/workflows/example.yaml",
            {
                "name": "example",
                "agent_runtime": "smolagents",
                "description": "metadata-only validation",
                "model_type": "powerful",
                "tools": [{"name": "grep_search", "fixed_args": {"path": "."}}],
                "workflow": "validate the definition",
            },
            catalog=("powerful", {"powerful": {"model": "openai/test"}}),
        )
        invalid_errors = validate_agent_definition(
            Path("."),
            "applications/example/workflows/example.yaml",
            {
                "name": "example",
                "agent_runtime": "smolagents",
                "description": "metadata-only validation",
                "model_type": "powerful",
                "tools": [{"name": "grep_search", "fixed_args": {"unknown": "."}}],
                "workflow": "validate the definition",
            },
            catalog=("powerful", {"powerful": {"model": "openai/test"}}),
        )
        implementation_prefixes = (
            "agentloom.runtimes.smolagents.tools",
            "agentloom.tools.context",
            "agentloom.tools.file_ops",
            "agentloom.tools.search",
            "agentloom.tools.self_learning",
            "agentloom.runtimes.smolagents.tools.shell",
            "agentloom.tools.skills",
            "agentloom.runtimes.smolagents.tools.todo",
        )
        loaded = sorted(
            name
            for name in sys.modules
            if name.startswith(implementation_prefixes)
        )
        print(json.dumps({
            "valid_errors": valid_errors,
            "invalid_errors": invalid_errors,
            "loaded": loaded,
        }))
        """
    )

    assert state["valid_errors"] == []
    assert state["invalid_errors"] == [
        f"{Path.cwd() / 'applications/example/workflows/example.yaml'}: "
        "Unknown fixed_args for tool 'grep_search': unknown"
    ]
    assert state["loaded"] == []


def test_context_engine_metadata_lookup_does_not_load_implementations() -> None:
    state = _run_in_fresh_interpreter(
        """
        import json
        import sys
        from agentloom.runtime.context_engine.config import ContextEngineConfig

        config = ContextEngineConfig()
        implementation_prefixes = (
            "agentloom.runtimes.smolagents.tools",
            "agentloom.tools.context",
            "agentloom.tools.file_ops",
            "agentloom.tools.search",
            "agentloom.tools.self_learning",
            "agentloom.runtimes.smolagents.tools.shell",
            "agentloom.tools.skills",
            "agentloom.runtimes.smolagents.tools.todo",
        )
        loaded = sorted(
            name
            for name in sys.modules
            if name.startswith(implementation_prefixes)
        )
        print(json.dumps({
            "skip_tools": sorted(config.safety.skip_tools),
            "loaded": loaded,
        }))
        """
    )

    assert "edit_file" in state["skip_tools"]
    assert state["loaded"] == []


def test_catalog_partitions_expose_ownership_without_loading_executors() -> None:
    state = _run_in_fresh_interpreter('''
        import json, sys
        from agentloom.tools.catalog import get_tool_spec, list_tool_specs
        specs = list_tool_specs()
        print(json.dumps({
            "owners": {name: [get_tool_spec(name).owner, get_tool_spec(name).provider,
                              get_tool_spec(name).capability]
                       for name in ("read_file", "todo_write", "memory", "get_file_outline")},
            "loaded": [spec.implementation.module for spec in specs
                       if spec.implementation.module in sys.modules],
        }))
    ''')
    assert state == {
        "owners": {
            "read_file": ["runtime", "smolagents", "file.read"],
            "todo_write": ["runtime", "smolagents", "planning.todo"],
            "memory": ["platform", "agentloom", "memory"],
            "get_file_outline": ["optional", "agentloom", "get_file_outline"],
        },
        "loaded": [],
    }
