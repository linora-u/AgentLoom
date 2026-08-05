"""Pure metadata catalog for AgentLoom's built-in tools.

Reading this module never imports a tool implementation.  Runtime code must
cross the explicit ``src.tools.loader`` seam to turn an implementation
reference into a callable.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass


@dataclass(frozen=True)
class ToolImplementation:
    """Import reference for a tool implementation without importing it."""

    module: str
    attribute: str


@dataclass(frozen=True)
class ToolSpec:
    name: str
    implementation: ToolImplementation
    toolset: str
    description: str
    category: str
    is_read_only: bool
    is_destructive: bool
    is_concurrency_safe: bool
    max_result_chars: int | None
    fixed_arg_names: tuple[str, ...]
    accepts_extra_fixed_args: bool = False
    path_params: tuple[str, ...] = ()
    output_kind: str = "text"


DEFAULT_TOOLSETS: tuple[str, ...] = (
    "core_shell",
    "core_file",
    "core_search",
    "context",
    "skills",
    "self_learning",
)

_CATALOG: dict[str, ToolSpec] | None = None
_TOOLSETS: dict[str, tuple[str, ...]] | None = None


def _spec(
    name: str,
    implementation_module: str,
    toolset: str,
    description: str,
    category: str,
    *,
    fixed_arg_names: Iterable[str],
    accepts_extra_fixed_args: bool = False,
    is_read_only: bool,
    is_destructive: bool = False,
    is_concurrency_safe: bool = True,
    max_result_chars: int | None = 20000,
    path_params: Iterable[str] = (),
    output_kind: str = "text",
) -> ToolSpec:
    return ToolSpec(
        name=name,
        implementation=ToolImplementation(
            module=implementation_module,
            attribute=name,
        ),
        toolset=toolset,
        description=description,
        category=category,
        is_read_only=is_read_only,
        is_destructive=is_destructive,
        is_concurrency_safe=is_concurrency_safe,
        max_result_chars=max_result_chars,
        fixed_arg_names=tuple(fixed_arg_names),
        accepts_extra_fixed_args=accepts_extra_fixed_args,
        path_params=tuple(path_params),
        output_kind=output_kind,
    )


def _build_catalog() -> dict[str, ToolSpec]:
    specs = [
        _spec(
            "shell_tool",
            "src.tools.shell.shell_tool",
            "core_shell",
            "Run a shell command with AgentLoom shell policy enforcement.",
            "shell",
            fixed_arg_names=("command", "timeout", "load_profile", "run_in_background"),
            is_read_only=False,
            is_concurrency_safe=False,
            max_result_chars=40000,
            output_kind="log",
        ),
        _spec(
            "check_background_task",
            "src.tools.shell.background_task_tools",
            "core_shell",
            "Read current output and status for a background shell task.",
            "shell",
            fixed_arg_names=("task_id",),
            is_read_only=True,
            max_result_chars=5000,
            output_kind="log",
        ),
        _spec(
            "kill_background_task",
            "src.tools.shell.background_task_tools",
            "core_shell",
            "Terminate a running background shell task.",
            "shell",
            fixed_arg_names=("task_id",),
            is_read_only=False,
            is_destructive=True,
            is_concurrency_safe=False,
            max_result_chars=3000,
            output_kind="log",
        ),
        _spec(
            "list_background_tasks",
            "src.tools.shell.background_task_tools",
            "core_shell",
            "List active and recent background shell tasks.",
            "shell",
            fixed_arg_names=(),
            is_read_only=True,
            max_result_chars=5000,
            output_kind="log",
        ),
        _spec(
            "read_file",
            "src.tools.file_ops.read_file",
            "core_file",
            "Read a file with pagination and read-state tracking.",
            "file_ops",
            fixed_arg_names=("file_path", "offset", "limit"),
            is_read_only=True,
            max_result_chars=None,
            path_params=("file_path",),
            output_kind="code",
        ),
        _spec(
            "edit_file",
            "src.tools.file_ops.edit_file",
            "core_file",
            "Apply one or more unique text edits to an existing file.",
            "file_ops",
            fixed_arg_names=("file_path", "edits"),
            is_read_only=False,
            is_destructive=True,
            path_params=("file_path",),
            output_kind="text",
        ),
        _spec(
            "write_file",
            "src.tools.file_ops.write_file",
            "core_file",
            "Create or completely overwrite a file with read-state protection.",
            "file_ops",
            fixed_arg_names=("file_path", "content"),
            is_read_only=False,
            is_destructive=True,
            path_params=("file_path",),
            output_kind="text",
        ),
        _spec(
            "list_directory",
            "src.tools.file_ops.directory_browser",
            "core_file",
            "List a directory tree with repository-oriented filtering.",
            "file_ops",
            fixed_arg_names=(
                "directory_path",
                "max_depth",
                "max_output_lines",
                "show_file_counts",
                "show_file_info",
                "include_hidden",
                "exclude_patterns",
                "count_timeout_seconds",
            ),
            is_read_only=True,
            max_result_chars=12000,
            path_params=("directory_path",),
            output_kind="text",
        ),
        _spec(
            "grep_search",
            "src.tools.search.grep_tool",
            "core_search",
            "Search file contents using ripgrep-compatible regular expressions.",
            "search",
            fixed_arg_names=(
                "pattern",
                "path",
                "include",
                "output_mode",
                "case_insensitive",
                "context_lines",
                "before_context",
                "after_context",
                "multiline",
                "max_results",
                "offset",
            ),
            is_read_only=True,
            max_result_chars=20000,
            path_params=("path",),
            output_kind="search",
        ),
        _spec(
            "glob_search",
            "src.tools.search.glob_tool",
            "core_search",
            "Find files by glob pattern under a directory.",
            "search",
            fixed_arg_names=("pattern", "path", "max_results", "sort_by"),
            is_read_only=True,
            max_result_chars=10000,
            path_params=("path",),
            output_kind="search",
        ),
        _spec(
            "loom_retrieve_context",
            "src.tools.context.retrieve_context",
            "context",
            "Retrieve original content behind a ContextRef.",
            "context",
            fixed_arg_names=("ref", "query", "offset", "limit"),
            is_read_only=True,
            max_result_chars=None,
            output_kind="text",
        ),
        _spec(
            "load_skill",
            "src.tools.skills.skill_tool",
            "skills",
            "Load a registered skill by name.",
            "skills",
            fixed_arg_names=("skill", "args"),
            is_read_only=True,
            max_result_chars=20000,
            output_kind="text",
        ),
        _spec(
            "list_skills",
            "src.tools.skills.skill_tool",
            "skills",
            "List skills visible to the current agent.",
            "skills",
            fixed_arg_names=("include_description", "detail"),
            is_read_only=True,
            max_result_chars=10000,
            output_kind="text",
        ),
        _spec(
            "session_search",
            "src.tools.self_learning.session_tools",
            "self_learning",
            "Search redacted records from prior AgentLoom runs.",
            "self_learning",
            fixed_arg_names=("query", "limit", "agent", "app", "since", "scope"),
            is_read_only=True,
            max_result_chars=30000,
            output_kind="text",
        ),
        _spec(
            "session_scroll",
            "src.tools.self_learning.session_tools",
            "self_learning",
            "Scroll around an indexed session event.",
            "self_learning",
            fixed_arg_names=("run_id", "event_id", "direction", "window"),
            is_read_only=True,
            max_result_chars=30000,
            output_kind="text",
        ),
        _spec(
            "memory",
            "src.tools.self_learning.memory_tool",
            "self_learning",
            "Read or change curated project/current-application facts that remain useful after this run.",
            "self_learning",
            fixed_arg_names=(
                "action",
                "scope",
                "kind",
                "memory_key",
                "text",
                "trigger",
                "symptom",
                "learned_action",
                "verification",
            ),
            is_read_only=False,
            is_concurrency_safe=False,
            max_result_chars=20000,
            output_kind="text",
        ),
        _spec(
            "skill_manage",
            "src.tools.self_learning.skill_manage_tool",
            "self_learning",
            "Create and update generated skill proposal packages.",
            "self_learning",
            fixed_arg_names=(
                "action",
                "name",
                "content",
                "old_string",
                "new_string",
                "path",
                "target",
            ),
            is_read_only=False,
            is_concurrency_safe=False,
            max_result_chars=20000,
            output_kind="text",
        ),
        _spec(
            "todo_write",
            "src.tools.todo.todo_write",
            "planning",
            "Write the current task plan.",
            "planning",
            fixed_arg_names=("sanitize_inputs_outputs",),
            accepts_extra_fixed_args=True,
            is_read_only=False,
            is_destructive=False,
            is_concurrency_safe=False,
            max_result_chars=10000,
            output_kind="text",
        ),
        _spec(
            "write_markdown_file",
            "src.tools.file_ops.markdown_writer",
            "markdown_report",
            "Write a Markdown report from structured sections.",
            "file_ops",
            fixed_arg_names=(
                "file_path",
                "sections",
                "title",
                "metadata",
                "overwrite",
                "create_directories",
                "encoding",
            ),
            is_read_only=False,
            is_destructive=True,
            path_params=("file_path",),
            output_kind="markdown",
        ),
        _spec(
            "write_markdown_file_raw",
            "src.tools.file_ops.markdown_writer",
            "markdown_report",
            "Write raw Markdown content to a file.",
            "file_ops",
            fixed_arg_names=(
                "file_path",
                "content_b64",
                "content_plain",
                "overwrite",
                "create_directories",
                "encoding",
            ),
            is_read_only=False,
            is_destructive=True,
            path_params=("file_path",),
            output_kind="markdown",
        ),
        _spec(
            "append_markdown_sections",
            "src.tools.file_ops.markdown_writer",
            "markdown_report",
            "Append structured Markdown sections to an existing report.",
            "file_ops",
            fixed_arg_names=("file_path", "sections", "encoding"),
            is_read_only=False,
            is_destructive=True,
            path_params=("file_path",),
            output_kind="markdown",
        ),
        _spec(
            "get_file_outline",
            "src.tools.file_ops.file_outliner",
            "code_nav",
            "Return a compact outline for a source file.",
            "code_nav",
            fixed_arg_names=(
                "file_path",
                "detail_level",
                "max_size_mb",
                "encoding",
                "include_line_numbers",
                "max_items_per_section",
            ),
            is_read_only=True,
            path_params=("file_path",),
            output_kind="code",
        ),
        _spec(
            "ast_grep_search_file",
            "src.tools.search.ast_grep_tool",
            "code_nav",
            "Search source structure in one file using AST-aware matching.",
            "code_nav",
            fixed_arg_names=("file_path", "keyword", "language"),
            is_read_only=True,
            path_params=("file_path",),
            output_kind="search",
        ),
        _spec(
            "lsp_find_definition",
            "src.tools.search.lsp_tool",
            "code_nav",
            "Find a symbol definition using LSP or tree-sitter fallback.",
            "code_nav",
            fixed_arg_names=("file_path", "line", "character", "language"),
            is_read_only=True,
            path_params=("file_path",),
            output_kind="code",
        ),
        _spec(
            "lsp_find_references",
            "src.tools.search.lsp_tool",
            "code_nav",
            "Find symbol references using LSP or tree-sitter fallback.",
            "code_nav",
            fixed_arg_names=("file_path", "line", "character", "language", "max_results"),
            is_read_only=True,
            path_params=("file_path",),
            output_kind="search",
        ),
        _spec(
            "lsp_get_document_symbols",
            "src.tools.search.lsp_tool",
            "code_nav",
            "List document symbols for one file.",
            "code_nav",
            fixed_arg_names=("file_path", "language"),
            is_read_only=True,
            path_params=("file_path",),
            output_kind="code",
        ),
        _spec(
            "lsp_hover",
            "src.tools.search.lsp_tool",
            "code_nav",
            "Return hover/type information at a source location.",
            "code_nav",
            fixed_arg_names=("file_path", "line", "character", "language"),
            is_read_only=True,
            path_params=("file_path",),
            output_kind="code",
        ),
        _spec(
            "lsp_get_workspace_symbols",
            "src.tools.search.lsp_tool",
            "code_nav",
            "Search symbols in a workspace directory.",
            "code_nav",
            fixed_arg_names=("directory", "query", "language", "max_results"),
            is_read_only=True,
            path_params=("directory",),
            output_kind="search",
        ),
    ]
    catalog = {spec.name: spec for spec in specs}
    if len(catalog) != len(specs):
        names = [spec.name for spec in specs]
        duplicates = sorted({name for name in names if names.count(name) > 1})
        raise RuntimeError(f"Duplicate ToolSpec names: {duplicates}")
    return catalog


def _ensure_catalog() -> dict[str, ToolSpec]:
    global _CATALOG
    if _CATALOG is None:
        _CATALOG = _build_catalog()
    return _CATALOG


def _ensure_toolsets() -> dict[str, tuple[str, ...]]:
    global _TOOLSETS
    if _TOOLSETS is None:
        grouped: dict[str, list[str]] = {}
        for spec in _ensure_catalog().values():
            grouped.setdefault(spec.toolset, []).append(spec.name)
        _TOOLSETS = {toolset: tuple(names) for toolset, names in grouped.items()}
    return _TOOLSETS


def get_tool_spec(tool_name: str) -> ToolSpec:
    name = str(tool_name or "").strip()
    spec = _ensure_catalog().get(name)
    if spec is None:
        available = ", ".join(sorted(_ensure_catalog()))
        raise ValueError(f"Tool '{tool_name}' is not a registered built-in tool. Available tools: {available}")
    return spec


def list_tool_specs() -> tuple[ToolSpec, ...]:
    return tuple(_ensure_catalog().values())


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
