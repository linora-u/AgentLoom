"""Published Pi SDK usage reaches public Worker receipts without double counting."""
from __future__ import annotations

import json
import os
import sys
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread

import pytest
import yaml
from agentloom.app import Application
from agentloom.config.config import bind_config, load_project_config
from agentloom.execution.concurrency import WorkItem
from agentloom.execution.observability import inspect_run

from tests.pi_test.test_application import project


def write_yaml(path, values):
    path.write_text(yaml.safe_dump(values))


def usage_probe(value: str) -> str:
    """Return the actual model tool argument.

    Args:
        value: Value to return to the next real Pi model turn.
    """
    return value


def provider_usage(reasoning=17, *, output=40):
    value = {"input_tokens": 100, "output_tokens": output, "total_tokens": 100 + output,
             "input_tokens_details": {"cached_tokens": 20, "cache_write_tokens": 5}}
    if reasoning is not None:
        value["output_tokens_details"] = {"reasoning_tokens": reasoning}
    return value


@contextmanager
def usage_service(turns):
    requests = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def do_POST(self):
            request = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            turn = turns[min(len(requests), len(turns) - 1)]
            row = {"request": request, "provider_usage": turn.get("usage"), "terminal": turn.get("terminal", "completed")}
            requests.append(row)
            if turn.get("http_error"):
                body = b'{"error":{"code":"ModelAccountTpmRateLimitExceeded","message":"controlled TPM failure"}}'
                self.send_response(429)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            if turn.get("tool"):
                item = {"type": "function_call", "id": "fc_usage", "call_id": "call_usage", "status": "completed",
                        "name": "usage_probe", "arguments": '{"value":"actual tool result"}'}
                item_events = [
                    {"type": "response.output_item.added", "output_index": 0, "item": {**item, "arguments": ""}},
                    {"type": "response.function_call_arguments.delta", "item_id": "fc_usage", "output_index": 0,
                     "delta": item["arguments"]},
                    {"type": "response.output_item.done", "output_index": 0, "item": item},
                ]
            else:
                item = {"type": "message", "id": "msg_usage", "role": "assistant", "status": "completed",
                        "content": [{"type": "output_text", "text": "Known usage answer", "annotations": []}]}
                item_events = [
                    {"type": "response.output_item.added", "output_index": 0, "item": {**item, "content": []}},
                    {"type": "response.content_part.added", "item_id": "msg_usage", "output_index": 0,
                     "content_index": 0, "part": {"type": "output_text", "text": "", "annotations": []}},
                    {"type": "response.output_text.delta", "item_id": "msg_usage", "output_index": 0,
                     "content_index": 0, "delta": "Known usage answer"},
                    {"type": "response.output_item.done", "output_index": 0, "item": item},
                ]
            terminal = turn.get("terminal", "completed")
            response = {"id": "resp_usage", "model": request["model"], "status": terminal, "output": [item]}
            if terminal == "incomplete":
                response["incomplete_details"] = {"reason": "max_output_tokens"}
            if turn.get("usage") is not None:
                response["usage"] = turn["usage"]
            events = [{"type": "response.created", "response": {"id": "resp_usage", "model": request["model"],
                                                                  "status": "in_progress", "output": []}},
                      *item_events, {"type": "response." + terminal, "response": response}]
            body = b"".join(("event: " + e["type"] + "\ndata: " + json.dumps(e) + "\n\n").encode() for e in events)
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}/v1", requests
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def usage_project(root, url, *, tool=False, continuation=False):
    root_worker = project(root, url)
    application = root_worker.parent.parent
    worker = application / "workflows/worker.yaml"
    worker.write_text(root_worker.read_text())
    config = yaml.safe_load((root / "config/model.yaml").read_text())
    for profile in ("test", "summary"):
        config["model"][profile].update(adapter="openai_responses", max_output_tokens=32768, context_window=65536)
    write_yaml(root / "config/model.yaml", config)
    worker = application / "workflows/worker.yaml"
    definition = yaml.safe_load(worker.read_text())
    definition["input_schema"] = {"type": "object", "properties": {"value": {"type": "integer"}},
                                  "additionalProperties": False, "required": ["value"]}
    definition["runtime_options"].update(transport="sse", cacheWarming="off")
    if tool:
        definition["tools"] = [{"name": "usage_probe", "module": __name__, "function": "usage_probe"}]
    if continuation:
        script = root / "stop_once.py"
        marker = root / "stop_count"
        script.write_text("import json,sys\nfrom pathlib import Path\njson.load(sys.stdin)\n"
                          f"p=Path({str(marker)!r})\nn=int(p.read_text()) if p.exists() else 0\np.write_text(str(n+1))\n"
                          "print(json.dumps({'decision':'block' if n==0 else 'allow','reason':'Continue once.'}))\n")
        definition["hooks"] = {"Stop": [{"id": "usage-continuation", "command": f"{sys.executable} {script}"}]}
    write_yaml(worker, definition)
    return application


