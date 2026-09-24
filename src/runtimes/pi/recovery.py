"""Validate Pi/host alignment before the SDK appends any recovered result."""
from __future__ import annotations

from typing import Any

from agentloom.execution.agent_runtime import AgentRuntimeError
from agentloom.execution.native_journal import snapshot
from agentloom.execution.native_tools import NativeCallIdentity
from agentloom.execution.tool_protocol import MODEL_OUTPUT_METADATA_KEY, ToolCallRecord
from agentloom.runtimes.pi.checkpoint import PiCheckpointStore


def _result(call: dict[str, Any], record: dict[str, Any] | None) -> dict[str, Any]:
    content: list[dict[str, Any]]
    details: Any
    if record is None:
        # Only an authoritative prepared/authorized/cancelled state permits
        # this result. A missing journal or executing call is never sufficient.
        content = [{"type": "text", "text": "Interrupted before dispatch; this tool did not execute. Request a new call if still needed."}]
        details = {"agentloom_recovery": {"state": "not_executed"}}
        error = True
    elif record["status"] != "completed":
        content = [{"type": "text", "text": ToolCallRecord.from_dict(record).model_content()}]
        # The SDK projects both blocked calls and thrown execution errors with
        # empty details, unlike successful platform results.
        details = {}
        error = True
    elif call["owner"] == "runtime":
        output = record["output"]
        if not isinstance(output, dict) or not isinstance(output.get("content"), list):
            raise ValueError("Committed Pi output is not an SDK tool result")
        content, details, error = output["content"], output.get("details"), False
    else:
        projection = ToolCallRecord.from_dict(record).model_content()
        content = [{"type": "text", "text": projection}]
        wire_record = (
            {**record, "output": None}
            if MODEL_OUTPUT_METADATA_KEY in record.get("metadata", {}) else record
        )
        details, error = {"agentloom": wire_record}, False
    result = {"role": "toolResult", "toolCallId": call["identity"]["call_id"],
              "toolName": call["tool_name"], "content": content, "isError": error}
    if details is not None:
        result["details"] = details
    return result


def reconcile(store: PiCheckpointStore, bundle: dict[str, Any]) -> dict[str, Any]:
    """Produce an all-checked append plan; never invoke an executor here."""
    entries = bundle["session"]["entries"]
    calls = {(call["identity"]["native_parent_id"], call["identity"]["call_id"]): call
             for call in bundle["calls"]}
    manifests = {tool.visible_name: tool for tool in store.definition.tool_manifest}
    observed: set[tuple[str | None, str]] = set()
    active: dict[str, tuple[str | None, str]] = {}
    results: dict[tuple[str | None, str], Any] = {}
    assistants: dict[tuple[str | None, str], Any] = {}
    previous = None
    entry_ids: set[str] = set()
    for entry in entries:
        if (not isinstance(entry, dict) or entry.get("parentId") != previous
                or not isinstance(entry.get("id"), str) or entry["id"] in entry_ids):
            raise ValueError("Pi recovery requires an unambiguous native session branch")
        entry_ids.add(entry["id"])
        previous = entry["id"]
        if entry.get("type") != "message":
            continue
        message = entry["message"]
        if message.get("role") == "assistant":
            for part in message.get("content", []):
                if part.get("type") != "toolCall":
                    continue
                key = (entry["parentId"], part["id"])
                call = calls.get(key)
                if (call is None or key in observed or part["id"] in active or call["tool_name"] != part["name"]
                        or call["arguments"] != part["arguments"]
                        or call["identity"]["native_parent_id"] != entry["parentId"]):
                    raise ValueError("Pi assistant and host call anchors do not align")
                observed.add(key)
                active[part["id"]] = key
                assistants[key] = entry
        elif message.get("role") == "toolResult":
            call_id = message["toolCallId"]
            result_key = active.pop(call_id) if call_id in active else None
            if result_key is None or result_key in results:
                raise ValueError("Pi session has an unmatched or duplicate tool result")
            results[result_key] = message
    if set(calls) != observed:
        raise ValueError("Pi checkpoint contains calls outside its native session")

    append = []
    for key in assistants:
        call = calls[key]
        call_id = key[1]
        identity = NativeCallIdentity(**call["identity"])
        manifest = manifests.get(call["tool_name"])
        if manifest is None or manifest.owner != call["owner"]:
            raise ValueError("Pi recovered tool is no longer selected")
        if call["owner"] == "runtime":
            data = store.native_receipt(identity)
            if (data["request"]["tool"] != snapshot(manifest)
                    or data.get("final_arguments", data["request"]["raw_arguments"]) != call["arguments"]):
                raise ValueError("Pi native journal inputs or mapping changed")
            record = data.get("record") or data.get("rejection") or data.get("dispatch_rejection")
        else:
            data = store.platform_receipt(identity)
            if data["tool_name"] != call["tool_name"] or data["arguments"] != call["arguments"]:
                raise ValueError("Pi platform journal inputs changed")
            if data["state"] == "executing":
                from agentloom.execution.tool_gateway import RecoverablePreparedToolGateway
                recovered = store.definition.tool_gateway.reconcile_committed(
                    call_id=identity.call_id,
                    tool_name=call["tool_name"],
                    arguments=call["arguments"],
                    run_id=identity.run_id,
                ) if isinstance(store.definition.tool_gateway, RecoverablePreparedToolGateway) else None
                if recovered is not None:
                    store.reconcile_platform(identity, recovered)
                    data = store.platform_receipt(identity)
            if data["state"] == "executing":
                data = store.platform_receipt(identity, recovering=True)
            record = data.get("record")
        if data.get("state") in {"executing", "uncertain"}:
            raise AgentRuntimeError("Pi recovery has an uncertain tool effect; automatic replay is forbidden", category="tool")
        if record is None and data.get("state") not in {"prepared", "authorized", "cancelled"}:
            raise ValueError("Pi recovery has no authoritative tool outcome")
        if record is not None and (record["call_id"] != call_id or record["tool_name"] != call["tool_name"]):
            raise ValueError("Pi committed result identity mismatch")
        expected = _result(call, record)
        existing = results.get(key)
        if existing is not None:
            if existing.get("toolName") != call["tool_name"] or existing.get("isError") != expected["isError"]:
                raise ValueError("Pi native result conflicts with host outcome")
            if existing.get("content") != expected["content"]:
                raise ValueError("Pi native result differs from committed host output")
            if existing.get("details") != expected.get("details"):
                raise ValueError("Pi native result details differ from committed host output")
            continue
        # Appending behind a later assistant would change which turn the
        # result belongs to. Such corruption is rejected before SDK mutation.
        assistant = assistants[key]
        if any(entry.get("type") == "message" and entry["message"].get("role") in {"user", "assistant"}
               for entry in entries[entries.index(assistant) + 1:]):
            raise ValueError("Pi missing result is not at a recoverable session tail")
        append.append(expected)
    return {"bundle": bundle, "append": append}
