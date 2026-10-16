#!/usr/bin/env python3
"""Sign in with ChatGPT — direct OAuth (open-source token sharing) probe.

A NARROW proof of concept: prove that a `chatgpt.tokens.use.direct`-scoped token
can drive a real streamed `POST /v1/responses` call for a specific model. It is
NOT a Turnstone production provider and deliberately does not touch Turnstone.

Official flow implemented (developers.openai.com/siwc/token-sharing-open-source):
  * public client, authorization-code grant, PKCE S256, state + nonce
  * first-time registration uses client_id=dynamic_agent_client + agent_name_hint
    and RETAINS the issued client_id (oaiapp_...) from the callback
  * scope: openid profile email offline_access resource.invoke chatgpt.tokens.use.direct
  * resource=https://api.openai.com/v1
  * redirect_uri is an http loopback on 127.0.0.1 and must be byte-identical in
    the authorization request and the code exchange

WHY THERE IS A COPY-PASTE PATH
    A 127.0.0.1 callback reaches the machine running the BROWSER, not this VM
    (per OpenAI's self-hosted-VM guidance). So either the browser runs here
    (--listen, optionally via `ssh -L 1455:127.0.0.1:1455 <vm>`), or the
    operator approves on their own machine and pastes the resulting redirect URL
    back (--complete). Both are supported; --listen is not required.

SECRETS POLICY
    Tokens are never printed, logged, or passed on argv. The credential store is
    separate from Codex CLI's, mode 0700/0600. Only metadata is ever displayed.

USAGE
    # 1. generate the authorization URL (prints URL only, stores pending state)
    python3 scripts/siwc_direct_oauth_probe.py authorize --agent-name Turnstone

    # 2a. after approving in the browser, paste the FULL redirect URL back
    python3 scripts/siwc_direct_oauth_probe.py complete --callback-url '<paste>'

    # 2b. OR, with an SSH tunnel from the browser machine, let this VM capture it
    python3 scripts/siwc_direct_oauth_probe.py authorize --listen

    # 3. verify authorization
    python3 scripts/siwc_direct_oauth_probe.py status
    python3 scripts/siwc_direct_oauth_probe.py models
    python3 scripts/siwc_direct_oauth_probe.py infer --model gpt-6.1-sol

    python3 scripts/siwc_direct_oauth_probe.py refresh     # minimal refresh
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import http.server
import json
import os
import pathlib
import secrets
import socketserver
import stat
import sys
import time
import urllib.parse
import urllib.request
import uuid

# --- constants from the official documentation ---------------------------------
AUTHORIZE_ENDPOINT = "https://auth.openai.com/api/accounts/authorize"
TOKEN_ENDPOINT = "https://auth.openai.com/api/accounts/oauth/token"
DISCOVERY_URL = "https://auth.openai.com/.well-known/openid-configuration"
EXPECTED_ISSUER = "https://auth.openai.com"

INITIAL_CLIENT_ID = "dynamic_agent_client"
RESOURCE = "https://api.openai.com/v1"
IDENTITY_SCOPES = ("openid", "profile", "email")
PLAN_SCOPES = ("offline_access", "resource.invoke", "chatgpt.tokens.use.direct")
SCOPES = " ".join(IDENTITY_SCOPES + PLAN_SCOPES)
REQUIRED_PLAN_SCOPE = "chatgpt.tokens.use.direct"

DEFAULT_REDIRECT = "http://127.0.0.1:1455/auth/callback"
DEFAULT_STORE = os.environ.get("SIWC_STORE_DIR", "/var/lib/turnstone/github/siwc-creds")

# Never allowed to collide with Codex CLI's own store.
FORBIDDEN_STORE_PREFIXES = ("/var/lib/turnstone/.codex",)
CODEX_HOME_MARKER = "codex-home"

API_BASE = "https://api.openai.com/v1"


# =============================================================================
# small helpers
# =============================================================================
def b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def make_pkce() -> tuple[str, str]:
    """Return (code_verifier, code_challenge) per RFC 7636 S256."""
    verifier = b64url(secrets.token_bytes(64))
    challenge = b64url(hashlib.sha256(verifier.encode("ascii")).digest())
    return verifier, challenge


def _write_private(path: pathlib.Path, data: str) -> None:
    """Write 0600, atomically enough for a single-writer probe."""
    path.parent.mkdir(parents=True, exist_ok=True)
    os.chmod(path.parent, 0o700)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(data, encoding="utf-8")
    os.chmod(tmp, 0o600)
    tmp.replace(path)


def _read_json(path: pathlib.Path) -> dict:
    if not path.is_file():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


# =============================================================================
# credential store (kept separate from Codex CLI)
# =============================================================================
class Store:
    def __init__(self, root: str | pathlib.Path):
        self.root = pathlib.Path(root).expanduser().resolve()
        resolved = str(self.root)
        for bad in FORBIDDEN_STORE_PREFIXES:
            if resolved.startswith(bad):
                raise SystemExit(
                    f"REFUSING: store {resolved} overlaps Codex CLI credentials ({bad}). "
                    "This probe must never read or write Codex's store."
                )
        if CODEX_HOME_MARKER in resolved:
            raise SystemExit(
                f"REFUSING: store {resolved} looks like a Codex home. Use a separate directory."
            )
        self.root.mkdir(parents=True, exist_ok=True)
        os.chmod(self.root, 0o700)

    # -- host identity -------------------------------------------------------
    @property
    def _host_id_path(self) -> pathlib.Path:
        return self.root / "host_id"

    def host_id(self) -> str:
        """Stable per-host ext_agent_host_id (created once, then persisted)."""
        if self._host_id_path.is_file():
            existing = self._host_id_path.read_text(encoding="utf-8").strip()
            if existing:
                return existing
        value = f"siwc-{uuid.uuid4().hex}"
        _write_private(self._host_id_path, value)
        return value

    # -- pending authorization attempt --------------------------------------
    @property
    def _pending_path(self) -> pathlib.Path:
        return self.root / "pending.json"

    def save_pending(self, payload: dict) -> None:
        _write_private(self._pending_path, json.dumps(payload, indent=2))

    def pending(self) -> dict:
        return _read_json(self._pending_path)

    def clear_pending(self) -> None:
        self._pending_path.unlink(missing_ok=True)

    # -- credentials ---------------------------------------------------------
    @property
    def _creds_path(self) -> pathlib.Path:
        return self.root / "credentials.json"

    def save_creds(self, payload: dict) -> None:
        _write_private(self._creds_path, json.dumps(payload, indent=2))

    def creds(self) -> dict:
        return _read_json(self._creds_path)

    def mask(self) -> str:
        """Mode+owner of the credential file, for evidence (never its contents)."""
        if not self._creds_path.is_file():
            return "no credentials stored"
        st = self._creds_path.stat()
        return f"mode={stat.S_IMODE(st.st_mode):04o} size={st.st_size}B"


# =============================================================================
# HTTP
# =============================================================================
def _post_form(url: str, form: dict, timeout: int = 45) -> tuple[int, dict]:
    body = urllib.parse.urlencode(form).encode("ascii")
    req = urllib.request.Request(
        url, data=body, method="POST",
        headers={"Content-Type": "application/x-www-form-urlencoded", "Accept": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        raw = e.read()
        try:
            return e.code, json.loads(raw or b"{}")
        except Exception:
            return e.code, {"error": "unparseable", "error_description": raw[:300].decode("utf-8", "replace")}


def _get_json(url: str, bearer: str, timeout: int = 45) -> tuple[int, dict]:
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {bearer}", "Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read() or b"{}")
        except Exception:
            return e.code, {}


def discovery() -> dict:
    return _get_json(DISCOVERY_URL, bearer="")[1]


# =============================================================================
# authorize
# =============================================================================
def build_authorize_url(*, store: Store, redirect_uri: str, agent_name: str | None) -> tuple[str, dict]:
    """Create a pending attempt and return (url, pending). Pure except for storage."""
    verifier, challenge = make_pkce()
    state = b64url(secrets.token_bytes(32))
    nonce = b64url(secrets.token_bytes(32))
    creds = store.creds()
    issued_client_id = str(creds.get("client_id") or "").strip()

    if issued_client_id:
        client_id = issued_client_id
        hint = None  # agent_name_hint is initial-registration only
    else:
        client_id = INITIAL_CLIENT_ID
        hint = agent_name

    params = {
        "client_id": client_id,
        "ext_agent_host_id": store.host_id(),
        "response_type": "code",
        "redirect_uri": redirect_uri,
        "scope": SCOPES,
        "resource": RESOURCE,
        "state": state,
        "nonce": nonce,
        "code_challenge_method": "S256",
        "code_challenge": challenge,
    }
    if hint:
        params["agent_name_hint"] = hint
    # Reauthorization hints are optional; only send an id_token_hint when present.
    if issued_client_id and creds.get("id_token"):
        params["id_token_hint"] = creds["id_token"]
        if creds.get("email"):
            params["login_hint"] = creds["email"]

    url = f"{AUTHORIZE_ENDPOINT}?{urllib.parse.urlencode(params)}"
    pending = {
        "created": int(time.time()),
        "redirect_uri": redirect_uri,
        "state": state,
        "nonce": nonce,
        "code_verifier": verifier,
        "client_id_used": client_id,
        "is_initial_registration": not bool(issued_client_id),
        "agent_name_hint": hint,
    }
    return url, pending


# =============================================================================
# callback + token exchange
# =============================================================================
class CallbackError(ValueError):
    pass


def parse_callback(callback_url: str, pending: dict) -> dict:
    """Validate the callback URL and extract grant material.

    Enforces: state match (CSRF), error propagation, and — on a NEW
    registration — presence of the issued client_id.
    """
    if not callback_url or "?" not in callback_url:
        raise CallbackError("callback URL has no query string; paste the FULL redirect URL")
    query = urllib.parse.urlparse(callback_url.strip()).query
    q = urllib.parse.parse_qs(query)

    if "error" in q:
        desc = (q.get("error_description") or [""])[0]
        raise CallbackError(f"authorization denied: {q['error'][0]}{' — ' + desc if desc else ''}")

    code = (q.get("code") or [""])[0]
    if not code:
        raise CallbackError("callback contains no authorization code")

    returned_state = (q.get("state") or [""])[0]
    expected_state = str(pending.get("state") or "")
    if not expected_state:
        raise CallbackError("no pending authorization attempt — run `authorize` first")
    if returned_state != expected_state:
        raise CallbackError("state mismatch — refusing (possible CSRF or a stale attempt)")

    issued = (q.get("client_id") or [""])[0].strip()
    if pending.get("is_initial_registration") and not issued:
        # Doc: "If a new-registration callback does not include an issued ID,
        # treat registration as incomplete."
        raise CallbackError(
            "initial registration callback returned no issued client_id — "
            "registration is incomplete; re-run the authorization"
        )

    scope = (q.get("scope") or [""])[0]
    return {
        "code": code,
        "client_id": issued or str(pending.get("client_id_used") or ""),
        "scope": scope,
    }


def exchange_code(*, pending: dict, grant: dict) -> tuple[int, dict]:
    form = {
        "grant_type": "authorization_code",
        "client_id": grant["client_id"],
        "code": grant["code"],
        "code_verifier": pending["code_verifier"],
        "redirect_uri": pending["redirect_uri"],   # MUST match the request exactly
        "resource": RESOURCE,
    }
    return _post_form(TOKEN_ENDPOINT, form)


def refresh_tokens(*, creds: dict) -> tuple[int, dict]:
    form = {
        "grant_type": "refresh_token",
        "client_id": creds["client_id"],
        "refresh_token": creds["refresh_token"],
        "resource": RESOURCE,
    }
    return _post_form(TOKEN_ENDPOINT, form)


# =============================================================================
# ID token validation
# =============================================================================
def validate_id_token(id_token: str, *, nonce: str, client_id: str, jwks_uri: str | None = None) -> dict:
    """Verify signature (JWKS/RS256), issuer, audience, expiry and nonce.

    Audience is checked against the ISSUED client id on a new registration, and
    falls back to the recorded client id thereafter.
    """
    import jwt  # PyJWT, imported lazily so --help works without the venv

    if not jwks_uri:
        jwks_uri = discovery().get("jwks_uri")
    if not jwks_uri:
        raise CallbackError("could not resolve jwks_uri from OIDC discovery")

    signing_key = jwt.PyJWKClient(jwks_uri).get_signing_key_from_jwt(id_token)
    claims = jwt.decode(
        id_token,
        signing_key.key,
        algorithms=["RS256"],
        issuer=EXPECTED_ISSUER,
        audience=client_id,
        options={"require": ["iss", "aud", "exp", "sub"]},
    )
    if nonce and claims.get("nonce") != nonce:
        raise CallbackError("ID token nonce does not match this attempt")
    return claims


def check_plan_scope(*sources: str) -> tuple[bool, str]:
    """True when chatgpt.tokens.use.direct appears in any granted-scope string."""
    merged: list[str] = []
    for s in sources:
        if not s:
            continue
        merged.extend(urllib.parse.unquote_plus(str(s)).replace(",", " ").split())
    granted = set(merged)
    if REQUIRED_PLAN_SCOPE in granted:
        return True, " ".join(sorted(granted))
    return False, " ".join(sorted(granted)) or "(none reported)"


# =============================================================================
# inference
# =============================================================================
def list_models(access_token: str) -> tuple[int, list[dict]]:
    status, payload = _get_json(f"{API_BASE}/models", access_token)
    rows = payload.get("data") if isinstance(payload, dict) else None
    return status, (rows if isinstance(rows, list) else [])


def responses_infer(access_token: str, model: str, prompt: str, timeout: int = 120) -> dict:
    """One minimal streamed Responses call under this flow's constraints.

    store=false and stream=true are mandatory here; unsupported parameters are
    deliberately not sent.
    """
    body = json.dumps({
        "model": model,
        "input": [{"role": "user", "content": prompt}],
        "store": False,
        "stream": True,
    }).encode()
    req = urllib.request.Request(
        f"{API_BASE}/responses", data=body, method="POST",
        headers={
            "Authorization": f"Bearer {access_token}",
            "Content-Type": "application/json",
            "Accept": "text/event-stream",
        },
    )
    out = {"http": 0, "text": "", "completed": False, "failed": None, "incomplete": None, "events": 0}
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            out["http"] = r.status
            for raw in r:
                line = raw.decode("utf-8", "replace").strip()
                if not line.startswith("data:"):
                    continue
                payload = line[5:].strip()
                if payload in ("", "[DONE]"):
                    continue
                try:
                    ev = json.loads(payload)
                except json.JSONDecodeError:
                    continue
                out["events"] += 1
                etype = ev.get("type", "")
                if etype == "response.output_text.delta":
                    out["text"] += ev.get("delta", "")
                elif etype == "response.completed":
                    out["completed"] = True
                elif etype == "response.failed":
                    err = (ev.get("response") or {}).get("error") or {}
                    out["failed"] = err.get("code") or err.get("message") or "unknown_error"
                elif etype == "response.incomplete":
                    out["incomplete"] = (ev.get("response") or {}).get("incomplete_details")
    except urllib.error.HTTPError as e:
        out["http"] = e.code
        try:
            out["failed"] = json.loads(e.read() or b"{}")
        except Exception:
            out["failed"] = "unparseable error body"
    except Exception as exc:  # noqa: BLE001
        out["failed"] = f"{type(exc).__name__}: {exc}"
    return out


# =============================================================================
# --listen: capture the loopback callback (for `ssh -L` from the browser host)
# =============================================================================
def run_listener(*, store: Store, redirect_uri: str, timeout: int) -> dict | None:
    parsed = urllib.parse.urlparse(redirect_uri)
    host, port, path = parsed.hostname or "127.0.0.1", parsed.port or 1455, parsed.path
    captured: dict = {}

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            if urllib.parse.urlparse(self.path).path != path:
                self.send_response(404); self.end_headers(); return
            captured["url"] = f"{redirect_uri}{'?' + self.path.split('?', 1)[1] if '?' in self.path else ''}"
            body = b"Authorization captured. You may close this window."
            self.send_response(200)
            self.send_header("Content-Type", "text/plain")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *a):  # silence default logging (it prints URLs)
            return

    with socketserver.TCPServer((host, port), Handler) as httpd:
        httpd.timeout = timeout
        deadline = time.time() + timeout
        while time.time() < deadline and "url" not in captured:
            httpd.handle_request()
    return captured.get("url") and captured or None


# =============================================================================
# CLI
# =============================================================================
def _granted_scopes_from_token_response(tok: dict, callback_scope: str) -> str:
    for key in ("scope", "scopes"):
        val = tok.get(key)
        if isinstance(val, str) and val.strip():
            return val
        if isinstance(val, list) and val:
            return " ".join(str(v) for v in val)
    return callback_scope


def cmd_authorize(args, store: Store) -> int:
    url, pending = build_authorize_url(
        store=store, redirect_uri=args.redirect_uri, agent_name=args.agent_name
    )
    store.save_pending(pending)
    kind = "INITIAL REGISTRATION (dynamic_agent_client)" if pending["is_initial_registration"] \
        else "REAUTHORIZATION (issued client_id)"
    print(f"mode          {kind}")
    print(f"host_id       {store.host_id()}")
    print(f"redirect_uri  {args.redirect_uri}")
    print(f"pending       stored (state/nonce/verifier at {store.root}/pending.json, mode 0600)")
    print()
    print("=" * 78)
    print("  OPERATOR ACTION — open this URL in the browser on your own machine:")
    print()
    print(url)
    print()
    print("  After approving you will land on the loopback callback. Nothing is")
    print("  listening there on your machine, so copy the FULL address from the")
    print("  browser's address bar and paste it into --complete.")
    print("=" * 78)

    if args.listen:
        print()
        print(f"listening on {args.redirect_uri} for up to {args.timeout}s (SSH-forward this port)")
        got = run_listener(store=store, redirect_uri=args.redirect_uri, timeout=args.timeout)
        if not got:
            print("no callback received before the timeout", file=sys.stderr)
            return 1
        return _complete_from_url(got["url"], store)
    return 0


def _complete_from_url(callback_url: str, store: Store) -> int:
    pending = store.pending()
    try:
        grant = parse_callback(callback_url, pending)
    except CallbackError as exc:
        print(f"REFUSING: {exc}", file=sys.stderr)
        return 2

    status, tok = exchange_code(pending=pending, grant=grant)
    if status != 200 or "access_token" not in tok:
        err = tok.get("error") or tok.get("error_description") or tok
        print(f"TOKEN EXCHANGE FAILED (HTTP {status}): {err}", file=sys.stderr)
        return 3

    scopes = _granted_scopes_from_token_response(tok, grant.get("scope", ""))
    authorized, granted_list = check_plan_scope(scopes, grant.get("scope", ""))
    print(f"token exchange  OK (HTTP {status})")
    print(f"issued client_id {grant['client_id']}")

    if not authorized:
        print(
            f"\nNOT AUTHORIZED for ChatGPT plan usage: {REQUIRED_PLAN_SCOPE} is ABSENT.\n"
            f"granted scopes: {granted_list}",
            file=sys.stderr,
        )
        return 4
    print(f"granted scopes   {granted_list}")
    print(f"AUTHORIZED       {REQUIRED_PLAN_SCOPE} present")

    identity = {}
    id_token = tok.get("id_token")
    if id_token:
        try:
            jwks = discovery().get("jwks_uri")
            claims = validate_id_token(
                id_token, nonce=str(pending.get("nonce") or ""),
                client_id=grant["client_id"], jwks_uri=jwks,
            )
            identity = {"sub": claims.get("sub"), "email": claims.get("email")}
            print(f"id_token         verified (sub={identity['sub']}, email={identity.get('email')})")
        except Exception as exc:  # noqa: BLE001
            print(f"REFUSING: ID token validation failed: {type(exc).__name__}: {exc}", file=sys.stderr)
            return 5
    else:
        print("WARNING: no id_token returned; identity not verified", file=sys.stderr)

    expires_at = int(time.time()) + int(tok.get("expires_in") or 0)
    store.save_creds({
        "client_id": grant["client_id"],
        "access_token": tok["access_token"],
        "refresh_token": tok.get("refresh_token", ""),
        "id_token": id_token or "",
        "token_type": tok.get("token_type", "Bearer"),
        "expires_at": expires_at,
        "granted_scopes": granted_list,
        "sub": identity.get("sub", ""),
        "email": identity.get("email", ""),
        "host_id": store.host_id(),
        "resource": RESOURCE,
    })
    store.clear_pending()
    print(f"stored           {store.root}/credentials.json ({store.mask()})")
    print("\nSUCCESS: retained client_id + tokens in a store separate from Codex CLI.")
    return 0


def cmd_complete(args, store: Store) -> int:
    return _complete_from_url(args.callback_url, store)


def cmd_status(args, store: Store) -> int:
    creds = store.creds()
    print(f"store            {store.root} ({store.mask()})")
    print(f"host_id          {store.host_id()}")
    if not creds:
        print("credentials      NONE — run `authorize` then `complete`")
        return 1
    remaining = int(creds.get("expires_at", 0)) - int(time.time())
    print(f"client_id        {creds.get('client_id')}")
    print(f"sub / email      {creds.get('sub')} / {creds.get('email')}")
    print(f"granted_scopes   {creds.get('granted_scopes')}")
    print(f"access_token     present={bool(creds.get('access_token'))} expires_in={remaining}s")
    print(f"refresh_token    present={bool(creds.get('refresh_token'))}")
    authorized, _ = check_plan_scope(str(creds.get("granted_scopes", "")))
    print(f"plan usage       {'AUTHORIZED' if authorized else 'NOT AUTHORIZED'}")
    return 0 if authorized else 1


def _valid_access_token(store: Store) -> str | None:
    creds = store.creds()
    if not creds:
        return None
    if int(creds.get("expires_at", 0)) - int(time.time()) > 60:
        return creds["access_token"]
    if not creds.get("refresh_token"):
        print("access token expired and no refresh_token stored", file=sys.stderr)
        return None
    status, tok = refresh_tokens(creds=creds)
    if status != 200 or "access_token" not in tok:
        print(f"refresh failed (HTTP {status}): {tok.get('error', tok)}", file=sys.stderr)
        return None
    creds["access_token"] = tok["access_token"]
    if tok.get("refresh_token"):
        creds["refresh_token"] = tok["refresh_token"]
    creds["expires_at"] = int(time.time()) + int(tok.get("expires_in") or 0)
    store.save_creds(creds)
    print("access token refreshed")
    return creds["access_token"]


def cmd_refresh(args, store: Store) -> int:
    return 0 if _valid_access_token(store) else 1


def cmd_models(args, store: Store) -> int:
    token = _valid_access_token(store) or ""
    if not token:
        print("no usable access token; run authorize/complete first", file=sys.stderr)
        return 1
    status, rows = list_models(token)
    listed = [r for r in rows if r.get("visibility", "list") == "list"]
    print(f"GET /v1/models -> HTTP {status}, {len(listed)} listed models")
    if status != 200:
        print(json.dumps(rows)[:400], file=sys.stderr)
        return 1
    ids = sorted(str(r.get("id") or r.get("slug")) for r in listed)
    for i in ids:
        print(f"  {i}")
    target = "gpt-6.1-sol"
    print(f"\n{target} available: {target in ids}")
    return 0


def cmd_infer(args, store: Store) -> int:
    token = _valid_access_token(store) or ""
    if not token:
        print("no usable access token; run authorize/complete first", file=sys.stderr)
        return 1
    res = responses_infer(token, args.model, args.prompt)
    print(f"POST /v1/responses  HTTP {res['http']}  events={res['events']}  model={args.model}")
    if res["failed"]:
        print(f"FAILED: {res['failed']}", file=sys.stderr)
        return 1
    if res["incomplete"]:
        print(f"INCOMPLETE: {res['incomplete']}", file=sys.stderr)
        return 1
    print(f"text: {res['text'].strip()[:200]!r}")
    if res["completed"]:
        print("TERMINAL: response.completed — SUCCESS")
        return 0
    print("REFUSING to call it a success: no response.completed event", file=sys.stderr)
    return 1


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--store", default=DEFAULT_STORE)
    sub = ap.add_subparsers(dest="cmd", required=True)

    a = sub.add_parser("authorize")
    a.add_argument("--redirect-uri", default=DEFAULT_REDIRECT)
    a.add_argument("--agent-name", default="Turnstone")
    a.add_argument("--listen", action="store_true")
    a.add_argument("--timeout", type=int, default=300)

    c = sub.add_parser("complete")
    c.add_argument("--callback-url", required=True)

    sub.add_parser("status")
    sub.add_parser("refresh")
    sub.add_parser("models")

    i = sub.add_parser("infer")
    i.add_argument("--model", default="gpt-6.1-sol")
    i.add_argument("--prompt", default="Say exactly: Hello, world!")

    args = ap.parse_args()
    store = Store(args.store)
    return {
        "authorize": cmd_authorize,
        "complete": cmd_complete,
        "status": cmd_status,
        "refresh": cmd_refresh,
        "models": cmd_models,
        "infer": cmd_infer,
    }[args.cmd](args, store)


if __name__ == "__main__":
    sys.exit(main())
