"""Run with an installed profile's Python, outside the source checkout.

Only the HTTP model is a fixture. SDK loops, Application, tools and CLI are real.
The release verifier copies this file and the MCP peer into its evidence directory.
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import importlib.metadata
import importlib.util
import hashlib
import json
import os
from pathlib import Path
import signal
import shutil
import subprocess
import sys
from threading import Thread
from typing import Any

import psutil
import yaml


@contextmanager
def model_service(profile, calls, failure=None, answer="PROFILE-APPLICATION-OK"):
    requests = []
    killed_children = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format: str, *args):
            pass

        def do_POST(self):
            payload = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            requests.append(payload)
            if failure == "child":
                for process in psutil.Process().children(recursive=True):
                    if any(arg.endswith("/bridge/dist/index.js") for arg in process.cmdline()):
                        killed_children.append(process.pid)
                        os.kill(process.pid, signal.SIGKILL)
            if failure:
                self.send_response(401)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(b'{"error":{"message":"PRIVATE-PROVIDER-ERROR","type":"authentication_error"}}')
                return
            index = len(requests) - 1
            call = calls[index] if index < len(calls) else None
            if call is None and profile == "smol":
                call = ("final_answer", {"answer": answer})
            message: dict[str, Any] = {"role": "assistant", "content": answer}
            finish = "stop"
            if call is not None:
                name, arguments = call
                message["content"] = None
                message["tool_calls"] = [{"id": f"call_{index}", "type": "function",
                    "function": {"name": name, "arguments": json.dumps(arguments)}}]
                finish = "tool_calls"
            self.send_response(200)
            usage = {"prompt_tokens": 20, "completion_tokens": 5, "total_tokens": 25}
            if payload.get("stream"):
                self.send_header("Content-Type", "text/event-stream")
                self.end_headers()
                if call is not None:
                    message["tool_calls"][0]["index"] = 0
                for chunk in [{"choices": [{"index": 0, "delta": message, "finish_reason": None}]},
                              {"choices": [{"index": 0, "delta": {}, "finish_reason": finish}], "usage": usage}]:
                    self.wfile.write(("data: " + json.dumps(chunk) + "\n\n").encode())
                self.wfile.write(b"data: [DONE]\n\n")
            else:
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(json.dumps({"id": "profile_fixture", "object": "chat.completion", "created": 1,
                    "model": payload["model"], "choices": [{"index": 0, "message": message,
                    "finish_reason": finish}], "usage": usage}).encode())

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}/v1", requests, killed_children
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def identity(profile, code_tools=False):
    import agentloom
    origin = Path(agentloom.__file__).resolve()
    assert "site-packages" in origin.parts, origin
    assert not importlib.util.find_spec("src"), "source tree leaked into environment"
    distributions = {d.metadata["Name"].lower().replace("_", "-") for d in importlib.metadata.distributions()}
    professional = {"serena-agent", "ast-grep-cli", "grep-ast", "tree-sitter-language-pack",
                    "go-bin", "nodejs-bin", "libclang", "tree-sitter-c", "networkx"}
    if code_tools:
        assert professional <= distributions, professional - distributions
    elif profile == "pi":
        assert not professional & distributions, professional & distributions
    if profile == "pi":
        assert "smolagents" not in distributions
        assert "openinference-instrumentation-smolagents" not in distributions
        assert importlib.util.find_spec("smolagents") is None
    else:
        assert importlib.metadata.version("smolagents") == "1.26.0"
    return {"package_origin": str(origin), "python": sys.version, "profile": profile, "code_tools": code_tools,
            "installed_distributions": sorted(distributions)}


def configure(workspace, profile, case, url):
    config = workspace / "config"
    config.mkdir(parents=True)
    source = workspace / "sample.py"
    source.write_text("def installed_entrypoint(value):\n    return value + 42\n")
    typescript = workspace / "sample.ts"
    typescript.write_text("export function installed_entrypoint(value: number) { return value + 42; }\n")
    (workspace / "note.txt").write_text("INSTALLED-READ-7541\n")
    system = {"runtime": {"root_dir": str(workspace / "runtime")},
        "checkpoint": {"enabled": False}, "logging": {"console_enabled": False},
        "self_learning": {"enabled": case == "memory"}, "lsp_servers": {"enabled": False},
        "default_toolsets": []}
    model = {"model": "openai/profile-fixture", "adapter": "openai_chat", "base_url": url,
        "api_key": "synthetic-fixture", "context_window": 32768, "max_output_tokens": 1000,
        "timeout": 10, "num_retries": 0, "requests_per_minute": 2000000}
    if case == "shipped_yaml":
        system.pop("default_toolsets")  # Exercise the shipped sample's default toolsets.
    (config / "system.yaml").write_text(yaml.safe_dump(system))
    (config / "llm.yaml").write_text(yaml.safe_dump({"model": {
        "default_model_type": "probe", "probe": model, "summary": model, "powerful": model}}))
    definition = {"name": "installed_profile", "agent_runtime": "pi" if profile == "pi" else "smolagents",
        "description": "Verify installed Application tools.", "workflow": "Execute the selected tool and report its result.",
        "tools": [], "toolsets": []}
    if profile == "smol":
        definition["runtime_options"] = {"smart_summary": False, "todo_mode": "off"}
    calls = []
    marker = None
    if case in {"read", "smol_read"}:
        name = "read" if profile == "pi" else "read_file"
        arguments = {"path" if profile == "pi" else "file_path": str(workspace / "note.txt")}
        calls = [(name, arguments)]
        marker = "INSTALLED-READ-7541"
    elif case.startswith("outline"):
        calls = [("get_file_outline", {"file_path": str(typescript if case.endswith("typescript") else source)})]
        marker = "installed_entrypoint"
    elif case == "ast":
        calls = [("ast_grep_search_file", {"file_path": str(source), "keyword": "installed_entrypoint", "language": "python"})]
        marker = "installed_entrypoint"
    elif case == "lsp":
        calls = [("lsp_get_document_symbols", {"file_path": str(source), "language": "python"})]
        marker = "installed_entrypoint"
    elif case == "memory":
        calls = [("memory", {"action": "list", "scope": "app"}),
                 ("memory", {"action": "list", "scope": "project"}),
                 ("session_search", {"query": "installed profile", "scope": "current_app"})]
    elif case == "goal":
        definition["goal"] = {"enabled": True}
        calls = [("get_goal", {}), ("update_goal", {"status": "complete", "evidence": "Installed Application verified."})]
    elif case == "mcp":
        mcp = config / "mcp.json"
        mcp.write_text(json.dumps({"mcpServers": {"facts": {"command": sys.executable,
            "args": [str(Path(__file__).with_name("stdio_server.py")), str(workspace / "mcp-events.jsonl")]}}}))
        definition["mcp_servers"] = str(mcp)
        calls = [("mcp__facts__lookup", {"query": "installed-mcp"})]
        marker = "installed-mcp"
    definition["tools"] = [{"name": name} for name in sorted({call[0] for call in calls})
                           if name not in {"get_goal", "update_goal"} and not name.startswith("mcp__")]
    if case in {"smol_read", "missing_smol"}:
        definition.update(agent_runtime="smolagents",
                          runtime_options={"max_steps": 3, "smart_summary": False, "todo_mode": "off"})
    app = workspace / "applications/probe/workflows/root.yaml"
    app.parent.mkdir(parents=True)
    app.write_text(yaml.safe_dump(definition))
    if case == "shipped_yaml":
        shutil.copyfile(Path(__file__).with_name("shipped-smol.yaml"), app)
    return app, calls, marker


def _pi_damage_target(case):
    if case == "stale_bridge":
        import agentloom
        return Path(agentloom.__file__).parent / "runtimes/pi/bridge/tools.ts"
    if case == "missing_asset":
        from agentloom.runtimes.pi.install import pi_runtime_root
        return pi_runtime_root() / "bridge/dist/tools.js"
    raise ValueError(f"Unsupported Pi damage case: {case}")


def run(profile, case, workspace, code_tools=False):
    workspace.mkdir(parents=True, exist_ok=False)
    evidence = identity(profile, code_tools)
    os.chdir(workspace)
    failure = {"provider_failure": "provider", "child_failure": "child"}.get(case)
    calls = []
    answer = "TODO_OFF_OK MOOL-4" if case == "shipped_yaml" else "PROFILE-APPLICATION-OK"
    with model_service(profile, calls, failure, answer) as (url, requests, killed_children):
        app, planned, marker = configure(workspace, profile, case, url)
        calls.extend(planned)
        if case in {"missing_yaml", "missing_smol", "missing_sdk", "stale_bridge", "missing_asset", "provider_failure", "child_failure", "help"}:
            command = [sys.executable, "-I", "-m", "agentloom"]
            command += ["--help"] if case == "help" else ["run", "missing.yaml" if case == "missing_yaml" else str(app), "--output-format", "jsonl"]
            env = dict(os.environ, AGENTLOOM_PROJECT_ROOT=str(workspace))
            damaged = None
            original = None
            if case in {"stale_bridge", "missing_asset"}:
                damaged = _pi_damage_target(case)
                original = damaged.read_bytes()
                if case == "stale_bridge":
                    damaged.write_bytes(original + b"\n// newly installed package source\n")
                else:
                    damaged.unlink()
            try:
                result = subprocess.run(command, cwd=workspace, env=env, capture_output=True, text=True, timeout=60)
            finally:
                if damaged is not None and original is not None:
                    damaged.write_bytes(original)
            (workspace / "stdout.log").write_text(result.stdout)
            (workspace / "stderr.log").write_text(result.stderr)
            assert result.returncode == (0 if case == "help" else 1), result.stderr
            assert "Traceback" not in result.stderr, result.stderr
            if case != "help":
                rows = [json.loads(line) for line in result.stdout.splitlines()]
                assert any(row["event"] in {"run.rejected", "run.failed"} for row in rows), rows
                assert "PRIVATE-PROVIDER-ERROR" not in result.stdout + result.stderr
            if case == "missing_smol":
                assert "AgentLoom[smol]" in result.stderr, result.stderr
            if case in {"missing_sdk", "stale_bridge", "missing_asset"}:
                assert "runtime install pi" in result.stderr, result.stderr
                assert not requests
            if failure:
                assert len(requests) == 1, requests
            if case == "child_failure":
                assert len(killed_children) == 1
                assert not psutil.pid_exists(killed_children[0])
                assert "bridge" in (result.stdout + result.stderr).lower()
            evidence.update(exit_code=result.returncode, model_requests=len(requests))
            if killed_children:
                evidence["terminated_sdk_children"] = killed_children
        else:
            from agentloom.app.runner import execute_app
            from agentloom.config.config import bind_config, load_project_config
            with bind_config(load_project_config(workspace)):
                result = execute_app(app, file_logging=True)
            assert result.output == answer, result.output
            if case == "shipped_yaml":
                assert app.read_bytes() == Path(__file__).with_name("shipped-smol.yaml").read_bytes()
                evidence["yaml_sha256"] = hashlib.sha256(app.read_bytes()).hexdigest()
                sample = yaml.safe_load(app.read_text())
                assert sample["runtime_options"] == {"max_steps": 6, "todo_mode": "off", "smart_summary": False}
                offered = {tool["function"]["name"] for tool in requests[0].get("tools", [])}
                assert {"read_file", "write_file", "shell_tool", "grep_search", "glob_search"} <= offered, offered
                assert "todo_write" not in offered, offered
            events = [json.loads(line) for line in (result.run.run_dir / "audit/runtime_events.jsonl").read_text().splitlines()]
            records = [event["details"]["record"] for event in events if event["kind"] == "tool"
                       and "record" in event["details"]]
            if profile == "smol":
                # smol emits compact runtime events. Match those to actual
                # tool replies delivered to the next HTTP model request.
                statuses = {event["details"]["call_id"]: event["details"]["status"]
                            for event in events if event["kind"] == "tool"}
                named = {call["id"]: call["function"]["name"] for message in requests[-1]["messages"]
                         for call in message.get("tool_calls", [])}
                records = [{"call_id": message["tool_call_id"], "tool_name": named[message["tool_call_id"]],
                            "status": statuses[message["tool_call_id"]], "output": message["content"]}
                           for message in requests[-1]["messages"] if message["role"] == "tool"]
            completed = {record["tool_name"] for record in records if record["status"] == "completed"}
            assert {name for name, _ in calls} <= completed, records
            if marker:
                assert any(marker in str(record["output"]) for record in records), records
            if case == "memory":
                assert all(json.loads(record["output"])["ok"] for record in records if record["tool_name"] in {"memory", "session_search"})
            if case == "goal":
                assert result.goal is not None
                assert result.goal["status"] == "complete"
            if case == "mcp":
                mcp_events = [json.loads(line) for line in (workspace / "mcp-events.jsonl").read_text().splitlines()]
                assert any(row.get("query") == "installed-mcp" for row in mcp_events)
                assert all(not psutil.pid_exists(row["pid"]) for row in mcp_events)
            if profile == "pi":
                selected = {name for name, _ in calls}
                if case == "mcp":
                    assert all(tool["function"]["name"].startswith("mcp__") for tool in requests[0].get("tools", []))
                else:
                    assert {tool["function"]["name"] for tool in requests[0].get("tools", [])} == selected
            evidence.update(run_id=result.run.run_id, manifest=str(result.run.manifest_path),
                            completed_tools=sorted(completed), model_requests=len(requests))
    if profile == "pi":
        forbidden = [name for name in sys.modules if name == "smolagents" or name.startswith((
            "smolagents.", "agentloom.runtimes.smolagents.tools", "agentloom.runtimes.smolagents.runtime",
            "agentloom.runtimes.smolagents.agents", "agentloom.runtimes.smolagents.monkey_patch"))]
        assert not forbidden, forbidden
        if case in {"no_tools", "read"}:
            assert "agentloom.tools.file_ops.file_outliner" not in sys.modules
            assert "agentloom.tools.search.lsp_tool" not in sys.modules
    evidence.update(case=case, status="passed")
    (workspace / "report.json").write_text(json.dumps(evidence, indent=2) + "\n")
    return evidence


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--profile", choices=["pi", "smol"], required=True)
    parser.add_argument("--case", required=True)
    parser.add_argument("--workspace", type=Path, required=True)
    parser.add_argument("--code-tools", action="store_true")
    args = parser.parse_args()
    result = run(args.profile, args.case, args.workspace.resolve(), args.code_tools)
    print(json.dumps({key: value for key, value in result.items() if key != "installed_distributions"}))
