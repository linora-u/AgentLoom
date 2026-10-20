"""Default native resource ownership using Python and the existing psutil.

Factories, Context and business code remain live in the caller. Only trusted
native commands/descriptors cross the independent Python guardian's channel.
"""
from __future__ import annotations

import json
import os
import signal
import socket
import subprocess
import sys
from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path
from threading import Condition, Lock, Thread
from time import monotonic, sleep
from uuid import uuid4

import psutil

_SCOPE: ContextVar[NativeProcessScope | None] = ContextVar("agentloom_native_resource_scope", default=None)


class ResourceCleanupError(RuntimeError):
    category = "infrastructure"

    def __init__(self, message, detail=None):
        super().__init__(message)
        self.detail = dict(detail or {})
        for name in ("errno", "sqlite_errorcode", "sqlite_errorname"):
            if name in self.detail:
                setattr(self, name, self.detail[name])


def validate_process_support():
    """Preflight without spawning resources, downloading or changing environment."""
    if sys.platform not in {"darwin", "linux"}:
        raise RuntimeError("Managed native execution requires macOS or Linux")
    if sys.platform == "linux":
        import ctypes

        if not hasattr(ctypes.CDLL(None), "prctl"):
            raise RuntimeError("Managed native execution requires Linux child subreaper support")
    if not Path(__file__).with_name("_process_guardian.py").is_file():
        raise RuntimeError("Managed native process bootstrap is missing")


def _alive(identity):
    try:
        return psutil.Process(identity["pid"]).create_time() == identity["creation"]
    except psutil.NoSuchProcess:
        return False


