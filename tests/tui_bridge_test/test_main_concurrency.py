from __future__ import annotations

import io
import json
import sys
import threading
import time

from src.tui_bridge import __main__ as bridge_main


def _run_main(monkeypatch, bridge, *requests: dict) -> list[dict]:
    stdin = io.StringIO("".join(json.dumps(request) + "\n" for request in requests))
    stdout = io.StringIO()
    monkeypatch.setattr(bridge_main, "TuiBridge", lambda _project_root: bridge)
    monkeypatch.setattr(sys, "stdin", stdin)
    monkeypatch.setattr(sys, "stdout", stdout)

    bridge_main.main()

    return [json.loads(line) for line in stdout.getvalue().splitlines()]


def test_response_writer_replaces_an_oversized_ndjson_line_with_a_bounded_error(monkeypatch) -> None:
    stream = io.StringIO()
    monkeypatch.setattr(bridge_main, "_MAX_RESPONSE_BYTES", 128)

    bridge_main._ResponseWriter(stream).write({"id": "large", "ok": True, "result": {"value": "x" * 1024}})

    response = json.loads(stream.getvalue())
    assert response == {
        "id": "large",
        "ok": False,
        "error": {
            "code": "internal_error",
            "message": "bridge response exceeded the safe size limit",
        },
    }


def test_oversized_response_drops_an_unbounded_id_to_preserve_the_hard_cap(monkeypatch) -> None:
    stream = io.StringIO()
    monkeypatch.setattr(bridge_main, "_MAX_RESPONSE_BYTES", 128)

    bridge_main._ResponseWriter(stream).write({"id": "request-" * 128, "ok": True, "result": {"value": "x" * 1024}})

    encoded = stream.getvalue().encode("utf-8")
    response = json.loads(stream.getvalue())
    assert len(encoded) <= 128
    assert response["id"] is None
    assert response["error"]["code"] == "internal_error"


def test_oversized_utf8_request_is_rejected_and_the_next_request_is_processed(monkeypatch) -> None:
    class BootstrapBridge:
        def dispatch(self, method: str, params: dict) -> dict:
            assert method == "bootstrap"
            assert params == {}
            return {"ready": True}

    oversized = json.dumps(
        {"id": "large", "method": "bootstrap", "params": {"value": "界" * 64}},
        ensure_ascii=False,
    )
    valid = json.dumps({"id": "next", "method": "bootstrap", "params": {}})
    stdin = io.BytesIO(f"{oversized}\n{valid}\n".encode())
    stdout = io.StringIO()
    monkeypatch.setattr(bridge_main, "_MAX_REQUEST_BYTES", 96)
    monkeypatch.setattr(bridge_main, "TuiBridge", lambda _project_root: BootstrapBridge())
    monkeypatch.setattr(sys, "stdin", stdin)
    monkeypatch.setattr(sys, "stdout", stdout)

    bridge_main.main()

    responses = [json.loads(line) for line in stdout.getvalue().splitlines()]
    assert responses == [
        {
            "id": None,
            "ok": False,
            "error": {
                "code": "invalid_request",
                "message": "request exceeded the safe size limit",
            },
        },
        {"id": "next", "ok": True, "result": {"ready": True}},
    ]


def test_observation_request_completes_while_builder_send_is_blocked(monkeypatch) -> None:
    class BlockingBuilderBridge:
        def __init__(self) -> None:
            self.builder_started = threading.Event()
            self.release_builder = threading.Event()
            self.observation_finished = threading.Event()
            self.observed_while_builder_active = False

        def dispatch(self, method: str, _params: dict) -> dict:
            if method == "builder.send":
                self.builder_started.set()
                self.release_builder.wait()
                return {
                    "kind": "builder",
                    "observation_seen": self.observation_finished.is_set(),
                }
            if method == "bootstrap":
                self.observed_while_builder_active = not self.release_builder.is_set()
                self.observation_finished.set()
                self.release_builder.set()
                return {"kind": "observation"}
            raise AssertionError(f"unexpected method: {method}")

    bridge = BlockingBuilderBridge()
    stdin = io.StringIO(
        "".join(
            json.dumps(request) + "\n"
            for request in (
                {
                    "id": "slow-builder",
                    "method": "builder.send",
                    "params": {"session_id": "session-1", "message": "create"},
                },
                {"id": "live-snapshot", "method": "bootstrap", "params": {}},
            )
        )
    )
    stdout = io.StringIO()
    monkeypatch.setattr(bridge_main, "TuiBridge", lambda _project_root: bridge)
    monkeypatch.setattr(sys, "stdin", stdin)
    monkeypatch.setattr(sys, "stdout", stdout)
    failures: list[BaseException] = []

    def run_bridge() -> None:
        try:
            bridge_main.main()
        except BaseException as error:
            failures.append(error)

    main_thread = threading.Thread(target=run_bridge)
    main_thread.start()
    assert bridge.builder_started.wait(timeout=1)
    observation_finished = bridge.observation_finished.wait(timeout=1)
    if not observation_finished:
        bridge.release_builder.set()
    main_thread.join(timeout=1)

    assert main_thread.is_alive() is False
    assert failures == []
    assert observation_finished is True
    assert bridge.observed_while_builder_active is True
    responses_by_id = {
        response["id"]: response for response in (json.loads(line) for line in stdout.getvalue().splitlines())
    }
    assert responses_by_id == {
        "live-snapshot": {
            "id": "live-snapshot",
            "ok": True,
            "result": {"kind": "observation"},
        },
        "slow-builder": {
            "id": "slow-builder",
            "ok": True,
            "result": {"kind": "builder", "observation_seen": True},
        },
    }


