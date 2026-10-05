"""Codex login coordination without touching the user's real Pi credentials."""
import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
from agentloom.execution.agent_runtime import AgentRuntimeError
from agentloom.runtimes.pi.auth import CodexLoginGate, open_login_browser


def test_concurrent_codex_runs_share_a_failed_login(tmp_path: Path):
    auth_path = tmp_path / "auth.json"
    first = CodexLoginGate(auth_path)
    first.acquire()
    started = threading.Barrier(3)

    def waiting_run():
        gate = CodexLoginGate(auth_path)
        started.wait(timeout=5)
        with pytest.raises(AgentRuntimeError, match="concurrent Pi Codex login"):
            gate.acquire()

    with ThreadPoolExecutor(max_workers=2) as pool:
        waits = [pool.submit(waiting_run) for _ in range(2)]
        started.wait(timeout=5)
        time.sleep(0.1)
        first.release(failed=True)
        for wait in waits:
            wait.result(timeout=5)

    # A new run, started after the failed group, may prompt again.
    later = CodexLoginGate(auth_path)
    later.acquire()
    later.release()


def test_valid_saved_codex_login_needs_no_gate(tmp_path: Path):
    auth_path = tmp_path / "auth.json"
    auth_path.write_text(json.dumps({"openai-codex": {
        "type": "oauth", "expires": time.time() * 1000 + 3_600_000,
    }}))
    gate = CodexLoginGate(auth_path)
    gate.acquire()
    assert gate._file is None


def test_waiting_codex_login_can_be_cancelled(tmp_path: Path):
    auth_path = tmp_path / "auth.json"
    first = CodexLoginGate(auth_path)
    first.acquire()
    try:
        with pytest.raises(AgentRuntimeError, match="interrupted while waiting"):
            CodexLoginGate(auth_path).acquire(cancelled=lambda: True)
    finally:
        first.release()


def test_login_browser_rejects_untrusted_host():
    with pytest.raises(AgentRuntimeError, match="invalid Codex login URL"):
        open_login_browser("https://evil.example/oauth/authorize")
