from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path

import pytest
from agentloom.app.harnessing.isolation import IsolationUnavailable, preflight_isolation
from agentloom.app.harnessing.process import execute_isolated
from agentloom.app.harnessing.snapshot import derive_candidate, load_bundle, snapshot_application


@dataclass
class Context:
    root: Path
    candidate_id: str
    experiment_id: str = "experiment"
    trial_id: str = "trial"
    task_snapshot_id: str = "task"


@pytest.fixture
def project(tmp_path):
    project = tmp_path / "project"
    app = project / "applications" / "sample"
    (project / "config").mkdir(parents=True)
    (project / "config" / "system.yaml").write_text("{}\n")
    (app / "prompts").mkdir(parents=True)
    (app / "prompts" / "main.md").write_text("frozen instructions")
    (app / "skills" / "nested" / "references").mkdir(parents=True)
    (app / "skills" / "nested" / "SKILL.md").write_text("---\nname: sample\ndescription: sample\n---\nRead references/detail.txt")
    (app / "skills" / "nested" / "references" / "detail.txt").write_text("frozen reference")
    (app / "operations.py").write_text('''
import errno, os, subprocess, sys
from pathlib import Path

def probe(payload, bundle, context):
    root = Path(context['root'])
    candidate = Path(bundle['root']) / 'project' / 'applications/sample/prompts/main.md'
    results = {'prompt': candidate.read_text(), 'parent_secret': os.environ.get('HARNESS_CANARY')}
    for name, operation in (
        ('read', lambda: Path(payload['hidden']).read_text()),
        ('metadata', lambda: os.chmod(payload['hidden'], 0o777)),
        ('write_bundle', lambda: candidate.write_text('corrupt')),
        ('forge_receipt', lambda: (root / 'process/input.json').write_text('corrupt')),
        ('read_env', lambda: Path('/proc/self/environ').read_bytes()),
    ):
        try:
            operation()
            results[name] = 'UNEXPECTED_ALLOW'
        except PermissionError:
            results[name] = 'denied'
    (root / 'workspace/link').symlink_to(payload['hidden'])
    try:
        (root / 'workspace/link').read_text()
        results['symlink'] = 'UNEXPECTED_ALLOW'
    except PermissionError:
        results['symlink'] = 'denied'
    child = subprocess.run(['/usr/bin/cat', payload['hidden']], capture_output=True)
    results['exec'] = child.returncode != 0
    (root / 'output/result.txt').write_text('delivered')
    from agentloom.config.config import load_project_config
    config = load_project_config(Path(bundle['root']) / 'project')
    results['model_bound'] = config.llm.models['test'].api_key == 'private-canary'
    environment = subprocess.run(['/usr/bin/env'], capture_output=True, text=True).stdout
    results['child_secret'] = 'private-canary' in environment
    return results
''')
    return project, app


def test_snapshot_preserves_all_skill_bytes_and_portable_identity(project, tmp_path):
    root, app = project
    (app / "config").mkdir()
    (app / "config/model.yaml").write_text("model:\n  test:\n    api_key: private-canary\n")
    hidden = app / "hidden.json"
    hidden.write_text('{"score":99}')
    bundle = snapshot_application(root, app, tmp_path / "bundles", excluded_paths=[hidden])
    assert "private-canary" not in (bundle.application_root / "config/model.yaml").read_text()
    assert not (bundle.application_root / "hidden.json").exists()
    (app / "skills/nested/references/detail.txt").write_text("live mutation")
    assert (bundle.application_root / "skills/nested/references/detail.txt").read_text() == "frozen reference"
    moved = tmp_path / "moved"
    shutil.copytree(bundle.root, moved)
    assert load_bundle(moved).candidate_id == bundle.candidate_id
    (moved / "project/applications/sample/prompts/main.md").chmod(0o644)
    (moved / "project/applications/sample/prompts/main.md").write_text("tampered")
    with pytest.raises(ValueError, match="changed"):
        load_bundle(moved)


