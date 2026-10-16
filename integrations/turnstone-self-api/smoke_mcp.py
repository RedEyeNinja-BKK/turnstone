#!/usr/bin/env python3
"""Read-only stdio MCP handshake + authenticated self-API smoke (no registration).

Run on turnstone-vm as root:
  sudo /opt/turnstone-venv/bin/python \
    /home/vincent/turnstone-five-node-source/integrations/turnstone-self-api/smoke_mcp.py

MCP server runs as root ONLY inside this smoke test to read its existing
root-only Turnstone API credential. This does not modify sudoers, MCP
definitions, roles or any running Turnstone service. No token is printed.
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

TARGETS = ("console", "head", "worker_2", "worker_3", "worker_4", "worker_5")

async def smoke() -> None:
    if os.geteuid() != 0:
        raise RuntimeError("Run as root for root-only staged API credential")
    server = Path(__file__).resolve().with_name("server.py")
    if not server.is_file():
        raise RuntimeError("Candidate MCP server.py not found")
    params = StdioServerParameters(
        command=sys.executable, args=[str(server)],
    )
    async with (
        stdio_client(params) as (incoming, outgoing),
        ClientSession(incoming, outgoing) as session,
    ):
            await session.initialize()
            print("MCP handshake: PASS")
            tools = await session.list_tools()
            names = sorted(tool.name for tool in tools.tools)
            expected = ["turnstone_api_change", "turnstone_api_read"]
            if names != expected:
                raise RuntimeError(f"Unexpected tool names: {names}")
            print("MCP tool discovery: PASS (read and change tools)")
            # LC-RD-003 patch: the shipped smoke checked tool NAMES only.
            # Verify each tool's declared input schema actually covers the
            # arguments the bridge accepts, so a renamed or emptied schema
            # cannot pass as a working tool.
            schemas = {tool.name: (tool.inputSchema or {}) for tool in tools.tools}
            want_props = {
                "turnstone_api_read": {"path", "target"},
                "turnstone_api_change": {"method", "path", "body_json", "target"},
            }
            for tool_name, required in want_props.items():
                props = set((schemas.get(tool_name, {}).get("properties") or {}).keys())
                missing = required - props
                if missing:
                    raise RuntimeError(f"{tool_name}: schema missing {sorted(missing)}")
            print("MCP tool schemas: PASS (required arguments present)")
            # Only read endpoints; do not invoke turnstone_api_change.
            for target in TARGETS:
                result = await session.call_tool(
                    "turnstone_api_read",
                    {"path": "/v1/api/auth/whoami", "target": target},
                )
                if result.isError:
                    raise RuntimeError(f"{target}: MCP tool returned error")
                texts = [x.text for x in result.content
                         if getattr(x, "type", "") == "text"]
                if len(texts) != 1:
                    raise RuntimeError(f"{target}: unexpected MCP content shape")
                data = json.loads(texts[0])
                if data.get("status") != 200:
                    raise RuntimeError(f"{target}: API HTTP {data.get('status')}")
                body = data.get("body")
                if not isinstance(body, dict) or not body.get("user_id"):
                    raise RuntimeError(f"{target}: whoami missing authenticated identity")
                print(f"{target}: MCP read → API HTTP 200 (authenticated)")
            print("MCP smoke PASS: 6 authenticated reads; zero writes performed")

if __name__ == "__main__":
    try:
        asyncio.run(asyncio.wait_for(smoke(), timeout=95))
    except Exception as exc:
        print(f"MCP smoke FAILED: {type(exc).__name__}; no tokens printed", file=sys.stderr)
        sys.exit(1)
