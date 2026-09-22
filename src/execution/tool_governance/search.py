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
        from agentloom.execution.permissions.workspace import get_rule_exclude_paths
        for excl in get_rule_exclude_paths(tool_name):
            if excl.strip():
                dirs.append(excl.strip().rstrip("/") or "/")
    except ImportError:
        pass

    return list(dict.fromkeys(dirs))  # deduplicate preserving order



def validate_shell_query_scope(command: str, cwd: str | None = None) -> None:
    """Only admit proved literal operations when recursive exclusions apply.

    A wrapper deny-list cannot secure an arbitrary Shell program. Constrained
    searches use the dedicated search API; this path admits only literal
    no-query builtins and direct, exclusion-checked file reads.
    """
    import os
    import shlex
    import re
    from pathlib import Path
    from agentloom.execution.tool_governance.shell.shell_command_ast import analyze_shell_command
    query_tools = ("grep_search", "glob_search", "list_directory", "shell_tool")
    if not any(load_exclude_paths(tool) for tool in query_tools):
        return
    analysis = analyze_shell_command(command)
    if any("<" in redirect.operator for redirect in analysis.redirections):
        raise ValueError("Shell input redirection has no verified query exclusion mapping")
    for invocation in analysis.commands:
        name = invocation.name.strip("\"'")
        # Positive literal grammar: shlex alone does not account for brace,
        # pathname, parameter or command expansion in Bash/Zsh.
        if not re.fullmatch(r"[A-Za-z0-9_./%:,=+@ \t'\"-]+", invocation.source):
            raise ValueError("Shell expansion has no verified query exclusion mapping")
        if name in {"printf", "echo", "pwd", "true", "false"}:
            continue
        if name == "cat":
            paths = shlex.split(invocation.source)[1:]
            root = Path(cwd or os.getcwd())
            if paths and all(not path.startswith("-") and not any(search_path_excluded(root / path, root, tool) for tool in query_tools) for path in paths):
                continue
        raise ValueError("Shell query execution has no verified exclusion mapping; use an exclusion-aware search tool")


def search_excludes(tool_name: str, root) -> list[str]:
    """Translate configured exclusions into patterns under the actual query root."""
    from pathlib import Path
    import os
    root = Path(root).resolve()
    from agentloom.execution.trace import capture_explicit_execution_context
    from agentloom.execution.permissions.workspace import get_workspace_root
    hook = capture_explicit_execution_context().hook_run
    workspace = Path(hook.project_root).resolve() if hook is not None and hook.project_root else get_workspace_root()
    patterns = []
    for value in load_exclude_paths(tool_name):
        path = Path(os.path.expanduser(value))
        candidates = [path]
        if not any(c in str(path) for c in "*?["):
            if path.is_absolute():
                candidates.append(path.resolve())
            else:
                candidates.extend(((root / path).resolve(), (workspace / path).resolve()))
        for candidate in dict.fromkeys(candidates):
            value = str(candidate)
            if candidate.is_absolute():
                try:
                    relative = candidate.relative_to(root)
                    value = "*" if str(relative) == "." else relative.as_posix()
                except ValueError:
                    if candidate in root.parents:
                        value = "*"
                    elif any(c in str(candidate) for c in "*?["):
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
    try:
        resolved_relative = path.resolve().relative_to(root.resolve()).as_posix()
    except (ValueError, OSError, RuntimeError):
        return True
    relatives = (relative, resolved_relative)
    candidates = [candidate for value in relatives for candidate in (value, *[str(parent) for parent in Path(value).parents if str(parent) != "."])]
    for pattern in search_excludes(tool_name, root):
        if pattern == "*":
            return True
        if "/" not in pattern and any(fnmatch.fnmatch(part, pattern) for value in relatives for part in Path(value).parts):
            return True
        if any(fnmatch.fnmatch(candidate, pattern) for candidate in candidates):
            return True
    return False
