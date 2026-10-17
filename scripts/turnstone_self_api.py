#!/usr/bin/env python3
"""Call Turnstone's documented /v1/api routes through the approved self-MCP bridge.

Why this exists: the admin credential at
/srv/turnstone/credentials/api/turnstone_admin_token is root-only, so the
service user cannot read it and a plain `sudo python3 <script>` is not an
approved argv. The one approved path is the exact-argv sudoers entry for
launch-self-mcp.sh, which spawns the stdio bridge as root. This client speaks
MCP to that launcher; the token never enters this process.

Usage:
  python scripts/turnstone_self_api.py get  <path> [--target console]
  python scripts/turnstone_self_api.py send <METHOD> <path> [--body FILE_OR_JSON]
                                             [--target console]

Exit 0 on HTTP 200, 1 otherwise. Prints the bridge receipt plus the (redacted)
body as JSON, so every call leaves an auditable receipt id.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

LAUNCHER = "/opt/turnstone-integrations/self-admin/launch-self-mcp.sh"


async def call(method: str, path: str, body: str = "", target: str = "console") -> dict:
    params = StdioServerParameters(command="sudo", args=["-n", LAUNCHER])
    async with (
        stdio_client(params) as (incoming, outgoing),
        ClientSession(incoming, outgoing) as session,
    ):
        await session.initialize()
        if method.upper() == "GET":
            result = await session.call_tool("turnstone_api_read", {"path": path, "target": target})
        else:
            result = await session.call_tool(
                "turnstone_api_change",
                {"method": method.upper(), "path": path, "body_json": body, "target": target},
            )
        if result.isError:
            raise RuntimeError("bridge returned a tool error")
        texts = [c.text for c in result.content if getattr(c, "type", "") == "text"]
        if len(texts) != 1:
            raise RuntimeError(f"unexpected content shape: {len(texts)} text parts")
        return json.loads(texts[0])


def main() -> int:
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    g = sub.add_parser("get")
    g.add_argument("path")
    g.add_argument("--target", default="console")
    s = sub.add_parser("send")
    s.add_argument("method")
    s.add_argument("path")
    s.add_argument("--body", default="")
    s.add_argument("--target", default="console")
    a = ap.parse_args()

    if a.cmd == "get":
        out = asyncio.run(call("GET", a.path, target=a.target))
    else:
        body = a.body
        if body and not body.lstrip().startswith(("{", "[")):
            import pathlib

            body = pathlib.Path(body).read_text(encoding="utf-8")
        out = asyncio.run(call(a.method, a.path, body, target=a.target))
    print(json.dumps(out, indent=2, sort_keys=False)[:20000])
    status = out.get("status")
    return 0 if status == 200 else 1


if __name__ == "__main__":
    sys.exit(main())
