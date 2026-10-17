# SPDX-License-Identifier: Apache-2.0
"""Bounded Switchyard integration: closed verbs, input filters, views.

The safety of this tool rests on structure, not on the remote account's own
permissions (which are broad):

* every remote command is a module constant -- caller input never reaches an
  SSH command line, a shell word or a path;
* the two input-taking verbs use their input only as an in-process filter
  after the remote output has been fetched;
* outputs are size-bounded.

The transport is not exercised here. These tests cover the pure surface.

Run standalone::

    TMPDIR=/var/tmp /opt/turnstone-venv/bin/python tests/test_switchyard_runtime.py
"""

from __future__ import annotations

import importlib.util
import json
import pathlib
import sys

_SCRIPT = (
    pathlib.Path(__file__).resolve().parents[1] / "scripts" / "turnstone_switchyard_runtime.py"
)
_spec = importlib.util.spec_from_file_location("switchyard_runtime", _SCRIPT)
assert _spec and _spec.loader
sy = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(sy)


def _raises(fn, *args, **kwargs) -> bool:
    return _raises_as(sy.RequestRefusedError, fn, *args, **kwargs)


def _raises_as(kind, fn, *args, **kwargs) -> bool:
    try:
        fn(*args, **kwargs)
    except kind:
        return True
    return False


# --------------------------------------------------------------------------
# request parsing
# --------------------------------------------------------------------------


def test_route_name_accepts_manifest_identifiers() -> None:
    for name in ("smart", "smartfree-bounded", "tier1", "direct-openrouter-xing4_0-29b-a4b-free"):
        assert sy.parse_route_name(name) == name


def test_route_name_rejects_traversal_and_whitespace() -> None:
    for name in ("../etc/passwd", "a b", "", "x" * (sy.MAX_ROUTE_NAME + 1), "route/child", "a$b"):
        assert _raises(sy.parse_route_name, name), name


def test_route_name_rejects_non_strings() -> None:
    for value in (None, 1, [], {}, True):
        assert _raises(sy.parse_route_name, value), repr(value)


def test_route_request_requires_route() -> None:
    assert _raises(sy.parse_route_request, b"{}")
    assert _raises(sy.parse_route_request, b"")


def test_route_request_rejects_unknown_fields() -> None:
    assert _raises(sy.parse_route_request, b'{"route":"smart","cmd":"id"}')
    assert sy.parse_route_request(b'{"route":"smart"}') == "smart"


def test_route_request_rejects_bad_json_and_non_object() -> None:
    assert _raises(sy.parse_route_request, b"{")
    assert _raises(sy.parse_route_request, b'["smart"]')
    assert _raises(sy.parse_route_request, b"null")


def test_route_request_is_size_bounded() -> None:
    assert _raises(sy.parse_route_request, b"{" + b" " * sy.MAX_STDIN_BYTES + b"}")


def test_log_request_defaults() -> None:
    assert sy.parse_log_request(b"{}") == (None, 25)


def test_log_request_accepts_filter_and_limit() -> None:
    assert sy.parse_log_request(b'{"route":"smartfree","limit":5}') == ("smartfree", 5)


def test_log_request_bounds_limit() -> None:
    for limit in (0, -1, sy.MAX_LOG_RETURN + 1, "10", 1.5, True):
        assert _raises(sy.parse_log_request, json.dumps({"limit": limit}).encode()), limit


def test_log_request_rejects_unknown_fields() -> None:
    assert _raises(sy.parse_log_request, b'{"tail":"/etc/shadow"}')


# --------------------------------------------------------------------------
# catalog / route views
# --------------------------------------------------------------------------

_DOC = {
    "schema_version": 1,
    "llm_clients": {"openrouter": {"api_key_env": "SY_X", "base_url": "https://example.invalid"}},
    "targets": {
        "t-free": {
            "id": "vendor/model:free",
            "llm_client": "openrouter",
            "vision": True,
            "tool_calling": True,
        },
        "t-text": {"id": "vendor/text", "llm_client": "openrouter", "vision": False},
    },
    "routes": {
        "smartfree": {
            "id": "smartfree",
            "type": "passthrough",
            "target": "t-free",
            "candidates": ["t-text"],
            "context_window": 262144,
            "route_reasoning_policy": "inherit",
            "api_key_env": "MUST_NOT_APPEAR",
        },
        "smartfree-bounded": {
            "type": "passthrough",
            "target": "t-free",
            "candidates": [],
            "route_reasoning_policy": "none",
        },
    },
    "composites": {
        "tier1": {"children": ["smartfree", "smartfree-bounded"], "context_window": 1000000}
    },
    "capabilities": {"switchyard-embedding-local": {"dimensions": 1024}},
    "decision_lanes": {"smartfree-decision": {}},
}


