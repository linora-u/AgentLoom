"""Reject inconsistent recovery evidence through the real Application and Pi SDK."""
from __future__ import annotations

from contextlib import contextmanager
import hashlib
import json
import sys

import pytest

from agentloom.app.run import ApplicationRunError
from agentloom.app.runner import execute_app
from agentloom.config.config import bind_config, load_project_config
from tests.pi_test.test_application import model_service, project
from tests.pi_test.test_recovery_application import audit, checkpoints, enable


_platform_calls = []


def receipt_probe(label: str) -> str:
    """Return a known platform result and record actual invocations.

    Args:
        label: Harmless value identifying this invocation.
    """
    _platform_calls.append(label)
    return "platform-receipt-proof:" + label


@contextmanager
def _interrupted_after_tool(root, kind):
    (root / "proof.txt").write_text("native-receipt-proof")
    if kind == "platform":
        _platform_calls.clear()
        call = ("receipt-call", "receipt_probe", {"label": "observed-once"})
        tools = [{"name": "receipt_probe", "module": __name__, "function": "receipt_probe"}]
    else:
        call = ("receipt-call", "read", {"path": "missing.txt" if kind == "error" else "proof.txt"})
        tools = [{"name": "read"}]
    with model_service(turns=[[call]], fail_requests={2: 500}) as (url, requests):
        app = project(root, url)
        options: dict = {"tools": tools}
        if kind == "rejection":
            hook = root / "reject_read.py"
            hook.write_text('import json\nprint(json.dumps({"decision": "block", "reason": "blocked-receipt-proof"}))\n')
            options["hooks"] = {"PreToolUse": [{"id": "reject-read", "matcher": "read",
                                               "command": f"{sys.executable} {hook}"}]}
        enable(app, **options)
        with bind_config(load_project_config(root)):
            with pytest.raises(ApplicationRunError) as interrupted:
                execute_app(app, file_logging=False)
            assert len(requests) == 2
            if kind == "platform":
                assert _platform_calls == ["observed-once"]
            yield app, requests, interrupted.value.run


def _receipt(root, *, platform=False):
    paths = list(root.rglob("pi/platform/*.json" if platform else "native-tools/*.json"))
    assert len(paths) == 1
    return paths[0], json.loads(paths[0].read_text())


def _rewrite_native_result(root, change):
    """Keep the content hash valid so recovery reaches semantic alignment."""
    [(checkpoint_path, checkpoint)] = checkpoints(root)
    envelope = checkpoint["runtime_checkpoint"]
    artifact = checkpoint_path.parent / "pi/sessions" / (envelope["payload"]["artifact"] + ".json")
    bundle = json.loads(artifact.read_text())
    results = [entry["message"] for entry in bundle["session"]["entries"]
               if entry.get("message", {}).get("role") == "toolResult"]
    assert len(results) == 1
    change(results[0])
    raw = json.dumps(bundle).encode()
    digest = hashlib.sha256(raw).hexdigest()
    artifact.with_name(digest + ".json").write_bytes(raw)
    envelope["payload"]["artifact"] = digest
    checkpoint_path.write_text(json.dumps(checkpoint))


def _assert_rejected_before_model(app, requests, first):
    with pytest.raises(ApplicationRunError) as rejected:
        execute_app(app, resume_task_id=first.task_id, file_logging=False)
    assert len(requests) == 2
    assert [event["details"]["state"] for event in audit(rejected.value.run)
            if event["kind"] == "terminal"] == ["failed"]


@pytest.mark.parametrize("kind", ["rejection", "error"])
def test_valid_native_negative_result_remains_recoverable(tmp_path, kind):
    with _interrupted_after_tool(tmp_path, kind) as (app, requests, first):
        path, receipt = _receipt(tmp_path)
        if kind == "rejection":
            assert receipt["rejection"]["status"] == "blocked"
            assert "authorization_id" not in receipt
        else:
            assert receipt["state"] == "committed"
            assert receipt["record"]["status"] == "error"
        before = path.read_bytes()
        resumed = execute_app(app, resume_task_id=first.task_id, file_logging=False)
        assert resumed.output == "Pi answer"
        assert len(requests) == 3
        assert path.read_bytes() == before
        assert list(tmp_path.rglob("native-tools/*.json")) == [path]
        assert [event["details"]["state"] for event in audit(resumed.run)
                if event["kind"] == "terminal"] == ["success"]


