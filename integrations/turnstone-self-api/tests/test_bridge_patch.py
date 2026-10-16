"""LC-RD-003 patch tests — exercise the SPECIFIC defects the patch fixes, and
prove no legitimate capability was removed.

Run: python3 -m unittest discover -s turnstone-self-api/tests -v
Pure offline: no network, no credential, no mutation.
"""
from __future__ import annotations

import importlib.util
import json
import os
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

# Overridable so the SAME assertions can be pointed at the ORIGINAL server.py,
# which is how we prove each new test actually exercises the patched defect
# (a test that also passes against the original proves nothing).
#   SELF_MCP_SERVER=/path/to/original_server.py python3 -m unittest ...
SCRIPT = Path(os.environ.get(
    "SELF_MCP_SERVER", Path(__file__).resolve().parents[1] / "server.py"
))
spec = importlib.util.spec_from_file_location("patched_self_mcp", SCRIPT)
bridge = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = bridge
spec.loader.exec_module(bridge)


class PatchACredentialDisclosureTests(unittest.TestCase):
    def test_refuses_token_mint_route(self):
        """Response contains a plaintext token by design; must be refused."""
        with self.assertRaises(bridge.BridgeRefusal) as ctx:
            bridge.validate_path("/v1/api/admin/users/f8fae2349aa94e1eafe9ddb7c08c6f93/tokens", "POST")
        self.assertIn("credential-minting", str(ctx.exception))

    def test_trailing_slash_variant_also_refused(self):
        """A trailing-slash spelling must not be a bypass of the mint guard."""
        for path in ("/v1/api/admin/users/abc/tokens/", "/v1/api/admin/users/abc/tokens"):
            with self.subTest(path=path):
                with self.assertRaises(bridge.BridgeRefusal) as ctx:
                    bridge.validate_path(path, "POST")
                self.assertIn("credential-minting", str(ctx.exception))

    def test_refusal_is_explicit_not_silent(self):
        """Operator must see WHY, never a bare failure."""
        with self.assertRaises(bridge.BridgeRefusal) as ctx:
            bridge.validate_path("/v1/api/admin/users/abc/tokens", "POST")
        msg = str(ctx.exception)
        self.assertIn("Console", msg)  # points at the sanctioned path
        self.assertNotEqual(msg.strip(), "")

    def test_get_on_token_route_is_not_the_mint_route(self):
        # minting is POST; a GET listing tokens is not a mint and stays allowed
        self.assertEqual(
            bridge.validate_path("/v1/api/admin/users/abc/tokens", "GET"),
            "/v1/api/admin/users/abc/tokens",
        )


class PatchDSelfDisableTests(unittest.TestCase):
    def setUp(self):
        # The guard is driven by the memoised resolver; pin it per test.
        self._saved = (bridge._resolved_own_id, bridge._resolved_own_id_source)

    def tearDown(self):
        bridge._resolved_own_id, bridge._resolved_own_id_source = self._saved

    def _pin(self, sid, source="env"):
        bridge._resolved_own_id, bridge._resolved_own_id_source = sid, source

    def test_self_disable_refused_when_own_id_configured(self):
        self._pin("abc123")
        for method in ("PUT", "DELETE", "PATCH"):
            with self.subTest(method=method):
                with self.assertRaises(bridge.BridgeRefusal) as ctx:
                    bridge.validate_path("/v1/api/admin/mcp-servers/abc123", method)
                self.assertIn("self-disable", str(ctx.exception))

    def test_trailing_slash_variant_of_own_row_refused(self):
        self._pin("abc123")
        with self.assertRaises(bridge.BridgeRefusal):
            bridge.validate_path("/v1/api/admin/mcp-servers/abc123/", "DELETE")

    def test_other_servers_still_mutable(self):
        """Full capability preserved: only the bridge's OWN row is guarded."""
        self._pin("abc123")
        for method in ("PUT", "DELETE"):
            with self.subTest(method=method):
                self.assertEqual(
                    bridge.validate_path("/v1/api/admin/mcp-servers/other999", method),
                    "/v1/api/admin/mcp-servers/other999",
                )

    def test_guard_inactive_when_unresolved(self):
        """Default (unresolved) keeps full administrative capability."""
        self._pin("")
        self.assertEqual(
            bridge.validate_path("/v1/api/admin/mcp-servers/anything", "DELETE"),
            "/v1/api/admin/mcp-servers/anything",
        )


