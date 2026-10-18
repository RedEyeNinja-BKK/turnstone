#!/usr/bin/env python3
"""Generate the Switchyard dashboard's static inventory from the live manifest.

WHY THIS EXISTS
  Every static panel on SWITCHYARD DASHBOARD V7 was authored by hand from a
  manifest snapshot and then inherited, unchanged, by later revisions. When the
  manifest changed (apodex/ling replaced by nemotron/xing, `smartfree-bounded`
  added, bounded tiers rewired) the panels kept describing the old deployment
  for two revisions. Nothing failed, because nothing compared the two.

  This module is the single source of truth for that text. Everything is
  derived from one catalog snapshot; no panel carries a hand-maintained copy of
  the route list.

TWO CONTENT FIELDS
  Grafana keeps a text panel's markdown in two places: the panel's top-level
  ``content`` (current schema) and ``options.content`` (legacy). They can drift
  apart, and a write to only one of them appears to succeed while the rendered
  panel keeps the old text. :func:`apply_to_dashboard` writes both, and the
  validator compares both.

USAGE
  # build the canonical inventory from live data
  python switchyard_dashboard.py inventory \\
      --catalog catalog.json --models models.json --status status.json \\
      --egress egress.json --out inventory.json

  # rewrite the static panels of an existing dashboard document
  python switchyard_dashboard.py apply \\
      --dashboard dashboard.json --inventory inventory.json --title "..." --out next.json
"""

from __future__ import annotations

import argparse
import json
import sys
from typing import Any

# Panels whose content is derived from the manifest. The mapping is by title as
# well as id, so a renumbered dashboard fails loudly instead of silently
# skipping a panel.
STATIC_PANEL_IDS: dict[int, str] = {
    9002: "CATALOG SUMMARY",
    9005: "PROVIDER ACCESS",
    9007: "ROUTE LINEUP",
    9008: "TIERS · DECISION LANES · CAPABILITIES",
    9201: "FREE LADDER — smartfree / smartfree-bounded",
}

INVENTORY_SCHEMA = "switchyard-inventory/v1"


# --------------------------------------------------------------------------
# inventory
# --------------------------------------------------------------------------


def _route_kind(name: str) -> str:
    """Direct routes are named ``direct-*``; everything else is a primitive."""
    return "direct" if name.startswith("direct-") else "primitive"


def _first(route: dict[str, Any], target: dict[str, Any], key: str) -> Any:
    """Route declaration wins; fall back to the target's own declaration."""
    if route.get(key) is not None:
        return route[key]
    return target.get(key)


