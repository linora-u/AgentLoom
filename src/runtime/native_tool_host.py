"""Production governance for externally executed, selected native reads.

This host owns no reading algorithm. Adapters must call start_execution with
the saved authorization and dispatch only its returned value, then settle the
actual outcome. Native write/shell and restart recovery belong to later tickets.
"""

from __future__ import annotations

import hashlib
import json
import time
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any
from uuid import uuid4

from agentloom.runtime import get_current_run_context
from agentloom.runtime.native_journal import NativeCallJournal, journal_entry, snapshot
from agentloom.runtime.native_tools import (
    NativeAuthorization,
    NativeCallIdentity,
    NativeCommitAck,
    NativeExecutionOutcome,
    NativeJournalEntry,
    NativePreparation,
    NativePrepareRequest,
    ToolManifestEntry,
)
from agentloom.runtime.tool_gateway import (
    ToolEvidenceExtractor,
    _compress_tool_result,
    _EvidenceCarrier,
    _prepare_tool_input,
    _validate_schema_value,
)
from agentloom.runtime.tool_protocol import ToolCallRecord
from agentloom.runtime.trace import capture_explicit_execution_context


def _check_read_schema(schema: Mapping[str, Any]) -> None:
    # The existing strict decoder supports this subset. Reject unsupported
    # constructs at registration instead of silently dropping constraints.
    allowed = {"type", "properties", "required", "additionalProperties", "items", "enum", "description", "title"}
    if set(schema) - allowed:
        raise ValueError("Unsupported native read schema keyword")
    kinds = schema.get("type")
    if not kinds or any(
        kind not in {"string", "integer", "number", "boolean", "object", "array", "null"}
        for kind in (kinds if isinstance(kinds, list) else [kinds])
    ):
        raise ValueError("Native read schema requires an explicit supported type")
    for child in schema.get("properties", {}).values():
        _check_read_schema(child)
    for key in ("items", "additionalProperties"):
        if isinstance(schema.get(key), Mapping):
            _check_read_schema(schema[key])


