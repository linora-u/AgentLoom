"""MCP client wired to AgentLoom's protocol-aware tool adapter."""

from __future__ import annotations

from types import TracebackType
from typing import Any

from mcpadapt.core import MCPAdapt

from src.mcp.adapter import AgentLoomSmolAgentsAdapter


class AgentLoomMCPClient:
    """Small synchronous MCP client compatible with ``smolagents.MCPClient``."""

    def __init__(
        self,
        server_parameters: Any,
        adapter_kwargs: dict[str, Any] | None = None,
    ) -> None:
        parameters = server_parameters
        if isinstance(parameters, dict):
            parameters = dict(parameters)
            transport = parameters.setdefault("transport", "streamable-http")
            if transport not in {"sse", "streamable-http"}:
                raise ValueError(
                    f"Unsupported transport: {transport}. Supported transports are 'streamable-http' and 'sse'."
                )

        self._adapter = MCPAdapt(
            parameters,
            AgentLoomSmolAgentsAdapter(structured_output=True),
            **(adapter_kwargs or {}),
        )
        self._tools: list[Any] | None = None
        self.connect()

    def connect(self) -> None:
        self._tools = self._adapter.__enter__()

    def get_tools(self) -> list[Any]:
        if self._tools is None:
            raise ValueError("Couldn't retrieve tools from MCP server, connect the client first")
        return self._tools

    def disconnect(
        self,
        exc_type: type[BaseException] | None = None,
        exc_value: BaseException | None = None,
        exc_traceback: TracebackType | None = None,
    ) -> None:
        self._adapter.__exit__(exc_type, exc_value, exc_traceback)
        self._tools = None

    def __enter__(self) -> list[Any]:
        return self.get_tools()

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        exc_traceback: TracebackType | None,
    ) -> None:
        self.disconnect(exc_type, exc_value, exc_traceback)
