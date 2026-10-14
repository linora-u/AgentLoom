"""Actual captured/MCP peer with a normally inherited detached child."""
import hashlib
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import psutil

marker = Path(sys.argv[1])


def spawn():
    child = subprocess.Popen([sys.executable, "-c", "import time;time.sleep(90)"],
        start_new_session=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    facts = {"root": {"pid": os.getpid(), "creation": psutil.Process().create_time()},
             "detached": {"pid": child.pid, "creation": psutil.Process(child.pid).create_time()}}
    for label, name in (("generation", "AGENTLOOM_NATIVE_SCOPE_GENERATION"),
                        ("instance", "AGENTLOOM_SUBPROCESS_RUN_TOKEN")):
        token = os.environ.get(name)
        facts[label + "_present"] = token is not None
        facts[label + "_sha256"] = hashlib.sha256(token.encode()).hexdigest() if token else None
    marker.write_text(json.dumps(facts))
    return {"answer": 42}


if sys.argv[2] == "captured":
    spawn()
    time.sleep(90)
else:
    from mcp.server.fastmcp import FastMCP

    server = FastMCP("managed-detached", log_level="ERROR")
    server.tool()(spawn)
    server.run()
