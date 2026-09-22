"""Published Pi executors through execute_app, with an HTTP-only model fixture."""
import json
import pytest

from agentloom.application.runner import execute_app
from agentloom.configuration.config import bind_config, load_project_config
from tests.pi_test.test_application import model_service, project
from tests.pi_test.test_tools_application import select
from tests.pi_test.test_governance_application import audit


def test_official_write_creates_selected_file_and_durable_receipt(tmp_path):
    with model_service(turns=[[("create", "write", {"path": "new/note.txt", "content": "saffron-write-1031\n"})]]) as (url, requests):
        app = project(tmp_path, url)
        select(app, tools=[{"name": "write"}])
        with bind_config(load_project_config(tmp_path)):
            result = execute_app(app, file_logging=False)
    assert (tmp_path / "new/note.txt").read_text() == "saffron-write-1031\n"
    records = [e["details"] for e in audit(result) if e["kind"] == "tool"]
    assert len(records) == 1
    record = records[0]["record"]
    assert (records[0]["owner"], records[0]["provider"], record["status"]) == ("runtime", "pi", "completed")
    assert record["metadata"]["native"]["logical_name"] == "write_file"
    assert record["input"] == {"path": "new/note.txt", "content": "saffron-write-1031\n"}
    assert [t["function"]["name"] for t in requests[0][1]["tools"]] == ["write"]
    assert "Successfully wrote" in json.dumps(requests[1][1])


def test_official_edit_preserves_original_in_history_after_confirmed_read(tmp_path):
    original = "version = 1031\nkept = True\n"
    source = tmp_path / "settings.py"
    source.write_text(original)
    with model_service(turns=[[("observe", "read", {"path": "settings.py"})],
                             [("change", "edit", {"path": "settings.py", "edits": [{"oldText": "version = 1031", "newText": "version = 2042"}]})]]) as (url, requests):
        app = project(tmp_path, url)
        select(app, tools=[{"name": name} for name in ("read", "edit")])
        with bind_config(load_project_config(tmp_path)):
            result = execute_app(app, file_logging=False)
    assert source.read_text() == "version = 2042\nkept = True\n"
    records = {e["details"]["record"]["call_id"]: e["details"]["record"] for e in audit(result) if e["kind"] == "tool"}
    assert records["change"]["status"] == "completed"
    assert records["change"]["metadata"]["native"]["logical_name"] == "edit_file"
    assert any(p.read_bytes() == original.encode() for p in (result.run.run_dir / "native-file-history").rglob("*") if p.is_file())
    assert "version = 2042" in json.dumps(requests[-1][1])


def test_official_bash_executes_allowed_command_with_run_receipt(tmp_path):
    with model_service(turns=[[("command", "bash", {"command": "printf 'shell-proof-1031'", "timeout": 5})]]) as (url, requests):
        app = project(tmp_path, url)
        select(app, tools=[{"name": "bash"}], shell_settings={"allowed_commands": ["printf"], "sandbox": {"enabled": False}})
        with bind_config(load_project_config(tmp_path)):
            result = execute_app(app, file_logging=False)
    records = [e["details"]["record"] for e in audit(result) if e["kind"] == "tool"]
    assert len(records) == 1
    assert records[0]["status"] == "completed"
    assert records[0]["metadata"]["native"]["logical_name"] == "shell_tool"
    assert "shell-proof-1031" in json.dumps(records[0]["output"])
    assert "shell-proof-1031" in json.dumps(requests[-1][1])


