#!/usr/bin/env python3
"""Offline tests for scripts/siwc_direct_oauth_probe.py.

No network, no credentials: the ID-token test serves a locally generated JWKS
from a loopback HTTP server so signature/issuer/audience/expiry/nonce checks are
exercised for real rather than mocked away.

Run:  /var/lib/turnstone/github/siwc-venv/bin/python -m pytest \
          integrations/turnstone-self-api/tests/test_siwc_direct_oauth.py -q
"""
from __future__ import annotations

import base64
import hashlib
import http.server
import importlib.util
import json
import pathlib
import socketserver
import threading
import time
import urllib.parse

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa

SCRIPT = pathlib.Path(__file__).resolve().parents[3] / "scripts" / "siwc_direct_oauth_probe.py"
_spec = importlib.util.spec_from_file_location("siwc", SCRIPT)
siwc = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(siwc)


# ---------------------------------------------------------------- PKCE / URLs
def test_pkce_is_s256_and_unpadded():
    verifier, challenge = siwc.make_pkce()
    expected = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    assert challenge == expected
    assert "=" not in challenge and "=" not in verifier


def test_pkce_matches_rfc7636_vector():
    """RFC 7636 Appendix B: the canonical verifier/challenge pair."""
    verifier = "dBjftJeZ4CVP-mB92K27uhbUJU1p1r_wW1gFWFOEjXk"
    expected = "E9Melhoa2OwvFrEMTJguCHaoeK1t8URWbuGJSstw-cM"
    assert base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode() == expected


def test_pkce_is_unique_per_call():
    assert siwc.make_pkce()[0] != siwc.make_pkce()[0]


@pytest.fixture()
def store(tmp_path):
    return siwc.Store(tmp_path / "siwc")


def _params(url):
    return urllib.parse.parse_qs(urllib.parse.urlparse(url).query)


def test_initial_authorization_uses_dynamic_client_and_hint(store):
    url, pending = siwc.build_authorize_url(
        store=store, redirect_uri=siwc.DEFAULT_REDIRECT, agent_name="Turnstone"
    )
    p = _params(url)
    assert p["client_id"][0] == "dynamic_agent_client"
    assert p["agent_name_hint"][0] == "Turnstone"
    assert p["response_type"][0] == "code"
    assert p["code_challenge_method"][0] == "S256"
    assert p["resource"][0] == "https://api.openai.com/v1"
    assert p["redirect_uri"][0] == siwc.DEFAULT_REDIRECT
    assert p["ext_agent_host_id"][0] == store.host_id()
    assert pending["is_initial_registration"] is True


def test_authorization_requests_every_required_scope(store):
    url, _ = siwc.build_authorize_url(store=store, redirect_uri=siwc.DEFAULT_REDIRECT, agent_name="X")
    scope = set(_params(url)["scope"][0].split())
    assert scope == {"openid", "profile", "email", "offline_access", "resource.invoke",
                     "chatgpt.tokens.use.direct"}


def test_redirect_uri_never_uses_localhost(store):
    url, _ = siwc.build_authorize_url(store=store, redirect_uri=siwc.DEFAULT_REDIRECT, agent_name="X")
    assert "localhost" not in _params(url)["redirect_uri"][0]


def test_reauthorization_uses_issued_client_id_and_omits_hint(store):
    store.save_creds({"client_id": "oaiapp_abc123", "access_token": "x", "refresh_token": "y",
                      "id_token": "", "expires_at": int(time.time()) + 999})
    url, pending = siwc.build_authorize_url(store=store, redirect_uri=siwc.DEFAULT_REDIRECT, agent_name="Turnstone")
    p = _params(url)
    assert p["client_id"][0] == "oaiapp_abc123"
    assert "agent_name_hint" not in p          # initial-registration only
    assert pending["is_initial_registration"] is False


def test_id_token_hint_sent_only_when_stored(store):
    store.save_creds({"client_id": "oaiapp_x", "id_token": "IDT", "email": "a@b.c", "expires_at": 0})
    p = _params(siwc.build_authorize_url(store=store, redirect_uri=siwc.DEFAULT_REDIRECT, agent_name=None)[0])
    assert p["id_token_hint"][0] == "IDT"
    assert p["login_hint"][0] == "a@b.c"