class OwnedProcess:
    """Root-process observations plus independent resource cleanup confirmation."""

    def __init__(self, argv, *, env, cwd=None, stdin=None, stdout=None, stderr=None,
                 text=False, encoding=None, errors=None, receipt_dir=None, gated=False, bufsize=-1, pass_fds=()):
        if sys.platform not in {"darwin", "linux"}:
            raise RuntimeError("Managed native execution requires macOS or Linux")
        if isinstance(argv, (str, bytes)) or not argv:
            raise TypeError("Managed native command requires nonempty argv")
        self.args = [os.fsdecode(arg) for arg in argv]
        inherited_fds = tuple(int(fd) for fd in pass_fds)
        if any(fd < 3 for fd in inherited_fds):
            raise ValueError("Private child descriptors must not replace stdio")
        for fd in inherited_fds:
            os.fstat(fd)
        self.pid = self.returncode = None
        self.receipt_dir = Path(receipt_dir) if receipt_dir is not None else None
        self._condition = Condition()
        self._send_lock = Lock()
        self._close_lock = Lock()
        self._launch_lock = Lock()
        self._stopped = False
        self._owner_ready = self._ready = self._extinct = self._ended = False
        self._error = None
        self._error_event = None
        self._cleanup_request = None
        self._cleanup_sequence = None
        self._generation = uuid4().hex
        self._communication = None
        self._guardian = self._channel = self._reader = None
        self.stdin = self.stdout = self.stderr = None
        self._released = False
        self._constructing = True
        scope = _SCOPE.get()
        try:
            with self._launch_lock:
                from agentloom.execution.process_scope import guardian_subprocess_env

                guardian_env = guardian_subprocess_env()
                if scope is not None:
                    scope.admit(self)
                local, remote = socket.socketpair()
                self._channel = local
                try:
                    self._guardian = subprocess.Popen(
                        [sys.executable, str(Path(__file__).with_name("_process_guardian.py")),
                         str(remote.fileno()), self._generation],
                        stdin=stdin, stdout=stdout, stderr=stderr, text=text, encoding=encoding, errors=errors,
                        env=guardian_env, bufsize=bufsize, pass_fds=(remote.fileno(), *inherited_fds),
                        close_fds=True, start_new_session=True,
                    )
                finally:
                    remote.close()
                self.stdin, self.stdout, self.stderr = self._guardian.stdin, self._guardian.stdout, self._guardian.stderr
                self._reader = Thread(target=self._read, daemon=True, name="native-resource-control")
                self._reader.start()
                # Registration is always closed-gate. Even an ungated caller
                # gets a separate release after its owner handshake completes.
                self._send({"op": "start", "argv": self.args, "env": dict(env), "cwd": os.fspath(cwd) if cwd else None,
                            "generation": self._generation, "gated": True, "pass_fds": inherited_fds,
                            "receipt_dir": str(self.receipt_dir) if self.receipt_dir else None,
                            "parent": {"pid": os.getpid(), "creation": psutil.Process().create_time()}})
                if self._stopped:
                    self._send({"op": "stop"})
            with self._condition:
                if not self._condition.wait_for(lambda: self._owner_ready or self._error or self._ended, 20):
                    raise ResourceCleanupError("Native owner handshake did not complete")
                self._raise_error()
            if not gated:
                self.release()
        except BaseException as error:
            if not self._released:
                # Only our bootstrap exists before release. It cannot run the
                # native command, so safely retire it and certify no start.
                if self._guardian is not None:
                    if self._guardian.poll() is None:
                        self._guardian.kill()
                    self._guardian.wait()
                if self.receipt_dir is not None and (self.receipt_dir / "description.json").exists():
                    from agentloom.execution._process_guardian import atomic_json

                    try:
                        atomic_json(self.receipt_dir / "exited.json", {
                            "physical_exit_confirmed": True, "generation": self._generation,
                        })
                    except OSError as persistence_error:
                        # Actual bootstrap wait still proves no start to this
                        # caller. Keep persistence failure as a separate cause;
                        # recovery will never accept a missing disk certificate.
                        error.managed_cleanup_errors = (persistence_error,)
                with self._condition:
                    self._extinct = self._ended = True
                    self._condition.notify_all()
                if self._channel is not None:
                    self._channel.close()
                error.physical_exit_confirmed = True
                error.not_started = True
            raise
        finally:
            with self._condition:
                self._constructing = False
                self._condition.notify_all()

    def _send(self, value):
        with self._send_lock:
            try:
                self._channel.sendall(json.dumps(value).encode() + b"\n")
            except OSError as error:
                raise ResourceCleanupError("Native resource control channel failed") from error

    def _read(self):
        try:
            with self._channel.makefile("rb") as stream:
                for line in stream:
                    value = json.loads(line)
                    with self._condition:
                        event = value["event"]
                        if event == "owner_ready":
                            self._owner_ready = True
                        elif event == "ready":
                            self.pid, self._ready = value["pid"], True
                        elif event == "root_exit":
                            self.returncode = value["returncode"]
                        elif event == "extinct":
                            self._extinct = value["physical_exit_confirmed"] is True and value["generation"] == self._generation
                        elif event == "cleanup_started":
                            # The guardian, rather than the requesting caller,
                            # orders retries for this one native scope.
                            self._cleanup_sequence = value["cleanup_sequence"]
                            if value["request_id"] == self._cleanup_request:
                                self._cleanup_request = None
                            if self._cleanup_request is None and self._error_event == "cleanup_error":
                                self._error = None
                        elif event == "startup_error" or (event == "cleanup_error" and (
                            self._cleanup_request is None
                            and value["error"]["cleanup_sequence"] == self._cleanup_sequence
                        )):
                            self._error = value["error"]
                            self._error_event = event
                        self._condition.notify_all()
        except (OSError, ValueError, KeyError) as error:
            self._error = {"error_type": type(error).__name__, "message": str(error), "category": "infrastructure"}
        finally:
            if self._extinct and self._guardian is not None:
                try:
                    self._guardian.wait(timeout=20)
                except subprocess.TimeoutExpired:
                    self._error = {"message": "Native anchor retirement remains unconfirmed",
                                   "error_type": "ResourceCleanupError", "category": "infrastructure"}
            with self._condition:
                self._ended = True
                self._condition.notify_all()

    def _raise_error(self):
        if self._error is not None:
            raise ResourceCleanupError(self._error["message"], self._error)
        if self._ended and not self._extinct:
            raise ResourceCleanupError("Native resource owner ended without an exit receipt")

    def release(self):
        with self._condition:
            if self._stopped:
                raise InterruptedError("Native startup admission is closed")
            self._released = True
            self._send({"op": "release"})
            if not self._condition.wait_for(lambda: self._ready or self._error or self._ended, 20):
                raise ResourceCleanupError("Native startup handshake did not complete")
            self._raise_error()

    def request_stop(self, sig="SIGKILL"):
        request_id = uuid4().hex
        with self._condition:
            self._stopped = True
        with self._launch_lock:
            if not self._extinct:
                with self._condition:
                    self._cleanup_request = request_id
                    self._error = None  # Prior failure remains in its original exception/receipt.
                self._send({"op": "stop", "signal": sig, "request_id": request_id})

    def observe(self):
        retired = self._guardian is None or self._guardian.poll() is not None
        return {"status": "exited" if self._extinct and retired else
                "cleanup_unconfirmed" if self._error or self._ended else "running" if self._ready else "starting",
                "physical_exit_confirmed": self._extinct and retired,
                "error": self._error, "root_result": None if self.returncode is None else {"returncode": self.returncode}}

    def poll(self):
        return self.returncode

    def wait(self, timeout=None):
        with self._condition:
            if not self._condition.wait_for(lambda: self.returncode is not None or self._error or self._ended, timeout):
                raise subprocess.TimeoutExpired(self.args, timeout)
            if self.returncode is None:
                self._raise_error()
                raise ResourceCleanupError("Native root result was not observed")
            return self.returncode

    def communicate(self, input=None, timeout=None):
        # The bootstrap's Popen owns only stdio descriptors. Its independent
        # reader observes control frames, so binary/text payloads never mix
        # with lifecycle messages and pipe backpressure cannot delay cleanup.
        deadline = None if timeout is None else monotonic() + timeout
        try:
            output = self._guardian.communicate(input, timeout)
            self.wait(None if deadline is None else max(0, deadline - monotonic()))
        except subprocess.TimeoutExpired as error:
            error.cmd = self.args
            raise
        return output

    def wait_exited(self, timeout=None):
        deadline = None if timeout is None else monotonic() + timeout
        with self._condition:
            if not self._condition.wait_for(
                lambda: self._extinct or (not self._constructing and (self._error or self._ended)), timeout,
            ):
                raise ResourceCleanupError("Native cleanup remains unconfirmed")
            if not self._extinct:
                self._raise_error()
                raise ResourceCleanupError("Native cleanup remains unconfirmed")
        try:
            if self._guardian is not None:
                self._guardian.wait(timeout=None if deadline is None else max(0, deadline - monotonic()))
        except subprocess.TimeoutExpired as error:
            raise ResourceCleanupError("Native anchor retirement remains unconfirmed") from error
        return self.observe()

    def close(self):
        with self._close_lock:
            if not self._extinct:
                try:
                    self.request_stop()
                except ResourceCleanupError:
                    pass  # A just-completed owner may have a receipt still in flight.
            self.wait_exited(timeout=20)
            for stream in (self.stdin, self.stdout, self.stderr):
                if stream is not None:
                    stream.close()
            if self._channel is not None:
                self._channel.close()
            if self._reader is not None:
                self._reader.join(timeout=1)

    def send_signal(self, sig):
        if sig not in {signal.SIGTERM, signal.SIGKILL}:
            raise ValueError("Managed resource cancellation supports TERM/KILL")
        self.request_stop("SIGTERM" if sig == signal.SIGTERM else "SIGKILL")

    def terminate(self):
        self.send_signal(signal.SIGTERM)

    def kill(self):
        self.send_signal(signal.SIGKILL)

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()