def build_inventory(
    catalog: dict[str, Any],
    models: dict[str, Any],
    status: dict[str, Any],
    *,
    collected_at: str,
    manifest_path: str = "/etc/switchyard/routes.toml",
) -> dict[str, Any]:
    routes_in = catalog.get("routes") or {}
    targets = catalog.get("targets") or {}
    composites = catalog.get("composites") or {}
    clients = catalog.get("llm_clients") or {}

    direct, primitives = [], []
    for name, route in sorted(routes_in.items()):
        target_name = route.get("target") or ""
        target = targets.get(target_name) or {}
        provider = target.get("llm_client") or ""
        candidates = []
        for candidate in route.get("candidates") or []:
            candidate_target = targets.get(candidate) or {}
            candidates.append(
                {
                    "target": candidate,
                    "id": candidate_target.get("id") or "",
                    "provider": candidate_target.get("llm_client") or "",
                    "vision": candidate_target.get("vision"),
                }
            )
        record = {
            "name": name,
            "kind": _route_kind(name),
            "provider": provider,
            "primary_target": target_name,
            "primary_id": target.get("id") or "",
            "candidates": candidates,
            "context_window": route.get("context_window"),
            "tool_calling": _first(route, target, "tool_calling"),
            "vision": _first(route, target, "vision"),
            # Route-level policy is authoritative and applies last. When the
            # route declares none, the target's own policy is the default that
            # only takes effect under `inherit`; keep the scope explicit so a
            # reader cannot mistake a target default for an enforced policy.
            "reasoning_policy": route.get("route_reasoning_policy"),
            "reasoning_scope": "route" if route.get("route_reasoning_policy") else "target",
            "target_reasoning_policy": target.get("reasoning_policy"),
            "escalation_target": route.get("escalation_target"),
        }
        (direct if record["kind"] == "direct" else primitives).append(record)

    ordered = direct + primitives
    for index, record in enumerate(ordered, start=1):
        record["n"] = index

    # Provider accounting: a route counts once, against its PRIMARY target's
    # client. Candidates are shown on the row but do not create a second route.
    providers: dict[str, dict[str, Any]] = {}
    for record in ordered:
        entry = providers.setdefault(
            record["provider"],
            {"name": record["provider"], "routes": [], "primary_ids": set(), "candidate_ids": set()},
        )
        entry["routes"].append(record["name"])
        entry["primary_ids"].add(record["primary_id"])
        for candidate in record["candidates"]:
            # Only a candidate this provider can actually serve belongs here.
            # A route's fallback often lives on another provider (deepseek ->
            # openrouter), and counting it would attribute that provider's
            # traffic to the wrong client.
            if candidate.get("provider") == record["provider"]:
                entry["candidate_ids"].add(candidate["id"])

    provider_rows = []
    for index, (name, entry) in enumerate(sorted(providers.items()), start=1):
        client = clients.get(name) or {}
        provider_rows.append(
            {
                "n": index,
                "name": name,
                "format": client.get("format") or "",
                "base_url": client.get("base_url") or "",
                "route_count": len(entry["routes"]),
                "primary_ids": sorted(i for i in entry["primary_ids"] if i),
                "candidate_only_ids": sorted(entry["candidate_ids"] - entry["primary_ids"]),
                "routes": entry["routes"],
            }
        )

    model_ids = sorted(item.get("id", "") for item in (models.get("data") or []))
    return {
        "schema": INVENTORY_SCHEMA,
        "collected_at": collected_at,
        "manifest": {
            "path": manifest_path,
            "sha256": status.get("config_sha256") or "",
            "schema_version": catalog.get("schema_version"),
        },
        "runtime": {
            "unit": status.get("unit") or "",
            "binary_version": status.get("version") or "",
            "main_pid": status.get("main_pid") or "",
            "active_since": status.get("active_since") or "",
            "models_endpoint_count": len(model_ids),
            "models_endpoint_ids": model_ids,
        },
        "counts": {
            "routes": len(ordered),
            "direct": len(direct),
            "primitives": len(primitives),
            "composites": len(composites),
            "targets": len(targets),
            "llm_clients": len(clients),
        },
        "routes": ordered,
        "composites": [
            {
                "n": index,
                "name": name,
                "children": list(spec.get("children") or []),
                "context_window": spec.get("context_window"),
                "tool_calling": spec.get("tool_calling"),
                "reasoning": spec.get("reasoning"),
                "vision": spec.get("vision"),
            }
            for index, (name, spec) in enumerate(sorted(composites.items()), start=1)
        ],
        "providers": provider_rows,
        "decision_lanes": [
            {"n": index, "name": name, "targets": list(body.get("targets") or []), "kind": "targets"}
            for index, (name, body) in enumerate(
                sorted((catalog.get("decision_lane_defs") or {}).items()), start=1
            )
        ],
        "decision_lane_compositions": [
            {"n": index, "name": name, "children": list(body.get("children") or []), "kind": "children"}
            for index, (name, body) in enumerate(
                sorted((catalog.get("decision_lane_composition") or {}).items()), start=1
            )
        ],
        "capabilities": [
            {"n": index, "name": name, **body}
            for index, (name, body) in enumerate(
                sorted((catalog.get("capability_defs") or {}).items()), start=1
            )
        ],
    }


# --------------------------------------------------------------------------
# rendering
# --------------------------------------------------------------------------


def _fmt_int(value: Any) -> str:
    return f"{int(value):,}" if isinstance(value, (int, float)) else "—"


