"""Optional adapter-owned audit sink; policy never imports adapter resources."""
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any
_sink: ContextVar[Any] = ContextVar("shell_policy_audit", default=None)

@contextmanager
def policy_audit(sink):
    token = _sink.set(sink)
    try:
        yield
    finally:
        _sink.reset(token)

class _NoAudit:
    def __getattr__(self, name):
        return lambda *args, **kwargs: None

def get_shell_audit_logger():
    return _sink.get() or _NoAudit()
