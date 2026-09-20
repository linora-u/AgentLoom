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


@server.tool(structured_output=False)
def context_payload(query: str) -> str:
    """Return a large text artifact; retrieve TARGET_RECORD using its ContextRef."""
    record("context_payload", query=query)
    lines = [f"record={index:04d} region=fixture status=ready note=ordinary-reference-data" for index in range(600)]
    lines[317] = "TARGET_RECORD source=ticket08 verification_value=PLATFORM-CTX-8426"
    return "\n".join(lines)


if __name__ == "__main__":
    record("started")
    if "--stall" in sys.argv:
        time.sleep(60)
    server.run()
