"""Real filesystem/subprocess executors at the production native host seam."""
from dataclasses import replace
from pathlib import Path

from agentloom.runtime.native_tools import NativeExecutionOutcome
from tests.lib_test.runtime.test_native_read_host import native_scope, read_manifest


def write_manifest():
    return read_manifest(logical_name="write_file", visible_name="native_write", capability="file.write", operation="write", parameters={
        "type": "object", "properties": {"path": {"type": "string"}, "content": {"type": "string"}},
        "required": ["path", "content"], "additionalProperties": False,
    })


def test_native_create_writes_only_the_consumed_final_arguments(tmp_path):
    with native_scope(tmp_path, manifest=write_manifest()) as (host, request, _):
        request = replace(request, raw_arguments={"path": "new.txt", "content": "oracle"})
        grant = host.prepare(request).authorization
        assert grant is not None
        assert not (Path(request.cwd) / "new.txt").exists()
        approved = host.start_execution(grant)
        (Path(approved.cwd) / approved.final_arguments["path"]).write_text(approved.final_arguments["content"])
        ack = host.settle(NativeExecutionOutcome(grant.identity, grant.authorization_id, "completed", "written"))
        assert ack.record.status == "completed"
        assert (Path(request.cwd) / "new.txt").read_text() == "oracle"


def test_native_overwrite_without_read_is_blocked_before_dispatch(tmp_path):
    with native_scope(tmp_path, manifest=write_manifest()) as (host, request, _):
        target = Path(request.cwd) / "source.txt"
        target.write_text("preserve")
        result = host.prepare(replace(request, raw_arguments={"path": "source.txt", "content": "forbidden"}))
        assert result.authorization is None and result.rejection.status == "blocked"
        assert "not been read" in result.rejection.reason
        assert target.read_text() == "preserve"


import subprocess
import pytest
from agentloom.runtime.hooks import HookEvent, HookHandler, HookResult
from agentloom.runtime.tool_protocol import ToolPolicyBlockedError


def read_then_write(host, request):
    reader = read_manifest()
    read_request = replace(request, tool=reader, raw_arguments={"path": "source.txt"}, identity=replace(request.identity, call_id="read-1"))
    grant = host.start_execution(host.prepare(read_request).authorization)
    raw = (Path(grant.cwd) / grant.final_arguments["path"]).read_text()
    host.settle(NativeExecutionOutcome(grant.identity, grant.authorization_id, "completed", raw))
    return replace(request, raw_arguments={"path": "source.txt", "content": "updated"})


def test_native_backup_precedes_overwrite_and_retains_original_bytes(tmp_path):
    with native_scope(tmp_path, manifest=write_manifest(), extra_tools=(read_manifest(),)) as (host, request, _):
        target = Path(request.cwd) / "source.txt"
        target.write_text("original oracle")
        request = read_then_write(host, request)
        grant = host.prepare(request).authorization
        assert grant is not None
        backups = list((tmp_path / "runtime").rglob("*@v1"))
        assert any(p.read_text() == "original oracle" for p in backups)
        assert target.read_text() == "original oracle"
        grant = host.start_execution(grant)
        target.write_text(grant.final_arguments["content"])
        host.settle(NativeExecutionOutcome(grant.identity, grant.authorization_id, "completed", "written"))
        assert any(p.read_text() == "original oracle" for p in backups)


@pytest.mark.parametrize("when", ["before_prepare", "before_dispatch"])
def test_native_changed_file_never_overwrites_unseen_content(tmp_path, when):
    with native_scope(tmp_path, manifest=write_manifest(), extra_tools=(read_manifest(),)) as (host, request, _):
        target = Path(request.cwd) / "source.txt"
        target.write_text("original")
        request = read_then_write(host, request)
        if when == "before_prepare":
            target.write_text("external edit")
            assert host.prepare(request).rejection.status == "blocked"
        else:
            grant = host.prepare(request).authorization
            target.write_text("external edit")
            with pytest.raises(ToolPolicyBlockedError):
                host.start_execution(grant)
            assert host.inspect(request.identity).state == "cancelled"
        assert target.read_text() == "external edit"


