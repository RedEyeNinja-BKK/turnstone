"""Tests for the Switchyard MCP front end.

Covers what can actually go wrong in a thin wrapper: argv construction (a verb
must never be caller-controlled), stdin shaping, exit-code mapping, and the
promise that a credential value never appears in a response.

Standard library only, so it runs without a project test dependency::

    PYTHONPATH=. /opt/turnstone-venv/bin/python tests/test_server.py
"""

from __future__ import annotations

import asyncio
import json
import unittest
from typing import Any
from unittest import mock

from mcp_switchyard_ops import server


class _Proc:
    def __init__(self, stdout: bytes, returncode: int = 0, stderr: bytes = b"") -> None:
        self.stdout = stdout
        self.stderr = stderr
        self.returncode = returncode


class Harness(unittest.TestCase):
    def patch(self, proc: _Proc, captured: dict[str, Any]) -> None:
        def fake_run(argv, **kwargs):  # noqa: ANN001, ANN003
            captured["argv"] = argv
            captured["input"] = kwargs.get("input")
            captured["timeout"] = kwargs.get("timeout")
            return proc

        patcher = mock.patch.object(server.subprocess, "run", fake_run)
        patcher.start()
        self.addCleanup(patcher.stop)

    def patch_raising(self, exc: BaseException) -> None:
        def boom(argv, **kwargs):  # noqa: ANN001, ANN003
            raise exc

        patcher = mock.patch.object(server.subprocess, "run", boom)
        patcher.start()
        self.addCleanup(patcher.stop)


class TestArgvAndStdin(Harness):
    def test_verb_is_a_fixed_argv_slot(self) -> None:
        captured: dict[str, Any] = {}
        self.patch(_Proc(b'{"ok": true}'), captured)
        self.assertEqual(server._run("status"), {"ok": True})
        argv = captured["argv"]
        self.assertEqual(argv[0], server.PYTHON)
        self.assertEqual(argv[1:4], ["-I", "-B", server.SCRIPT])
        self.assertEqual(argv[4], "status")
        self.assertEqual(captured["input"], b"")

    def test_route_payload_is_one_json_object(self) -> None:
        captured: dict[str, Any] = {}
        self.patch(_Proc(b'{"name":"smartfree"}'), captured)
        server.switchyard_route("smartfree")
        self.assertEqual(captured["argv"][4], "route")
        self.assertEqual(json.loads(captured["input"]), {"route": "smartfree"})

    def test_log_omits_absent_route_filter(self) -> None:
        captured: dict[str, Any] = {}
        self.patch(_Proc(b'{"returned":0}'), captured)
        server.switchyard_log(limit=5)
        self.assertEqual(json.loads(captured["input"]), {"limit": 5})

    def test_log_includes_route_when_given(self) -> None:
        captured: dict[str, Any] = {}
        self.patch(_Proc(b'{"returned":0}'), captured)
        server.switchyard_log(route="tier2", limit=10)
        self.assertEqual(json.loads(captured["input"]), {"route": "tier2", "limit": 10})

    def test_timeout_is_bounded_by_configuration(self) -> None:
        captured: dict[str, Any] = {}
        self.patch(_Proc(b"{}"), captured)
        server._run("status")
        self.assertEqual(captured["timeout"], server._TIMEOUT)


