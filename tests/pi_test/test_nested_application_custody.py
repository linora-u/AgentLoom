"""Nested public CLI custody through permitted genuine Pi native Bash."""
import json
import os
import shlex
import signal
import sys
from threading import Event

import psutil
import pytest

from tests.application_test.test_python_process_owner import _alive, _safety
from tests.pi_test.test_application import model_service, project
from tests.pi_test.test_process_lifecycle import node_launcher, start_cli, until
from tests.pi_test.test_tools_application import select


def test_top_python_sigkill_joins_nested_application_guardian_and_detached_children(tmp_path):
    outer, inner = tmp_path / "outer", tmp_path / "inner"
    outer.mkdir()
    inner.mkdir()
    marker = tmp_path / "inner-shell.json"
    script = tmp_path / "spawn.py"
    script.write_text("import os,sys,json,subprocess,time\nfrom pathlib import Path\n"
        "p=subprocess.Popen([sys.executable,'-c','import time;time.sleep(90)'],start_new_session=True,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)\n"
        f"Path({str(marker)!r}).write_text(json.dumps([os.getpid(),p.pid]))\ntime.sleep(90)\n")
    with model_service(turns=[[("inner", "bash", {"command": shlex.join([sys.executable, str(script)])})]]) as (inner_url, _):
        inner_app = project(inner, inner_url)
        select(inner_app, tools=[{"name": "bash"}], shell_settings={"allowed_commands": ["*"], "sandbox": {"enabled": False}})
        _environment, inner_sdk_file = node_launcher(inner)
        entry = tmp_path / "inner-cli.py"
        entry.write_text("import os\nfrom pathlib import Path\n"
            # Reuse the public CLI while giving the nested Run its own paths.
            "os.environ['TMPDIR']='/tmp'\n"
            "for key in ('AGENTLOOM_RUNTIME_ROOT','AGENTLOOM_RUN_DIR','AGENTLOOM_CHECKPOINT_DIR','AGENTLOOM_SHELL_AUDIT_PATH'): os.environ.pop(key,None)\n"
            f"os.environ['PATH']={str(inner / 'bin')!r}+os.pathsep+os.environ['PATH']\n"
            "from agentloom.config.config import bind_config,load_project_config\nfrom agentloom.__main__ import main\n"
            f"with bind_config(load_project_config(Path({str(inner)!r}))):\n"
            f" main(['run',{str(inner_app)!r},'--no-file-log','--output-format','jsonl'])\n")
        with model_service(turns=[[("outer", "bash", {"command": shlex.join([sys.executable, str(entry)])})]]) as (outer_url, _):
            outer_app = project(outer, outer_url)
            select(outer_app, tools=[{"name": "bash"}], shell_settings={"allowed_commands": ["*"], "sandbox": {"enabled": False}})
            environment, outer_sdk_file = node_launcher(outer)
            host = start_cli(outer, outer_app, environment)
            identities = []
            try:
                until(marker.exists, timeout=20)
                for process in psutil.Process(host.pid).children(recursive=True):
                    identities.append({"pid": process.pid, "creation": process.create_time()})
                for pid in json.loads(marker.read_text()):
                    identity = {"pid": pid, "creation": psutil.Process(pid).create_time()}
                    if identity not in identities:
                        identities.append(identity)
                owners = [os.getpgid(int(path.read_text())) for path in [outer_sdk_file, inner_sdk_file]]
                receipts = []
                for owner in owners:
                    description = next(path for path in tmp_path.rglob("native_resources/*/description.json")
                                       if json.loads(path.read_text())["anchor"]["pid"] == owner)
                    receipts.append(description.with_name("exited.json"))
                host.kill()
                host.wait(timeout=5)
                until(lambda: not any(_alive(identity) for identity in identities), timeout=12)
                assert host.returncode == -signal.SIGKILL
                assert all(json.loads(path.read_text())["physical_exit_confirmed"] for path in receipts)
                # The outer exit fact may only be persisted after the nested
                # guardian's own complete scope extinction has been recorded.
                assert receipts[0].stat().st_mtime_ns >= receipts[1].stat().st_mtime_ns
            finally:
                _safety(host, identities)