def test_state_and_nonce_are_fresh_per_attempt(store):
    _, a = siwc.build_authorize_url(store=store, redirect_uri=siwc.DEFAULT_REDIRECT, agent_name="X")
    _, b = siwc.build_authorize_url(store=store, redirect_uri=siwc.DEFAULT_REDIRECT, agent_name="X")
    assert a["state"] != b["state"] and a["nonce"] != b["nonce"]


# ------------------------------------------------------------ callback parsing
def _pending(state="S", initial=True):
    return {"state": state, "nonce": "N", "code_verifier": "V",
            "redirect_uri": siwc.DEFAULT_REDIRECT, "client_id_used": "dynamic_agent_client",
            "is_initial_registration": initial}


def test_parse_callback_happy_path_registration():
    url = f"{siwc.DEFAULT_REDIRECT}?code=CODE&state=S&client_id=oaiapp_new&scope=openid+chatgpt.tokens.use.direct"
    g = siwc.parse_callback(url, _pending())
    assert g["code"] == "CODE" and g["client_id"] == "oaiapp_new"


def test_parse_callback_rejects_state_mismatch():
    with pytest.raises(siwc.CallbackError, match="state mismatch"):
        siwc.parse_callback(f"{siwc.DEFAULT_REDIRECT}?code=C&state=WRONG", _pending())


def test_parse_callback_propagates_denial():
    with pytest.raises(siwc.CallbackError, match="denied"):
        siwc.parse_callback(f"{siwc.DEFAULT_REDIRECT}?error=access_denied&error_description=nope", _pending())


def test_parse_callback_requires_code():
    with pytest.raises(siwc.CallbackError, match="no authorization code"):
        siwc.parse_callback(f"{siwc.DEFAULT_REDIRECT}?state=S", _pending())


def test_parse_callback_requires_issued_client_id_on_registration():
    with pytest.raises(siwc.CallbackError, match="no issued client_id"):
        siwc.parse_callback(f"{siwc.DEFAULT_REDIRECT}?code=C&state=S", _pending(initial=True))


def test_parse_callback_reauthorization_may_omit_client_id():
    g = siwc.parse_callback(f"{siwc.DEFAULT_REDIRECT}?code=C&state=S", _pending(initial=False))
    assert g["client_id"] == "dynamic_agent_client"  # falls back to the recorded one


def test_parse_callback_without_pending_refuses():
    with pytest.raises(siwc.CallbackError, match="no pending"):
        siwc.parse_callback(f"{siwc.DEFAULT_REDIRECT}?code=C&state=S", {})


def test_parse_callback_rejects_bare_url():
    with pytest.raises(siwc.CallbackError, match="(?i)full redirect URL"):
        siwc.parse_callback(siwc.DEFAULT_REDIRECT, _pending())


# ------------------------------------------------------------------- scopes
def test_check_plan_scope_present_and_absent():
    ok, granted = siwc.check_plan_scope("openid chatgpt.tokens.use.direct offline_access")
    assert ok and "chatgpt.tokens.use.direct" in granted
    ok2, granted2 = siwc.check_plan_scope("openid profile email")
    assert not ok2 and granted2


def test_check_plan_scope_handles_plus_and_commas():
    assert siwc.check_plan_scope("openid+chatgpt.tokens.use.direct")[0]
    assert siwc.check_plan_scope("openid,chatgpt.tokens.use.direct")[0]
    assert siwc.check_plan_scope("")[0] is False


def test_granted_scopes_prefers_token_response():
    assert siwc._granted_scopes_from_token_response({"scope": "a b"}, "c d") == "a b"
    assert siwc._granted_scopes_from_token_response({"scopes": ["a", "b"]}, "c") == "a b"
    assert siwc._granted_scopes_from_token_response({}, "c d") == "c d"


