"""Freeze the callable tools an Application candidate may distribute.

This is a static closure of existing tool declarations, not a Python sandbox.
Candidate YAML may select registered tools and move the base's custom bindings
between Agents, but must not turn an arbitrary import into a model-facing tool.
No configured module or callable is imported while checking this boundary.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path

import yaml

from agentloom.config.yaml_loader import UniqueKeySafeLoader


def _documents(project: Path):
    # Workers may be referenced outside workflows/, including Skill resources.
    # Read every archived YAML/JSON mapping rather than trusting only the
    # original reachable tree; newly added or previously unused Agents count.
    for path in sorted(project.rglob("*")):
        if path.suffix.lower() not in {".yaml", ".yml", ".json"} or not path.is_file():
            continue
        if path.is_symlink():
            raise ValueError("Tool capability resources cannot be symbolic links")
        raw = path.read_text()
        # Deliberately do not call load_unique_yaml: it restores private refs.
        value = json.loads(raw) if path.suffix.lower() == ".json" else yaml.load(raw, Loader=UniqueKeySafeLoader)
        if isinstance(value, Mapping):
            yield path.relative_to(project).as_posix(), value


def _tools(source: str, document: Mapping):
    tools = document.get("tools")
    if tools is None:
        return
    if not isinstance(tools, list):
        raise ValueError(f"Tool capability declarations must be a list: {source}")
    for tool in tools:
        if not isinstance(tool, Mapping) or not isinstance(tool.get("name"), str) or not tool["name"].strip():
            raise ValueError(f"Invalid tool capability declaration: {source}")
        if "module" in tool or "function" in tool:
            if not all(isinstance(tool.get(key), str) and tool[key].strip() for key in ("module", "function")):
                raise ValueError(f"Custom tool requires an exact module/function binding: {source}")
        yield tool


def capture_tool_capabilities(project: Path) -> dict:
    """Capture the trusted registry and exact base custom import references."""
    from agentloom.tools.catalog import list_tool_specs, list_toolsets

    builtins = [
        {"name": spec.name, "module": spec.implementation.module, "function": spec.implementation.attribute}
        for spec in sorted(list_tool_specs(), key=lambda spec: spec.name)
    ]
    custom = {
        (tool["module"], tool["function"])
        for source, document in _documents(project)
        for tool in _tools(source, document)
        if "module" in tool
    }
    closure = {
        "schema_version": 1,
        "builtin_tools": builtins,
        "custom_bindings": [{"module": module, "function": function} for module, function in sorted(custom)],
        "toolsets": {name: list(tools) for name, tools in sorted(list_toolsets().items())},
    }
    validate_tool_capabilities(project, closure)
    return closure


def validate_tool_capabilities(project: Path, closure: Mapping) -> None:
    """Reject a candidate's new Python binding before constructing any Agent."""
    if closure.get("schema_version") != 1:
        raise ValueError("Candidate lacks a frozen tool capability closure")
    names = {item["name"] for item in closure["builtin_tools"]}
    bindings = {(item["module"], item["function"]) for item in (*closure["builtin_tools"], *closure["custom_bindings"])}
    toolsets = closure["toolsets"]
    for source, document in _documents(project):
        for tool in _tools(source, document):
            if "module" in tool:
                if (tool["module"], tool["function"]) not in bindings:
                    raise ValueError(
                        f"Candidate introduces an unapproved Python tool binding in {source}: {tool['name']}"
                    )
            elif tool["name"].strip() not in names:
                raise ValueError(f"Candidate selects a tool outside the frozen registry in {source}: {tool['name']}")
        for key in ("toolsets", "default_toolsets"):
            if key not in document:
                continue
            selected = document[key]
            if not isinstance(selected, list) or any(
                not isinstance(name, str) or (name.strip() and name.strip() not in toolsets) for name in selected
            ):
                raise ValueError(f"Candidate selects a toolset outside the frozen registry in {source}")
