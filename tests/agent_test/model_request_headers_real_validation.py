"""Functional AgentLoom validation for model request headers.

This script runs real AgentLoom workflows against a local OpenAI-compatible
capture server and asserts the headers received by that server. It is
intentionally outside the normal pytest path because it boots the full runner
repeatedly.
"""

from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
import threading
import time
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from uuid import UUID

PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import src.lib.config.config as config_module
from src.lib.config.model_request_header_profiles import (
    MODEL_REQUEST_HEADER_PROFILES,
)
from src.lib.smolagents.models import model_manager as model_manager_module
from src.lib.smolagents.models.request_headers import GENERIC_MODEL_USER_AGENT
from src.runner import run_app

UUID_SENTINEL = "<uuid>"
SESSION_TOKEN_SENTINEL = "<session-token>"


@dataclass(frozen=True)
class HeaderValidationCase:
    name: str
    system_header_config: dict[str, Any]
    model_extra_headers: dict[str, str] | None
    expected_headers: dict[str, str]
    expected_user_agent: str


class CaptureState:
    def __init__(self) -> None:
        self.requests: list[dict[str, Any]] = []
        self.lock = threading.Lock()

    def append(self, request: dict[str, Any]) -> None:
        with self.lock:
            self.requests.append(request)

    def latest(self) -> dict[str, Any]:
        with self.lock:
            if not self.requests:
                raise AssertionError("local model server received no requests")
            return self.requests[-1]


def _make_handler(state: CaptureState):
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, format: str, *args: Any) -> None:
            return None

        def do_POST(self) -> None:
            length = int(self.headers.get("Content-Length", "0"))
            body = self.rfile.read(length).decode("utf-8", errors="replace")
            state.append(
                {
                    "path": self.path,
                    "headers": dict(self.headers.items()),
                    "body": body,
                }
            )

            payload = {
                "id": f"chatcmpl-{int(time.time() * 1000)}",
                "object": "chat.completion",
                "created": int(time.time()),
                "model": "header-test-model",
                "choices": [
                    {
                        "index": 0,
                        "message": {
                            "role": "assistant",
                            "content": None,
                            "tool_calls": [
                                {
                                    "id": "call_header_validation",
                                    "type": "function",
                                    "function": {
                                        "name": "final_answer",
                                        "arguments": json.dumps({"answer": "HEADER_VALIDATION_PASS"}),
                                    },
                                }
                            ],
                        },
                        "finish_reason": "tool_calls",
                    }
                ],
                "usage": {"prompt_tokens": 8, "completion_tokens": 3, "total_tokens": 11},
            }
            data = json.dumps(payload).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

    return Handler


def _write_workflow(app_root: Path, name: str) -> Path:
    workflow_dir = app_root / "workflows"
    workflow_dir.mkdir(parents=True)
    workflow_path = workflow_dir / f"{name}_agent.yaml"
    workflow_path.write_text(
        f"""name: "{name}"
description: |
  Return exactly HEADER_VALIDATION_PASS.
model_type: "powerful"
tool_call_type: "tool_call"
workflow: |
  Return exactly HEADER_VALIDATION_PASS and do not call tools.
tools: []
worker_agents: []
default_toolsets: []
inject_default_file_tools: false
max_steps: 2
""",
        encoding="utf-8",
    )
    return workflow_path


def _install_temp_config(
    *,
    base_url: str,
    system_header_config: dict[str, Any],
    model_extra_headers: dict[str, str] | None,
    runtime_root: Path,
) -> None:
    system_config = {
        "system": {"name": "AgentLoom", "version": "validation", "user_agent": "AgentLoom/validation"},
        "model_request_headers": system_header_config,
        "lsp_servers": {"enabled": False},
        "checkpoint": {"enabled": False},
        "runtime": {"root_dir": str(runtime_root / ".agentloom")},
        "self_learning": {"enabled": False},
        "skills": [],
        "default_toolsets": [],
        "logging": {
            "level": "ERROR",
            "console_enabled": True,
            "file_enabled": True,
            "max_file_bytes": 25 * 1024 * 1024,
            "backup_count": 3,
        },
    }
    model_config = {
        "model": {
            "default_model_type": "powerful",
            "powerful": {
                "model": "openai/header-test-model",
                "base_url": base_url,
                "api_key": "test-key",
                "temperature": 0,
                "max_tokens": 4096,
                "timeout": 30,
                "num_retries": 1,
                "requests_per_minute": 999999,
                **({"extra_headers": model_extra_headers} if model_extra_headers else {}),
            },
            "summary": {
                "model": "openai/header-test-summary",
                "base_url": base_url,
                "api_key": "test-key",
                "max_tokens": 4096,
                "timeout": 30,
            },
        }
    }
    config_module._ACTIVE_CONFIG = config_module.UnifiedConfig(
        system_config,
        agent_root=PROJECT_ROOT,
        llm_config=config_module.LLMConfig.from_dict(model_config),
    )
    model_manager_module.model_manager.clear_cache()