def _yn(value: Any) -> str:
    if value is True:
        return "y"
    if value is False:
        return "n"
    return "—"


def _reasoning(record: dict[str, Any]) -> str:
    if record.get("reasoning_policy"):
        return f"`{record['reasoning_policy']}` (route)"
    if record.get("target_reasoning_policy"):
        return f"`{record['target_reasoning_policy']}` (target)"
    return "—"


def _candidates(record: dict[str, Any]) -> str:
    if not record.get("candidates"):
        return "—"
    return ", ".join(f"`{c['id']}`" for c in record["candidates"])


def render_route_lineup(inventory: dict[str, Any]) -> str:
    """The numbered inventory. One row per route, 1..N, direct then primitive."""
    lines = [
        "| # | Route | Type | Provider | Primary upstream ID | Context | Tools | Vision | Reasoning policy | Ordered fallback candidates |",
        "|---|---|---|---|---|---|---|---|---|---|",
    ]
    for record in inventory["routes"]:
        lines.append(
            "| {n} | `{name}` | {kind} | {provider} | `{pid}` | {ctx} | {tools} | {vision} | "
            "{reasoning} | {candidates} |".format(
                n=record["n"],
                name=record["name"],
                kind=record["kind"],
                provider=record["provider"],
                pid=record["primary_id"],
                ctx=_fmt_int(record.get("context_window")),
                tools=_yn(record.get("tool_calling")),
                vision=_yn(record.get("vision")),
                reasoning=_reasoning(record),
                candidates=_candidates(record),
            )
        )
    counts = inventory["counts"]
    lines.append("")
    lines.append(
        f"<sub>{counts['direct']} direct (rows 1–{counts['direct']}) · "
        f"{counts['primitives']} primitive (rows {counts['direct'] + 1}–{counts['routes']}) · "
        f"{counts['routes']} total. `(route)` = route-level policy, applied last and therefore "
        "authoritative; `(target)` = the target's own default, which only takes effect under "
        "`inherit`. Context is the route's declared window.</sub>"
    )
    return "\n".join(lines)


def render_summary(inventory: dict[str, Any]) -> str:
    counts = inventory["counts"]
    primitives = [r["name"] for r in inventory["routes"] if r["kind"] == "primitive"]
    no_tools = [r["name"] for r in inventory["routes"] if r.get("tool_calling") is False]
    manifest = inventory["manifest"]
    runtime = inventory["runtime"]
    return "\n".join(
        [
            f"**Routes served:** {counts['routes']} — {counts['direct']} direct, "
            f"{counts['primitives']} primitive",
            "&nbsp;&nbsp;primitive: " + ", ".join(f"`{name}`" for name in primitives),
            "",
            f"**Providers:** {counts['llm_clients']} clients · "
            f"**Composite tiers:** {counts['composites']} · "
            f"**Decision lanes:** {len(inventory['decision_lanes'])} "
            f"(+{len(inventory['decision_lane_compositions'])} composed)",
            "",
            f"**Catalog entries:** {counts['routes']} · "
            f"**no tool calling:** {len(no_tools)} "
            f"({', '.join(no_tools) if no_tools else 'none'})",
            "",
            f"**Runtime catalog:** `GET /v1/models` returns "
            f"{runtime['models_endpoint_count']} ids — "
            + (
                "same set as the manifest."
                if runtime["models_endpoint_count"] == counts["routes"]
                else "**MISMATCH against the manifest — do not trust this panel.**"
            ),
            "",
            f"<sub>STATIC CATALOG — derived from manifest `{manifest['path']}` · "
            f"sha256 `{str(manifest['sha256'])[:16]}…` · collected {inventory['collected_at']}. "
            f"Binary `{runtime['binary_version']}`, PID {runtime['main_pid']}. "
            "Every count here is the row count of ROUTE LINEUP.</sub>",
        ]
    )