def run_public(root, application, requests, entry):
    with bind_config(load_project_config(root)), Application.open(
        application, project_root=root, file_logging=False
    ) as app:
        if entry == "single":
            outcome = app.worker("workflows/worker.yaml").invoke(WorkItem("usage", {"value": 1})).to_dict()
        else:
            batch = app.worker("workflows/worker.yaml").map([WorkItem("usage", {"value": 1})])
            outcome = batch.outcomes[0].to_dict()
        with inspect_run(app.run) as trace:
            events = [e for e in trace.events() if e.get("agent_id") == outcome["local_run_id"]]
            responses = [e for e in events if e["kind"] == "model_response"]
            sdk_responses = [json.loads(trace.read_text(e["response_ref"])) for e in responses]
        run = app.run
    audit = [json.loads(line) for line in (run.run_dir / "audit/runtime_events.jsonl").read_text().splitlines()]
    usage_events = [e["details"] for e in audit if e["kind"] == "usage"]
    receipt = {"outcome": outcome, "requests": requests,
               "events": events, "sdk_responses": sdk_responses, "runtime_usage_events": usage_events}
    (root / "public-usage-receipts.json").write_text(json.dumps(receipt, ensure_ascii=False, indent=2))
    return receipt


@pytest.mark.parametrize("entry", ["finite", "single"])
@pytest.mark.parametrize("scenario", ["success", "length", "tool-turns", "stop-continuation", "tool-length-tail", "stop-length-tail"])
def test_known_sdk_reasoning_reaches_public_usage_without_double_counting(tmp_path, entry, scenario):
    if scenario == "length":
        turns = [{"usage": provider_usage(32768, output=32768), "terminal": "incomplete"}]
        expected_reasoning, expected_output = 32768, 32768
    elif scenario in {"tool-length-tail", "stop-length-tail"}:
        turns = [{"usage": provider_usage(17), "tool": scenario == "tool-length-tail"},
                 {"usage": provider_usage(32768, output=32768), "terminal": "incomplete"}]
        expected_reasoning, expected_output = 32785, 32808
    elif scenario in {"tool-turns", "stop-continuation"}:
        turns = [{"usage": provider_usage(17), "tool": scenario == "tool-turns"},
                 {"usage": provider_usage(23)}]
        expected_reasoning, expected_output = 40, 80
    else:
        turns = [{"usage": provider_usage(17)}]
        expected_reasoning, expected_output = 17, 40
    with usage_service(turns) as (url, requests):
        application = usage_project(tmp_path, url, tool=scenario in {"tool-turns", "tool-length-tail"},
                                    continuation=scenario in {"stop-continuation", "stop-length-tail"})
        receipt = run_public(tmp_path, application, requests, entry)
    outcome = receipt["outcome"]
    expected_turn_reasoning = [32768] if scenario == "length" else [17, 32768] if scenario.endswith("length-tail") else [17, 23] if len(turns) == 2 else [17]
    assert [r["usage"]["reasoning"] for r in receipt["sdk_responses"]] == expected_turn_reasoning
    assert outcome["usage"]["reasoning_tokens"] == expected_reasoning
    assert outcome["usage"]["input_tokens"] == 100 * len(turns)
    assert outcome["usage"]["cached_input_tokens"] == 20 * len(turns)
    assert outcome["usage"]["output_tokens"] == expected_output
    assert outcome["usage"]["total_tokens"] == 100 * len(turns) + expected_output
    assert receipt["runtime_usage_events"][-1]["reasoning_tokens"] == expected_reasoning
    assert receipt["runtime_usage_events"][-1]["total_tokens"] == 100 * len(turns) + expected_output
    model_events = [e for e in receipt["events"] if e["kind"] == "model_response"]
    assert [e["usage"]["reasoning_tokens"] for e in model_events] == expected_turn_reasoning
    if scenario == "length" or scenario.endswith("length-tail"):
        assert outcome["status"] == "failed" and outcome["error"]["category"] == "output_validation"
        assert outcome["error"]["retryable"] is True
        assert receipt["sdk_responses"][-1]["provider_finish_reason"] == "max_output_tokens"
    else:
        assert outcome["status"] == "success"
    assert all(r["request"]["max_output_tokens"] == 32768 for r in requests)


