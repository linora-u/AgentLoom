"""One OS-owned invocation. Identity handshake precedes all host/Runtime code."""
from __future__ import annotations

import importlib.util
import json
import os
import sys
import time
import traceback
from contextlib import nullcontext
from pathlib import Path

import psutil

from .client import TaskDescription
from .files import atomic_json, read_json
from .host import HostSpec, exception_outcomes


def _install_harness_boundary(value, job):
    from agentloom.config.private_bindings import require_harness_bindings
    require_harness_bindings()
    # Load the trusted frozen support directly; importing agentloom.app before
    # the policy would initialize application machinery outside the boundary.
    modules = {}
    for name in ("snapshot", "isolation"):
        path = Path(__file__).parents[2] / "app" / "harnessing" / (name + ".py")
        spec = importlib.util.spec_from_file_location("_agentloom_host_harness_" + name, path)
        module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = module
        spec.loader.exec_module(module)
        modules[name] = module
    bundle = modules["snapshot"].load_bundle(Path(value["bundle"]["root"]))
    context = value["context"]
    if bundle.candidate_id != context["candidate_id"] or bundle.candidate_id != value["bundle"]["candidate_id"]:
        raise ValueError("Managed Host candidate identity differs from its frozen bundle")
    if not Path(__file__).resolve().is_relative_to(bundle.root / "python"):
        raise ValueError("Managed Harness Host must execute the frozen framework dependency")
    root = Path(context["root"]).resolve(strict=True)
    if not job.is_relative_to(root / "control-execution"):
        raise ValueError("Managed Host control receipts must be outside writable Trial roots")
    isolation = modules["isolation"]
    policy = isolation.trial_spec(bundle.root, context, read_paths=value.get("read_paths", ()), denied_paths=value.get("denied_paths", ()), environment=os.environ)
    policy = isolation.IsolationSpec((*policy.read_paths, job / "authorized.json"), policy.write_paths, (*policy.directory_paths, job))
    isolation.enforce_isolation(policy)


def _write_open_result(fd, value):
    with os.fdopen(fd, "w") as stream:
        json.dump(value, stream, ensure_ascii=False, allow_nan=False)
        stream.flush()
        os.fsync(stream.fileno())


def main():
    job = Path(sys.argv[1]).resolve(strict=True)
    atomic_json(job / "identity.json", {"pid": os.getpid(), "creation": psutil.Process().create_time(),
                                       "group": os.getpgrp()})
    # No host code, model request or business permission before acknowledgment.
    while not (job / "authorized.json").exists():
        if (job / "cancelled.json").exists():
            raise RuntimeError("Execution attempt cancelled before authorization")
        time.sleep(0.02)
    descriptor = read_json(job / "description.json")
    task = TaskDescription.from_dict(descriptor["task"])
    from agentloom.config.private_bindings import consume_harness_bindings
    from agentloom.execution.durability_scope import bind_durability_boundary
    from agentloom.execution.process_scope import bind_managed_process_scope
    from agentloom.execution.storage import bind_file_copy_metadata

    from .authorization import assert_current_attempt, bind_attempt_authorization

    boundary = descriptor["host"]["options"].get("harness_isolation")
    # Only this trusted interpreter owns the receipt FD; all exec tools close
    # it. Never grant the whole Durable job directory writable to an Agent.
    result_fd = os.open(job / "result.json", os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_CLOEXEC, 0o600) if boundary else None
    try:
        with bind_managed_process_scope(), bind_attempt_authorization(job, descriptor["invocation_id"]), consume_harness_bindings(), (bind_durability_boundary(Path(boundary["context"]["root"])) if boundary else nullcontext()), (bind_file_copy_metadata(False) if boundary else nullcontext()):
            if boundary:
                _install_harness_boundary(boundary, job)
            # Factory imports/construction are part of the isolated invocation,
            # including any Runtime/tool transport they initialize.
            host = HostSpec(**descriptor["host"]).build()
            assert_current_attempt()
            result = host.invoke(task, descriptor["invocation_id"])
        if boundary:
            _write_open_result(result_fd, {"result": result})
            result_fd = None
        else:
            atomic_json(job / "result.json", {"result": result})
    except BaseException as error:
        outcomes = exception_outcomes(error)
        outcome = outcomes[0] if outcomes else None
        value = {"error": {"error_type": type(error).__name__, "message": "isolated managed Host failed" if boundary else str(error),
            "traceback": "" if boundary else "".join(traceback.format_exception(error)),
            "retryable": getattr(error, "retryable", False), "category": getattr(error, "category", "invocation")},
            "outcome": outcome.to_dict() if outcome is not None else None,
            "outcomes": [item.to_dict() for item in outcomes]}
        if boundary:
            value["error"]["frames"] = [{"file": frame.filename, "line": frame.lineno, "function": frame.name} for frame in traceback.extract_tb(error.__traceback__)]
        if boundary:
            _write_open_result(result_fd, value)
            result_fd = None
        else:
            atomic_json(job / "result.json", value)
        if boundary:
            raise SystemExit(1) from None
        raise
    finally:
        if result_fd is not None:
            os.close(result_fd)


if __name__ == "__main__":
    main()
