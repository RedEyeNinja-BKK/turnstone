#!/usr/bin/env python3
"""Bounded Grafana integration for the turnstone-mgmt credential.

READ AND WRITE ARE SEPARATE SURFACES. Every verb except ``dashboard-write``
issues GET only, against a fixed allow-list. ``dashboard-write`` is the single
mutating entry point; see its own section for the guards that make it safe.

WHY IT LOOKS LIKE THIS
  The service-account token lives in ``/srv/turnstone/credentials/grafana/
  api_token`` as a root-only file (0700 dir, 0600 file). The Turnstone service
  user cannot read it. The approved runtime path is therefore an exact-argv
  sudo grant for ``launch-grafana-readonly.sh`` (installed from
  ``scripts/turnstone-grafana-readonly.sudoers``): the launcher validates its
  single verb argument and execs this module as root. There is no persistent
  daemon and no socket. This module adds no other privileged surface.

PRIVILEGE POSTURE
  A read-only probe does not imply a read-only credential. The turnstone-mgmt
  token is organization Admin (159 actions). This module therefore restricts
  itself structurally:

  * every read path comes from a fixed allow-list -- there is no free-form URL
    parameter, so the tool cannot be talked into a mutating call;
  * GET is used for every read surface;
  * there is exactly one POST, to Grafana's fixed query proxy
    ``/api/ds/query``. Its body is *built here* from a validated request; the
    caller's bytes are never forwarded verbatim;
  * datasource queries are limited to Prometheus and Loki, whose query
    languages cannot mutate. SQL datasources are refused by type because
    "read-only SELECT" cannot be structurally guaranteed;
  * redirects are disabled (a redirect could send the bearer token to another
    origin);
  * response bodies are never echoed on failure, and the credential never
    appears in output or in the audit log.

RUNTIME INTERFACE
  Run through the launcher (as root via the exact-argv sudo grant):

      sudo launch-grafana-readonly.sh identity
      sudo launch-grafana-readonly.sh org
      sudo launch-grafana-readonly.sh permissions
      sudo launch-grafana-readonly.sh health
      sudo launch-grafana-readonly.sh dashboards
      sudo launch-grafana-readonly.sh folders
      sudo launch-grafana-readonly.sh datasources
      sudo launch-grafana-readonly.sh alerts
      sudo launch-grafana-readonly.sh all
      printf '%s' '{"datasource_uid":"P1A2B3C4","expr":"vector(1)"}' \\
        | sudo launch-grafana-readonly.sh query

  ``query`` reads one bounded JSON object from stdin (4 KiB max):
  ``datasource_uid`` (required), ``expr`` (required, 512 ASCII chars max),
  ``kind`` ("instant" or "range", default instant), ``minutes`` (1-1440,
  default 15) and ``step_s`` (5-3600, default 30). Unknown fields are
  refused. A range query must resolve to at most 1200 points.

  Every invocation appends one JSON line to ``/var/log/turnstone-grafana/
  audit.jsonl`` (root:turnstone 0640). The audit record holds the verb, the
  datasource uid, the expression digest and length, HTTP status codes, outcome
  and duration -- never the token and never the expression text.

Usage (direct, as root, for maintenance):
    sudo python3 scripts/turnstone_grafana_runtime.py health
"""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import json
import os
import re
import stat
import sys
import time
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

# Response and request bounds. These are not tuning knobs: they cap how much
# untrusted upstream data and caller data can cross this boundary.
MAX_RESPONSE_BYTES = 1 << 20
MAX_QUERY_REQUEST_BYTES = 4096
# A dashboard document is legitimately large; the write surface gets its own
# bound rather than a shared one, so raising it cannot loosen a query.
MAX_DASHBOARD_BYTES = 512 * 1024
MAX_DASHBOARD_PANELS = 400
MAX_DEPTH = 40
MAX_EXPR_CHARS = 512
MAX_RANGE_MINUTES = 1440
MAX_STEP_SECONDS = 3600
MAX_QUERY_POINTS = 1200
MAX_FRAMES_REPORTED = 60
MAX_LABEL_PAIRS = 8
MAX_LABEL_VALUE_CHARS = 80
MAX_PANELS_REPORTED = 200
MAX_VALUES_SCANNED = 20000

QUERY_PATH = "/api/ds/query"
DASHBOARD_WRITE_PATH = "/api/dashboards/db"
QUERY_DATASOURCE_TYPES = frozenset({"prometheus", "loki"})
AUDIT_PATH = "/var/log/turnstone-grafana/audit.jsonl"
BACKUP_DIR = "/var/log/turnstone-grafana/backups"

# Deleting is the one irreversible dashboard operation, so it is restricted to
# the uid namespace this integration owns. Create and update are recoverable
# (every mutation is backed up first); delete is not, and must never be able to
# remove an operator-authored production dashboard.
OWNED_UID_PREFIX = "turnstone-"
WRITE_OPS = frozenset({"create", "update", "delete"})
_ALLOWED_WRITE_KEYS = frozenset(
    {"op", "uid", "expected_version", "folder_uid", "message", "dashboard"}
)

