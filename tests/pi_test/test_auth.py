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


def test_subscription_capture_retains_refresh_authority_in_parent_store(tmp_path, monkeypatch):
    from agentloom.runtimes.pi.credentials import capture_pi_credentials

    source = tmp_path / "auth.json"
    source.write_text(json.dumps({
        "openai-codex": {"type": "oauth", "access": "selected-access",
                         "refresh": "parent-refresh", "expires": (time.time() + 3600) * 1000},
        "other-provider": {"type": "api_key", "key": "unrelated-secret"},
    }))
    original = source.read_bytes()
    monkeypatch.setenv("PI_CODING_AGENT_DIR", str(tmp_path))
    captured = capture_pi_credentials({"models": {"business": {"adapter": "openai_codex_responses"}}})
    assert set(captured) == {"openai-codex"}
    assert captured["openai-codex"]["access"] == "selected-access"
    assert captured["openai-codex"]["refresh"] == ""
    assert source.read_bytes() == original


def test_private_memory_credentials_guard_remembers_rotation_without_enabling_ordinary_runs():
    import subprocess

    from agentloom.runtimes.pi.environment import build_pi_subprocess_env
    from agentloom.runtimes.pi.install import find_node, installed_pi_entry

    env = build_pi_subprocess_env()
    node = find_node(env)
    bridge = installed_pi_entry(node=node).parent
    script = '''
const {memoryCredentials} = await import(process.argv[1]);
const {credentialGuard} = await import(process.argv[2]);
const guard = credentialGuard([]);
const store = memoryCredentials({provider:{type:'oauth',access:'old-access',refresh:'old-refresh',expires:1}}, guard.remember);
await store.modify('provider', () => ({type:'oauth',access:'new-access',refresh:'new-refresh',expires:2}));
const bound = ['old-access','old-refresh','new-access','new-refresh'].every(value => guard.contains({id:value}));
const ordinary = credentialGuard(undefined);
ordinary.remember(await store.read('provider'));
console.log(JSON.stringify({bound, ordinaryInactive:!ordinary.contains('new-access')}));
'''
    result = subprocess.run([node, "--input-type=module", "-e", script,
        (bridge / "credentials.js").as_uri(), (bridge / "privacy.js").as_uri()],
        env=env, check=True, capture_output=True, text=True)
    assert json.loads(result.stdout) == {"bound": True, "ordinaryInactive": True}
