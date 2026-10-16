#!/usr/bin/env python3
"""Sign in with ChatGPT — direct OAuth (open-source token sharing) probe.

A NARROW proof of concept: prove that a token granted `chatgpt.tokens.use.direct`
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

HANDLING THE CALLBACK (no code ever travels through argv)
    A 127.0.0.1 callback reaches the machine running the BROWSER, not this VM.
    Two supported shapes, neither of which puts the authorization code on a
    command line:
      1. `--listen`  (preferred): forward the port and let this process capture
         the callback directly.
             ssh -L 1455:127.0.0.1:1455 <vm>          # from the Windows box
      2. piped/interactive stdin, read WITHOUT echo and never logged.
    There is intentionally no `--callback-url` flag: argv is world-readable via
    /proc/<pid>/cmdline and lands in shell history and transcripts.

SECRETS POLICY
    Tokens are never printed, logged, or passed on argv. The credential store is
    separate from Codex CLI's, mode 0700/0600. Only metadata is ever displayed.
    An authorization URL that would carry `id_token_hint` (a signed JWT) is
    written to a 0600 file instead of being printed, so it cannot reach a
    transcript; `--with-id-token-hint` is opt-in and off by default.

USAGE
    # 1. generate the authorization URL
    python3 scripts/siwc_direct_oauth_probe.py authorize --agent-name Turnstone

    # 2a. preferred: forward the port, then this captures the callback itself
    python3 scripts/siwc_direct_oauth_probe.py authorize --listen

    # 2b. otherwise pipe the pasted redirect URL on stdin (input is hidden)
    printf '%s\\n' "$PASTED" | python3 scripts/siwc_direct_oauth_probe.py complete

    # 3. verify authorization and prove inference
    python3 scripts/siwc_direct_oauth_probe.py status
    python3 scripts/siwc_direct_oauth_probe.py models
    python3 scripts/siwc_direct_oauth_probe.py infer --model gpt-6.1-sol
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import http.server
import json
import os
import pathlib
import re
import secrets
import socketserver
import stat
import sys
import time
import urllib.error
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
SCOPES_REQUESTED = " ".join(IDENTITY_SCOPES + PLAN_SCOPES)
REQUIRED_PLAN_SCOPE = "chatgpt.tokens.use.direct"

DEFAULT_REDIRECT = "http://127.0.0.1:1455/auth/callback"
DEFAULT_STORE = os.environ.get("SIWC_STORE_DIR", "/var/lib/turnstone/github/siwc-creds")

# Never allowed to collide with Codex CLI's own store.
FORBIDDEN_STORE_PREFIXES = ("/var/lib/turnstone/.codex",)
CODEX_HOME_MARKER = "codex-home"

API_BASE = "https://api.openai.com/v1"

# RFC 4122 v4, as a URN — the documented supported ext_agent_host_id shape.
HOST_ID_RE = re.compile(
    r"^urn:uuid:[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$"
)


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


def new_host_id() -> str:
    """A fresh ext_agent_host_id in the documented `urn:uuid:<UUIDv4>` shape."""
    return f"urn:uuid:{uuid.uuid4()}"


def _write_private(path: pathlib.Path, data: str) -> None:
    """Write 0600 under a 0700 parent."""
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


def read_callback_silently() -> str:
    """Read the callback URL from stdin WITHOUT echo, and without logging it.

    Uses getpass when attached to a tty (no echo, prompt on stderr); falls back
    to a plain line read when piped. Either way the value is never printed.
    """
    if sys.stdin.isatty():
        import getpass

        return getpass.getpass("Paste the full callback URL (input hidden): ").strip()
    return sys.stdin.readline().strip()


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
        """Stable per-host ext_agent_host_id, created once then preserved.

        A persisted value is validated against the documented URN shape; a
        malformed or legacy value is replaced (the identity is only ever used
        for a registration this store has not yet completed at that point).
        """
        if self._host_id_path.is_file():
            existing = self._host_id_path.read_text(encoding="utf-8").strip()
            if HOST_ID_RE.match(existing):
                return existing
        value = new_host_id()
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

    @property
    def _url_path(self) -> pathlib.Path:
        return self.root / "authorize_url.txt"

    def save_authorize_url(self, url: str) -> pathlib.Path:
        """Write a possibly-sensitive authorization URL to a private file."""
        _write_private(self._url_path, url)
        return self._url_path

    def save_creds(self, payload: dict) -> None:
        _write_private(self._creds_path, json.dumps(payload, indent=2))

    def creds(self) -> dict:
        return _read_json(self._creds_path)

    def mask(self) -> str:
        """Mode+size of the credential file, for evidence (never its contents)."""
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
            return e.code, {
                "error": "unparseable",
                "error_description": raw[:300].decode("utf-8", "replace"),
            }


def _get_json(url: str, bearer: str, timeout: int = 45) -> tuple[int, dict]:
    req = urllib.request.Request(
        url, headers={"Authorization": f"Bearer {bearer}", "Accept": "application/json"}
    )
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
# granted-scope evidence — the TOKEN RESPONSE is authoritative, nothing else
# =============================================================================
class ScopeEvidenceError(ValueError):
    """The token response did not carry usable granted-scope evidence."""


def parse_granted_scopes(token_response: dict) -> list[str]:
    """Extract granted scopes from the token response, or raise.

    RFC 6749 §5.1 defines a space-delimited ``scope`` parameter. Some servers
    return an array instead, so both shapes are accepted. The callback's `scope`
    query parameter and the scopes we *requested* are deliberately NOT consulted:
    only the token endpoint may assert what was granted. Absent, empty, or
    malformed evidence fails closed.
    """
    if not isinstance(token_response, dict):
        raise ScopeEvidenceError("token response is not an object")

    raw = token_response.get("scope")
    if raw is None:
        raw = token_response.get("scopes")

    if raw is None:
        raise ScopeEvidenceError(
            "token response carries no 'scope'/'scopes' field — cannot establish granted scopes"
        )
    if isinstance(raw, str):
        tokens = raw.replace(",", " ").split()
    elif isinstance(raw, list) and all(isinstance(x, str) for x in raw):
        tokens = [t for part in raw for t in part.replace(",", " ").split()]
    else:
        raise ScopeEvidenceError(f"malformed granted-scope evidence: {type(raw).__name__}")

    tokens = [t.strip() for t in tokens if t and t.strip()]
    if not tokens:
        raise ScopeEvidenceError("token response reported an EMPTY granted-scope set")
    return tokens


def has_plan_scope(token_response: dict) -> tuple[bool, list[str]]:
    """(authorized, granted) — raises ScopeEvidenceError when evidence is unusable."""
    granted = parse_granted_scopes(token_response)
    return (REQUIRED_PLAN_SCOPE in granted), granted


# =============================================================================
# authorize
# =============================================================================
def build_authorize_url(
    *, store: Store, redirect_uri: str, agent_name: str | None, with_id_token_hint: bool = False
) -> tuple[str, dict]:
    """Create a pending attempt and return (url, pending)."""
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
        "scope": SCOPES_REQUESTED,
        "resource": RESOURCE,
        "state": state,
        "nonce": nonce,
        "code_challenge_method": "S256",
        "code_challenge": challenge,
    }
    if hint:
        params["agent_name_hint"] = hint

    # Reauthorization hints are optional. id_token_hint is a signed JWT, so it is
    # sent only when explicitly opted into, and the resulting URL is never
    # printed (see cmd_authorize) — it goes to a 0600 file instead.
    sent_id_token_hint = False
    if issued_client_id and with_id_token_hint and creds.get("id_token"):
        params["id_token_hint"] = creds["id_token"]
        sent_id_token_hint = True
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
        "sent_id_token_hint": sent_id_token_hint,
    }
    return url, pending


# =============================================================================
# callback + token exchange
# =============================================================================
class CallbackError(ValueError):
    pass


def parse_callback(
    callback_url: str, pending: dict, *, expected_client_id: str | None = None
) -> dict:
    """Validate the callback URL and extract grant material.

    Enforces: state match (CSRF), error propagation, presence of the issued
    client_id on a NEW registration, and — on reauthorization — that any
    client_id the callback supplies matches the registration already selected.
    Error messages never embed the URL or the code.
    """
    if not callback_url or "?" not in callback_url:
        raise CallbackError("callback input has no query string; expected the full redirect URL")
    query = urllib.parse.urlparse(callback_url.strip()).query
    q = urllib.parse.parse_qs(query)

    if "error" in q:
        desc = (q.get("error_description") or [""])[0]
        raise CallbackError(
            f"authorization denied: {q['error'][0]}{' — ' + desc if desc else ''}"
        )

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
    if pending.get("is_initial_registration"):
        if not issued:
            # Doc: "If a new-registration callback does not include an issued ID,
            # treat registration as incomplete."
            raise CallbackError(
                "initial registration callback returned no issued client_id — "
                "registration is incomplete; re-run the authorization"
            )
        resolved = issued
    else:
        # Reauthorization: never let a callback swap the registration identity.
        bound = (expected_client_id or str(pending.get("client_id_used") or "")).strip()
        if issued and bound and issued != bound:
            raise CallbackError(
                "callback client_id does not match the selected registration — refusing "
                "to replace the saved registration"
            )
        resolved = bound or issued
        if not resolved:
            raise CallbackError("no client_id bound to this attempt — re-run `authorize`")

    return {"code": code, "client_id": resolved}


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
# ID token validation — mandatory, never optional
# =============================================================================
def validate_id_token(
    id_token: str, *, nonce: str, client_id: str, jwks_uri: str | None = None
) -> dict:
    """Verify signature (JWKS/RS256), issuer, audience, expiry, subject, nonce."""
    import jwt  # PyJWT, imported lazily so --help works without the venv

    if not id_token or not isinstance(id_token, str):
        raise CallbackError("no id_token present — an authenticated connection requires one")

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
    if not claims.get("sub"):
        raise CallbackError("ID token has no subject claim")
    if not nonce or claims.get("nonce") != nonce:
        raise CallbackError("ID token nonce does not match this attempt")
    return claims


# =============================================================================
# model discovery — documented `models[]` with slug/display_name/visibility
# =============================================================================
class ModelCatalogError(ValueError):
    """The model-list response did not match the documented shape."""


def parse_model_catalog(payload: object) -> list[dict]:
    """Parse the Sign-in-with-ChatGPT model list.

    Documented shape: a top-level ``models`` array whose entries carry ``slug``,
    ``display_name`` and ``visibility``; entries with ``visibility == "list"``
    are the display-relevant set, and ``slug`` is what is passed back as
    ``model``. Anything else fails closed with the observed keys named, so a
    schema change is diagnosable instead of silently yielding "no models".
    """
    if not isinstance(payload, dict):
        raise ModelCatalogError(f"model list is {type(payload).__name__}, not an object")
    rows = payload.get("models")
    if not isinstance(rows, list):
        observed = ", ".join(sorted(payload)) or "(empty object)"
        raise ModelCatalogError(
            f"model list has no 'models' array (observed top-level keys: {observed})"
        )

    out: list[dict] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        slug = row.get("slug")
        if not isinstance(slug, str) or not slug.strip():
            continue
        out.append({
            "slug": slug.strip(),
            "display_name": str(row.get("display_name") or ""),
            "visibility": str(row.get("visibility") or ""),
        })
    if not out:
        raise ModelCatalogError("'models' array contained no entry with a usable 'slug'")
    return out


def listable_models(payload: object) -> list[dict]:
    """The display-relevant subset (visibility == 'list'), per the docs."""
    return [m for m in parse_model_catalog(payload) if m["visibility"] == "list"]


def find_model(payload: object, slug: str) -> dict | None:
    for m in parse_model_catalog(payload):
        if m["slug"] == slug:
            return m
    return None


# =============================================================================
# inference
# =============================================================================
def models_request(access_token: str) -> tuple[int, dict]:
    return _get_json(f"{API_BASE}/models", access_token)


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
    out = {"http": 0, "text": "", "completed": False, "failed": None,
           "incomplete": None, "events": 0}
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
def run_listener(*, redirect_uri: str, timeout: int) -> str | None:
    parsed = urllib.parse.urlparse(redirect_uri)
    host = parsed.hostname or "127.0.0.1"
    port = parsed.port or 1455
    path = parsed.path
    captured: dict = {}

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            if urllib.parse.urlparse(self.path).path != path:
                self.send_response(404)
                self.end_headers()
                return
            qs = self.path.split("?", 1)[1] if "?" in self.path else ""
            captured["url"] = f"{redirect_uri}?{qs}" if qs else ""
            body = b"Authorization captured. You may close this window."
            self.send_response(200)
            self.send_header("Content-Type", "text/plain")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *a):  # never log the request line (it carries a code)
            return

    with socketserver.TCPServer((host, port), Handler) as httpd:
        httpd.timeout = timeout
        deadline = time.time() + timeout
        while time.time() < deadline and not captured.get("url"):
            httpd.handle_request()
    return captured.get("url") or None


# =============================================================================
# completion (shared by --listen and stdin)
# =============================================================================
def complete_from_stdin_or_url(callback_url: str, store: Store) -> int:
    pending = store.pending()
    try:
        grant = parse_callback(
            callback_url, pending, expected_client_id=str(store.creds().get("client_id") or "")
        )
    except CallbackError as exc:
        print(f"REFUSING: {exc}", file=sys.stderr)
        return 2

    status, tok = exchange_code(pending=pending, grant=grant)
    if status != 200 or "access_token" not in tok:
        err = tok.get("error") or tok.get("error_description") or tok
        print(f"TOKEN EXCHANGE FAILED (HTTP {status}): {err}", file=sys.stderr)
        return 3

    # --- authoritative granted-scope check ---------------------------------
    try:
        authorized, granted = has_plan_scope(tok)
    except ScopeEvidenceError as exc:
        print(
            f"AUTHORIZATION INDETERMINATE — failing closed: {exc}\n"
            "No credential was stored.",
            file=sys.stderr,
        )
        return 4

    print(f"token exchange   OK (HTTP {status})")
    print(f"issued client_id {grant['client_id']}")
    print(f"granted scopes   {' '.join(granted)}")
    if not authorized:
        print(
            f"\nNOT AUTHORIZED for ChatGPT plan usage: {REQUIRED_PLAN_SCOPE} was not granted.\n"
            "No credential was stored.",
            file=sys.stderr,
        )
        return 4
    print(f"AUTHORIZED       {REQUIRED_PLAN_SCOPE} present")

    # --- ID token is mandatory --------------------------------------------
    id_token = tok.get("id_token")
    try:
        claims = validate_id_token(
            id_token,
            nonce=str(pending.get("nonce") or ""),
            client_id=grant["client_id"],
            jwks_uri=discovery().get("jwks_uri"),
        )
    except CallbackError as exc:
        print(f"REFUSING: ID token validation failed: {exc}", file=sys.stderr)
        return 5
    except Exception as exc:  # noqa: BLE001
        print(f"REFUSING: ID token validation failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 5

    print(f"id_token         verified (sub={claims.get('sub')}, email={claims.get('email')})")

    store.save_creds({
        "client_id": grant["client_id"],
        "access_token": tok["access_token"],
        "refresh_token": tok.get("refresh_token", ""),
        "id_token": id_token,
        "token_type": tok.get("token_type", "Bearer"),
        "expires_at": int(time.time()) + int(tok.get("expires_in") or 0),
        "granted_scopes": " ".join(granted),
        "sub": claims.get("sub", ""),
        "email": claims.get("email", ""),
        "host_id": store.host_id(),
        "resource": RESOURCE,
    })
    store.clear_pending()
    print(f"stored           {store.root}/credentials.json ({store.mask()})")
    print("\nSUCCESS: retained client_id + tokens in a store separate from Codex CLI.")
    return 0


# =============================================================================
# CLI
# =============================================================================
def cmd_authorize(args, store: Store) -> int:
    url, pending = build_authorize_url(
        store=store,
        redirect_uri=args.redirect_uri,
        agent_name=args.agent_name,
        with_id_token_hint=args.with_id_token_hint,
    )
    store.save_pending(pending)
    kind = ("INITIAL REGISTRATION (dynamic_agent_client)"
            if pending["is_initial_registration"] else "REAUTHORIZATION (issued client_id)")
    print(f"mode          {kind}")
    print(f"host_id       {store.host_id()}")
    print(f"redirect_uri  {args.redirect_uri}")
    print(f"pending       stored (state/nonce/verifier, mode 0600)")

    if pending["sent_id_token_hint"]:
        # The URL now carries a signed JWT — never print it.
        path = store.save_authorize_url(url)
        print()
        print("This authorization URL carries an id_token_hint, so it is NOT printed.")
        print(f"Open it from: {path}  (mode 0600)")
        print("Do not paste it into any transcript, ticket or chat.")
    else:
        print()
        print("=" * 78)
        print("  OPERATOR ACTION — open this URL in the browser on your own machine:")
        print()
        print(url)
        print("=" * 78)

    if args.listen:
        print()
        print(f"listening on {args.redirect_uri} for up to {args.timeout}s")
        print("from your Windows box:  ssh -L 1455:127.0.0.1:1455 <this-vm>")
        got = run_listener(redirect_uri=args.redirect_uri, timeout=args.timeout)
        if not got:
            print("no callback received before the timeout", file=sys.stderr)
            return 1
        return complete_from_stdin_or_url(got, store)
    return 0


def cmd_complete(args, store: Store) -> int:
    url = read_callback_silently()
    if not url:
        print("REFUSING: no callback URL provided on stdin", file=sys.stderr)
        return 2
    return complete_from_stdin_or_url(url, store)


def cmd_status(args, store: Store) -> int:
    creds = store.creds()
    print(f"store            {store.root} ({store.mask()})")
    print(f"host_id          {store.host_id()}")
    if not creds:
        print("credentials      NONE — run `authorize` then complete the consent")
        return 1
    print(f"client_id        {creds.get('client_id')}")
    print(f"sub / email      {creds.get('sub')} / {creds.get('email')}")
    print(f"granted_scopes   {creds.get('granted_scopes')}")
    print(f"access_token     present={bool(creds.get('access_token'))} "
          f"expires_in={int(creds.get('expires_at', 0)) - int(time.time())}s")
    print(f"refresh_token    present={bool(creds.get('refresh_token'))}")
    try:
        authorized, _ = has_plan_scope({"scope": str(creds.get("granted_scopes", ""))})
    except ScopeEvidenceError:
        authorized = False
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
    token = _valid_access_token(store)
    if not token:
        print("no usable access token; run authorize/complete first", file=sys.stderr)
        return 1
    status, payload = models_request(token)
    print(f"GET /v1/models -> HTTP {status}")
    if status != 200:
        print(json.dumps(payload)[:400], file=sys.stderr)
        return 1
    try:
        everything = parse_model_catalog(payload)
        listed = [m for m in everything if m["visibility"] == "list"]
    except ModelCatalogError as exc:
        print(f"REFUSING: {exc}", file=sys.stderr)
        return 1
    print(f"{len(listed)} listed of {len(everything)} reported")
    for m in sorted(listed, key=lambda r: r["slug"]):
        print(f"  {m['slug']:34s} {m['display_name']}")
    target = args.model
    match = find_model(payload, target)
    print()
    if match is None:
        print(f"{target} available: NO (not present in the catalog)")
        return 1
    print(f"{target} available: YES  visibility={match['visibility']!r} "
          f"display_name={match['display_name']!r}")
    return 0


def cmd_infer(args, store: Store) -> int:
    token = _valid_access_token(store)
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


def reject_credentialish_argv(argv: list[str]) -> None:
    """Refuse a callback URL / authorization code on the command line.

    This runs BEFORE argparse: argparse echoes unrecognized values into its error
    text, so a stray `--callback-url 'http://…?code=…'` would copy the one-time
    authorization code into stderr and from there into logs and transcripts. The
    guard refuses without repeating the offending value.

    `--redirect-uri` (a static loopback URI with no code) remains allowed.
    """
    for arg in argv:
        lowered = arg.lower()
        if "callback-url" in lowered or "code=" in lowered:
            raise SystemExit(
                "REFUSING: a callback URL or authorization code must not be passed on the "
                "command line.\nargv is world-readable (/proc/<pid>/cmdline) and is echoed "
                "verbatim by argparse on error.\n"
                "Pipe it on stdin instead:  printf '%s\\n' \"$URL\" | "
                "<prog> complete"
            )


def main() -> int:
    reject_credentialish_argv(sys.argv[1:])
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--store", default=DEFAULT_STORE)
    sub = ap.add_subparsers(dest="cmd", required=True)

    a = sub.add_parser("authorize")
    a.add_argument("--redirect-uri", default=DEFAULT_REDIRECT)
    a.add_argument("--agent-name", default="Turnstone")
    a.add_argument("--listen", action="store_true",
                   help="capture the loopback callback here (use with ssh -L)")
    a.add_argument("--timeout", type=int, default=300)
    a.add_argument("--with-id-token-hint", action="store_true",
                   help="reauthorization only: send id_token_hint (URL then goes to a file)")

    sub.add_parser("complete", help="read the callback URL from stdin (input is not echoed)")
    sub.add_parser("status")
    sub.add_parser("refresh")
    m = sub.add_parser("models")
    m.add_argument("--model", default="gpt-6.1-sol")

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
