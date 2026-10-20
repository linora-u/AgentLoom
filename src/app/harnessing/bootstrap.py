"""Trusted direct child bootstrap; executed by pathname before Host import."""
from __future__ import annotations

import importlib
import importlib.util
import inspect
import json
import os
import sys
import traceback
from pathlib import Path


def _write_result(fd, value):
    with os.fdopen(fd, "w") as stream:
        json.dump(value, stream, ensure_ascii=False, allow_nan=False)
        stream.flush()
        os.fsync(stream.fileno())


def main():
    job = Path(sys.argv[1]).resolve(strict=True)
    descriptor = json.loads((job / "launch.json").read_text())
    result_fd = os.open(job / "result.json", os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_CLOEXEC, 0o600)
    raw = sys.stdin.buffer.readline(4 * 1024 * 1024 + 2)
    if not raw.endswith(b"\n") or len(raw) > 4 * 1024 * 1024:
        raise RuntimeError("Invalid one-shot model configuration transport")
    private = json.loads(raw)
    # Consume and close the anonymous secret channel before any tool exists.
    os.close(0)
    null = os.open("/dev/null", os.O_RDONLY)
    if null != 0:
        os.dup2(null, 0)
        os.close(null)
    # Scope metadata is itself an anonymous authenticated bootstrap pipe. The
    # trusted execution module validates/consumes it before OS restrictions.
    from agentloom.execution.process_scope import bind_managed_process_scope
    with bind_managed_process_scope(guardian_entry=Path(descriptor["guardian_entry"])):
        module_path = Path(__file__).with_name("isolation.py")
        module_spec = importlib.util.spec_from_file_location("_agentloom_harness_isolation", module_path)
        isolation = importlib.util.module_from_spec(module_spec)
        sys.modules[module_spec.name] = isolation
        module_spec.loader.exec_module(isolation)
        spec = isolation.IsolationSpec(**descriptor["isolation"])
        isolation.enforce_isolation(spec)
        try:
            from agentloom.config.config import bind_harness_model_config
            from agentloom.config.private_bindings import bind_harness_credentials
            model_config = private.pop("model_config", None)
            credentials = private.pop("credentials", {})
            with bind_harness_credentials(credentials), bind_harness_model_config(model_config):
                module, name = descriptor["factory"].split(":", 1)
                factory = getattr(importlib.import_module(module), name)
                result = factory(descriptor["payload"], descriptor["bundle"], descriptor["context"])
                if inspect.isawaitable(result):
                    import asyncio
                    result = asyncio.run(result)
                if hasattr(result, "to_dict"):
                    result = result.to_dict()
                if not isinstance(result, dict):
                    raise ValueError("Isolated factory must return a JSON mapping")
            _write_result(result_fd, {"identity": descriptor["identity"], "result": result})
        except BaseException as error:
            # Do not persist a provider/config exception's input values or
            # tracebacks: Pydantic errors can contain credential-bearing data.
            frames = [{"file": frame.filename, "line": frame.lineno, "function": frame.name} for frame in traceback.extract_tb(error.__traceback__)]
            _write_result(result_fd, {"identity": descriptor["identity"], "error": {"type": type(error).__name__, "message": "trusted isolated operation failed", "frames": frames}})
            raise SystemExit(1)


if __name__ == "__main__":
    main()
