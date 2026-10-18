# SPDX-License-Identifier: Apache-2.0
"""Inventory generation and dashboard validation.

The generator is the only place the static panel text is produced. These tests
check two different things:

* that the generator derives the right facts from a catalog (so the generated
  text is not merely self-consistent), and
* that the validator rejects a dashboard whose static text has drifted (so a
  green run means agreement with the manifest, not agreement with itself).

Every negative test mutates *generated* output, which is what makes it a
regression test rather than a restatement of the implementation.

Run standalone::

    TMPDIR=/var/tmp /opt/turnstone-venv/bin/python tests/test_switchyard_dashboard_inventory.py
"""

from __future__ import annotations

import copy
import importlib.util
import pathlib
import re
import sys
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Callable

_SCRIPTS = pathlib.Path(__file__).resolve().parents[1] / "scripts"


def _load(name: str, filename: str):
    spec = importlib.util.spec_from_file_location(name, _SCRIPTS / filename)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


dash = _load("switchyard_dashboard", "switchyard_dashboard.py")
val = _load("validate_switchyard_dashboard", "validate_switchyard_dashboard.py")


# --------------------------------------------------------------------------
# fixtures
# --------------------------------------------------------------------------

CATALOG: dict[str, Any] = {
    "schema_version": 1,
    "llm_clients": {
        "openrouter": {"format": "openai_responses", "base_url": "https://openrouter.example/api/v1"},
        "openai": {"format": "openai_responses", "base_url": "https://openai.example/v1"},
    },
    "targets": {
        "or-free": {"id": "vendor/free-vl:free", "llm_client": "openrouter", "vision": True,
                    "tool_calling": True, "reasoning_policy": "enabled"},
        "or-text": {"id": "vendor/text:free", "llm_client": "openrouter", "vision": False,
                    "tool_calling": True, "reasoning_policy": "enabled"},
        "oa-a": {"id": "gpt-x", "llm_client": "openai", "vision": True, "tool_calling": True},
    },
    "routes": {
        "direct-openrouter-free": {
            "type": "passthrough", "target": "or-free", "candidates": [],
            "context_window": 262144, "tool_calling": True, "vision": True,
        },
        "direct-openai-a": {
            "type": "passthrough", "target": "oa-a", "candidates": [],
            "context_window": 1000000, "tool_calling": True, "vision": True,
        },
        "smartfree": {
            "type": "passthrough", "target": "or-free", "candidates": ["or-text"],
            "context_window": 262144, "tool_calling": True, "vision": True,
            "route_reasoning_policy": "inherit",
        },
        "smartfree-bounded": {
            "type": "passthrough", "target": "or-free", "candidates": ["or-text"],
            "context_window": 262144, "tool_calling": True, "vision": True,
            "route_reasoning_policy": "none",
        },
    },
    "composites": {
        "tier1": {"children": ["smartfree", "direct-openai-a"], "context_window": 1000000},
        "tier1-bounded": {"children": ["smartfree-bounded", "direct-openai-a"],
                          "context_window": 1000000},
    },
    "capability_defs": {
        "switchyard-rerank-local": {
            "id": "switchyard-rerank-local", "target": "reranker-local",
            "max_candidates": 64, "top_n": 64, "max_doc_chars": 1024, "max_query_chars": 512,
        }
    },
    "capabilities": ["switchyard-rerank-local"],
    "decision_lane_defs": {"smartfree-decision": {"targets": ["direct-openrouter-free"]}},
    "decision_lanes": ["smartfree-decision"],
    "decision_lane_composition": {"tier1-decision": {"children": ["smartfree-decision"]}},
}

MODELS = {
    "data": [
        {"id": "direct-openrouter-free"},
        {"id": "direct-openai-a"},
        {"id": "smartfree"},
        {"id": "smartfree-bounded"},
    ]
}

STATUS = {
    "unit": "switchyard-test.service",
    "version": "switchyard-server 0.0.0",
    "main_pid": "123",
    "active_since": "now",
    "config_sha256": "a" * 64,
}


def build() -> dict[str, Any]:
    return dash.build_inventory(
        copy.deepcopy(CATALOG),
        copy.deepcopy(MODELS),
        copy.deepcopy(STATUS),
        collected_at="2026-01-01T00:00:00+00:00",
    )