def test_candidate_cannot_change_fixed_business_code(project, tmp_path):
    root, app = project
    bundle = snapshot_application(root, app, tmp_path / "bundles")
    with pytest.raises(ValueError, match="fixed code"):
        derive_candidate(bundle, {"applications/sample/operations.py": "print('changed')"}, tmp_path / "bundles", editable=["tools"])
    derived = derive_candidate(bundle, {"applications/sample/prompts/main.md": "improved"}, tmp_path / "bundles", editable=["system_prompt"], validate=lambda project, app: None)
    assert derived.candidate_id != bundle.candidate_id
    assert (bundle.application_root / "prompts/main.md").read_text() == "frozen instructions"


def test_dotenv_variants_remain_private_even_when_declared_as_extra_resources(project, tmp_path):
    root, app = project
    files = [app / name for name in (".env", ".env.local", ".env.production")]
    for path in files:
        path.write_text("API_KEY=PRIVATE-DOTENV-CANARY")
    bundle = snapshot_application(root, app, tmp_path / "bundles", additional_resources=files)
    assert all(not (bundle.application_root / path.name).exists() for path in files)
    assert all(b"PRIVATE-DOTENV-CANARY" not in path.read_bytes()
               for path in bundle.root.rglob("*") if path.is_file())


@pytest.mark.skipif(sys.platform != "linux", reason="Real Linux kernel isolation acceptance; Darwin backend is rejected")
def test_public_managed_process_denies_hidden_exec_metadata_symlink_and_credentials(project, tmp_path, monkeypatch):
    try:
        preflight_isolation()
    except IsolationUnavailable as error:
        pytest.skip(str(error))
    root, app = project
    hidden = tmp_path / "hidden-score"
    hidden.write_text("held out score")
    original_mode = hidden.stat().st_mode
    monkeypatch.setenv("HARNESS_CANARY", "parent-only")
    bundle = snapshot_application(root, app, tmp_path / "bundles")
    context = Context(tmp_path / "trial", bundle.candidate_id)
    models = {"model": {"test": {"model": "test", "adapter": "openai_chat", "api_key": "private-canary"}}}
    result = execute_isolated(bundle, context, "applications.sample.operations:probe", {"hidden": str(hidden)}, denied_paths=[hidden], model_config=models, timeout=20)
    assert result == {"prompt": "frozen instructions", "parent_secret": None, "read": "denied", "metadata": "denied", "write_bundle": "denied", "forge_receipt": "denied", "read_env": "denied", "symlink": "denied", "exec": True, "model_bound": True, "child_secret": False}
    assert (context.root / "output/result.txt").read_text() == "delivered"
    assert hidden.stat().st_mode == original_mode
    assert execute_isolated(bundle, context, "applications.sample.operations:probe", {"hidden": str(hidden)}, denied_paths=[hidden], model_config=models) == result
    assert len(list((context.root / "process/attempts").iterdir())) == 1
    assert "private-canary" not in (context.root / "process/input.json").read_text()
    assert json.loads((context.root / "process/attempts/000001/cleanup.json").read_text())["physical_exit_confirmed"]


def test_credential_binding_cannot_be_moved_or_redirected(project, tmp_path):
    root, app = project
    (app / "config").mkdir()
    (app / "config/.mcp.json").write_text(json.dumps({"mcpServers": {"fixture": {"command": "/usr/bin/python3", "env": {"SERVICE_TOKEN": "mcp-private-canary"}}}}))
    credentials = {}
    bundle = snapshot_application(root, app, tmp_path / "bundles", credential_sink=credentials)
    frozen = json.loads((bundle.application_root / "config/.mcp.json").read_text())
    assert list(credentials.values()) == ["mcp-private-canary"]
    frozen["mcpServers"]["fixture"]["command"] = "/usr/bin/env"
    with pytest.raises(ValueError, match="redirect"):
        derive_candidate(bundle, {"applications/sample/config/.mcp.json": json.dumps(frozen)}, tmp_path / "bundles", editable=["mcp_servers"], validate=lambda project, app: None)


