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

    def read(self, n: int = -1) -> bytes:
        raw = json.dumps(self._body).encode()
        return raw if n is None or n < 0 else raw[:n]

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
                request.full_url,
                self.status,
                "err",
                {},
                None,  # type: ignore[arg-type]
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


# ---------------------------------------------------------------------------
# runtime interface: identity, discovery, bounded queries
# ---------------------------------------------------------------------------

_SCRIPTS = pathlib.Path(__file__).resolve().parents[1] / "scripts"
LAUNCHER = _SCRIPTS / "launch-grafana-readonly.sh"
SUDOERS = _SCRIPTS / "turnstone-grafana-readonly.sudoers"
INSTALLER = _SCRIPTS / "install_grafana_runtime.sh"


def _refuses(raw: bytes) -> str:
    try:
        grafana.parse_query_request(raw)
    except grafana.GrafanaError as exc:
        return str(exc)
    raise AssertionError(f"request must be refused: {raw!r}")


def test_identity_summary_omits_email() -> None:
    out = grafana.summarize(
        "identity",
        {
            "id": 7,
            "login": "svc",
            "name": "Service",
            "email": "person@example.invalid",
            "orgId": 1,
            "isGrafanaAdmin": False,
        },
    )
    assert "email" not in out
    assert out["login"] == "svc"
    assert out["orgId"] == 1


def test_folder_and_dashboard_search_kinds_are_distinct() -> None:
    assert "type=dash-folder" in grafana.ENDPOINTS["folders"](100)
    assert "type=dash-db" in grafana.ENDPOINTS["dashboards"](100)


def test_query_request_defaults_are_normalised() -> None:
    req = grafana.parse_query_request(b'{"datasource_uid":"P1abc","expr":"vector(1)"}')
    assert req == {
        "datasource_uid": "P1abc",
        "expr": "vector(1)",
        "kind": "instant",
        "minutes": 15,
        "step_s": 30,
    }


def test_query_request_refuses_unknown_fields() -> None:
    message = _refuses(b'{"datasource_uid":"P1","expr":"up","url":"https://evil.invalid"}')
    assert "url" in message


def test_query_request_refuses_control_characters_and_non_ascii() -> None:
    assert "ASCII" in _refuses(b'{"datasource_uid":"P1","expr":"up\\nvector(1)"}')
    assert "ASCII" in _refuses('{"datasource_uid":"P1","expr":"üp"}'.encode())


def test_query_request_refuses_bool_as_integer() -> None:
    assert "integer" in _refuses(b'{"datasource_uid":"P1","expr":"up","minutes":true}')


def test_query_request_refuses_an_oversized_window() -> None:
    message = _refuses(
        b'{"datasource_uid":"P1","expr":"up","kind":"range","minutes":120,"step_s":5}'
    )
    assert "points" in message


def test_query_request_refuses_excess_bytes() -> None:
    raw = b'{"datasource_uid":"P1","expr":"' + b"a" * grafana.MAX_QUERY_REQUEST_BYTES + b'"}'
    assert "size bound" in _refuses(raw)


def test_datasource_resolution_refuses_sql_by_type() -> None:
    original = grafana.get_json

    def fake(path: str, token: str, *, timeout: int = 12):  # noqa: ANN001
        return 200, [{"uid": "sql1", "name": "pg", "type": "postgres"}]

    grafana.get_json = fake  # type: ignore[assignment]
    try:
        grafana.resolve_datasource("tok", "sql1")
    except grafana.GrafanaError as exc:
        assert "not queryable" in str(exc)
    else:
        raise AssertionError("a SQL datasource must be refused")
    finally:
        grafana.get_json = original


def test_datasource_resolution_refuses_an_unknown_uid() -> None:
    original = grafana.get_json

    def fake(path: str, token: str, *, timeout: int = 12):  # noqa: ANN001
        return 200, [{"uid": "prom1", "name": "p", "type": "prometheus"}]

    grafana.get_json = fake  # type: ignore[assignment]
    try:
        try:
            grafana.resolve_datasource("tok", "other")
        except grafana.GrafanaError as exc:
            assert "not present" in str(exc)
        else:
            raise AssertionError("an unknown uid must be refused")
    finally:
        grafana.get_json = original


# The exact payload the CLI produced against Grafana 13.2.4: main() wraps each
# verb result under its own key. query-smoke parsed the unwrapped shape and
# therefore saw no datasources at all.
_REAL_DATASOURCES_PAYLOAD = {
    "datasources": {
        "data": [
            {
                "isDefault": False,
                "name": "loki",
                "queryable": True,
                "readOnly": False,
                "type": "loki",
                "uid": "bfajhiz1i96o0c",
            },
            {
                "isDefault": True,
                "name": "prometheus",
                "queryable": True,
                "readOnly": False,
                "type": "prometheus",
                "uid": "df7wrp11a9hq8c",
            },
        ],
        "status": 200,
    }
}