class SelfIdResolutionTests(unittest.TestCase):
    """LC-RD-003: SELF_MCP_SERVER_ID must not depend on surviving sudo env_reset."""

    def setUp(self):
        self._saved = (
            bridge._resolved_own_id,
            bridge._resolved_own_id_source,
            bridge._api_lookup_attempted,
        )

    def tearDown(self):
        (bridge._resolved_own_id, bridge._resolved_own_id_source,
         bridge._api_lookup_attempted) = self._saved

    def test_env_is_used_when_present(self):
        bridge._resolved_own_id = None
        with patch.object(bridge, "OWN_ID_ENV", "envid123"):
            self.assertEqual(bridge._own_server_id(), ("envid123", "env"))

    def test_file_is_used_when_env_absent(self):
        """The file survives sudo env_reset -- the real propagation fix."""
        bridge._resolved_own_id = None
        with patch.object(bridge, "OWN_ID_ENV", ""), patch.object(
            bridge, "_read_self_id_file", return_value="fileid456"
        ):
            self.assertEqual(bridge._own_server_id(), ("fileid456", "file"))

    def test_non_root_owned_id_file_is_rejected(self):
        """A user-writable id file must never activate the guard."""
        import tempfile

        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / bridge.SELF_ID_FILENAME
            p.write_text("attacker99")
            with patch.object(bridge, "__file__", str(Path(d) / "server.py")):
                self.assertEqual(bridge._read_self_id_file(), "")

    def test_malformed_id_is_rejected(self):
        with patch.object(bridge, "_read_self_id_file", return_value="bad id with spaces"):
            self.assertEqual(bridge._own_server_id(), ("", ""))

    def test_api_lookup_used_only_when_asked(self):
        bridge._resolved_own_id = None
        bridge._api_lookup_attempted = False
        with patch.object(bridge, "OWN_ID_ENV", ""), patch.object(
            bridge, "_read_self_id_file", return_value=""
        ), patch.object(bridge, "_lookup_own_server_id") as lk:
            lk.return_value = "apiid789"
            self.assertEqual(bridge._own_server_id(), ("", ""))  # not asked
            lk.assert_not_called()
            self.assertEqual(bridge._own_server_id(allow_api=True), ("apiid789", "api"))

    def test_guard_state_is_loud_when_unresolved(self):
        bridge._resolved_own_id = ""
        self.assertEqual(bridge.self_guard_state(), "inactive_unresolved")

    def test_guard_state_reports_active_with_source(self):
        bridge._resolved_own_id, bridge._resolved_own_id_source = "abc123", "file"
        self.assertEqual(bridge.self_guard_state(), "active:abc123 (file)")


