"""Local MCP peer used by transport and Application acceptance tests."""

import asyncio
import json
import os
import sys
import time
from pathlib import Path

from mcp.server.fastmcp import FastMCP

server = FastMCP("ticket08", log_level="ERROR")
evidence = Path(sys.argv[1]) if len(sys.argv) > 1 else None
calls = 0


def record(event: str, **fields):
    if evidence is not None:
        with evidence.open("a") as stream:
            stream.write(json.dumps({"event": event, "pid": os.getpid(), **fields}) + "\n")


@server.tool()
def lookup(query: str) -> dict:
    """Return the query with the identity of this isolated server session."""
    global calls
    calls += 1
    record("lookup", query=query)
    return {"query": query, "pid": os.getpid(), "calls": calls, "answer": 42}


@server.tool()
async def slow_lookup(query: str) -> dict:
    """Wait for cancellation or return an isolated result after ten seconds."""
    record("slow_started", query=query)
    await asyncio.sleep(10)
    return lookup(query)


@server.tool()
def fail_lookup(query: str) -> str:
    """Return an MCP tool error to exercise canonical failure settlement."""
    raise ValueError("Fixture lookup failed")


if __name__ == "__main__":
    record("started")
    if "--stall" in sys.argv:
        time.sleep(60)
    server.run()
