"""Production native governance; only the external file executor is a fixture."""

from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path

import pytest
from agentloom.runtime import RuntimeContext, bind_run_context
from agentloom.runtime.hooks import HookEvent, HookHandler, HookPlan, HookResult, HookRun
from agentloom.runtime.native_tool_host import NativeReadToolHost
from agentloom.runtime.native_tools import NativeCallIdentity, NativePrepareRequest, ToolManifestEntry
from agentloom.runtime.trace import ExplicitExecutionContext, bind_explicit_execution_context


def read_manifest(**changes):
    return replace(
        ToolManifestEntry(
            logical_name="read_file",
            visible_name="native_read",
            owner="runtime",
            provider="external-test-reader",
            capability="file.read",
            operation="read",
            parameters={
                "type": "object",
                "properties": {"path": {"type": "string"}},
                "required": ["path"],
                "additionalProperties": False,
            },
            path_parameters=("path",),
        ),
        **changes,
    )


@contextmanager
def native_scope(tmp_path, *, handlers=(), manifest=None, instance="worker-1", extractors=None, extra_tools=(), config_extra=None):
    workspace = tmp_path / "workspace"
    workspace.mkdir(exist_ok=True)
    runtime = RuntimeContext(tmp_path / "runtime", "native-test", "task-1", "run-1")
    config = {
        "tool_access_control": {
            "path_validation": [{"tools": ["read_file"], "exclude_paths": [str(workspace / "denied.txt")]}]
        }
    }
    config.update(config_extra or {})
    run = HookRun(
        HookPlan(tuple(handlers)),
        local_run_id="local-1",
        root_run_id="root-1",
        agent_config=config,
        project_root=str(workspace),
    )
    execution = ExplicitExecutionContext(
        task_id=runtime.task_id,
        sub_task_id=None,
        agent_id=instance,
        agent_name="worker",
        agent_config=config,
        skill_catalog=None,
        hook_run=run,
        runtime_agent_path="worker",
        root_run_id=run.root_run_id,
        local_run_id=run.local_run_id,
    )
    tool = manifest or read_manifest()
    with bind_run_context(runtime), bind_explicit_execution_context(execution):
        with NativeReadToolHost(tools=(tool, *extra_tools), cwd=str(workspace), evidence_extractors=extractors) as host:
            identity = NativeCallIdentity(runtime.application_id, runtime.task_id, runtime.run_id, instance, "call-1")
            yield host, NativePrepareRequest(identity, tool, str(workspace), {"path": "source.txt"}), run


def test_transform_strict_decode_authorize_and_consume_once(tmp_path):
    seen = []

    def transform(context):
        seen.append((context.tool_input, context.cwd))
        return HookResult(decision="modify", modified_input={"path": "allowed.txt"})

    with native_scope(tmp_path, handlers=[HookHandler(HookEvent.PRE_TOOL_USE, "native_read", transform)]) as (
        host,
        request,
        run,
    ):
        (Path(request.cwd) / "allowed.txt").write_text("independent oracle")
        request = replace(request, raw_arguments={"path": 7})
        grant = host.prepare(request).authorization
        assert grant is not None
        assert dict(grant.final_arguments) == {"path": "allowed.txt"}
        assert seen == [({"path": 7}, request.cwd)]
        delivered = host.start_execution(grant)
        assert delivered == grant
        assert host.inspect(request.identity).state == "executing"
        assert (Path(delivered.cwd) / delivered.final_arguments["path"]).read_text() == "independent oracle"
        with pytest.raises(ValueError, match="consumed"):
            host.start_execution(grant)


@pytest.mark.parametrize(
    "arguments,stage",
    [
        ({"path": "denied.txt"}, "core_tool_guard"),
        ({"path": "../outside.txt"}, "core_tool_guard"),
        ({"path": 7}, "final_decode"),
        ({"path": "source.txt", "hidden": "denied.txt"}, "final_decode"),
    ],
)
def test_rejection_never_authorizes_an_executor(tmp_path, arguments, stage):
    with native_scope(tmp_path) as (host, request, run):
        rejected = host.prepare(replace(request, raw_arguments=arguments))
        assert rejected.authorization is None
        assert (rejected.rejection.status, rejected.rejection.stage) == ("blocked", stage)
        assert run.tool_outcomes_snapshot()[0].call_id == "call-1"
        with pytest.raises(ValueError, match="already prepared"):
            host.prepare(request)