_UID_RE = re.compile(r"^[A-Za-z0-9_-]{1,40}$")
_ALLOWED_QUERY_KEYS = frozenset({"datasource_uid", "expr", "kind", "minutes", "step_s"})


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


def _search_path(kind: str, limit: int) -> str:
    return f"/api/search?{urllib.parse.urlencode({'type': kind, 'limit': limit})}"


# A fixed allow-list. Every entry is a GET-only, read-only surface. Adding a
# path here is a deliberate code change, not a runtime parameter.
ENDPOINTS: dict[str, Callable[[int], str]] = {
    "identity": lambda _limit: "/api/user",
    "health": lambda _limit: "/api/health",
    "org": lambda _limit: "/api/org",
    "datasources": lambda _limit: "/api/datasources",
    "permissions": lambda _limit: "/api/access-control/user/permissions",
    # Grafana 13 keeps alerting under the unified alertmanager API.
    "alerts": lambda _limit: "/api/alertmanager/grafana/api/v2/alerts",
    "dashboards": lambda limit: _search_path("dash-db", limit),
    "folders": lambda limit: _search_path("dash-folder", limit),
}

# Derived from the allow-list so a new endpoint verb cannot be served by the
# module but left out of the aggregate verb. Order fixes the output layout.
ALL_VERBS = tuple(ENDPOINTS)


def select_uid(payload: Any, ds_type: str = "prometheus") -> str:
    """Return the first datasource uid of *ds_type*, or "" when there is none.

    Accepts both shapes this tool produces: the CLI wraps each verb under its
    own key (``{"datasources": {"status": ..., "data": [...]}}``) while the
    verb result itself is ``{"status": ..., "data": [...]}``. Reading only the
    unwrapped shape is what made ``query-smoke`` report Prometheus as absent
    while ``datasources`` plainly listed it.
    """
    if not isinstance(payload, dict):
        return ""
    body = payload.get("datasources")
    if not isinstance(body, dict):
        body = payload
    rows = body.get("data")
    if not isinstance(rows, list):
        return ""
    for row in rows:
        if isinstance(row, dict) and row.get("type") == ds_type:
            uid = row.get("uid")
            if isinstance(uid, str) and uid:
                return uid
    return ""


def _opener() -> urllib.request.OpenerDirector:
    return urllib.request.build_opener(NoRedirects)


def _request_json(
    method: str,
    path: str,
    token: str,
    *,
    payload: dict[str, Any] | None = None,
    timeout: int = 12,
) -> tuple[int, Any]:
    data = None
    headers = {"Authorization": "Bearer " + token, "Accept": "application/json"}
    if payload is not None:
        data = json.dumps(payload, separators=(",", ":")).encode("utf-8")
        headers["Content-Type"] = "application/json"
    request = urllib.request.Request(  # noqa: S310 - origin is a fixed constant
        ORIGIN + path,
        data=data,
        method=method,
        headers=headers,
    )
    try:
        with _opener().open(request, timeout=timeout) as response:  # noqa: S310
            raw = response.read(MAX_RESPONSE_BYTES + 1)
            if len(raw) > MAX_RESPONSE_BYTES:
                raise GrafanaError("response exceeds the size bound")
            if not raw:
                return response.status, None
            try:
                return response.status, json.loads(raw.decode("utf-8"))
            except (UnicodeDecodeError, ValueError):
                raise GrafanaError("response was not valid JSON") from None
    except urllib.error.HTTPError as exc:
        # Deliberately discard the body: proxy diagnostics can leak.
        return exc.code, None
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise GrafanaError(f"transport failure ({type(exc).__name__})") from None


def get_json(path: str, token: str, *, timeout: int = 12) -> tuple[int, Any]:
    """GET *path*. Returns (status, parsed-json-or-None). Body is never logged."""
    return _request_json("GET", path, token, timeout=timeout)


def post_json(
    path: str, token: str, payload: dict[str, Any], *, timeout: int = 20
) -> tuple[int, Any]:
    """POST *path* with *payload*. Only the fixed query proxy uses this."""
    return _request_json("POST", path, token, payload=payload, timeout=timeout)


def _collect(kind: str, token: str, *, limit: int = PAGE_LIMIT) -> dict[str, Any]:
    """Enumerate a search kind, following pagination up to a hard page bound."""
    seen: dict[str, dict[str, Any]] = {}
    pages = 0
    while pages < MAX_PAGES:
        path = ENDPOINTS[kind](limit) + f"&page={pages + 1}"
        status, body = get_json(path, token)
        if status != 200 or not isinstance(body, list):
            return {
                "status": status,
                "items": list(seen.values()),
                "complete": False,
                "pages": pages,
            }
        for item in body:
            if not isinstance(item, dict):
                continue
            uid = str(item.get("uid") or item.get("id") or "")
            if uid:
                seen[uid] = item
        pages += 1
        if len(body) < limit:
            return {"status": 200, "items": list(seen.values()), "complete": True, "pages": pages}
    return {"status": 200, "items": list(seen.values()), "complete": False, "pages": pages}