# --------------------------------------------------------- ID token validation
@pytest.fixture(scope="module")
def jwks_server():
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pub = key.public_key().public_numbers()

    def _b64(i: int) -> str:
        return base64.urlsafe_b64encode(i.to_bytes((i.bit_length() + 7) // 8, "big")).rstrip(b"=").decode()

    jwks = {"keys": [{"kty": "RSA", "kid": "test-kid", "use": "sig", "alg": "RS256",
                      "n": _b64(pub.n), "e": _b64(pub.e)}]}
    payload = json.dumps(jwks).encode()

    class H(http.server.BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, *a):
            return

    with socketserver.TCPServer(("127.0.0.1", 0), H) as httpd:
        t = threading.Thread(target=httpd.serve_forever, daemon=True)
        t.start()
        yield {"key": key, "url": f"http://127.0.0.1:{httpd.server_address[1]}/jwks.json"}
        httpd.shutdown()


def _mk_id_token(jwks_server, **overrides):
    now = int(time.time())
    claims = {"iss": siwc.EXPECTED_ISSUER, "aud": "oaiapp_test", "exp": now + 300,
              "iat": now, "sub": "user-123", "email": "a@b.c", "nonce": "NONCE"}
    claims.update(overrides)
    return jwt.encode(claims, jwks_server["key"], algorithm="RS256", headers={"kid": "test-kid"})


def test_id_token_valid(jwks_server):
    tok = _mk_id_token(jwks_server)
    claims = siwc.validate_id_token(tok, nonce="NONCE", client_id="oaiapp_test",
                                    jwks_uri=jwks_server["url"])
    assert claims["sub"] == "user-123"


def test_id_token_rejects_wrong_nonce(jwks_server):
    with pytest.raises(siwc.CallbackError, match="nonce"):
        siwc.validate_id_token(_mk_id_token(jwks_server), nonce="OTHER",
                               client_id="oaiapp_test", jwks_uri=jwks_server["url"])


def test_id_token_rejects_wrong_audience(jwks_server):
    with pytest.raises(jwt.InvalidAudienceError):
        siwc.validate_id_token(_mk_id_token(jwks_server, aud="someone_else"), nonce="NONCE",
                               client_id="oaiapp_test", jwks_uri=jwks_server["url"])


def test_id_token_rejects_wrong_issuer(jwks_server):
    with pytest.raises(jwt.InvalidIssuerError):
        siwc.validate_id_token(_mk_id_token(jwks_server, iss="https://evil.example"), nonce="NONCE",
                               client_id="oaiapp_test", jwks_uri=jwks_server["url"])


def test_id_token_rejects_expired(jwks_server):
    with pytest.raises(jwt.ExpiredSignatureError):
        siwc.validate_id_token(_mk_id_token(jwks_server, exp=int(time.time()) - 10), nonce="NONCE",
                               client_id="oaiapp_test", jwks_uri=jwks_server["url"])


def test_id_token_rejects_bad_signature(jwks_server):
    other = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    forged = jwt.encode({"iss": siwc.EXPECTED_ISSUER, "aud": "oaiapp_test", "exp": int(time.time()) + 300,
                         "sub": "x", "nonce": "NONCE"}, other, algorithm="RS256", headers={"kid": "test-kid"})
    with pytest.raises(jwt.InvalidSignatureError):
        siwc.validate_id_token(forged, nonce="NONCE", client_id="oaiapp_test", jwks_uri=jwks_server["url"])


# --------------------------------------------------------------- credential store
def test_store_refuses_codex_cli_location(tmp_path):
    with pytest.raises(SystemExit, match="Codex CLI"):
        siwc.Store("/var/lib/turnstone/.codex/siwc")


def test_store_refuses_codex_home_lookalike(tmp_path):
    with pytest.raises(SystemExit, match="Codex home"):
        siwc.Store(tmp_path / "codex-home" / "siwc")


def test_store_permissions_are_private(tmp_path):
    import stat
    s = siwc.Store(tmp_path / "siwc")
    s.save_creds({"access_token": "secret-value", "client_id": "oaiapp_x"})
    m = stat.S_IMODE((tmp_path / "siwc" / "credentials.json").stat().st_mode)
    assert m == 0o600, f"credentials file mode {m:04o} is too permissive"
    assert stat.S_IMODE((tmp_path / "siwc").stat().st_mode) == 0o700


def test_host_id_is_stable_and_persisted(tmp_path):
    s = siwc.Store(tmp_path / "siwc")
    first = s.host_id()
    assert first and s.host_id() == first              # stable across reads
    assert siwc.Store(tmp_path / "siwc").host_id() == first  # and across instances


def test_display_never_contains_token_values(tmp_path):
    """status/models output must not leak secrets (guard the reporting path)."""
    s = siwc.Store(tmp_path / "siwc")
    s.save_creds({"client_id": "oaiapp_x", "access_token": "SUPERSECRETVALUE",
                  "refresh_token": "REFRESHSECRET", "granted_scopes": "openid",
                  "expires_at": int(time.time()) + 100})
    assert "SUPERSECRETVALUE" not in s.mask()
    assert "REFRESHSECRET" not in s.mask()


# ------------------------------------------------------------------ request shape
def test_responses_request_honours_flow_constraints(monkeypatch):
    """POST /v1/responses must send store=false, stream=true, no forbidden params."""
    captured: dict = {}

    class FakeResp:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def __iter__(self):
            yield b'data: {"type":"response.output_text.delta","delta":"Hi"}\n'
            yield b'data: {"type":"response.completed"}\n'

    def fake_urlopen(req, timeout=None):
        captured["body"] = json.loads(req.data)
        return FakeResp()

    monkeypatch.setattr(siwc.urllib.request, "urlopen", fake_urlopen)
    out = siwc.responses_infer("tok", "gpt-6.1-sol", "hi")
    body = captured["body"]
    assert body["store"] is False
    assert body["stream"] is True
    assert body["model"] == "gpt-6.1-sol"
    assert isinstance(body["input"], list)
    forbidden = {"background", "conversation", "max_output_tokens", "max_tool_calls", "metadata",
                 "moderation", "multi_agent", "prompt", "prompt_cache_retention",
                 "safety_identifier", "temperature", "top_logprobs", "top_p", "truncation", "user"}
    assert not (forbidden & set(body)), f"sent unsupported params: {forbidden & set(body)}"
    assert out["completed"] is True and out["text"] == "Hi"


def test_inference_requires_terminal_completed(monkeypatch):
    """A stream that ends without response.completed must NOT be reported as success."""

    class FakeResp:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def __iter__(self):
            yield b'data: {"type":"response.output_text.delta","delta":"partial"}\n'

    monkeypatch.setattr(siwc.urllib.request, "urlopen", lambda req, timeout=None: FakeResp())
    out = siwc.responses_infer("tok", "gpt-6.1-sol", "hi")
    assert out["completed"] is False


def test_inference_surfaces_failed_event(monkeypatch):
    class FakeResp:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def __iter__(self):
            yield (b'data: {"type":"response.failed","response":{"error":'
                   b'{"code":"subscription_sharing_usage_limit_exceeded"}}}\n')

    monkeypatch.setattr(siwc.urllib.request, "urlopen", lambda req, timeout=None: FakeResp())
    out = siwc.responses_infer("tok", "gpt-6.1-sol", "hi")
    assert out["completed"] is False
    assert out["failed"] == "subscription_sharing_usage_limit_exceeded"


def test_token_exchange_form_is_complete():
    """The code exchange must carry grant_type/client_id/code/verifier/redirect/resource."""
    captured: dict = {}

    class FakeResp:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            return b'{"access_token":"a","id_token":"i","refresh_token":"r","expires_in":3600}'

    def fake_urlopen(req, timeout=None):
        captured["form"] = urllib.parse.parse_qs(req.data.decode())
        return FakeResp()

    import unittest.mock as mock
    with mock.patch.object(siwc.urllib.request, "urlopen", fake_urlopen):
        siwc.exchange_code(pending={"code_verifier": "VER", "redirect_uri": siwc.DEFAULT_REDIRECT},
                           grant={"code": "C", "client_id": "oaiapp_x"})
    form = captured["form"]
    assert form["grant_type"] == ["authorization_code"]
    assert form["client_id"] == ["oaiapp_x"]
    assert form["code"] == ["C"]
    assert form["code_verifier"] == ["VER"]
    assert form["redirect_uri"] == [siwc.DEFAULT_REDIRECT]
    assert form["resource"] == ["https://api.openai.com/v1"]


def test_refresh_form_shape():
    captured: dict = {}

    class FakeResp:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            return b'{"access_token":"a2","expires_in":3600}'

    import unittest.mock as mock
    with mock.patch.object(siwc.urllib.request, "urlopen",
                           lambda req, timeout=None: (captured.update(
                               form=urllib.parse.parse_qs(req.data.decode())) or FakeResp())):
        siwc.refresh_tokens(creds={"client_id": "oaiapp_x", "refresh_token": "R"})
    assert captured["form"]["grant_type"] == ["refresh_token"]
    assert captured["form"]["refresh_token"] == ["R"]
    assert captured["form"]["client_id"] == ["oaiapp_x"]