@pytest.mark.parametrize(
    "field,value",
    [
        ("authorization_id", "forged"),
        ("cwd", "/"),
        ("final_arguments", {"path": "denied.txt"}),
        ("identity", NativeCallIdentity("native-test", "task-1", "run-2", "worker-1", "call-1")),
        ("identity", NativeCallIdentity("native-test", "task-1", "run-1", "worker-2", "call-1")),
    ],
)
def test_execution_gate_checks_stored_grant_not_caller_copy(tmp_path, field, value):
    with native_scope(tmp_path) as (host, request, _):
        grant = host.prepare(request).authorization
        with pytest.raises(ValueError, match="mismatch"):
            host.start_execution(replace(grant, **{field: value}))
        assert host.inspect(request.identity).state == "authorized"
        assert host.start_execution(grant) == grant


def test_read_is_durable_before_success_and_duplicate_settlement_is_idempotent(tmp_path):
    from agentloom.runtime.native_tools import NativeExecutionOutcome

    observations = []

    def after(context):
        observations.append(context.tool_response)
        # Deliberately failing observers cannot control commit acknowledgement.
        raise RuntimeError("observer unavailable")

    with native_scope(tmp_path, handlers=[HookHandler(HookEvent.POST_TOOL_USE, "*", after)]) as (host, request, _):
        (Path(request.cwd) / "source.txt").write_text("native receipt: 42\n")
        grant = host.start_execution(host.prepare(request).authorization)
        output = (Path(grant.cwd) / grant.final_arguments["path"]).read_text()
        outcome = NativeExecutionOutcome(grant.identity, grant.authorization_id, "completed", output)
        ack = host.settle(outcome)
        # Reopen production storage through a fresh host, not an observer trace.
        with NativeReadToolHost(tools=(request.tool,), cwd=request.cwd) as reader:
            receipt = reader.receipt(request.identity)
            assert reader.inspect(request.identity).commit == ack
            assert receipt["raw_output"] == "native receipt: 42\n"
            assert receipt["request"]["raw_arguments"] == {"path": "source.txt"}
            assert receipt["request"]["tool"]["provider"] == "external-test-reader"
        assert host.settle(outcome) == ack
        assert len(observations) == 1
        with pytest.raises(ValueError, match="settlement"):
            host.settle(replace(outcome, output="forged replacement"))
        assert host.inspect(request.identity).commit == ack


@pytest.mark.parametrize("started,expected", [(False, "cancelled"), (True, "uncertain")])
def test_cancel_and_late_settlement_do_not_invent_a_terminal_result(tmp_path, started, expected):
    from agentloom.runtime.native_tools import NativeExecutionOutcome

    with native_scope(tmp_path) as (host, request, _):
        grant = host.prepare(request).authorization
        if started:
            host.start_execution(grant)
        assert host.cancel(request.identity).state == expected
        late = host.settle(NativeExecutionOutcome(grant.identity, grant.authorization_id, "completed", "too late"))
        assert late.state == expected and late.commit is None
        with pytest.raises(ValueError, match="consumed"):
            host.start_execution(grant)


@pytest.mark.parametrize("status", ["error", "uncertain"])
def test_executor_error_and_unknown_result_have_distinct_states(tmp_path, status):
    from agentloom.runtime.native_tools import NativeExecutionOutcome
    from agentloom.runtime.tool_protocol import ToolErrorRecord

    with native_scope(tmp_path) as (host, request, _):
        grant = host.start_execution(host.prepare(request).authorization)
        error = ToolErrorRecord("external_error", "reader disconnected", False, "native_executor")
        result = host.settle(NativeExecutionOutcome(grant.identity, grant.authorization_id, status, error=error))
        if status == "error":
            assert result.record.status == "error" and result.record.output is None
        else:
            assert result.state == "uncertain" and result.commit is None
        assert host.inspect(request.identity).recovery_action == (
            "replay_result" if status == "error" else "no_reexecute"
        )