class TestErrorMapping(Harness):
    def test_refused_exit_is_reported_with_its_message(self) -> None:
        body = json.dumps({"ok": False, "error": "route must match ^[A-Za-z0-9._:-]{1,64}$"}).encode()
        self.patch(_Proc(body, returncode=2), {})
        with self.assertRaises(server.SwitchyardToolError) as ctx:
            server.switchyard_route("../etc/passwd")
        self.assertIn("refused", str(ctx.exception))
        self.assertIn("must match", str(ctx.exception))

    def test_transport_exit_is_labelled(self) -> None:
        self.patch(_Proc(b'{"ok": false, "error": "ssh timed out"}', returncode=3), {})
        with self.assertRaises(server.SwitchyardToolError) as ctx:
            server.switchyard_status()
        self.assertIn("transport_failure", str(ctx.exception))

    def test_remote_exit_is_labelled(self) -> None:
        self.patch(
            _Proc(b'{"ok": false, "error": "manifest is not valid TOML"}', returncode=4), {}
        )
        with self.assertRaises(server.SwitchyardToolError) as ctx:
            server.switchyard_catalog()
        self.assertIn("remote_failure", str(ctx.exception))

    def test_timeout_becomes_a_tool_error(self) -> None:
        self.patch_raising(server.subprocess.TimeoutExpired(["x"], server._TIMEOUT))
        with self.assertRaises(server.SwitchyardToolError) as ctx:
            server.switchyard_stats()
        self.assertIn("timed out", str(ctx.exception))

    def test_missing_interpreter_becomes_a_tool_error(self) -> None:
        self.patch_raising(OSError("No such file or directory"))
        with self.assertRaises(server.SwitchyardToolError) as ctx:
            server.switchyard_metrics()
        self.assertIn("could not start", str(ctx.exception))

    def test_empty_output_becomes_a_tool_error(self) -> None:
        self.patch(_Proc(b"", returncode=1, stderr=b"traceback"), {})
        with self.assertRaises(server.SwitchyardToolError) as ctx:
            server.switchyard_validate()
        self.assertIn("no output", str(ctx.exception))

    def test_non_json_output_becomes_a_tool_error(self) -> None:
        self.patch(_Proc(b"not json"), {})
        with self.assertRaises(server.SwitchyardToolError) as ctx:
            server.switchyard_validate()
        self.assertIn("non-JSON", str(ctx.exception))


class TestSurface(Harness):
    def test_exit_codes_are_all_mapped(self) -> None:
        self.assertEqual(set(server.EXIT_MEANING), {0, 2, 3, 4})

    def test_tool_surface_is_the_seven_read_verbs(self) -> None:
        tools = asyncio.run(server.mcp.list_tools())
        names = {tool.name for tool in tools}
        self.assertEqual(
            names,
            {
                "switchyard_status",
                "switchyard_catalog",
                "switchyard_route",
                "switchyard_stats",
                "switchyard_metrics",
                "switchyard_validate",
                "switchyard_log",
            },
        )

    def test_no_tool_accepts_a_command_or_path(self) -> None:
        tools = asyncio.run(server.mcp.list_tools())
        forbidden = {"command", "cmd", "path", "url", "sudo", "argv", "remote", "file"}
        for tool in tools:
            props = set((tool.inputSchema or {}).get("properties", {}))
            self.assertFalse(props & forbidden, f"{tool.name} exposes {props & forbidden}")

    def test_only_input_verbs_take_parameters(self) -> None:
        tools = asyncio.run(server.mcp.list_tools())
        with_params = {
            tool.name
            for tool in tools
            if (tool.inputSchema or {}).get("properties")
        }
        self.assertEqual(with_params, {"switchyard_route", "switchyard_log"})

    def test_validate_result_is_returned_without_merging_local_environment(self) -> None:
        """The wrapper must not inject anything of its own into a tool result.

        ``validate`` is the only verb that runs with the unit environment
        loaded, so it is the one place a local credential could be merged in by
        a careless wrapper. Set a sentinel in this process's environment and
        assert it never appears in the returned payload.
        """
        import os

        sentinel = "MCP_SWITCHYARD_TEST_SENTINEL"
        with mock.patch.dict(os.environ, {sentinel: "do-not-leak-0123456789"}):
            captured: dict[str, Any] = {}
            body = json.dumps(
                {
                    "unit": "switchyard-neutral-green.service",
                    "dry_run_ok": True,
                    "returncode": 0,
                    "stdout": ["server OK: smartfree"],
                    "stderr": [],
                }
            ).encode()
            self.patch(_Proc(body), captured)
            result = server.switchyard_validate()
        self.assertEqual(
            set(result), {"unit", "dry_run_ok", "returncode", "stdout", "stderr"}
        )
        self.assertNotIn("do-not-leak", json.dumps(result))

    def test_validate_passes_through_runtime_redaction(self) -> None:
        """Runtime-redacted diagnostics survive the wrapper unchanged."""
        captured: dict[str, Any] = {}
        body = json.dumps(
            {
                "unit": "switchyard-neutral-green.service",
                "dry_run_ok": False,
                "returncode": 1,
                "stdout": [],
                "stderr": ["SWITCHYARD_OPENROUTER_API_KEY=[REDACTED]"],
            }
        ).encode()
        self.patch(_Proc(body), captured)
        result = server.switchyard_validate()
        self.assertEqual(result["stderr"], ["SWITCHYARD_OPENROUTER_API_KEY=[REDACTED]"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
