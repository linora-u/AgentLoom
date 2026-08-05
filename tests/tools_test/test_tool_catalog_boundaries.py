"""Import boundaries for the built-in tool catalog and implementation loader."""

from __future__ import annotations

import json
import subprocess
import sys
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
        import src.tools as tools

        implementation_prefixes = (
            "src.tools.context",
            "src.tools.file_ops",
            "src.tools.search",
            "src.tools.self_learning",
            "src.tools.shell",
            "src.tools.skills",
            "src.tools.todo",
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
        from src.tools.catalog import list_tool_specs

        specs = list_tool_specs()
        implementation_prefixes = (
            "src.tools.context",
            "src.tools.file_ops",
            "src.tools.search",
            "src.tools.self_learning",
            "src.tools.shell",
            "src.tools.skills",
            "src.tools.todo",
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


def test_loader_imports_only_the_selected_tool_group() -> None:
    state = _run_in_fresh_interpreter(
        """
        import json
        import sys
        from src.tools.loader import resolve_tool_function

        resolved = resolve_tool_function("grep_search")
        loaded_groups = sorted({
            prefix
            for prefix in (
                "src.tools.context",
                "src.tools.file_ops",
                "src.tools.search",
                "src.tools.self_learning",
                "src.tools.shell",
                "src.tools.skills",
                "src.tools.todo",
            )
            if any(name == prefix or name.startswith(prefix + ".") for name in sys.modules)
        })
        print(json.dumps({
            "name": resolved.__name__,
            "loaded_groups": loaded_groups,
        }))
        """
    )

    assert state == {
        "name": "grep_search",
        "loaded_groups": ["src.tools.search"],
    }


def test_context_engine_metadata_lookup_does_not_load_implementations() -> None:
    state = _run_in_fresh_interpreter(
        """
        import json
        import sys
        from src.lib.context_engine.config import ContextEngineConfig

        config = ContextEngineConfig()
        implementation_prefixes = (
            "src.tools.context",
            "src.tools.file_ops",
            "src.tools.search",
            "src.tools.self_learning",
            "src.tools.shell",
            "src.tools.skills",
            "src.tools.todo",
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