def _case_headers(headers: dict[str, Any]) -> dict[str, str]:
    return {str(k).lower(): str(v) for k, v in headers.items()}


def _assert_case(case: HeaderValidationCase, captured: dict[str, Any]) -> dict[str, Any]:
    headers = _case_headers(captured["headers"])
    user_agent = headers.get("user-agent", "")
    if user_agent != case.expected_user_agent:
        raise AssertionError(
            f"{case.name}: expected user-agent {case.expected_user_agent!r}, got {user_agent!r}"
        )
    if "agentloom" in user_agent.lower():
        raise AssertionError(f"{case.name}: AgentLoom identity leaked in user-agent: {user_agent!r}")

    for key, expected in case.expected_headers.items():
        actual = headers.get(key.lower())
        if expected == UUID_SENTINEL:
            if actual is None:
                raise AssertionError(f"{case.name}: expected {key} UUID header, got missing")
            UUID(actual)
            continue
        if expected == SESSION_TOKEN_SENTINEL:
            if actual is None or not actual.startswith("ses_"):
                raise AssertionError(f"{case.name}: expected {key} session token, got {actual!r}")
            continue
        if actual != expected:
            raise AssertionError(f"{case.name}: expected {key}={expected!r}, got {actual!r}")

    return {
        "case": case.name,
        "path": captured["path"],
        "user_agent": user_agent,
        "checked_headers": sorted(case.expected_headers),
        "received_header_names": sorted(k for k in headers if k != "authorization"),
    }


def run_case(case: HeaderValidationCase, *, base_url: str, runtime_root: Path) -> str:
    if runtime_root.exists():
        shutil.rmtree(runtime_root)
    runtime_root.mkdir(parents=True)
    app_name = f"_tmp_model_request_headers_{case.name}"
    app_root = PROJECT_ROOT / "applications" / app_name
    if app_root.exists():
        shutil.rmtree(app_root)
    workflow_path = _write_workflow(app_root, app_name)
    _install_temp_config(
        base_url=base_url,
        system_header_config=case.system_header_config,
        model_extra_headers=case.model_extra_headers,
        runtime_root=runtime_root,
    )

    try:
        result = run_app(workflow_path, task_override="Return exactly HEADER_VALIDATION_PASS.")
        if "HEADER_VALIDATION_PASS" not in result:
            raise AssertionError(f"{case.name}: workflow result did not include pass marker: {result!r}")
        return result
    finally:
        if app_root.exists():
            shutil.rmtree(app_root)


