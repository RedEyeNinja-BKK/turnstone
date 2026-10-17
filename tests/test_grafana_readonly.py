# SPDX-License-Identifier: Apache-2.0
"""Read-only Grafana integration: allow-list, token guard, summarisation.

The credential is organization Admin (159 actions), so the safety of this tool
rests on structure rather than on the token's own permissions:

* only GET is ever issued;
* the path always comes from a fixed allow-list -- there is no free-form URL
  input to smuggle a mutating call through;
* redirects are refused (a redirect could carry the bearer token off-origin);
* response bodies are dropped on failure and tokens never appear in output.

Network calls are exercised through a stub opener, so this suite is offline.

Run standalone::

    TMPDIR=/var/tmp /opt/turnstone-venv/bin/python tests/test_grafana_readonly.py
"""
from __future__ import annotations

import importlib.util
import json
import pathlib
import sys
import urllib.error
from typing import Any

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

_SCRIPT = pathlib.Path(__file__).resolve().parents[1] / "scripts" / "turnstone_grafana_readonly.py"
_spec = importlib.util.spec_from_file_location("grafana_readonly", _SCRIPT)
assert _spec and _spec.loader
grafana = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(grafana)


class _Response:
    def __init__(self, status: int, body: Any) -> None:
        self.status = status
        self._body = body

    def read(self) -> bytes:
        return json.dumps(self._body).encode()

    def __enter__(self) -> _Response:
        return self

    def __exit__(self, *exc: Any) -> None:
        return None


class _Opener:
    def __init__(self, status: int = 200, body: Any = None) -> None:
        self.status = status
        self.body = body
        self.requests: list[Any] = []

    def open(self, request, timeout=None):  # noqa: ANN001
        self.requests.append(request)
        if self.status >= 400:
            raise urllib.error.HTTPError(
                request.full_url, self.status, "err", {}, None  # type: ignore[arg-type]
            )
        return _Response(self.status, self.body)


def _patch(opener: _Opener) -> None:
    grafana._opener = lambda: opener  # type: ignore[assignment]


# ---------------------------------------------------------------------------
# structure: read-only, allow-listed
# ---------------------------------------------------------------------------


def test_every_endpoint_is_a_fixed_get_path() -> None:
    for name, build in grafana.ENDPOINTS.items():
        path = build(100)
        assert path.startswith("/api/"), f"{name} must be an /api path"
        assert " " not in path


def test_only_get_is_issued() -> None:
    opener = _Opener(200, {"version": "13.2.4"})
    _patch(opener)
    grafana.get_json("/api/health", "tok")
    assert opener.requests[0].get_method() == "GET"


def test_unknown_command_is_rejected_by_the_parser() -> None:
    try:
        grafana.main(["delete-everything"])
    except SystemExit as exc:
        assert exc.code != 0
    else:
        raise AssertionError("a non-allow-listed command must not parse")


def test_redirects_are_not_followed() -> None:
    handler = grafana.NoRedirects()
    assert handler.redirect_request(None, None, 302, "m", {}, "https://evil.invalid") is None


# ---------------------------------------------------------------------------
# failure handling never leaks
# ---------------------------------------------------------------------------


def test_http_error_body_is_discarded() -> None:
    opener = _Opener(500, {"secret": "should-not-surface"})
    _patch(opener)
    status, body = grafana.get_json("/api/health", "tok")
    assert status == 500
    assert body is None


def test_transport_failure_names_only_the_class() -> None:
    class _Boom:
        def open(self, request, timeout=None):  # noqa: ANN001
            raise urllib.error.URLError("connection refused to internal host")

    grafana._opener = lambda: _Boom()  # type: ignore[assignment]
    try:
        grafana.get_json("/api/health", "tok")
    except grafana.GrafanaError as exc:
        assert "internal host" not in str(exc)
        assert "URLError" in str(exc)
    else:
        raise AssertionError("transport failure must raise GrafanaError")