class PatchBQueryEncodingTests(unittest.TestCase):
    def test_sensitive_query_keys_refused(self):
        bad = [
            "/v1/api/admin/mcp-servers?token=abc",
            "/v1/api/admin/mcp-servers?access_token=abc",
            "/v1/api/admin/mcp-servers?client_secret=abc",
            "/v1/api/admin/mcp-servers?api_key=abc",
            "/v1/api/admin/mcp-servers?reveal=true",
            "/v1/api/admin/mcp-servers?REVEAL=false",
            "/v1/api/admin/mcp-servers?show_secrets=yes",
        ]
        for path in bad:
            with self.subTest(path=path), self.assertRaises(bridge.BridgeRefusal):
                bridge.validate_path(path, "GET")

    def test_encoded_sensitive_keys_refused(self):
        bad = [
            "/v1/api/admin/mcp-servers?%72eveal=true",
            "/v1/api/admin/mcp-servers?%2572eveal=true",
            "/v1/api/admin/mcp-servers?%2574oken=abc",
            "/v1/api/admin/mcp-servers?show%5fsecrets=yes",
            "/v1/api/admin/mcp-servers?token%3Dabc",
        ]
        for path in bad:
            with self.subTest(path=path), self.assertRaises(bridge.BridgeRefusal):
                bridge.validate_path(path, "GET")

    def test_duplicate_query_keys_all_checked(self):
        with self.assertRaises(bridge.BridgeRefusal):
            bridge.validate_path("/v1/api/admin/mcp-servers?limit=2&token=abc", "GET")

    def test_semicolon_separated_params_refused(self):
        """Some parsers treat ';' as a separator, hiding a sensitive sibling."""
        for path in ("/v1/api/admin/mcp-servers?a=1;token=2",
                     "/v1/api/admin/mcp-servers?token=2;a=1"):
            with self.subTest(path=path), self.assertRaises(bridge.BridgeRefusal):
                bridge.validate_path(path, "GET")

    def test_encoded_crlf_in_query_refused(self):
        """Regression: the original rejected CR/LF; the patch must not lose it."""
        for path in ("/v1/api/admin/skills?abc=%0d%0aX-Test%3aok",
                     "/v1/api/admin/skills?abc=%0a",
                     "/v1/api/admin/skills?a=%250d%250a"):
            with self.subTest(path=path), self.assertRaises(bridge.BridgeRefusal):
                bridge.validate_path(path, "GET")

    def test_percent_encoded_path_refused(self):
        """Validator decoded for inspection but forwarded the encoded path."""
        for path in ("/v1/api/%2e%2e/secrets", "/v1/api/admin%2Ftokens", "/v1/api/%252e%252e/x"):
            with self.subTest(path=path):
                with self.assertRaises(bridge.BridgeRefusal) as ctx:
                    bridge.validate_path(path, "GET")
                self.assertIn("percent-encoded", str(ctx.exception))

    def test_legitimate_pagination_still_accepted(self):
        """No over-restriction: ordinary read filters keep working."""
        good = [
            "/v1/api/admin/skills?limit=2",
            "/v1/api/admin/mcp-servers?reveal=false".replace("?reveal=false", "?limit=10"),
            "/v1/api/cluster/nodes?page=2&per_page=50",
            "/v1/api/admin/users?sort=name",
        ]
        for path in good:
            with self.subTest(path=path):
                self.assertEqual(bridge.validate_path(path, "GET"), path)