def dashboard_for(inventory: dict[str, Any], egress: dict[str, Any] | None = None) -> dict[str, Any]:
    egress = egress or {p["name"]: {"status": "200", "at": "t"} for p in inventory["providers"]}
    content = dash.panel_content(inventory, egress)
    panels = [
        {"id": pid, "type": "text", "title": title,
         "content": content[pid], "options": {"mode": "markdown", "content": content[pid]}}
        for pid, title in dash.STATIC_PANEL_IDS.items()
    ]
    panels.append(
        {
            "id": 9006,
            "type": "timeseries",
            "title": "REQUESTS BY PROVIDER",
            "targets": [
                {"expr": expr, "legendFormat": label}
                for label, expr in dash.provider_queries(inventory)
            ],
        }
    )
    return {"title": "TEST", "panels": panels}


def violations(inventory: dict[str, Any], document: dict[str, Any]) -> list[str]:
    return val.check_dashboard(
        copy.deepcopy(CATALOG), copy.deepcopy(MODELS), inventory, document
    )


def _panel(document: dict[str, Any], panel_id: int) -> dict[str, Any]:
    return next(p for p in document["panels"] if p["id"] == panel_id)


def _set_both(panel: dict[str, Any], text: str) -> None:
    panel["content"] = text
    panel["options"]["content"] = text


# --------------------------------------------------------------------------
# generation
# --------------------------------------------------------------------------


def test_numbering_is_sequential_direct_then_primitive() -> None:
    inv = build()
    numbers = [r["n"] for r in inv["routes"]]
    kinds = [r["kind"] for r in inv["routes"]]
    assert numbers == [1, 2, 3, 4]
    assert kinds == ["direct", "direct", "primitive", "primitive"]


def test_counts_match_routes() -> None:
    inv = build()
    assert inv["counts"] == {
        "routes": 4, "direct": 2, "primitives": 2,
        "composites": 2, "targets": 3, "llm_clients": 2,
    }


def test_primary_and_candidates_come_from_targets() -> None:
    inv = build()
    smartfree = next(r for r in inv["routes"] if r["name"] == "smartfree")
    assert smartfree["primary_id"] == "vendor/free-vl:free"
    assert [c["id"] for c in smartfree["candidates"]] == ["vendor/text:free"]
    assert smartfree["vision"] is True


def test_route_policy_scope_is_explicit() -> None:
    inv = build()
    bounded = next(r for r in inv["routes"] if r["name"] == "smartfree-bounded")
    assert bounded["reasoning_policy"] == "none"
    assert bounded["reasoning_scope"] == "route"
    direct = next(r for r in inv["routes"] if r["name"] == "direct-openai-a")
    assert direct["reasoning_policy"] is None
    assert direct["reasoning_scope"] == "target"


def test_provider_route_counts_sum_to_total() -> None:
    inv = build()
    assert sum(p["route_count"] for p in inv["providers"]) == inv["counts"]["routes"]


def test_openai_shared_id_is_merged_not_split() -> None:
    catalog = copy.deepcopy(CATALOG)
    catalog["llm_clients"]["openai_nt"] = {"format": "openai_responses",
                                           "base_url": "https://openai.example/v1"}
    catalog["targets"]["oa-a-nt"] = {"id": "gpt-x", "llm_client": "openai_nt",
                                     "vision": True, "tool_calling": True}
    catalog["routes"]["direct-openai-a-nt"] = {
        "type": "passthrough", "target": "oa-a-nt", "candidates": [],
        "context_window": 1000000, "tool_calling": True, "vision": True,
    }
    inv = dash.build_inventory(catalog, {"data": []}, STATUS, collected_at="t")
    labels = [label for label, _ in dash.provider_queries(inv)]
    assert any("openai/openai_nt" in label for label in labels)
    assert not any(label == "openai_nt" for label in labels)


def test_provider_queries_select_live_ids_only() -> None:
    inv = build()
    joined = " ".join(expr for _, expr in dash.provider_queries(inv))
    assert "vendor/free-vl:free" in joined
    assert "vendor/text:free" in joined
    assert "gpt-x" in joined


# --------------------------------------------------------------------------
# provider attribution (D1/D2): a candidate belongs to ITS provider
# --------------------------------------------------------------------------

