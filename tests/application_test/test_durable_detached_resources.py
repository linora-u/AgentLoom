"""Detached tool resources through public Application/durable execution."""
import hashlib
import json
import os
import shlex
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Event

import psutil
import pytest
from agentloom.app import Application
from agentloom.execution.concurrency import WorkItem

from test_support.worker_batch import batch_project as _batch_project
from tests.application_test.test_managed_durable_executor import consumer, wait_for
from tests.application_test.test_python_process_owner import _alive, _safety
from tests.pi_test.test_application import model_service, project
from tests.pi_test.test_tools_application import select

batch_project = _batch_project


def test_public_sibling_applications_ignore_known_foreign_scope_in_ambient_and_tool_environment(batch_project):
    from agentloom.execution.process import spawn_registered_process

    root, application, _, probe, _ = batch_project
    ready = {name: Event() for name in ("first", "second")}
    release = {name: Event() for name in ready}
    processes = {}
    identities = {}
    peer = Path(__file__).resolve().parents[2] / "test_support/detached_resource_peer.py"

    def behavior(payload):
        name = payload["value"]
        environment = {"AGENTLOOM_SUBPROCESS_RUN_TOKEN": "shared-ambient-token"}
        previous = {}
        if name == "second":
            scope = processes["first"].environ()
            environment.update({key: scope[key] for key in
                                ("AGENTLOOM_NATIVE_SCOPE_GENERATION", "AGENTLOOM_MANAGED_PROCESS_GROUP")})
            for key, value in environment.items():
                previous[key] = os.environ.get(key)
                os.environ[key] = value
        try:
            process, _close = spawn_registered_process(
                [sys.executable, str(peer), str(root / f"{name}.json"), "captured"], env=environment,
            )
        finally:
            for key, value in previous.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value
        processes[name] = psutil.Process(process.pid)
        wait_for(lambda: (root / f"{name}.json").exists())
        facts = json.loads((root / f"{name}.json").read_text())
        identities[name] = [facts["root"], facts["detached"]]
        ready[name].set()
        assert release[name].wait(20), "Sibling observation was not released"
        return payload

    probe.behavior = behavior
    with Application.open(application, project_root=root, file_logging=False) as first, Application.open(
        application, project_root=root, file_logging=False,
    ) as second, ThreadPoolExecutor(max_workers=2) as callers:
        try:
            left = callers.submit(first.worker("workflows/worker.yaml").invoke, WorkItem("first", {"value": "first"}))
            assert ready["first"].wait(15)
            right = callers.submit(second.worker("workflows/worker.yaml").invoke, WorkItem("second", {"value": "second"}))
            assert ready["second"].wait(15)
            release["first"].set()
            assert left.result(timeout=10).status == "success"
            assert not any(_alive(identity) for identity in identities["first"])
            assert all(_alive(identity) for identity in identities["second"]), "Foreign markers changed sibling custody"
            release["second"].set()
            assert right.result(timeout=10).status == "success"
            assert not any(_alive(identity) for identity in identities["second"])
        finally:
            for event in release.values():
                event.set()
            for values in identities.values():
                for identity in values:
                    if _alive(identity):
                        psutil.Process(identity["pid"]).kill()


@pytest.mark.parametrize("backend", ["captured", "mcp"])
@pytest.mark.parametrize("environment", [{}, {"AGENTLOOM_NATIVE_SCOPE_GENERATION": "forged-generation",
                                            "AGENTLOOM_MANAGED_PROCESS_GROUP": "1",
                                            "AGENTLOOM_SUBPROCESS_RUN_TOKEN": "shared-ambient-token"}])
def test_durable_sigkill_reclaims_detached_tools_with_minimal_or_forged_environment(batch_project, backend, environment):
    root, application, _, _, _ = batch_project
    namespace, case = root / "durable", root / "case"
    case.mkdir()
    with Application.open(application, project_root=root, file_logging=False) as app, app.durable(root=namespace) as execution:
        handle = execution.worker("workflows/worker.yaml").submit(
            WorkItem("one", {"value": {"env": environment}, "action": f"detached-{backend}"}))
        identities = []
        with consumer(root, application, namespace, case) as host:
            try:
                marker = case / "detached-one.json"
                wait_for(marker.exists, host)
                facts = json.loads(marker.read_text())
                identities = [facts["root"], facts["detached"]]
                assert all(_alive(identity) for identity in identities)
                assert facts["generation_present"]
                assert facts["generation_sha256"] != hashlib.sha256(b"forged-generation").hexdigest()
                if backend == "captured":
                    assert facts["instance_present"]
                    assert facts["instance_sha256"] != hashlib.sha256(b"shared-ambient-token").hexdigest()
                host.kill()
                host.wait(timeout=5)
                # Observe autonomous cleanup before recovery, replacement or
                # any fixture release/safety cleanup is allowed to run.
                wait_for(lambda: not any(_alive(identity) for identity in identities))
                wait_for(lambda: execution.resource_status(handle.workflow_id)[0]["physical_exit_confirmed"])
            finally:
                _safety(host, identities)