@pytest.mark.parametrize("entry", ["finite", "single"])
@pytest.mark.parametrize("case", ["sdk-not-reported", "reported-zero", "provider-not-reported", "mixed-not-reported", "provider-tail-error", "stop-provider-tail-error"])
def test_reasoning_reporting_boundaries_preserve_only_available_sdk_facts(tmp_path, entry, case):
    if case == "sdk-not-reported":
        turns = [{"usage": None}]
        expected_reasoning, expected_input, expected_output, expected_total = 0, 0, 0, 0
        sdk_reasoning = [None]
    elif case in {"reported-zero", "provider-not-reported"}:
        turns = [{"usage": provider_usage(0 if case == "reported-zero" else None)}]
        expected_reasoning, expected_input, expected_output, expected_total = 0, 100, 40, 140
        # SDK1.0.0 Responses applies ||0, so provider omission is irreversibly
        # normalized to the same numeric0; do not claim upstream certainty.
        sdk_reasoning = [0]
    else:
        turns = [{"usage": provider_usage(17), "tool": case != "stop-provider-tail-error"},
                 {"http_error": True} if case.endswith("provider-tail-error") else {"usage": None}]
        expected_reasoning, expected_input, expected_output, expected_total = 17, 100, 40, 140
        sdk_reasoning = [17, None]
    with usage_service(turns) as (url, requests):
        application = usage_project(tmp_path, url, tool=len(turns) == 2 and case != "stop-provider-tail-error",
                                    continuation=case == "stop-provider-tail-error")
        receipt = run_public(tmp_path, application, requests, entry)
    actual = receipt["outcome"]["usage"]
    assert actual["reasoning_tokens"] == expected_reasoning
    assert actual["input_tokens"] == expected_input and actual["output_tokens"] == expected_output
    assert actual["total_tokens"] == expected_total
    assert [r["usage"].get("reasoning") for r in receipt["sdk_responses"]] == sdk_reasoning
    events = [e for e in receipt["events"] if e["kind"] == "model_response"]
    for expected, event in zip(sdk_reasoning, events, strict=True):
        if expected is None:
            assert "reasoning_tokens" not in event["usage"]
        else:
            assert event["usage"]["reasoning_tokens"] == expected
    if case == "provider-not-reported":
        assert "output_tokens_details" not in requests[0]["provider_usage"]
    assert receipt["runtime_usage_events"][-1]["reasoning_tokens"] == expected_reasoning
    assert receipt["runtime_usage_events"][-1]["total_tokens"] == expected_total
    if case.endswith("provider-tail-error"):
        assert receipt["outcome"]["status"] == "failed"
        # This usage test does not install the separate TPM classification fix.
        # An otherwise unknown HTTP 429 retains this checkout's failure policy.
        assert receipt["outcome"]["error"]["retryable"] is False
    else:
        assert receipt["outcome"]["status"] == "success"