# Inventory shaped like the live ``smart-bounded`` route: an openai primary
# with an openrouter candidate. The route's provider and the candidate's
# provider differ, which is the case the old grouping got wrong.
CROSS_PROVIDER_INVENTORY: dict[str, Any] = {
    "routes": [
        {
            "name": "smart-bounded",
            "provider": "openai",
            "primary_target": "oa-a",
            "primary_id": "gpt-x",
            "candidates": [
                {"target": "or-text", "id": "vendor/text:free",
                 "provider": "openrouter", "vision": False},
            ],
        },
        {
            "name": "direct-openrouter-free",
            "provider": "openrouter",
            "primary_target": "or-free",
            "primary_id": "vendor/free-vl:free",
            "candidates": [],
        },
    ],
    "composites": [],
    "providers": [],
    "counts": {"routes": 2},
}


def _live_ids(inventory: dict[str, Any]) -> set[str]:
    ids: set[str] = set()
    for record in inventory["routes"]:
        if record.get("primary_id"):
            ids.add(record["primary_id"])
        for candidate in record["candidates"]:
            if candidate.get("id"):
                ids.add(candidate["id"])
    return ids


def _selected(inventory: dict[str, Any], label: str) -> set[str]:
    """Live ids a named series' selector matches."""
    expr = dict(dash.provider_queries(inventory))[label]
    match = re.search(r'model=~"([^"]+)"', expr)
    assert match, expr
    compiled = re.compile(match.group(1))
    return {i for i in _live_ids(inventory) if compiled.fullmatch(i)}


def test_candidate_is_attributed_to_its_own_provider() -> None:
    """A fallback on another client must not be credited to the primary's."""
    assert _selected(CROSS_PROVIDER_INVENTORY, "openrouter") == {
        "vendor/text:free",
        "vendor/free-vl:free",
    }
    assert _selected(CROSS_PROVIDER_INVENTORY, "openai") == {"gpt-x"}


def test_provider_selectors_are_pairwise_disjoint() -> None:
    queries = dash.provider_queries(CROSS_PROVIDER_INVENTORY)
    seen: dict[str, str] = {}
    for label, _ in queries:
        for model_id in _selected(CROSS_PROVIDER_INVENTORY, label):
            assert model_id not in seen, f"{model_id} counted by {seen[model_id]} and {label}"
            seen[model_id] = label
    assert len(seen) == 3


def test_apply_regenerates_the_provider_panel() -> None:
    """The panel must follow the manifest, not the last author's typing."""
    inv = build()
    document = dashboard_for(inv)
    provider = next(p for p in document["panels"] if p["title"] == "REQUESTS BY PROVIDER")
    provider["targets"] = [{"expr": 'model=~"retired/old-id"', "legendFormat": "stale"}]
    provider["description"] = "operator wording"
    provider["datasource"] = {"uid": "prom", "type": "prometheus"}

    dash.apply_to_dashboard(document, inv, title="T")

    provider = next(p for p in document["panels"] if p["title"] == "REQUESTS BY PROVIDER")
    assert [(t["legendFormat"], t["expr"]) for t in provider["targets"]] == [
        (label, expr) for label, expr in dash.provider_queries(inv)
    ]
    assert "retired/old-id" not in str(provider["targets"])
    # Datasource and operator prose are preserved: this is a data fix.
    assert provider["datasource"] == {"uid": "prom", "type": "prometheus"}
    assert provider["description"] == "operator wording"
    assert [t["refId"] for t in provider["targets"]] == [
        chr(65 + i) for i in range(len(provider["targets"]))
    ]


def test_apply_requires_the_provider_panel() -> None:
    inv = build()
    document = dashboard_for(inv)
    document["panels"] = [
        p for p in document["panels"] if p["title"] != "REQUESTS BY PROVIDER"
    ]
    try:
        dash.apply_to_dashboard(document, inv, title="T")
    except SystemExit as exc:
        assert "REQUESTS BY PROVIDER" in str(exc)
    else:  # pragma: no cover - the assertion below is the point
        raise AssertionError("a missing provider panel must fail loudly")


def test_overlapping_provider_selector_is_rejected() -> None:
    """Two series matching one id is the double-count the panel shipped with."""
    inv = CROSS_PROVIDER_INVENTORY
    graph = copy.deepcopy(provider_panel_doc(inv))
    provider = next(p for p in graph["panels"] if p["title"] == "REQUESTS BY PROVIDER")
    provider["targets"].append(
        {
            "expr": 'sum(rate(switchyard_requests_total{job="switchyard-v7", '
                    'model=~"vendor/text:free"}[5m]))',
            "legendFormat": "openai",
            "refId": "Z",
        }
    )
    found: list[str] = []
    val.check_provider_selectors_disjoint(inv, graph, found)
    assert any("counted by 2 provider series" in item for item in found), found