@pytest.mark.parametrize("mutation", ["plaintext", "new_reference", "move_reference", "private_url", "redirect_args"])
def test_candidate_secret_rejection_leaves_no_archive_or_staging_bytes(project, tmp_path, mutation):
    from copy import deepcopy

    root, app = project
    (app / "config").mkdir()
    relative = "applications/sample/config/.mcp.json"
    private_values = ("PARENT-TOKEN-CANARY", "PARENT-ARGV-CANARY", "CANDIDATE-TOKEN-CANARY")
    (app / "config/.mcp.json").write_text(json.dumps({"mcpServers": {"fixture": {
        "command": "/usr/bin/python3", "args": ["--token", private_values[1]],
        "env": {"SERVICE_TOKEN": private_values[0]}}}}))
    credentials = {}
    destination = tmp_path / "bundles"
    bundle = snapshot_application(root, app, destination, credential_sink=credentials)
    frozen = json.loads((bundle.application_root / "config/.mcp.json").read_text())
    candidate = deepcopy(frozen)
    server = candidate["mcpServers"]["fixture"]
    if mutation == "plaintext":
        candidate["mcpServers"]["other"] = {"command": "/usr/bin/python3", "env": {"SERVICE_TOKEN": private_values[2]}}
    elif mutation == "new_reference":
        candidate["mcpServers"]["other"] = {"command": "/usr/bin/python3", "env": {"SERVICE_TOKEN": "harness-credential-ref:" + relative + "#/new"}}
    elif mutation == "move_reference":
        candidate["mcpServers"]["other"] = {"command": "/usr/bin/python3", "env": {"SERVICE_TOKEN": server["env"]["SERVICE_TOKEN"]}}
    elif mutation == "private_url":
        candidate["mcpServers"]["other"] = {"type": "streamable_http", "url": "https://service.test/?token=" + private_values[2]}
    else:
        server["args"] = ["--different-launch"]
    existing = set(destination.iterdir())
    with pytest.raises(ValueError, match="credential|capability"):
        derive_candidate(bundle, {relative: json.dumps(candidate)}, destination,
                         editable=["mcp_servers"], validate=lambda *args: None)
    assert set(destination.iterdir()) == existing
    bundle.verify()
    assert all(canary.encode() not in path.read_bytes() for canary in private_values
               for path in destination.rglob("*") if path.is_file())
    # The same original references remain legitimate without rebinding or
    # writing their private values; a normal prompt edit can still commit.
    derived = derive_candidate(bundle, {relative: json.dumps(frozen), "applications/sample/prompts/main.md": "PUBLIC-CANDIDATE-EDIT"},
                               destination, editable=["mcp_servers", "system_prompt"], validate=lambda *args: None)
    assert (derived.application_root / "prompts/main.md").read_text() == "PUBLIC-CANDIDATE-EDIT"
    assert json.loads((derived.application_root / "config/.mcp.json").read_text()) == frozen
    assert credentials[relative + "#/mcpServers/fixture/env/SERVICE_TOKEN"] == private_values[0]
    assert all(canary.encode() not in path.read_bytes() for canary in private_values
               for path in destination.rglob("*") if path.is_file())


@pytest.mark.skipif(sys.platform != "linux", reason="Real inherited Linux enforcement")
def test_local_stdio_mcp_receives_only_its_bound_capability(project, tmp_path):
    preflight_isolation()
    root, app = project
    (app / "config").mkdir()
    (app / "mcp_server.py").write_text('''
import os
from mcp.server.fastmcp import FastMCP
server = FastMCP('fixture')
@server.tool()
def authorized() -> dict:
    import sys
    return {'authenticated': os.environ.get('SERVICE_TOKEN') == 'mcp-private-canary'
            and sys.argv[-2:] == ['--token', 'mcp-argv-private-canary']}
server.run(transport='stdio')
''')
    (app / "operations.py").write_text((app / "operations.py").read_text() + '''

async def mcp_probe(payload, bundle, context):
    import json, subprocess
    from mcp import StdioServerParameters
    from agentloom.integrations.mcp.config import load_mcp_json, parse_mcp_servers_from_json
    from agentloom.integrations.mcp.managed_stdio import managed_stdio_tools
    settings = load_mcp_json(Path(bundle['root']) / 'project/applications/sample/config/.mcp.json')
    binding = parse_mcp_servers_from_json(settings, strict=True)[0]
    server = StdioServerParameters(command=binding.command, args=binding.args, env=binding.env)
    async with managed_stdio_tools(server, timeout=5.0) as (session, tools):
        result = await session.call_tool('authorized', {})
    environment = subprocess.run(['/usr/bin/env'],capture_output=True,text=True).stdout
    return {'authenticated': json.loads(result.content[0].text)['authenticated'],
            'shell_can_see_server_secret': 'mcp-private-canary' in environment}
''')
    (app / "config/.mcp.json").write_text(json.dumps({"mcpServers": {"fixture": {"command": sys.executable, "args": ["applications/sample/mcp_server.py", "--token", "mcp-argv-private-canary"], "env": {"SERVICE_TOKEN": "mcp-private-canary"}}}}))
    credentials = {}
    bundle = snapshot_application(root, app, tmp_path / "bundles", credential_sink=credentials)
    context = Context(tmp_path / "trial", bundle.candidate_id)
    assert "mcp-argv-private-canary" not in (bundle.application_root / "config/.mcp.json").read_text()
    assert isinstance(json.loads((bundle.application_root / "config/.mcp.json").read_text())["mcpServers"]["fixture"]["args"], str)
    assert execute_isolated(bundle, context, "applications.sample.operations:mcp_probe", {}, credentials=credentials, timeout=20) == {"authenticated": True, "shell_can_see_server_secret": False}
    assert all("mcp-private-canary" not in path.read_text() for path in (context.root / "process").rglob("*.json"))


