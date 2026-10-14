"""Trusted attempt authority, separate from business grants and queue capacity."""
from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path

from .files import read_json

_AUTHORITY = ContextVar("agentloom_attempt_authority", default=None)


def assert_current_attempt():
    check = _AUTHORITY.get()
    if check is not None:
        check()


@contextmanager
def bind_attempt_authorization(job: Path, invocation_id: str):
    from .os_job import AdmissionStopped
    def check():
        if (job / "cancelled.json").exists():
            raise AdmissionStopped("Execution attempt was cancelled before dispatch")
        authorization = job / "authorized.json"
        if not authorization.exists() or read_json(authorization).get("invocation_id") != invocation_id:
            raise AdmissionStopped("Execution attempt has no current startup permission")

    check()
    token = _AUTHORITY.set(check)
    try:
        yield
    finally:
        _AUTHORITY.reset(token)