def test_builder_session_operations_are_dispatched_serially(monkeypatch) -> None:
    class SerialBuilderBridge:
        def __init__(self) -> None:
            self.active = False
            self.overlapped = False
            self.trace: list[tuple[str, str]] = []
            self.lock = threading.Lock()

        def dispatch(self, method: str, params: dict) -> dict:
            with self.lock:
                if self.active:
                    self.overlapped = True
                self.active = True
                self.trace.append(("start", method))
            time.sleep(0.02)
            with self.lock:
                self.trace.append(("finish", method))
                self.active = False
            return {"method": method, "session_id": params["session_id"]}

    bridge = SerialBuilderBridge()
    responses = _run_main(
        monkeypatch,
        bridge,
        {
            "id": "send",
            "method": "builder.send",
            "params": {"session_id": "session-1", "message": "create"},
        },
        {
            "id": "draft",
            "method": "builder.draft",
            "params": {"session_id": "session-1"},
        },
        {
            "id": "apply",
            "method": "draft.apply",
            "params": {"session_id": "session-1", "expected_revision": 1},
        },
    )

    assert bridge.overlapped is False
    assert bridge.trace == [
        ("start", "builder.send"),
        ("finish", "builder.send"),
        ("start", "builder.draft"),
        ("finish", "builder.draft"),
        ("start", "draft.apply"),
        ("finish", "draft.apply"),
    ]
    assert [response["id"] for response in responses] == ["send", "draft", "apply"]
    assert all(response["ok"] is True for response in responses)


def test_full_builder_lane_rejects_work_without_blocking_observation(monkeypatch) -> None:
    class SaturatedBuilderBridge:
        def __init__(self) -> None:
            self.release_builder = threading.Event()

        def dispatch(self, method: str, params: dict) -> dict:
            if method == "builder.send" and params["session_id"] == "slow":
                self.release_builder.wait(timeout=1)
                return {"kind": "slow-builder"}
            if method == "builder.send":
                return {"kind": "queued-builder"}
            if method == "bootstrap":
                self.release_builder.set()
                return {"kind": "observation"}
            raise AssertionError(f"unexpected method: {method}")

    builder_requests = [
        {
            "id": "slow",
            "method": "builder.send",
            "params": {"session_id": "slow", "message": "create"},
        },
        *[
            {
                "id": f"queued-{index}",
                "method": "builder.send",
                "params": {"session_id": f"queued-{index}", "message": "create"},
            }
            for index in range(15)
        ],
        {
            "id": "overflow",
            "method": "builder.send",
            "params": {"session_id": "overflow", "message": "create"},
        },
    ]
    responses = _run_main(
        monkeypatch,
        SaturatedBuilderBridge(),
        *builder_requests,
        {"id": "live", "method": "bootstrap", "params": {}},
    )
    responses_by_id = {response["id"]: response for response in responses}

    assert responses_by_id["live"] == {
        "id": "live",
        "ok": True,
        "result": {"kind": "observation"},
    }
    assert responses_by_id["overflow"]["ok"] is False
    assert responses_by_id["overflow"]["error"]["code"] == "busy"


def test_concurrent_responses_are_complete_ndjson_lines_and_eof_waits(monkeypatch) -> None:
    class DetectingStream(io.StringIO):
        def __init__(self) -> None:
            super().__init__()
            self._active_writers = 0
            self._guard = threading.Lock()
            self.concurrent_write = False

        def write(self, value: str) -> int:
            with self._guard:
                self._active_writers += 1
                if self._active_writers > 1:
                    self.concurrent_write = True
            try:
                time.sleep(0.002)
                return super().write(value)
            finally:
                with self._guard:
                    self._active_writers -= 1

    class NoisyObservationBridge:
        def __init__(self) -> None:
            self.first_pair = threading.Barrier(2)

        def dispatch(self, method: str, params: dict) -> dict:
            print(f"incidental output for {params['sequence']}")
            if params["sequence"] < 2:
                self.first_pair.wait(timeout=1)
            time.sleep((params["sequence"] % 2) * 0.002)
            return {"method": method, "sequence": params["sequence"]}

    requests = [
        {
            "id": f"request-{sequence}",
            "method": "builder.send" if sequence == 0 else "bootstrap",
            "params": {"sequence": sequence},
        }
        for sequence in range(20)
    ]
    stdin = io.StringIO("".join(json.dumps(request) + "\n" for request in requests))
    stdout = DetectingStream()
    monkeypatch.setattr(bridge_main, "TuiBridge", lambda _project_root: NoisyObservationBridge())
    monkeypatch.setattr(sys, "stdin", stdin)
    monkeypatch.setattr(sys, "stdout", stdout)

    bridge_main.main()

    lines = stdout.getvalue().splitlines()
    responses = [json.loads(line) for line in lines]
    assert stdout.concurrent_write is False
    assert len(responses) == len(requests)
    assert {response["id"] for response in responses} == {request["id"] for request in requests}
    assert all(response["ok"] is True for response in responses)
    assert "incidental output" not in stdout.getvalue()