def _cases() -> tuple[HeaderValidationCase, ...]:
    opencode_headers = MODEL_REQUEST_HEADER_PROFILES["opencode"]
    custom_claudecode_headers = {
        "User-Agent": "claude-cli/2.1.159 (external, sdk-cli)",
        "X-App": "cli",
        "X-Claude-Code-Session-Id": "${agentloom.session_uuid}",
    }
    return (
        HeaderValidationCase(
            name="cline_builtin",
            system_header_config={
                "profile": "cline",
                "headers": {"X-AgentLoom-Privacy": "enabled"},
            },
            model_extra_headers=None,
            expected_headers={
                "X-AgentLoom-Privacy": "enabled",
            },
            expected_user_agent=MODEL_REQUEST_HEADER_PROFILES["cline"]["User-Agent"],
        ),
        HeaderValidationCase(
            name="kimicode_builtin",
            system_header_config={
                "profile": "kimicode",
                "headers": {"X-AgentLoom-Privacy": "enabled"},
            },
            model_extra_headers=None,
            expected_headers={
                "X-AgentLoom-Privacy": "enabled",
                "X-Stainless-Lang": "js",
                "X-Stainless-Runtime": "node",
            },
            expected_user_agent=MODEL_REQUEST_HEADER_PROFILES["kimicode"]["User-Agent"],
        ),
        HeaderValidationCase(
            name="openclaw_builtin",
            system_header_config={
                "profile": "openclaw",
                "headers": {"X-AgentLoom-Privacy": "enabled"},
            },
            model_extra_headers=None,
            expected_headers={
                "X-AgentLoom-Privacy": "enabled",
                "X-Stainless-Package-Version": "6.39.1",
            },
            expected_user_agent=MODEL_REQUEST_HEADER_PROFILES["openclaw"]["User-Agent"],
        ),
        HeaderValidationCase(
            name="opencode_builtin",
            system_header_config={
                "profile": "opencode",
                "headers": {"X-AgentLoom-Privacy": "enabled"},
            },
            model_extra_headers=None,
            expected_headers={
                "X-AgentLoom-Privacy": "enabled",
                "X-Session-Affinity": SESSION_TOKEN_SENTINEL,
                "X-Session-Id": SESSION_TOKEN_SENTINEL,
            },
            expected_user_agent=opencode_headers["User-Agent"],
        ),
        HeaderValidationCase(
            name="roo_builtin",
            system_header_config={
                "profile": "roo",
                "headers": {"X-AgentLoom-Privacy": "enabled"},
            },
            model_extra_headers=None,
            expected_headers={
                "X-AgentLoom-Privacy": "enabled",
                "HTTP-Referer": "https://github.com/RooVetGit/Roo-Cline",
                "X-Title": "Roo Code",
            },
            expected_user_agent=MODEL_REQUEST_HEADER_PROFILES["roo"]["User-Agent"],
        ),
        HeaderValidationCase(
            name="model_override",
            system_header_config={
                "profile": "opencode",
                "headers": {"X-AgentLoom-Privacy": "enabled"},
            },
            model_extra_headers={"user-agent": "model-client/2.0", "X-Model-Header": "powerful"},
            expected_headers={
                "X-AgentLoom-Privacy": "enabled",
                "X-Model-Header": "powerful",
                "X-Session-Id": SESSION_TOKEN_SENTINEL,
            },
            expected_user_agent="model-client/2.0",
        ),
        HeaderValidationCase(
            name="custom_profile",
            system_header_config={
                "profile": "codex",
                "profiles": {
                    "codex": {
                        "headers": {
                            "User-Agent": "configured-agent/1.0",
                            "X-Client-Profile": "codex",
                        }
                    }
                },
                "headers": {"X-AgentLoom-Privacy": "enabled"},
            },
            model_extra_headers=None,
            expected_headers={
                "X-AgentLoom-Privacy": "enabled",
                "X-Client-Profile": "codex",
            },
            expected_user_agent="configured-agent/1.0",
        ),
        HeaderValidationCase(
            name="custom_session_placeholder",
            system_header_config={
                "profile": "claude_code",
                "profiles": {
                    "claude_code": {
                        "headers": custom_claudecode_headers
                    }
                },
                "headers": {"X-AgentLoom-Privacy": "enabled"},
            },
            model_extra_headers=None,
            expected_headers={
                "X-AgentLoom-Privacy": "enabled",
                "X-App": "cli",
                "X-Claude-Code-Session-Id": UUID_SENTINEL,
            },
            expected_user_agent=custom_claudecode_headers["User-Agent"],
        ),
        HeaderValidationCase(
            name="system_generic",
            system_header_config={"profile": "generic", "headers": {"X-AgentLoom-Privacy": "enabled"}},
            model_extra_headers=None,
            expected_headers={"X-AgentLoom-Privacy": "enabled"},
            expected_user_agent=GENERIC_MODEL_USER_AGENT,
        ),
    )


def main() -> None:
    previous_config = config_module._ACTIVE_CONFIG
    previous_runtime_root = os.environ.get("AGENTLOOM_RUNTIME_ROOT")
    state = CaptureState()
    server = ThreadingHTTPServer(("127.0.0.1", 0), _make_handler(state))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base_url = f"http://127.0.0.1:{server.server_address[1]}/v1"

    tmp_root = Path(tempfile.mkdtemp(prefix="agentloom-header-validation-"))
    reports: list[dict[str, Any]] = []

    try:
        os.environ["AGENTLOOM_RUNTIME_ROOT"] = str(tmp_root / "runtime")
        for case in _cases():
            before = len(state.requests)
            result = run_case(case, base_url=base_url, runtime_root=tmp_root / case.name)
            if len(state.requests) <= before:
                raise AssertionError(f"{case.name}: no new request captured")
            report = _assert_case(case, state.latest())
            report["result"] = result
            reports.append(report)
    finally:
        server.shutdown()
        server.server_close()
        if previous_config is None:
            config_module._ACTIVE_CONFIG = None
        else:
            config_module._ACTIVE_CONFIG = previous_config
        model_manager_module.model_manager.clear_cache()
        if previous_runtime_root is None:
            os.environ.pop("AGENTLOOM_RUNTIME_ROOT", None)
        else:
            os.environ["AGENTLOOM_RUNTIME_ROOT"] = previous_runtime_root
        shutil.rmtree(tmp_root, ignore_errors=True)

    print(json.dumps({"status": "passed", "base_url": base_url, "cases": reports}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