@pytest.mark.parametrize("damage", ["cancelled", "unknown", "missing_state", "missing_commit_id"])
def test_native_commit_requires_consistent_state_and_commit_ack(tmp_path, damage):
    with _interrupted_after_tool(tmp_path, "native") as (app, requests, first):
        path, receipt = _receipt(tmp_path)
        assert receipt["state"] == "committed"
        assert receipt["record"]["status"] == "completed"
        if damage == "missing_state":
            del receipt["state"]
        elif damage == "missing_commit_id":
            del receipt["commit_id"]
        else:
            receipt["state"] = damage
        path.write_text(json.dumps(receipt))
        _assert_rejected_before_model(app, requests, first)


@pytest.mark.parametrize("damage", [None, "prepared", "cancelled", "unknown", "missing_state"])
def test_platform_commit_requires_consistent_terminal_state(tmp_path, damage):
    with _interrupted_after_tool(tmp_path, "platform") as (app, requests, first):
        path, receipt = _receipt(tmp_path, platform=True)
        assert receipt["state"] == "committed"
        assert receipt["record"]["status"] == "completed"
        if damage is None:
            resumed = execute_app(app, resume_task_id=first.task_id, file_logging=False)
            assert resumed.output == "Pi answer"
            assert len(requests) == 3
        else:
            if damage == "missing_state":
                del receipt["state"]
            else:
                receipt["state"] = damage
            path.write_text(json.dumps(receipt))
            _assert_rejected_before_model(app, requests, first)
        assert _platform_calls == ["observed-once"]


@pytest.mark.parametrize("kind", ["rejection", "error"])
def test_native_negative_transcript_content_must_match_host_outcome(tmp_path, kind):
    with _interrupted_after_tool(tmp_path, kind) as (app, requests, first):
        def corrupt(message):
            assert message["isError"] is True
            message["content"] = [{"type": "text", "text": "forged-negative-result-body"}]

        _rewrite_native_result(tmp_path, corrupt)
        _assert_rejected_before_model(app, requests, first)


def test_platform_transcript_record_metadata_must_match_host_receipt(tmp_path):
    with _interrupted_after_tool(tmp_path, "platform") as (app, requests, first):
        def corrupt(message):
            assert message["isError"] is False
            assert message["details"]["agentloom"]["status"] == "completed"
            message["details"]["agentloom"]["tool_name"] = "unrelated-tool"

        _rewrite_native_result(tmp_path, corrupt)
        _assert_rejected_before_model(app, requests, first)
        assert _platform_calls == ["observed-once"]


@pytest.mark.parametrize("decision", ["modify", "block"])
def test_platform_preparation_runs_once_and_survives_recovery(tmp_path, decision):
    _platform_calls.clear()
    marker = tmp_path / 'hook-count.txt'
    hook = tmp_path / 'prepare.py'
    effect = ({'decision': 'modify', 'modified_input': {'label': 'observed-once'}}
              if decision == 'modify' else {'decision': 'block', 'reason': 'preparation-blocked-proof'})
    hook.write_text('import json\nfrom pathlib import Path\n'
                    f'p=Path({str(marker)!r});p.write_text(p.read_text()+"x" if p.exists() else "x")\n'
                    f'print(json.dumps({effect!r}))\n')
    with model_service(turns=[[('prepared-call', 'receipt_probe', {'label': 19})]], fail_requests={2: 500}) as (url, requests):
        app = project(tmp_path, url)
        enable(app, tools=[{'name': 'receipt_probe', 'module': __name__, 'function': 'receipt_probe'}],
               hooks={'PreToolUse': [{'id': 'prepare', 'matcher': 'receipt_probe', 'command': f'{sys.executable} {hook}'}]})
        with bind_config(load_project_config(tmp_path)):
            with pytest.raises(ApplicationRunError) as interrupted:
                execute_app(app, file_logging=False)
            assert len(requests) == 2 and marker.read_text() == 'x'
            path, receipt = _receipt(tmp_path, platform=True)
            assert receipt['state'] == 'committed'
            assert receipt['record']['status'] == ('completed' if decision == 'modify' else 'blocked')
            assert _platform_calls == (['observed-once'] if decision == 'modify' else [])
            original = path.read_bytes()
            resumed = execute_app(app, resume_task_id=interrupted.value.run.task_id, file_logging=False)
        assert resumed.output == 'Pi answer' and len(requests) == 3
        assert marker.read_text() == 'x' and path.read_bytes() == original
        assert _platform_calls == (['observed-once'] if decision == 'modify' else [])
        if decision == 'modify':
            for captured in requests[1:]:
                request = captured[1]
                assistant = next(message for message in request['messages'] if message.get('tool_calls'))
                assert json.loads(assistant['tool_calls'][0]['function']['arguments']) == {'label': 'observed-once'}


