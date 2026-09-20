"""Executable contract example shared with tickets 05/06/09/12.

This intentionally tiny host is a fixture, NOT the production Tool Gateway or
permission engine. Production implementations must rerun these scenarios with
their own host and the real Pi executor before advertising native support.
"""

import json
import os
from dataclasses import replace
from pathlib import Path

from agentloom.runtime.native_tools import (
    NativeAuthorization,
    NativeCommitAck,
    NativeExecutionOutcome,
    NativeJournalEntry,
    NativePreparation,
    NativePrepareRequest,
)
from agentloom.runtime.tool_protocol import ToolCallRecord


class ContractHostFixture:
    def __init__(self, directory: Path):
        self.directory = directory
        self.trace = []
        self.journal = None
        self.consumed = set()

    def prepare(self, request: NativePrepareRequest) -> NativePreparation:
        self.trace.append("transform")
        final = dict(request.raw_arguments)
        if type(final.get("path")) is int:
            final["path"] = f"{final['path']}.txt"
        if type(final.get("content")) is int:
            final["content"] = str(final["content"])
        self.trace.append("strict_decode")
        if set(final) != {"path", "content"} or any(type(value) is not str for value in final.values()):
            raise ValueError("Fixture write requires path and content")
        self.trace.append("authorize")
        # A deliberately narrow fixture policy, independent of production rules.
        if final["path"] != "7.txt":
            return NativePreparation(
                rejection=ToolCallRecord.blocked(
                    call_id=request.identity.call_id,
                    tool_name=request.tool.visible_name,
                    input=final,
                    message="Fixture denies this path",
                    stage="authorization",
                )
            )
        self.trace.append("backup")
        path = self.directory / final["path"]
        if path.exists():
            (self.directory / "backup.txt").write_bytes(path.read_bytes())
        grant = NativeAuthorization("grant-1", request.identity, request.tool, request.cwd, final)
        self.journal = NativeJournalEntry(grant, "authorized", request)
        self.trace.append("grant")
        return NativePreparation(authorization=grant)

    def start_execution(self, grant):
        grant.require_match(
            self.journal.request.identity, self.journal.request.tool, self.journal.request.cwd, grant.final_arguments
        )
        if grant.authorization_id in self.consumed or self.journal.state != "authorized":
            raise ValueError("Authorization already consumed or cancelled")
        self.consumed.add(grant.authorization_id)
        self.journal = replace(self.journal, state="executing")
        self.trace.append("execute")

    def settle(self, outcome: NativeExecutionOutcome):
        grant = self.journal.authorization
        if (
            outcome.identity != grant.identity
            or outcome.authorization_id != grant.authorization_id
            or self.journal.state != "executing"
        ):
            raise ValueError("Unmatched settlement")
        if outcome.status == "uncertain":
            self.journal = replace(self.journal, state="uncertain")
            return self.journal
        record = ToolCallRecord(
            call_id=outcome.identity.call_id,
            tool_name=grant.tool.visible_name,
            input=dict(grant.final_arguments),
            status=outcome.status,
            output=outcome.output,
            error=outcome.error,
        )
        # A real fsync establishes the fixture's ack ordering, not production durability.
        with (self.directory / "committed.json").open("w") as stream:
            json.dump(record.to_dict(), stream)
            stream.flush()
            os.fsync(stream.fileno())
        ack = NativeCommitAck(outcome.identity, grant.authorization_id, "commit-1", record)
        self.journal = replace(self.journal, state="committed", commit=ack)
        self.trace.append("commit")
        return ack

    def cancel(self, identity):
        if identity != self.journal.authorization.identity:
            raise ValueError("Cancellation identity mismatch")
        state = "uncertain" if self.journal.state == "executing" else "cancelled"
        self.journal = replace(self.journal, state=state)
        self.trace.append("cancel")
        return self.journal