def test_catalog_projects_structural_fields_only() -> None:
    out = sy.build_catalog(_DOC)
    assert out["schema_version"] == 1
    assert set(out["routes"]) == {"smartfree", "smartfree-bounded"}
    assert out["routes"]["smartfree"]["target"] == "t-free"
    assert out["routes"]["smartfree"]["candidates"] == ["t-text"]
    assert "api_key_env" not in out["routes"]["smartfree"]
    assert "id" not in out["routes"]["smartfree"]  # not part of the projection


def test_catalog_reports_composition_references() -> None:
    out = sy.build_catalog(_DOC)
    assert out["routes"]["smartfree"]["referenced_by"] == ["tier1"]
    assert out["routes"]["smartfree-bounded"]["referenced_by"] == ["tier1"]


def test_catalog_counts_and_lists() -> None:
    out = sy.build_catalog(_DOC)
    assert out["counts"] == {"routes": 2, "composites": 1, "targets": 2, "llm_clients": 1}
    assert out["capabilities"] == ["switchyard-embedding-local"]
    assert out["decision_lanes"] == ["smartfree-decision"]


def test_catalog_survives_empty_document() -> None:
    out = sy.build_catalog({})
    assert out["counts"]["routes"] == 0
    assert out["routes"] == {}


def test_inspect_route_resolves_target_and_candidates() -> None:
    out = sy.inspect_route(_DOC, "smartfree")
    assert out["kind"] == "route"
    assert out["target"]["definition"]["vision"] is True
    assert out["candidates"][0]["definition"]["vision"] is False
    assert out["referenced_by"] == ["tier1"]


def test_inspect_route_expands_composite_children() -> None:
    out = sy.inspect_route(_DOC, "tier1")
    assert out["kind"] == "composite"
    assert [child["name"] for child in out["children"]] == ["smartfree", "smartfree-bounded"]
    assert out["children"][0]["target"]["definition"]["id"] == "vendor/model:free"


def test_inspect_route_refuses_unknown_name() -> None:
    assert _raises(sy.inspect_route, _DOC, "nope")


# --------------------------------------------------------------------------
# metrics
# --------------------------------------------------------------------------

_METRICS = """\
# HELP switchyard_requests_total Requests served.
# TYPE switchyard_requests_total counter
switchyard_requests_total{model="a",outcome="ok"} 12
switchyard_requests_total{model="b",outcome="ok"} 3
# TYPE switchyard_llm_call_duration_ms histogram
switchyard_llm_call_duration_ms_bucket{le="10"} 1
switchyard_llm_call_duration_ms_bucket{le="+Inf"} 2
switchyard_llm_call_duration_ms_sum 20.5
switchyard_llm_call_duration_ms_count 2
# TYPE switchyard_algorithms_in_flight gauge
switchyard_algorithms_in_flight 0
# TYPE target_info gauge
target_info{job="switchyard-v7"} 1
not_a_declared_family{bad="1"} 9
"""


def test_metrics_parses_declared_families() -> None:
    out = sy.parse_metrics(_METRICS)
    families = out["families"]
    assert families["switchyard_requests_total"]["type"] == "counter"
    assert families["switchyard_requests_total"]["samples"] == 2
    assert families["switchyard_requests_total"]["help"] == "Requests served."
    assert families["switchyard_llm_call_duration_ms"]["type"] == "histogram"


def test_metrics_maps_histogram_suffixes_to_family() -> None:
    families = sy.parse_metrics(_METRICS)["families"]
    assert families["switchyard_llm_call_duration_ms"]["samples"] == 4