class NativeReadToolHost:
    """One host bound to the active Application Run, Hook Run and Agent instance."""

    def __init__(
        self,
        *,
        tools: Iterable[ToolManifestEntry],
        cwd: str,
        evidence_extractors: Mapping[str, ToolEvidenceExtractor] | None = None,
    ):
        self._runtime = get_current_run_context(required=True)
        self._execution = capture_explicit_execution_context()
        if self._execution.hook_run is None or not self._execution.agent_id:
            raise RuntimeError("Native tools require an explicit Hook Run and Agent instance")
        if self._execution.task_id != self._runtime.task_id:
            raise ValueError("Native host task mismatch")
        self._cwd = str(Path(cwd).resolve(strict=True))
        selected = tuple(tools)
        self._tools = {tool.visible_name: tool for tool in selected}
        if len(self._tools) != len(selected):
            raise ValueError("Duplicate native tool definition")
        self._extractors = dict(evidence_extractors or {})
        if set(self._extractors) - set(self._tools) or any(not callable(value) for value in self._extractors.values()):
            raise ValueError("Evidence extractors must belong to selected native tools")
        for tool in self._tools.values():
            if tool.operation != "read" or not tool.path_parameters:
                raise ValueError("NativeReadToolHost only supports declared path reads")
            _check_read_schema(tool.parameters)
            if tool.parameters.get("type") != "object" or tool.parameters.get("additionalProperties") is not False:
                raise ValueError("Native read input must be a closed object schema")
            if any(name not in tool.parameters.get("properties", {}) for name in tool.path_parameters):
                raise ValueError("Native path parameters must be declared in the schema")
        self._journal = NativeCallJournal(self._runtime.run_dir / "native-tools")
        self._closed = False
        self._owned: set[NativeCallIdentity] = set()
        from agentloom.runtime.resources import register_resource

        register_resource(f"native-host:{uuid4().hex}", self.close, instance_id=self._execution.agent_id)

    @property
    def journal_directory(self) -> Path:
        return self._journal.directory

    def _require_scope(self, identity: NativeCallIdentity) -> None:
        active = capture_explicit_execution_context()
        if self._closed:
            raise RuntimeError("Native tool host is closed")
        if (
            get_current_run_context(required=True) != self._runtime
            or active.hook_run is not self._execution.hook_run
            or active.agent_id != self._execution.agent_id
            or active.task_id != self._runtime.task_id
            or (identity.application_id, identity.task_id, identity.run_id, identity.instance_id)
            != (self._runtime.application_id, self._runtime.task_id, self._runtime.run_id, self._execution.agent_id)
        ):
            raise ValueError("Native call scope mismatch")

    def _existing(self, data: dict[str, Any], identity: NativeCallIdentity) -> NativeJournalEntry:
        if not data or data.get("version") != 1 or "authorization_id" not in data:
            raise ValueError("Unknown native call or journal version")
        entry = journal_entry(data)
        if entry.authorization.identity != identity or data["hook_run_id"] != self._execution.local_run_id:
            raise ValueError("Native call identity mismatch")
        return entry

    def prepare(self, request: NativePrepareRequest) -> NativePreparation:
        self._require_scope(request.identity)
        if self._tools.get(request.tool.visible_name) != request.tool or request.cwd != self._cwd:
            raise ValueError("Native tool selection or cwd mismatch")
        with self._journal.transaction(request.identity) as data:
            if data:
                raise ValueError("Native call ID already prepared")

            def decode(arguments):
                final = dict(arguments)
                for key, value in request.tool.fixed_arguments.items():
                    if key in final and final[key] != value:
                        raise ValueError("Native fixed argument cannot be overridden")
                    final[key] = value
                _validate_schema_value(final, request.tool.parameters, "arguments")
                # File-path values must be concrete strings; metadata cannot hide
                # a path inside an unvalidated container or omit it entirely.
                if any(
                    not isinstance(final.get(name), str) or not final[name].strip()
                    for name in request.tool.path_parameters
                ):
                    raise ValueError("Native read requires non-empty path arguments")
                return final, final

            started = time.time()
            prepared = _prepare_tool_input(
                hook_run=self._execution.hook_run,
                call_id=request.identity.call_id,
                tool_name=request.tool.visible_name,
                arguments={**request.tool.fixed_arguments, **request.raw_arguments},
                inputs_schema=request.tool.parameters["properties"],
                decode=decode,
                started_at=started,
                coerce=False,
                cwd=self._cwd,
                manifest=request.tool,
            )
            if isinstance(prepared, ToolCallRecord):
                # A rejected call is durably terminal as well, but never has a grant.
                data.update(
                    version=1,
                    request=snapshot(request),
                    rejection=prepared.to_dict(),
                    hook_run_id=self._execution.local_run_id,
                )
                return NativePreparation(rejection=prepared)
            grant = NativeAuthorization(uuid4().hex, request.identity, request.tool, request.cwd, prepared[0])
            data.update(
                version=1,
                request=snapshot(request),
                authorization_id=grant.authorization_id,
                final_arguments=dict(grant.final_arguments),
                state="authorized",
                hook_run_id=self._execution.local_run_id,
                started_at=started,
            )
            self._owned.add(request.identity)
        return NativePreparation(authorization=grant)

    def start_execution(self, grant: NativeAuthorization) -> NativeAuthorization:
        self._require_scope(grant.identity)
        with self._journal.transaction(grant.identity) as data:
            entry = self._existing(data, grant.identity)
            expected = entry.authorization
            if expected.authorization_id != grant.authorization_id:
                raise ValueError("Native authorization mismatch")
            expected.require_match(grant.identity, grant.tool, grant.cwd, grant.final_arguments)
            if entry.state != "authorized":
                raise ValueError("Native authorization already consumed or cancelled")
            data["state"] = "executing"
        # The fsync barrier above completes before the adapter can execute.
        return expected

    def inspect(self, identity: NativeCallIdentity) -> NativeJournalEntry:
        self._require_scope(identity)
        with self._journal.transaction(identity) as data:
            return self._existing(data, identity)

    def receipt(self, identity: NativeCallIdentity) -> dict[str, Any]:
        """Detached durable provenance, raw output and verified evidence for audit."""
        self._require_scope(identity)
        with self._journal.transaction(identity) as data:
            if (
                not data
                or data.get("version") != 1
                or data["request"]["identity"] != snapshot(identity)
                or data["hook_run_id"] != self._execution.local_run_id
            ):
                raise ValueError("Native receipt identity mismatch")
            return snapshot(data)

    def settle(self, outcome: NativeExecutionOutcome) -> NativeCommitAck | NativeJournalEntry:
        self._require_scope(outcome.identity)
        with self._journal.transaction(outcome.identity) as data:
            entry = self._existing(data, outcome.identity)
            grant = entry.authorization
            if outcome.authorization_id != grant.authorization_id:
                raise ValueError("Native settlement authorization mismatch")
            actual = snapshot(outcome)
            if entry.state == "committed":
                if data["outcome"] != actual:
                    raise ValueError("Conflicting native settlement")
                assert entry.commit is not None
                return entry.commit
            if entry.state in {"cancelled", "uncertain"}:
                return entry
            if entry.state != "executing":
                raise ValueError("Native settlement requires a consumed authorization")
            data["outcome"] = actual
            if outcome.status == "uncertain":
                data["state"] = "uncertain"
                return journal_entry(data)

            evidence: tuple[dict[str, str], ...] = ()
            evidence_status = "none"
            extractor = self._extractors.get(grant.tool.visible_name)
            if outcome.status == "completed" and extractor is not None:
                from agentloom.runtime.trusted_memory_evidence import extract_trusted_memory_evidence

                try:
                    evidence = extract_trusted_memory_evidence(_EvidenceCarrier(extractor), outcome.output)
                    evidence_status = "verified"
                except Exception:
                    evidence_status = "rejected"

            output = outcome.output
            if outcome.status == "completed" and isinstance(output, str):
                try:
                    output = _compress_tool_result(
                        tool_name=grant.tool.visible_name,
                        source=f"native:{grant.tool.provider}:{grant.identity.call_id}",
                        result=output,
                    )
                except Exception:
                    output = outcome.output
            commit_id = uuid4().hex
            output_digest = hashlib.sha256(
                json.dumps(actual["output"], sort_keys=True, ensure_ascii=False).encode()
            ).hexdigest()
            record = ToolCallRecord(
                call_id=grant.identity.call_id,
                tool_name=grant.tool.visible_name,
                input=dict(grant.final_arguments),
                status=outcome.status,
                output=output,
                error=outcome.error,
                started_at=data["started_at"],
                ended_at=time.time(),
                metadata={
                    "native": {
                        "identity": snapshot(grant.identity),
                        "provider": grant.tool.provider,
                        "logical_name": grant.tool.logical_name,
                        "authorization_id": grant.authorization_id,
                        "commit_id": commit_id,
                        "cwd": grant.cwd,
                        "raw_output_sha256": output_digest,
                    }
                },
            )
            data.update(
                state="committed",
                commit_id=commit_id,
                record=record.to_dict(),
                raw_output=actual["output"],
                evidence=snapshot(evidence),
                evidence_status=evidence_status,
            )
            ack = journal_entry(data).commit
            assert ack is not None
        # Observer failure is deliberately outside the atomic commit. Replays do
        # not emit evidence twice, and observers never supply a commit barrier.
        self._observe(ack, grant, evidence)
        return ack

    def _observe(self, ack: NativeCommitAck, grant: NativeAuthorization, evidence: tuple[dict[str, str], ...]) -> None:
        from agentloom.runtime.hooks.types import HookEvent
        from agentloom.runtime.logging import get_logger
        from agentloom.runtime.trusted_memory_evidence import (
            TRUSTED_MEMORY_EVIDENCE_RESPONSE_KEY,
            TrustedMemoryEvidenceEnvelope,
        )

        run = self._execution.hook_run
        record = ack.record
        response = {"result": record.output} if record.status == "completed" else {"error": record.reason}
        if evidence:
            response[TRUSTED_MEMORY_EVIDENCE_RESPONSE_KEY] = TrustedMemoryEvidenceEnvelope(evidence)
        try:
            run.record_tool_outcome(record)
            run.dispatch(
                HookEvent.POST_TOOL_USE if record.status == "completed" else HookEvent.POST_TOOL_USE_FAILURE,
                record.tool_name,
                record.input,
                tool_call_id=record.call_id,
                tool_response=response,
                tool_inputs_schema=dict(grant.tool.parameters["properties"]),
                cwd=grant.cwd,
            )
            run.flush_user_messages()
        except Exception:
            get_logger(__name__).warning("Native tool outcome observer failed after durable commit")

    def _cancel_owned(self, identity: NativeCallIdentity) -> NativeJournalEntry:
        with self._journal.transaction(identity) as data:
            entry = self._existing(data, identity)
            if entry.state == "authorized":
                data["state"] = "cancelled"
            elif entry.state == "executing":
                data["state"] = "uncertain"
            return journal_entry(data)

    def cancel(self, identity: NativeCallIdentity) -> NativeJournalEntry:
        self._require_scope(identity)
        return self._cancel_owned(identity)

    def close(self) -> None:
        if not self._closed:
            try:
                for identity in self._owned:
                    self._cancel_owned(identity)
            finally:
                self._closed = True
                self._journal.close()

    def __enter__(self) -> NativeReadToolHost:
        return self

    def __exit__(self, *_args: Any) -> None:
        self.close()
