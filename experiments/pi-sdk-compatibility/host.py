"""Isolated governance fixture: async subprocess IPC, no production authorization claim."""

import json
import os
import sys
import time
from pathlib import Path


def append_durable(path, value):
    with open(path, "a", encoding="utf-8") as stream:
        stream.write(json.dumps(value) + "\n")
        stream.flush()
        os.fsync(stream.fileno())


request = json.load(sys.stdin)
root = Path(request["root"])
identity = {key: request[key] for key in ("taskId", "runId", "sessionId", "callId", "toolName")}
if request["action"] == "prepare":
    append_durable(root / "host-trace.jsonl", {**identity, "phase": "hook-start", "raw": request["raw"]})
    # Deliberately asynchronous: Node must await a real external Python result.
    time.sleep(0.025)
    mode = request["mode"]
    if mode == "fail":
        raise RuntimeError("Injected Hook failure")
    if mode == "deny":
        append_durable(root / "host-trace.jsonl", {**identity, "phase": "hook-denied"})
        print(json.dumps({"allowed": False}))
    else:
        final = dict(request["raw"])
        if mode == "repair":
            if isinstance(final.get("path"), int):
                final["path"] = f'{final["path"]}.txt'
            if isinstance(final.get("content"), int):
                final["content"] = str(final["content"])
        append_durable(root / "host-trace.jsonl", {**identity, "phase": "hook-allowed", "final": final})
        print(json.dumps({"allowed": True, "final": final}))
elif request["action"] == "commit":
    append_durable(root / "journal.jsonl", {**identity, "status": "committed", "args": request["args"],
                                            "result": request["result"], "nativeParent": request["nativeParent"]})
    append_durable(root / "host-trace.jsonl", {**identity, "phase": "committed"})
    print(json.dumps({"committed": True}))
else:
    raise ValueError("Unknown fixture request")