class RecoveredProcess:
    def __init__(self, receipt_dir):
        self.receipt_dir = Path(receipt_dir)
        self._cleanup_request = None
        self._cleanup_sequence = None
        self._stop_lock = Lock()
        self.description = json.loads((self.receipt_dir / "description.json").read_text())
        if self.description["version"] not in {1, 2}:
            raise ResourceCleanupError("Unknown native resource description version")

    def observe(self):
        proof = self.receipt_dir / "exited.json"
        error_path = self.receipt_dir / "cleanup-error.json"
        error = json.loads(error_path.read_text()) if error_path.exists() else None
        anchor_alive = _alive(self.description["anchor"])
        confirmed = (proof.is_file() and json.loads(proof.read_text()) == {
            "physical_exit_confirmed": True, "generation": self.description["generation"]}
            and not anchor_alive)
        root_path = self.receipt_dir / "root-result.json"
        return {"status": "exited" if confirmed else "cleanup_unconfirmed" if error or not anchor_alive
                else "running", "physical_exit_confirmed": confirmed,
                "error": error, "root_result": json.loads(root_path.read_text()) if root_path.exists() else None}

    def request_stop(self):
        with self._stop_lock:
            self._request_stop()

    def _request_stop(self):
        if self.observe()["physical_exit_confirmed"]:
            return
        if self.description["version"] != 2:
            raise ResourceCleanupError("Native resource owner does not support acknowledged cleanup")
        if not _alive(self.description["anchor"]):
            if self.observe()["physical_exit_confirmed"]:
                return
            raise ResourceCleanupError("Native anchor was lost without an exit certificate")
        request_id = uuid4().hex
        self._cleanup_request, self._cleanup_sequence = request_id, None
        try:
            with socket.socket(socket.AF_UNIX) as channel:
                channel.settimeout(1)
                channel.connect(self.description["endpoint"])
                channel.sendall(json.dumps({"op": "stop", "generation": self.description["generation"],
                                           "request_id": request_id}).encode() + b"\n")
                with channel.makefile("rb") as stream:
                    frame = stream.readline(4097)
                acknowledgement = json.loads(frame) if len(frame) <= 4096 and frame.endswith(b"\n") else None
                if (not isinstance(acknowledgement, dict) or acknowledgement.get("event") != "cleanup_started"
                        or acknowledgement.get("request_id") != request_id
                        or acknowledgement.get("generation") != self.description["generation"]
                        or type(acknowledgement.get("cleanup_sequence")) is not int
                        or acknowledgement["cleanup_sequence"] < 1):
                    raise ResourceCleanupError("Native cleanup acknowledgement remains unconfirmed")
                self._cleanup_sequence = acknowledgement["cleanup_sequence"]
        except (OSError, ValueError) as error:
            if self.observe()["physical_exit_confirmed"]:
                return
            detail = {"error_type": type(error).__name__, "message": str(error), "category": "infrastructure"}
            if getattr(error, "errno", None) is not None:
                detail["errno"] = error.errno
            raise ResourceCleanupError("Native cleanup acknowledgement remains unconfirmed", detail) from error

    def wait_exited(self, timeout=None):
        deadline = None if timeout is None else monotonic() + timeout
        while True:
            observed = self.observe()
            if observed["physical_exit_confirmed"]:
                return observed
            anchor_lost = not _alive(self.description["anchor"])
            if anchor_lost:
                # The owner writes its certificate before retiring. It may
                # have done both after the first filesystem observation;
                # recheck that final fact before reporting an older error.
                observed = self.observe()
                if observed["physical_exit_confirmed"]:
                    return observed
            detail = observed["error"] or {}
            # Ignore failures before our accepted retry, but retain the current
            # scope failure even when another caller initiated its latest STOP.
            if observed["status"] == "cleanup_unconfirmed" and (
                self._cleanup_request is None or anchor_lost
                or (self._cleanup_sequence is not None
                    and detail.get("cleanup_sequence", -1) >= self._cleanup_sequence)
            ):
                raise ResourceCleanupError(detail.get("message", "Native cleanup remains unconfirmed"), detail)
            if deadline is not None and monotonic() >= deadline:
                raise ResourceCleanupError("Native cleanup remains unconfirmed")
            sleep(.02)


