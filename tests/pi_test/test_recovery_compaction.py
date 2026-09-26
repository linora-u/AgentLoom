"""Interrupted native compaction and resumed process cleanup via public entry points."""
from __future__ import annotations

import json
import os
import signal
from threading import Event
from uuid import uuid4

import psutil
import pytest
from agentloom.app.run import ApplicationRunError
from agentloom.app.runner import execute_app
from agentloom.config.config import bind_config, load_project_config
from agentloom.runtimes.pi.metadata import BRIDGE_VERSION
from agentloom.runtimes.pi.protocol import (
    PI_BRIDGE_PROTOCOL_VERSION,
    Handshake,
    ModelPermit,
    ModelPrepare,
    ModelSelection,
    ModelTrace,
    ModelTraceResult,
    Run,
    RunResult,
)
from agentloom.runtimes.pi.transport import PiTransport

from tests.pi_test.test_application import change_model, model_service, project
from tests.pi_test.test_process_lifecycle import assert_gone, start_cli
from tests.pi_test.test_recovery_application import audit, checkpoints, enable


def _is_compaction(request):
    return any(message["role"] == "system" and "context summarization assistant" in str(message["content"])
               for message in request["messages"])


def _sdk_pid(parent_pid):
    # Observe the real managed process, independent of PATH selection or a
    # launcher shim. The SDK has already reached HTTP when this is called.
    processes = [process for process in psutil.Process(parent_pid).children(recursive=True)
                 if any(argument.endswith("/pi/bridge/dist/index.js") for argument in process.cmdline())]
    assert len(processes) == 1, "Expected one managed Pi SDK process"
    return processes[0].pid


def _terminal_states(events):
    return [event["details"]["state"] for event in events if event["kind"] == "terminal"]


def test_continued_empty_task_does_not_recompact_without_new_context(tmp_path):
    overflow = "Requested token count exceeds the model's maximum context length of 64 tokens"
    with model_service(
        fail_requests={2: 400, 4: 400, 5: 400},
        fail_messages={2: overflow, 4: overflow, 5: overflow},
    ) as (url, requests):
        transport = PiTransport(uuid4().hex)
        try:
            handshake = transport.request(Handshake(
                method="handshake",
                protocol_version=PI_BRIDGE_PROTOCOL_VERSION,
                bridge_version=BRIDGE_VERSION,
                native_tool_contract=1,
            ), timeout=15)
            assert handshake.payload is not None

            model = ModelSelection(
                model_type="test",
                model_id="openai/fixture-model",
                protocol="openai_chat",
                settings={
                    "api_key": "fixture-secret",
                    "base_url": url,
                    "temperature": 0,
                    "context_window": 64,
                    "max_output_tokens": 16,
                    "timeout": 10,
                    "num_retries": 0,
                    "retry_delay": 0,
                    "max_retry_delay": 0,
                    "requests_per_minute": 2_000_000,
                    "context_cache": False,
                    "extra_completion_params": {},
                },
                request_headers={},
            )
            runtime_options = {
                "max_stop_attempts": 3,
                "compaction": {
                    "enabled": True,
                    "reserveTokens": 48,
                    "keepRecentTokens": 1,
                },
            }
            events = []

            def permit(payload):
                if isinstance(payload, ModelTrace):
                    return ModelTraceResult(
                        method="model_trace", identity=payload.identity,
                        attempt=payload.attempt, phase=payload.phase,
                        accepted=True,
                    )
                assert isinstance(payload, ModelPrepare)
                return ModelPermit(
                    method="model_prepare",
                    identity=payload.identity,
                    state="work",
                )

            def run(task, *, continue_session):
                run_id = uuid4().hex
                response = transport.request(Run(
                    method="run",
                    application_id="pi",
                    task_id=run_id,
                    task=task,
                    cwd=str(tmp_path),
                    instructions="Answer directly.",
                    model=model,
                    tools=[],
                    runtime_options=runtime_options,
                    continue_session=continue_session,
                ), run_id=run_id, observe=events.append, callback=permit)
                assert isinstance(response.payload, RunResult)
                return response.payload

            assert run("Seed the reusable session.", continue_session=False).state == "success"
            exhausted = run(None, continue_session=True)
            result = run(None, continue_session=True)
        finally:
            transport.close()

    assert exhausted.state == "failed"
    # Pi 0.87.1 cannot compact a second time when the previous compaction has
    # left no new model-visible user context to summarize. It fails the turn.
    assert result.state == "failed"
    assert len(requests) == 5
    assert sum(message["role"] == "user" for message in requests[1][1]["messages"]) == 1
    assert "agentloom_instruction_only_turn" not in json.dumps(requests)
    assert _is_compaction(requests[2][1])
    checkpoint_phases = [event.payload.get("phase") for event in events
                         if event.event == "checkpoint"]
    assert checkpoint_phases.count("compaction_started") == 1
    assert checkpoint_phases[-1] == "compaction_ended"