class PatchCReceiptTests(unittest.TestCase):
    class Response:
        status = 200
        headers = {"Content-Type": "application/json"}

        def read(self, n):
            return b'{"result":"ok"}'

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

    class Opener:
        def __init__(self):
            self.request = None

        def open(self, request, timeout=0):
            self.request = request
            return PatchCReceiptTests.Response()

    def _call(self, method="GET", path="/v1/api/auth/whoami", body=""):
        fake = self.Opener()
        with patch.object(bridge, "read_token", return_value="ts_" + "a" * 64), patch.object(
            bridge.urllib.request, "build_opener", return_value=fake
        ):
            return json.loads(bridge.perform(method, path, body)), fake

    def test_receipt_present_and_predictable(self):
        out, _ = self._call()
        self.assertEqual(out["status"], 200)
        r = out["receipt"]
        for field in ("receipt_id", "ts", "method", "path", "target", "status",
                      "request_sha256", "self_guard"):
            self.assertIn(field, r)
        self.assertEqual(r["method"], "GET")
        self.assertEqual(r["path"], "/v1/api/auth/whoami")
        self.assertEqual(r["target"], "console")
        self.assertEqual(r["status"], 200)
        self.assertEqual(len(r["receipt_id"]), 32)

    def test_receipt_digest_is_stable_for_same_request(self):
        a, _ = self._call()
        b, _ = self._call()
        self.assertEqual(a["receipt"]["request_sha256"], b["receipt"]["request_sha256"])
        self.assertNotEqual(a["receipt"]["receipt_id"], b["receipt"]["receipt_id"])

    def test_receipt_digest_changes_with_body(self):
        a, _ = self._call("POST", "/v1/api/admin/skills/install", '{"name":"a"}')
        b, _ = self._call("POST", "/v1/api/admin/skills/install", '{"name":"b"}')
        self.assertNotEqual(a["receipt"]["request_sha256"], b["receipt"]["request_sha256"])

    def test_receipt_digest_is_canonical_for_semantically_equal_bodies(self):
        """Key order / whitespace must not change the digest."""
        a, _ = self._call("POST", "/v1/api/admin/skills/install", '{"a":1,"b":2}')
        b, _ = self._call("POST", "/v1/api/admin/skills/install", '{ "b": 2, "a": 1 }')
        self.assertEqual(a["receipt"]["request_sha256"], b["receipt"]["request_sha256"])

    def test_receipt_survives_truncation_branch(self):
        """The oversized-response branch must still carry a receipt + status."""
        big = {"blob": "x" * (bridge.MAX_RETURN + 5000)}
        out = json.loads(bridge._emit({"receipt_id": "r", "status": 200}, 200, big))
        self.assertTrue(out["truncated"])
        self.assertEqual(out["status"], 200)
        self.assertIn("receipt", out)

    def test_http_error_with_none_headers_does_not_crash(self):
        """Regression: exc.headers may be None; must not raise AttributeError."""
        import urllib.error

        def boom(request, timeout=0):
            # HTTPError(url, code, msg, hdrs, fp) -- hdrs=None makes .headers None
            raise urllib.error.HTTPError(
                request.full_url, 500, "boom", None, None
            )

        class Opener:
            def open(self, request, timeout=0):
                return boom(request, timeout)

        with patch.object(bridge, "read_token", return_value="ts_" + "a" * 64), patch.object(
            bridge.urllib.request, "build_opener", return_value=Opener()
        ):
            out = json.loads(bridge.perform("GET", "/v1/api/auth/whoami"))
        self.assertEqual(out["status"], 500)
        self.assertIn("receipt", out)

    def test_existing_envelope_keys_preserved(self):
        """Backwards compatibility: status/body must remain top-level."""
        out, _ = self._call()
        self.assertIn("status", out)
        self.assertIn("body", out)


class PatchERedactionTests(unittest.TestCase):
    def test_client_id_and_ciphertext_fields_redacted(self):
        out = bridge.redact({"client_id": "SEKRET", "oauth_client_secret_ct": "SEKRET"})
        self.assertEqual(out["client_id"], "[REDACTED]")
        self.assertEqual(out["oauth_client_secret_ct"], "[REDACTED]")

    def test_common_token_prefixes_redacted(self):
        for value in ("sk-" + "a" * 24, "ghp_" + "b" * 24, "xoxb-" + "c" * 24):
            with self.subTest(value=value[:6]):
                out = json.dumps(bridge.redact({"note": value}))
                self.assertNotIn(value, out)

    def test_normal_fields_untouched(self):
        out = bridge.redact({"state": "healthy", "count": 3, "name": "engineer"})
        self.assertEqual(out, {"state": "healthy", "count": 3, "name": "engineer"})


class NoCapabilityRegressionTests(unittest.TestCase):
    """The patch must not remove legitimate self-management capability."""

    def test_broad_administrative_write_routes_still_allowed(self):
        allowed = [
            ("POST", "/v1/api/admin/skills/install"),
            ("POST", "/v1/api/admin/mcp-servers"),
            ("POST", "/v1/api/admin/mcp-servers/reload"),
            ("PUT", "/v1/api/admin/settings/interface.theme"),
            ("POST", "/v1/api/admin/model-definitions/reload"),
            ("DELETE", "/v1/api/admin/personas/some-persona"),
            ("POST", "/v1/api/admin/users"),
            ("PUT", "/v1/api/admin/roles/builtin-operator"),
        ]
        for method, path in allowed:
            with self.subTest(method=method, path=path):
                self.assertEqual(bridge.validate_path(path, method), path)

    def test_all_six_targets_present(self):
        self.assertEqual(len(bridge.ORIGINS), 6)
        self.assertTrue(all(u.startswith("http://127.0.0.1:") for u in bridge.ORIGINS.values()))


if __name__ == "__main__":
    unittest.main()
