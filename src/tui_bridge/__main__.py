"""Long-lived NDJSON entrypoint for the AgentLoom TUI bridge."""

from __future__ import annotations

import contextlib
import json
import os
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import TracebackType
from typing import Any

from .bridge import BridgeError, TuiBridge

_BUILDER_METHODS = frozenset({"builder.send", "builder.draft", "draft.apply"})
_MAX_REQUEST_BYTES = 8 * 1024 * 1024
_MAX_RESPONSE_BYTES = 8 * 1024 * 1024


class _ResponseWriter:
    """Serialize NDJSON responses onto the protocol stdout."""

    def __init__(self, stream: Any) -> None:
        self._stream = stream
        self._lock = threading.Lock()

    def write(self, response: dict[str, Any]) -> None:
        line = json.dumps(response, ensure_ascii=False, separators=(",", ":")) + "\n"
        if len(line.encode("utf-8")) > _MAX_RESPONSE_BYTES:
            fallback = {
                "id": response.get("id"),
                "ok": False,
                "error": {
                    "code": "internal_error",
                    "message": "bridge response exceeded the safe size limit",
                },
            }
            line = json.dumps(fallback, ensure_ascii=False, separators=(",", ":")) + "\n"
            if len(line.encode("utf-8")) > _MAX_RESPONSE_BYTES:
                fallback["id"] = None
                line = json.dumps(fallback, ensure_ascii=False, separators=(",", ":")) + "\n"
        with self._lock:
            self._stream.write(line)
            self._stream.flush()


class _BoundedExecutor:
    """A fixed worker pool with bounded queued work and input backpressure."""

    def __init__(self, *, max_workers: int, max_in_flight: int, thread_name_prefix: str) -> None:
        self._executor = ThreadPoolExecutor(
            max_workers=max_workers,
            thread_name_prefix=thread_name_prefix,
        )
        self._slots = threading.BoundedSemaphore(max_in_flight)

    def submit(self, function: Any, *args: Any) -> bool:
        if not self._slots.acquire(blocking=False):
            return False
        try:
            future = self._executor.submit(function, *args)
        except BaseException:
            self._slots.release()
            raise
        future.add_done_callback(lambda _future: self._slots.release())
        return True

    def __enter__(self) -> _BoundedExecutor:
        return self

    def __exit__(
        self,
        exception_type: type[BaseException] | None,
        exception: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self._executor.shutdown(wait=True)


def _error_response(request_id: Any, error: BridgeError) -> dict[str, Any]:
    return {
        "id": request_id,
        "ok": False,
        "error": {"code": error.code, "message": str(error)},
    }


def _dispatch_request(
    bridge: TuiBridge,
    writer: _ResponseWriter,
    request_id: Any,
    method: str,
    params: dict[str, Any],
) -> None:
    try:
        result = bridge.dispatch(method, params)
        response = {"id": request_id, "ok": True, "result": result}
    except BridgeError as error:
        response = _error_response(request_id, error)
    except Exception:
        response = _error_response(
            request_id,
            BridgeError("internal_error", "bridge request failed"),
        )
    writer.write(response)


def _read_request_line(stream: Any) -> tuple[str | None, str | None]:
    """Read one request with bounded buffering and return any framing error."""

    raw_line = stream.readline(_MAX_REQUEST_BYTES + 1)
    if raw_line in (b"", ""):
        return None, None

    is_text = isinstance(raw_line, str)
    encoded = raw_line.encode("utf-8") if is_text else raw_line
    line_ended = raw_line.endswith("\n" if is_text else b"\n")
    oversized = len(encoded) > _MAX_REQUEST_BYTES + (1 if line_ended else 0)

    # readline(size) may stop in the middle of an oversized line. Discard the
    # remainder in bounded chunks so the next request starts at a clean line.
    while not line_ended:
        remainder = stream.readline(_MAX_REQUEST_BYTES + 1)
        if remainder in (b"", ""):
            break
        remainder_bytes = remainder.encode("utf-8") if isinstance(remainder, str) else remainder
        oversized = oversized or len(encoded) + len(remainder_bytes) > _MAX_REQUEST_BYTES
        line_ended = remainder.endswith("\n" if isinstance(remainder, str) else b"\n")
        if not oversized:
            encoded += remainder_bytes

    if oversized:
        return None, "request exceeded the safe size limit"
    try:
        return encoded.decode("utf-8"), None
    except UnicodeDecodeError:
        return None, "request must be valid UTF-8"


def main() -> None:
    bridge = TuiBridge(Path.cwd())
    protocol_stdout = sys.stdout
    writer = _ResponseWriter(protocol_stdout)
    # stdout belongs exclusively to NDJSON. Keep incidental third-party output
    # suppressed for the full lifetime of every concurrent dispatch.
    with open(os.devnull, "w", encoding="utf-8") as discarded_stdout:
        with contextlib.redirect_stdout(discarded_stdout):
            # Builder operations share one worker because they mutate session
            # history/drafts. Read-only project observations use an independent
            # pool, so a model call cannot freeze live status requests.
            with (
                _BoundedExecutor(
                    max_workers=1,
                    max_in_flight=16,
                    thread_name_prefix="agentloom-builder",
                ) as builder_executor,
                _BoundedExecutor(
                    max_workers=1,
                    max_in_flight=32,
                    thread_name_prefix="agentloom-observe",
                ) as observation_executor,
            ):
                protocol_stdin = getattr(sys.stdin, "buffer", sys.stdin)
                while True:
                    raw_line, framing_error = _read_request_line(protocol_stdin)
                    if framing_error:
                        writer.write(
                            _error_response(
                                None,
                                BridgeError(
                                    "invalid_request",
                                    framing_error,
                                ),
                            )
                        )
                        continue
                    if raw_line is None:
                        break
                    if not raw_line.strip():
                        continue
                    request_id: Any = None
                    try:
                        request = json.loads(raw_line)
                        if not isinstance(request, dict):
                            raise BridgeError("invalid_request", "request must be a JSON object")
                        request_id = request.get("id")
                        method = request.get("method")
                        params = request.get("params", {})
                        if not isinstance(method, str) or not method:
                            raise BridgeError("invalid_request", "method must be a non-empty string")
                        if not isinstance(params, dict):
                            raise BridgeError("invalid_params", "params must be a JSON object")
                    except json.JSONDecodeError:
                        writer.write(
                            _error_response(
                                request_id,
                                BridgeError("invalid_request", "request must be valid JSON"),
                            )
                        )
                        continue
                    except BridgeError as error:
                        writer.write(_error_response(request_id, error))
                        continue

                    executor = builder_executor if method in _BUILDER_METHODS else observation_executor
                    accepted = executor.submit(
                        _dispatch_request,
                        bridge,
                        writer,
                        request_id,
                        method,
                        params,
                    )
                    if not accepted:
                        writer.write(
                            _error_response(
                                request_id,
                                BridgeError("busy", "bridge request lane is busy; retry shortly"),
                            )
                        )


if __name__ == "__main__":
    main()
