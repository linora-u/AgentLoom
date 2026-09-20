"""Pi-owned declarations; execution is provided by the installed official SDK."""
from agentloom.tools.catalog_types import ToolSpec, _spec


def tool_specs() -> tuple[ToolSpec, ...]:
    return (_spec(
        "read", "agentloom.adapters.pi.native_tools", "pi_read", "Read a file with the official Pi SDK.",
        "file_ops", fixed_arg_names=(), is_read_only=True, path_params=("path",),
        owner="runtime", provider="pi", capability="file.read", operation="read",
    ),)