def test_native_write_rechecks_file_after_preparation_before_sdk_execution(tmp_path, monkeypatch):
    import os
    import sys
    from tests.pi_test.test_process_lifecycle import sdk_node

    source = tmp_path / "shared.txt"
    source.write_text("initial-version-1031")
    # Independently change the real file after authorization, just before the
    # genuine SDK's dispatch request reaches the host. Tool batch ordering is
    # not an oracle for this race: platform execution now follows persistence.
    binary = sdk_node()
    launcher = tmp_path / 'bin/node'
    launcher.parent.mkdir()
    launcher.write_text(f'''#!{sys.executable}
import json,os,subprocess,sys,threading
from pathlib import Path
if sys.argv[1:]==['--version'] or sys.argv[1:3]==['-p','process.versions.modules']:os.execv({binary!r},[{binary!r},*sys.argv[1:]])
p=subprocess.Popen([{binary!r},*sys.argv[1:]],stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.DEVNULL)
def forward():
 try:
  for line in sys.stdin.buffer:p.stdin.write(line);p.stdin.flush()
 except BrokenPipeError:pass
threading.Thread(target=forward,daemon=True).start()
for line in p.stdout:
 value=json.loads(line);payload=value.get('payload') or {{}}
 if value.get('kind')=='request' and payload.get('method')=='tool_dispatch' and payload['authorization']['tool']['visible_name']=='write':
  Path({str(source)!r}).write_text('external-version-3093')
 sys.stdout.buffer.write(line);sys.stdout.buffer.flush()
p.wait();sys.exit(p.returncode)
''')
    launcher.chmod(0o755)
    monkeypatch.setenv('PATH', str(launcher.parent) + os.pathsep + os.environ['PATH'])
    with model_service(turns=[[("observe", "read", {"path": str(source)})],
                             [("overwrite", "write", {"path": str(source), "content": "must-not-overwrite"})]]) as (url, requests):
        app = project(tmp_path, url)
        select(app, tools=[{"name": "read"}, {"name": "write"}])
        with bind_config(load_project_config(tmp_path)):
            result = execute_app(app, file_logging=False)
    assert source.read_text() == "external-version-3093"
    records = {e["details"]["record"]["call_id"]: e["details"]["record"] for e in audit(result) if e["kind"] == "tool"}
    assert records["overwrite"]["status"] == "blocked"
    assert records["overwrite"]["error"]["stage"] == "native_dispatch"


def test_sdk_read_truncation_keeps_only_original_selected_range_retrievable(tmp_path):
    import re
    source = tmp_path / "large.txt"
    lines = [f"record-{i:04d}" for i in range(5500)]
    lines[3800] = "TARGET_RECORD: original-saffron-1031"
    lines[5300] = "OUTSIDE_QUERY_SECRET"
    source.write_text("\n".join(lines))
    def retrieve(request):
        content = next(m["content"] for m in request["messages"] if m["role"] == "tool")
        match = re.search(r"\[ContextRef (ctx_[a-zA-Z0-9]+)", content)
        assert match is not None, "SDK display truncation lost its original query artifact"
        source.write_text("changed after initial read")
        return [("retrieve", "loom_retrieve_context", {"ref": match.group(1), "query": "TARGET_RECORD", "offset": 0, "limit": 5})]
    with model_service(turns=[[("bounded-read", "read", {"path": str(source), "offset": 1001, "limit": 3000})], retrieve]) as (url, requests):
        app = project(tmp_path, url)
        select(app, tools=[{"name": "read"}, {"name": "loom_retrieve_context"}], context_engine={"min_chars": 1000, "preview_max_chars": 300})
        with bind_config(load_project_config(tmp_path)):
            result = execute_app(app, file_logging=False)
    records = {e["details"]["record"]["call_id"]: e["details"]["record"] for e in audit(result) if e["kind"] == "tool"}
    assert "original-saffron-1031" in records["retrieve"]["output"]
    assert "OUTSIDE_QUERY_SECRET" not in json.dumps(records)
    scope = records["bounded-read"]["metadata"]["native"]["result_scope"]
    assert scope["source_completeness"] == "complete"
    assert scope["coverage"] == "complete_query"
    assert scope["query_limits"] == {"offset": 1001, "limit": 3000}
    assert scope["display_truncated"] is True
    raw = json.loads(__import__("pathlib").Path(scope["raw_artifact"]["path"]).read_text())["raw_output"]
    assert raw == "\n".join(lines[1000:4000])


