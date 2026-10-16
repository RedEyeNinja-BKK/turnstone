#!/usr/bin/env python3
"""Offline tests for scripts/siwc_direct_oauth_probe.py.

No network, no credentials: the ID-token and callback paths are exercised for
real (a locally generated JWKS is served over loopback), and the OAuth HTTP
exchanges are driven end to end with mocked responses.

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
import unittest.mock as mock
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
    assert set(_params(url)["scope"][0].split()) == {
        "openid", "profile", "email", "offline_access", "resource.invoke",
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
    assert "agent_name_hint" not in p
    assert pending["is_initial_registration"] is False


def test_state_and_nonce_are_fresh_per_attempt(store):
    _, a = siwc.build_authorize_url(store=store, redirect_uri=siwc.DEFAULT_REDIRECT, agent_name="X")
    _, b = siwc.build_authorize_url(store=store, redirect_uri=siwc.DEFAULT_REDIRECT, agent_name="X")
    assert a["state"] != b["state"] and a["nonce"] != b["nonce"]


# ---- correction 7: id_token_hint must never be printed ----------------------
def test_id_token_hint_is_opt_in_and_off_by_default(store):
    store.save_creds({"client_id": "oaiapp_x", "id_token": "SECRETJWT", "email": "a@b.c", "expires_at": 0})
    url, pending = siwc.build_authorize_url(
        store=store, redirect_uri=siwc.DEFAULT_REDIRECT, agent_name=None)
    assert "id_token_hint" not in _params(url)
    assert "login_hint" not in _params(url)
    assert pending["sent_id_token_hint"] is False


def test_id_token_hint_sent_only_when_opted_in(store):
    store.save_creds({"client_id": "oaiapp_x", "id_token": "SECRETJWT", "email": "a@b.c", "expires_at": 0})
    url, pending = siwc.build_authorize_url(
        store=store, redirect_uri=siwc.DEFAULT_REDIRECT, agent_name=None, with_id_token_hint=True)
    assert _params(url)["id_token_hint"][0] == "SECRETJWT"
    assert pending["sent_id_token_hint"] is True


def test_authorize_does_not_print_id_token_hint_url(store, capsys):
    """The opt-in reauthorization URL must go to a 0600 file, never stdout."""
    store.save_creds({"client_id": "oaiapp_x", "id_token": "SECRETJWT", "email": "a@b.c", "expires_at": 0})

    class Args:
        redirect_uri = siwc.DEFAULT_REDIRECT
        agent_name = None
        listen = False
        timeout = 1
        with_id_token_hint = True

    assert siwc.cmd_authorize(Args(), store) == 0
    out = capsys.readouterr().out
    assert "SECRETJWT" not in out, "the id_token_hint JWT leaked to stdout"
    assert siwc.AUTHORIZE_ENDPOINT not in out, "the authorization URL was printed"
    assert "state=" not in out and "code_challenge=" not in out
    assert "so it is NOT printed" in out  # the operator-facing explanation still shows
    url_file = store.root / "authorize_url.txt"
    assert url_file.is_file()
    import stat as _stat
    assert _stat.S_IMODE(url_file.stat().st_mode) == 0o600
    assert "SECRETJWT" in url_file.read_text()


# ------------------------------------------------------------ callback parsing
def _pending(state="S", initial=True, client_id_used="dynamic_agent_client"):
    return {"state": state, "nonce": "N", "code_verifier": "V",
            "redirect_uri": siwc.DEFAULT_REDIRECT, "client_id_used": client_id_used,
            "is_initial_registration": initial}


def test_parse_callback_happy_path_registration():
    url = f"{siwc.DEFAULT_REDIRECT}?code=CODE&state=S&client_id=oaiapp_new"
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


def test_parse_callback_without_pending_refuses():
    with pytest.raises(siwc.CallbackError, match="no pending"):
        siwc.parse_callback(f"{siwc.DEFAULT_REDIRECT}?code=C&state=S", {})


def test_parse_callback_rejects_bare_url():
    with pytest.raises(siwc.CallbackError, match="(?i)full redirect URL"):
        siwc.parse_callback(siwc.DEFAULT_REDIRECT, _pending())


# ---- correction 6: reauthorization must not swap registration identity ------
def test_reauthorization_rejects_mismatched_callback_client_id():
    with pytest.raises(siwc.CallbackError, match="does not match the selected registration"):
        siwc.parse_callback(
            f"{siwc.DEFAULT_REDIRECT}?code=C&state=S&client_id=oaiapp_SOMEBODY_ELSE",
            _pending(initial=False, client_id_used="oaiapp_mine"),
            expected_client_id="oaiapp_mine",
        )


def test_reauthorization_accepts_matching_or_absent_client_id():
    ok = siwc.parse_callback(
        f"{siwc.DEFAULT_REDIRECT}?code=C&state=S&client_id=oaiapp_mine",
        _pending(initial=False, client_id_used="oaiapp_mine"), expected_client_id="oaiapp_mine")
    assert ok["client_id"] == "oaiapp_mine"
    absent = siwc.parse_callback(
        f"{siwc.DEFAULT_REDIRECT}?code=C&state=S",
        _pending(initial=False, client_id_used="oaiapp_mine"), expected_client_id="oaiapp_mine")
    assert absent["client_id"] == "oaiapp_mine"


def test_callback_errors_never_echo_the_code_or_url():
    secret_code = "CODE_SHOULD_NOT_APPEAR"
    try:
        siwc.parse_callback(f"{siwc.DEFAULT_REDIRECT}?code={secret_code}&state=WRONG", _pending())
    except siwc.CallbackError as exc:
        assert secret_code not in str(exc)
        assert "http" not in str(exc)
    else:
        pytest.fail("expected CallbackError")


# ---- correction 1: granted scopes are authoritative, fail closed ------------
def test_granted_scope_authoritative_when_present():
    ok, granted = siwc.has_plan_scope({"scope": "openid chatgpt.tokens.use.direct offline_access"})
    assert ok and siwc.REQUIRED_PLAN_SCOPE in granted


def test_granted_scope_absent_is_fail_closed():
    with pytest.raises(siwc.ScopeEvidenceError, match="no 'scope'"):
        siwc.has_plan_scope({"access_token": "a", "id_token": "i"})


def test_granted_scope_empty_string_is_fail_closed():
    with pytest.raises(siwc.ScopeEvidenceError, match="EMPTY"):
        siwc.has_plan_scope({"scope": ""})


def test_granted_scope_malformed_is_fail_closed():
    with pytest.raises(siwc.ScopeEvidenceError, match="malformed"):
        siwc.has_plan_scope({"scope": {"unexpected": "object"}})


def test_granted_scope_list_form_accepted():
    ok, granted = siwc.has_plan_scope({"scopes": ["openid", "chatgpt.tokens.use.direct"]})
    assert ok and granted == ["openid", "chatgpt.tokens.use.direct"]


def test_scope_check_ignores_callback_and_requested_scopes():
    """The requested scope string and callback scope must NOT be able to authorize."""
    # A token response with no scope evidence at all, while we requested everything.
    with pytest.raises(siwc.ScopeEvidenceError):
        siwc.has_plan_scope({"access_token": "a"})
    assert siwc.REQUIRED_PLAN_SCOPE in siwc.SCOPES_REQUESTED  # we asked...


def test_negative_requested_but_not_granted_fails_closed():
    """REQUIRED NEGATIVE: callback/request asked for direct, token response did not grant it."""
    token_response = {"access_token": "a", "id_token": "i", "scope": "openid profile email"}
    ok, granted = siwc.has_plan_scope(token_response)
    assert ok is False
    assert siwc.REQUIRED_PLAN_SCOPE not in granted


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
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        yield {"key": key, "url": f"http://127.0.0.1:{httpd.server_address[1]}/jwks.json"}
        httpd.shutdown()


def _mk_id_token(jwks_server, **overrides):
    now = int(time.time())
    claims = {"iss": siwc.EXPECTED_ISSUER, "aud": "oaiapp_test", "exp": now + 300,
              "iat": now, "sub": "user-123", "email": "a@b.c", "nonce": "NONCE"}
    claims.update(overrides)
    return jwt.encode(claims, jwks_server["key"], algorithm="RS256", headers={"kid": "test-kid"})


def test_id_token_valid(jwks_server):
    claims = siwc.validate_id_token(_mk_id_token(jwks_server), nonce="NONCE",
                                    client_id="oaiapp_test", jwks_uri=jwks_server["url"])
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


# ---- correction 4: a missing ID token is not acceptable ---------------------
def test_missing_id_token_is_refused():
    with pytest.raises(siwc.CallbackError, match="requires one"):
        siwc.validate_id_token("", nonce="NONCE", client_id="oaiapp_test",
                               jwks_uri="http://127.0.0.1:1/jwks.json")


def test_none_id_token_is_refused():
    with pytest.raises(siwc.CallbackError, match="requires one"):
        siwc.validate_id_token(None, nonce="NONCE", client_id="oaiapp_test",
                               jwks_uri="http://127.0.0.1:1/jwks.json")


# ---- correction 5: documented ext_agent_host_id shape -----------------------
def test_host_id_uses_urn_uuid_v4():
    hid = siwc.new_host_id()
    assert siwc.HOST_ID_RE.match(hid), hid
    assert hid.startswith("urn:uuid:")


def test_host_id_is_stable_and_preserved(tmp_path):
    s = siwc.Store(tmp_path / "siwc")
    first = s.host_id()
    assert siwc.HOST_ID_RE.match(first)
    assert s.host_id() == first                              # stable within instance
    assert siwc.Store(tmp_path / "siwc").host_id() == first   # preserved across instances


def test_malformed_legacy_host_id_is_replaced(tmp_path):
    s = siwc.Store(tmp_path / "siwc")
    (tmp_path / "siwc" / "host_id").write_text("siwc-legacyformat")
    assert siwc.HOST_ID_RE.match(s.host_id())


# --------------------------------------------------------------- credential store
def test_store_refuses_codex_cli_location():
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


def test_display_never_contains_token_values(tmp_path):
    s = siwc.Store(tmp_path / "siwc")
    s.save_creds({"client_id": "oaiapp_x", "access_token": "SUPERSECRETVALUE",
                  "refresh_token": "REFRESHSECRET", "granted_scopes": "openid",
                  "expires_at": int(time.time()) + 100})
    assert "SUPERSECRETVALUE" not in s.mask()
    assert "REFRESHSECRET" not in s.mask()


# ---- correction 2: no authorization code on argv ---------------------------
def test_no_callback_url_flag_exists(monkeypatch):
    """`complete` must not accept the authorization code via argv.

    SystemExit's message lives on the exception (pytest catches it before the
    interpreter would print it), so assert there -- and assert the code value is
    absent, which is the property that actually matters.
    """
    monkeypatch.setattr(siwc.sys, "argv",
                        ["prog", "complete", "--callback-url", "http://x/?code=ARGV_CODE"])
    with pytest.raises(SystemExit) as exc:
        siwc.main()
    assert exc.value.code != 0
    message = str(exc.value)
    assert "must not be passed on the command line" in message
    assert "ARGV_CODE" not in message, "the guard repeated the authorization code"


def test_argv_guard_allows_static_redirect_uri(monkeypatch, capsys):
    """--redirect-uri is a static loopback URI (no code) and must keep working."""
    siwc.reject_credentialish_argv(["--redirect-uri", siwc.DEFAULT_REDIRECT, "authorize"])
    siwc.reject_credentialish_argv(["authorize", "--agent-name", "Turnstone"])


@pytest.mark.parametrize("argv", [
    ["complete", "--callback-url", "http://127.0.0.1:1455/auth/callback?code=SECRET"],
    ["complete", "http://127.0.0.1:1455/auth/callback?code=SECRET"],
    ["complete", "--url=http://x/?code=SECRET"],
])
def test_argv_guard_blocks_every_code_bearing_form(argv):
    with pytest.raises(SystemExit, match="must not be passed on the command line"):
        siwc.reject_credentialish_argv(argv)


def test_argv_guard_message_never_repeats_the_value():
    try:
        siwc.reject_credentialish_argv(["--callback-url", "http://x/?code=TOPSECRET"])
    except SystemExit as exc:
        assert "TOPSECRET" not in str(exc)
    else:
        pytest.fail("expected SystemExit")


def test_complete_reads_stdin_without_echo(monkeypatch, tmp_path, capsys):
    """The code arrives via stdin and is never echoed back."""
    store_dir = tmp_path / "siwc"
    monkeypatch.setattr(siwc.sys, "argv", ["prog", "--store", str(store_dir), "complete"])
    secret_code = "STDIN_ONLY_CODE"
    monkeypatch.setattr(siwc.sys, "stdin", __import__("io").StringIO(
        f"{siwc.DEFAULT_REDIRECT}?code={secret_code}&state=S\n"))
    # No pending attempt -> refuses, but must not print the input.
    rc = siwc.main()
    out = capsys.readouterr()
    assert rc == 2
    assert secret_code not in out.out and secret_code not in out.err


# ---- correction 3: documented models[] schema -------------------------------
REALISTIC_MODEL_LIST = {
    "models": [
        {"slug": "gpt-6.1-sol", "display_name": "GPT-6.1 Sol", "visibility": "list"},
        {"slug": "gpt-6-luna", "display_name": "GPT-6 Luna", "visibility": "list"},
        {"slug": "gpt-6-luna-nt", "display_name": "GPT-6 Luna (non-thinking)", "visibility": "list"},
        {"slug": "internal-eval-only", "display_name": "Internal", "visibility": "hidden"},
    ]
}


def test_parse_model_catalog_realistic_schema():
    models = siwc.parse_model_catalog(REALISTIC_MODEL_LIST)
    assert [m["slug"] for m in models] == ["gpt-6.1-sol", "gpt-6-luna", "gpt-6-luna-nt", "internal-eval-only"]
    first = models[0]
    assert first["display_name"] == "GPT-6.1 Sol"
    assert first["visibility"] == "list"


def test_listable_models_filters_by_visibility():
    listed = siwc.listable_models(REALISTIC_MODEL_LIST)
    assert [m["slug"] for m in listed] == ["gpt-6.1-sol", "gpt-6-luna", "gpt-6-luna-nt"]
    assert all(m["visibility"] == "list" for m in listed)


def test_find_model_reports_availability_and_visibility():
    assert siwc.find_model(REALISTIC_MODEL_LIST, "gpt-6.1-sol")["display_name"] == "GPT-6.1 Sol"
    assert siwc.find_model(REALISTIC_MODEL_LIST, "nope") is None
    assert siwc.find_model(REALISTIC_MODEL_LIST, "internal-eval-only")["visibility"] == "hidden"


def test_model_catalog_wrong_envelope_fails_closed():
    with pytest.raises(siwc.ModelCatalogError, match="no 'models' array"):
        siwc.parse_model_catalog({"data": [{"id": "gpt-6.1-sol"}]})


def test_model_catalog_reports_observed_keys_on_failure():
    with pytest.raises(siwc.ModelCatalogError, match="object"):
        siwc.parse_model_catalog([{"slug": "x"}])
    try:
        siwc.parse_model_catalog({"unexpected": 1})
    except siwc.ModelCatalogError as exc:
        assert "unexpected" in str(exc)


def test_model_catalog_empty_array_fails_closed():
    with pytest.raises(siwc.ModelCatalogError, match="no entry with a usable 'slug'"):
        siwc.parse_model_catalog({"models": []})


def test_cmd_models_identifies_gpt_6_1_sol(monkeypatch, tmp_path, capsys):
    store = siwc.Store(tmp_path / "siwc")
    store.save_creds({"client_id": "oaiapp_x", "access_token": "TOK", "refresh_token": "R",
                      "granted_scopes": "openid chatgpt.tokens.use.direct",
                      "expires_at": int(time.time()) + 1000})
    monkeypatch.setattr(siwc, "models_request", lambda tok: (200, REALISTIC_MODEL_LIST))

    class Args:
        model = "gpt-6.1-sol"

    assert siwc.cmd_models(Args(), store) == 0
    out = capsys.readouterr().out
    assert "gpt-6.1-sol available: YES" in out
    assert "GPT-6.1 Sol" in out


def test_cmd_models_reports_absent_target(monkeypatch, tmp_path, capsys):
    store = siwc.Store(tmp_path / "siwc")
    store.save_creds({"client_id": "oaiapp_x", "access_token": "TOK", "refresh_token": "R",
                      "expires_at": int(time.time()) + 1000})

    class Args:
        model = "gpt-9-absent"

    monkeypatch.setattr(siwc, "models_request", lambda tok: (200, REALISTIC_MODEL_LIST))
    assert siwc.cmd_models(Args(), store) == 1
    assert "gpt-9-absent available: NO" in capsys.readouterr().out


# --------------------------------------------------------------- request shape
def test_responses_request_honours_flow_constraints(monkeypatch):
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
    assert body["store"] is False and body["stream"] is True
    assert body["model"] == "gpt-6.1-sol"
    assert isinstance(body["input"], list)
    forbidden = {"background", "conversation", "max_output_tokens", "max_tool_calls", "metadata",
                 "moderation", "multi_agent", "prompt", "prompt_cache_retention",
                 "safety_identifier", "temperature", "top_logprobs", "top_p", "truncation", "user"}
    assert not (forbidden & set(body)), f"sent unsupported params: {forbidden & set(body)}"
    assert out["completed"] is True and out["text"] == "Hi"


def test_inference_requires_terminal_completed(monkeypatch):
    class FakeResp:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def __iter__(self):
            yield b'data: {"type":"response.output_text.delta","delta":"partial"}\n'

    monkeypatch.setattr(siwc.urllib.request, "urlopen", lambda req, timeout=None: FakeResp())
    assert siwc.responses_infer("tok", "gpt-6.1-sol", "hi")["completed"] is False


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

    with mock.patch.object(siwc.urllib.request, "urlopen",
                           lambda req, timeout=None: (captured.update(
                               form=urllib.parse.parse_qs(req.data.decode())) or FakeResp())):
        siwc.refresh_tokens(creds={"client_id": "oaiapp_x", "refresh_token": "R"})
    assert captured["form"]["grant_type"] == ["refresh_token"]
    assert captured["form"]["refresh_token"] == ["R"]
    assert captured["form"]["client_id"] == ["oaiapp_x"]


# =============================================================================
# end-to-end flow with mocked OAuth responses
# =============================================================================
def _mock_oauth(monkeypatch, jwks_server, *, nonce="NONCE",
                scope="openid chatgpt.tokens.use.direct",
                client_id="oaiapp_e2e", include_id_token=True, id_token_kwargs=None):
    """Wire exchange/validation to canned responses; return the seen form.

    `nonce` must match the pending attempt's nonce, so callers build the pending
    authorization first and pass `pending["nonce"]` here.
    """
    seen: dict = {}
    idv = {"nonce": nonce}
    idv.update(id_token_kwargs or {})

    def fake_post_form(url, form, timeout=45):
        seen["form"] = form
        tok = {"access_token": "AT", "refresh_token": "RT", "expires_in": 3600,
               "token_type": "Bearer", "client_id": client_id}
        if scope is not None:
            tok["scope"] = scope
        if include_id_token:
            now = int(time.time())
            claims = {"iss": siwc.EXPECTED_ISSUER, "aud": client_id, "exp": now + 300,
                      "iat": now, "sub": "user-e2e"}
            claims.update(idv)
            tok["id_token"] = jwt.encode(claims, jwks_server["key"], algorithm="RS256",
                                         headers={"kid": "test-kid"})
        return 200, tok

    monkeypatch.setattr(siwc, "_post_form", fake_post_form)
    monkeypatch.setattr(siwc, "discovery", lambda: {"jwks_uri": jwks_server["url"]})
    return seen


@pytest.mark.parametrize("granted", [
    "openid profile email offline_access resource.invoke chatgpt.tokens.use.direct",
    None,
    "openid profile email",
])
def test_e2e_scope_gate(monkeypatch, tmp_path, jwks_server, capsys, granted):
    """Full flow: only a token response that actually grants the scope succeeds."""
    store = siwc.Store(tmp_path / "siwc")
    _, pending = siwc.build_authorize_url(
        store=store, redirect_uri=siwc.DEFAULT_REDIRECT, agent_name="Turnstone")
    store.save_pending(pending)
    seen = _mock_oauth(monkeypatch, jwks_server, nonce=pending["nonce"], scope=granted)

    callback = f"{siwc.DEFAULT_REDIRECT}?code=C&state={pending['state']}&client_id=oaiapp_e2e"
    rc = siwc.complete_from_stdin_or_url(callback, store)
    out = capsys.readouterr()

    if granted and siwc.REQUIRED_PLAN_SCOPE in granted.split():
        assert rc == 0, out.err
        creds = store.creds()
        assert creds["granted_scopes"].split() == granted.split()
        assert creds["client_id"] == "oaiapp_e2e"
        assert creds["sub"] == "user-e2e"
        assert creds["refresh_token"] == "RT"
        # the exchange used the PKCE verifier from the pending attempt
        assert seen["form"]["code_verifier"] == pending["code_verifier"]
        assert seen["form"]["redirect_uri"] == siwc.DEFAULT_REDIRECT
        assert not store.pending()          # pending cleared only on success
    else:
        assert rc == 4, out.out
        assert not store.creds(), "a credential was stored despite the scope gate"
        assert store.pending(), "pending attempt should survive a failed gate"


def test_e2e_missing_id_token_fails_closed(monkeypatch, tmp_path, jwks_server, capsys):
    store = siwc.Store(tmp_path / "siwc")
    _, pending = siwc.build_authorize_url(
        store=store, redirect_uri=siwc.DEFAULT_REDIRECT, agent_name="Turnstone")
    store.save_pending(pending)
    _mock_oauth(monkeypatch, jwks_server, nonce=pending["nonce"], include_id_token=False)
    rc = siwc.complete_from_stdin_or_url(
        f"{siwc.DEFAULT_REDIRECT}?code=C&state={pending['state']}&client_id=oaiapp_e2e", store)
    assert rc == 5
    assert not store.creds()
    assert "requires one" in capsys.readouterr().err


def test_e2e_wrong_nonce_fails_closed(monkeypatch, tmp_path, jwks_server, capsys):
    store = siwc.Store(tmp_path / "siwc")
    _, pending = siwc.build_authorize_url(
        store=store, redirect_uri=siwc.DEFAULT_REDIRECT, agent_name="Turnstone")
    store.save_pending(pending)
    _mock_oauth(monkeypatch, jwks_server, nonce=pending["nonce"],
                id_token_kwargs={"nonce": "DIFFERENT"})
    rc = siwc.complete_from_stdin_or_url(
        f"{siwc.DEFAULT_REDIRECT}?code=C&state={pending['state']}&client_id=oaiapp_e2e", store)
    assert rc == 5
    assert not store.creds()


def test_e2e_wrong_state_never_reaches_network(monkeypatch, tmp_path):
    store = siwc.Store(tmp_path / "siwc")
    called = {"n": 0}

    def boom(*a, **k):
        called["n"] += 1
        raise AssertionError("network reached despite bad state")

    monkeypatch.setattr(siwc, "_post_form", boom)
    _, pending = siwc.build_authorize_url(
        store=store, redirect_uri=siwc.DEFAULT_REDIRECT, agent_name="Turnstone")
    store.save_pending(pending)
    rc = siwc.complete_from_stdin_or_url(
        f"{siwc.DEFAULT_REDIRECT}?code=C&state=WRONG&client_id=oaiapp_e2e", store)
    assert rc == 2 and called["n"] == 0


def test_e2e_success_never_prints_tokens(monkeypatch, tmp_path, jwks_server, capsys):
    store = siwc.Store(tmp_path / "siwc")
    _, pending = siwc.build_authorize_url(
        store=store, redirect_uri=siwc.DEFAULT_REDIRECT, agent_name="Turnstone")
    store.save_pending(pending)
    _mock_oauth(monkeypatch, jwks_server, nonce=pending["nonce"])
    rc = siwc.complete_from_stdin_or_url(
        f"{siwc.DEFAULT_REDIRECT}?code=C&state={pending['state']}&client_id=oaiapp_e2e", store)
    out = capsys.readouterr()
    assert rc == 0
    for secret in ("AT", pending["code_verifier"], store.creds().get("id_token", "x")):
        assert secret not in out.out and secret not in out.err
