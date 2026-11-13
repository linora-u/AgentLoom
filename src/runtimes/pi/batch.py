"""Batch-owned Pi processes; each borrowed process still starts a fresh SDK session."""
from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar, copy_context
from threading import Condition
from uuid import uuid4

_POOL = ContextVar('pi_batch_pool', default=None)
_IN_INVOCATION = ContextVar('pi_batch_invocation', default=False)
_ELIGIBLE = ContextVar('pi_batch_transport_eligible', default=False)


class _TransportPool:
    def __init__(self, capacity):
        from agentloom.execution import get_current_run_context

        self.run_context = get_current_run_context()
        self.capacity = capacity
        self.owner = copy_context()
        self.condition = Condition()
        self.idle = []
        self.transports = set()
        self.creating = 0
        self.closed = False
        self.installation = None
        self.installation_error = None

    def _prepare(self):
        from agentloom.runtimes.pi.environment import build_pi_subprocess_env
        from agentloom.runtimes.pi.install import find_node, installed_pi_entry

        node = find_node(build_pi_subprocess_env())
        return node, installed_pi_entry(node=node)

    def acquire(self):
        from agentloom.execution.agent_runtime import AgentRuntimeError
        from agentloom.runtimes.pi.transport import PiTransport

        with self.condition:
            self.condition.wait_for(lambda: self.closed or self.idle or
                                    len(self.transports) + self.creating < self.capacity)
            if self.closed:
                raise AgentRuntimeError('Pi batch has closed', category='interrupted')
            if self.idle:
                transport = self.idle.pop()
                if transport.process.poll() is not None:
                    self.transports.remove(transport)
                    self._retire(transport)
                    self.condition.notify_all()
                    raise AgentRuntimeError('Pi batch process exited between tasks', category='internal')
                return transport, True
            # Probe once in the batch owner's native scope, not in a task's scope.
            # Do not publish partial or failed installation checks.
            if self.installation_error is not None:
                raise AgentRuntimeError(self.installation_error, category="configuration")
            if self.installation is None:
                try:
                    self.installation = self.owner.copy().run(self._prepare)
                except RuntimeError as error:
                    self.installation_error = str(error)
                    raise AgentRuntimeError(str(error), category="configuration") from error
            self.creating += 1
        try:
            transport = self.owner.copy().run(
                PiTransport, uuid4().hex, installation=self.installation,
            )
        except BaseException:
            with self.condition:
                self.creating -= 1
                self.condition.notify_all()
            raise
        with self.condition:
            self.creating -= 1
            self.transports.add(transport)
            self.condition.notify_all()
        return transport, False

    def _retire(self, transport):
        from agentloom.execution.resources import close_instance_resources

        def close():
            transport.close()
            close_instance_resources(transport._resource_instance_id)

        self.owner.copy().run(close)

    def release(self, transport, reusable):
        with self.condition:
            if reusable and not self.closed and transport._failure is None and transport.process.poll() is None:
                self.idle.append(transport)
                self.condition.notify_all()
                return
            self.transports.discard(transport)
            self.condition.notify_all()
        self._retire(transport)

    def close(self):
        with self.condition:
            self.closed = True
            transports = tuple(self.transports)
            self.idle.clear()
            self.condition.notify_all()
        errors = []
        for transport in transports:
            try:
                self._retire(transport)
            except BaseException as error:
                errors.append(error)
        if errors:
            raise BaseExceptionGroup('Pi batch process cleanup failed', errors)


@contextmanager
def batch_transport_scope(capacity):
    from agentloom.execution.cleanup import close_preserving_error
    from agentloom.execution.managed_process import managed_attempt_scope

    # Physical processes outlive individual WorkItems, but not this map call.
    with managed_attempt_scope(independent=True):
        pool = _TransportPool(capacity)
        tokens = (_POOL.set(pool), _IN_INVOCATION.set(False), _ELIGIBLE.set(False))
        try:
            yield
        finally:
            try:
                close_preserving_error(pool.close)
            finally:
                _ELIGIBLE.reset(tokens[2])
                _IN_INVOCATION.reset(tokens[1])
                _POOL.reset(tokens[0])


@contextmanager
def invocation_transport_scope():
    # Nested direct Worker calls keep their own process lifetime. A nested map
    # establishes its own bounded pool instead of waiting for the caller's slot.
    from agentloom.execution import get_current_run_context

    pool = _POOL.get()
    eligible = (pool is not None and get_current_run_context() is pool.run_context
                and not _IN_INVOCATION.get())
    tokens = (_ELIGIBLE.set(eligible), _IN_INVOCATION.set(True))
    try:
        yield
    finally:
        _IN_INVOCATION.reset(tokens[1])
        _ELIGIBLE.reset(tokens[0])


def acquire_transport(definition):
    pool = _POOL.get()
    if pool is None or not _ELIGIBLE.get():
        return None
    _ELIGIBLE.set(False)
    # SDK shell descendants belong to the individual native attempt. Retain
    # their existing per-task cleanup boundary rather than extending it.
    if any(tool.owner == 'runtime' and tool.operation == 'shell' for tool in definition.tool_manifest):
        return None
    transport, reused = pool.acquire()
    return pool, transport, reused