def test_native_write_logical_rule_blocks_transformed_actual_path(tmp_path):
    handler = HookHandler(HookEvent.PRE_TOOL_USE, "write_file", lambda _: HookResult(decision="modify", modified_input={"path": "denied.txt", "content": "forbidden"}))
    with native_scope(tmp_path, manifest=write_manifest(), handlers=(handler,), config_extra={"tool_access_control": {"path_validation": [{"tools": ["write_file"], "exclude_paths": ["denied.txt"]}]}}) as (host, request, _):
        result = host.prepare(replace(request, raw_arguments={"path": "new.txt", "content": "allowed"}))
        assert result.authorization is None and result.rejection.stage == "core_tool_guard"
        assert not (Path(request.cwd) / "denied.txt").exists()
        assert not (Path(request.cwd) / "new.txt").exists()


def shell_manifest():
    return read_manifest(logical_name="shell_tool", visible_name="bash", capability="shell.execute", operation="shell", path_parameters=(), command_parameter="command", parameters={"type": "object", "properties": {"command": {"type": "string"}}, "required": ["command"], "additionalProperties": False})


def test_native_shell_command_policy_prevents_real_side_effect(tmp_path):
    with native_scope(tmp_path, manifest=shell_manifest(), config_extra={"shell_settings": {"allowed_commands": ["printf"], "allowed_operators": ["*"], "sandbox": {"enabled": False}}}) as (host, request, _):
        command = "touch forbidden.txt"
        result = host.prepare(replace(request, raw_arguments={"command": command}))
        assert result.rejection.status == "blocked" and "not allowed" in result.rejection.reason
        assert not (Path(request.cwd) / "forbidden.txt").exists()


def test_native_shell_retains_first_execution_raw_result(tmp_path):
    with native_scope(tmp_path, manifest=shell_manifest(), config_extra={"shell_settings": {"allowed_commands": ["printf"], "allowed_operators": ["*"], "sandbox": {"enabled": False}}}) as (host, request, _):
        grant = host.start_execution(host.prepare(replace(request, raw_arguments={"command": "printf original_receipt"})).authorization)
        result = subprocess.run(["/bin/sh", "-c", grant.final_arguments["command"]], cwd=grant.cwd, check=True, capture_output=True, text=True)
        ack = host.settle(NativeExecutionOutcome(grant.identity, grant.authorization_id, "completed", result.stdout))
        assert ack.record.output == "original_receipt"
        receipt = host.receipt(request.identity)
        assert receipt["raw_output"] == "original_receipt"
        assert receipt["result_scope"]["coverage"] == "executor_result_only"
        assert receipt["result_scope"]["source_completeness"] == "unknown"
        assert receipt["result_scope"]["display_truncated"] is False
        with pytest.raises(ValueError, match="consumed"):
            host.start_execution(grant)


@pytest.mark.parametrize("policy", ["sandbox", "query_exclusions"])
def test_unverified_native_execution_constraints_fail_closed(tmp_path, policy):
    config = {"shell_settings": {"allowed_commands": ["printf"], "allowed_operators": ["*"], "sandbox": {"enabled": policy == "sandbox", "mode": "bwrap"}}}
    if policy == "query_exclusions":
        config["tool_access_control"] = {"path_validation": [{"tools": ["shell_tool"], "exclude_paths": ["secrets"]}]}
    with native_scope(tmp_path, manifest=shell_manifest(), config_extra=config) as (host, request, _):
        result = host.prepare(replace(request, raw_arguments={"command": "printf forbidden > artifact.txt"}))
        assert result.rejection.status == "blocked"
        assert not (Path(request.cwd) / "artifact.txt").exists()