def test_repeated_bridge_usage_snapshots_are_not_added_as_increments(tmp_path, monkeypatch):
    bootstrap = tmp_path / "repeat-usage.mjs"
    bootstrap.write_text("""
const nativeWrite = process.stdout.write.bind(process.stdout);
const offsets = new Map();
process.stdout.write = (chunk, ...args) => {
  const text = String(chunk);
  let frame;
  try {frame = JSON.parse(text);} catch {return nativeWrite(chunk, ...args);}
  if (frame.kind !== 'event') return nativeWrite(chunk, ...args);
  const offset = offsets.get(frame.request_id) || 0;
  frame.sequence += offset;
  nativeWrite(JSON.stringify(frame) + '\\n');
  if (frame.event === 'usage') {
    offsets.set(frame.request_id, offset + 1);
    nativeWrite(JSON.stringify({...frame, sequence: frame.sequence + 1}) + '\\n');
  }
  return true;
};
""")
    from tests.pi_test.test_process_lifecycle import sdk_node

    binary = sdk_node()
    launcher = tmp_path / "bin/node"
    launcher.parent.mkdir()
    launcher.write_text(f"#!{sys.executable}\nimport os,sys\n"
                        f"binary={binary!r}; bootstrap={str(bootstrap)!r}\nargs=sys.argv[1:]\n"
                        "if any(arg.endswith('/pi/bridge/dist/index.js') for arg in args):\n"
                        " args=['--import',bootstrap,*args]\n"
                        "os.execv(binary,[binary,*args])\n")
    launcher.chmod(0o755)
    monkeypatch.setenv("PATH", str(launcher.parent) + os.pathsep + os.environ["PATH"])
    with usage_service([{"usage": provider_usage(17)}, {"usage": provider_usage(23)}]) as (url, requests):
        application = usage_project(tmp_path, url, continuation=True)
        receipt = run_public(tmp_path, application, requests, "single")
    assert receipt["outcome"]["usage"]["reasoning_tokens"] == 40
    assert receipt["outcome"]["usage"]["total_tokens"] == 280
    assert [event["reasoning_tokens"] for event in receipt["runtime_usage_events"]] == [17, 17, 40, 40]
    assert [event["total_tokens"] for event in receipt["runtime_usage_events"]] == [140, 140, 280, 280]


@pytest.mark.parametrize("control", ["interrupt", "exit"])
def test_control_after_reported_second_run_snapshot_keeps_cumulative_usage(tmp_path, monkeypatch, control):
    from agentloom.app.lifecycle import ApplicationRunLifecycle

    signal = KeyboardInterrupt("reported usage observer interruption") if control == "interrupt" else SystemExit(73)
    original = ApplicationRunLifecycle.observe_runtime_event
    snapshots = []

    def interrupt_after_second_snapshot(lifecycle, event):
        original(lifecycle, event)
        if event.kind == "usage":
            snapshots.append(dict(event.details))
            if len(snapshots) == 2:
                raise signal

    monkeypatch.setattr(ApplicationRunLifecycle, "observe_runtime_event", interrupt_after_second_snapshot)
    with usage_service([{"usage": provider_usage(17)}, {"usage": provider_usage(23)}]) as (url, requests):
        application = usage_project(tmp_path, url, continuation=True)
        with bind_config(load_project_config(tmp_path)), Application.open(
            application, project_root=tmp_path, file_logging=False
        ) as app:
            with pytest.raises(BaseException) as caught:
                app.worker("workflows/worker.yaml").invoke(WorkItem("usage", {"value": 1}))
            outcome = caught.value.worker_outcome.to_dict()
            receipt = {"snapshots": snapshots, "outcome": outcome, "requests": requests,
                       "caught_type": type(caught.value).__name__, "original_control_preserved": caught.value is signal}
            (tmp_path / "public-control-usage-receipt.json").write_text(json.dumps(receipt, indent=2))
            assert caught.value is signal
            assert len(requests) == 2
            assert [snapshot["reasoning_tokens"] for snapshot in snapshots] == [17, 40]
            assert outcome["usage"]["reasoning_tokens"] == 40
            assert outcome["usage"]["input_tokens"] == 200
            assert outcome["usage"]["output_tokens"] == 80
            assert outcome["usage"]["total_tokens"] == 280
            assert outcome["cleanup_errors"] == []
