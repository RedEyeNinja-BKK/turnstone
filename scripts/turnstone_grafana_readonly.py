#!/usr/bin/env python3
"""Read-only Grafana integration for the existing turnstone-mgmt credential.

WHY IT LOOKS LIKE THIS
  The service account's token lives in ``/srv/turnstone/credentials/grafana/
  api_token`` as a root-only file (0700 dir, 0600 file). The Turnstone service
  user cannot read it, and the credential architecture says that is correct:
  the mode follows the consumer, and this consumer is a root-invoked operator
  command. This module therefore adds NO privileged launcher, NO systemd unit
  and NO sudoers entry. It is run on demand as root, exactly like the existing
  probe.

PRIVILEGE POSTURE
  A read-only probe does not imply a read-only credential. The turnstone-mgmt
  token is organization Admin (159 actions). This module therefore restricts
  itself structurally: it only ever issues GET against a fixed allow-list of
  paths. There is no free-form URL parameter, so the tool cannot be talked into
  a mutating call. It never mints a token and never changes permissions.

  Requests are issued with redirects DISABLED (a redirect could send the bearer
  token to another origin) and response bodies are never echoed on failure
  (a proxy error page can carry sensitive diagnostics).

Usage (as root):
    sudo python3 scripts/turnstone_grafana_readonly.py health
    sudo python3 scripts/turnstone_grafana_readonly.py dashboards [--limit N]
    sudo python3 scripts/turnstone_grafana_readonly.py datasources
    sudo python3 scripts/turnstone_grafana_readonly.py alerts
    sudo python3 scripts/turnstone_grafana_readonly.py org
    sudo python3 scripts/turnstone_grafana_readonly.py permissions
    sudo python3 scripts/turnstone_grafana_readonly.py all
"""
from __future__ import annotations

import argparse
import json
import os
import stat
import sys
import urllib.error
import urllib.parse
import urllib.request
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Callable

ORIGIN = "https://grafana.bkk.redeyeninja.com"
TOKEN_PATH = "/srv/turnstone/credentials/grafana/api_token"
MAX_TOKEN_BYTES = 8192
PAGE_LIMIT = 100
MAX_PAGES = 50


class GrafanaError(RuntimeError):
    """A refusal or transport failure. Never carries the token."""