def test_select_uid_reads_the_wrapped_cli_shape() -> None:
    assert grafana.select_uid(_REAL_DATASOURCES_PAYLOAD) == "df7wrp11a9hq8c"


def test_select_uid_also_reads_the_unwrapped_shape() -> None:
    assert grafana.select_uid(_REAL_DATASOURCES_PAYLOAD["datasources"]) == "df7wrp11a9hq8c"


def test_select_uid_is_empty_when_the_type_is_absent() -> None:
    payload = {"datasources": {"data": _REAL_DATASOURCES_PAYLOAD["datasources"]["data"][:1]}}
    assert grafana.select_uid(payload) == ""
    assert grafana.select_uid({"data": []}) == ""
    assert grafana.select_uid(None) == ""


def test_select_uid_ignores_malformed_rows() -> None:
    payload = {"data": ["nope", {"uid": "x"}, {"type": "prometheus", "uid": "ok"}]}
    assert grafana.select_uid(payload) == "ok"


def test_datasource_summary_exposes_the_backend_url() -> None:
    out = grafana.summarize(
        "datasources",
        [
            {
                "uid": "p",
                "name": "prometheus",
                "type": "prometheus",
                "url": "https://prometheus.example.invalid",
                "secureJsonData": {"password": "x"},
            }
        ],
    )
    assert out[0]["url"] == "https://prometheus.example.invalid"
    assert "secureJsonData" not in out[0]


def test_build_query_instant_is_bounded() -> None:
    request = grafana.parse_query_request(b'{"datasource_uid":"P1","expr":"up","minutes":5}')
    ds = {"uid": "P1", "type": "prometheus", "name": "p"}
    payload = grafana.build_query(request, ds, now_ms=1_700_000_000_000)
    assert payload["to"] == "1700000000000"
    assert payload["from"] == str(1_700_000_000_000 - 5 * 60 * 1000)
    query = payload["queries"][0]
    assert query["instant"] is True
    assert query["range"] is False
    assert query["maxDataPoints"] == 1
    assert "queryType" not in query  # Prometheus, not Loki


def test_build_query_range_caps_points_and_sets_loki_type() -> None:
    request = grafana.parse_query_request(
        b'{"datasource_uid":"L1","expr":"{job=~\\".+\\"}","kind":"range","minutes":10,"step_s":60}'
    )
    ds = {"uid": "L1", "type": "loki", "name": "l"}
    query = grafana.build_query(request, ds, now_ms=1_700_000_000_000)["queries"][0]
    assert query["maxDataPoints"] == 10
    assert query["intervalMs"] == 60_000
    assert query["queryType"] == "range"
    assert query["maxLines"] == 100


def test_audit_file_follows_the_directory_group() -> None:
    import types

    calls: list[tuple[str, tuple]] = []

    class _Stat:
        st_gid = 105

    shim = types.SimpleNamespace(
        geteuid=lambda: 0,
        fchmod=lambda fd, mode: calls.append(("fchmod", (fd, mode))),
        fchown=lambda fd, uid, gid: calls.append(("fchown", (fd, uid, gid))),
        stat=lambda path: _Stat(),
    )
    original = grafana.os
    grafana.os = shim  # type: ignore[assignment]
    try:
        grafana._apply_audit_ownership(7, "/var/log/turnstone-grafana")
    finally:
        grafana.os = original

    assert ("fchmod", (7, 0o640)) in calls
    # -1 leaves the owner (root) alone; the group follows the directory.
    assert ("fchown", (7, -1, 105)) in calls


def test_audit_ownership_is_a_no_op_for_non_root() -> None:
    import types

    calls: list[str] = []
    shim = types.SimpleNamespace(
        geteuid=lambda: 1001,
        fchmod=lambda fd, mode: calls.append("fchmod"),
        fchown=lambda fd, uid, gid: calls.append("fchown"),
        stat=lambda path: types.SimpleNamespace(st_gid=105),
    )
    original = grafana.os
    grafana.os = shim  # type: ignore[assignment]
    try:
        grafana._apply_audit_ownership(7, "/tmp")
    finally:
        grafana.os = original
    assert calls == []