def test_default_durable_sigkill_reclaims_pi_detached_child_after_shell_root_exits(tmp_path):
    marker = tmp_path / "shell-processes.json"
    script = tmp_path / "spawn.py"
    script.write_text("import os,sys,json,subprocess\nfrom pathlib import Path\n"
        "p=subprocess.Popen([sys.executable,'-c','import time;time.sleep(90)'],start_new_session=True,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)\n"
        f"Path({str(marker)!r}).write_text(json.dumps([os.getpid(),p.pid,os.getppid()]))\n")
    next_turn, release = Event(), Event()

    def after_shell(number, _request):
        if number > 1:
            next_turn.set()
            release.wait(20)

    identities = []
    host = None
    with model_service(turns=[[("spawn", "bash", {"command": shlex.join([sys.executable, str(script)])})]],
                       on_request=after_shell) as (url, _requests):
        workflow = project(tmp_path, url)
        select(workflow, tools=[{"name": "bash"}], shell_settings={"allowed_commands": ["*"], "sandbox": {"enabled": False}},
               input_schema={"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]})
        namespace = tmp_path / "durable"
        with Application.open(workflow.parent.parent, project_root=tmp_path, file_logging=False) as app, app.durable(root=namespace) as execution:
            handle = execution.worker("workflows/root.yaml").submit(WorkItem("one", {"query": "Run the shell command"}))
            with (tmp_path / "consumer.log").open("w") as log:
                host = subprocess.Popen([sys.executable, "-m", "test_support.detached_durable_host", str(tmp_path)],
                    stdout=log, stderr=log, cwd=Path(__file__).resolve().parents[2])
            try:
                wait_for(marker.exists, host)
                wait_for(next_turn.is_set, host)
                shell, child, sdk = json.loads(marker.read_text())
                for pid in [child, sdk]:
                    identities.append({"pid": pid, "creation": psutil.Process(pid).create_time()})
                wait_for(lambda: not psutil.pid_exists(shell))
                assert psutil.Process(child).ppid() != shell
                assert _alive(identities[0]), "Detached child must be alive before the host fault"
                host.kill()
                host.wait(timeout=5)
                wait_for(lambda: not any(_alive(identity) for identity in identities))
                wait_for(lambda: execution.resource_status(handle.workflow_id)[0]["physical_exit_confirmed"])
            finally:
                release.set()
                _safety(host, identities)


def test_durable_captured_timeout_waits_for_nested_application_cleanup_observer(batch_project):
    root, application, _, _, _ = batch_project
    namespace, case = root / "durable", root / "case"
    case.mkdir()
    inner = root / "inner"
    inner.mkdir()
    marker = inner / "business.json"
    business = inner / "business.py"
    business.write_text("import os,sys,json,subprocess,time\nfrom pathlib import Path\n"
        "p=subprocess.Popen([sys.executable,'-c','import time;time.sleep(90)'],start_new_session=True,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)\n"
        f"Path({str(marker)!r}).write_text(json.dumps([os.getpid(),p.pid,os.getppid()]))\ntime.sleep(90)\n")
    with model_service(turns=[[("business", "bash", {"command": shlex.join([sys.executable, str(business)])})]]) as (url, _requests):
        workflow = project(inner, url)
        select(workflow, tools=[{"name": "bash"}], shell_settings={"allowed_commands": ["*"], "sandbox": {"enabled": False}})
        entry = inner / "entry.py"
        entry.write_text("import os\nfrom pathlib import Path\n"
            "os.environ['TMPDIR']='/tmp'\n"
            "for key in ('AGENTLOOM_RUNTIME_ROOT','AGENTLOOM_RUN_DIR','AGENTLOOM_CHECKPOINT_DIR','AGENTLOOM_SHELL_AUDIT_PATH'): os.environ.pop(key,None)\n"
            "from agentloom.config.config import bind_config,load_project_config\nfrom agentloom.app.runner import execute_app\n"
            f"with bind_config(load_project_config(Path({str(inner)!r}))): execute_app(Path({str(workflow)!r}),file_logging=False)\n")
        identities = []
        with Application.open(application, project_root=root, file_logging=False) as app, app.durable(root=namespace) as execution:
            handle = execution.worker("workflows/worker.yaml").submit(
                WorkItem("one", {"value": {"entry": str(entry)}, "action": "captured-nested-timeout"}))
            with consumer(root, application, namespace, case) as host:
                try:
                    wait_for(marker.exists, host)
                    shell, child, sdk = json.loads(marker.read_text())
                    for pid in [shell, child, sdk, os.getpgid(sdk)]:
                        identities.append({"pid": pid, "creation": psutil.Process(pid).create_time()})
                    wait_for(lambda: execution.status(handle.workflow_id).status == "SUCCESS", host)
                    assert handle.get_result()["status"] == "success"
                    assert not any(_alive(identity) for identity in identities), "Capture cleanup killed a nested observer before its children drained"
                    assert execution.resource_status(handle.workflow_id)[0]["physical_exit_confirmed"]
                finally:
                    for identity in identities:
                        if _alive(identity):
                            psutil.Process(identity["pid"]).kill()
