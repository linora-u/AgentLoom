"""Deterministic tools for probing browser-harness from AgentLoom."""

from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import tempfile
import time
import urllib.request
from pathlib import Path
from typing import Any


DEFAULT_DEMO_URL = "https://github.com/browser-use/browser-harness"
DEFAULT_TIMEOUT_SECONDS = 90
REAL_MODE_NAME = "agentloom-real-probe"


def _json(data: dict[str, Any]) -> str:
    return json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True, default=str)


def _browser_harness_command() -> str | None:
    path = shutil.which("browser-harness")
    if path:
        return path
    user_local = Path.home() / ".local" / "bin" / "browser-harness"
    if user_local.exists():
        return str(user_local)
    return None


def _chrome_command() -> str | None:
    env_path = os.environ.get("BH_CHROME_PATH") or os.environ.get("CHROME_PATH")
    candidates = [
        env_path,
        "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
        "/Applications/Google Chrome Canary.app/Contents/MacOS/Google Chrome Canary",
        shutil.which("google-chrome-stable"),
        shutil.which("google-chrome"),
        shutil.which("chromium"),
        shutil.which("chromium-browser"),
    ]
    for candidate in candidates:
        if candidate and Path(candidate).exists():
            return str(candidate)
    return None


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _wait_for_devtools(port: int, process: subprocess.Popen, timeout_seconds: float) -> None:
    deadline = time.time() + timeout_seconds
    last_error = ""
    while time.time() < deadline:
        if process.poll() is not None:
            raise RuntimeError(f"isolated Chrome exited early with code {process.returncode}")
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/json/version", timeout=0.5) as response:
                if response.status == 200:
                    return
        except Exception as exc:  # noqa: BLE001 - this is a polling loop.
            last_error = str(exc)
        time.sleep(0.2)
    raise TimeoutError(f"isolated Chrome DevTools endpoint did not open on port {port}: {last_error}")