@pytest.mark.parametrize(
    "text,scope,expected",
    [
        ("actual fact", "project", "verified"),
        ("forged fact", "project", "rejected"),
        ("actual fact", "unknown", "rejected"),
    ],
)
def test_only_registered_output_bound_memory_evidence_is_accepted(tmp_path, text, scope, expected):
    from agentloom.runtime.native_tools import NativeExecutionOutcome
    from agentloom.runtime.trusted_memory_evidence import (
        TRUSTED_MEMORY_EVIDENCE_RESPONSE_KEY,
        TrustedMemoryEvidenceEnvelope,
    )

    observed = []
    extractors = {
        "native_read": lambda output: [{"kind": "durable_fact", "scope": scope, "source": "fixture-file", "text": text}]
    }
    handler = HookHandler(HookEvent.POST_TOOL_USE, "*", lambda context: observed.append(context.tool_response))
    with native_scope(tmp_path, handlers=[handler], extractors=extractors) as (host, request, _):
        grant = host.start_execution(host.prepare(request).authorization)
        host.settle(NativeExecutionOutcome(grant.identity, grant.authorization_id, "completed", "actual fact"))
        receipt = host.receipt(request.identity)
        assert receipt["evidence_status"] == expected
        if expected == "verified":
            assert receipt["evidence"][0]["text"] == "actual fact"
            assert isinstance(observed[0][TRUSTED_MEMORY_EVIDENCE_RESPONSE_KEY], TrustedMemoryEvidenceEnvelope)
        else:
            assert receipt["evidence"] == []
            assert TRUSTED_MEMORY_EVIDENCE_RESPONSE_KEY not in observed[0]


def test_json_evidence_claims_from_executor_are_not_trusted(tmp_path):
    from agentloom.runtime.native_tools import NativeExecutionOutcome

    with native_scope(tmp_path) as (host, request, _):
        grant = host.start_execution(host.prepare(request).authorization)
        payload = {"_agentloom_trusted_memory_evidence_v1": [{"text": "forged"}]}
        host.settle(NativeExecutionOutcome(grant.identity, grant.authorization_id, "completed", payload))
        assert host.receipt(request.identity)["evidence"] == []


def test_disk_failure_never_acknowledges_success(tmp_path, monkeypatch):
    from agentloom.runtime.native_tools import NativeExecutionOutcome
    from agentloom.runtime.storage import SecureDirectory

    with native_scope(tmp_path) as (host, request, _):
        grant = host.start_execution(host.prepare(request).authorization)
        with monkeypatch.context() as failure:
            failure.setattr(
                SecureDirectory, "atomic_write_json", lambda *a, **kw: (_ for _ in ()).throw(OSError("disk full"))
            )
            with pytest.raises(OSError, match="disk full"):
                host.settle(NativeExecutionOutcome(grant.identity, grant.authorization_id, "completed", "actual read"))
        assert host.inspect(request.identity).state == "executing"


def test_two_hosts_cannot_consume_the_same_authorization(tmp_path):
    from concurrent.futures import ThreadPoolExecutor

    from agentloom.runtime import get_current_run_context
    from agentloom.runtime.trace import capture_explicit_execution_context

    with native_scope(tmp_path) as (host, request, _):
        grant = host.prepare(request).authorization
        context, execution = get_current_run_context(), capture_explicit_execution_context()

        def consume():
            with bind_run_context(context), bind_explicit_execution_context(execution):
                with NativeReadToolHost(tools=(request.tool,), cwd=request.cwd) as other:
                    try:
                        other.start_execution(grant)
                        return "executed"
                    except ValueError:
                        return "rejected"

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda _: consume(), range(2)))
        assert sorted(results) == ["executed", "rejected"]


def test_logical_tool_hook_cannot_be_bypassed_by_native_visible_name(tmp_path):
    calls = []
    handlers = [
        HookHandler(HookEvent.PRE_TOOL_USE, "*", lambda context: calls.append("wildcard")),
        HookHandler(
            HookEvent.PRE_TOOL_USE, "read_file", lambda context: HookResult(decision="block", reason="logical rule")
        ),
    ]
    with native_scope(tmp_path, handlers=handlers) as (host, request, _):
        assert host.prepare(request).rejection.status == "blocked"
        assert calls == ["wildcard"]