def render_providers(inventory: dict[str, Any], egress: dict[str, Any] | None = None) -> str:
    egress = egress or {}
    lines = [
        "| # | Provider | Endpoint | Format | Routes | Upstream IDs | Unauthenticated reachability |",
        "|---|---|---|---|---|---|---|",
    ]
    for row in inventory["providers"]:
        probe = egress.get(row["name"]) or {}
        if probe:
            reach = (
                f"HTTP {probe.get('status')} at {probe.get('at')} "
                "(TCP+TLS only, no credential sent)"
            )
        else:
            reach = "not probed"
        ids = ", ".join(f"`{i}`" for i in row["primary_ids"]) or "—"
        if row["candidate_only_ids"]:
            ids += " · fallback: " + ", ".join(f"`{i}`" for i in row["candidate_only_ids"])
        lines.append(
            f"| {row['n']} | **{row['name']}** | `{row['base_url']}` | `{row['format']}` | "
            f"{row['route_count']} | {ids} | {reach} |"
        )
    lines += [
        "",
        "*Route counts are primary-target attribution and sum to the ROUTE LINEUP row count. "
        "Reachability is an **unauthenticated** network probe: it proves the endpoint answers, "
        "never that the credential is valid or that inference would succeed. No inference "
        "request is sent by the probe.*",
        "",
        "*`openai` and `openai_nt` share the upstream id `gpt-6-luna`, so the Prometheus `model` "
        "label cannot separate them. Traffic for that id is reported once, as `openai/openai_nt`.*",
    ]
    return "\n".join(lines)


def render_tiers(inventory: dict[str, Any]) -> str:
    lines = [
        "**Whole-route composites**",
        "",
        "| # | Tier | Child 1 | Child 2 | Context |",
        "|---|---|---|---|---|",
    ]
    for composite in inventory["composites"]:
        children = composite["children"] + ["—"] * (2 - len(composite["children"]))
        lines.append(
            f"| {composite['n']} | `{composite['name']}` | `{children[0]}` | `{children[1]}` | "
            f"{_fmt_int(composite['context_window'])} |"
        )

    lines += ["", "**Decision lanes**", "", "| # | Lane | Kind | Members |", "|---|---|---|---|"]
    # One sequence across both kinds: they share a table, so restarting the
    # count at the composed lane would read as a duplicate row.
    lane_rows = []
    for lane in inventory["decision_lanes"]:
        lane_rows.append(
            (lane["name"], "targets", ", ".join(f"`{t}`" for t in lane["targets"]))
        )
    for comp in inventory["decision_lane_compositions"]:
        lane_rows.append(
            (comp["name"], "composed", ", ".join(f"`{c}`" for c in comp["children"]))
        )
    for index, (name, kind, members) in enumerate(lane_rows, start=1):
        lines.append(f"| {index} | `{name}` | {kind} | {members} |")
    lines.append("")
    lines.append(
        "*No default lane is configured and `/v1/decisions` requires an explicit `model`; a "
        "request that omits it is rejected rather than routed to an implicit default.*"
    )

    lines += ["", "**Capabilities**", "", "| # | id | target | contract / limits |", "|---|---|---|---|"]
    for cap in inventory["capabilities"]:
        parts = []
        if cap.get("contract"):
            parts.append(f"contract `{cap['contract']}`")
        if cap.get("dimensions"):
            parts.append(f"{cap['dimensions']}-dim {cap.get('normalization') or ''}".strip())
        if cap.get("max_batch"):
            parts.append(f"batch ≤ {cap['max_batch']}")
        if cap.get("max_candidates"):
            parts.append(f"candidates ≤ {cap['max_candidates']}")
        if cap.get("top_n"):
            parts.append(f"top_n {cap['top_n']}")
        if cap.get("max_query_chars"):
            parts.append(f"query ≤ {cap['max_query_chars']}ch")
        if cap.get("max_doc_chars"):
            parts.append(f"doc ≤ {cap['max_doc_chars']}ch")
        lines.append(
            f"| {cap['n']} | `{cap['name']}` | `{cap.get('target') or '—'}` | "
            f"{' · '.join(parts) or '—'} |"
        )
    return "\n".join(lines)