@pytest.mark.parametrize("fault", ["keyboard", "sdk_death"])
def test_cancelled_native_bash_reaps_managed_detached_descendants(tmp_path, fault):
    import os
    import signal
    import shlex
    import sys
    import psutil
    from tests.pi_test.test_process_lifecycle import node_launcher, start_cli, until
    marker = tmp_path / "shell-processes.json"
    script = tmp_path / "spawn.py"
    script.write_text("import os,sys,json,subprocess,time\nfrom pathlib import Path\n"
        "p=subprocess.Popen([sys.executable,'-c','import time;time.sleep(60)'],start_new_session=True,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)\n"
        f"Path({str(marker)!r}).write_text(json.dumps([os.getpid(),p.pid]))\ntime.sleep(60)\n")
    command = f"{shlex.quote(sys.executable)} {shlex.quote(str(script))}"
    with model_service(turns=[[("long-bash", "bash", {"command": command})]]) as (url, requests):
        app = project(tmp_path, url)
        select(app, tools=[{"name": "bash"}], shell_settings={"allowed_commands": ["*"], "sandbox": {"enabled": False}})
        env, node_pid = node_launcher(tmp_path)
        child = start_cli(tmp_path, app, env)
        tracked = []
        try:
            until(marker.exists)
            tracked = json.loads(marker.read_text())
            os.kill(child.pid if fault == "keyboard" else int(node_pid.read_text()), signal.SIGINT if fault == "keyboard" else signal.SIGKILL)
            stdout, stderr = child.communicate(timeout=12)
            alive = [pid for pid in tracked if psutil.pid_exists(pid) and psutil.Process(pid).status() != psutil.STATUS_ZOMBIE]
        finally:
            if child.poll() is None: child.kill(); child.wait()
            for pid in tracked:
                try: os.kill(pid, signal.SIGKILL)
                except ProcessLookupError: pass
        assert child.returncode != 0
        assert alive == [], "Native Shell descendants outlived their owning Application"
        events = [json.loads(line) for line in stdout.splitlines()]
        assert events[-1]["event"] in {"run.interrupted", "run.failed"}


def test_bash_without_an_exit_code_cannot_become_successful_evidence(tmp_path):
    from agentloom.application.run import ApplicationRunError
    with model_service(turns=[[("killed-shell", "bash", {"command": "kill -KILL $$"})]]) as (url, requests):
        app = project(tmp_path, url)
        select(app, tools=[{"name": "bash"}], shell_settings={"allowed_commands": ["*"], "allowed_operators": ["*"], "sandbox": {"enabled": False}})
        with bind_config(load_project_config(tmp_path)), pytest.raises(ApplicationRunError):
            execute_app(app, file_logging=False)
    entries = [json.loads(p.read_text()) for p in tmp_path.rglob("native-tools/**/*.json")]
    assert len(entries) == 1
    assert entries[0]["state"] == "uncertain"
    assert "record" not in entries[0]
    assert len(requests) == 1


@pytest.mark.parametrize("scenario", ["unread", "excluded", "stale", "command_denied", "sandbox_required", "query_excluded"])
def test_native_mutations_respect_existing_platform_policy(tmp_path, scenario):
    source = tmp_path / "protected.txt"
    source.write_text("original-protected-1031")
    turns = [[("attempt", "write", {"path": str(source), "content": "forbidden-change"})]]
    config = {"tools": [{"name": "read"}, {"name": "write"}]}
    if scenario == "excluded":
        config["tool_access_control"] = {"path_validation": [{"tools": ["write_file"], "exclude_paths": [str(source)]}]}
    if scenario == "stale":
        turns.insert(0, [("observe", "read", {"path": str(source)})])
        config["hooks"] = {"PreToolUse": [{"id": "external-change", "matcher": "write_file", "command": f"printf 'external-protected-1031' > {source}"}]}
    if scenario.startswith("command") or scenario in {"sandbox_required", "query_excluded"}:
        config["tools"] = [{"name": "bash"}]
        config["shell_settings"] = {"allowed_commands": ["printf", "cat"], "allowed_operators": ["*"],
            "sandbox": {"enabled": scenario == "sandbox_required", "mode": "bwrap"}}
        command = "touch forbidden.txt" if scenario == "command_denied" else "printf forbidden > forbidden.txt"
        if scenario == "query_excluded":
            command = "cat protected.txt"
            config["tool_access_control"] = {"path_validation": [{"tools": ["shell_tool"], "exclude_paths": [str(source)]}]}
        turns = [[("attempt", "bash", {"command": command})]]
    with model_service(turns=turns) as (url, requests):
        app = project(tmp_path, url)
        select(app, **config)
        with bind_config(load_project_config(tmp_path)):
            result = execute_app(app, file_logging=False)
    assert source.read_text() == ("external-protected-1031" if scenario == "stale" else "original-protected-1031")
    assert not (tmp_path / "forbidden.txt").exists()
    records = {e["details"]["record"]["call_id"]: e["details"]["record"] for e in audit(result) if e["kind"] == "tool"}
    assert records["attempt"]["status"] == "blocked"
    assert records["attempt"]["output"] is None