def test_metrics_records_unlabelled_value() -> None:
    families = sy.parse_metrics(_METRICS)["families"]
    assert families["switchyard_algorithms_in_flight"]["value"] == 0.0


def test_metrics_ignores_undeclared_series() -> None:
    assert "not_a_declared_family" not in sy.parse_metrics(_METRICS)["families"]


def test_metrics_family_count_matches() -> None:
    out = sy.parse_metrics(_METRICS)
    assert out["family_count"] == len(out["families"]) == 4


# --------------------------------------------------------------------------
# stats
# --------------------------------------------------------------------------

_STATS = {
    "total_requests": 7,
    "total_errors": 1,
    "total_tokens": {"total": 900},
    "models": {
        "a": {
            "calls": 5,
            "errors": 0,
            "total_tokens": 800,
            "reasoning_tokens": 0,
            "model_call_latency": {"avg_ms": 10.0, "p99_ms": 20.0},
        },
        "b": {"calls": 9, "errors": 1, "total_tokens": 100, "reasoning_tokens": 12},
        "c": "not-an-object",
    },
}


def test_stats_sorts_by_calls_and_skips_malformed() -> None:
    out = sy.summarize_stats(_STATS)
    assert [row["model"] for row in out["models"]] == ["b", "a"]
    assert out["models"][0]["p99_ms"] is None
    assert out["models"][1]["p99_ms"] == 20.0


def test_stats_keeps_totals_and_model_count() -> None:
    out = sy.summarize_stats(_STATS)
    assert out["total_requests"] == 7
    assert out["total_tokens"] == {"total": 900}
    assert out["model_count"] == 3


def test_stats_refuses_non_object() -> None:
    assert _raises_as(sy.RemoteError, sy.summarize_stats, [])


# --------------------------------------------------------------------------
# routing log
# --------------------------------------------------------------------------

_LOG = [
    json.dumps(
        {"ts": "t1", "route_id": "smartfree", "model": "m1", "reasoning_tokens": 3, "secret": "x"}
    ),
    "not json",
    json.dumps({"ts": "t2", "route_id": "smart", "model": "m2"}),
    json.dumps({"ts": "t3", "route_id": "smartfree", "model": "m1", "child_route": "smartfree"}),
    "",
]


def test_log_filters_by_route_and_keeps_order() -> None:
    out = sy.filter_log(_LOG, "smartfree", 10)
    assert [row["ts"] for row in out] == ["t1", "t3"]
    assert all(row["route_id"] == "smartfree" for row in out)


def test_log_limit_applies_to_newest_records() -> None:
    out = sy.filter_log(_LOG, None, 2)
    assert [row["ts"] for row in out] == ["t2", "t3"]


def test_log_projects_whitelisted_fields_only() -> None:
    out = sy.filter_log(_LOG, "smartfree", 10)
    assert "secret" not in out[0]


def test_log_skips_malformed_lines() -> None:
    # "{}" is a well-formed JSON object, so it survives as an empty projection;
    # only text that is not a JSON object is dropped.
    assert sy.filter_log(["", "no", "[]", "null"], None, 10) == []
    assert sy.filter_log(["{}"], None, 10) == [{}]


# --------------------------------------------------------------------------
# helpers and surface
# --------------------------------------------------------------------------


def test_tail_bounds_and_drops_blanks() -> None:
    text = "\n".join(f"line{i}" for i in range(100)) + "\n\n"
    out = sy._tail(text, 3)
    assert out == ["line97", "line98", "line99"]


def test_verb_table_is_closed() -> None:
    assert set(sy.VERBS) == {"status", "catalog", "stats", "metrics", "validate", "route", "log"}


def test_only_input_verbs_read_stdin() -> None:
    assert {"route", "log"} == sy.STDIN_VERBS


def test_remote_constants_contain_no_caller_slots() -> None:
    # A status run must not interpolate anything but the module constants.
    for value in (sy.UNIT, sy.BINARY, sy.CONFIG_PATH, sy.BASE_URL):
        assert value in sy.STATUS_SCRIPT
    assert "{" not in sy.STATUS_SCRIPT


def test_shquote_handles_embedded_quotes() -> None:
    assert sy._shquote("plain") == "'plain'"
    assert sy._shquote("a'b") == "'a'\\''b'"


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