def test_post_is_used_only_for_the_fixed_query_path() -> None:
    opener = _Opener(200, {"results": {}})
    _patch(opener)
    grafana.post_json(grafana.QUERY_PATH, "SUPER-SECRET-TOKEN", {"queries": []})
    request = opener.requests[0]
    assert request.get_method() == "POST"
    assert request.full_url.endswith("/api/ds/query")
    assert "SUPER-SECRET-TOKEN" not in request.full_url
    assert request.get_header("Authorization", "").startswith("Bearer ")


def test_oversized_response_is_refused() -> None:
    class _Big:
        status = 200

        def read(self, n: int = -1) -> bytes:
            return b"x" * (grafana.MAX_RESPONSE_BYTES + 1)

        def __enter__(self):  # noqa: ANN204
            return self

        def __exit__(self, *exc: Any) -> None:
            return None

    class _BigOpener:
        def open(self, request, timeout=None):  # noqa: ANN001
            return _Big()

    grafana._opener = lambda: _BigOpener()  # type: ignore[assignment]
    try:
        grafana.get_json("/api/health", "tok")
    except grafana.GrafanaError as exc:
        assert "size bound" in str(exc)
    else:
        raise AssertionError("an oversized response must be refused")


def test_audit_record_hashes_the_expression_and_never_the_token() -> None:
    import hashlib
    import os
    import tempfile

    with tempfile.TemporaryDirectory(dir="/var/tmp") as tmp:
        path = os.path.join(tmp, "audit.jsonl")
        fd = grafana._open_audit(path)
        try:
            grafana._audit(
                fd,
                {
                    "verb": "query",
                    "detail": {
                        "expr_len": 2,
                        "expr_sha256": hashlib.sha256(b"up").hexdigest(),
                        "datasource_uid": "P1",
                    },
                },
            )
        finally:
            os.close(fd)
        line = pathlib.Path(path).read_text(encoding="utf-8")
    record = json.loads(line)
    assert record["detail"]["expr_sha256"] == hashlib.sha256(b"up").hexdigest()
    assert "expr" not in record["detail"]
    assert "SUPER-SECRET-TOKEN" not in line


def test_query_summary_is_bounded() -> None:
    body = {
        "results": {
            "A": {
                "status": 200,
                "frames": [
                    {
                        "schema": {"name": "f", "fields": [{}, {"labels": {"a": "b"}}]},
                        "data": {"values": [[1000, 2000], [1, 2]]},
                    }
                ],
            }
        }
    }
    out = grafana.summarize_query(body)
    assert out is not None
    assert out["A"]["points"] == 2
    assert out["A"]["series"][0]["last"] == 2
    assert out["A"]["series"][0]["label_count"] == 1


def test_aggregate_verb_covers_every_read_endpoint() -> None:
    assert "query" not in grafana.ALL_VERBS
    assert set(grafana.ALL_VERBS) == set(grafana.ENDPOINTS)


def test_sudoers_verbs_match_the_launcher_exactly() -> None:
    import re

    rule_verbs = set(
        re.findall(r"launch-grafana-readonly\.sh ([a-z]+)$", SUDOERS.read_text(), re.M)
    )
    case_line = next(
        line.strip()
        for line in LAUNCHER.read_text().splitlines()
        if line.strip().startswith("identity|")
    )
    launcher_verbs = set(case_line.split(")")[0].split("|"))
    # Derived from the module, never a hand-maintained list: a verb the module
    # serves but the launcher or the sudoers template omits is unreachable at
    # runtime (the deployed 'org' verb was exactly that defect).
    expected = set(grafana.ENDPOINTS) | {"all", "query"}
    assert launcher_verbs == expected
    assert rule_verbs == expected


def test_every_endpoint_verb_is_reachable_through_the_grant() -> None:
    import re

    rule_verbs = set(
        re.findall(r"launch-grafana-readonly\.sh ([a-z]+)$", SUDOERS.read_text(), re.M)
    )
    missing = set(grafana.ENDPOINTS) - rule_verbs
    assert not missing, f"verbs served by the module but grantless: {sorted(missing)}"


def test_sudoers_grants_no_wildcards_or_unrestricted_powers() -> None:
    text = SUDOERS.read_text()
    assert "*" not in text
    assert "NOPASSWD: ALL" not in text
    assert "SETENV:" not in text
    expected = set(grafana.ENDPOINTS) | {"all", "query"}
    assert text.count("NOPASSWD:") == len(expected)
    # The existing self-management bridge grant must not be touched here.
    assert "launch-self-mcp" not in text


def test_launcher_and_installer_parse() -> None:
    import subprocess

    for script in (LAUNCHER, INSTALLER):
        subprocess.run(["bash", "-n", str(script)], check=True)


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
