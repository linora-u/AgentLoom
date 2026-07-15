"""Canonical built-in tool registry.

The registry is the only supported entry point for resolving built-in tool
names.  Importing a function in ``src.tools`` does not make it a public
built-in tool; a tool must have a ``ToolSpec`` here.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Iterable


@dataclass(frozen=True)
class ToolSpec:
    name: str
    function: Callable[..., Any]
    toolset: str
    description: str
    category: str
    is_read_only: bool
    is_destructive: bool
    is_concurrency_safe: bool
    max_result_chars: int | None
    path_params: tuple[str, ...] = ()
    output_kind: str = "text"
    check_fn: Callable[..., Any] | None = None


DEFAULT_TOOLSETS: tuple[str, ...] = (
    "core_shell",
    "core_file",
    "core_search",
    "context",
    "skills",
    "self_learning",
)

_REGISTRY: dict[str, ToolSpec] | None = None
_TOOLSETS: dict[str, tuple[str, ...]] | None = None


def _spec(
    name: str,
    function: Callable[..., Any],
    toolset: str,
    description: str,
    category: str,
    *,
    is_read_only: bool,
    is_destructive: bool = False,
    is_concurrency_safe: bool = True,
    max_result_chars: int | None = 20000,
    path_params: Iterable[str] = (),
    output_kind: str = "text",
    check_fn: Callable[..., Any] | None = None,
) -> ToolSpec:
    return ToolSpec(
        name=name,
        function=function,
        toolset=toolset,
        description=description,
        category=category,
        is_read_only=is_read_only,
        is_destructive=is_destructive,
        is_concurrency_safe=is_concurrency_safe,
        max_result_chars=max_result_chars,
        path_params=tuple(path_params),
        output_kind=output_kind,
        check_fn=check_fn,
    )


def _build_registry() -> dict[str, ToolSpec]:
    from src.tools.context import loom_retrieve_context
    from src.tools.file_ops import (
        append_markdown_sections,
        edit_file,
        get_file_outline,
        list_directory,
        read_file,
        write_file,
        write_markdown_file,
        write_markdown_file_raw,
    )
    from src.tools.search import (
        ast_grep_search_file,
        glob_search,
        grep_search,
        lsp_find_definition,
        lsp_find_references,
        lsp_get_document_symbols,
        lsp_get_workspace_symbols,
        lsp_hover,
    )
    from src.tools.shell import (
        check_background_task,
        kill_background_task,
        list_background_tasks,
        shell_tool,
    )
    from src.tools.self_learning import memory, session_scroll, session_search, skill_manage
    from src.tools.skills import list_skills, load_skill
    from src.tools.todo import todo_write

    specs = [
        _spec(
            "shell_tool",
            shell_tool,
            "core_shell",
            "Run a shell command with AgentLoom shell policy enforcement.",
            "shell",
            is_read_only=False,
            is_concurrency_safe=False,
            max_result_chars=40000,
            output_kind="log",
        ),
        _spec(
            "check_background_task",
            check_background_task,
            "core_shell",
            "Read current output and status for a background shell task.",
            "shell",
            is_read_only=True,
            max_result_chars=5000,
            output_kind="log",
        ),
        _spec(
            "kill_background_task",
            kill_background_task,
            "core_shell",
            "Terminate a running background shell task.",
            "shell",
            is_read_only=False,
            is_destructive=True,
            is_concurrency_safe=False,
            max_result_chars=3000,
            output_kind="log",
        ),
        _spec(
            "list_background_tasks",
            list_background_tasks,
            "core_shell",
            "List active and recent background shell tasks.",
            "shell",
            is_read_only=True,
            max_result_chars=5000,
            output_kind="log",
        ),
        _spec(
            "read_file",
            read_file,
            "core_file",
            "Read a file with pagination and read-state tracking.",
            "file_ops",
            is_read_only=True,
            max_result_chars=None,
            path_params=("file_path",),
            output_kind="code",
        ),
        _spec(
            "edit_file",
            edit_file,
            "core_file",
            "Apply one or more unique text edits to an existing file.",
            "file_ops",
            is_read_only=False,
            is_destructive=True,
            path_params=("file_path",),
            output_kind="text",
        ),
        _spec(
            "write_file",
            write_file,
            "core_file",
            "Create or completely overwrite a file with read-state protection.",
            "file_ops",
            is_read_only=False,
            is_destructive=True,
            path_params=("file_path",),
            output_kind="text",
        ),
        _spec(
            "list_directory",
            list_directory,
            "core_file",
            "List a directory tree with repository-oriented filtering.",
            "file_ops",
            is_read_only=True,
            max_result_chars=12000,
            path_params=("directory_path",),
            output_kind="text",
        ),
        _spec(
            "grep_search",
            grep_search,
            "core_search",
            "Search file contents using ripgrep-compatible regular expressions.",
            "search",
            is_read_only=True,
            max_result_chars=20000,
            path_params=("path",),
            output_kind="search",
        ),
        _spec(
            "glob_search",
            glob_search,
            "core_search",
            "Find files by glob pattern under a directory.",
            "search",
            is_read_only=True,
            max_result_chars=10000,
            path_params=("path",),
            output_kind="search",
        ),
        _spec(
            "loom_retrieve_context",
            loom_retrieve_context,
            "context",
            "Retrieve original content behind a ContextRef.",
            "context",
            is_read_only=True,
            max_result_chars=None,
            output_kind="text",
        ),
        _spec(
            "load_skill",
            load_skill,
            "skills",
            "Load a registered skill by name.",
            "skills",
            is_read_only=True,
            max_result_chars=20000,
            output_kind="text",
        ),
        _spec(
            "list_skills",
            list_skills,
            "skills",
            "List skills visible to the current agent.",
            "skills",
            is_read_only=True,
            max_result_chars=10000,
            output_kind="text",
        ),
        _spec(
            "session_search",
            session_search,
            "self_learning",
            "Search redacted records from prior AgentLoom runs.",
            "self_learning",
            is_read_only=True,
            max_result_chars=30000,
            output_kind="text",
        ),
        _spec(
            "session_scroll",
            session_scroll,
            "self_learning",
            "Scroll around an indexed session event.",
            "self_learning",
            is_read_only=True,
            max_result_chars=30000,
            output_kind="text",
        ),
        _spec(
            "memory",
            memory,
            "self_learning",
            "Read or change curated project/current-application facts that remain useful after this run.",
            "self_learning",
            is_read_only=False,
            is_concurrency_safe=False,
            max_result_chars=20000,
            output_kind="text",
        ),
        _spec(
            "skill_manage",
            skill_manage,
            "self_learning",
            "Create and update generated skill proposal packages.",
            "self_learning",
            is_read_only=False,
            is_concurrency_safe=False,
            max_result_chars=20000,
            output_kind="text",
        ),
        _spec(
            "todo_write",
            todo_write,
            "planning",
            "Write the current task plan.",
            "planning",
            is_read_only=False,
            is_destructive=False,
            is_concurrency_safe=False,
            max_result_chars=10000,
            output_kind="text",
        ),
        _spec(
            "write_markdown_file",
            write_markdown_file,
            "markdown_report",
            "Write a Markdown report from structured sections.",
            "file_ops",
            is_read_only=False,
            is_destructive=True,
            path_params=("file_path",),
            output_kind="markdown",
        ),
        _spec(
            "write_markdown_file_raw",
            write_markdown_file_raw,
            "markdown_report",
            "Write raw Markdown content to a file.",
            "file_ops",
            is_read_only=False,
            is_destructive=True,
            path_params=("file_path",),
            output_kind="markdown",
        ),
        _spec(
            "append_markdown_sections",
            append_markdown_sections,
            "markdown_report",
            "Append structured Markdown sections to an existing report.",
            "file_ops",
            is_read_only=False,
            is_destructive=True,
            path_params=("file_path",),
            output_kind="markdown",
        ),
        _spec(
            "get_file_outline",
            get_file_outline,
            "code_nav",
            "Return a compact outline for a source file.",
            "code_nav",
            is_read_only=True,
            path_params=("file_path",),
            output_kind="code",
        ),
        _spec(
            "ast_grep_search_file",
            ast_grep_search_file,
            "code_nav",
            "Search source structure in one file using AST-aware matching.",
            "code_nav",
            is_read_only=True,
            path_params=("file_path",),
            output_kind="search",
        ),
        _spec(
            "lsp_find_definition",
            lsp_find_definition,
            "code_nav",
            "Find a symbol definition using LSP or tree-sitter fallback.",
            "code_nav",
            is_read_only=True,
            path_params=("file_path",),
            output_kind="code",
        ),
        _spec(
            "lsp_find_references",
            lsp_find_references,
            "code_nav",
            "Find symbol references using LSP or tree-sitter fallback.",
            "code_nav",
            is_read_only=True,
            path_params=("file_path",),
            output_kind="search",
        ),
        _spec(
            "lsp_get_document_symbols",
            lsp_get_document_symbols,
            "code_nav",
            "List document symbols for one file.",
            "code_nav",
            is_read_only=True,
            path_params=("file_path",),
            output_kind="code",
        ),
        _spec(
            "lsp_hover",
            lsp_hover,
            "code_nav",
            "Return hover/type information at a source location.",
            "code_nav",
            is_read_only=True,
            path_params=("file_path",),
            output_kind="code",
        ),
        _spec(
            "lsp_get_workspace_symbols",
            lsp_get_workspace_symbols,
            "code_nav",
            "Search symbols in a workspace directory.",
            "code_nav",
            is_read_only=True,
            path_params=("directory",),
            output_kind="search",
        ),
    ]
    registry = {spec.name: spec for spec in specs}
    if len(registry) != len(specs):
        names = [spec.name for spec in specs]
        duplicates = sorted({name for name in names if names.count(name) > 1})
        raise RuntimeError(f"Duplicate ToolSpec names: {duplicates}")
    return registry


def _ensure_registry() -> dict[str, ToolSpec]:
    global _REGISTRY
    if _REGISTRY is None:
        _REGISTRY = _build_registry()
    return _REGISTRY


def _ensure_toolsets() -> dict[str, tuple[str, ...]]:
    global _TOOLSETS
    if _TOOLSETS is None:
        grouped: dict[str, list[str]] = {}
        for spec in _ensure_registry().values():
            grouped.setdefault(spec.toolset, []).append(spec.name)
        _TOOLSETS = {toolset: tuple(names) for toolset, names in grouped.items()}
    return _TOOLSETS


def get_tool_spec(tool_name: str) -> ToolSpec:
    name = str(tool_name or "").strip()
    spec = _ensure_registry().get(name)
    if spec is None:
        available = ", ".join(sorted(_ensure_registry()))
        raise ValueError(f"Tool '{tool_name}' is not a registered built-in tool. Available tools: {available}")
    return spec


def resolve_tool_function(tool_name: str) -> Callable[..., Any]:
    return get_tool_spec(tool_name).function


def list_tool_specs() -> tuple[ToolSpec, ...]:
    return tuple(_ensure_registry().values())


def list_toolsets() -> dict[str, tuple[str, ...]]:
    return dict(_ensure_toolsets())


def resolve_toolsets(toolsets: Iterable[str] | None) -> list[str]:
    raw_toolsets = DEFAULT_TOOLSETS if toolsets is None else tuple(toolsets)
    available_toolsets = _ensure_toolsets()
    result: list[str] = []
    seen: set[str] = set()
    for raw_name in raw_toolsets:
        name = str(raw_name or "").strip()
        if not name:
            continue
        tools = available_toolsets.get(name)
        if tools is None:
            available = ", ".join(sorted(available_toolsets))
            raise ValueError(f"Unknown toolset '{name}'. Available toolsets: {available}")
        for tool_name in tools:
            if tool_name not in seen:
                seen.add(tool_name)
                result.append(tool_name)
    return result
