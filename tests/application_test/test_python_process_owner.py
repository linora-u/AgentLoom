"""Native lifecycle through public Application calls; no foreign runtime owner."""
import json
import os
import signal
import socket
import subprocess
import sys
import time
from contextvars import copy_context
from pathlib import Path
from threading import Event, Thread

import psutil
import pytest

from test_support.worker_batch import batch_project as _batch_project

batch_project = _batch_project

pytestmark = pytest.mark.skipif(sys.platform not in {"darwin", "linux"}, reason="managed POSIX execution")


def test_application_keeps_native_cleanup_diagnostic_when_scope_retry_succeeds(batch_project, monkeypatch):
    from agentloom.app import Application
    from agentloom.execution.concurrency import WorkerBatchError, WorkItem
    from agentloom.execution.process import CapturedProcessTimeout, run_captured_process

    root, application, _worker, probe, _write = batch_project
    fault = root / "fail-native-exit-write"
    fault.touch()
    injection = root / "filesystem-fault"
    injection.mkdir()
    (injection / "sitecustomize.py").write_text(
        "import errno,os\nfrom pathlib import Path\n_replace=os.replace\n"
        f"_fault=Path({str(fault)!r})\n"
        "def replace(source,target,*args,**kwargs):\n"
        "    if _fault.exists() and Path(target).name=='exited.json':\n"
        "        raise OSError(errno.EIO,'original native cleanup write failure')\n"
        "    return _replace(source,target,*args,**kwargs)\n"
        "os.replace=replace\n"
    )
    monkeypatch.setenv("PYTHONPATH", str(injection) + os.pathsep + os.environ.get("PYTHONPATH", ""))
    errors = []

    def behavior(_payload):
        try:
            run_captured_process([sys.executable, "-c", "import time;time.sleep(120)"],
                                 env=dict(os.environ), cwd=str(root), shell=False, timeout=.1,
                                 stdin=b"", stdout_limit=32, stderr_limit=32)
        except BaseException as error:
            errors.append(error)
            fault.unlink()  # The later attempt-scope close can now actually succeed.
            raise

    probe.behavior = behavior
    with Application.open(application, project_root=root, file_logging=False) as app:
        with pytest.raises(WorkerBatchError) as caught:
            app.worker("workflows/worker.yaml").map([WorkItem("cleanup", {"value": 1})])
    assert caught.value.phase == "cleanup"
    outcome = caught.value.outcomes[0]
    assert outcome.error.category == "infrastructure"
    assert outcome.cleanup_errors and "original native cleanup write failure" in outcome.cleanup_errors[0].message
    assert isinstance(errors[0].exceptions[0], CapturedProcessTimeout)
    assert errors[0].managed_cleanup_errors[0].errno == 5
    assert all(json.loads(path.read_text())["physical_exit_confirmed"]
               for path in root.rglob("native_resources/*/exited.json"))