def test_corrected_selectors_pass_the_disjoint_check() -> None:
    inv = CROSS_PROVIDER_INVENTORY
    graph = copy.deepcopy(provider_panel_doc(inv))
    found: list[str] = []
    val.check_provider_selectors_disjoint(inv, graph, found)
    assert found == [], found


def provider_panel_doc(inventory: dict[str, Any]) -> dict[str, Any]:
    return {
        "title": "T",
        "panels": [
            {
                "id": 9006,
                "type": "timeseries",
                "title": "REQUESTS BY PROVIDER",
                "targets": [
                    {"expr": expr, "legendFormat": label, "refId": chr(65 + i)}
                    for i, (label, expr) in enumerate(dash.provider_queries(inventory))
                ],
            }
        ],
    }


# --------------------------------------------------------------------------
# happy path
# --------------------------------------------------------------------------


def test_generated_dashboard_passes_validation() -> None:
    inv = build()
    assert violations(inv, dashboard_for(inv)) == []


def test_every_static_panel_writes_both_content_fields() -> None:
    inv = build()
    document = dashboard_for(inv)
    for panel in document["panels"]:
        if panel["id"] in dash.STATIC_PANEL_IDS:
            assert panel["content"] == panel["options"]["content"]


# --------------------------------------------------------------------------
# negative tests: each must be detected
# --------------------------------------------------------------------------


def test_stale_apodex_row_is_rejected() -> None:
    """A retired model reappearing as an active row must fail publication."""
    inv = build()
    document = dashboard_for(inv)
    panel = _panel(document, 9007)
    text = panel["content"].replace(
        "| 2 | `direct-openai-a` | direct | openai |",
        "| 2 | `direct-openrouter-apodex-1.1-mini:free` | direct | openrouter |",
    ).replace("`gpt-x`", "`apodex/apodex-1.1-mini:free`")
    _set_both(panel, text)
    found = violations(inv, document)
    assert any("apodex" in item for item in found), found


def test_missing_smartfree_bounded_is_rejected() -> None:
    """Dropping a primitive must fail, not pass silently."""
    inv = build()
    document = dashboard_for(inv)
    panel = _panel(document, 9007)
    lines = [ln for ln in panel["content"].splitlines() if "`smartfree-bounded`" not in ln]
    _set_both(panel, "\n".join(lines))
    found = violations(inv, document)
    assert any("smartfree-bounded" in item for item in found), found


def test_duplicate_row_number_is_rejected() -> None:
    inv = build()
    document = dashboard_for(inv)
    panel = _panel(document, 9007)
    text = panel["content"].replace("| 3 | `smartfree`", "| 2 | `smartfree`", 1)
    _set_both(panel, text)
    found = violations(inv, document)
    assert any("repeats row number" in item or "not sequential" in item for item in found), found


def test_missing_row_number_is_rejected() -> None:
    inv = build()
    document = dashboard_for(inv)
    panel = _panel(document, 9007)
    text = panel["content"].replace("| 3 | `smartfree`", "|  | `smartfree`", 1)
    _set_both(panel, text)
    found = violations(inv, document)
    assert any("non-numeric" in item for item in found), found


def test_wrong_tier_children_are_rejected() -> None:
    inv = build()
    document = dashboard_for(inv)
    panel = _panel(document, 9008)
    text = panel["content"].replace(
        "| 2 | `tier1-bounded` | `smartfree-bounded` |",
        "| 2 | `tier1-bounded` | `smartfree` |",
    )
    _set_both(panel, text)
    found = violations(inv, document)
    assert any("tier1-bounded" in item and "children" in item for item in found), found


def test_stale_context_window_is_rejected() -> None:
    inv = build()
    document = dashboard_for(inv)
    panel = _panel(document, 9007)
    text = panel["content"].replace("| 262,144 |", "| 200,000 |", 1)
    _set_both(panel, text)
    found = violations(inv, document)
    assert any("Context" in item for item in found), found