def render_free_ladder(inventory: dict[str, Any]) -> str:
    """The free ladder, derived from the same records as ROUTE LINEUP.

    R1 is the route's primary target; R2 is its first ordered candidate. Both
    primitives share the same physical ladder, so the difference between them is
    the route policy, which is shown per column rather than per row.
    """
    by_name = {r["name"]: r for r in inventory["routes"]}
    routes = [by_name.get(name) for name in ("smartfree", "smartfree-bounded")]
    if any(record is None for record in routes) or any(not r["candidates"] for r in routes):
        return (
            "**STATIC CATALOG** (manifest, not measurement)\n\n"
            "**MISSING** - `smartfree`, `smartfree-bounded` or their ordered candidate are "
            "absent from the manifest. Do not trust this panel."
        )
    primary = routes[0]
    candidate = primary["candidates"][0]

    def policy(record: dict[str, Any]) -> str:
        return f"`{record.get('reasoning_policy')}` (route)"

    lines = [
        "**STATIC CATALOG** (manifest, not measurement)",
        "",
        "| Rank | Role | Upstream ID | Context | Vision | `smartfree` policy | "
        "`smartfree-bounded` policy |",
        "|---|---|---|---|---|---|---|",
        f"| R1 | primary | `{primary['primary_id']}` | {_fmt_int(primary.get('context_window'))} | "
        f"**{_yn(primary.get('vision'))}** | {policy(routes[0])} | {policy(routes[1])} |",
        f"| R2 | ordered candidate | `{candidate['id']}` | {_fmt_int(primary.get('context_window'))} "
        f"| **{_yn(candidate.get('vision'))}** | {policy(routes[0])} | {policy(routes[1])} |",
        "",
        "*R2 is text-only, so an image request that loses R1 has no eligible candidate left: "
        "standalone `smartfree` fails closed, and a composite promotes to its next child "
        "instead. That is the designed behaviour, not a defect.*",
        "",
        "*Reasoning: the R2 target declares `reasoning_policy = enabled`, but that is a "
        "**target default** and only applies under `inherit`. `smartfree` is `inherit`, so the "
        "caller's value survives. `smartfree-bounded` is `none`, and a route policy is applied "
        "last, so a bounded request cannot re-enable reasoning through the candidate.*",
        "",
        "*Verified 2026-10-17 by deterministic fault injection on an isolated instance (mock 429 "
        "on both free targets):* `smartfree` TEXT → Xing; `smartfree` IMAGE → 503 with no Xing "
        "call; `tier1`/`tier1-bounded`/`tier2`/`tier2-bounded` each promoted and served.",
    ]
    return "\n".join(lines)


def panel_content(inventory: dict[str, Any], egress: dict[str, Any] | None = None) -> dict[int, str]:
    return {
        9002: render_summary(inventory),
        9005: render_providers(inventory, egress),
        9007: render_route_lineup(inventory),
        9008: render_tiers(inventory),
        9201: render_free_ladder(inventory),
    }