def launch_owned_process(argv, **options):
    if options.pop("shell", False):
        argv = ["/bin/sh", "-c", *([argv] if isinstance(argv, (str, bytes)) else argv)]
    if options.get("receipt_dir") is None:
        from agentloom.execution import get_current_run_context

        context = get_current_run_context()
        if context is not None:
            options["receipt_dir"] = context.run_dir / "native_resources" / uuid4().hex
    return OwnedProcess(argv, **options)


def recover_owned_process(receipt_dir):
    return RecoveredProcess(receipt_dir)


class NativeProcessScope:
    def __init__(self):
        self.closed = False
        self.processes = []
        self.lock = Lock()

    def admit(self, process):
        with self.lock:
            if self.closed:
                raise InterruptedError("Native resource admission is closed")
            self.processes.append(process)

    def close(self):
        with self.lock:
            self.closed = True
            processes = tuple(self.processes)
        failures = []
        for process in reversed(processes):
            try:
                process.close()
            except BaseException as error:
                failures.append(error)
        if failures:
            error = BaseExceptionGroup("Native resource cleanup remains unconfirmed", failures)
            error.category = "infrastructure"
            error.managed_cleanup_errors = tuple(failures)
            raise error


def native_owner_active():
    return _SCOPE.get() is not None


@contextmanager
def managed_attempt_scope(*, independent=False):
    from agentloom.execution.process_scope import managed_process_group
    from agentloom.execution.resources import register_resource, unregister_resource

    if (not independent and _SCOPE.get() is not None) or managed_process_group() is not None:
        yield
        return
    scope = NativeProcessScope()
    key = f"native-attempt:{uuid4().hex}"
    register_resource(key, scope.close)
    token = _SCOPE.set(scope)
    try:
        if scope.closed:
            raise InterruptedError("Native resource admission is closed")
        yield
    finally:
        from agentloom.execution.cleanup import close_preserving_error

        def close_scope():
            scope.close()
            unregister_resource(key)

        try:
            close_preserving_error(close_scope)
        finally:
            _SCOPE.reset(token)