def test_summary_count_mismatch_is_rejected() -> None:
    inv = build()
    document = dashboard_for(inv)
    panel = _panel(document, 9002)
    _set_both(panel, panel["content"].replace("**Routes served:** 4", "**Routes served:** 5"))
    found = violations(inv, document)
    assert any("CATALOG SUMMARY says 5" in item for item in found), found


def _edit_provider_row(text: str, provider: str, edit: Callable[[list[str]], None]) -> str:
    lines = []
    for line in text.splitlines():
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if len(cells) == 7 and cells[1].strip("*") == provider:
            edit(cells)
            line = "| " + " | ".join(cells) + " |"
        lines.append(line)
    return "\n".join(lines)


def test_provider_upstream_ids_must_be_in_their_own_row() -> None:
    """An id that drifted into a neighbouring row must not satisfy a search."""
    inv = build()
    document = dashboard_for(inv)
    panel = _panel(document, 9005)
    text = _edit_provider_row(panel["content"], "openai", lambda cells: cells.__setitem__(5, "—"))
    text = _edit_provider_row(
        text, "openrouter", lambda cells: cells.__setitem__(5, cells[5] + ", `gpt-x`")
    )
    assert "`gpt-x`" in text, "the id must still be present in the panel"
    _set_both(panel, text)
    found = violations(inv, document)
    assert any("openai" in item and "upstream ids" in item for item in found), found


def test_provider_route_count_mismatch_is_rejected() -> None:
    inv = build()
    document = dashboard_for(inv)
    panel = _panel(document, 9005)
    lines = []
    for line in panel["content"].splitlines():
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if len(cells) == 7 and "openrouter" in cells[1]:
            cells[4] = "9"  # the Routes column
            line = "| " + " | ".join(cells) + " |"
        lines.append(line)
    _set_both(panel, "\n".join(lines))
    found = violations(inv, document)
    assert any("openrouter" in item and "routes" in item for item in found), found


def test_footer_count_mismatch_is_rejected() -> None:
    inv = build()
    document = dashboard_for(inv)
    panel = _panel(document, 9007)
    _set_both(panel, panel["content"].replace("· 4 total.", "· 3 total."))
    found = violations(inv, document)
    assert any("footer says 3 total" in item for item in found), found


def test_footer_row_range_mismatch_is_rejected() -> None:
    inv = build()
    document = dashboard_for(inv)
    panel = _panel(document, 9007)
    _set_both(panel, panel["content"].replace("(rows 1–2)", "(rows 1–1)"))
    found = violations(inv, document)
    assert any("footer direct rows" in item for item in found), found


def test_missing_footer_is_rejected() -> None:
    inv = build()
    document = dashboard_for(inv)
    panel = _panel(document, 9007)
    kept = [ln for ln in panel["content"].splitlines() if not ln.lstrip().startswith("<sub>")]
    _set_both(panel, "\n".join(kept))
    found = violations(inv, document)
    assert any("no <sub> footer" in item for item in found), found


def test_duplicate_footer_is_rejected() -> None:
    inv = build()
    document = dashboard_for(inv)
    panel = _panel(document, 9007)
    footer = next(ln for ln in panel["content"].splitlines() if ln.lstrip().startswith("<sub>"))
    _set_both(panel, footer + "\n" + panel["content"])
    found = violations(inv, document)
    assert any("2 footers" in item for item in found), found


def test_zero_category_range_is_checked() -> None:
    """A zero-count category still has to name the empty range it generated."""
    header = ["Type"]
    rows = [["direct"]]
    good = "<sub>1 direct (rows 1–1) · 0 primitive (rows 2–1) · 1 total.</sub>"
    bad = "<sub>1 direct (rows 1–1) · 0 primitive (rows 2–9) · 1 total.</sub>"
    found: list[str] = []
    val.check_lineup_footer(rows, header, good, found)
    assert found == [], found
    val.check_lineup_footer(rows, header, bad, found)
    assert any("primitive rows 2–9" in item for item in found), found


def test_ragged_table_row_is_rejected() -> None:
    """A short row reads as 'not declared' and can pass an empty-field compare."""
    inv = build()
    document = dashboard_for(inv)
    panel = _panel(document, 9007)
    _set_both(
        panel,
        panel["content"].replace(
            "| 1 | `direct-openai-a` | direct | openai | `gpt-x` | 1,000,000 | y | y | — | — |",
            "| 1 | `direct-openai-a` | direct | openai | `gpt-x` | 1,000,000 | y | y | — |",
        ),
    )
    found = violations(inv, document)
    assert any("cell(s), header has" in item for item in found), found