def provider_queries(inventory: dict[str, Any]) -> list[tuple[str, str]]:
    """One PromQL series per provider, selecting the live model ids it serves.

    Attribution follows the TARGET that declares an id, never the route that
    happens to use it. A route's ordered candidate routinely lives on another
    provider -- ``smart`` targets deepseek and falls back to openrouter,
    ``smart-bounded`` targets openai and falls back to openrouter -- so
    grouping a candidate under the route's primary provider credits one client
    with traffic another client served. That is exactly the double-counting
    this function used to produce.

    Keying on the id's own target makes the selectors disjoint by
    construction: an id belongs to exactly one target, and a target names
    exactly one ``llm_client``.

    The one unavoidable exception is a SHARED upstream id: ``openai`` and
    ``openai_nt`` both serve ``gpt-6-luna``, so the ``model`` label cannot
    separate them. They are emitted as one merged series rather than split on
    a label that cannot distinguish them, and the panel says so instead of
    implying a split the metrics cannot support.
    """
    grouped: dict[str, set[str]] = {}
    for record in inventory["routes"]:
        primary_provider = str(record.get("provider") or "")
        primary_id = str(record.get("primary_id") or "")
        if primary_provider and primary_id:
            grouped.setdefault(primary_provider, set()).add(primary_id)
        for candidate in record["candidates"]:
            # The candidate's OWN provider, not the route's. An id with no
            # resolved provider (a target absent from the manifest) stays
            # unselected rather than being attributed to whichever route
            # happened to name it.
            provider = str(candidate.get("provider") or "")
            candidate_id = str(candidate.get("id") or "")
            if provider and candidate_id:
                grouped.setdefault(provider, set()).add(candidate_id)

    shared = {"openai", "openai_nt"}
    # Only providers that actually contribute an id take part in the merge.
    # Merging unconditionally would label a lone `openai` series
    # "openai/openai_nt" and imply a split the metrics never had to make.
    shared_present = sorted(p for p in grouped if p in shared and grouped[p])
    merged_ids: set[str] = set()
    for provider in shared_present:
        merged_ids |= grouped[provider]
    queries: list[tuple[str, str]] = []
    for provider in sorted(grouped):
        if provider in shared:
            continue
        ids = sorted(grouped[provider])
        if not ids:
            continue
        pattern = "|".join(_promql_escape(i) for i in ids)
        queries.append(
            (
                provider,
                f'sum(rate(switchyard_requests_total{{job="switchyard-v7", model=~"{pattern}"}}[5m]))',
            )
        )
    if merged_ids:
        pattern = "|".join(_promql_escape(i) for i in sorted(merged_ids))
        label = (
            "openai/openai_nt (gpt-6-luna shared)"
            if len(shared_present) > 1
            else shared_present[0]
        )
        queries.append(
            (
                label,
                f'sum(rate(switchyard_requests_total{{job="switchyard-v7", model=~"{pattern}"}}[5m]))',
            )
        )
    return queries


def _promql_escape(value: str) -> str:
    """Prepare a literal model id for a PromQL regex string.

    Only the string-literal delimiters are escaped. Dots are deliberately left
    alone: PromQL rejects an escaped dot inside a quoted string, and an
    unescaped dot can only widen the match to ids that do not exist.
    """
    return value.replace("\\", "\\\\").replace('"', '\\"')


# --------------------------------------------------------------------------
# dashboard application
# --------------------------------------------------------------------------


def normalize_text_panels(document: dict[str, Any]) -> list[int]:
    """Make every text panel carry the same markdown in both content fields.

    Grafana stores a text panel's markdown twice: the panel's top-level
    ``content`` and ``options.content``. A panel with only one of them is a
    coin flip at render time, and a panel written to only the legacy field is
    how V7.2's static edits appeared to succeed while the rendered text stayed
    at V7.1. Returns the ids that were changed.
    """
    changed: list[int] = []
    for panel in document.get("panels") or []:
        if panel.get("type") != "text":
            continue
        options = panel.setdefault("options", {})
        top = panel.get("content")
        legacy = options.get("content")
        text = top if isinstance(top, str) and top.strip() else legacy
        if not isinstance(text, str):
            continue
        if top != text or legacy != text:
            changed.append(panel.get("id"))
        panel["content"] = text
        options["mode"] = options.get("mode") or "markdown"
        options["content"] = text
        migration = options.get("__angularMigration")
        if isinstance(migration, dict):
            original = migration.get("originalOptions")
            if isinstance(original, dict) and "content" in original:
                original["content"] = text
    return changed


PROVIDER_PANEL_TITLE = "REQUESTS BY PROVIDER"


