"""MCP stdio byte transport for a managed Application invocation.

Uses public AnyIO streams/processes and MCP ClientSession/JSONRPC types. The
ordinary MCP SDK transport starts a new session and terminates that group;
it cannot be reused for children sharing the Application's group.
"""
import logging
import subprocess
import sys
from contextlib import asynccontextmanager
from datetime import timedelta
from functools import partial

import anyio
from anyio.abc import ByteReceiveStream, ByteSendStream
from anyio.streams.text import TextReceiveStream

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import get_default_environment
from mcp.shared.message import SessionMessage
from mcp.types import JSONRPCMessage

logger = logging.getLogger(__name__)


class _NativeReceive(ByteReceiveStream):
    def __init__(self, stream):
        self.stream = stream

    async def receive(self, max_bytes=65536):
        try:
            value = await anyio.to_thread.run_sync(self.stream.read, max_bytes)
        except (OSError, ValueError) as error:
            raise anyio.BrokenResourceError from error
        if not value:
            raise anyio.EndOfStream
        return value

    async def aclose(self):
        await anyio.to_thread.run_sync(self.stream.close)


class _NativeSend(ByteSendStream):
    def __init__(self, stream):
        self.stream = stream

    async def send(self, item):
        def write_all():
            remaining = memoryview(item)
            while remaining:
                count = self.stream.write(remaining)
                if not count:
                    raise anyio.BrokenResourceError
                remaining = remaining[count:]

        try:
            await anyio.to_thread.run_sync(write_all)
        except (OSError, ValueError) as error:
            raise anyio.BrokenResourceError from error

    async def aclose(self):
        await anyio.to_thread.run_sync(self.stream.close)


class _NativeProcess:
    """Public AnyIO byte streams around the common synchronous resource owner."""

    def __init__(self, process):
        self.process = process
        self.stdin = _NativeSend(process.stdin)
        self.stdout = _NativeReceive(process.stdout)

    async def wait(self):
        # Cancellable polling avoids abandoning a thread blocked in wait().
        while self.process.poll() is None:
            await anyio.sleep(.02)
        return self.process.returncode

    def terminate(self):
        self.process.terminate()

    def kill(self):
        self.process.kill()

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        with anyio.CancelScope(shield=True):
            await anyio.to_thread.run_sync(self.process.close)


@asynccontextmanager
async def managed_stdio_tools(server: StdioServerParameters, timeout):
    """Connect/discover through ClientSession while preserving task ownership."""
    incoming, read = anyio.create_memory_object_stream(0)
    write, outgoing = anyio.create_memory_object_stream(0)
    try:
        from agentloom.execution.managed_process import launch_owned_process, native_owner_active

        command = [server.command, *server.args]
        options = dict(env={**get_default_environment(), **(server.env or {})}, cwd=server.cwd, stderr=sys.stderr)
        if native_owner_active():
            process = _NativeProcess(await anyio.to_thread.run_sync(partial(
                launch_owned_process, command, stdin=subprocess.PIPE, stdout=subprocess.PIPE, bufsize=0, **options,
            )))
        else:
            process = await anyio.open_process(command, start_new_session=False, **options)
    except BaseException:
        with anyio.CancelScope(shield=True):
            for stream in (incoming, read, write, outgoing):
                await stream.aclose()
        raise

    async def receive():
        assert process.stdout is not None
        try:
            async with incoming:
                buffer = ""
                async for chunk in TextReceiveStream(process.stdout, encoding=server.encoding,
                                                      errors=server.encoding_error_handler):
                    lines = (buffer + chunk).split("\n")
                    buffer = lines.pop()
                    for line in lines:
                        try:
                            value = SessionMessage(JSONRPCMessage.model_validate_json(line))
                        except Exception as error:
                            logger.exception("Failed to parse JSONRPC message from server")
                            value = error
                        await incoming.send(value)
        except (anyio.ClosedResourceError, anyio.BrokenResourceError):
            # ClientSession may close its receiver while a late reply arrives.
            # Preserve the session's original timeout/cancellation error.
            pass

    async def send():
        assert process.stdin is not None
        try:
            async with outgoing:
                async for value in outgoing:
                    line = value.message.model_dump_json(by_alias=True, exclude_none=True) + "\n"
                    await process.stdin.send(line.encode(server.encoding, errors=server.encoding_error_handler))
        except (anyio.ClosedResourceError, anyio.BrokenResourceError):
            pass

    session_timeout = timedelta(seconds=timeout) if isinstance(timeout, float) else (
        timeout if isinstance(timeout, timedelta) else None
    )
    async with anyio.create_task_group() as tasks, process, read, write:
        tasks.start_soon(receive)
        tasks.start_soon(send)
        try:
            async with ClientSession(read, write, session_timeout) as session:
                await session.initialize()
                tools = await session.list_tools()
                yield session, tools.tools
        finally:
            # Cancelled portals must still close/wait this concrete server.
            # Signal only its PID: the external OS owner owns the whole group.
            with anyio.CancelScope(shield=True):
                if process.stdin is not None:
                    try:
                        await process.stdin.aclose()
                    except (anyio.ClosedResourceError, anyio.BrokenResourceError):
                        pass
                with anyio.move_on_after(2) as grace:
                    await process.wait()
                if grace.cancel_called:
                    try:
                        process.terminate()
                    except ProcessLookupError:
                        pass
                    with anyio.move_on_after(0.2) as termination:
                        await process.wait()
                    if termination.cancel_called:
                        try:
                            process.kill()
                        except ProcessLookupError:
                            pass
                        await process.wait()
                tasks.cancel_scope.cancel()
