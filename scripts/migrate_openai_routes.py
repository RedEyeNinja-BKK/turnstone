#!/usr/bin/env python3
"""Migrate the three OpenAI registry aliases to direct OpenAI OAuth.

WHAT IT CHANGES (and nothing else)
  gpt-6-luna      -> provider openai, model gpt-6-luna,    thinking on
  gpt-6-luna-nt   -> provider openai, model gpt-6-luna,    thinking off
  gpt-6.1-sol     -> provider openai, model gpt-6.1-sol,   thinking on
All three: base_url https://api.openai.com/v1, auth_mode openai_oauth,
obo_audience https://api.openai.com/v1, no Switchyard reference.

HOW IT TALKS TO TURNSTONE
Through the approved self-MCP bridge (see scripts/turnstone_self_api.py), so the
root-only management token never enters this process. Every call leaves a bridge
receipt id in the output.

SAFETY
  * Refuses unless the RUNNING console accepts auth_mode=openai_oauth. Before the
    code activation it does not, and a late refusal after two successful writes
    would leave a half-migrated registry.
  * `snapshot` captures every row it will touch, including the stored api_key,
    into a rollback file. Run it first; it is read-only against the registry.
  * The allow-list is written as a union; existing entries are never dropped.
  * `verify` re-reads the registry and asserts the target shape, and reports any
    remaining Switchyard reference for these three aliases.

The static api_key CANNOT be cleared through this API: the update handler treats
"" and "***" as "keep the existing value". It is inert for an openai_oauth row
(the dispatch never reads it and fails closed unconditionally), but it remains
stored. `--allow-residual-key` must be passed to acknowledge that; otherwise the
script stops and says so.

Usage:
  python scripts/migrate_openai_routes.py status
  python scripts/migrate_openai_routes.py snapshot
  python scripts/migrate_openai_routes.py apply --allow-residual-key
  python scripts/migrate_openai_routes.py verify
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import pathlib
import sys
from typing import Any

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

from turnstone_self_api import call  # noqa: E402

RESOURCE = "https://api.openai.com/v1"
SWITCHYARD_HOST = "switchyard-vm"

ALIASES: dict[str, dict[str, Any]] = {
    "gpt-6-luna": {
        "model": "gpt-6-luna",
        "reasoning_effort": "medium",
        "surface_persisted_reasoning": True,
        "replay_reasoning_to_model": False,
    },
    "gpt-6-luna-nt": {
        "model": "gpt-6-luna",
        # "none" is forwarded verbatim only when the row DECLARES a "none"
        # level; without that declaration Turnstone omits the parameter and the
        # server default applies (which may be reasoning ON). Declaring it is
        # therefore the mechanism for the non-thinking contract -- and it is
        # only honest if the provider accepts it, which `verify` must prove.
        "reasoning_effort": "none",
        "surface_persisted_reasoning": False,
        "replay_reasoning_to_model": False,
    },
    "gpt-6.1-sol": {
        "model": "gpt-6.1-sol",
        # Measured on this deployment: gpt-6.1-sol emits no reasoning at
        # low/medium and does at high. "high" is therefore the lowest level at
        # which "thinking" is an observation rather than a label.
        "reasoning_effort": "high",
        "surface_persisted_reasoning": True,
        "replay_reasoning_to_model": False,
    },
}

# Commercial OpenAI rows: no operator capability table for the compat lane, and
# no server_compat block (that describes an operator-run compatible server).
CAPABILITIES: dict[str, Any] = {
    "supports_vision": True,
    "supports_web_search": False,
    "supports_pdf": False,
}

SNAPSHOT_DIR = pathlib.Path(
    os.environ.get("ROUTE_SNAPSHOT_DIR", "/var/lib/turnstone/route-snapshots")
)


def _rows(body: Any) -> list[dict[str, Any]]:
    if isinstance(body, dict):
        rows = body.get("models") or body.get("model_definitions") or []
    else:
        rows = body or []
    return [r for r in rows if isinstance(r, dict)]


async def _list() -> list[dict[str, Any]]:
    out = await call("GET", "/v1/api/admin/model-definitions")
    if out.get("status") != 200:
        raise SystemExit(f"list failed: HTTP {out.get('status')} {out.get('body')}")
    return _rows(out.get("body"))


async def _constraints() -> dict[str, Any]:
    out = await call("GET", "/v1/api/admin/model-definitions/auth-constraints")
    if out.get("status") != 200:
        raise SystemExit(f"auth-constraints failed: HTTP {out.get('status')}")
    return out.get("body") or {}


def _by_alias(rows: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    return {r["alias"]: r for r in rows if r.get("alias") in ALIASES}


def _definition_id(row: dict[str, Any]) -> str:
    return str(row.get("definition_id") or row.get("id") or "")


def _is_switchyard(row: dict[str, Any]) -> bool:
    return SWITCHYARD_HOST in str(row.get("base_url") or "") or str(
        row.get("provider") or ""
    ) == "openai-compatible"


async def cmd_status() -> int:
    rows = _by_alias(await _list())
    cons = await _constraints()
    modes = cons.get("dynamic_auth_modes") or []
    print(f"running console dynamic_auth_modes: {sorted(modes)}")
    print(f"openai_oauth accepted by the live console: {'openai_oauth' in modes}")
    print(f"allow-list: {cons.get('auth_audience_allowlist')}")
    print()
    for alias in ALIASES:
        row = rows.get(alias)
        if not row:
            print(f"{alias:14s} MISSING")
            continue
        print(
            f"{alias:14s} id={_definition_id(row)[:8]} provider={row.get('provider')!r} "
            f"model={row.get('model')!r} auth={row.get('auth_mode')!r} "
            f"switchyard={_is_switchyard(row)}"
        )
        print(f"{'':14s} base_url={row.get('base_url')}")
    return 0


async def cmd_snapshot() -> int:
    rows = _by_alias(await _list())
    missing = [a for a in ALIASES if a not in rows]
    if missing:
        raise SystemExit(f"refusing: aliases missing from the registry: {missing}")
    SNAPSHOT_DIR.mkdir(parents=True, exist_ok=True)
    os.chmod(SNAPSHOT_DIR, 0o700)
    out = SNAPSHOT_DIR / "before.json"
    # Full rows, including the stored api_key, so a rollback can restore the
    # exact prior configuration. The file is 0600 and never printed.
    out.write_text(json.dumps(rows, indent=2, sort_keys=True), encoding="utf-8")
    os.chmod(out, 0o600)
    print(f"snapshot: {out} ({len(rows)} rows, mode 0600)")
    for alias, row in sorted(rows.items()):
        print(f"  {alias}: id={_definition_id(row)} baseline captured")
    return 0


def _target_payload(alias: str, row: dict[str, Any]) -> dict[str, Any]:
    spec = ALIASES[alias]
    caps = dict(CAPABILITIES)
    # Only declare an effort ladder when the route needs one AND the value can
    # be forwarded: "none" requires a declared "none" level.
    if spec["reasoning_effort"] == "none":
        caps["reasoning_effort_values"] = ["none"]
    elif spec["model"] == "gpt-6.1-sol":
        # Provider-supported set, quoted verbatim by a 400 on 'minimal':
        # "Supported values are: 'low', 'medium', 'high', 'xhigh', and 'max'".
        caps["reasoning_effort_values"] = ["low", "medium", "high", "xhigh", "max"]
    else:
        # gpt-6-luna accepted every level tested, including 'minimal'.
        caps["reasoning_effort_values"] = ["minimal", "low", "medium", "high", "xhigh", "max"]
    return {
        "provider": "openai",
        "model": spec["model"],
        "base_url": RESOURCE,
        "auth_mode": "openai_oauth",
        "obo_audience": RESOURCE,
        "capabilities": caps,
        "reasoning_effort": spec["reasoning_effort"],
        "surface_persisted_reasoning": spec["surface_persisted_reasoning"],
        "replay_reasoning_to_model": spec["replay_reasoning_to_model"],
        # Preserved deliberately.
        "context_window": row.get("context_window") or 0,
        "enabled": row.get("enabled", True),
    }


async def cmd_apply(allow_residual_key: bool) -> int:
    cons = await _constraints()
    modes = set(cons.get("dynamic_auth_modes") or [])
    if "openai_oauth" not in modes:
        print(
            "REFUSED: the running console does not accept auth_mode=openai_oauth.\n"
            "The code activation has not been done. Nothing was written.\n"
            "Run the 94- activation first, then re-run this command.",
            file=sys.stderr,
        )
        return 2

    if not allow_residual_key:
        print(
            "REFUSED: this API cannot clear a stored api_key ('' and '***' both mean\n"
            "'keep the existing value'). Each row would keep its inert Switchyard key.\n"
            "That key is never read on an openai_oauth row and cannot become a fallback\n"
            "(the dispatch fails closed unconditionally), but it stays at rest.\n"
            "Re-run with --allow-residual-key to accept it, or clear the columns by a\n"
            "direct DB edit with the operator's approval.",
            file=sys.stderr,
        )
        return 2

    # Allow-list first, additively: a row cannot be saved with an audience the
    # allow-list does not contain.
    current = list(cons.get("auth_audience_allowlist") or [])
    if RESOURCE in current:
        print(f"allow-list: already contains {RESOURCE} (unchanged)")
    else:
        new = current + [RESOURCE]
        body = json.dumps({"value": ",".join(new)})
        out = await call("PUT", "/v1/api/admin/settings/model.auth_audience_allowlist", body)
        if out.get("status") != 200:
            print(f"REFUSED: allow-list write failed: HTTP {out.get('status')} {out.get('body')}",
                  file=sys.stderr)
            return 2
        print(f"allow-list: {current} -> {new}")

    rows = _by_alias(await _list())
    missing = [a for a in ALIASES if a not in rows]
    if missing:
        print(f"REFUSED: aliases missing: {missing}", file=sys.stderr)
        return 2

    failures = 0
    for alias in ALIASES:
        row = rows[alias]
        payload = _target_payload(alias, row)
        out = await call(
            "PUT", f"/v1/api/admin/model-definitions/{_definition_id(row)}", json.dumps(payload)
        )
        status = out.get("status")
        receipt = (out.get("receipt") or {}).get("receipt_id", "-")
        warning = ""
        if isinstance(out.get("body"), dict):
            warning = str(out["body"].get("registry_warning") or "")
        if status == 200 and not warning:
            print(f"{alias:14s} updated (receipt {receipt})")
        elif status == 200:
            failures += 1
            print(f"{alias:14s} WRITTEN but the live registry did NOT adopt it: {warning}",
                  file=sys.stderr)
        else:
            failures += 1
            print(f"{alias:14s} FAILED HTTP {status}: {out.get('body')}", file=sys.stderr)

    if failures:
        print(f"\n{failures} row(s) failed. Snapshot for rollback: {SNAPSHOT_DIR/'before.json'}",
              file=sys.stderr)
        return 1
    print("\nall three rows updated; run `verify` next")
    return 0


async def cmd_verify() -> int:
    rows = _by_alias(await _list())
    cons = await _constraints()
    problems: list[str] = []

    if RESOURCE not in (cons.get("auth_audience_allowlist") or []):
        problems.append(f"allow-list does not contain {RESOURCE}")

    for alias, spec in ALIASES.items():
        row = rows.get(alias)
        if not row:
            problems.append(f"{alias}: missing")
            continue
        checks = {
            "provider": (row.get("provider"), "openai"),
            "model": (row.get("model"), spec["model"]),
            "base_url": (row.get("base_url"), RESOURCE),
            "auth_mode": (row.get("auth_mode"), "openai_oauth"),
            "obo_audience": (row.get("obo_audience"), RESOURCE),
        }
        bad = [f"{k}={v[0]!r} (want {v[1]!r})" for k, v in checks.items() if v[0] != v[1]]
        if bad:
            problems.append(f"{alias}: " + "; ".join(bad))
        if _is_switchyard(row):
            problems.append(f"{alias}: still references Switchyard")
        # The bridge redacts any value under api_key, so its CONTENT is not
        # readable here -- only whether one is present. That is enough: an
        # openai_oauth row never reads api_key, and presence is expected
        # (accepted residual).
        stored_key = row.get("api_key") not in ("", None)
        if stored_key:
            print(f"{alias:14s} note: a static key is still stored, masked (inert on this mode)")

    if problems:
        print("VERIFY FAILED:")
        for p in problems:
            print(f"  {p}")
        return 1
    print("VERIFY OK: three aliases bound to direct OpenAI OAuth; no Switchyard reference")
    for alias in ALIASES:
        print(f"  {alias:14s} -> {ALIASES[alias]['model']} (effort={ALIASES[alias]['reasoning_effort']})")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("status")
    sub.add_parser("snapshot")
    a = sub.add_parser("apply")
    a.add_argument("--allow-residual-key", action="store_true")
    sub.add_parser("verify")
    args = ap.parse_args()
    if args.cmd == "status":
        return asyncio.run(cmd_status())
    if args.cmd == "snapshot":
        return asyncio.run(cmd_snapshot())
    if args.cmd == "apply":
        return asyncio.run(cmd_apply(args.allow_residual_key))
    return asyncio.run(cmd_verify())


if __name__ == "__main__":
    sys.exit(main())
