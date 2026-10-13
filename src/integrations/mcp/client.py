"""MCP client wired to AgentLoom's protocol-aware tool adapter."""

from __future__ import annotations

from concurrent.futures import Future
from functools import partial
from threading import Event, Lock, RLock
from types import TracebackType
from typing import Any

import anyio
from agentloom.integrations.mcp.adapter import AgentLoomMCPAdapter
from agentloom.execution.tool_gateway import ToolBinding
from anyio.from_thread import start_blocking_portal
from mcp import ClientSession, StdioServerParameters
from mcp.types import CallToolResult
from mcpadapt.core import mcptools


class _MCPConnection:
    """Own one async transport and cancel every synchronous waiter on close."""

    def __init__(self, parameters: Any, options: dict[str, Any]) -> None:
        unknown = options.keys() - {"connect_timeout", "client_session_timeout_seconds"}
        if unknown:
            raise TypeError(f"Unsupported MCP connection options: {sorted(unknown)}")
        self._parameters = parameters
        self._tool_timeout = options.get("client_session_timeout_seconds")
        self._lock = Lock()
        self._closed = False
        self._pending: set[Future[CallToolResult]] = set()
        self._ready = Event()
        self._session: ClientSession | None = None
        self.tools: list[ToolBinding] = []
        self._portal_context = start_blocking_portal(name="agentloom-mcp")
        self._portal = self._portal_context.__enter__()
        self._lifecycle: Future[None] | None = None
        self._cancel_scope: anyio.CancelScope | None = None
        try:
            self._lifecycle, self._cancel_scope = self._portal.start_task(self._serve)
            self._lifecycle.add_done_callback(lambda _: self._ready.set())
            if not self._ready.wait(timeout=options.get("connect_timeout", 30)):
                raise TimeoutError("Timed out connecting to the MCP server")
            if self._session is None:
                self._lifecycle.result()
                raise RuntimeError("MCP connection ended before discovery")
        except BaseException as startup_error:
            try:
                self.close()
            except BaseException as cleanup_error:
                if cleanup_error is not startup_error:
                    raise BaseExceptionGroup("MCP startup and cleanup failed", [startup_error, cleanup_error]) from None
            raise

    async def _serve(self, *, task_status=anyio.TASK_STATUS_IGNORED) -> None:
        # Cancel the async scope, never the lifecycle Future: a cancelled
        # concurrent Future discards exceptions raised during transport cleanup.
        with anyio.CancelScope() as scope:
            task_status.started(scope)
            await self._serve_transport()

    async def _serve_transport(self) -> None:
        from agentloom.execution.process_scope import managed_process_group
        if isinstance(self._parameters, StdioServerParameters) and managed_process_group() is not None:
            from agentloom.integrations.mcp.managed_stdio import managed_stdio_tools
            transport = managed_stdio_tools(self._parameters, self._tool_timeout)
        else:
            transport = mcptools(self._parameters, self._tool_timeout)
        async with transport as (session, discovered):
            adapter = AgentLoomMCPAdapter()
            self.tools = [adapter.adapt(partial(self.call_tool, tool.name), tool) for tool in discovered]
            self._session = session
            self._ready.set()
            await anyio.sleep_forever()

    def call_tool(self, name: str, arguments: dict | None) -> CallToolResult:
        with self._lock:
            if self._closed or self._session is None:
                raise RuntimeError("MCP connection is closed")
            future = self._portal.start_task_soon(self._session.call_tool, name, arguments)
            self._pending.add(future)
        try:
            return future.result()
        finally:
            with self._lock:
                self._pending.discard(future)

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
            pending = tuple(self._pending)
            self._pending.clear()
        for future in pending:
            future.cancel()
        try:
            if self._cancel_scope is not None:
                self._portal.call(self._cancel_scope.cancel)
            if self._lifecycle is not None:
                self._lifecycle.result()
        finally:
            # Join the portal even when transport cleanup fails; the original
            # lifecycle result remains visible to the synchronous caller.
            self._portal_context.__exit__(None, None, None)


class AgentLoomMCPClient:
    """Synchronous MCP connection exposing runtime-neutral tools."""

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

        self._parameters = parameters
        self._adapter_kwargs = dict(adapter_kwargs or {})
        self._connection: _MCPConnection | None = None
        self._lifecycle_lock = RLock()
        self.connect()

    def connect(self) -> None:
        with self._lifecycle_lock:
            if self._connection is None:
                self._connection = _MCPConnection(self._parameters, self._adapter_kwargs)

    def get_tools(self) -> list[ToolBinding]:
        with self._lifecycle_lock:
            if self._connection is None:
                raise ValueError("Couldn't retrieve tools from MCP server, connect the client first")
            return list(self._connection.tools)

    def disconnect(
        self,
        exc_type: type[BaseException] | None = None,
        exc_value: BaseException | None = None,
        exc_traceback: TracebackType | None = None,
    ) -> None:
        with self._lifecycle_lock:
            connection = self._connection
            self._connection = None
            if connection is not None:
                connection.close()

    def __enter__(self) -> list[ToolBinding]:
        return self.get_tools()

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        exc_traceback: TracebackType | None,
    ) -> None:
        self.disconnect(exc_type, exc_value, exc_traceback)
