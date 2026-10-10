"""Configuration paths belong to the project or Application declaring them."""
from __future__ import annotations

import os
from copy import deepcopy
from pathlib import Path


def configuration_root(source_path: Path | str, project_root: Path | str) -> Path:
    project = Path(project_root).expanduser().absolute()
    source = resolve_config_path(str(source_path), project)
    current = source.parent
    while current != current.parent:
        if current.name == "workflows":
            app = current.parent
            return app if app.is_relative_to(project / "applications") else project
        if current == project:
            break
        current = current.parent
    return project


def resolve_config_path(value: str, root: Path | str) -> Path:
    """Normalize a declared path without erasing symlink components."""
    path = Path(value).expanduser()
    if not path.is_absolute():
        path = Path(root) / path
    return Path(os.path.abspath(path))


def normalize_config_paths(data: dict, root: Path | str) -> dict:
    """Resolve filesystem fields before merging so inherited values keep their root."""
    result = deepcopy(data)

    def path(value):
        return str(resolve_config_path(value, root)) if isinstance(value, str) and value.strip() else value

    mcp = result.get("mcp_servers")
    if isinstance(mcp, str):
        result["mcp_servers"] = path(mcp)
    elif isinstance(mcp, list):
        result["mcp_servers"] = [path(value) for value in mcp]
    elif isinstance(mcp, dict):
        if "path" in mcp:
            mcp["path"] = path(mcp["path"])
        if isinstance(mcp.get("paths"), list):
            mcp["paths"] = [path(value) for value in mcp["paths"]]
    options = result.get("runtime_options")
    if isinstance(options, dict) and "prompt_template_path" in options:
        options["prompt_template_path"] = path(options["prompt_template_path"])
    access = result.get("tool_access_control")
    if isinstance(access, dict):
        rules = access.get("path_validation")
        for rule in rules if isinstance(rules, list) else []:
            if not isinstance(rule, dict):
                continue
            for key in ("include_paths", "exclude_paths"):
                if isinstance(rule.get(key), list):
                    rule[key] = [value if value == "*" else path(value) for value in rule[key]]
    shell = result.get("shell_settings")
    if isinstance(shell, dict) and isinstance(shell.get("dangerous_paths"), list):
        shell["dangerous_paths"] = [value if value == "*" else path(value) for value in shell["dangerous_paths"]]
    return result
