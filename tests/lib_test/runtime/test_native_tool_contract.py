"""Reusable public contract cases for native executor implementations."""

from dataclasses import replace

import pytest


def test_native_authorization_binds_final_input_provider_and_instance():
    from agentloom.runtime.native_tools import (
        NativeAuthorization,
        NativeCallIdentity,
        ToolManifestEntry,
    )

    identity = NativeCallIdentity("app", "task", "run", "worker-a", "call-1")
    entry = ToolManifestEntry(
        logical_name="read_file",
        visible_name="read",
        owner="runtime",
        provider="pi",
        capability="file.read",
        operation="read",
        parameters={"type": "object"},
        path_parameters=("path",),
    )
    authorization = NativeAuthorization(
        authorization_id="grant-1",
        identity=identity,
        tool=entry,
        cwd="/workspace",
        final_arguments={"path": "approved.txt"},
    )
    authorization.require_match(identity, entry, "/workspace", {"path": "approved.txt"})
    for other_id, other_tool, other_cwd, arguments in (
        (replace(identity, instance_id="worker-b"), entry, "/workspace", {"path": "approved.txt"}),
        (identity, replace(entry, provider="smolagents"), "/workspace", {"path": "approved.txt"}),
        (identity, entry, "/other", {"path": "approved.txt"}),
        (identity, entry, "/workspace", {"path": "secret.txt"}),
    ):
        with pytest.raises(ValueError, match="authorization mismatch"):
            authorization.require_match(other_id, other_tool, other_cwd, arguments)


def test_python_tool_gateway_exposes_selected_owned_manifest():
    from agentloom.runtime.tool_gateway import AgentLoomToolGateway, bind_tool
    from agentloom.tools.loader import resolve_tool_function

    gateway = AgentLoomToolGateway([bind_tool(resolve_tool_function("read_file"))])
    (entry,) = gateway.manifest
    assert (entry.logical_name, entry.visible_name, entry.provider, entry.owner) == (
        "read_file",
        "read_file",
        "smolagents",
        "runtime",
    )
    assert entry.capability == "file.read"
    assert entry.parameters == gateway.definitions[0].parameters
    assert entry.path_parameters == ("file_path",)


@pytest.fixture
def write_contract(tmp_path):
    from agentloom.runtime.native_tools import NativeCallIdentity, NativePrepareRequest, ToolManifestEntry
    from native_contract_fixture import ContractHostFixture

    request = NativePrepareRequest(
        NativeCallIdentity("app", "task", "run", "worker", "call", "session", "parent"),
        ToolManifestEntry("write_file", "write", "runtime", "pi", "file.write", "write", {"type": "object"}, ("path",)),
        str(tmp_path),
        {"path": 7, "content": 42},
    )
    return ContractHostFixture(tmp_path), request, tmp_path / "7.txt"


def test_shared_contract_repairs_before_backup_and_commits_before_ack(write_contract):
    import json

    from agentloom.runtime.native_tools import NativeExecutionOutcome

    host, request, target = write_contract
    target.write_text("original")
    grant = host.prepare(request).authorization
    assert request.raw_arguments == {"path": 7, "content": 42}
    assert grant.final_arguments == {"path": "7.txt", "content": "42"}
    assert host.trace == ["transform", "strict_decode", "authorize", "backup", "grant"]
    assert (target.parent / "backup.txt").read_text() == "original"
    host.start_execution(grant)
    target.write_text(grant.final_arguments["content"])
    ack = host.settle(NativeExecutionOutcome(request.identity, grant.authorization_id, "completed", "written"))
    assert json.loads((target.parent / "committed.json").read_text())["output"] == ack.record.output
    assert target.read_text() == "42"
    assert host.journal.recovery_action == "replay_result"
    with pytest.raises(ValueError, match="already consumed"):
        host.start_execution(grant)


def test_shared_contract_denial_prevents_effect_and_backup(write_contract):
    host, request, target = write_contract
    request = replace(request, raw_arguments={"path": "denied.txt", "content": "never"})
    result = host.prepare(request)
    assert result.authorization is None
    assert result.rejection.status == "blocked"
    assert host.trace == ["transform", "strict_decode", "authorize"]
    assert list(target.parent.iterdir()) == []


@pytest.mark.parametrize("committed", [False, True])
def test_shared_contract_crash_windows_never_reexecute(write_contract, committed):
    from agentloom.runtime.native_tools import NativeExecutionOutcome
    from agentloom.runtime.tool_protocol import ToolErrorRecord

    host, request, target = write_contract
    grant = host.prepare(request).authorization
    host.start_execution(grant)
    target.write_text("one effect\n")
    if committed:
        host.settle(NativeExecutionOutcome(request.identity, grant.authorization_id, "completed", "written"))
        # Native toolResult persistence has not happened yet: replay only this result.
        assert host.journal.recovery_action == "replay_result"
        assert host.journal.commit.record.output == "written"
    else:
        host.settle(
            NativeExecutionOutcome(
                request.identity,
                grant.authorization_id,
                "uncertain",
                error=ToolErrorRecord("process_died", "lost after effect", False, "execution"),
            )
        )
        assert host.journal.recovery_action == "no_reexecute"
        assert host.journal.commit is None
    assert target.read_text().splitlines() == ["one effect"]
    with pytest.raises(ValueError, match="already consumed"):
        host.start_execution(grant)


@pytest.mark.parametrize("started", [False, True])
def test_shared_contract_cancellation_distinguishes_unstarted_and_uncertain(write_contract, started):
    host, request, target = write_contract
    grant = host.prepare(request).authorization
    if started:
        host.start_execution(grant)
    entry = host.cancel(request.identity)
    assert entry.recovery_action == ("no_reexecute" if started else "not_executed")
    assert entry.commit is None
    with pytest.raises(ValueError, match="cancelled"):
        host.start_execution(grant)
    assert not target.exists()