@pytest.mark.parametrize("detached_session", [True, False])
def test_reparented_shell_caller_requires_live_custody_before_new_nested_native_admission(tmp_path, detached_session):
    outer, inner = tmp_path / "outer", tmp_path / "inner"
    outer.mkdir()
    inner.mkdir()
    entry_marker, launch_marker = tmp_path / "entry.pid", tmp_path / "launch.json"
    release_entry, rejected = tmp_path / "release-entry", tmp_path / "rejected.json"
    business_marker = tmp_path / "inner-business.json"
    business = tmp_path / "business.py"
    business.write_text("import os,sys,json,subprocess,time\nfrom pathlib import Path\n"
        "p=subprocess.Popen([sys.executable,'-c','import time;time.sleep(90)'],start_new_session=True,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)\n"
        f"Path({str(business_marker)!r}).write_text(json.dumps([os.getpid(),p.pid]))\ntime.sleep(90)\n")
    next_turn, release_model = Event(), Event()

    def after_launcher(number, _request):
        if number > 1:
            next_turn.set()
            release_model.wait(30)

    with model_service(turns=[[("business", "bash", {"command": shlex.join([sys.executable, str(business)])})]]) as (inner_url, inner_requests):
        inner_app = project(inner, inner_url)
        select(inner_app, tools=[{"name": "bash"}], shell_settings={"allowed_commands": ["*"], "sandbox": {"enabled": False}})
        node_launcher(inner)
        entry = tmp_path / "entry.py"
        entry.write_text("import os,json,time\nfrom pathlib import Path\n"
            f"Path({str(entry_marker)!r}).write_text(str(os.getpid()))\n"
            f"while not Path({str(release_entry)!r}).exists(): time.sleep(.02)\n"
            "os.environ['TMPDIR']='/tmp'\n"
            "for key in ('AGENTLOOM_RUNTIME_ROOT','AGENTLOOM_RUN_DIR','AGENTLOOM_CHECKPOINT_DIR','AGENTLOOM_SHELL_AUDIT_PATH'): os.environ.pop(key,None)\n"
            f"os.environ['PATH']={str(inner / 'bin')!r}+os.pathsep+os.environ['PATH']\n"
            "from agentloom.config.config import bind_config,load_project_config\nfrom agentloom.app.runner import execute_app\n"
            "try:\n"
            f" with bind_config(load_project_config(Path({str(inner)!r}))): execute_app(Path({str(inner_app)!r}),file_logging=False)\n"
            "except Exception as error:\n"
            f" Path({str(rejected)!r}).write_text(json.dumps({{'type':type(error).__name__,'message':str(error)}}))\n"
            "time.sleep(90)\n")
        launcher = tmp_path / "launcher.py"
        launcher.write_text("import os,sys,json,subprocess\nfrom pathlib import Path\n"
            f"p=subprocess.Popen([sys.executable,{str(entry)!r}],start_new_session={detached_session!r},stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)\n"
            f"Path({str(launch_marker)!r}).write_text(json.dumps([os.getpid(),p.pid]))\n")
        with model_service(turns=[[("detach", "bash", {"command": shlex.join([sys.executable, str(launcher)])})]],
                           on_request=after_launcher) as (outer_url, _):
            outer_app = project(outer, outer_url)
            select(outer_app, tools=[{"name": "bash"}], shell_settings={"allowed_commands": ["*"], "sandbox": {"enabled": False}})
            environment, sdk_file = node_launcher(outer)
            host = start_cli(outer, outer_app, environment)
            identities = []
            try:
                until(entry_marker.exists, timeout=20)
                until(next_turn.is_set)
                launcher_pid, raw_pid = json.loads(launch_marker.read_text())
                until(lambda: not psutil.pid_exists(launcher_pid))
                raw = psutil.Process(raw_pid)
                outer_owner = os.getpgid(int(sdk_file.read_text()))
                # Linux's real subreaper retains actual ancestry; macOS has no
                # such custody after this deliberate ordinary session detach.
                expected_parent = outer_owner if sys.platform == "linux" else 1
                until(lambda: raw.ppid() == expected_parent)
                identities.append({"pid": raw.pid, "creation": raw.create_time()})
                release_entry.touch()
                if sys.platform == "darwin" and detached_session:
                    until(rejected.exists)
                    assert "without verified guardian custody" in json.loads(rejected.read_text())["message"]
                    assert inner_requests == [], "Unsupported admission reached model/business execution"
                    assert not business_marker.exists()
                    assert list(inner.rglob("native_resources/*/description.json")) == [], "Rejection started a new owner"
                else:
                    until(business_marker.exists, timeout=20)
                    assert inner_requests, "Actually adopted caller lost its valid enclosing custody"
                    assert not rejected.exists()
                    for pid in json.loads(business_marker.read_text()):
                        identities.append({"pid": pid, "creation": psutil.Process(pid).create_time()})
                for process in psutil.Process(host.pid).children(recursive=True):
                    identity = {"pid": process.pid, "creation": process.create_time()}
                    if identity not in identities:
                        identities.append(identity)
                host.kill()
                host.wait(timeout=5)
                # The original tagged orphan still belongs to its old scope
                # and must be reclaimed without recovery or fixture cleanup.
                until(lambda: not any(_alive(identity) for identity in identities), timeout=12)
                outer_description = next(path for path in outer.rglob("native_resources/*/description.json")
                                         if json.loads(path.read_text())["anchor"]["pid"] == outer_owner)
                assert json.loads(outer_description.with_name("exited.json").read_text())["physical_exit_confirmed"]
            finally:
                release_model.set()
                release_entry.touch()
                _safety(host, identities)