def test_platform_call_id_can_be_reused_at_a_later_native_position(tmp_path):
    _platform_calls.clear()
    call = [('reused-platform-id', 'receipt_probe', {'label': 'same-id'})]
    with model_service(turns=[call, call]) as (url, requests):
        app = project(tmp_path, url)
        enable(app, tools=[
            {'name': 'receipt_probe', 'module': __name__, 'function': 'receipt_probe'},
        ])
        with bind_config(load_project_config(tmp_path)):
            result = execute_app(app, file_logging=False)
    assert result.output == 'Pi answer'
    assert _platform_calls == ['same-id', 'same-id']
    receipts = [json.loads(path.read_text()) for path in tmp_path.rglob('pi/platform/*.json')]
    assert len(receipts) == 2
    identities = [receipt['identity'] for receipt in receipts]
    assert {identity['call_id'] for identity in identities} == {'reused-platform-id'}
    assert len({identity['native_parent_id'] for identity in identities}) == 2
    assert len(requests) == 3


@pytest.mark.parametrize(
    ("owner", "arguments"),
    [
        ("native", {}),
        ("native", {"path": []}),
        ("platform", {}),
        ("platform", {"label": []}),
    ],
    ids=[
        "native-missing-field",
        "native-wrong-type",
        "platform-missing-field",
        "platform-wrong-type",
    ],
)
def test_invalid_arguments_reuse_the_durable_rejection_across_resume(
    tmp_path, owner, arguments,
):
    """Pi's first result and recovery must project one host-owned rejection."""
    _platform_calls.clear()
    if owner == "native":
        tool_name = "read"
        tools = [{"name": tool_name}]
    else:
        tool_name = "receipt_probe"
        tools = [
            {
                "name": tool_name,
                "module": __name__,
                "function": "receipt_probe",
            }
        ]

    with model_service(
        turns=[[("invalid-call", tool_name, arguments)]],
        fail_requests={2: 500},
    ) as (url, requests):
        app = project(tmp_path, url)
        enable(app, tools=tools)
        with bind_config(load_project_config(tmp_path)):
            with pytest.raises(ApplicationRunError) as interrupted:
                execute_app(app, file_logging=False)

            receipt_path, receipt = _receipt(
                tmp_path,
                platform=owner == "platform",
            )
            record = (
                receipt["record"]
                if owner == "platform"
                else receipt["rejection"]
            )
            assert record["status"] == "blocked"
            expected_text = record["error"]["message"]
            [(checkpoint_path, checkpoint)] = checkpoints(tmp_path)
            envelope = checkpoint["runtime_checkpoint"]
            artifact = checkpoint_path.parent / "pi/sessions" / (
                envelope["payload"]["artifact"] + ".json"
            )
            bundle = json.loads(artifact.read_text())
            tool_results = [
                entry["message"]
                for entry in bundle["session"]["entries"]
                if entry.get("message", {}).get("role") == "toolResult"
            ]
            assert len(tool_results) == 1
            assert tool_results[0]["isError"] is True
            assert tool_results[0]["content"] == [
                {"type": "text", "text": expected_text}
            ]
            assert tool_results[0]["details"] == {}
            receipt_before = receipt_path.read_bytes()

            resumed = execute_app(
                app,
                resume_task_id=interrupted.value.run.task_id,
                file_logging=False,
            )

    assert resumed.output == "Pi answer"
    assert len(requests) == 3
    assert receipt_path.read_bytes() == receipt_before
    assert _platform_calls == []