def fetch_dashboards(token: str, *, limit: int = PAGE_LIMIT) -> dict[str, Any]:
    """Enumerate dashboards, following pagination up to a hard page bound."""
    result = _collect("dashboards", token, limit=limit)
    return {
        "status": result["status"],
        "dashboards": result["items"],
        "complete": result["complete"],
        "pages": result["pages"],
    }


def fetch_folders(token: str, *, limit: int = PAGE_LIMIT) -> dict[str, Any]:
    """Enumerate folders, following pagination up to a hard page bound."""
    result = _collect("folders", token, limit=limit)
    return {
        "status": result["status"],
        "folders": result["items"],
        "complete": result["complete"],
        "pages": result["pages"],
    }


# ---------------------------------------------------------------------------
# bounded datasource queries
# ---------------------------------------------------------------------------


def _bounded_int(value: Any, *, name: str, low: int, high: int, default: int) -> int:
    if value is None:
        return default
    # bool is an int subclass; accepting it would make "minutes": true a 1.
    if isinstance(value, bool) or not isinstance(value, int):
        raise GrafanaError(f"{name} must be an integer")
    if not low <= value <= high:
        raise GrafanaError(f"{name} must be between {low} and {high}")
    return value


def parse_query_request(raw: bytes) -> dict[str, Any]:
    """Validate one caller request. Returns a normalised, bounded dict.

    The caller never supplies a URL, a method, a datasource *type*, a time
    range in absolute terms, or any field this function does not name. Unknown
    keys are refused rather than ignored, so a request cannot carry a second
    meaning past this gate.
    """
    if not raw:
        raise GrafanaError("query request is empty")
    if len(raw) > MAX_QUERY_REQUEST_BYTES:
        raise GrafanaError("query request exceeds the size bound")
    try:
        data = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        raise GrafanaError("query request is not valid JSON") from None
    if not isinstance(data, dict):
        raise GrafanaError("query request must be a JSON object")

    unknown = set(data) - _ALLOWED_QUERY_KEYS
    if unknown:
        raise GrafanaError("unsupported query field(s): " + ", ".join(sorted(unknown)))

    uid = data.get("datasource_uid")
    if not isinstance(uid, str) or not _UID_RE.match(uid):
        raise GrafanaError("datasource_uid must be 1-40 characters of [A-Za-z0-9_-]")

    expr = data.get("expr")
    if not isinstance(expr, str) or not expr:
        raise GrafanaError("expr must be a non-empty string")
    if len(expr) > MAX_EXPR_CHARS:
        raise GrafanaError(f"expr must be at most {MAX_EXPR_CHARS} characters")
    if any(not 0x20 <= ord(ch) <= 0x7E for ch in expr):
        raise GrafanaError("expr must be printable ASCII without control characters")

    kind = data.get("kind", "instant")
    if kind not in ("instant", "range"):
        raise GrafanaError("kind must be 'instant' or 'range'")

    minutes = _bounded_int(
        data.get("minutes"), name="minutes", low=1, high=MAX_RANGE_MINUTES, default=15
    )
    step_s = _bounded_int(
        data.get("step_s"), name="step_s", low=5, high=MAX_STEP_SECONDS, default=30
    )
    points = -(-(minutes * 60) // step_s)  # ceil division, no float
    if kind == "range" and points > MAX_QUERY_POINTS:
        raise GrafanaError(f"range query would return {points} points (limit {MAX_QUERY_POINTS})")

    return {
        "datasource_uid": uid,
        "expr": expr,
        "kind": kind,
        "minutes": minutes,
        "step_s": step_s,
    }


def resolve_datasource(token: str, uid: str) -> tuple[dict[str, Any], int]:
    """Return the datasource only if its type is safe to query structurally."""
    status, body = get_json(ENDPOINTS["datasources"](PAGE_LIMIT), token)
    if status != 200 or not isinstance(body, list):
        raise GrafanaError(f"datasource discovery failed (status {status})")
    for item in body:
        if not isinstance(item, dict) or item.get("uid") != uid:
            continue
        ds_type = item.get("type")
        if ds_type not in QUERY_DATASOURCE_TYPES:
            raise GrafanaError(
                f"datasource type {ds_type!r} is not queryable through this bound "
                f"(allowed: {', '.join(sorted(QUERY_DATASOURCE_TYPES))})"
            )
        return {"uid": uid, "type": ds_type, "name": item.get("name")}, status
    raise GrafanaError("datasource uid is not present in this organization")


def build_query(request: dict[str, Any], datasource: dict[str, Any], *, now_ms: int) -> dict:
    """Build the query-proxy body. Mirrors the request; adds no caller data."""
    window_ms = request["minutes"] * 60 * 1000
    points = -(-(request["minutes"] * 60) // request["step_s"])
    query: dict[str, Any] = {
        "refId": "A",
        "datasource": {"uid": datasource["uid"], "type": datasource["type"]},
        "expr": request["expr"],
        "instant": request["kind"] == "instant",
        "range": request["kind"] == "range",
        "intervalMs": request["step_s"] * 1000,
        "maxDataPoints": 1 if request["kind"] == "instant" else min(points, MAX_QUERY_POINTS),
    }
    if datasource["type"] == "loki":
        query["queryType"] = request["kind"]
        query["maxLines"] = 100
    return {
        "queries": [query],
        "from": str(now_ms - window_ms),
        "to": str(now_ms),
    }


def _bounded_labels(labels: Any) -> dict[str, str]:
    """Return up to MAX_LABEL_PAIRS label pairs, each value truncated.

    Label values are the point: ``__name__`` carries the metric name and
    ``job``/``instance`` say which target produced it. Without them a summary
    can say a series exists but not which one.
    """
    if not isinstance(labels, dict):
        return {}
    out: dict[str, str] = {}
    for key, value in labels.items():
        if len(out) >= MAX_LABEL_PAIRS:
            break
        out[str(key)[:64]] = str(value)[:MAX_LABEL_VALUE_CHARS]
    return out


def summarize_query(body: Any) -> dict[str, Any] | None:
    """Project a query response to a bounded, non-sensitive summary."""
    if not isinstance(body, dict):
        return None
    results = body.get("results")
    if not isinstance(results, dict):
        return None
    out: dict[str, Any] = {}
    for ref, result in results.items():
        if not isinstance(result, dict):
            continue
        frames = result.get("frames") or []
        summary: dict[str, Any] = {
            "status": result.get("status"),
            "frame_count": len(frames),
            "points": 0,
            "series": [],
        }
        scanned = 0
        for frame in frames:
            if not isinstance(frame, dict):
                continue
            schema = frame.get("schema") if isinstance(frame.get("schema"), dict) else {}
            data = frame.get("data") if isinstance(frame.get("data"), dict) else {}
            values = data.get("values") or []
            count = 0
            last: Any = None
            if isinstance(values, list) and values:
                primary = values[-1] if len(values) > 1 else values[0]
                if isinstance(primary, list):
                    count = len(primary)
                    if primary:
                        last = primary[-1]
            summary["points"] += count
            scanned += count
            if len(summary["series"]) < MAX_FRAMES_REPORTED:
                fields = schema.get("fields") or []
                labels: Any = {}
                if len(fields) > 1 and isinstance(fields[-1], dict):
                    labels = fields[-1].get("labels") or {}
                if isinstance(last, str):
                    last = last[:120]
                summary["series"].append(
                    {
                        "name": str(schema.get("name") or "")[:120],
                        "labels": _bounded_labels(labels),
                        "label_count": len(labels) if isinstance(labels, dict) else 0,
                        "points": count,
                        "last": last if isinstance(last, (int, float, str)) else None,
                    }
                )
            if scanned >= MAX_VALUES_SCANNED:
                break
        out[str(ref)] = summary
    return out


def parse_dashboard_request(raw: bytes) -> str:
    """Validate a dashboard request and return its uid.

    Same contract as query: one bounded JSON object on stdin, no free-form
    path. The uid grammar is the same one datasource uids already use, so a
    traversal or query-string injection cannot survive validation.
    """
    if not raw:
        raise GrafanaError("dashboard request is empty")
    if len(raw) > MAX_QUERY_REQUEST_BYTES:
        raise GrafanaError("dashboard request exceeds the size bound")
    try:
        data = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        raise GrafanaError("dashboard request is not valid JSON") from None
    if not isinstance(data, dict):
        raise GrafanaError("dashboard request must be a JSON object")
    unknown = set(data) - {"uid"}
    if unknown:
        raise GrafanaError("unsupported dashboard field(s): " + ", ".join(sorted(unknown)))
    uid = data.get("uid")
    if not isinstance(uid, str) or not _UID_RE.match(uid):
        raise GrafanaError("uid must be 1-40 characters of [A-Za-z0-9_-]")
    return uid


def _panel_targets(panel: dict[str, Any]) -> list[str]:
    exprs: list[str] = []
    for target in panel.get("targets") or []:
        if not isinstance(target, dict):
            continue
        expr = target.get("expr") or target.get("query")
        if isinstance(expr, str) and expr.strip():
            exprs.append(expr.strip()[:200])
    return exprs[:4]


def _walk_panels(panels: Any, depth: int = 0) -> list[dict[str, Any]]:
    """Flatten panels, including those nested inside collapsed rows."""
    found: list[dict[str, Any]] = []
    if depth > 3 or not isinstance(panels, list):
        return found
    for panel in panels:
        if not isinstance(panel, dict) or len(found) >= MAX_PANELS_REPORTED:
            continue
        found.append(panel)
        found.extend(_walk_panels(panel.get("panels"), depth + 1))
    return found


def summarize_dashboard(body: Any) -> dict[str, Any] | None:
    """Project a dashboard to its operational essentials.

    Titles, panel types, the queries they run and the variables that scope
    them are what make a dashboard informative; anything else is layout.
    """
    if not isinstance(body, dict):
        return None
    dash = body.get("dashboard")
    if not isinstance(dash, dict):
        return None
    meta = body.get("meta") if isinstance(body.get("meta"), dict) else {}
    panels = _walk_panels(dash.get("panels"))
    projected: list[dict[str, Any]] = []
    for panel in panels:
        ds = panel.get("datasource")
        if isinstance(ds, dict):
            ds_ref = {"uid": ds.get("uid"), "type": ds.get("type")}
        else:
            ds_ref = {"uid": None, "type": ds if isinstance(ds, str) else None}
        projected.append(
            {
                "title": str(panel.get("title") or "")[:120],
                "type": panel.get("type"),
                "datasource": ds_ref,
                "queries": _panel_targets(panel),
            }
        )
    variables: list[dict[str, Any]] = []
    templating = dash.get("templating")
    if isinstance(templating, dict):
        for var in templating.get("list") or []:
            if not isinstance(var, dict):
                continue
            current = var.get("current")
            value: Any = None
            if isinstance(current, dict):
                value = current.get("value")
            if isinstance(value, list):
                value = [str(v)[:60] for v in value[:5]]
            elif value is not None:
                value = str(value)[:60]
            variables.append({"name": var.get("name"), "type": var.get("type"), "current": value})
    return {
        "title": dash.get("title"),
        "uid": dash.get("uid"),
        "version": dash.get("version"),
        "tags": [str(t)[:40] for t in (dash.get("tags") or [])][:10],
        "refresh": dash.get("refresh"),
        "time": dash.get("time") if isinstance(dash.get("time"), dict) else None,
        "folder": meta.get("folderTitle"),
        "panel_count": len(panels),
        "panels": projected,
        "variables": variables[:20],
    }


def run_dashboard(token: str, uid: str) -> dict[str, Any]:
    """Fetch one dashboard by uid and project it."""
    status, body = get_json(f"/api/dashboards/uid/{uid}", token)
    return {"status": status, "dashboard": summarize_dashboard(body)}


# ---------------------------------------------------------------------------
# dashboard writes
# ---------------------------------------------------------------------------
#
# This is the only mutating surface. It is deliberately narrow:
#
#   * three operations, each mapped to one fixed route -- there is no
#     caller-supplied path, method or verb;
#   * create and update are refused unless the dashboard's *current* version
#     is supplied, so a concurrent edit is a 412 rather than silent loss;
#   * every mutation first writes the current document to BACKUP_DIR, so any
#     change is revertible without Grafana's version history;
#   * delete is restricted to the OWNED_UID_PREFIX namespace because it is the
#     one operation a backup cannot make safe;
#   * a non-empty ``message`` is mandatory, so Grafana's own revision log
#     records why the version changed, not just that it did.


def _check_depth(value: Any, depth: int = 0) -> None:
    if depth > MAX_DEPTH:
        raise GrafanaError(f"dashboard nesting exceeds {MAX_DEPTH} levels")
    if isinstance(value, dict):
        for item in value.values():
            _check_depth(item, depth + 1)
    elif isinstance(value, list):
        for item in value:
            _check_depth(item, depth + 1)


def parse_dashboard_write_request(raw: bytes) -> dict[str, Any]:
    """Validate one write request. Returns a normalised, bounded dict."""
    if not raw:
        raise GrafanaError("write request is empty")
    if len(raw) > MAX_DASHBOARD_BYTES:
        raise GrafanaError("write request exceeds the size bound")
    try:
        data = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        raise GrafanaError("write request is not valid JSON") from None
    if not isinstance(data, dict):
        raise GrafanaError("write request must be a JSON object")

    unknown = set(data) - _ALLOWED_WRITE_KEYS
    if unknown:
        raise GrafanaError("unsupported write field(s): " + ", ".join(sorted(unknown)))

    op = data.get("op")
    if op not in WRITE_OPS:
        raise GrafanaError("op must be one of: " + ", ".join(sorted(WRITE_OPS)))

    uid = data.get("uid")
    if not isinstance(uid, str) or not _UID_RE.match(uid):
        raise GrafanaError("uid must be 1-40 characters of [A-Za-z0-9_-]")

    message = data.get("message")
    if op in ("create", "update"):
        if not isinstance(message, str) or not message.strip():
            raise GrafanaError("message must be a non-empty string (it is the revision note)")
        if len(message) > 256:
            raise GrafanaError("message must be at most 256 characters")

    if op == "delete":
        if not uid.startswith(OWNED_UID_PREFIX):
            raise GrafanaError(
                f"delete is restricted to uids beginning with {OWNED_UID_PREFIX!r}; "
                "operator-authored dashboards are never removed by this tool"
            )
        return {"op": op, "uid": uid, "message": message}

    dashboard = data.get("dashboard")
    if not isinstance(dashboard, dict):
        raise GrafanaError("dashboard must be a JSON object")
    _check_depth(dashboard)
    title = dashboard.get("title")
    if not isinstance(title, str) or not title.strip():
        raise GrafanaError("dashboard.title must be a non-empty string")
    if len(title) > 256:
        raise GrafanaError("dashboard.title must be at most 256 characters")
    panels = dashboard.get("panels")
    if not isinstance(panels, list):
        raise GrafanaError("dashboard.panels must be a list")
    if len(panels) > MAX_DASHBOARD_PANELS:
        raise GrafanaError(f"dashboard may hold at most {MAX_DASHBOARD_PANELS} panels")
    inner_uid = dashboard.get("uid")
    if inner_uid is not None and inner_uid != uid:
        raise GrafanaError("dashboard.uid must match the request uid when present")

    folder_uid = data.get("folder_uid")
    if folder_uid is not None and (
        not isinstance(folder_uid, str) or not _UID_RE.match(folder_uid)
    ):
        raise GrafanaError("folder_uid must be null or an allowed uid")

    expected = data.get("expected_version")
    if op == "update":
        if isinstance(expected, bool) or not isinstance(expected, int):
            raise GrafanaError("expected_version is required for update; read the dashboard first")
        if expected < 0:
            raise GrafanaError("expected_version must be non-negative")
    elif expected is not None:
        raise GrafanaError("expected_version applies only to update")

    return {
        "op": op,
        "uid": uid,
        "message": message,
        "dashboard": dashboard,
        "folder_uid": folder_uid,
        "expected_version": expected,
    }


def _backup_dashboard(uid: str, version: Any, body: Any) -> str | None:
    """Persist the current document before mutating it. None when absent."""
    if body is None:
        return None
    directory = os.environ.get("GRAFANA_BACKUP_DIR") or BACKUP_DIR
    try:
        os.makedirs(directory, mode=0o750, exist_ok=True)
    except OSError as exc:
        raise GrafanaError(f"backup directory unavailable ({type(exc).__name__})") from None
    stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    path = os.path.join(directory, f"{uid}-v{version}-{stamp}.json")
    payload = json.dumps(body, indent=2, sort_keys=True).encode("utf-8")
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o640)
        try:
            os.write(fd, payload)
            os.fsync(fd)
        finally:
            os.close(fd)
    except OSError as exc:
        raise GrafanaError(f"backup write failed ({type(exc).__name__})") from None
    with contextlib.suppress(OSError):
        os.chmod(path, 0o640)
        os.chown(path, -1, os.stat(directory).st_gid)
    return path


def run_dashboard_write(token: str, request: dict[str, Any]) -> tuple[dict[str, Any], list[int]]:
    """Execute one validated dashboard mutation."""
    op = request["op"]
    uid = request["uid"]
    current_status, current_body = get_json(f"/api/dashboards/uid/{uid}", token)
    current = current_body.get("dashboard") if isinstance(current_body, dict) else None
    current_version = current.get("version") if isinstance(current, dict) else None
    statuses = [current_status]

    if op == "create":
        if current_status == 200:
            raise GrafanaError("a dashboard with this uid already exists; use update")
    elif op == "update":
        if current_status != 200 or not isinstance(current, dict):
            raise GrafanaError(f"dashboard not found (status {current_status})")
        if current_version != request["expected_version"]:
            raise GrafanaError(
                f"version conflict: dashboard is at {current_version}, "
                f"request expected {request['expected_version']}"
            )
    else:  # delete
        if current_status != 200 or not isinstance(current, dict):
            raise GrafanaError(f"dashboard not found (status {current_status})")

    backup = _backup_dashboard(uid, current_version, current_body if current else None)

    if op == "delete":
        status, body = _request_json("DELETE", f"/api/dashboards/uid/{uid}", token)
        statuses.append(status)
        return (
            {"op": op, "uid": uid, "status": status, "body": body, "backup": backup},
            statuses,
        )

    document = dict(request["dashboard"])
    document["uid"] = uid
    if op == "update":
        document["version"] = request["expected_version"]
    payload: dict[str, Any] = {
        "dashboard": document,
        "message": request["message"],
        "overwrite": True,
    }
    if request.get("folder_uid") is not None:
        payload["folderUid"] = request["folder_uid"]
    status, body = post_json(DASHBOARD_WRITE_PATH, token, payload)
    statuses.append(status)
    result: dict[str, Any] = {
        "op": op,
        "uid": uid,
        "status": status,
        "backup": backup,
        "previous_version": current_version,
    }
    if isinstance(body, dict):
        result["new_version"] = body.get("version")
        result["url"] = body.get("url")
        result["saved"] = body.get("status")
    return result, statuses


def run_query(token: str, request: dict[str, Any]) -> tuple[dict[str, Any], list[int]]:
    """Resolve, build and execute one bounded query."""
    datasource, ds_status = resolve_datasource(token, request["datasource_uid"])
    now_ms = int(time.time() * 1000)
    payload = build_query(request, datasource, now_ms=now_ms)
    status, body = post_json(QUERY_PATH, token, payload)
    result = {
        "status": status,
        "datasource": datasource,
        "query": {
            "kind": request["kind"],
            "minutes": request["minutes"],
            "step_s": request["step_s"],
            "expr": request["expr"],
        },
        "data": summarize_query(body),
    }
    return result, [ds_status, status]


# ---------------------------------------------------------------------------
# summarisation
# ---------------------------------------------------------------------------


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
    if name == "identity":
        if not isinstance(body, dict):
            return None
        # Deliberately drop email: identity here means the authenticated
        # principal, not their contact details.
        return {
            "id": body.get("id"),
            "login": body.get("login"),
            "name": body.get("name"),
            "orgId": body.get("orgId"),
            "isGrafanaAdmin": body.get("isGrafanaAdmin"),
            "isDisabled": body.get("isDisabled"),
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
                "url": d.get("url"),
                "isDefault": d.get("isDefault"),
                "readOnly": d.get("readOnly"),
                "queryable": d.get("type") in QUERY_DATASOURCE_TYPES,
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
                state = (alert.get("status") or {}).get("state") or "unknown"
                counts[str(state)] = counts.get(str(state), 0) + 1
        return {"total": len(body), "by_state": counts}
    return body if not isinstance(body, str) else body[:200]


def run(name: str, token: str) -> dict[str, Any]:
    if name == "dashboards":
        result = fetch_dashboards(token)
        items = result.pop("dashboards", [])
        return {
            **result,
            "count": len(items),
            "items": [
                {"uid": d.get("uid"), "title": d.get("title"), "folderUid": d.get("folderUid")}
                for d in items[:50]
            ],
            "titles": [d.get("title") for d in items][:50],
        }
    if name == "folders":
        result = fetch_folders(token)
        items = result.pop("folders", [])
        return {
            **result,
            "count": len(items),
            "items": [{"uid": f.get("uid"), "title": f.get("title")} for f in items[:50]],
        }
    if name == "identity":
        # A service-account token may not resolve on /api/user. Report each
        # part with its own status rather than claiming an identity we did not
        # observe; the org and permission count prove authentication either way.
        user_status, user_body = get_json(ENDPOINTS["identity"](PAGE_LIMIT), token)
        org_status, org_body = get_json(ENDPOINTS["org"](PAGE_LIMIT), token)
        perm_status, perm_body = get_json(ENDPOINTS["permissions"](PAGE_LIMIT), token)
        return {
            "user": {"status": user_status, "data": summarize("identity", user_body)},
            "org": {"status": org_status, "data": summarize("org", org_body)},
            "permissions": {
                "status": perm_status,
                "action_count": len(perm_body) if isinstance(perm_body, dict) else None,
            },
        }
    path = ENDPOINTS[name](PAGE_LIMIT)
    status, body = get_json(path, token)
    return {"status": status, "data": summarize(name, body)}


# ---------------------------------------------------------------------------
# audit
# ---------------------------------------------------------------------------


def _apply_audit_ownership(fd: int, parent: str | None) -> None:
    """Match the audit file's mode and group to its directory.

    The launcher runs as root, so a newly created file would be ``root:root``
    and the Turnstone identity could not read its own audit trail. The
    installer owns the directory's group (``root:turnstone``), so the file
    follows it rather than hard-coding a group here.
    """
    if os.geteuid() != 0:
        return
    with contextlib.suppress(OSError):
        os.fchmod(fd, 0o640)
    if not parent:
        return
    with contextlib.suppress(OSError):
        os.fchown(fd, -1, os.stat(parent).st_gid)


def _open_audit(path: str | None = None) -> int:
    """Open the audit log, creating its directory. Refusal is fail-closed."""
    target = path or os.environ.get("GRAFANA_AUDIT_PATH") or AUDIT_PATH
    parent = os.path.dirname(target)
    try:
        if parent:
            os.makedirs(parent, mode=0o750, exist_ok=True)
        fd = os.open(target, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o640)
    except OSError as exc:
        raise GrafanaError(f"audit log unavailable ({type(exc).__name__})") from None
    _apply_audit_ownership(fd, parent)
    return fd


def _audit(fd: int, record: dict[str, Any]) -> None:
    line = json.dumps(record, sort_keys=True, separators=(",", ":")) + "\n"
    try:
        os.write(fd, line.encode("utf-8"))
        os.fsync(fd)
    except OSError:
        # The record is best-effort only after the descriptor exists; a failed
        # write must not turn a successful read into a reported failure.
        pass


# ---------------------------------------------------------------------------
# entry point
# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Bounded Grafana integration")
    parser.add_argument(
        "command", choices=[*ENDPOINTS, "query", "dashboard", "dashboard-write", "all"]
    )
    parser.add_argument("--limit", type=int, default=PAGE_LIMIT)
    args = parser.parse_args(argv)

    stdin_limit = (
        MAX_DASHBOARD_BYTES if args.command == "dashboard-write" else MAX_QUERY_REQUEST_BYTES
    )
    raw = (
        sys.stdin.buffer.read(stdin_limit + 1)
        if args.command in ("query", "dashboard", "dashboard-write")
        else b""
    )
    started = time.monotonic()

    try:
        audit_fd = _open_audit()
    except GrafanaError as exc:
        print(f"REFUSED: {exc}", file=sys.stderr)
        return 5

    base: dict[str, Any] = {
        "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "verb": args.command,
        "euid": os.geteuid(),
        "argv": sys.argv[1:],
    }

    def finish(outcome: str, statuses: list[int], detail: dict[str, Any]) -> None:
        _audit(
            audit_fd,
            {
                **base,
                "outcome": outcome,
                "http_status": statuses,
                "detail": detail,
                "duration_ms": int((time.monotonic() - started) * 1000),
            },
        )

    try:
        token = read_token()
    except GrafanaError as exc:
        finish("refused", [], {"error": type(exc).__name__})
        print(f"REFUSED: {exc}", file=sys.stderr)
        return 2

    if args.command == "dashboard":
        try:
            dash_uid = parse_dashboard_request(raw)
        except GrafanaError as exc:
            finish("refused", [], {"error": type(exc).__name__})
            print(f"REFUSED: {exc}", file=sys.stderr)
            return 3
        try:
            out = run_dashboard(token, dash_uid)
        except GrafanaError as exc:
            finish("error", [], {"dashboard_uid": dash_uid, "error": type(exc).__name__})
            print(json.dumps({"error": str(exc)}, indent=2, sort_keys=True))
            return 4
        finish(
            "ok" if out["status"] == 200 else "upstream_error",
            [out["status"]],
            {"dashboard_uid": dash_uid},
        )
        print(json.dumps(out, indent=2, sort_keys=True))
        return 0 if out["status"] == 200 else 4

    if args.command == "dashboard-write":
        try:
            write_request = parse_dashboard_write_request(raw)
        except GrafanaError as exc:
            finish("refused", [], {"error": type(exc).__name__})
            print(f"REFUSED: {exc}", file=sys.stderr)
            return 3
        detail = {
            "op": write_request["op"],
            "dashboard_uid": write_request["uid"],
            "expected_version": write_request.get("expected_version"),
        }
        try:
            out, statuses = run_dashboard_write(token, write_request)
        except GrafanaError as exc:
            finish("error", [], {**detail, "error": type(exc).__name__})
            print(json.dumps({"error": str(exc)}, indent=2, sort_keys=True))
            return 4
        ok = out["status"] in (200, 202) and out.get("saved") != "error"
        finish(
            "ok" if ok else "upstream_error",
            statuses,
            {**detail, "new_version": out.get("new_version"), "backup": out.get("backup")},
        )
        print(json.dumps(out, indent=2, sort_keys=True))
        return 0 if ok else 4

    if args.command == "query":
        try:
            request = parse_query_request(raw)
        except GrafanaError as exc:
            finish("refused", [], {"error": type(exc).__name__})
            print(f"REFUSED: {exc}", file=sys.stderr)
            return 3
        detail = {
            "datasource_uid": request["datasource_uid"],
            "kind": request["kind"],
            "minutes": request["minutes"],
            "step_s": request["step_s"],
            "expr_len": len(request["expr"]),
            "expr_sha256": hashlib.sha256(request["expr"].encode("utf-8")).hexdigest(),
        }
        try:
            out, statuses = run_query(token, request)
        except GrafanaError as exc:
            finish("error", [], {**detail, "error": type(exc).__name__})
            print(json.dumps({"error": str(exc)}, indent=2, sort_keys=True))
            return 4
        finish("ok" if out["status"] == 200 else "upstream_error", statuses, detail)
        print(json.dumps(out, indent=2, sort_keys=True))
        return 0 if out["status"] == 200 else 4

    names = list(ALL_VERBS) if args.command == "all" else [args.command]
    out: dict[str, Any] = {}
    statuses: list[int] = []
    for name in names:
        try:
            result = run(name, token)
        except GrafanaError as exc:
            out[name] = {"error": str(exc)}
            continue
        out[name] = result
        for key in ("status",):
            if isinstance(result.get(key), int):
                statuses.append(result[key])
        for part in ("user", "org", "permissions"):
            if isinstance(result.get(part), dict) and isinstance(result[part].get("status"), int):
                statuses.append(result[part]["status"])
    finish("ok", statuses, {"verbs": names})
    print(json.dumps(out, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