@pytest.mark.skipif(sys.platform != "linux", reason="Harness preflight deliberately rejects unvalidated backends")
def test_original_guardian_digest_must_match_snapshot(project, tmp_path, monkeypatch):
    import agentloom.execution.managed_process as managed
    root, app = project
    bundle = snapshot_application(root, app, tmp_path / "bundles")
    fake = tmp_path / "fake-framework"
    fake.mkdir()
    (fake / "_process_guardian.py").write_text("# another guardian")
    monkeypatch.setattr(managed, "__file__", str(fake / "managed_process.py"))
    with pytest.raises(ValueError, match="guardian source differs"):
        execute_isolated(bundle, Context(tmp_path / "trial", bundle.candidate_id), "applications.sample.operations:probe", {})


@pytest.mark.skipif(sys.platform != "linux", reason="Actual Linux guardian and inherited policy acceptance")
def test_stop_confirms_detached_resource_exit_before_same_trial_retry(project, tmp_path):
    import psutil
    root, app = project
    (app / "operations.py").write_text((app / "operations.py").read_text() + '''

def controlled_stop(payload, bundle, context):
    import time
    root = Path(context['root'])
    marker = root / 'output/child-pid'
    if marker.exists():
        return {'recovered': True}
    process = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(120)'], start_new_session=True)
    marker.write_text(str(process.pid))
    while True:
        time.sleep(.02)
''')
    bundle = snapshot_application(root, app, tmp_path / "bundles")
    context = Context(tmp_path / "trial", bundle.candidate_id)
    stop = threading.Event()
    def cancel_after_child_exists():
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline and not (context.root / "output/child-pid").exists():
            time.sleep(.01)
        stop.set()
    thread = threading.Thread(target=cancel_after_child_exists)
    thread.start()
    with pytest.raises(InterruptedError):
        execute_isolated(bundle, context, "applications.sample.operations:controlled_stop", {}, stop=stop, timeout=15)
    thread.join()
    child = int((context.root / "output/child-pid").read_text())
    assert not psutil.pid_exists(child)
    assert json.loads((context.root / "process/attempts/000001/cleanup.json").read_text())["physical_exit_confirmed"]
    assert execute_isolated(bundle, context, "applications.sample.operations:controlled_stop", {}, timeout=10) == {"recovered": True}
    assert len(list((context.root / "process/attempts").iterdir())) == 2