def test_a_host_with_different_selected_provider_cannot_consume_old_grant(tmp_path):
    with native_scope(tmp_path) as (host, request, _):
        grant = host.prepare(request).authorization
        changed = replace(request.tool, provider="unselected-provider")
        with NativeReadToolHost(tools=(changed,), cwd=request.cwd) as other:
            with pytest.raises(ValueError, match="selection"):
                other.start_execution(grant)
        assert host.inspect(request.identity).state == "authorized"


def test_evidence_extractor_cannot_mutate_the_original_receipt(tmp_path):
    from agentloom.runtime.native_tools import NativeExecutionOutcome

    def extract(output):
        output["text"] = "injected claim"
        return [{"kind": "durable_fact", "scope": "project", "source": "reader", "text": "injected claim"}]

    with native_scope(tmp_path, extractors={"native_read": extract}) as (host, request, _):
        grant = host.start_execution(host.prepare(request).authorization)
        ack = host.settle(
            NativeExecutionOutcome(grant.identity, grant.authorization_id, "completed", {"text": "original"})
        )
        receipt = host.receipt(request.identity)
        assert receipt["evidence_status"] == "rejected"
        assert receipt["evidence"] == []
        assert receipt["raw_output"] == ack.record.output == {"text": "original"}


def test_nested_closed_schema_rejects_undeclared_options(tmp_path):
    tool = read_manifest(
        parameters={
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "options": {"type": "object", "additionalProperties": False},
            },
            "required": ["path"],
            "additionalProperties": False,
        }
    )
    with native_scope(tmp_path, manifest=tool) as (host, request, _):
        result = host.prepare(replace(request, raw_arguments={"path": "source.txt", "options": {"undeclared": True}}))
        assert result.rejection.status == "blocked"


@pytest.mark.parametrize("syntax", ["uri", "tilde"])
def test_executor_path_interpretation_cannot_differ_from_the_guard(tmp_path, syntax):
    with native_scope(tmp_path) as (host, request, _):
        cwd = Path(request.cwd)
        (cwd / "denied.txt").mkdir()
        if syntax == "uri":
            (cwd / "file:").symlink_to(cwd / "denied.txt", target_is_directory=True)
            path = "file://" + str(cwd / "source.txt")
        else:
            (cwd / "~").symlink_to(cwd / "denied.txt", target_is_directory=True)
            path = "~/source.txt"
        literal_target = cwd / path
        literal_target.parent.mkdir(parents=True, exist_ok=True)
        literal_target.write_text("MUST_NOT_READ")
        result = host.prepare(replace(request, raw_arguments={"path": path}))
        assert result.authorization is None
        assert result.rejection.stage == "final_decode"


def test_new_journal_directory_is_durable_before_authorization(tmp_path, monkeypatch):
    import os
    import stat

    synced = set()
    real_fsync = os.fsync

    def fsync(fd):
        info = os.fstat(fd)
        if stat.S_ISDIR(info.st_mode):
            synced.add((info.st_dev, info.st_ino))
        real_fsync(fd)

    monkeypatch.setattr(os, "fsync", fsync)
    with native_scope(tmp_path) as (host, request, _):
        assert host.prepare(request).authorization is not None
        for directory in (host.journal_directory, host.journal_directory.parent, tmp_path / "runtime"):
            info = directory.stat()
            assert (info.st_dev, info.st_ino) in synced


def test_another_hook_run_cannot_reuse_the_same_application_call(tmp_path):
    from agentloom.runtime.trace import capture_explicit_execution_context

    with native_scope(tmp_path) as (host, request, run):
        grant = host.prepare(request).authorization
        other_run = HookRun(
            run.plan,
            local_run_id="another-invocation",
            root_run_id=run.root_run_id,
            agent_config=run.agent_config,
            project_root=run.project_root,
        )
        execution = replace(
            capture_explicit_execution_context(), hook_run=other_run, local_run_id=other_run.local_run_id
        )
        with bind_explicit_execution_context(execution):
            with pytest.raises(ValueError, match="scope"):
                host.start_execution(grant)
            with NativeReadToolHost(tools=(request.tool,), cwd=request.cwd) as other:
                with pytest.raises(ValueError, match="identity"):
                    other.start_execution(grant)
