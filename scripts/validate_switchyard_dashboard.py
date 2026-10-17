#!/usr/bin/env python3
"""Fail-closed inventory validation for the Switchyard dashboard.

Compares four things that used to drift apart silently:

  A. the active production manifest (via the runtime ``catalog`` verb)
  B. the live ``GET /v1/models`` catalogue
  C. the canonical numbered inventory generated from A
  D. the published dashboard's static panels and PromQL selectors

A successful PromQL sweep says nothing about whether the panels describe the
deployment. This module checks the description.

EXIT CODES
  0  every check passed
  1  at least one violation (the caller must not publish)

USAGE
  python validate_switchyard_dashboard.py \\
      --catalog catalog.json --models models.json --inventory inventory.json \\
      --dashboard published.json
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from typing import Any

# Names that must never appear as a live route or model again. They may appear
# only on a line that explicitly marks them as historical.
RETIRED_TOKENS = (
    "apodex/apodex-1.1-mini:free",
    "inclusionai/ling-3.1-flash",
    "direct-openrouter-apodex-1.1-mini:free",
    "direct-openrouter-ling-3_1-flash",
)

HISTORICAL_MARKERS = ("historical", "retired", "no longer")

STATIC_PANEL_IDS = (9002, 9005, 9007, 9008, 9201)


# --------------------------------------------------------------------------
# markdown parsing
# --------------------------------------------------------------------------


def panel_texts(panel: dict[str, Any]) -> dict[str, str]:
    """Every place this panel's markdown can live.

    Grafana keeps text in the panel's top-level ``content`` and in
    ``options.content``; the two can drift, and a write to only one of them
    looks successful while the rendered panel keeps the old text. Both are
    returned so the caller can assert they agree.
    """
    found: dict[str, str] = {}
    if isinstance(panel.get("content"), str):
        found["content"] = panel["content"]
    options = panel.get("options") if isinstance(panel.get("options"), dict) else {}
    if isinstance(options.get("content"), str):
        found["options.content"] = options["content"]
    migration = options.get("__angularMigration")
    if isinstance(migration, dict):
        original = migration.get("originalOptions")
        if isinstance(original, dict) and isinstance(original.get("content"), str):
            found["__angularMigration.originalOptions.content"] = original["content"]
    return found


def _cells(line: str) -> list[str]:
    """Split a table line on literal pipes.

    Cell text is generated from manifest ids and route names. An id containing
    ``|`` or a backtick would not round-trip, so the shape and id checks reject
    such a table instead of publishing a mangled one -- failing closed is the
    intended behaviour, not a parsing limitation to work around.
    """
    stripped = line.strip()
    if not stripped.startswith("|"):
        return []
    return [cell.strip() for cell in stripped.strip("|").split("|")]


def _is_rule(cells: list[str]) -> bool:
    """A GFM separator row: every cell is dashes/colons, at least one dash."""
    return (
        bool(cells)
        and all(cell and set(cell) <= {"-", ":", " "} for cell in cells)
        and any("-" in cell for cell in cells)
    )


def _table_blocks(markdown: str) -> list[tuple[int, list[str]]]:
    """Every run of consecutive table lines, with the line number it starts on."""
    blocks: list[tuple[int, list[str]]] = []
    current: list[str] = []
    start = 0
    for index, line in enumerate(markdown.splitlines()):
        if _cells(line):
            if not current:
                start = index
            current.append(line)
        elif current:
            blocks.append((start, current))
            current = []
    if current:
        blocks.append((start, current))
    return blocks


def table_shape_problems(markdown: str) -> list[str]:
    """Structural defects that make a table render or parse wrongly.

    A table without a separator rule is a paragraph in GFM; a row with more
    cells than the header loses the extras silently, and a row with fewer makes
    ``_cell`` return ``""`` -- which reads as "not declared" and can pass a
    comparison against an empty manifest value. Neither is a content drift, so
    neither is caught by comparing cells: they need their own check.

    Every table in the panel is checked, not just the one that ``parse_table``
    reads: a malformed secondary table still renders as prose.
    """
    blocks = _table_blocks(markdown)
    if not blocks:
        return ["no markdown table"]
    problems: list[str] = []
    for start, block in blocks:
        problems.extend(_table_block_problems(block, start))
    return problems


def _table_block_problems(block: list[str], start: int) -> list[str]:
    header = _cells(block[0])
    problems: list[str] = []
    if len(block) < 2 or not _is_rule(_cells(block[1])):
        problems.append(
            f"line {start + 1}: header row is not followed by a separator rule, "
            "so it will not render as a table"
        )
        position = 1
    else:
        rule = _cells(block[1])
        if len(rule) != len(header):
            problems.append(
                f"line {start + 2}: separator has {len(rule)} column(s), header has {len(header)}"
            )
        position = 2
    for offset in range(position, len(block)):
        cells = _cells(block[offset])
        if _is_rule(cells):
            problems.append(f"line {start + offset + 1} is a second separator inside the table")
        elif len(cells) != len(header):
            problems.append(
                f"line {start + offset + 1} has {len(cells)} cell(s), header has {len(header)}"
            )
    return problems


def check_table_shape(markdown: str, label: str, v: list[str]) -> bool:
    """Append shape violations; return False when the table cannot be trusted."""
    problems = table_shape_problems(markdown)
    for problem in problems:
        v.append(f"{label}: {problem}")
    return not problems


def parse_table(markdown: str) -> list[list[str]]:
    """Return data rows of the first markdown table (header and rule dropped)."""
    return parse_table_with_header(markdown)[1]


def parse_table_with_header(markdown: str) -> tuple[list[str], list[list[str]]]:
    """Return ``(header, rows)`` for the first markdown table.

    Parsing by header name rather than by column position is what lets the
    validator say "this table is the wrong shape" once, instead of emitting one
    confusing message per row when a panel was never regenerated.
    """
    header: list[str] = []
    rows: list[list[str]] = []
    in_table = False
    for line in markdown.splitlines():
        cells = _cells(line)
        if not cells:
            if in_table:
                break
            continue
        if set("".join(cells)) <= {"-", ":", " "}:
            continue
        if not in_table:
            in_table = True
            header = cells
            continue
        rows.append(cells)
    return header, rows


def _index(header: list[str], name: str) -> int | None:
    for position, column in enumerate(header):
        if column.strip().lower() == name.lower():
            return position
    return None


def _policy_token(cell: str) -> str:
    """Extract the policy name from a cell like `` `none` (route) ``."""
    match = re.search(r"`([^`]+)`", cell)
    if match:
        return match.group(1)
    return _unquote(cell).split(" (")[0] or EMPTY


def _cell(row: list[str], header: list[str], name: str) -> str:
    position = _index(header, name)
    if position is None or position >= len(row):
        return ""
    return row[position]


def _unquote(cell: str) -> str:
    return cell.strip().strip("`").strip()


EMPTY = "—"  # the generator's placeholder for "not declared"


def _yn(value: Any) -> str:
    if value is True:
        return "y"
    if value is False:
        return "n"
    return EMPTY


# --------------------------------------------------------------------------
# checks
# --------------------------------------------------------------------------


def check_models_agree(catalog: dict[str, Any], models: dict[str, Any], v: list[str]) -> None:
    """A vs B: manifest routes and the runtime catalogue must be the same set."""
    routes = set((catalog.get("routes") or {}).keys())
    ids = {item.get("id") for item in (models.get("data") or [])}
    if routes != ids:
        missing = sorted(routes - ids)
        extra = sorted(ids - routes)
        v.append(
            f"/v1/models disagrees with the manifest: {len(routes)} routes vs {len(ids)} ids; "
            f"manifest-only={missing} models-only={extra}"
        )


def check_route_rows(
    catalog: dict[str, Any],
    inventory: dict[str, Any],
    lineup: str,
    v: list[str],
) -> None:
    """1-6: every route once, correct order, correct fields, sequential numbering."""
    expected = inventory["routes"]
    if not check_table_shape(lineup, "ROUTE LINEUP", v):
        return
    header, rows = parse_table_with_header(lineup)
    required = [
        "#",
        "Route",
        "Type",
        "Provider",
        "Primary upstream ID",
        "Context",
        "Tools",
        "Vision",
        "Reasoning policy",
        "Ordered fallback candidates",
    ]
    missing_columns = [name for name in required if _index(header, name) is None]
    if missing_columns:
        v.append(f"ROUTE LINEUP is missing column(s): {missing_columns}")
        return
    if len(rows) != len(expected):
        v.append(f"ROUTE LINEUP has {len(rows)} data rows, inventory has {len(expected)}")

    numbers: list[int] = []
    names: list[str] = []
    for index, row in enumerate(rows, start=1):
        raw = _cell(row, header, "#")
        if not raw.isdigit():
            v.append(f"ROUTE LINEUP row {index} has a non-numeric '#' cell: {raw!r}")
            continue
        numbers.append(int(raw))
        names.append(_unquote(_cell(row, header, "Route")))

    check_lineup_footer(rows, header, lineup, v)

    if numbers != list(range(1, len(numbers) + 1)):
        v.append(f"ROUTE LINEUP numbering is not sequential 1..N: {numbers}")
    duplicates = sorted({n for n in numbers if numbers.count(n) > 1})
    if duplicates:
        v.append(f"ROUTE LINEUP repeats row number(s) {duplicates}")
    duplicates = sorted({n for n in names if names.count(n) > 1})
    if duplicates:
        v.append(f"ROUTE LINEUP repeats route(s) {duplicates}")

    expected_names = [record["name"] for record in expected]
    if names != expected_names:
        v.append(
            "ROUTE LINEUP route order/content differs from the manifest: "
            f"first divergence at row {_first_diff(names, expected_names)}"
        )

    by_name = {record["name"]: record for record in expected}
    for row in rows:
        name = _unquote(_cell(row, header, "Route"))
        record = by_name.get(name)
        if record is None:
            v.append(f"ROUTE LINEUP lists {name!r}, which is not in the manifest")
            continue
        comparisons = {
            "Type": (_cell(row, header, "Type"), record["kind"]),
            "Provider": (_unquote(_cell(row, header, "Provider")), record["provider"]),
            "Primary upstream ID": (
                _unquote(_cell(row, header, "Primary upstream ID")),
                record["primary_id"],
            ),
            "Context": (
                _cell(row, header, "Context").replace(",", ""),
                str(record.get("context_window") or ""),
            ),
            "Tools": (_cell(row, header, "Tools"), _yn(record.get("tool_calling"))),
            "Vision": (_cell(row, header, "Vision"), _yn(record.get("vision"))),
            "Reasoning policy": (
                _policy_token(_cell(row, header, "Reasoning policy")),
                record.get("reasoning_policy") or record.get("target_reasoning_policy") or EMPTY,
            ),
        }
        for label, (got, want) in comparisons.items():
            if got != want:
                v.append(f"ROUTE LINEUP {name} {label}: published {got!r}, manifest {want!r}")
        candidate_ids = ", ".join(f"`{c['id']}`" for c in record["candidates"]) or EMPTY
        published_candidates = _cell(row, header, "Ordered fallback candidates")
        if published_candidates != candidate_ids:
            v.append(
                f"ROUTE LINEUP {name} fallback candidates: published "
                f"{published_candidates!r}, manifest {candidate_ids!r}"
            )

    missing = sorted(set(expected_names) - set(names))
    if missing:
        v.append(f"ROUTE LINEUP is missing manifest route(s): {missing}")
    primitives = [r["name"] for r in expected if r["kind"] == "primitive"]
    missing_primitives = [name for name in primitives if name not in names]
    if missing_primitives:
        v.append(f"ROUTE LINEUP is missing primitive route(s): {missing_primitives}")


FOOTER_DIRECT_RE = re.compile(r"(\d+)\s+direct\s+\(rows\s+(\d+)[–-](\d+)\)")
FOOTER_PRIMITIVE_RE = re.compile(r"(\d+)\s+primitive\s+\(rows\s+(\d+)[–-](\d+)\)")
FOOTER_TOTAL_RE = re.compile(r"(\d+)\s+total\b")


def check_lineup_footer(
    rows: list[list[str]], header: list[str], lineup: str, v: list[str]
) -> None:
    """The footer restates the counts of the rows it sits under -- verify it.

    The footer is the line a reader trusts at a glance, and it is hand-placed
    prose, so it can disagree with the table it summarises without any cell
    comparison noticing.
    """
    footer_lines = [
        line
        for line in lineup.splitlines()
        if line.lstrip().startswith("<sub>") and FOOTER_TOTAL_RE.search(line)
    ]
    if not footer_lines:
        v.append("ROUTE LINEUP has no <sub> footer stating its row counts")
        return
    if len(footer_lines) > 1:
        v.append(f"ROUTE LINEUP states its row counts in {len(footer_lines)} footers")
    footer = footer_lines[0]
    kinds = [_cell(row, header, "Type").strip().lower() for row in rows]
    direct_rows = kinds.count("direct")
    primitive_rows = kinds.count("primitive")
    claims = (
        (FOOTER_DIRECT_RE, "direct", direct_rows, 1, direct_rows),
        (FOOTER_PRIMITIVE_RE, "primitive", primitive_rows, direct_rows + 1, len(rows)),
    )
    stated: dict[str, int] = {}
    for pattern, label, actual, first_row, last_row in claims:
        matches = pattern.findall(footer)
        if not matches:
            v.append(f"ROUTE LINEUP footer does not state a {label} count")
            continue
        if len(matches) > 1:
            v.append(f"ROUTE LINEUP footer states the {label} count {len(matches)} times")
        stated[label] = int(matches[0][0])
        if stated[label] != actual:
            v.append(f"ROUTE LINEUP footer says {stated[label]} {label}, the table has {actual}")
        published_range = (int(matches[0][1]), int(matches[0][2]))
        if published_range != (first_row, last_row):
            v.append(
                f"ROUTE LINEUP footer {label} rows "
                f"{published_range[0]}–{published_range[1]}, the table has "
                f"{first_row}–{last_row}"
            )
    totals = FOOTER_TOTAL_RE.findall(footer)
    if not totals:
        v.append("ROUTE LINEUP footer does not state a total")
        return
    if len(totals) > 1:
        v.append(f"ROUTE LINEUP footer states the total {len(totals)} times")
    stated_total = int(totals[0])
    if stated_total != len(rows):
        v.append(f"ROUTE LINEUP footer says {stated_total} total, the table has {len(rows)} rows")
    if len(stated) == len(claims) and sum(stated.values()) != stated_total:
        v.append(
            f"ROUTE LINEUP footer components sum to {sum(stated.values())}, "
            f"not its stated total {stated_total}"
        )


def _first_diff(got: list[str], want: list[str]) -> str:
    for index, (a, b) in enumerate(zip(got, want, strict=False), start=1):
        if a != b:
            return f"{index} ({a!r} vs {b!r})"
    return f"{min(len(got), len(want)) + 1} (length)"


def check_summary(inventory: dict[str, Any], summary: str, lineup_rows: list[list[str]], v: list[str]) -> None:
    """9: every count in the summary equals the real row count."""
    counts = inventory["counts"]
    expected_total = len(lineup_rows)
    match = re.search(r"\*\*Routes served:\*\*\s*(\d+)", summary)
    if not match:
        v.append("CATALOG SUMMARY does not state a route count")
    elif int(match.group(1)) != expected_total:
        v.append(
            f"CATALOG SUMMARY says {match.group(1)} routes, ROUTE LINEUP has {expected_total}"
        )
    for pattern, want, label in (
        (r"(\d+)\s+direct", counts["direct"], "direct"),
        (r"(\d+)\s+primitive", counts["primitives"], "primitive"),
        (r"\*\*Catalog entries:\*\*\s*(\d+)", counts["routes"], "catalog entries"),
    ):
        found = re.search(pattern, summary)
        if not found:
            v.append(f"CATALOG SUMMARY does not state the {label} count")
        elif int(found.group(1)) != want:
            v.append(f"CATALOG SUMMARY {label}: published {found.group(1)}, manifest {want}")

    for record in inventory["routes"]:
        if record["kind"] == "primitive" and f"`{record['name']}`" not in summary:
            v.append(f"CATALOG SUMMARY omits primitive {record['name']!r}")
    if "MISMATCH" in summary:
        v.append("CATALOG SUMMARY reports a manifest/runtime mismatch")


def check_tiers(inventory: dict[str, Any], tiers: str, v: list[str]) -> None:
    """5: composite children and their order match the manifest."""
    if not check_table_shape(tiers, "TIERS", v):
        return
    header, rows = parse_table_with_header(tiers)
    required = ["#", "Tier", "Child 1", "Child 2", "Context"]
    missing_columns = [name for name in required if _index(header, name) is None]
    if missing_columns:
        v.append(f"TIERS composite table is missing column(s): {missing_columns}")
        return
    by_name = {c["name"]: c for c in inventory["composites"]}
    seen = []
    for row in rows:
        name = _unquote(_cell(row, header, "Tier"))
        if name not in by_name:
            continue
        seen.append(name)
        composite = by_name[name]
        expected = composite["children"]
        published = [
            _unquote(_cell(row, header, "Child 1")),
            _unquote(_cell(row, header, "Child 2")),
        ]
        if published[: len(expected)] != expected:
            v.append(f"TIERS {name} children: published {published}, manifest {expected}")
        published_ctx = _cell(row, header, "Context").replace(",", "")
        if published_ctx != str(composite.get("context_window")):
            v.append(
                f"TIERS {name} context: published "
                f"{_cell(row, header, 'Context')!r}, manifest "
                f"{composite.get('context_window')}"
            )
    missing = sorted(set(by_name) - set(seen))
    if missing:
        v.append(f"TIERS is missing composite(s): {missing}")


def check_lanes(inventory: dict[str, Any], tiers: str, v: list[str]) -> None:
    for lane in inventory["decision_lanes"]:
        for target in lane["targets"]:
            if f"`{target}`" not in tiers:
                v.append(f"TIERS decision lane {lane['name']} omits target {target!r}")
    for comp in inventory["decision_lane_compositions"]:
        for child in comp["children"]:
            if f"`{child}`" not in tiers:
                v.append(f"TIERS composed lane {comp['name']} omits child {child!r}")


def check_capabilities(inventory: dict[str, Any], tiers: str, v: list[str]) -> None:
    for cap in inventory["capabilities"]:
        if f"`{cap['name']}`" not in tiers:
            v.append(f"TIERS capabilities table omits {cap['name']!r}")
        if cap.get("top_n") and f"top_n {cap['top_n']}" not in tiers:
            v.append(f"TIERS capability {cap['name']} does not show top_n={cap['top_n']}")


def check_providers(inventory: dict[str, Any], providers: str, v: list[str]) -> None:
    """11: provider route counts reconcile with the inventory."""
    if not check_table_shape(providers, "PROVIDER ACCESS", v):
        return
    header, rows = parse_table_with_header(providers)
    required = ["#", "Provider", "Endpoint", "Format", "Routes", "Upstream IDs"]
    missing_columns = [name for name in required if _index(header, name) is None]
    if missing_columns:
        v.append(f"PROVIDER ACCESS is missing column(s): {missing_columns}")
        return
    by_name = {p["name"]: p for p in inventory["providers"]}
    seen = 0
    for row in rows:
        name = _unquote(_cell(row, header, "Provider")).strip("*")
        provider = by_name.get(name)
        if provider is None:
            v.append(f"PROVIDER ACCESS lists unknown provider {name!r}")
            continue
        seen += 1
        published = _cell(row, header, "Routes")
        if published != str(provider["route_count"]):
            v.append(
                f"PROVIDER ACCESS {name}: published {published} routes, "
                f"inventory {provider['route_count']}"
            )
        # Row-scoped: the ids must be in this provider's own cell. A panel-wide
        # substring search passes when an id drifted into a neighbouring row.
        upstream_cell = _cell(row, header, "Upstream IDs")
        published_primary, _, published_fallback = upstream_cell.partition(" · fallback: ")
        published_primary_ids = re.findall(r"`([^`]+)`", published_primary)
        published_candidate_ids = re.findall(r"`([^`]+)`", published_fallback)
        if published_primary_ids != provider["primary_ids"]:
            v.append(
                f"PROVIDER ACCESS {name} upstream ids: published "
                f"{published_primary_ids}, inventory {provider['primary_ids']}"
            )
        if published_candidate_ids != provider["candidate_only_ids"]:
            v.append(
                f"PROVIDER ACCESS {name} fallback ids: published "
                f"{published_candidate_ids}, inventory {provider['candidate_only_ids']}"
            )
    total = sum(p["route_count"] for p in inventory["providers"])
    if total != inventory["counts"]["routes"]:
        v.append(
            f"provider route counts sum to {total}, route total is "
            f"{inventory['counts']['routes']}"
        )
    if seen != len(inventory["providers"]):
        v.append(
            f"PROVIDER ACCESS shows {seen} providers, inventory has "
            f"{len(inventory['providers'])}"
        )
    reach_col = _index(header, "Unauthenticated reachability")
    if reach_col is None:
        v.append(
            "PROVIDER ACCESS does not label its reachability column as an "
            "unauthenticated network probe"
        )
    elif "unauthenticated" not in providers.lower():
        v.append(
            "PROVIDER ACCESS body does not state that reachability was measured "
            "without credentials"
        )


def check_static_agreement(texts: dict[str, str], v: list[str]) -> None:
    """12: panels must agree with each other on the numbers they share."""
    counts = {}
    for panel_id in STATIC_PANEL_IDS:
        text = texts.get(panel_id, "")
        found = re.findall(r"(\d+)\s+direct,\s+(\d+)\s+primitive", text)
        if found:
            counts[panel_id] = tuple(int(x) for x in found[0])
    if len(set(counts.values())) > 1:
        v.append(f"static panels disagree on direct/primitive counts: {counts}")


def check_route_lineup_vs_free_ladder(inventory: dict[str, Any], texts: dict[str, str], v: list[str]) -> None:
    lineup = texts.get(9007, "")
    ladder = texts.get(9201, "")
    rows = {_unquote(r[1]): r for r in parse_table(lineup) if len(r) >= 10}
    for name in ("smartfree", "smartfree-bounded"):
        record = next((r for r in inventory["routes"] if r["name"] == name), None)
        if record is None:
            v.append(f"inventory is missing {name}")
            continue
        if name not in rows:
            v.append(f"ROUTE LINEUP is missing {name}")
            continue
        if record["primary_id"] not in rows[name][4]:
            v.append(f"ROUTE LINEUP {name} primary id differs from the manifest")
        expected = record["primary_id"]
        if expected not in ladder:
            v.append(f"FREE LADDER does not show {name}'s primary id {expected!r}")
        if record.get("reasoning_policy") and f"`{record['reasoning_policy']}` (route)" not in lineup:
            v.append(f"ROUTE LINEUP does not mark {name}'s route policy as route-level")


def check_selectors(
    inventory: dict[str, Any], dashboard: dict[str, Any], v: list[str]
) -> None:
    """13-14: PromQL model selectors cover the live inventory and no retired id."""
    patterns: list[str] = []
    for panel in dashboard.get("panels") or []:
        for target in panel.get("targets") or []:
            expr = target.get("expr") or ""
            for match in re.finditer(r'model=~"([^"]+)"', expr):
                patterns.append(match.group(1))

    live_ids = {r["primary_id"] for r in inventory["routes"] if r["primary_id"]}
    live_ids |= {
        c["id"] for r in inventory["routes"] for c in r["candidates"] if c["id"]
    }
    covered = set()
    for pattern in patterns:
        regex = re.compile(pattern)
        for model_id in live_ids:
            if regex.fullmatch(model_id):
                covered.add(model_id)
    uncovered = sorted(live_ids - covered)
    if uncovered:
        v.append(
            "PromQL model selectors do not cover live model id(s): " + ", ".join(uncovered)
        )

    for pattern in patterns:
        for retired in RETIRED_TOKENS:
            if retired in pattern:
                v.append(f"a PromQL model selector still names retired id {retired!r}")


def check_no_stale_text(texts: dict[str, str], v: list[str]) -> None:
    """2, 15: retired names may appear only on a line marked historical."""
    for panel_id, text in texts.items():
        for line in text.splitlines():
            low = line.lower()
            if any(marker in low for marker in HISTORICAL_MARKERS):
                continue
            for retired in RETIRED_TOKENS:
                if retired in line:
                    v.append(
                        f"panel {panel_id} presents retired id {retired!r} without marking "
                        "it historical"
                    )


def check_content_fields(dashboard: dict[str, Any], egress_ok: bool, v: list[str]) -> None:
    """16: every text panel must carry the same markdown in both content fields.

    A panel with only one field is a render-time coin flip; a panel whose two
    fields differ is the exact V7.2 failure (the write went to the legacy field
    and the rendered text stayed old).
    """
    panels = dashboard.get("panels") or []
    by_id = {p.get("id"): p for p in panels}
    for panel_id in STATIC_PANEL_IDS:
        if panel_id not in by_id:
            v.append(f"dashboard is missing static panel {panel_id}")
    for panel in panels:
        if panel.get("type") != "text":
            continue
        panel_id = panel.get("id")
        texts = panel_texts(panel)
        missing = {"content", "options.content"} - set(texts)
        if missing:
            v.append(
                f"panel {panel_id} ({panel.get('title')}) is missing content in "
                f"{sorted(missing)}; it can render blank or stale"
            )
            continue
        if texts["content"] != texts["options.content"]:
            v.append(
                f"panel {panel_id} ({panel.get('title')}) content fields disagree "
                "(a write to only one field leaves the rendered text stale)"
            )
        for key, text in texts.items():
            if not text.strip():
                v.append(f"panel {panel_id} ({panel.get('title')}) has empty {key}")
    if not egress_ok:
        v.append("PROVIDER ACCESS reachability was not probed for this run")


def check_dashboard(
    catalog: dict[str, Any],
    models: dict[str, Any],
    inventory: dict[str, Any],
    dashboard: dict[str, Any],
    *,
    egress_ok: bool = True,
) -> list[str]:
    """Run every check; return the violations (empty means publishable)."""
    violations: list[str] = []
    check_models_agree(catalog, models, violations)

    by_id = {p.get("id"): p for p in dashboard.get("panels") or []}
    texts: dict[int, str] = {}
    for panel_id in STATIC_PANEL_IDS:
        panel = by_id.get(panel_id) or {}
        found = panel_texts(panel)
        texts[panel_id] = found.get("content") or found.get("options.content") or ""

    check_route_rows(catalog, inventory, texts[9007], violations)
    check_summary(inventory, texts[9002], parse_table(texts[9007]), violations)
    check_tiers(inventory, texts[9008], violations)
    check_lanes(inventory, texts[9008], violations)
    check_capabilities(inventory, texts[9008], violations)
    check_providers(inventory, texts[9005], violations)
    check_static_agreement(texts, violations)
    check_route_lineup_vs_free_ladder(inventory, texts, violations)
    check_selectors(inventory, dashboard, violations)
    check_no_stale_text(texts, violations)
    check_content_fields(dashboard, egress_ok, violations)
    return violations


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------


def _load(path: str) -> dict[str, Any]:
    with open(path, encoding="utf-8") as handle:
        return json.load(handle)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Validate the Switchyard dashboard inventory")
    parser.add_argument("--catalog", required=True)
    parser.add_argument("--models", required=True)
    parser.add_argument("--inventory", required=True)
    parser.add_argument("--dashboard", required=True)
    parser.add_argument("--no-egress", action="store_true", help="skip the egress-probe requirement")
    args = parser.parse_args(argv)

    dashboard = _load(args.dashboard)
    dashboard = dashboard.get("document", dashboard)
    violations = check_dashboard(
        _load(args.catalog),
        _load(args.models),
        _load(args.inventory),
        dashboard,
        egress_ok=not args.no_egress,
    )
    if violations:
        print(f"FAIL — {len(violations)} violation(s):")
        for item in violations:
            print(f"  - {item}")
        return 1
    print("PASS — dashboard inventory agrees with the live manifest and runtime catalogue")
    return 0


if __name__ == "__main__":
    sys.exit(main())
