"""Common query exclusions, independent of a search backend."""
from typing import List

def load_exclude_paths(tool_name: str = "grep_search") -> List[str]:
    """Read ``exclude_paths`` from ``path_validation`` rules for *tool_name*.

    Uses the unified permissions library to get exclude_paths from
    all matching rules for the given tool.  Supports glob patterns
    and ``"*"`` (deny all).

    Returns plain directory names, e.g. ``["secrets", "build/dist"]``.
    """
    dirs: List[str] = []

    try:
        from agentloom.runtime.permissions.workspace import get_rule_exclude_paths
        for excl in get_rule_exclude_paths(tool_name):
            if excl.strip():
                dirs.append(excl.strip().rstrip("/"))
    except ImportError:
        pass

    return list(dict.fromkeys(dirs))  # deduplicate preserving order



def validate_shell_query_scope(command: str) -> None:
    """Refuse Shell traversal when exclusion-aware execution cannot be proved.

    Smol grep/glob have an exclusion-aware executor. Arbitrary Shell searches
    must use that API when the query carries excluded paths; flags in untrusted
    command strings are not accepted as evidence of enforced exclusions.
    """
    from agentloom.runtime.tool_governance.shell.shell_command_ast import analyze_shell_command
    from pathlib import Path
    for invocation in analyze_shell_command(command).commands:
        name = Path(invocation.name.strip("\"'")).name
        tools = {"rg": ("grep_search", "glob_search"), "grep": ("grep_search",), "find": ("glob_search",), "ls": ("list_directory",)}.get(name, ())
        if tools and any(load_exclude_paths(tool) for tool in (*tools, "shell_tool")):
            raise ValueError("Shell query exclusions cannot be enforced by this executor; use an exclusion-aware search tool")


def search_excludes(tool_name: str, root) -> list[str]:
    """Translate configured exclusions into patterns under the actual query root."""
    from pathlib import Path
    import os
    root = Path(root).resolve()
    patterns = []
    for value in load_exclude_paths(tool_name):
        path = Path(os.path.expanduser(value))
        if path.is_absolute():
            try:
                value = path.relative_to(root).as_posix() or "*"
            except ValueError:
                if root == path or path in root.parents:
                    value = "*"
                elif any(c in str(path) for c in "*?["):
                    raise ValueError("Cannot safely map this absolute query exclusion to the search root")
                else:
                    continue
        patterns.append(value)
    return patterns


def search_path_excluded(path, root, tool_name: str) -> bool:
    """Apply the same rule to directories before traversal and files before read."""
    import fnmatch
    from pathlib import Path
    path, root = Path(path), Path(root)
    try:
        relative = path.relative_to(root).as_posix()
    except ValueError:
        return True
    candidates = [relative, *[str(parent) for parent in Path(relative).parents if str(parent) != "."]]
    for pattern in search_excludes(tool_name, root):
        if pattern == "*":
            return True
        if "/" not in pattern and any(fnmatch.fnmatch(part, pattern) for part in Path(relative).parts):
            return True
        if any(fnmatch.fnmatch(candidate, pattern) for candidate in candidates):
            return True
    return False