def test_hook_final_write_arguments_drive_protection_execution_and_receipt(tmp_path):
    import shlex
    source = tmp_path / "approved.txt"
    response = {"decision": "modify", "modified_input": {"path": str(source), "content": "approved-hook-1031"}}
    with model_service(turns=[[("repaired", "write", {"path": 19, "content": False})]]) as (url, requests):
        app = project(tmp_path, url)
        select(app, tools=[{"name": "write"}], hooks={"PreToolUse": [{"id": "repair", "matcher": "write_file",
            "command": "printf '%s' " + shlex.quote(json.dumps(response))}]})
        with bind_config(load_project_config(tmp_path)):
            result = execute_app(app, file_logging=False)
    assert source.read_text() == "approved-hook-1031"
    record = next(e["details"]["record"] for e in audit(result) if e["kind"] == "tool")
    assert record["input"] == response["modified_input"]
    assert record["status"] == "completed"


def test_large_bash_artifact_is_retrievable_without_reexecution(tmp_path):
    import re
    import shlex
    import sys
    producer = tmp_path / "produce.py"
    counter = tmp_path / "executions.txt"
    producer.write_text("from pathlib import Path\n"
        f"p=Path({str(counter)!r});p.write_text(p.read_text()+'x' if p.exists() else 'x')\n"
        "print('TARGET_RECORD: first-execution-1031')\nprint('trailing-record\\n'*5000)\n")
    command = f"{shlex.quote(sys.executable)} {shlex.quote(str(producer))}"
    def retrieve(request):
        content = next(m["content"] for m in request["messages"] if m["role"] == "tool")
        match = re.search(r"\[ContextRef (ctx_[a-zA-Z0-9]+)", content)
        assert match is not None
        return [("retrieve", "loom_retrieve_context", {"ref": match.group(1), "query": "TARGET_RECORD", "offset": 0, "limit": 5})]
    with model_service(turns=[[("large-bash", "bash", {"command": command})], retrieve]) as (url, requests):
        app = project(tmp_path, url)
        select(app, tools=[{"name": "bash"}, {"name": "loom_retrieve_context"}],
               shell_settings={"allowed_commands": ["*"], "sandbox": {"enabled": False}},
               context_engine={"min_chars": 1000, "preview_max_chars": 300})
        with bind_config(load_project_config(tmp_path)):
            result = execute_app(app, file_logging=False)
    assert counter.read_text() == "x"
    records = {e["details"]["record"]["call_id"]: e["details"]["record"] for e in audit(result) if e["kind"] == "tool"}
    assert "first-execution-1031" in records["retrieve"]["output"]
    scope = records["large-bash"]["metadata"]["native"]["result_scope"]
    assert scope["source_completeness"] == "unknown"
    assert scope["display_truncated"] is True


def test_timed_out_bash_stops_application_and_keeps_uncertain_journal(tmp_path):
    from agentloom.application.run import ApplicationRunError
    with model_service(turns=[[('timeout', 'bash', {'command': 'sleep 10', 'timeout': 0.1})]]) as (url, requests):
        app = project(tmp_path, url)
        select(app, tools=[{'name': 'bash'}], shell_settings={'allowed_commands': ['sleep'], 'sandbox': {'enabled': False}})
        with bind_config(load_project_config(tmp_path)), pytest.raises(ApplicationRunError):
            execute_app(app, file_logging=False)
    entries = [json.loads(p.read_text()) for p in tmp_path.rglob('native-tools/**/*.json')]
    assert len(entries) == 1 and entries[0]['state'] == 'uncertain'
    assert 'record' not in entries[0]
    assert len(requests) == 1


def test_failed_bash_retains_partial_artifact_without_success_evidence(tmp_path):
    with model_service(turns=[[('failure', 'bash', {'command': "printf 'partial-result-1031'; exit 7"})]]) as (url, requests):
        app = project(tmp_path, url)
        select(app, tools=[{'name': 'bash'}], shell_settings={'allowed_commands': ['printf', 'exit'], 'allowed_operators': ['*'], 'sandbox': {'enabled': False}})
        with bind_config(load_project_config(tmp_path)):
            result = execute_app(app, file_logging=False)
    entries = [json.loads(p.read_text()) for p in (result.run.run_dir / 'native-tools').rglob('*.json')]
    assert len(entries) == 1
    entry = entries[0]
    assert entry['record']['status'] == 'error' and entry['record']['output'] is None
    assert entry['raw_output'] == 'partial-result-1031'
    assert entry['result_scope']['source_completeness'] == 'partial'
    assert entry['evidence'] == []


