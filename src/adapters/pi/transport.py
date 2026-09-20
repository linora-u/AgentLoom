"""Correlated JSONL transport. Readers never execute callbacks outside the Run context."""
from __future__ import annotations

from dataclasses import dataclass, field
from concurrent.futures import ThreadPoolExecutor
from contextvars import copy_context
import os
from pathlib import Path
from queue import Queue, Empty
import signal
import sys
import subprocess
from tempfile import TemporaryDirectory
from threading import Lock, RLock, Thread
import time
from typing import Callable
from uuid import uuid4

from agentloom.adapters.pi.protocol import (
    BridgeError, Cancel, Close, Event, Request, RequestPayload, Response,
    decode_message, encode_message,
)
from agentloom.adapters.pi.install import find_node
from agentloom.runtime.agent_runtime import AgentRuntimeError, RuntimeErrorCategory
from agentloom.runtime.resources import register_resource
from agentloom.runtime.subprocess_env import build_subprocess_env


@dataclass
class Pending:
    request: Request
    queue: Queue = field(default_factory=Queue)
    sequence: int = 0


class PiTransport:
    def __init__(self, instance_id: str):
        bridge = Path(__file__).parent / "bridge"
        entry = bridge / "dist/index.js"
        env = build_subprocess_env()
        for name in list(env):
            if name.startswith(("PI_", "NODE_")):
                env.pop(name)
        # AgentLoom's other tools may prepend their bundled Node 18. Select a
        # compatible executable without mutating the process-wide PATH.
        try:
            node = find_node(env)
        except RuntimeError as exc:
            raise AgentRuntimeError(str(exc), category="configuration") from None
        if not entry.is_file() or not (bridge / "node_modules/@earendil-works/pi-coding-agent").is_dir():
            raise AgentRuntimeError(
                "Pi SDK is not installed. Run uv run loom install-runtime pi.",
                category="configuration",
            )
        self.instance_id = instance_id
        self._lock = RLock()
        self._write_lock = Lock()
        self._pending: dict[str, Pending] = {}
        self._callbacks: set[str] = set()
        self._failure: AgentRuntimeError | None = None
        self._closed = False
        self._closing = False
        self._directory = TemporaryDirectory(prefix="agentloom-pi-")
        env.update(HOME=self._directory.name, XDG_CONFIG_HOME=self._directory.name)
        try:
            self.process = subprocess.Popen(
                [node, str(entry), self._directory.name], stdin=subprocess.PIPE,
                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, env=env,
                start_new_session=True, cwd=self._directory.name,
            )
        except OSError:
            self._directory.cleanup()
            raise AgentRuntimeError("Cannot start Pi bridge", category="configuration") from None
        self._reader = Thread(target=self._read, name=f"pi-reader-{self.process.pid}", daemon=True)
        self._reader.start()
        register_resource(f"pi:{self.process.pid}", self.close, instance_id=instance_id)

    def _write(self, message: Request | Response):
        with self._write_lock:
            try:
                assert self.process.stdin is not None
                self.process.stdin.write(encode_message(message).encode())
                self.process.stdin.flush()
            except (OSError, ValueError):
                self._fail("Pi bridge input closed")
                assert self._failure is not None
                raise self._failure from None

    def _fail(self, message: str, category: RuntimeErrorCategory = "internal"):
        with self._lock:
            if self._failure is None:
                self._failure = AgentRuntimeError(message, category=category)
                for pending in self._pending.values():
                    pending.queue.put(self._failure)

    def _read(self):
        try:
            assert self.process.stdout is not None
            while line := self.process.stdout.readline(8 * 1024 * 1024 + 1):
                if len(line) > 8 * 1024 * 1024 or not line.endswith(b"\n"):
                    raise ValueError()
                message = decode_message(line.decode("utf-8"))
                with self._lock:
                    if message.instance_id != self.instance_id:
                        raise ValueError()
                    if isinstance(message, Request):
                        if (not message.request_id.startswith("pi:") or message.request_id in self._callbacks or message.payload.method not in
                            {"tool_prepare", "tool_settle", "platform_invoke", "model_prepare"} or not any(
                                item.request.run_id == message.run_id and item.request.payload.method == "run"
                                for item in self._pending.values())):
                            raise ValueError()
                        self._callbacks.add(message.request_id)
                        pending = next(item for item in self._pending.values()
                                       if item.request.run_id == message.run_id and item.request.payload.method == "run")
                        pending.queue.put(message)
                    else:
                        pending = self._pending.get(message.request_id)
                        if pending is None or message.run_id != pending.request.run_id:
                            raise ValueError()
                        if isinstance(message, Event):
                            if pending.request.payload.method != "run" or message.sequence != pending.sequence + 1:
                                raise ValueError()
                            pending.sequence = message.sequence
                        else:
                            if message.payload is not None and message.payload.method != pending.request.payload.method:
                                raise ValueError()
                            del self._pending[message.request_id]
                        pending.queue.put(message)
            self._fail("Pi bridge exited before request completion", "interrupted" if self._closing else "internal")
        except Exception:
            self._fail("Pi bridge protocol failure")
            self._terminate()

    def request(self, payload: RequestPayload, *, run_id=None, observe: Callable[[Event], None] | None = None,
                timeout: float | None = None, callback=None, cancel_callbacks: Callable[[], None] | None = None) -> Response:
        request = Request(version=1, kind="request", instance_id=self.instance_id, run_id=run_id,
                          request_id=f"host:{uuid4().hex}", payload=payload)
        pending = Pending(request)
        with self._lock:
            if self._failure is not None:
                raise self._failure
            if self._closed or (self._closing and not isinstance(payload, Close)):
                raise AgentRuntimeError("Pi bridge is closed", category="interrupted")
            self._pending[request.request_id] = pending
        self._write(request)
        deadline = time.monotonic() + timeout if timeout is not None else None
        executor = ThreadPoolExecutor(max_workers=8, thread_name_prefix=f"pi-callback-{self.process.pid}") if callback else None
        completed = False

        def dispatch(message):
            try:
                assert callback is not None
                result = callback(message.payload)
                response = Response(version=1, kind="response", instance_id=self.instance_id,
                    run_id=message.run_id, request_id=message.request_id, payload=result)
                pending.queue.put((message, response))
            except BaseException as error:
                category = "protocol" if isinstance(error, AgentRuntimeError) and error.category == "internal" else "tool"
                pending.queue.put((message, Response(version=1, kind="response", instance_id=self.instance_id,
                    run_id=message.run_id, request_id=message.request_id,
                    error=BridgeError(category=category, message="Pi tool callback failed"))))

        try:
            while True:
                remaining = max(0, deadline - time.monotonic()) if deadline else None
                message = pending.queue.get(timeout=remaining)
                if isinstance(message, BaseException):
                    raise message
                if isinstance(message, tuple):
                    self._write(message[1])
                    if message[1].error:
                        self._fail("Pi bridge protocol failure" if message[1].error.category == "protocol" else "Pi tool callback failed")
                        self._terminate()
                    continue
                if isinstance(message, Request):
                    if callback is None:
                        self._write(Response(version=1, kind="response", instance_id=self.instance_id,
                            run_id=message.run_id, request_id=message.request_id,
                            error=BridgeError(category="unsupported_capability", message="Pi tools are not enabled")))
                    else:
                        assert executor is not None
                        executor.submit(copy_context().run, dispatch, message)
                    continue
                if isinstance(message, Event):
                    if observe:
                        observe(message)
                    continue
                if message.error:
                    error = message.error
                    raise AgentRuntimeError(error.message, category="internal" if error.category == "protocol" else error.category,
                                            retryable=error.retryable)
                completed = getattr(message.payload, "state", "success") not in {"failed", "interrupted"}
                return message
        except KeyboardInterrupt:
            self.cancel()
            raise
        except Empty:
            self._fail("Pi bridge request timed out")
            self.close()
            assert self._failure is not None
            raise self._failure from None
        finally:
            if executor is not None:
                # Stop queued work before releasing running callbacks' resources.
                # Otherwise a freed worker can start a new Hook during cleanup.
                if not completed:
                    executor.shutdown(wait=False, cancel_futures=True)
                if not completed and cancel_callbacks is not None:
                    active_error = sys.exception()
                    try:
                        cancel_callbacks()
                    except Exception as cleanup_error:
                        if active_error is not None and active_error is not cleanup_error:
                            active_error.add_note(f"Pi callback cleanup failed: {type(cleanup_error).__name__}")
                # Never let a live Python callback mutate a finalized Application.
                # Native process cancellation stays serviceable on the reader thread.
                executor.shutdown(wait=True, cancel_futures=True)

    def cancel(self) -> bool:
        with self._lock:
            runs = [item.request for item in self._pending.values() if item.request.payload.method == "run"]
        accepted = False
        for run in runs:
            response = self.request(Cancel(method="cancel", target_request_id=run.request_id), run_id=run.run_id, timeout=2)
            accepted = accepted or bool(getattr(response.payload, "accepted", False))
        return accepted

    def _terminate(self):
        if self.process.poll() is None:
            try:
                os.killpg(self.process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass

    def close(self):
        with self._lock:
            if self._closed or self._closing:
                return
            self._closing = True
        try:
            if self.process.poll() is None:
                try:
                    self.request(Close(method="close"), timeout=2)
                    self.process.wait(timeout=2)
                except (AgentRuntimeError, subprocess.TimeoutExpired):
                    self._terminate()
                    self.process.wait(timeout=2)
        finally:
            self._closed = True
            self._reader.join(timeout=2)
            for pipe in (self.process.stdin, self.process.stdout):
                if pipe is not None:
                    pipe.close()
            # Validate buffered frames before installing a synthetic close reason.
            # A late duplicate response must retain its protocol failure.
            self._fail("Pi bridge closed", category="interrupted")
            self._directory.cleanup()
        if self._failure is not None and self._failure.category != "interrupted":
            raise self._failure