@pytest.mark.skipif(sys.platform != "linux", reason="Actual controller death and managed child exit")
@pytest.mark.parametrize("receipt_change", ["none", "identity", "malformed", "partial"])
def test_controller_death_reuses_only_complete_matching_child_receipt(project, tmp_path, receipt_change):
    from agentloom.app.harnessing.process import IsolatedExecutionError
    root, app = project
    (app / "operations.py").write_text('''
from pathlib import Path
def completed(payload, bundle, context):
    path = Path(context['root']) / 'output/invocations'
    count = int(path.read_text()) + 1 if path.exists() else 1
    path.write_text(str(count))
    return {'completed_invocation': count}
''')
    bundle = snapshot_application(root, app, tmp_path / "bundles")
    context = Context(tmp_path / "trial", bundle.candidate_id)
    script = '''
import os, sys
from pathlib import Path
from agentloom.app.harnessing import process
from agentloom.app.harnessing.snapshot import load_bundle
atomic = process._atomic
def crash_before_parent_commit(path, value):
    if path.name == 'result.json' and path.parent.name == 'process':
        os._exit(91)
    atomic(path, value)
process._atomic = crash_before_parent_commit
bundle = load_bundle(Path(sys.argv[1]))
context = {'root':sys.argv[2], 'candidate_id':bundle.candidate_id,
           'experiment_id':'experiment', 'trial_id':'trial', 'task_snapshot_id':'task'}
process.execute_isolated(bundle, context, 'applications.sample.operations:completed', {}, timeout=10)
'''
    controller = subprocess.run([sys.executable, "-c", script, str(bundle.root), str(context.root)],
        capture_output=True, text=True, timeout=20)
    assert controller.returncode == 91, controller.stderr
    job = context.root / "process"
    assert not (job / "result.json").exists()
    child = job / "attempts/000001/result.json"
    value = json.loads(child.read_text())
    assert value["result"] == {"completed_invocation": 1}
    assert json.loads((child.parent / "cleanup.json").read_text())["physical_exit_confirmed"]
    if receipt_change == "partial":
        child.write_text('{"identity":')
    elif receipt_change != "none":
        if receipt_change == "identity":
            value["identity"] = "different-operation"
        else:
            value["result"] = []
        child.write_text(json.dumps(value))
        with pytest.raises(IsolatedExecutionError, match="identity|JSON mapping"):
            execute_isolated(bundle, context, "applications.sample.operations:completed", {}, timeout=10)
        assert (context.root / "output/invocations").read_text() == "1"
        assert len(list((job / "attempts").iterdir())) == 1
        return
    result = execute_isolated(bundle, context, "applications.sample.operations:completed", {}, timeout=10)
    expected = 2 if receipt_change == "partial" else 1
    assert result == {"completed_invocation": expected}
    assert (context.root / "output/invocations").read_text() == str(expected)
    assert len(list((job / "attempts").iterdir())) == expected