def test_native_capture_larger_than_wire_limit_is_retained_without_reexecution(tmp_path):
    import shlex
    import sys
    producer = tmp_path / 'large_producer.py'
    producer.write_text("print('a'* (9 * 1024 * 1024))\nprint('FINAL_RECORD: first-result-1031')\n")
    with model_service(turns=[[('large', 'bash', {'command': f'{shlex.quote(sys.executable)} {shlex.quote(str(producer))}'})]]) as (url, requests):
        app = project(tmp_path, url)
        select(app, tools=[{'name': 'bash'}], shell_settings={'allowed_commands': ['*'], 'sandbox': {'enabled': False}},
               context_engine={'min_chars': 1000, 'preview_max_chars': 300})
        with bind_config(load_project_config(tmp_path)):
            result = execute_app(app, file_logging=False)
    entries = [json.loads(p.read_text()) for p in (result.run.run_dir / 'native-tools').rglob('*.json')]
    assert len(entries) == 1
    entry = entries[0]
    assert entry['record']['status'] == 'completed'
    assert entry['result_scope']['source_completeness'] == 'unknown'
    assert entry['raw_output'] == 'a' * (9 * 1024 * 1024) + '\nFINAL_RECORD: first-result-1031\n'
    assert '[ContextRef ' in json.dumps(entry['record']['output'])
    assert len(requests) == 2


def test_large_edit_result_is_durable_and_retrievable_without_reexecution(tmp_path):
    import re
    source = tmp_path / 'large.txt'
    source.write_text('x' * 4_300_000 + '\nold\n')
    def retrieve(request):
        content = [m['content'] for m in request['messages'] if m['role'] == 'tool'][-1]
        match = re.search(r'\[ContextRef (ctx_[a-zA-Z0-9]+)', content)
        assert match is not None
        return [('retrieve', 'loom_retrieve_context', {'ref': match.group(1), 'query': 'firstChangedLine', 'offset': 0, 'limit': 5})]
    with model_service(turns=[[('read', 'read', {'path': str(source), 'offset': 2, 'limit': 1})],
                             [('edit', 'edit', {'path': str(source), 'edits': [{'oldText': 'old', 'newText': 'new-proof-1031'}]})], retrieve]) as (url, requests):
        app = project(tmp_path, url)
        select(app, tools=[{'name': name} for name in ('read', 'edit', 'loom_retrieve_context')],
               context_engine={'min_chars': 1000, 'preview_max_chars': 300})
        with bind_config(load_project_config(tmp_path)):
            result = execute_app(app, file_logging=False)
    assert source.read_text() == 'x' * 4_300_000 + '\nnew-proof-1031\n'
    records = {e['details']['record']['call_id']: e['details']['record'] for e in audit(result) if e['kind'] == 'tool'}
    assert records['edit']['status'] == 'completed'
    assert 'firstChangedLine' in records['retrieve']['output']
    assert len(json.dumps(records['edit']['output'])) < 20000
    entries = [json.loads(p.read_text()) for p in (result.run.run_dir / 'native-tools').rglob('*.json')]
    edit = next(e for e in entries if e['request']['identity']['call_id'] == 'edit')
    assert len(json.dumps(edit['raw_output'])) > 8 * 1024 * 1024
    assert edit['result_scope']['display_truncated'] is True


def test_bash_cannot_claim_complete_capture_of_background_output(tmp_path):
    with model_service(turns=[[('background', 'bash', {'command': '(sleep 0.5; printf tail) & printf head'})]]) as (url, requests):
        app = project(tmp_path, url)
        select(app, tools=[{'name': 'bash'}], shell_settings={'allowed_commands': ['*'], 'allowed_operators': ['*'], 'sandbox': {'enabled': False}})
        with bind_config(load_project_config(tmp_path)):
            result = execute_app(app, file_logging=False)
    entries = [json.loads(p.read_text()) for p in (result.run.run_dir / 'native-tools').rglob('*.json')]
    assert len(entries) == 1
    assert entries[0]['record']['status'] == 'completed'
    assert entries[0]['raw_output'] == 'head'
    assert entries[0]['result_scope']['source_completeness'] == 'unknown'
    assert entries[0]['result_scope']['coverage'] == 'captured_stream'
    assert entries[0]['result_scope']['limitations']
    assert entries[0]['evidence'] == []
