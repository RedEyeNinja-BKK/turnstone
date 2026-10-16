"""Offline security and behavior tests for the candidate self-API MCP bridge.

Run: python3 -m unittest discover -s integrations/turnstone-self-api/tests -v
No running Turnstone required. Does not import MCP or read real credentials.
"""
import importlib.util
import json
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

SCRIPT = Path(__file__).resolve().parents[1] / "server.py"
spec = importlib.util.spec_from_file_location("candidate_self_mcp", SCRIPT)
bridge = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = bridge
spec.loader.exec_module(bridge)

class SecurityTests(unittest.TestCase):
    def test_targets_only_loopback(self):
        self.assertEqual(len(bridge.ORIGINS), 6)
        self.assertTrue(all(url.startswith("http://127.0.0.1:") for url in bridge.ORIGINS.values()))

    def test_valid_read_endpoint(self):
        self.assertEqual(
            bridge.validate_path("/v1/api/admin/skills?limit=2", "GET"),
            "/v1/api/admin/skills?limit=2",
        )

    def test_rejects_traversal_authority_and_secret_reveal(self):
        bad = [
            "//evil/v1/api/admin/users", "https://evil/v1/api/admin",
            "/outside/api/", "/v1/api/../secrets", "/v1/api/%2e%2e/secrets",
            "/v1/api/%252e%252e/secrets", "/v1/api//admin/roles",
            "/v1/api/admin/roles#ignored",
            "/v1/api/admin/mcp-servers?reveal=true",
            "/v1/api/admin/mcp-servers?REVEAL=false",
        ]
        for path in bad:
            with self.subTest(path=path):
                with self.assertRaises(bridge.BridgeRefusal):
                    bridge.validate_path(path, "GET")

    def test_rejects_query_on_write(self):
        with self.assertRaises(bridge.BridgeRefusal):
            bridge.validate_path("/v1/api/admin/skills?force=true", "POST")

    def test_redacts_nested_secrets(self):
        raw_token = "ts_" + "a" * 64
        source = {
            "token": raw_token,
            "headers": {"Authorization": "Bearer hidden"},
            "model": {"api_key": "private"},
            "description": "This has " + raw_token,
            "name": "ordinary",
        }
        output = json.dumps(bridge.redact(source))
        self.assertNotIn(raw_token, output)
        self.assertNotIn("Bearer hidden", output)
        self.assertNotIn("private", output)
        self.assertIn("ordinary", output)

    def test_redacts_jwt_and_camelcase_fields(self):
        sensitive = {
            "jwt": "eyJ.fake.signature",
            "accessToken": "secret-value",
            "refreshToken": "another-secret",
            "clientSecret": "hidden-client",
            "apiKey": "sk-secret",
            "token": "glsa_" + "A" * 24,
            "notes": "Bearer " + "x" * 25,
            "state": "healthy",
        }
        out = bridge.redact(sensitive)
        for name in ("jwt", "accessToken", "refreshToken", "clientSecret", "apiKey", "token"):
            self.assertEqual(out[name], "[REDACTED]")
        self.assertNotIn("Bearer", out["notes"])
        self.assertEqual(out["state"], "healthy")

    def test_refuses_double_encoded_reveal(self):
        for path in (
            "/v1/api/admin/mcp-servers?%72eveal=true",
            "/v1/api/admin/mcp-servers?%2572eveal=true",
            "/v1/api/admin/mcp-servers?show%5fsecrets=yes",
        ):
            with self.subTest(path=path):
                with self.assertRaises(bridge.BridgeRefusal):
                    bridge.validate_path(path, "GET")

    def test_rejects_raw_control_character(self):
        with self.assertRaises(bridge.BridgeRefusal):
            bridge.validate_path("/v1/api/admin/skills\nX: injected", "GET")

    def test_rejects_non_root_token_access(self):
        with patch.object(bridge.os, "geteuid", return_value=102):
            with self.assertRaises(bridge.BridgeRefusal):
                bridge.read_token()

    def test_rejects_invalid_target_before_token_access(self):
        with self.assertRaises(bridge.BridgeRefusal):
            bridge.perform("GET", "/v1/api/auth/whoami", target="not-local")

    def test_mocked_read_redacts_token(self):
        class Response:
            status = 200
            headers = {"Content-Type": "application/json"}
            def read(self, n): return b'{"token":"hidden","result":"ok"}'
            def __enter__(self): return self
            def __exit__(self, *args): return False
        class Opener:
            def open(self, request, timeout=0):
                self.request = request
                return Response()
        fake = Opener()
        with patch.object(bridge, "read_token", return_value="ts_" + "a" * 64):
            with patch.object(bridge.urllib.request, "build_opener", return_value=fake):
                response = json.loads(bridge.perform("GET", "/v1/api/auth/whoami"))
        self.assertEqual(response["status"], 200)
        self.assertEqual(response["body"]["token"], "[REDACTED]")
        self.assertEqual(fake.request.get_method(), "GET")
        self.assertIsNone(fake.request.data)

if __name__ == "__main__":
    unittest.main()
