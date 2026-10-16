#!/usr/bin/env python3
"""LC-RD-003 acceptance helper: act as an MCP client against the deployed bridge.

This is a TEST/ACCEPTANCE HELPER, not part of the bridge. It exists so the
acceptance evidence in LC-RD-003H is reproducible by anyone with the service
account, using the standard MCP handshake rather than a bespoke path.

It performs NO writes unless given a turnstone_api_change call explicitly.

The bridge (/opt/turnstone-integrations/self-admin/launch-self-mcp.sh) is only
launchable via the exact-argv sudoers rule:

    (root) NOPASSWD: /opt/turnstone-integrations/self-admin/launch-self-mcp.sh ""

so this spawns it as root over stdio and speaks newline-delimited JSON-RPC 2.0
(the MCP stdio transport). The bridge reads the root-only Turnstone API
credential itself -- no token is handled here.

Usage:
    python3 mcp_client.py --list-tools
    python3 mcp_client.py --call turnstone_api_read '{"path":"/v1/api/auth/whoami"}'
    python3 mcp_client.py --script calls.json
"""

from __future__ import annotations

import argparse
import json
import queue
import subprocess
import sys
import threading
import time

BRIDGE = [
    "/usr/bin/sudo",
    "-n",
    "/opt/turnstone-integrations/self-admin/launch-self-mcp.sh",
]
PROTOCOL_VERSION = "2024-11-05"
CLIENT_INFO = {"name": "lc-rd-003-acceptance", "version": "1.0"}


class McpStdioClient:
    def __init__(self, timeout: float = 60.0) -> None:
        self.timeout = timeout
        self.proc = subprocess.Popen(  # noqa: S603
            BRIDGE,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
        )
        self._q: queue.Queue[str | None] = queue.Queue()
        self.stderr_lines: list[str] = []
        self.raw_lines: list[str] = []
        threading.Thread(target=self._pump_stdout, daemon=True).start()
        threading.Thread(target=self._pump_stderr, daemon=True).start()
        self._id = 0

    def _pump_stdout(self) -> None:
        assert self.proc.stdout is not None
        for line in self.proc.stdout:
            self._q.put(line)
        self._q.put(None)

    def _pump_stderr(self) -> None:
        assert self.proc.stderr is not None
        for line in self.proc.stderr:
            self.stderr_lines.append(line.rstrip("\n"))

    def _write(self, obj: dict) -> None:
        assert self.proc.stdin is not None
        self.proc.stdin.write(json.dumps(obj) + "\n")
        self.proc.stdin.flush()

    def notify(self, method: str, params: dict | None = None) -> None:
        msg: dict = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            msg["params"] = params
        self._write(msg)

    def request(self, method: str, params: dict | None = None) -> dict:
        self._id += 1
        mid = self._id
        msg: dict = {"jsonrpc": "2.0", "id": mid, "method": method}
        if params is not None:
            msg["params"] = params
        self._write(msg)

        deadline = time.time() + self.timeout
        while True:
            remaining = deadline - time.time()
            if remaining <= 0:
                raise TimeoutError(f"{method} (id={mid}) timed out after {self.timeout}s")
            try:
                line = self._q.get(timeout=remaining)
            except queue.Empty:
                raise TimeoutError(f"{method} (id={mid}) timed out after {self.timeout}s") from None
            if line is None:
                raise EOFError(f"{method}: bridge closed stdout. stderr={self.stderr_lines!r}")
            # A pty in the sudo path can translate \n -> \r\n; be tolerant.
            cleaned = line.replace("\r\n", "\n").strip()
            if not cleaned:
                continue
            self.raw_lines.append(cleaned)
            try:
                obj = json.loads(cleaned)
            except json.JSONDecodeError:
                # Not a JSON-RPC frame (e.g. a banner). Keep it for diagnostics.
                continue
            if isinstance(obj, dict) and obj.get("id") == mid:
                return obj
            # Otherwise: a notification or a response to another id. Ignore.

    def initialize(self) -> dict:
        resp = self.request(
            "initialize",
            {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {},
                "clientInfo": CLIENT_INFO,
            },
        )
        self.notify("notifications/initialized")
        return resp

    def list_tools(self) -> dict:
        return self.request("tools/list", {})

    def call_tool(self, name: str, arguments: dict) -> dict:
        return self.request("tools/call", {"name": name, "arguments": arguments})

    def close(self) -> None:
        try:
            if self.proc.stdin is not None:
                self.proc.stdin.close()
        except OSError:
            pass
        try:
            self.proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.proc.kill()

    def __enter__(self) -> "McpStdioClient":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