def _start_isolated_chrome() -> tuple[subprocess.Popen, dict[str, str], str, int]:
    chrome = _chrome_command()
    if not chrome:
        raise FileNotFoundError("Chrome executable not found. Set BH_CHROME_PATH or install Google Chrome.")

    port = _free_port()
    cache_root = Path.home() / "Library" / "Caches" / "AgentLoom" / "browser_harness_probe"
    if os.name != "posix" or not str(cache_root).startswith(str(Path.home())):
        cache_root = Path(tempfile.gettempdir()) / "agentloom-browser-harness-probe"
    cache_root.mkdir(parents=True, exist_ok=True)
    profile_dir = tempfile.mkdtemp(prefix="chrome-profile-", dir=str(cache_root))

    process = subprocess.Popen(
        [
            chrome,
            f"--remote-debugging-port={port}",
            f"--user-data-dir={profile_dir}",
            "--no-first-run",
            "--no-default-browser-check",
            "--new-window",
            "about:blank",
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    _wait_for_devtools(port, process, timeout_seconds=20)

    env = os.environ.copy()
    env["BU_CDP_URL"] = f"http://127.0.0.1:{port}"
    env["BU_NAME"] = f"agentloom-isolated-{port}"
    return process, env, profile_dir, port


def _stop_daemon(env: dict[str, str]) -> dict[str, Any]:
    command = _browser_harness_command()
    if not command:
        return {"success": False, "error": "browser-harness command not found"}
    completed = subprocess.run(
        [command, "--reload"],
        text=True,
        capture_output=True,
        env=env,
        timeout=15,
        check=False,
    )
    return {
        "success": completed.returncode == 0,
        "returncode": completed.returncode,
        "stdout": completed.stdout,
        "stderr": completed.stderr,
    }


def _run_harness(script: str, env: dict[str, str], timeout_seconds: int) -> dict[str, Any]:
    command = _browser_harness_command()
    if not command:
        return {
            "success": False,
            "error": "browser-harness command not found; run `uv tool install -e /Users/bytedance/code/browser-harness`.",
        }
    started_at = time.monotonic()
    try:
        completed = subprocess.run(
            [command],
            input=script,
            text=True,
            capture_output=True,
            env=env,
            timeout=timeout_seconds,
            check=False,
        )
        elapsed = round(time.monotonic() - started_at, 3)
        return {
            "success": completed.returncode == 0,
            "returncode": completed.returncode,
            "stdout": completed.stdout,
            "stderr": completed.stderr,
            "elapsed_seconds": elapsed,
            "command": command,
        }
    except subprocess.TimeoutExpired as exc:
        elapsed = round(time.monotonic() - started_at, 3)
        return {
            "success": False,
            "returncode": None,
            "stdout": exc.stdout or "",
            "stderr": exc.stderr or "",
            "elapsed_seconds": elapsed,
            "error": f"browser-harness timed out after {timeout_seconds}s",
            "command": command,
        }


def browser_harness_doctor(timeout_seconds: str = "30") -> str:
    """Run `browser-harness --doctor` and return structured diagnostics.

    Args:
        timeout_seconds: Maximum seconds to wait for the doctor command.

    Returns:
        JSON text with command, return code, stdout, stderr, and success.
    """

    command = _browser_harness_command()
    if not command:
        return _json({"success": False, "error": "browser-harness command not found"})

    started_at = time.monotonic()
    try:
        completed = subprocess.run(
            [command, "--doctor"],
            text=True,
            capture_output=True,
            timeout=int(timeout_seconds),
            check=False,
        )
        return _json(
            {
                "success": completed.returncode == 0,
                "returncode": completed.returncode,
                "stdout": completed.stdout,
                "stderr": completed.stderr,
                "elapsed_seconds": round(time.monotonic() - started_at, 3),
                "command": command,
            }
        )
    except subprocess.TimeoutExpired as exc:
        return _json(
            {
                "success": False,
                "returncode": None,
                "stdout": exc.stdout or "",
                "stderr": exc.stderr or "",
                "elapsed_seconds": round(time.monotonic() - started_at, 3),
                "error": f"browser-harness --doctor timed out after {timeout_seconds}s",
                "command": command,
            }
        )


def run_browser_harness_python(script: str, browser_mode: str = "real", timeout_seconds: str = "60") -> str:
    """Run Python code through browser-harness via stdin.

    Args:
        script: Python code to execute with browser-harness helpers pre-imported.
        browser_mode: `real` connects to the user's Chrome; `isolated` launches a separate Chrome profile.
        timeout_seconds: Maximum seconds to wait for the harness command.

    Returns:
        JSON text with mode, stdout, stderr, return code, elapsed time, and setup metadata.
    """

    mode = browser_mode.strip().lower()
    if mode not in {"real", "isolated"}:
        return _json({"success": False, "error": "browser_mode must be `real` or `isolated`", "mode": mode})
    if not script.strip():
        return _json({"success": False, "error": "script must be non-empty", "mode": mode})

    timeout = int(timeout_seconds)
    if mode == "real":
        env = os.environ.copy()
        env.pop("BU_CDP_URL", None)
        env.pop("BU_CDP_WS", None)
        env["BU_NAME"] = REAL_MODE_NAME
        result = _run_harness(script, env=env, timeout_seconds=timeout)
        result.update(
            {
                "mode": mode,
                "setup": {
                    "connection": "real Chrome via chrome://inspect remote debugging",
                    "bu_name": REAL_MODE_NAME,
                },
            }
        )
        return _json(result)

    chrome_process: subprocess.Popen | None = None
    env: dict[str, str] | None = None
    profile_dir = ""
    port: int | None = None
    daemon_stop: dict[str, Any] | None = None
    try:
        chrome_process, env, profile_dir, port = _start_isolated_chrome()
        result = _run_harness(script, env=env, timeout_seconds=timeout)
        daemon_stop = _stop_daemon(env)
    except Exception as exc:  # noqa: BLE001 - return failures as tool output.
        result = {"success": False, "error": str(exc)}
    finally:
        if env is not None and daemon_stop is None:
            try:
                daemon_stop = _stop_daemon(env)
            except Exception as exc:  # noqa: BLE001
                daemon_stop = {"success": False, "error": str(exc)}
        if chrome_process is not None and chrome_process.poll() is None:
            chrome_process.terminate()
            try:
                chrome_process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                chrome_process.kill()

    result.update(
        {
            "mode": mode,
            "setup": {
                "connection": "isolated Chrome via BU_CDP_URL",
                "profile_dir": profile_dir,
                "port": port,
                "bu_cdp_url": f"http://127.0.0.1:{port}" if port is not None else None,
                "daemon_stop": daemon_stop,
            },
        }
    )
    return _json(result)


def run_demo_probe(
    url: str = DEFAULT_DEMO_URL,
    browser_mode: str = "isolated",
    timeout_seconds: str = str(DEFAULT_TIMEOUT_SECONDS),
) -> str:
    """Open a URL in a new tab with browser-harness and print page_info().

    Args:
        url: URL to open. Defaults to the browser-harness GitHub repository.
        browser_mode: `real` or `isolated`.
        timeout_seconds: Maximum seconds to wait for the harness command.

    Returns:
        JSON text from run_browser_harness_python.
    """

    safe_url = json.dumps(url)
    script = f"""
import json

target_url = {safe_url}
new_tab(target_url)
wait_for_load()
info = page_info()
print(json.dumps({{"target_url": target_url, "page_info": info}}, ensure_ascii=False, default=str))
"""
    return run_browser_harness_python(
        script=script,
        browser_mode=browser_mode,
        timeout_seconds=timeout_seconds,
    )
