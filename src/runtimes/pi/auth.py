"""Coordinate Codex subscription login across concurrent Pi runs."""
from __future__ import annotations

import fcntl
import json
import os
import sys
import time
import webbrowser
from collections.abc import Callable
from pathlib import Path
from urllib.parse import urlparse

from agentloom.execution.agent_runtime import AgentRuntimeError

LOGIN_WAIT_SECONDS = 16 * 60


def _has_current_codex_login(auth_path: Path) -> bool:
    try:
        credential = json.loads(auth_path.read_text()).get("openai-codex", {})
        return (credential.get("type") == "oauth"
                and isinstance(credential.get("expires"), (int, float))
                and credential["expires"] > time.time() * 1000 + 60_000)
    except (OSError, ValueError, TypeError, AttributeError):
        return False


class CodexLoginGate:
    """Keep only auth setup serialized; model work starts after releasing it."""

    def __init__(self, auth_path: Path):
        self.auth_path = auth_path
        self._file = None
        self._locked = False
        self._started_ns = time.time_ns()

    def acquire(self, *, cancelled: Callable[[], bool] = lambda: False) -> None:
        if _has_current_codex_login(self.auth_path):
            return
        lock_path = self.auth_path.with_name(".agentloom-codex-login.lock")
        lock_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
        self._file = os.fdopen(fd, "r+")
        deadline = time.monotonic() + LOGIN_WAIT_SECONDS
        try:
            while True:
                if cancelled():
                    raise AgentRuntimeError("Pi run interrupted while waiting for Codex login", category="interrupted")
                try:
                    fcntl.flock(self._file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                    self._locked = True
                    break
                except BlockingIOError:
                    if time.monotonic() >= deadline:
                        raise AgentRuntimeError("Timed out waiting for Pi Codex login", category="provider") from None
                    time.sleep(0.2)
            self._file.seek(0)
            previous_failure = self._file.read().strip()
            if not _has_current_codex_login(self.auth_path) and previous_failure.isdecimal() and int(previous_failure) >= self._started_ns:
                raise AgentRuntimeError("A concurrent Pi Codex login was not completed", category="provider")
            if _has_current_codex_login(self.auth_path):
                self.release()
        except BaseException:
            self.release(preserve=True)
            raise

    def release(self, *, failed: bool = False, preserve: bool = False) -> None:
        if self._file is None:
            return
        try:
            if self._locked and not preserve:
                self._file.seek(0)
                self._file.truncate()
                if failed:
                    self._file.write(str(time.time_ns()))
                self._file.flush()
        finally:
            if self._locked:
                fcntl.flock(self._file.fileno(), fcntl.LOCK_UN)
            self._file.close()
            self._file = None
            self._locked = False


def open_login_browser(url: str) -> bool:
    """Open only the Pi OAuth URL; never persist its state/PKCE parameters."""
    parsed = urlparse(url)
    if parsed.scheme != "https" or parsed.hostname != "auth.openai.com" or parsed.username or parsed.password:
        raise AgentRuntimeError("Pi supplied an invalid Codex login URL", category="internal")
    try:
        opened = webbrowser.open(url, new=1, autoraise=True)
    except (OSError, webbrowser.Error):
        opened = False
    if not opened:
        print(f"Pi Codex login: open this URL in a browser on this computer: {url}", file=sys.stderr, flush=True)
    return opened


def show_device_code(url: str, code: str) -> None:
    parsed = urlparse(url)
    if parsed.scheme != "https" or parsed.hostname != "auth.openai.com" or parsed.username or parsed.password:
        raise AgentRuntimeError("Pi supplied an invalid Codex device login URL", category="internal")
    print(f"Pi Codex login: visit {url} and enter code {code}", file=sys.stderr, flush=True)
    open_login_browser(url)
