"""Receipt commits remain authoritative when Trace events have not committed."""

import json
import re
from dataclasses import asdict, replace
from types import SimpleNamespace

import pytest
from agentloom.execution.context import RuntimeContext
from agentloom.execution.native_journal import snapshot
from agentloom.execution.native_tools import NativeCallIdentity, ToolManifestEntry
from agentloom.execution.observability import RunTrace, TraceStorageError, bind_trace_recorder
from agentloom.execution.storage import SecureDirectory
from agentloom.execution.tool_protocol import MODEL_OUTPUT_METADATA_KEY, ToolCallRecord
from agentloom.runtimes.pi.recovery import reconcile


def receipt_store(context, owner, model_content):
    manifest = ToolManifestEntry(logical_name="probe", visible_name="probe", owner=owner,
        provider="fixture", capability="fixture.read", operation="read",
        parameters={"type": "object", "properties": {"query": {"type": "string"}}})
    identity = NativeCallIdentity(context.application_id, context.task_id, "run_before", "instance",
                                  "tool-one", "session", None)
    arguments = {"query": "one record"}
    call = {"identity": asdict(identity), "tool_name": "probe", "arguments": arguments, "owner": owner}
    output = {"content": [{"type": "text", "text": model_content}]} if owner == "runtime" else "original payload"
    record = ToolCallRecord.completed(call_id="tool-one", tool_name="probe", input=arguments, output=output,
        metadata={} if owner == "runtime" else {MODEL_OUTPUT_METADATA_KEY: model_content}).to_dict()
    receipt = {"state": "committed", "tool_name": "probe", "arguments": arguments, "record": record,
               "request": {"tool": snapshot(manifest), "raw_arguments": arguments}}
    store = SimpleNamespace(runtime=context, definition=SimpleNamespace(tool_manifest=(manifest,)),
        native_receipt=lambda _: receipt, platform_receipt=lambda _: receipt)
    bundle = {"calls": [call], "session": {"entries": [{"type": "message", "id": "assistant",
        "parentId": None, "message": {"role": "assistant", "content": [
            {"type": "toolCall", "id": "tool-one", "name": "probe", "arguments": arguments}]}}]}}
    return store, bundle


@pytest.mark.parametrize("owner", ["platform", "runtime"])
@pytest.mark.parametrize("damage", [None, "reference", "payload", "trace"])
def test_trace_off_verifies_journal_reference_before_tool_trace_event(tmp_path, monkeypatch, owner, damage):
    previous = RuntimeContext(tmp_path / "runtime", "app", "task_one", "run_before")
    current = replace(previous, run_id="run_after")
    with bind_trace_recorder(previous) as recorder:
        model_content = recorder.project_tool_result("retained-context-" * 4000,
            tool_name="probe", source="receipt", call_id="tool-one")
        assert "[ContextRef " in model_content
        # Simulate settlement between the journal commit and record_tool().
        with RunTrace(recorder.storage.duplicate(), previous.run_id) as trace:
            assert trace.events() == []
    reference = re.search(r"ctx_[0-9a-f]{32}", model_content).group()
    reference_path = previous.trace_dir / "refs" / (reference + ".json")
    if damage == "reference":
        reference_path.unlink()
    elif damage == "payload":
        metadata = json.loads(reference_path.read_text())
        digest = metadata.get("storage_sha256", metadata["sha256"])
        (previous.trace_dir / "payloads" / (digest + ".blob")).write_bytes(b"damaged")
    elif damage == "trace":
        previous.trace_dir.rename(previous.trace_dir.with_name("missing-prior-trace"))
    before = {path.relative_to(current.root_dir): path.read_bytes()
              for path in current.root_dir.rglob("*") if path.is_file()}
    readers = []

    class ReadOnlyDirectory(SecureDirectory):
        def __init__(self, path, *, create=True):
            assert create is False
            super().__init__(path, create=create)
            readers.append(self)

    def forbidden_recorder(*_args, **_kwargs):
        raise AssertionError("Recovery must not create a Trace recorder")

    monkeypatch.setattr("agentloom.runtimes.pi.recovery.SecureDirectory", ReadOnlyDirectory)
    monkeypatch.setattr("agentloom.execution.observability.TraceRecorder.__init__", forbidden_recorder)
    store, bundle = receipt_store(current, owner, model_content)
    with bind_trace_recorder(None):
        if damage:
            with pytest.raises(TraceStorageError):
                reconcile(store, bundle)
        else:
            plan = reconcile(store, bundle)
            assert len(plan["append"]) == 1
            assert plan["append"][0]["content"] == [{"type": "text", "text": model_content}]
    assert all(reader.closed for reader in readers)
    after = {path.relative_to(current.root_dir): path.read_bytes()
             for path in current.root_dir.rglob("*") if path.is_file()}
    assert after == before
    if damage == "trace":
        assert not current.trace_dir.exists()


@pytest.mark.parametrize("owner", ["platform", "runtime"])
def test_trace_off_receipt_without_retained_refs_never_opens_trace(tmp_path, monkeypatch, owner):
    current = RuntimeContext(tmp_path / "runtime", "app", "task_one", "run_after")

    def forbidden(*_args, **_kwargs):
        raise AssertionError("A receipt without retained references needs no Trace storage")

    monkeypatch.setattr("agentloom.runtimes.pi.recovery.SecureDirectory", forbidden)
    monkeypatch.setattr("agentloom.execution.observability.TraceRecorder.__init__", forbidden)
    store, bundle = receipt_store(current, owner, "plain tool result")
    with bind_trace_recorder(None):
        assert len(reconcile(store, bundle)["append"]) == 1
    assert not current.root_dir.exists()


@pytest.mark.parametrize("owner", ["platform", "runtime"])
def test_full_trace_recovery_keeps_the_borrowed_recorder_open(tmp_path, monkeypatch, owner):
    current = RuntimeContext(tmp_path / "runtime", "app", "task_one", "run_after")

    def forbidden(*_args, **_kwargs):
        raise AssertionError("An active recorder already owns the reference reader")

    with bind_trace_recorder(current) as recorder:
        model_content = recorder.project_tool_result("retained-context-" * 4000,
            tool_name="probe", source="receipt", call_id="tool-one")
        store, bundle = receipt_store(current, owner, model_content)
        monkeypatch.setattr("agentloom.runtimes.pi.recovery.SecureDirectory", forbidden)
        assert len(reconcile(store, bundle)["append"]) == 1
        assert not recorder.storage.closed
        recorder.record_final_answer("still writable after recovery")
