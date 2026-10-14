"""AgentLoom's small Python resource guardian, independent of its caller.

Only stdlib and the existing psutil dependency are used. The guardian pins its
own group identity until every managed member has exited; it never schedules
business work. Descendants must remain in this owned process group.
"""
from __future__ import annotations

import ctypes
import json
import os
import select
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import psutil

SCOPE_FD_ENV = "AGENTLOOM_NATIVE_SCOPE_FD"


def atomic_json(path, value):
    pending = path.with_name(f".{path.name}-{os.getpid()}.pending")
    with pending.open("w", encoding="utf-8") as stream:
        os.chmod(pending, 0o600)
        json.dump(value, stream)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(pending, path)
    descriptor = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


class Guardian:
    def __init__(self, channel):
        if os.getpgrp() != os.getpid():
            raise RuntimeError("Resource guardian must own its process group")
        if sys.platform == "linux":
            # Adopt managed orphans so the guardian, rather than an unrelated
            # container PID1, can join their actual waitpid extinction.
            libc = ctypes.CDLL(None, use_errno=True)
            if libc.prctl(36, 1, 0, 0, 0):  # PR_SET_CHILD_SUBREAPER
                raise OSError(ctypes.get_errno(), "Cannot acquire child subreaper")
        self.channel = channel
        self.parent_lost = False
        self.command = None
        self.process = None
        self.root_result = None
        self.stopping = False
        self.cleanup_requested = False
        self.cleanup_at = 0
        self.receipt_dir = None
        self.description = None
        self.listener = None
        self.temporary = None
        self.known_members = set()

    def send(self, value):
        if not self.parent_lost:
            try:
                self.channel.sendall(json.dumps(value).encode() + b"\n")
            except OSError:
                self.parent_lost = True
                self.stop()

    def members(self):
        members = []
        for process in psutil.process_iter(["pid"]):
            if process.pid <= 0 or process.pid == os.getpid():
                continue
            try:
                if os.getpgid(process.pid) == os.getpid():
                    # psutil's signaling methods verify create_time against PID
                    # reuse. Keep this exact handle through freeze and kill.
                    process.create_time()
                    members.append(process)
                    self.known_members.add(process.pid)
            except (ProcessLookupError, psutil.NoSuchProcess):
                pass
            except (PermissionError, psutil.AccessDenied):
                if process.pid in self.known_members:
                    raise  # Known owned identities cannot silently disappear.
                if process.uids().real == os.getuid():
                    raise  # Potential managed member remains unconfirmed.
        return members

    def reap(self):
        if self.process is not None and self.root_result is None:
            result = self.process.poll()
            if result is not None:
                self.root_result = {"returncode": result}
                if self.receipt_dir is not None:
                    atomic_json(self.receipt_dir / "root-result.json", self.root_result)
                self.send({"event": "root_exit", **self.root_result})
                self.stop()  # A native root's retirement closes its owned scope.
        if sys.platform == "linux":
            for process in psutil.Process().children():
                if self.process is not None and process.pid == self.process.pid:
                    continue  # Popen alone owns the root's authentic returncode.
                try:
                    os.waitpid(process.pid, os.WNOHANG)
                except ChildProcessError:
                    pass

    def start(self, command):
        if self.command is not None:
            raise RuntimeError("Resource admission was already registered")
        self.command = command
        self.receipt_dir = Path(command["receipt_dir"]) if command.get("receipt_dir") else None
        identity = {"pid": os.getpid(), "creation": psutil.Process().create_time(), "group": os.getpgrp()}
        self.description = {"version": 1, "generation": command["generation"], "anchor": identity,
                            "parent": command["parent"]}
        if self.receipt_dir is not None:
            self.receipt_dir.mkdir(parents=True, exist_ok=True)
            self.temporary = tempfile.TemporaryDirectory(prefix="agentloom-process-")
            endpoint = str(Path(self.temporary.name) / "control.sock")
            self.listener = socket.socket(socket.AF_UNIX)
            self.listener.bind(endpoint)
            os.chmod(endpoint, 0o600)
            self.listener.listen()
            self.description["endpoint"] = endpoint
            atomic_json(self.receipt_dir / "description.json", self.description)
        self.send({"event": "owner_ready"})
        if not command.get("gated"):
            self.release()

    def release(self):
        if self.stopping:
            raise InterruptedError("Resource startup permission is closed")
        if self.process is not None:
            raise RuntimeError("Resource startup permission was already released")
        read, write = os.pipe()
        try:
            env = {key: value for key, value in self.command["env"].items() if key != SCOPE_FD_ENV}
            env[SCOPE_FD_ENV] = str(read)
            env["AGENTLOOM_MANAGED_PROCESS_GROUP"] = str(os.getpgrp())
            self.process = subprocess.Popen(self.command["argv"], env=env, cwd=self.command.get("cwd"),
                                            stdin=0, stdout=1, stderr=2, pass_fds=(read,), close_fds=True)
            identity = {"pid": self.process.pid, "creation": psutil.Process(self.process.pid).create_time()}
            os.write(write, json.dumps({"anchor": self.description["anchor"], "child": identity}).encode())
        finally:
            os.close(read)
            os.close(write)
        self.send({"event": "ready", **identity})
        # Only the real command owns the caller's stdio. The independent
        # control channel remains alive even when output is not being consumed.
        for descriptor in (0, 1, 2):
            os.close(descriptor)

    def stop(self, sig="SIGKILL"):
        self.stopping = True  # Synchronous gate revocation precedes cleanup.
        self.cleanup_requested = True
        self.cleanup_at = time.monotonic()
        if sig == "SIGTERM" and self.process is not None and self.process.poll() is None:
            self.process.terminate()
            self.cleanup_at += .3

    def cleanup(self):
        # Keep the anchor alive: group identity cannot be reassigned while this
        # exact process holds it. Never kill the anchor and then signal its PGID.
        frozen = set()
        while True:
            members = self.members()
            newly_found = [p for p in members if (p.pid, p.create_time()) not in frozen]
            if not newly_found:
                break
            for process in newly_found:
                try:
                    process.suspend()
                    frozen.add((process.pid, process.create_time()))
                except psutil.NoSuchProcess:
                    pass
        for process in reversed(members):
            try:
                process.kill()
            except psutil.NoSuchProcess:
                pass

    def failure(self, error, event="cleanup_error"):
        detail = {"error_type": type(error).__name__, "message": str(error), "category": "infrastructure"}
        if self.receipt_dir is not None:
            try:
                atomic_json(self.receipt_dir / "cleanup-error.json", detail)
            except OSError:
                pass  # Never mistake failed diagnostic storage for physical exit.
        self.send({"event": event, "error": detail})
        self.cleanup_requested = False

    def confirm_exit(self):
        if self.process is not None and self.root_result is None:
            return False
        if self.members():
            return False
        receipt = {"physical_exit_confirmed": True,
                   "generation": self.description["generation"] if self.description else None}
        if self.receipt_dir is not None:
            atomic_json(self.receipt_dir / "exited.json", receipt)
        self.send({"event": "extinct", **receipt})
        return True

    def run(self):
        pending = b""
        while True:
            channels = ([self.channel] if not self.parent_lost else []) + ([self.listener] if self.listener else [])
            ready, _, _ = select.select(channels, [], [], .02)
            for channel in ready:
                if channel is self.listener:
                    client, _ = self.listener.accept()
                    with client:
                        client.settimeout(1)
                        message = json.loads(client.recv(4096))
                        if message == {"op": "stop", "generation": self.description["generation"]}:
                            self.stop()
                    continue
                chunk = self.channel.recv(65536)
                if not chunk:
                    self.parent_lost = True
                    self.stop()
                else:
                    pending += chunk
                    if len(pending) > 1024 * 1024:
                        raise ValueError("Trusted resource descriptor exceeds control limit")
                    while b"\n" in pending:
                        line, pending = pending.split(b"\n", 1)
                        message = json.loads(line)
                        try:
                            if message["op"] == "start":
                                self.start(message)
                            elif message["op"] == "release":
                                self.release()
                            elif message["op"] == "stop":
                                self.stop(message.get("signal", "SIGKILL"))
                        except Exception as error:
                            self.failure(error, "startup_error")
                            self.stop()
            try:
                self.reap()
                if self.cleanup_requested and time.monotonic() >= self.cleanup_at:
                    self.cleanup()
                if self.command is not None and (self.process is not None or self.stopping) and self.confirm_exit():
                    return
                if self.command is None and self.parent_lost:
                    return  # No native command descriptor was admitted.
            except Exception as error:
                self.failure(error)


if __name__ == "__main__":
    channel = socket.socket(fileno=int(sys.argv[1]))
    guardian = Guardian(channel)
    try:
        guardian.run()
    finally:
        if guardian.temporary is not None:
            guardian.temporary.cleanup()
        channel.close()