def test_missing_table_separator_is_rejected() -> None:
    """Without a separator rule the panel renders as prose, not as a table."""
    inv = build()
    document = dashboard_for(inv)
    panel = _panel(document, 9007)
    _set_both(panel, panel["content"].replace("|---|---|---|---|---|---|---|---|---|---|\n", "", 1))
    found = violations(inv, document)
    assert any("separator rule" in item for item in found), found


def test_malformed_secondary_table_is_rejected() -> None:
    """9008 holds three tables; the one parse_table never reads must still render."""
    inv = build()
    document = dashboard_for(inv)
    panel = _panel(document, 9008)
    text = panel["content"].replace(
        "| # | id | target | contract / limits |\n|---|---|---|---|\n",
        "| # | id | target | contract / limits |\n",
    )
    assert text != panel["content"]
    _set_both(panel, text)
    found = violations(inv, document)
    assert any("separator rule" in item for item in found), found


def test_selector_missing_a_live_model_is_rejected() -> None:
    inv = build()
    document = dashboard_for(inv)
    target = _panel(document, 9006)["targets"][0]
    target["expr"] = 'sum(rate(switchyard_requests_total{job="switchyard-v7", model=~"gpt-x"}[5m]))'
    found = violations(inv, document)
    assert any("do not cover live model id" in item for item in found), found


def test_retired_model_in_selector_is_rejected() -> None:
    inv = build()
    document = dashboard_for(inv)
    _panel(document, 9006)["targets"].append(
        {"expr": 'sum(rate(switchyard_requests_total{job="switchyard-v7", '
                 'model=~"apodex/apodex-1.1-mini:free"}[5m]))'}
    )
    found = violations(inv, document)
    assert any("retired id" in item for item in found), found


def test_one_content_field_drift_is_rejected() -> None:
    """The exact V7.2 failure mode: writing only the legacy field."""
    inv = build()
    document = dashboard_for(inv)
    panel = _panel(document, 9002)
    panel["content"] = "stale text that never got regenerated"
    found = violations(inv, document)
    assert any("content fields disagree" in item for item in found), found


def test_text_panel_missing_a_content_field_is_rejected() -> None:
    """A legacy-only panel can render blank; that must not pass."""
    inv = build()
    document = dashboard_for(inv)
    _panel(document, 9002).pop("content")
    found = violations(inv, document)
    assert any("missing content in" in item for item in found), found


def test_normalize_fills_both_content_fields() -> None:
    document = {
        "panels": [
            {"id": 1, "type": "text", "title": "A", "options": {"content": "text"}},
            {"id": 2, "type": "text", "title": "B", "content": "text",
             "options": {}},
            {"id": 3, "type": "text", "title": "C", "content": "new",
             "options": {"content": "old"}},
        ]
    }
    dash.normalize_text_panels(document)
    for panel in document["panels"]:
        assert panel["content"] == panel["options"]["content"]
    assert document["panels"][2]["content"] == "new"


def test_models_endpoint_disagreement_is_rejected() -> None:
    inv = build()
    broken = copy.deepcopy(MODELS)
    broken["data"] = broken["data"][:-1]
    found = val.check_dashboard(CATALOG, broken, inv, dashboard_for(inv))
    assert any("/v1/models disagrees" in item for item in found), found


def test_unlabelled_reachability_is_rejected() -> None:
    inv = build()
    document = dashboard_for(inv)
    panel = _panel(document, 9005)
    _set_both(panel, panel["content"].replace("Unauthenticated reachability", "Reachability"))
    found = violations(inv, document)
    assert any("unauthenticated" in item.lower() for item in found), found


def test_historical_line_is_permitted_only_when_marked() -> None:
    inv = build()
    document = dashboard_for(inv)
    panel = _panel(document, 9007)
    _set_both(
        panel,
        panel["content"]
        + "\n<sub>Historical: `apodex/apodex-1.1-mini:free` retired 2026-10-17.</sub>",
    )
    assert violations(inv, document) == [], violations(inv, document)


# --------------------------------------------------------------------------
# runner
# --------------------------------------------------------------------------


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
