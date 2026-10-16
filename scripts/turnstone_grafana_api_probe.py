#!/usr/bin/env python3
"""Read-only Grafana API checks using an existing root-only staged token.

No token arguments, logging, file modifications, redirected token forwarding,
API writes, or service integration. Run as root with sudo.
"""
import json
import os
import ssl
import stat
import sys
import urllib.error
import urllib.request
from pathlib import Path

ORIGIN = "https://grafana.bkk.redeyeninja.com"
TOKEN_PATH = Path("/srv/turnstone/credentials/grafana/api_token")


class NoRedirects(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def token_from_private_file():
    if os.geteuid() != 0:
        raise RuntimeError("Requires sudo/root to read the root-only token")
    st = TOKEN_PATH.lstat()
    if not stat.S_ISREG(st.st_mode) or st.st_uid != 0 or (st.st_mode & 0o077):
        raise RuntimeError("Token file must be a root-owned regular file mode 0600")
    with TOKEN_PATH.open("rb") as f:
        b = f.read(8193)
    if not b or len(b) > 8192 or any(x < 33 or x > 126 for x in b):
        raise RuntimeError("Token must be nonempty, printable ASCII without whitespace")
    return b.decode("ascii")


def get_json(opener, token, path):
    request = urllib.request.Request(
        ORIGIN + path,
        method="GET",
        headers={"Authorization": "Bearer " + token, "Accept": "application/json"},
    )
    try:
        with opener.open(request, timeout=12) as response:
            return response.status, json.load(response)
    except urllib.error.HTTPError as exc:
        # Never print response body; proxies may include sensitive diagnostic content.
        return exc.code, None


def main():
    token = token_from_private_file()
    opener = urllib.request.build_opener(
        NoRedirects(), urllib.request.HTTPSHandler(context=ssl.create_default_context())
    )
    targets = [
        ("organization", "/api/org/"),
        ("dashboard_search", "/api/search?type=dash-db&limit=1"),
        ("datasources", "/api/datasources"),
        ("organization_users", "/api/org/users"),
    ]
    statuses = {}
    for label, path in targets:
        try:
            status, obj = get_json(opener, token, path)
            statuses[label] = status
            if status == 200 and label == "organization" and isinstance(obj, dict):
                print(f"{label}: HTTP 200 org_id={obj.get('id')} name={str(obj.get('name',''))[:64]!r}")
            elif status == 200 and isinstance(obj, list):
                print(f"{label}: HTTP 200 readable (returned={len(obj)})")
            else:
                print(f"{label}: HTTP {status}")
        except (urllib.error.URLError, TimeoutError, ValueError, OSError) as exc:
            statuses[label] = "ERROR"
            print(f"{label}: ERROR ({type(exc).__name__}; details withheld)")
    print("Scope note: org-scoped service-account Admin is not Grafana server Admin.")
    if statuses.get("organization") != 200:
        sys.exit("Grafana token authentication not confirmed; no changes made.")


if __name__ == "__main__":
    try:
        main()
    except (OSError, RuntimeError, ValueError) as exc:
        print(f"REFUSED: {type(exc).__name__} (details withheld)", file=sys.stderr)
        sys.exit(1)
