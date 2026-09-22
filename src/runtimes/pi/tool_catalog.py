"""Pi-owned declarations; execution is provided by the installed official SDK."""
from agentloom.tools.catalog_types import ToolSpec, _spec


def tool_specs() -> tuple[ToolSpec, ...]:
    return (_spec(
        "read", "agentloom.runtimes.pi.native_tools", "pi_read", "Read a file with the official Pi SDK.",
        "file_ops", fixed_arg_names=(), is_read_only=True, path_params=("path",),
        owner="runtime", provider="pi", logical_name="read_file", capability="file.read", operation="read",
    ), _spec(
        "write", "agentloom.runtimes.pi.native_tools", "pi_write", "Write a file with the official Pi SDK.",
        "file_ops", fixed_arg_names=(), is_read_only=False, is_destructive=True, is_concurrency_safe=False,
        path_params=("path",), owner="runtime", provider="pi", logical_name="write_file", capability="file.write", operation="write",
    ), _spec(
        "edit", "agentloom.runtimes.pi.native_tools", "pi_edit", "Edit a file with the official Pi SDK.",
        "file_ops", fixed_arg_names=(), is_read_only=False, is_destructive=True, is_concurrency_safe=False,
        path_params=("path",), owner="runtime", provider="pi", logical_name="edit_file", capability="file.edit", operation="write",
    ), _spec(
        "bash", "agentloom.runtimes.pi.native_tools", "pi_bash", "Run a command with the official Pi SDK.",
        "shell", fixed_arg_names=(), is_read_only=False, is_destructive=True, is_concurrency_safe=False,
        owner="runtime", provider="pi", logical_name="shell_tool", capability="shell.execute", operation="shell",
        command_parameter="command",
    ))