def apply_provider_queries(document: dict[str, Any], inventory: dict[str, Any]) -> None:
    """Rewrite the provider panel's series from the current inventory.

    Why this is not optional: the panel's description promises selectors
    generated from the manifest. Before this, nothing regenerated them -- the
    static text panels were rewritten and the provider series kept whatever
    ids the last author typed. A manifest change therefore left a selector
    naming a retired id (silently counting zero) and omitted a new one
    (silently counting nothing). Failing loudly when the panel is absent is
    the same posture the static panels already take.

    Only ``targets`` is replaced: the panel's type, layout, datasource and
    operator-authored description survive, so this stays a data correction
    rather than a panel rebuild.
    """
    panel = next(
        (p for p in document.get("panels") or [] if p.get("title") == PROVIDER_PANEL_TITLE),
        None,
    )
    if panel is None:
        raise SystemExit(f"panel titled {PROVIDER_PANEL_TITLE!r} is not in the document")
    existing = panel.get("targets") or []
    datasource = existing[0].get("datasource") if existing else None
    targets: list[dict[str, Any]] = []
    for index, (label, expr) in enumerate(provider_queries(inventory)):
        target: dict[str, Any] = {
            "expr": expr,
            "legendFormat": label,
            "refId": chr(ord("A") + index),
        }
        if datasource is not None:
            target["datasource"] = datasource
        targets.append(target)
    panel["targets"] = targets


def apply_to_dashboard(
    document: dict[str, Any],
    inventory: dict[str, Any],
    *,
    title: str,
    egress: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Rewrite every generated panel from the inventory. Fails if one is missing."""
    content = panel_content(inventory, egress)
    present = {p.get("id"): p for p in document.get("panels") or []}
    for panel_id, expected_title in STATIC_PANEL_IDS.items():
        panel = present.get(panel_id)
        if panel is None:
            raise SystemExit(f"panel {panel_id} ({expected_title}) is not in the document")
        if panel.get("title") != expected_title:
            raise SystemExit(
                f"panel {panel_id} is titled {panel.get('title')!r}, expected {expected_title!r}"
            )
        text = content[panel_id]
        panel["content"] = text
        options = panel.setdefault("options", {})
        options["mode"] = "markdown"
        options["content"] = text
        legacy = options.get("__angularMigration")
        if isinstance(legacy, dict):
            original = legacy.get("originalOptions")
            if isinstance(original, dict) and "content" in original:
                original["content"] = text
    document["title"] = title
    apply_provider_queries(document, inventory)
    for key in ("id", "version"):
        document.pop(key, None)
    return document


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------


def _load(path: str) -> dict[str, Any]:
    with open(path, encoding="utf-8") as handle:
        return json.load(handle)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    inventory_cmd = sub.add_parser("inventory", help="build the canonical inventory")
    inventory_cmd.add_argument("--catalog", required=True)
    inventory_cmd.add_argument("--models", required=True)
    inventory_cmd.add_argument("--status", required=True)
    inventory_cmd.add_argument("--egress")
    inventory_cmd.add_argument("--collected-at", required=True)
    inventory_cmd.add_argument("--out", required=True)

    apply_cmd = sub.add_parser("apply", help="rewrite static panels of a dashboard document")
    apply_cmd.add_argument("--dashboard", required=True)
    apply_cmd.add_argument("--inventory", required=True)
    apply_cmd.add_argument("--egress")
    apply_cmd.add_argument("--title", required=True)
    apply_cmd.add_argument("--out", required=True)

    args = parser.parse_args(argv)
    if args.command == "inventory":
        inventory = build_inventory(
            _load(args.catalog),
            _load(args.models),
            _load(args.status),
            collected_at=args.collected_at,
        )
        with open(args.out, "w", encoding="utf-8") as handle:
            json.dump(inventory, handle, indent=1)
        counts = inventory["counts"]
        print(
            f"inventory: {counts['routes']} routes "
            f"({counts['direct']} direct + {counts['primitives']} primitive), "
            f"{counts['composites']} composites -> {args.out}"
        )
        return 0

    document = _load(args.dashboard)
    # A raw dashboard read is wrapped; accept either shape.
    document = document.get("document", document)
    egress = _load(args.egress) if args.egress else None
    updated = apply_to_dashboard(
        document, _load(args.inventory), title=args.title, egress=egress
    )
    with open(args.out, "w", encoding="utf-8") as handle:
        json.dump(updated, handle, indent=1)
    print(f"applied: {len(STATIC_PANEL_IDS)} static panels -> {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