def test_application_keeps_control_identity_and_accumulates_both_native_cleanup_failures(batch_project, monkeypatch):
    from agentloom.app import Application
    from agentloom.execution.concurrency import WorkItem
    from agentloom.execution.process import run_registered_process

    root, application, _worker, probe, _write = batch_project
    fault = root / "fail-exit-write"
    fault.touch()
    injection = root / "filesystem-fault"
    injection.mkdir()
    (injection / "sitecustomize.py").write_text(
        "import errno,os\nfrom pathlib import Path\n_replace=os.replace\n"
        f"_fault=Path({str(fault)!r})\n"
        "def replace(source,target,*args,**kwargs):\n"
        "    if _fault.exists() and Path(target).name=='exited.json':\n"
        "        raise OSError(errno.EIO,'current native cleanup write failure')\n"
        "    return _replace(source,target,*args,**kwargs)\n"
        "os.replace=replace\n"
    )
    monkeypatch.setenv("PYTHONPATH", str(injection) + os.pathsep + os.environ.get("PYTHONPATH", ""))
    original = KeyboardInterrupt("preserved native control")
    earlier = OSError(28, "earlier cleanup error")
    original.managed_cleanup_errors = (earlier,)
    communicate = subprocess.Popen.communicate

    def interrupted_communicate(process, *args, **kwargs):
        if len(process.args) > 1 and str(process.args[1]).endswith("_process_guardian.py"):
            raise original  # Native communication boundary; both real closes still run.
        return communicate(process, *args, **kwargs)

    monkeypatch.setattr(subprocess.Popen, "communicate", interrupted_communicate)
    probe.behavior = lambda _payload: run_registered_process(
        [sys.executable, "-c", "import time;time.sleep(120)"], env=dict(os.environ),
        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    with Application.open(application, project_root=root, file_logging=False) as app:
        try:
            with pytest.raises(KeyboardInterrupt) as caught:
                app.worker("workflows/worker.yaml").map([WorkItem("control", {"value": 1})])
            assert caught.value is original
            assert original.managed_cleanup_errors[0] is earlier
            assert len(original.managed_cleanup_errors) == 3
            assert all(error.errno == 5 for error in original.managed_cleanup_errors[1:])
            diagnostics = original.worker_outcome.cleanup_errors
            assert any("earlier cleanup error" in entry.message for entry in diagnostics)
            assert sum("current native cleanup write failure" in entry.message for entry in diagnostics) >= 2
        finally:
            fault.unlink()  # Root teardown repairs only after public error observations.


def _alive(identity):
    try:
        process = psutil.Process(identity["pid"])
        return process.create_time() == identity["creation"] and process.status() != psutil.STATUS_ZOMBIE
    except psutil.Error:
        return False


def _marker(path, host, identities):
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        try:
            for p in psutil.Process(host.pid).children(recursive=True):
                identity = {"pid": p.pid, "creation": p.create_time()}
                if identity not in identities:
                    identities.append(identity)
        except psutil.Error:
            pass
        if path.exists():
            try:
                return json.loads(path.read_text())
            except json.JSONDecodeError:
                pass
        if host.poll() is not None:
            raise AssertionError(f"Application probe exited before readiness: {host.returncode}")
        time.sleep(.02)
    raise AssertionError("Native readiness watchdog")


def _safety(host, identities):
    # This runs only after product observations/assertions; never acceptance evidence.
    if host.poll() is None:
        host.kill()
        host.wait(timeout=5)
    for identity in reversed(identities):
        if _alive(identity):
            try:
                psutil.Process(identity["pid"]).kill()
            except psutil.Error:
                pass


@pytest.mark.parametrize("backend", ["registered", "captured", "fds"])
def test_default_map_reclaims_native_tree_after_top_python_sigkill(tmp_path, backend):
    root = Path(__file__).resolve().parents[2]
    identities = []
    with (tmp_path / "host.log").open("wb") as log:
        host = subprocess.Popen([sys.executable, "-m", "test_support.python_owner_host", str(tmp_path), backend],
                                cwd=root, stdout=log, stderr=log)
        try:
            marker = _marker(tmp_path / "native.json", host, identities)
            for identity in ({"pid": marker["pid"], "creation": marker["creation"]},
                             {"pid": marker["child_pid"], "creation": marker["child_creation"]}):
                if identity not in identities:
                    identities.append(identity)
            descriptions = list(tmp_path.rglob("native_resources/*/description.json"))
            assert len(descriptions) == 1, "Actual guardian identity is required before fault injection"
            anchor = json.loads(descriptions[0].read_text())["anchor"]
            identity = {"pid": anchor["pid"], "creation": anchor["creation"]}
            if identity not in identities:
                identities.append(identity)
            assert _alive({"pid": marker["child_pid"], "creation": marker["child_creation"]})
            os.kill(host.pid, signal.SIGKILL)
            host.wait(timeout=5)
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline and any(_alive(p) for p in identities):
                time.sleep(.02)
            survivors = [p for p in identities if _alive(p)]
            (tmp_path / "automatic-exit-observation.json").write_text(json.dumps({"survivors": survivors}))
            assert survivors == [], "Managed native descendants survived top-level Python SIGKILL"
            with socket.socket() as connection:
                assert connection.connect_ex(("127.0.0.1", marker["port"])) != 0
        finally:
            _safety(host, identities)


def test_application_native_owner_uses_short_private_endpoint_with_deep_trial_tmpdir(batch_project, monkeypatch):
    from agentloom.app import Application
    from agentloom.execution.concurrency import WorkItem
    from agentloom.execution.process import run_registered_process

    root, application, _worker, probe, _write = batch_project
    temporary = root / ("deep-trial-" * 12) / "tmp"
    temporary.mkdir(parents=True)
    assert len(os.fsencode(temporary)) > 108
    monkeypatch.setenv("TMPDIR", str(temporary))

    def behavior(payload):
        process = run_registered_process([sys.executable, "-c", "print('owned')"], env=dict(os.environ),
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        assert process.returncode == 0
        return payload

    probe.behavior = behavior
    with Application.open(application, project_root=root, file_logging=False) as app:
        outcome = app.worker("workflows/worker.yaml").invoke(WorkItem("short-socket", {"value": 1}))
    assert outcome.status == "success", outcome.to_dict()
    descriptions = list(root.rglob("native_resources/*/description.json"))
    assert descriptions
    for path in descriptions:
        descriptor = json.loads(path.read_text())
        assert len(os.fsencode(descriptor["endpoint"])) < 108
        assert json.loads(path.with_name("exited.json").read_text())["physical_exit_confirmed"]
        assert not Path(descriptor["endpoint"]).exists()


def test_public_harness_refresh_reclaims_helper_tree_after_parent_sigkill(tmp_path):
    from agentloom.app.harnessing.isolation import IsolationUnavailable, preflight_isolation

    try:
        preflight_isolation()
    except IsolationUnavailable as error:
        pytest.skip(str(error))
    identities = []
    with (tmp_path / "host.log").open("wb") as log:
        host = subprocess.Popen([sys.executable, "-m", "test_support.harness_oauth_owner_host", str(tmp_path)],
            cwd=Path(__file__).resolve().parents[2], stdout=log, stderr=log)
        try:
            marker = _marker(tmp_path / "refresh.json", host, identities)
            identities.extend([{"pid": marker["pid"], "creation": marker["creation"]},
                {"pid": marker["child_pid"], "creation": marker["child_creation"]}])
            assert _alive(identities[-1])
            # The real public controller reached credential capture before
            # admitting a business Trial; this helper is still owned.
            assert not list((tmp_path / "experiments").rglob("trials/*/process/input.json"))
            os.kill(host.pid, signal.SIGKILL)
            host.wait(timeout=5)
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline and any(_alive(item) for item in identities):
                time.sleep(.02)
            assert [item for item in identities if _alive(item)] == []
        finally:
            _safety(host, identities)


def test_native_start_failure_preserves_os_cause_without_unconfirmed_cleanup(batch_project, monkeypatch):
    from agentloom.app import Application
    from agentloom.execution.concurrency import WorkItem
    from agentloom.execution.process import spawn_registered_process

    root, application, _worker, probe, _write = batch_project
    native_popen = subprocess.Popen

    def unavailable_executable(argv, *args, **options):
        if len(argv) > 1 and str(argv[1]).endswith("_process_guardian.py"):
            options["executable"] = str(root / "missing-python-executable")
        return native_popen(argv, *args, **options)

    monkeypatch.setattr(subprocess, "Popen", unavailable_executable)

    def behavior(_payload):
        spawn_registered_process([sys.executable, "-c", "print(1)"], env=dict(os.environ))

    probe.behavior = behavior
    with Application.open(application, project_root=root, file_logging=False) as app:
        receipt = app.worker("workflows/worker.yaml").map([WorkItem("native", {"value": 1})])
    outcome = receipt.outcomes[0]
    assert outcome.status == "failed" and outcome.error.error_type == "FileNotFoundError"
    assert outcome.cleanup_errors == (), "No process existed; cleanup uncertainty was fabricated"


@pytest.mark.parametrize("text", [False, True])
def test_default_map_preserves_native_stdio_root_result_and_root_first_cleanup(batch_project, text):
    from agentloom.app import Application
    from agentloom.execution.concurrency import WorkItem
    from agentloom.execution.process import spawn_registered_process

    root, application, _worker, probe, _write = batch_project
    payload = "中文 stdin\n" * 20000
    identities = []
    command = """
import json,os,signal,subprocess,sys
import psutil
child = subprocess.Popen([sys.executable,'-c',
 'import signal,time;signal.signal(signal.SIGTERM,signal.SIG_IGN);time.sleep(120)'])
print(json.dumps({'pid':os.getpid(),'child_pid':child.pid,
 'child_creation':psutil.Process(child.pid).create_time()}), flush=True)
data=sys.stdin.buffer.read()
sys.stdout.buffer.write(data);sys.stdout.buffer.flush()
sys.stderr.buffer.write(b'actual native stderr');sys.stderr.buffer.flush()
sys.exit(7)
"""

    def behavior(_payload):
        process, _close = spawn_registered_process(
            [sys.executable, "-c", command], env=dict(os.environ), text=text,
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )
        output, error = process.communicate(payload if text else payload.encode(), timeout=10)
        if not text:
            output, error = output.decode(), error.decode()
        marker, data = output.split("\n", 1)
        marker = json.loads(marker)
        identities.append({"pid": marker["child_pid"], "creation": marker["child_creation"]})
        assert process.pid == marker["pid"], "The public handle must identify the native root"
        assert process.returncode == 7, "The native exit code must not be replaced by the guardian's code"
        assert data == payload and error == "actual native stderr"
        return {"code": process.returncode, "size": len(data)}

    probe.behavior = behavior
    with Application.open(application, project_root=root, file_logging=False) as app:
        receipt = app.worker("workflows/worker.yaml").map([WorkItem("stdio", {"value": 1})])
    assert receipt.outcomes[0].status == "success", receipt.to_dict()
    assert identities and not any(_alive(identity) for identity in identities)


def test_cancel_during_real_bootstrap_construction_never_dispatches_native_command(batch_project, monkeypatch):
    from agentloom.app import Application
    from agentloom.execution import managed_process
    from agentloom.execution.concurrency import WorkItem
    from agentloom.execution.resources import close_run_resources

    root, application, _worker, probe, _write = batch_project
    marker = root / "native-must-not-start"
    startup, release = Event(), Event()
    handles, bootstraps, failures = [], [], []
    original_popen, original_admit = subprocess.Popen, managed_process.NativeProcessScope.admit

    def admit(scope, process):
        original_admit(scope, process)
        handles.append(process)

    def popen(argv, *args, **options):
        process = original_popen(argv, *args, **options)
        if len(argv) > 1 and str(argv[1]).endswith("_process_guardian.py"):
            bootstraps.append({"pid": process.pid, "creation": psutil.Process(process.pid).create_time()})
            startup.set()
            assert release.wait(timeout=10)
        return process

    monkeypatch.setattr(managed_process.NativeProcessScope, "admit", admit)
    monkeypatch.setattr(subprocess, "Popen", popen)

    def behavior(_payload):
        context = copy_context()

        def cancel():
            assert startup.wait(timeout=10)
            try:
                context.run(close_run_resources)
            except BaseException as error:
                failures.append(error)

        def allow_bootstrap():
            assert startup.wait(timeout=10)
            deadline = time.monotonic() + 5
            # The private flag is only a fault-injection barrier. Acceptance
            # observes the public outcome and whether the command ever ran.
            while not handles[0]._stopped and time.monotonic() < deadline:
                time.sleep(.005)
            release.set()

        closer, releaser = Thread(target=cancel), Thread(target=allow_bootstrap)
        closer.start()
        releaser.start()
        try:
            managed_process.launch_owned_process(
                [sys.executable, "-c", f"from pathlib import Path;Path({str(marker)!r}).touch()"],
                env=dict(os.environ), stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
        finally:
            release.set()
            closer.join(timeout=10)
            releaser.join(timeout=10)
            assert not closer.is_alive() and not releaser.is_alive()

    probe.behavior = behavior
    with Application.open(application, project_root=root, file_logging=False) as app:
        receipt = app.worker("workflows/worker.yaml").map([WorkItem("startup", {"value": 1})])
    assert receipt.outcomes[0].status == "failed", receipt.to_dict()
    assert receipt.outcomes[0].error.error_type == "InterruptedError", receipt.to_dict()
    assert not marker.exists() and not any(_alive(identity) for identity in bootstraps)
    assert not failures, failures


@pytest.mark.parametrize("tool", ["grep", "glob", "snapshot"])
def test_application_search_and_profile_helpers_join_default_native_owner(batch_project, tool):
    from agentloom.app import Application
    from agentloom.execution.concurrency import WorkItem
    from agentloom.runtimes.smolagents.tools.search.glob_tool.glob_tool import glob_search
    from agentloom.runtimes.smolagents.tools.search.grep_tool.grep_tool import grep_search
    from agentloom.runtimes.smolagents.tools.shell.shell_snapshot import create_snapshot, remove_snapshot

    root, application, _worker, probe, _write = batch_project
    (root / "target.txt").write_text("native-search-target\n")

    def behavior(_payload):
        if tool == "grep":
            assert "native-search-target" in grep_search("native-search-target", path=str(root))
        elif tool == "glob":
            assert "target.txt" in glob_search("*.txt", path=str(root))
        else:
            snapshot = create_snapshot("/bin/bash", env={"PATH": os.environ["PATH"], "HOME": str(root)})
            try:
                assert snapshot is not None
            finally:
                remove_snapshot(snapshot)
        return {"tool": tool}

    probe.behavior = behavior
    with Application.open(application, project_root=root, file_logging=False) as app:
        receipt = app.worker("workflows/worker.yaml").map([WorkItem("helper", {"value": 1})])
    assert receipt.outcomes[0].status == "success", receipt.to_dict()
    descriptions = list(root.rglob("native_resources/*/description.json"))
    assert descriptions, "Task helper bypassed the common native resource admission"
    for description in descriptions:
        assert json.loads(description.with_name("exited.json").read_text())["physical_exit_confirmed"]


@pytest.mark.parametrize("recovered,stale_observation", [(False, False), (True, False), (True, True)])
def test_application_retries_a_real_exit_receipt_write_failure_without_inventing_exit(
    batch_project, monkeypatch, recovered, stale_observation,
):
    from agentloom.app import Application
    from agentloom.execution.concurrency import WorkItem
    from agentloom.execution.managed_process import ResourceCleanupError, recover_owned_process
    from agentloom.execution.process import spawn_registered_process

    root, application, _worker, probe, _write = batch_project
    resources, anchors = [], []
    release = root / "release-exit-receipt"
    if stale_observation:
        injection = root / "exit-write-barrier"
        injection.mkdir()
        (injection / "sitecustomize.py").write_text(
            "import os,time\nfrom pathlib import Path\n_replace=os.replace\n"
            f"_release=Path({str(release)!r})\n"
            "def replace(source,target,*args,**kwargs):\n"
            "    target=Path(target)\n"
            "    if target.name=='exited.json' and not target.is_dir():\n"
            "        while not _release.exists():time.sleep(.005)\n"
            "    return _replace(source,target,*args,**kwargs)\n"
            "os.replace=replace\n"
        )
        monkeypatch.setenv("PYTHONPATH", str(injection) + os.pathsep + os.environ.get("PYTHONPATH", ""))

    def behavior(_payload):
        process, _close = spawn_registered_process(
            [sys.executable, "-c", "import time;time.sleep(120)"], env=dict(os.environ),
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        directory = process.receipt_dir
        resources.append(directory)
        anchors.append(json.loads((directory / "description.json").read_text())["anchor"])
        obstruction = directory / "exited.json"
        obstruction.mkdir()  # Real os.replace failure in the independent owner.
        try:
            process.request_stop()
            with pytest.raises(ResourceCleanupError) as caught:
                process.wait_exited(timeout=5)
            assert not process.observe()["physical_exit_confirmed"]
            assert isinstance(caught.value.errno, int), "Original OS error code must survive the control channel"
        finally:
            obstruction.rmdir()
        if recovered:
            recovery = recover_owned_process(directory)
            recovery.request_stop()
            if stale_observation:
                original = Path.is_file
                raced = []

                def is_file(path):
                    observed = original(path)
                    if path == obstruction and not observed and not raced:
                        raced.append(True)
                        release.touch()
                        deadline = time.monotonic() + 5
                        # Commit/retire the real independent guardian after
                        # the reader has already observed the missing proof.
                        while time.monotonic() < deadline and (
                            not original(path) or psutil.pid_exists(anchors[-1]["pid"])
                        ):
                            time.sleep(.005)
                        assert original(path) and not psutil.pid_exists(anchors[-1]["pid"])
                    return observed

                monkeypatch.setattr(Path, "is_file", is_file)
            assert recovery.wait_exited(timeout=5)["physical_exit_confirmed"]
            if stale_observation:
                assert raced, "The actual receipt/anchor observation race was not exercised"
                monkeypatch.setattr(Path, "is_file", original)
        else:
            _close()
        return {"first_cleanup_error": str(caught.value)}

    probe.behavior = behavior
    try:
        with Application.open(application, project_root=root, file_logging=False) as app:
            receipt = app.worker("workflows/worker.yaml").map([WorkItem("retry", {"value": 1})])
        assert receipt.outcomes[0].status == "success", receipt.to_dict()
        assert resources and anchors
        assert all(json.loads((directory / "exited.json").read_text())["physical_exit_confirmed"] for directory in resources)
        assert not any(_alive(identity) for identity in anchors)
    finally:
        release.touch()
        # Last-resort test cleanup is never used to satisfy the assertions.
        for identity in anchors:
            if _alive(identity):
                psutil.Process(identity["pid"]).kill()


def test_recovery_control_rejects_a_fragmented_wrong_generation_without_losing_owner(batch_project):
    from agentloom.app import Application
    from agentloom.execution.concurrency import WorkItem
    from agentloom.execution.process import spawn_registered_process

    root, application, _worker, probe, _write = batch_project
    identities = []

    def behavior(_payload):
        process, _close = spawn_registered_process(
            [sys.executable, "-c", "import time;time.sleep(120)"], env=dict(os.environ),
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        identities.append({"pid": process.pid, "creation": psutil.Process(process.pid).create_time()})
        description = json.loads((process.receipt_dir / "description.json").read_text())
        identities.append(description["anchor"])
        with socket.socket(socket.AF_UNIX) as channel:
            channel.connect(description["endpoint"])
            channel.sendall(b'{"op":')
            time.sleep(.03)  # Explicitly fragment the stream request.
            channel.sendall(b'"stop","generation":"wrong-generation"}\n')
        time.sleep(.03)
        assert process.observe()["status"] == "running"
        assert all(_alive(identity) for identity in identities)
        return {"alive_after_wrong_generation": True}

    probe.behavior = behavior
    try:
        with Application.open(application, project_root=root, file_logging=False) as app:
            receipt = app.worker("workflows/worker.yaml").map([WorkItem("control", {"value": 1})])
        assert receipt.outcomes[0].status == "success", receipt.to_dict()
        assert not any(_alive(identity) for identity in identities)
    finally:
        for identity in identities:
            if _alive(identity):
                psutil.Process(identity["pid"]).kill()


def test_native_root_result_survives_its_receipt_write_failure_and_retry(batch_project):
    from agentloom.app import Application
    from agentloom.execution.concurrency import WorkItem
    from agentloom.execution.managed_process import ResourceCleanupError
    from agentloom.execution.process import spawn_registered_process

    root, application, _worker, probe, _write = batch_project
    resources, anchors = [], []

    def behavior(_payload):
        process, _close = spawn_registered_process(
            [sys.executable, "-c", "import time,sys;time.sleep(.2);sys.exit(7)"], env=dict(os.environ),
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        resources.append(process.receipt_dir)
        anchors.append(json.loads((process.receipt_dir / "description.json").read_text())["anchor"])
        obstruction = process.receipt_dir / "root-result.json"
        obstruction.mkdir()
        try:
            assert process.wait(timeout=5) == 7, "Observed native exit code is independent of receipt storage"
            with pytest.raises(ResourceCleanupError) as caught:
                process.wait_exited(timeout=5)
            assert isinstance(caught.value.errno, int)
            assert not process.observe()["physical_exit_confirmed"]
        finally:
            obstruction.rmdir()
        _close()
        return {"actual_root_code": process.returncode}

    probe.behavior = behavior
    try:
        with Application.open(application, project_root=root, file_logging=False) as app:
            receipt = app.worker("workflows/worker.yaml").map([WorkItem("result", {"value": 1})])
        assert receipt.outcomes[0].status == "success", receipt.to_dict()
        assert all(json.loads((directory / "root-result.json").read_text())["returncode"] == 7 for directory in resources)
        assert not any(_alive(identity) for identity in anchors)
    finally:
        for identity in anchors:
            if _alive(identity):
                psutil.Process(identity["pid"]).kill()


@pytest.mark.parametrize("retry_from", ["live", "recovered"])
def test_application_exposes_current_cleanup_error_from_both_stop_callers(batch_project, monkeypatch, retry_from):
    from agentloom.app import Application
    from agentloom.execution.concurrency import WorkItem
    from agentloom.execution.managed_process import ResourceCleanupError, recover_owned_process
    from agentloom.execution.process import spawn_registered_process

    root, application, _worker, probe, _write = batch_project
    fault, gate, paused = (root / name for name in ("block-exit-write", "hold-select", "select-paused"))
    fault.touch()
    injection = root / "filesystem-fault"
    injection.mkdir()
    # Real guardian, command, sockets and os.replace. Only the filesystem fault
    # and select boundary are controlled, so both STOPs share a readiness batch.
    (injection / "sitecustomize.py").write_text(
        "import errno,os,select,time\nfrom pathlib import Path\n"
        "_replace=os.replace\n_select=select.select\n"
        f"_fault=Path({str(fault)!r})\n_gate=Path({str(gate)!r})\n_paused=Path({str(paused)!r})\n"
        "def replace(source,target,*args,**kwargs):\n"
        "    if _fault.exists() and Path(target).name=='exited.json':\n"
        "        raise OSError(errno.EIO,'native exit receipt write failure')\n"
        "    return _replace(source,target,*args,**kwargs)\n"
        "os.replace=replace\n"
        "def ready(read,write,error,timeout=None):\n"
        "    if len(read)==2 and _gate.exists():\n"
        "        _paused.touch()\n"
        "        while _gate.exists(): time.sleep(.005)\n"
        "    return _select(read,write,error,timeout)\n"
        "select.select=ready\n"
    )
    monkeypatch.setenv("PYTHONPATH", str(injection) + os.pathsep + os.environ.get("PYTHONPATH", ""))
    resources, anchors = [], []
    queued, failures = Event(), []
    original_sendall = socket.socket.sendall

    def sendall(channel, data, *args, **kwargs):
        result = original_sendall(channel, data, *args, **kwargs)
        message = json.loads(data)
        if message.get("op") == "stop" and message.get("generation"):
            queued.set()  # The recovery frame actually reached its real socket.
        return result

    monkeypatch.setattr(socket.socket, "sendall", sendall)

    def wait(predicate):
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            if predicate():
                return
            time.sleep(.01)
        raise AssertionError("Native cleanup observation watchdog")

    def behavior(_payload):
        process, _close = spawn_registered_process(
            [sys.executable, "-c", "import time,sys;time.sleep(.15);sys.exit(7)"], env=dict(os.environ),
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        directory = process.receipt_dir
        resources.append(directory)
        anchors.append(json.loads((directory / "description.json").read_text())["anchor"])
        wait(lambda: process.observe()["status"] == "cleanup_unconfirmed")
        first_error = process.observe()["error"]
        assert first_error["errno"] == 5 and process.wait(timeout=5) == 7
        recovery = recover_owned_process(directory)

        def stop_recovered():
            try:
                recovery.request_stop()
            except BaseException as error:
                failures.append(error)

        gate.touch()
        wait(paused.exists)
        process.request_stop()
        stopper = Thread(target=stop_recovered)
        stopper.start()
        try:
            assert queued.wait(timeout=5)
        finally:
            gate.unlink(missing_ok=True)
            stopper.join(timeout=5)
        assert not stopper.is_alive() and not failures, failures
        error_path = directory / "cleanup-error.json"
        wait(lambda: json.loads(error_path.read_text()).get("request_id") is not None)
        current_error = json.loads(error_path.read_text())
        wait(lambda: process.observe()["error"] == current_error)
        assert current_error["errno"] == 5 and current_error["first_error"] == first_error
        assert process.observe()["status"] == "cleanup_unconfirmed"
        assert not process.observe()["physical_exit_confirmed"] and _alive(anchors[-1])
        with pytest.raises(ResourceCleanupError) as caught:
            recovery.wait_exited(timeout=1)
        assert caught.value.errno == 5, "Recovery must expose the current guardian error's original errno"
        fault.unlink()
        target = process if retry_from == "live" else recovery
        target.request_stop()
        assert target.wait_exited(timeout=5)["physical_exit_confirmed"]
        return {"actual_root_code": process.returncode, "cleanup_errno": current_error["errno"]}

    probe.behavior = behavior
    try:
        with Application.open(application, project_root=root, file_logging=False) as app:
            receipt = app.worker("workflows/worker.yaml").map([WorkItem("two-callers", {"value": 1})])
        assert receipt.outcomes[0].status == "success", receipt.to_dict()
        assert all(json.loads((directory / "exited.json").read_text())["physical_exit_confirmed"] for directory in resources)
        assert not any(_alive(identity) for identity in anchors)
    finally:
        gate.unlink(missing_ok=True)
        fault.unlink(missing_ok=True)
        for directory in resources:
            try:
                recovery = recover_owned_process(directory)
                recovery.request_stop()
                recovery.wait_exited(timeout=5)
            except ResourceCleanupError:
                pass  # Safety is never acceptance evidence.
        for identity in anchors:
            if _alive(identity):
                psutil.Process(identity["pid"]).kill()


@pytest.mark.parametrize("later_caller", ["live", "recovered"])
def test_application_recovered_wait_preserves_current_error_from_a_later_caller(batch_project, later_caller):
    from agentloom.app import Application
    from agentloom.execution.concurrency import WorkItem
    from agentloom.execution.managed_process import ResourceCleanupError, recover_owned_process
    from agentloom.execution.process import spawn_registered_process

    root, application, _worker, probe, _write = batch_project
    resources, anchors = [], []

    def behavior(_payload):
        process, _close = spawn_registered_process(
            [sys.executable, "-c", "import time;time.sleep(120)"], env=dict(os.environ),
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        directory = process.receipt_dir
        resources.append(directory)
        anchors.append(json.loads((directory / "description.json").read_text())["anchor"])
        obstruction = directory / "exited.json"
        obstruction.mkdir()
        recovery = recover_owned_process(directory)

        def wait_for_error(previous_request=None):
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                error = recovery.observe()["error"]
                if error is not None and error["request_id"] != previous_request:
                    return error
                time.sleep(.01)
            raise AssertionError("Native cleanup error watchdog")

        try:
            recovery.request_stop()
            first_error = wait_for_error()
            other = process if later_caller == "live" else recover_owned_process(directory)
            other.request_stop()
            current_error = wait_for_error(first_error["request_id"])
            with pytest.raises(ResourceCleanupError) as caught:
                recovery.wait_exited(timeout=1)
            assert caught.value.detail == current_error, "A later same-scope error is authoritative for every caller"
            assert isinstance(caught.value.errno, int)
            assert not recovery.observe()["physical_exit_confirmed"] and _alive(anchors[-1])
        finally:
            obstruction.rmdir()
        recovery.request_stop()
        assert recovery.wait_exited(timeout=5)["physical_exit_confirmed"]
        return {"current_errno": current_error["errno"]}

    probe.behavior = behavior
    try:
        with Application.open(application, project_root=root, file_logging=False) as app:
            receipt = app.worker("workflows/worker.yaml").map([WorkItem("recovered-callers", {"value": 1})])
        assert receipt.outcomes[0].status == "success", receipt.to_dict()
        assert not any(_alive(identity) for identity in anchors)
    finally:
        for directory in resources:
            try:
                recovery = recover_owned_process(directory)
                recovery.request_stop()
                recovery.wait_exited(timeout=5)
            except ResourceCleanupError:
                pass
        for identity in anchors:
            if _alive(identity):
                psutil.Process(identity["pid"]).kill()
