from __future__ import annotations

import json
import os
import shutil
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
    return {'authenticated': os.environ.get('SERVICE_TOKEN') == 'mcp-private-canary'}
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
    (app / "config/.mcp.json").write_text(json.dumps({"mcpServers": {"fixture": {"command": sys.executable, "args": ["applications/sample/mcp_server.py"], "env": {"SERVICE_TOKEN": "mcp-private-canary"}}}}))
    credentials = {}
    bundle = snapshot_application(root, app, tmp_path / "bundles", credential_sink=credentials)
    context = Context(tmp_path / "trial", bundle.candidate_id)
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


@pytest.mark.skipif(sys.platform != "linux", reason="Real managed Host kernel enforcement and anonymous forwarding")
def test_managed_host_enforces_before_factory_and_consumes_forwarded_capabilities(project, tmp_path, monkeypatch):
    from agentloom.config.config import bind_harness_model_config
    from agentloom.config.private_bindings import bind_harness_credentials, forward_harness_bindings
    from agentloom.execution.managed_process import launch_owned_process
    from agentloom.execution.durable.client import TaskDescription
    from agentloom.execution.concurrency import WorkItem
    from agentloom.execution.durable.files import atomic_json
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
        from agentloom.execution.durable.authorization import assert_current_attempt
        assert_current_attempt()
        try:
            Path(self.options['job'], 'authorized.json').write_text('{}')
            control = 'UNEXPECTED_ALLOW'
        except PermissionError:
            control = 'denied'
        env = subprocess.run(['/usr/bin/env'], capture_output=True, text=True).stdout
        return {'hidden':self.hidden, 'control':control,
                'model':load_project_config(Path(self.options['project_root'])).llm.models['test'].api_key == 'model-private',
                'tool':resolve_bound_credentials('harness-credential-ref:fixture#/token') == 'tool-private',
                'private_fd_visible': 'AGENTLOOM_HARNESS_PRIVATE_FD' in env,
                'secret_visible': 'model-private' in env or 'tool-private' in env}
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
                  "host": {"factory": "applications.sample.operations:host", "options": {"project_root": str(bundle.project_root), "hidden": str(hidden), "job": str(job),
                    "harness_isolation": {"bundle": bundle.to_dict(), "context": {**context.__dict__, "root": str(context.root)}, "read_paths": [], "denied_paths": [str(hidden)]}}}}
    atomic_json(job / "description.json", descriptor)
    atomic_json(job / "authorized.json", {"invocation_id": "attempt"})
    env = {"PATH": os.defpath, "HOME": str(context.root / "home"), "PYTHONDONTWRITEBYTECODE": "1", "PYTHONNOUSERSITE": "1", "PYTHONPATH": os.pathsep.join((str(bundle.root / "python"), str(bundle.project_root)))}
    models = {"model": {"test": {"model": "test", "adapter": "openai_chat", "api_key": "model-private"}}}
    with bind_harness_credentials({"fixture#/token": "tool-private"}), bind_harness_model_config(models), forward_harness_bindings() as (private_env, private_fds), (job / "stdout.log").open("wb") as stdout, (job / "stderr.log").open("wb") as stderr:
        env.update(private_env)
        owner = launch_owned_process([sys.executable, "-B", "-m", "agentloom.execution.durable.job_entry", str(job)], env=env, cwd=bundle.project_root, stdout=stdout, stderr=stderr, pass_fds=private_fds, receipt_dir=job / "resource", gated=True)
    try:
        owner.release()
        assert owner.wait(timeout=15) == 0, (job / "stderr.log").read_text()
        assert owner.wait_exited(timeout=10)["physical_exit_confirmed"]
    finally:
        owner.close()
    assert json.loads((job / "result.json").read_text())["result"] == {"hidden": "denied", "control": "denied", "model": True, "tool": True, "private_fd_visible": False, "secret_visible": False}
    assert all("model-private" not in path.read_text() and "tool-private" not in path.read_text() for path in job.rglob("*.json"))


@pytest.mark.skipif(sys.platform != "linux", reason="Harness preflight rejects unvalidated OS backends first")
def test_proxy_credentials_rejected_before_any_public_archive(project, tmp_path):
    root, app = project
    bundle = snapshot_application(root, app, tmp_path / "bundles")
    context = Context(tmp_path / "trial", bundle.candidate_id)
    with pytest.raises(ValueError, match="proxy"):
        execute_isolated(bundle, context, "applications.sample.operations:probe", {}, environment={"HTTPS_PROXY": "http://user:password@127.0.0.1:8080"})
    assert not context.root.exists()