def test_token_is_never_in_the_request_url() -> None:
    opener = _Opener(200, {})
    _patch(opener)
    grafana.get_json("/api/org", "SUPER-SECRET-TOKEN")
    url = opener.requests[0].full_url
    assert "SUPER-SECRET-TOKEN" not in url
    assert opener.requests[0].get_header("Authorization", "").startswith("Bearer ")


# ---------------------------------------------------------------------------
# token guard
# ---------------------------------------------------------------------------


def test_token_guard_rejects_a_loose_mode() -> None:
    import os
    import tempfile

    with tempfile.TemporaryDirectory(dir="/var/tmp") as tmp:
        path = pathlib.Path(tmp) / "api_token"
        path.write_text("tok", encoding="utf-8")
        os.chmod(path, 0o644)  # too open
        try:
            grafana._read_validated(str(path))
        except grafana.GrafanaError as exc:
            assert "0600" in str(exc)
        else:
            raise AssertionError("a group/world-readable token must be refused")


# ---------------------------------------------------------------------------
# pagination and summarisation
# ---------------------------------------------------------------------------


def test_read_token_refuses_without_root() -> None:
    import os

    if os.geteuid() == 0:
        return
    try:
        grafana.read_token()
    except grafana.GrafanaError as exc:
        assert "root" in str(exc)
    else:
        raise AssertionError("a non-root caller must be refused")


def test_token_guard_rejects_a_non_root_owned_file() -> None:
    import os
    import tempfile

    with tempfile.TemporaryDirectory(dir="/var/tmp") as tmp:
        path = pathlib.Path(tmp) / "api_token"
        path.write_text("tok", encoding="utf-8")
        os.chmod(path, 0o600)
        if os.stat(path).st_uid == 0:
            return  # running as root: ownership check cannot be isolated here
        try:
            grafana._read_validated(str(path))
        except grafana.GrafanaError as exc:
            assert "root-owned" in str(exc)
        else:
            raise AssertionError("a non-root-owned token must be refused")


def test_dashboards_follow_pagination_and_deduplicate() -> None:
    pages = {
        1: [{"uid": f"u{i}", "title": f"d{i}"} for i in range(2)],
        2: [{"uid": "u2", "title": "d2"}],
    }

    def fake(path: str, token: str, *, timeout: int = 12):  # noqa: ANN001
        page = int(path.rsplit("page=", 1)[1])
        return 200, pages.get(page, [])

    original = grafana.get_json
    grafana.get_json = fake  # type: ignore[assignment]
    try:
        result = grafana.fetch_dashboards("tok", limit=2)
    finally:
        grafana.get_json = original
    assert result["complete"] is True
    assert result["pages"] == 2
    assert len(result["dashboards"]) == 3


def test_summaries_drop_sensitive_fields() -> None:
    ds = grafana.summarize(
        "datasources",
        [{"uid": "a", "name": "prom", "type": "prometheus", "secureJsonData": {"password": "x"}}],
    )
    assert ds and "secureJsonData" not in ds[0]
    assert ds[0]["name"] == "prom"


def test_alert_summary_counts_by_state() -> None:
    body = [
        {"status": {"state": "active"}},
        {"status": {"state": "active"}},
        {"status": {"state": "suppressed"}},
    ]
    out = grafana.summarize("alerts", body)
    assert out["total"] == 3
    assert out["by_state"] == {"active": 2, "suppressed": 1}


def test_permissions_summary_counts_without_listing() -> None:
    out = grafana.summarize("permissions", {"dashboards:read": ["*"], "orgs:read": ["*"]})
    assert out == {"action_count": 2}


def _run_all() -> int:
    tests = [(n, o) for n, o in sorted(globals().items()) if n.startswith("test_") and callable(o)]
    passed = failed = 0
    for name, fn in tests:
        try:
            fn()
            passed += 1
            print(f"  PASS {name}")
        except Exception as exc:  # noqa: BLE001
            failed += 1
            print(f"  FAIL {name}: {type(exc).__name__}: {exc}")
    print(f"\n  {passed} passed, {failed} failed, {len(tests)} total")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(_run_all())