def test_compaction_sigint_resumes_assistant_tail_with_native_compaction_enabled(tmp_path):
    compaction_started = Event()
    release = Event()
    sdk_pids = set()

    def pause_first_compaction(_number, request):
        sdk_pids.add(_sdk_pid(os.getpid()))
        if _is_compaction(request) and not compaction_started.is_set():
            compaction_started.set()
            release.wait(timeout=30)

    with model_service(on_request=pause_first_compaction) as (url, requests):
        app = project(tmp_path, url)
        # The fixture reports 15 tokens. SDK threshold is contextWindow -
        # reserveTokens = 8, so its own post-turn compaction must start.
        change_model(tmp_path, context_window=64, max_output_tokens=16)
        enable(app, runtime_options={"compaction": {
            "enabled": True, "reserveTokens": 56, "keepRecentTokens": 1,
        }})
        child = start_cli(tmp_path, app)
        try:
            assert compaction_started.wait(timeout=20), "SDK did not request native compaction"
            first_sdk_pid = _sdk_pid(child.pid)
            os.kill(child.pid, signal.SIGINT)
            stdout, stderr = child.communicate(timeout=10)
        finally:
            release.set()
            if child.poll() is None:
                child.kill()
                child.wait()
        assert child.returncode != 0, stdout + stderr
        assert_gone(first_sdk_pid)
        cli_records = [json.loads(line) for line in stdout.splitlines()]
        assert [record["event"] for record in cli_records if record["event"] in {
            "run.completed", "run.failed", "run.interrupted"}] == ["run.interrupted"]
        first_events = [json.loads(line) for line in next(tmp_path.rglob("runtime_events.jsonl")).read_text().splitlines()]
        assert _terminal_states(first_events) == ["interrupted"]
        assert any(event["kind"] == "checkpoint" and event["details"].get("phase") == "compaction_started"
                   for event in first_events)

        saved = checkpoints(tmp_path)
        assert len(saved) == 1
        envelope = saved[0][1]["runtime_checkpoint"]
        bundle = json.loads(next(tmp_path.rglob(envelope["payload"]["artifact"] + ".json")).read_text())
        assert bundle["phase"] == "running"
        messages = [entry["message"] for entry in bundle["session"]["entries"] if entry["type"] == "message"]
        assert messages[-1]["role"] == "assistant"
        assert not any(entry["type"] == "compaction" for entry in bundle["session"]["entries"])

        resumed_from = len(requests)
        with bind_config(load_project_config(tmp_path)):
            result = execute_app(app, resume_task_id=envelope["task_id"], file_logging=False)
        assert result.output == "Pi answer"
        assert result.run.task_id == envelope["task_id"]
        assert result.run.run_id != envelope["run_id"]
        assert len(sdk_pids) == 2
        for sdk_pid in sdk_pids:
            assert_gone(sdk_pid)
        resumed_requests = [request for _, request, _ in requests[resumed_from:]]
        ordinary = next(request for request in resumed_requests if not _is_compaction(request))
        assert any("Resume the interrupted task" in str(message["content"]) for message in ordinary["messages"])
        assert any(message["role"] == "assistant" and "Pi answer" in str(message["content"])
                   for message in ordinary["messages"])
        assert any(_is_compaction(request) for request in resumed_requests)
        events = audit(result.run)
        assert _terminal_states(events) == ["success"]
        assert any(event["kind"] == "checkpoint" and event["details"].get("phase") == "compaction_ended"
                   and event["details"].get("aborted") is False for event in events)
        final = checkpoints(tmp_path)[0][1]["runtime_checkpoint"]
        final_bundle = json.loads(next(tmp_path.rglob(final["payload"]["artifact"] + ".json")).read_text())
        assert final_bundle["session"]["header"]["id"] == bundle["session"]["header"]["id"]
        assert any(entry["type"] == "compaction" for entry in final_bundle["session"]["entries"])


@pytest.mark.parametrize("interrupt", ["keyboard", "child_exit"])
def test_restored_model_wait_interruption_terminates_once_and_reaps_sdk(tmp_path, interrupt):
    resumed_model_started = Event()
    release = Event()

    def pause_resumed_model(number, _request):
        if number == 2:
            resumed_model_started.set()
            release.wait(timeout=30)

    with model_service(fail_requests={1: 500}, on_request=pause_resumed_model) as (url, requests):
        app = project(tmp_path, url)
        enable(app)
        with bind_config(load_project_config(tmp_path)):
            with pytest.raises(ApplicationRunError) as first:
                execute_app(app, file_logging=False)
        child = start_cli(tmp_path, app, resume=first.value.run.task_id)
        try:
            assert resumed_model_started.wait(timeout=20), "Restored SDK did not request its model"
            sdk_pid = _sdk_pid(child.pid)
            os.kill(child.pid if interrupt == "keyboard" else sdk_pid,
                    signal.SIGINT if interrupt == "keyboard" else signal.SIGKILL)
            stdout, stderr = child.communicate(timeout=10)
        finally:
            release.set()
            if child.poll() is None:
                child.kill()
                child.wait()
        assert child.returncode != 0, stdout + stderr
        assert_gone(sdk_pid)
        expected = "interrupted" if interrupt == "keyboard" else "failed"
        records = [json.loads(line) for line in stdout.splitlines()]
        assert [record["event"] for record in records if record["event"] in {
            "run.completed", "run.failed", "run.interrupted"}] == [f"run.{expected}"]
        other_audits = [path for path in tmp_path.rglob("runtime_events.jsonl")
                       if path != first.value.run.run_dir / "audit/runtime_events.jsonl"]
        assert len(other_audits) == 1
        events = [json.loads(line) for line in other_audits[0].read_text().splitlines()]
        assert _terminal_states(events) == [expected]
        assert any(event["kind"] == "run" and event["details"].get("resumed") for event in events)
        assert len(requests) == 2