@pytest.mark.skipif(sys.platform != "linux", reason="Real managed Host kernel enforcement and anonymous forwarding")
def test_managed_host_enforces_before_factory_and_consumes_forwarded_capabilities(project, tmp_path, monkeypatch):
    from agentloom.config.config import bind_harness_model_config
    from agentloom.config.private_bindings import bind_harness_credentials, forward_harness_bindings
    from agentloom.execution.concurrency import WorkItem
    from agentloom.execution.durable.client import TaskDescription
    from agentloom.execution.durable.files import atomic_json
    from agentloom.execution.managed_process import launch_owned_process
    from agentloom.runtimes.pi.credentials import bind_pi_credentials
    root, app = project
    hidden = tmp_path / "held-out.json"
    hidden.write_text("hidden evaluation")
    (app / "operations.py").write_text('''
import os, subprocess
from pathlib import Path
class Host:
    def __init__(self, options):
        self.options = options
        # Construction itself must already be restricted.
        try:
            Path(options['hidden']).read_text()
            self.hidden = 'UNEXPECTED_ALLOW'
        except PermissionError:
            self.hidden = 'denied'
    def invoke(self, task, invocation_id):
        from agentloom.config.config import load_project_config
        from agentloom.config.private_bindings import resolve_bound_credentials
        from agentloom.runtimes.pi.credentials import current_pi_credentials
        from agentloom.execution.durable.authorization import assert_current_attempt
        from agentloom.execution.native_journal import NativeCallJournal
        assert_current_attempt()
        journal = NativeCallJournal(Path(self.options['runtime_root']) / 'new/deep/native-tools')
        journal.close()
        try:
            Path(self.options['job'], 'authorized.json').write_text('{}')
            control = 'UNEXPECTED_ALLOW'
        except PermissionError:
            control = 'denied'
        env = subprocess.run(['/usr/bin/env'], capture_output=True, text=True).stdout
        return {'hidden':self.hidden, 'control':control,
                'model':load_project_config(Path(self.options['project_root'])).llm.models['test'].api_key == 'model-private',
                'tool':resolve_bound_credentials('harness-credential-ref:fixture#/token') == 'tool-private',
                'pi':current_pi_credentials()['fixture']['access'] == 'pi-private',
                'private_fd_visible': 'AGENTLOOM_HARNESS_PRIVATE_FD' in env,
                'secret_visible': any(value in env for value in ('model-private','tool-private','pi-private'))}
def host(options):
    return Host(options)
''')
    bundle = snapshot_application(root, app, tmp_path / "bundles")
    # The trusted news outer process imports the archived framework. Select
    # that identical guardian entry here while keeping ownership in pytest.
    import agentloom.execution.managed_process as managed
    monkeypatch.setattr(managed, "__file__", str(bundle.root / "python/agentloom/execution/managed_process.py"))
    context = Context(tmp_path / "trial", bundle.candidate_id)
    context.root.mkdir()
    job = context.root / "control-execution/jobs/workflow/attempt"
    job.mkdir(parents=True)
    descriptor = {"invocation_id": "attempt", "task": TaskDescription("agentloom-fixture", "worker.yaml", WorkItem("task", {}), "revision").to_dict(),
                  "host": {"factory": "applications.sample.operations:host", "options": {"project_root": str(bundle.project_root), "hidden": str(hidden), "job": str(job), "runtime_root": str(context.root / 'runtime'),
                    "harness_isolation": {"bundle": bundle.to_dict(), "context": {**context.__dict__, "root": str(context.root)}, "read_paths": [], "denied_paths": [str(hidden)]}}}}
    atomic_json(job / "description.json", descriptor)
    atomic_json(job / "authorized.json", {"invocation_id": "attempt"})
    env = {"PATH": os.defpath, "HOME": str(context.root / "home"), "PYTHONDONTWRITEBYTECODE": "1", "PYTHONNOUSERSITE": "1", "PYTHONPATH": os.pathsep.join((str(bundle.root / "python"), str(bundle.project_root)))}
    models = {"model": {"test": {"model": "test", "adapter": "openai_chat", "api_key": "model-private"}}}
    with bind_harness_credentials({"fixture#/token": "tool-private"}), bind_harness_model_config(models), bind_pi_credentials({"fixture": {"type": "oauth", "access": "pi-private"}}), forward_harness_bindings() as (private_env, private_fds), (job / "stdout.log").open("wb") as stdout, (job / "stderr.log").open("wb") as stderr:
        env.update(private_env)
        owner = launch_owned_process([sys.executable, "-B", "-m", "agentloom.execution.durable.job_entry", str(job)], env=env, cwd=bundle.project_root, stdout=stdout, stderr=stderr, pass_fds=private_fds, receipt_dir=job / "resource", gated=True)
    try:
        owner.release()
        assert owner.wait(timeout=15) == 0, (job / "stderr.log").read_text()
        assert owner.wait_exited(timeout=10)["physical_exit_confirmed"]
    finally:
        owner.close()
    assert json.loads((job / "result.json").read_text())["result"] == {"hidden": "denied", "control": "denied", "model": True, "tool": True, "pi": True, "private_fd_visible": False, "secret_visible": False}
    assert all(all(secret not in path.read_text() for secret in ("model-private", "tool-private", "pi-private")) for path in job.rglob("*.json"))


@pytest.mark.skipif(sys.platform != "linux", reason="Harness preflight rejects unvalidated OS backends first")
def test_proxy_credentials_rejected_before_any_public_archive(project, tmp_path):
    root, app = project
    bundle = snapshot_application(root, app, tmp_path / "bundles")
    context = Context(tmp_path / "trial", bundle.candidate_id)
    with pytest.raises(ValueError, match="proxy"):
        execute_isolated(bundle, context, "applications.sample.operations:probe", {}, environment={"HTTPS_PROXY": "http://user:password@127.0.0.1:8080"})
    assert not context.root.exists()


