"""Wire validation only; production Pi registration belongs to ticket 07."""

import json

import pytest


def test_published_bridge_schema_matches_the_python_codec():
    from pathlib import Path

    from agentloom.runtimes.pi.protocol import protocol_schema

    root = Path(__file__).resolve().parents[3]
    published = json.loads((root / "src/runtimes/pi/bridge-v2.schema.json").read_text())
    assert published == protocol_schema()


def test_pi_handshake_roundtrip_and_rejects_unknown_protocol():
    from agentloom.runtimes.pi.protocol import decode_message, encode_message

    message = {
        "version": 2,
        "kind": "request",
        "request_id": "host:1",
        "instance_id": "worker-a",
        "payload": {
            "method": "handshake",
            "protocol_version": 2,
            "bridge_version": 1,
            "native_tool_contract": 1,
        },
    }
    parsed = decode_message(json.dumps(message))
    assert json.loads(encode_message(parsed))["payload"] == message["payload"]
    with pytest.raises(ValueError, match="Invalid Pi bridge message"):
        decode_message(json.dumps({**message, "version": 1}))

    response = {
        "version": 2,
        "kind": "response",
        "request_id": "host:1",
        "instance_id": "worker-a",
        "payload": {
            "method": "handshake",
            "runtime_id": "pi",
            "protocol_version": 2,
            "bridge_version": 1,
            "sdk_version": "0.79.4",
            "node_version": "v22.19.0",
            "native_tool_contract": 1,
            "capabilities": {
                "structured_tools": True,
                "parallel_tools": True,
                "checkpoint_resume": True,
                "subagents": True,
                "goal": True,
                "stop_hooks": True,
                "structured_output": True,
            },
        },
        "error": None,
    }
    roundtrip = json.loads(encode_message(decode_message(json.dumps(response))))
    assert roundtrip["payload"] == response["payload"]
    assert roundtrip["run_id"] is None


def test_snapshot_can_acknowledge_no_checkpoint():
    from agentloom.runtimes.pi.protocol import decode_message, encode_message

    message = {
        "version": 2,
        "kind": "response",
        "request_id": "host:2",
        "instance_id": "worker-a",
        "run_id": "run",
        "payload": {"method": "snapshot", "checkpoint": None},
    }
    assert json.loads(encode_message(decode_message(json.dumps(message))))["payload"] == message["payload"]


def test_run_error_preserves_output_validation_category():
    from agentloom.runtimes.pi.protocol import decode_message, encode_message

    message = {
        "version": 2,
        "kind": "response",
        "request_id": "host:output-validation",
        "instance_id": "worker-a",
        "run_id": "run",
        "payload": {
            "method": "run",
            "state": "max_steps_error",
            "terminal_rejections": 2,
            "output": None,
            "usage": {},
            "artifacts": [],
            "checkpoint": None,
            "error": {
                "category": "output_validation",
                "message": "Agent exhausted its output correction budget",
                "retryable": True,
            },
        },
    }

    roundtrip = json.loads(encode_message(decode_message(json.dumps(message))))
    assert roundtrip["payload"]["error"]["category"] == "output_validation"
    assert roundtrip["payload"]["terminal_rejections"] == 2


@pytest.mark.parametrize(
    "frame",
    [
        '{"version":2,"version":1,"kind":"request","request_id":"h:1","instance_id":"w","payload":{"method":"close"}}',
        '{"version":true,"kind":"request","request_id":"h:1","instance_id":"w","payload":{"method":"close"}}',
    ],
)
def test_ambiguous_or_coerced_protocol_version_is_rejected(frame):
    from agentloom.runtimes.pi.protocol import decode_message

    with pytest.raises(ValueError, match="Invalid Pi bridge message"):
        decode_message(frame)


def test_tool_settlement_cannot_cross_run_or_claim_uncertain_success():
    from agentloom.runtimes.pi.protocol import decode_message

    identity = dict(application_id="app", task_id="task", run_id="different", instance_id="worker-a", call_id="call")
    message = {
        "version": 2,
        "kind": "response",
        "request_id": "pi:3",
        "instance_id": "worker-a",
        "run_id": "run",
        "payload": {"method": "tool_settle", "identity": identity, "authorization_id": "grant", "state": "uncertain"},
    }
    with pytest.raises(ValueError, match="Invalid Pi bridge message"):
        decode_message(json.dumps(message))


def test_run_frame_keeps_native_tool_schema_and_private_model_settings():
    from agentloom.runtimes.pi.protocol import decode_message, encode_message

    message = {
        "version": 2,
        "kind": "request",
        "request_id": "host:2",
        "instance_id": "worker-a",
        "run_id": "run",
        "payload": {
            "method": "run",
            "application_id": "app",
            "task_id": "task",
            "task": "read",
            "cwd": "/workspace",
            "instructions": "read only",
            "model": {
                "model_type": "test",
                "model_id": "test/model",
                "protocol": "openai_chat",
                "settings": {"api_key": "private-test-key"},
                "request_headers": {},
            },
            "tools": [
                {
                    "logical_name": "read_file",
                    "visible_name": "read",
                    "owner": "runtime",
                    "provider": "pi",
                    "capability": "file.read",
                    "operation": "read",
                    "parameters": {"type": "object"},
                    "path_parameters": ["path"],
                }
            ],
            "runtime_options": {},
        },
    }
    decoded = decode_message(json.dumps(message))
    assert "private-test-key" not in repr(decoded)
    roundtrip = json.loads(encode_message(decoded))
    assert roundtrip["payload"]["tools"][0]["parameters"] == {"type": "object"}
    assert roundtrip["payload"]["model"]["settings"] == {"api_key": "private-test-key"}