def test_backup_storage_failure_blocks_native_overwrite(tmp_path):
    with native_scope(tmp_path, manifest=write_manifest(), extra_tools=(read_manifest(),)) as (host, request, _):
        target = Path(request.cwd) / "source.txt"
        target.write_text("original oracle")
        request = read_then_write(host, request)
        history_root = next((tmp_path / "runtime").rglob("native-file-history"))
        for directory in history_root.iterdir():
            directory.rmdir()  # Real filesystem failure at the backup sink.
        prepared = host.prepare(request)
        assert prepared.authorization is None and prepared.rejection.status == "blocked"
        assert target.read_text() == "original oracle"


def test_backup_index_failure_stays_blocked_on_new_call(tmp_path):
    with native_scope(tmp_path, manifest=write_manifest(), extra_tools=(read_manifest(),)) as (host, request, _):
        target = Path(request.cwd) / "source.txt"
        target.write_text("original oracle")
        request = read_then_write(host, request)
        history_root = next((tmp_path / "runtime").rglob("native-file-history"))
        for directory in history_root.iterdir():
            (directory / "snapshots.json").mkdir()  # Copy succeeds; index replace fails.
        for number in range(2):
            prepared = host.prepare(replace(request, identity=replace(request.identity, call_id=f"write-{number}")))
            assert prepared.authorization is None and prepared.rejection.status == "blocked"
        assert target.read_text() == "original oracle"

@pytest.mark.parametrize("wrapper", ["env", "timeout 30", "nice -n 5", "xargs", "command", "sudo", "sh -c"])
def test_shell_query_wrapper_cannot_hide_excluded_search(tmp_path, wrapper):
    config = {"shell_settings": {"allowed_commands": ["*"], "allowed_operators": ["*"], "sandbox": {"enabled": False}}, "tool_access_control": {"path_validation": [{"tools": ["grep_search"], "exclude_paths": ["secrets"]}]}}
    with native_scope(tmp_path, manifest=shell_manifest(), config_extra=config) as (host, request, _):
        result = host.prepare(replace(request, raw_arguments={"command": f"printf x; {wrapper} grep -r SECRET ."}))
        assert result.authorization is None and result.rejection.status == "blocked"


def test_query_limit_is_not_display_truncation_and_artifact_is_original(tmp_path):
    import json
    reader = read_manifest(parameters={"type": "object", "properties": {"path": {"type": "string"}, "limit": {"type": "integer"}}, "required": ["path", "limit"], "additionalProperties": False})
    with native_scope(tmp_path, manifest=reader) as (host, request, _):
        target = Path(request.cwd) / "source.txt"
        target.write_text("first\nsecond\nthird\n")
        grant = host.start_execution(host.prepare(replace(request, raw_arguments={"path": "source.txt", "limit": 1})).authorization)
        raw = "".join(target.read_text().splitlines(keepends=True)[:grant.final_arguments["limit"]])
        target.write_text("later content must never become original evidence")
        ack = host.settle(NativeExecutionOutcome(grant.identity, grant.authorization_id, "completed", raw))
        scope = host.receipt(grant.identity)["result_scope"]
        assert scope["query_limits"] == {"limit": 1}
        assert scope["display_truncated"] is False
        assert scope == ack.record.metadata["native"]["result_scope"]
        artifact = json.loads(Path(scope["raw_artifact"]["path"]).read_text())
        assert artifact["raw_output"] == "first\n"
        assert scope["raw_artifact"]["json_pointer"] == "/raw_output"

@pytest.mark.parametrize("excluded,command", [("/", "cat hidden.txt"), (".", "cat hidden.txt"), ("secrets", "cat {secrets,public}/hidden.txt"), ("secrets", "echo {secrets,public}/*")])
def test_literal_shell_scope_rejects_root_exclusions_and_expansion(tmp_path, excluded, command):
    config = {"shell_settings": {"allowed_commands": ["*"], "allowed_operators": ["*"], "sandbox": {"enabled": False}}, "tool_access_control": {"path_validation": [{"tools": ["grep_search"], "exclude_paths": [excluded]}]}}
    with native_scope(tmp_path, manifest=shell_manifest(), config_extra=config) as (host, request, _):
        result = host.prepare(replace(request, raw_arguments={"command": command}))
        assert result.authorization is None and result.rejection.status == "blocked"