def _tool_text(resp: dict) -> str:
    """Pull the text payload out of an MCP tools/call result."""
    if "error" in resp:
        return f"[JSON-RPC error] {json.dumps(resp['error'])}"
    result = resp.get("result") or {}
    if result.get("isError"):
        content = result.get("content") or []
        return "[isError] " + " ".join(c.get("text", "") for c in content)
    content = result.get("content") or []
    if not content:
        return "[no content] " + json.dumps(result)
    return "\n".join(c.get("text", "") for c in content)


def _print_envelope(label: str, text: str) -> None:
    """Pretty-print the bridge's {status, receipt, body} envelope."""
    print(f"--- {label} ---")
    try:
        env = json.loads(text)
    except json.JSONDecodeError:
        print(text)
        print()
        return
    receipt = env.get("receipt") or {}
    print("  receipt:")
    for key in (
        "receipt_id",
        "ts",
        "method",
        "path",
        "target",
        "status",
        "self_guard",
        "request_sha256",
    ):
        if key in receipt:
            print(f"    {key:16s} {receipt[key]}")
    if "truncated" in env:
        print(f"  truncated: yes (length {env.get('length')}) {env.get('message', '')}")
    body = env.get("body")
    print("  body:")
    print(json.dumps(body, indent=2, ensure_ascii=True, sort_keys=False)[:6000])
    print()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--list-tools", action="store_true")
    ap.add_argument("--call", nargs=2, metavar=("TOOL", "ARGS_JSON"), action="append", default=[])
    ap.add_argument("--script", help="JSON file: [{\"tool\":...,\"args\":{...}}, ...]")
    ap.add_argument("--timeout", type=float, default=60.0)
    args = ap.parse_args()

    calls: list[tuple[str, dict]] = [(t, json.loads(a)) for t, a in args.call]
    if args.script:
        with open(args.script, encoding="utf-8") as fh:
            for item in json.load(fh):
                calls.append((item["tool"], item.get("args", {})))

    with McpStdioClient(timeout=args.timeout) as client:
        init = client.initialize()
        srv = (init.get("result") or {}).get("serverInfo") or {}
        proto = (init.get("result") or {}).get("protocolVersion")
        print("=== MCP initialize ===")
        print(f"  server   {srv.get('name')} {srv.get('version')}")
        print(f"  protocol {proto}")
        print()

        if args.list_tools or not calls:
            tools = client.list_tools()
            print("=== tools/list ===")
            for tool in (tools.get("result") or {}).get("tools", []):
                print(f"  {tool.get('name')}")
                schema = tool.get("inputSchema") or {}
                props = schema.get("properties") or {}
                req = set(schema.get("required") or [])
                for pname, pspec in props.items():
                    mark = "*" if pname in req else " "
                    print(f"      {mark}{pname}: {pspec.get('type', '?')}")
            print()

        for tool_name, tool_args in calls:
            label = f"{tool_name} {json.dumps(tool_args, ensure_ascii=True)}"
            try:
                resp = client.call_tool(tool_name, tool_args)
            except (TimeoutError, EOFError) as exc:
                print(f"--- {label} ---")
                print(f"  [transport failure] {exc}")
                print()
                continue
            _print_envelope(label, _tool_text(resp))

        if client.stderr_lines:
            print("=== bridge stderr ===")
            for line in client.stderr_lines[-20:]:
                print(f"  {line}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