def test_secret_urls_are_bound_and_metadata_refs_are_not_file_restoration_refs(project, tmp_path):
    root, app = project
    (app / "config").mkdir()
    (app / "config/.mcp.json").write_text(json.dumps({"mcpServers": {"fixture": {"type": "streamable_http", "url": "https://service.test/mcp?api_key=url-private"}}}))
    credentials = {}
    bundle = snapshot_application(root, app, tmp_path / "bundles", credential_sink=credentials, model_config={"model": {"test": {"api_key": "model-private"}}})
    assert credentials == {"applications/sample/config/.mcp.json#/mcpServers/fixture/url": "https://service.test/mcp?api_key=url-private"}
    assert bundle.manifest["credential_refs"] == list(credentials)
    assert "url-private" not in (bundle.application_root / "config/.mcp.json").read_text()
    assert "model-private" not in (bundle.root / "manifest.json").read_text()
    changed = {"mcpServers": {"new": {"type": "streamable_http", "url": "https://new.test/?token=new-private"}}}
    with pytest.raises(ValueError, match="credential-bearing URLs"):
        derive_candidate(bundle, {"applications/sample/config/.mcp.json": json.dumps(changed)}, tmp_path / "bundles", editable=["mcp_servers"], validate=lambda *args: None)


@pytest.mark.skipif(os.name != "posix", reason="Actual POSIX identity check")
def test_scope_rejects_a_real_child_with_forged_wrong_parent_pipe(tmp_path):
    import subprocess

    import psutil
    from agentloom.execution.process_scope import NATIVE_SCOPE_FD_ENV
    read, write = os.pipe()
    env = os.environ.copy()
    env[NATIVE_SCOPE_FD_ENV] = str(read)
    code = "from agentloom.execution.process_scope import bind_managed_process_scope\nwith bind_managed_process_scope():\n raise SystemExit('UNEXPECTED_ALLOW')\n"
    # This innocent process has a genuine PID/create_time/group, but does not
    # own the child. A forged anonymous pipe cannot create custody authority.
    other = subprocess.Popen([sys.executable, "-c", "import time;time.sleep(30)"], start_new_session=True)
    child = subprocess.Popen([sys.executable, "-c", code], env=env, pass_fds=(read,), start_new_session=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    os.close(read)
    try:
        os.write(write, json.dumps({"anchor": {"pid": other.pid, "creation": psutil.Process(other.pid).create_time(), "group": other.pid}, "child": {"pid": child.pid, "creation": psutil.Process(child.pid).create_time()}}).encode())
        os.close(write)
        write = None
        stdout, stderr = child.communicate(timeout=10)
        assert child.returncode != 0
        assert b"does not match its actual guardian identity" in stderr
        assert b"UNEXPECTED_ALLOW" not in stdout
    finally:
        if write is not None:
            os.close(write)
        if child.poll() is None:
            child.kill()
        child.wait()
        other.terminate()
        other.wait()


@pytest.mark.skipif(sys.platform != "linux", reason="Real Node process inherited filesystem policy")
def test_real_node_initializes_with_valid_inherited_and_reopened_stdio(project, tmp_path):
    import subprocess

    from agentloom.runtimes.pi.credentials import bind_pi_credentials
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node is not installed")
    version = subprocess.check_output([node, "--version"], text=True).strip()
    root, app = project
    (app / "operations.py").write_text('''
import subprocess
def node_probe(payload, bundle, context):
    from agentloom.runtimes.pi.credentials import current_pi_credentials
    inherited = subprocess.run([payload['node'], '--version'], capture_output=True, text=True)
    closed = subprocess.run([payload['node'], '--version'], capture_output=True, text=True, preexec_fn=lambda: __import__('os').close(0))
    return {'inherited_code':inherited.returncode, 'inherited_version':inherited.stdout.strip(), 'reopened_code':closed.returncode, 'reopened_version':closed.stdout.strip(), 'pi_bound':current_pi_credentials()['fixture']['access'] == 'pi-private'}
''')
    bundle = snapshot_application(root, app, tmp_path / "bundles")
    with bind_pi_credentials({"fixture": {"type": "oauth", "access": "pi-private"}}):
        result = execute_isolated(bundle, Context(tmp_path / "trial", bundle.candidate_id), "applications.sample.operations:node_probe", {"node": node}, environment={"PATH": os.environ["PATH"]}, timeout=10)
    assert result == {"inherited_code": 0, "inherited_version": version, "reopened_code": 0, "reopened_version": version, "pi_bound": True}


def test_snapshot_preserves_runtime_python_packages_and_all_declared_skill_resources(project, tmp_path):
    root, app = project
    (app / "runtime").mkdir()
    (app / "runtime/__init__.py").write_text("from .fixed import VALUE\n")
    (app / "runtime/fixed.py").write_text("VALUE = 'frozen program'\n")
    (app / "data").mkdir()
    (app / "data/raw-input.json").write_text("dynamic input, not a dependency")
    skill = app / "skills/nested"
    (skill / "data").mkdir()
    (skill / "data/reference.json").write_text('{"fixed":true}')
    (skill / "dist").mkdir()
    (skill / "dist/run.js").write_text("console.log('fixed skill script')")
    bundle = snapshot_application(root, app, tmp_path / "bundles")
    assert (bundle.application_root / "runtime/fixed.py").read_text() == "VALUE = 'frozen program'\n"
    assert (bundle.application_root / "skills/nested/data/reference.json").read_text() == '{"fixed":true}'
    assert (bundle.application_root / "skills/nested/dist/run.js").is_file()
    assert not (bundle.application_root / "data").exists()
    environment = {item['name']: item for item in bundle.manifest['environment']}
    assert environment['runtime:python']['implementation']
    assert environment['runtime:platform']['machine']


@pytest.mark.skipif(sys.platform != "linux", reason="Actual external subreaper custody under inherited OS policy")
@pytest.mark.parametrize("mode", ["completed", "timeout"])
def test_scoped_shell_cleanup_defers_descendants_until_confirmed_whole_host_exit(project, tmp_path, mode):
    import psutil
    root, app = project
    (app / "operations.py").write_text('''
import os, subprocess, sys
from pathlib import Path
from agentloom.execution.process import run_captured_process, CapturedProcessTimeout
def hook_probe(payload, bundle, context):
    marker = str(Path(context['root']) / 'output/detached-pid')
    script = "import os,subprocess,sys,time;from pathlib import Path; child=subprocess.Popen([sys.executable,'-c','import time; time.sleep(120)'],env={},start_new_session=True,stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL);Path("+repr(marker)+").write_text(str(child.pid));print('hook output',flush=True)"
    if payload['mode'] == 'timeout':
        script += ";time.sleep(120)"
    import shlex
    command = shlex.join([sys.executable, '-c', script])
    try:
        result = run_captured_process(command, cwd=str(Path(bundle['root'])/'project'), env=os.environ.copy(), stdin=b'{}', timeout=.3, stdout_limit=1000, stderr_limit=1000)
        return {'timed_out':False,'scope':result.cleanup_scope,'physical_exit_confirmed':result.physical_exit_confirmed,'output':result.stdout.strip()}
    except CapturedProcessTimeout as error:
        return {'timed_out':True,'scope':error.cleanup_scope,'physical_exit_confirmed':error.physical_exit_confirmed,'effect_uncertain':error.effect_uncertain}
''')
    bundle = snapshot_application(root, app, tmp_path / "bundles")
    context = Context(tmp_path / "trial", bundle.candidate_id)
    result = execute_isolated(bundle, context, "applications.sample.operations:hook_probe", {"mode": mode}, timeout=15)
    assert result["timed_out"] == (mode == "timeout")
    assert result["scope"] == "outer_host"
    assert result["physical_exit_confirmed"] is False
    if mode == "timeout":
        assert result["effect_uncertain"]
    else:
        assert result["output"] == "hook output"
    child = int((context.root / "output/detached-pid").read_text())
    assert not psutil.pid_exists(child)
    cleanup = json.loads((context.root / "process/attempts/000001/cleanup.json").read_text())
    assert cleanup["physical_exit_confirmed"] is True


def test_bound_credential_echo_in_plaintext_prompt_is_rejected_before_staging(project, tmp_path):
    from agentloom.config.private_bindings import bind_harness_credentials
    root, app = project
    (app / "prompts").mkdir(exist_ok=True)
    (app / "prompts/root.md").write_text("PUBLIC BASE")
    bundle = snapshot_application(root, app, tmp_path / "bundles")
    before = set((tmp_path / "bundles").iterdir())
    with bind_harness_credentials({"fixture#/token": "private-prompt-canary"}):
        with pytest.raises(RuntimeError, match="bound credential"):
            derive_candidate(bundle, {"applications/sample/prompts/root.md": "private-prompt-canary"},
                             tmp_path / "bundles", editable=["system_prompt"])
    assert set((tmp_path / "bundles").iterdir()) == before
    assert all(b"private-prompt-canary" not in path.read_bytes() for path in bundle.root.rglob("*") if path.is_file())