class NoRedirects(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: ANN001, D102
        return None


def read_token(path: str = TOKEN_PATH) -> str:
    """Return the service-account token, enforcing the root-only contract."""
    if os.geteuid() != 0:
        raise GrafanaError("requires sudo/root to read the root-only token")
    return _read_validated(path)


def _read_validated(path: str) -> str:
    """Validate the file contract and return its contents.

    Split out from :func:`read_token` so the ownership/mode rules can be tested
    without being root -- otherwise only the euid refusal is ever exercised.
    """
    try:
        st = os.lstat(path)
    except OSError as exc:
        raise GrafanaError(f"token file unavailable ({type(exc).__name__})") from None
    if not stat.S_ISREG(st.st_mode):
        raise GrafanaError("token path is not a regular file")
    # Mode before ownership: both are contract violations, but the mode check is
    # the one that can be exercised on a non-root-owned fixture, so testing it
    # does not depend on who owns the file.
    if st.st_mode & 0o077:
        raise GrafanaError("token file must be mode 0600")
    if st.st_uid != 0:
        raise GrafanaError("token file is not root-owned")
    with open(path, "rb") as handle:  # noqa: PTH123
        raw = handle.read(MAX_TOKEN_BYTES + 1)
    if not raw or len(raw) > MAX_TOKEN_BYTES:
        raise GrafanaError("token must be non-empty and within the size bound")
    if any(byte < 33 or byte > 126 for byte in raw):
        raise GrafanaError("token must be printable ASCII without whitespace")
    return raw.decode("ascii")


# A fixed allow-list. Every entry is a GET-only, read-only surface. Adding a
# path here is a deliberate code change, not a runtime parameter.
ENDPOINTS: dict[str, Callable[[int], str]] = {
    "health": lambda _limit: "/api/health",
    "org": lambda _limit: "/api/org",
    "datasources": lambda _limit: "/api/datasources",
    "permissions": lambda _limit: "/api/access-control/user/permissions",
    # Grafana 13 keeps alerting under the unified alertmanager API.
    "alerts": lambda _limit: "/api/alertmanager/grafana/api/v2/alerts",
    "dashboards": lambda limit: f"/api/search?{urllib.parse.urlencode({'type': 'dash-db', 'limit': limit})}",
}


def _opener() -> urllib.request.OpenerDirector:
    return urllib.request.build_opener(NoRedirects)


def get_json(path: str, token: str, *, timeout: int = 12) -> tuple[int, Any]:
    """GET *path*. Returns (status, parsed-json-or-None). Body is never logged."""
    request = urllib.request.Request(  # noqa: S310 - origin is a fixed constant
        ORIGIN + path,
        method="GET",
        headers={"Authorization": "Bearer " + token, "Accept": "application/json"},
    )
    try:
        with _opener().open(request, timeout=timeout) as response:  # noqa: S310
            return response.status, json.load(response)
    except urllib.error.HTTPError as exc:
        # Deliberately discard the body: proxy diagnostics can leak.
        return exc.code, None
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise GrafanaError(f"transport failure ({type(exc).__name__})") from None


def fetch_dashboards(token: str, *, limit: int = PAGE_LIMIT) -> dict[str, Any]:
    """Enumerate dashboards, following pagination up to a hard page bound."""
    seen: dict[str, dict[str, Any]] = {}
    pages = 0
    while pages < MAX_PAGES:
        path = ENDPOINTS["dashboards"](limit) + f"&page={pages + 1}"
        status, body = get_json(path, token)
        if status != 200 or not isinstance(body, list):
            return {"status": status, "dashboards": list(seen.values()), "complete": False, "pages": pages}
        for item in body:
            uid = str(item.get("uid") or item.get("id") or "")
            if uid:
                seen[uid] = item
        pages += 1
        if len(body) < limit:
            return {"status": 200, "dashboards": list(seen.values()), "complete": True, "pages": pages}
    return {"status": 200, "dashboards": list(seen.values()), "complete": False, "pages": pages}


def summarize(name: str, body: Any) -> Any:
    """Project a response down to non-sensitive, operator-useful fields."""
    if body is None:
        return None
    if name == "health":
        if not isinstance(body, dict):
            return None
        return {
            "version": body.get("version"),
            "database": body.get("database"),
            "commit": str(body.get("commit", ""))[:12],
        }
    if name == "org":
        if not isinstance(body, dict):
            return None
        return {"id": body.get("id"), "name": body.get("name")}
    if name == "datasources":
        if not isinstance(body, list):
            return None
        return [
            {
                "uid": d.get("uid"),
                "name": d.get("name"),
                "type": d.get("type"),
                "isDefault": d.get("isDefault"),
                "readOnly": d.get("readOnly"),
            }
            for d in body
            if isinstance(d, dict)
        ]
    if name == "permissions":
        if not isinstance(body, dict):
            return None
        return {"action_count": len(body)}
    if name == "alerts":
        if not isinstance(body, list):
            return None
        counts: dict[str, int] = {}
        for alert in body:
            if isinstance(alert, dict):
                state = ((alert.get("status") or {}).get("state") or "unknown")
                counts[str(state)] = counts.get(str(state), 0) + 1
        return {"total": len(body), "by_state": counts}
    return body if not isinstance(body, str) else body[:200]


def run(name: str, token: str) -> dict[str, Any]:
    if name == "dashboards":
        result = fetch_dashboards(token)
        dashboards = result.pop("dashboards", [])
        return {**result, "count": len(dashboards), "titles": [d.get("title") for d in dashboards][:50]}
    path = ENDPOINTS[name](PAGE_LIMIT)
    status, body = get_json(path, token)
    return {"status": status, "data": summarize(name, body)}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Read-only Grafana checks")
    parser.add_argument(
        "command",
        choices=[*ENDPOINTS, "all"],
    )
    parser.add_argument("--limit", type=int, default=PAGE_LIMIT)
    args = parser.parse_args(argv)

    try:
        token = read_token()
    except GrafanaError as exc:
        print(f"REFUSED: {exc}", file=sys.stderr)
        return 2

    names = list(ENDPOINTS) if args.command == "all" else [args.command]
    out: dict[str, Any] = {}
    for name in names:
        try:
            out[name] = run(name, token)
        except GrafanaError as exc:
            out[name] = {"error": str(exc)}
    print(json.dumps(out, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
