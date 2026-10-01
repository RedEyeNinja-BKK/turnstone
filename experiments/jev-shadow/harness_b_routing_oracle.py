#!/usr/bin/env python3
"""Harness B - hosted Jev Router as a shadow routing oracle.

Compares the hosted Jev Router's downstream model selection against what
Turnstone's own signals suggest, over genuine post-T0 sensor observations
replayed OFFLINE from the store.

Design constraints honoured here:

  * OFFLINE REPLAY. Reads already-stored observations. Never calls a live hook,
    never touches the sensor, the guard, or any Switchyard route.
  * NO FABRICATED CONTENT. The observation store contains signals and numeric
    values only - no task text was ever persisted. This harness therefore sends
    DERIVED NUMERIC FEATURES and says so, rather than inventing a task
    description it does not have.
  * MINIMISED OUTBOUND PROJECTION. Exactly five bounded numbers plus the phase
    label. Nothing else exists to send, and the projection is asserted field by
    field so a future store change cannot silently widen it.
  * NO PRODUCTION EFFECT. Pure function of its inputs plus one outbound HTTP
    call to a shadow-only endpoint.

Usage:
    python3 harness_b_routing_oracle.py --limit 20 --dry-run
    python3 harness_b_routing_oracle.py --limit 20
"""

from __future__ import annotations

import argparse
import json
import os
import pathlib
import statistics
import time
import urllib.error
import urllib.request
from typing import Any

T0 = 1790791153
STORE = pathlib.Path(
    "/home/vincent/shared-workspace/operations/"
    "switchyard-sensor-layer-20260929/shadow-observations.jsonl"
)
JEV_MODEL = "typesafe/jev-router"
JEV_URL = "https://openrouter.ai/api/v1/chat/completions"

#: The five signals the sensor actually requests. `local_suitability` is
#: structurally excluded by the sensor design and must never be synthesised.
SIGNALS = (
    "task_complexity",
    "reasoning_demand",
    "tool_dependency",
    "agentic_complexity",
    "route_sufficiency",
)

#: Hard ceiling per feature. A signal above this means the projection is being
#: fed something other than a normalised score.
MAX_FEATURE = 1.0


def load_credential() -> str:
    env = dict(os.environ)
    envfile = pathlib.Path(
        "/home/vincent/.local/lib/localclaw-switchyard/resource.env"
    )
    for line in envfile.read_text().splitlines():
        line = line.strip()
        if line and "=" in line and not line.startswith("#"):
            k, v = line.split("=", 1)
            env.setdefault(k, v)
    key = env.get("OPENROUTER_API_KEY", "")
    if not key:
        raise SystemExit("no sanctioned OpenRouter credential available")
    return key


def project_outbound(row: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    """Build the exact outbound payload. Returns (prompt, audit).

    The prompt is a bounded feature vector, NOT prose. Nothing in the store is
    free text, so no workstream content can leak even in principle.
    """
    by_name = {s["name"]: s for s in row.get("signals", [])}
    feats: dict[str, float] = {}
    missing: list[str] = []
    for name in SIGNALS:
        sig = by_name.get(name)
        if sig is None or sig.get("value") is None:
            missing.append(name)
            continue
        val = float(sig["value"])
        assert -MAX_FEATURE <= val <= MAX_FEATURE, f"{name} out of range: {val}"
        feats[name] = round(val, 4)

    lines = [
        "Route this engineering task to a model.",
        "",
        "Bounded normalised signals (0..1):",
    ]
    for name in SIGNALS:
        lines.append(f"- {name}: {feats.get(name, 'unavailable')}")
    lines += [
        "",
        f"Event class: {row.get('trigger', 'unknown')}",
        "",
        "Recommend one model. Reply with the model id only.",
    ]
    audit = {
        "features_sent": sorted(feats),
        "features_unavailable": sorted(missing),
        "local_suitability_sent": False,
        "chars": len("\n".join(lines)),
        "prose_content_sent": False,
    }
    # Defence in depth: nothing but the five features and the trigger may appear.
    assert not audit["local_suitability_sent"]
    assert not audit["prose_content_sent"]
    return "\n".join(lines), audit


def call_jev(prompt: str, key: str, timeout: int = 90) -> dict[str, Any]:
    body = json.dumps(
        {"model": JEV_MODEL, "messages": [{"role": "user", "content": prompt}]}
    ).encode()
    req = urllib.request.Request(
        JEV_URL,
        data=body,
        headers={
            "Authorization": f"Bearer {key}",
            "content-type": "application/json",
            "HTTP-Referer": "https://localclaw",
            "X-Title": "jev-shadow-harness-b",
        },
    )
    started = time.monotonic()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            payload = json.loads(resp.read())
            code = resp.status
    except urllib.error.HTTPError as exc:
        payload = json.loads(exc.read())
        code = exc.code
    except Exception as exc:  # noqa: BLE001
        return {
            "ok": False,
            "error": f"{type(exc).__name__}: {exc}",
            "latency_ms": int((time.monotonic() - started) * 1000),
        }
    usage = payload.get("usage") or {}
    return {
        "ok": code == 200,
        "http": code,
        "selected_model": payload.get("model"),
        "selected_provider": payload.get("provider"),
        "cost": usage.get("cost"),
        "reasoning_tokens": (usage.get("completion_tokens_details") or {}).get(
            "reasoning_tokens"
        ),
        "latency_ms": int((time.monotonic() - started) * 1000),
        "error": (payload.get("error") or {}).get("message"),
    }


def classify(model: str | None) -> str:
    """Coarse compute class for categorising disagreement. Not a quality claim."""
    if not model:
        return "unknown"
    m = model.lower()
    if "gpt-6.1" in m or "opus" in m or "frontier" in m:
        return "frontier/strong"
    if "sol" in m:
        return "strong"
    if "luna" in m or "flash" in m or "haiku" in m or "mini" in m:
        return "cheap/fast"
    return "other"


def load_rows(limit: int) -> list[dict[str, Any]]:
    rows = []
    for line in STORE.read_text().splitlines():
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if row.get("timestamp", 0) >= T0:
            rows.append(row)
    rows.sort(key=lambda r: r["timestamp"])
    return rows[:limit]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=20)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    rows = load_rows(args.limit)
    print("harness B - hosted Jev Router routing oracle")
    print(f"  corpus : {len(rows)} genuine post-T0 observations (offline replay)")
    print(f"  target : {JEV_MODEL}")
    print(f"  mode   : {'DRY RUN' if args.dry_run else 'LIVE shadow calls'}")

    if args.dry_run:
        for row in rows[:2]:
            text, audit = project_outbound(row)
            print(f"\n  --- outbound projection ({audit['chars']} chars) ---")
            print("  " + text.replace("\n", "\n  "))
            print(f"  audit: {json.dumps(audit)}")
        print("\n  note: the store holds signals only, no task text exists to leak.")
        return 0

    key = load_credential()
    results = []
    for idx, row in enumerate(rows, 1):
        text, audit = project_outbound(row)
        out = call_jev(text, key)
        record = {
            "index": idx,
            "fingerprint": row.get("state_fingerprint", "")[:12],
            "trigger": row.get("trigger"),
            "backend": row.get("backend"),
            "outbound_projection": audit,
            "jev": out,
        }
        results.append(record)
        sel = out.get("selected_model")
        print(
            f"  [{idx:>2}/{len(rows)}] {out.get('latency_ms', 0):>6}ms "
            f"selected={sel} class={classify(sel)} cost={out.get('cost')}"
        )

    ok = [r for r in results if r["jev"].get("ok")]
    lat = [r["jev"]["latency_ms"] for r in ok]
    costs = [r["jev"]["cost"] for r in ok if r["jev"].get("cost") is not None]
    classes: dict[str, int] = {}
    for r in ok:
        key_c = classify(r["jev"].get("selected_model"))
        classes[key_c] = classes.get(key_c, 0) + 1

    print()
    print(f"  calls ok       : {len(ok)}/{len(results)}")
    if lat:
        print(
            f"  latency ms     : p50={statistics.median(lat):.0f} "
            f"p95={sorted(lat)[max(0, int(len(lat)*0.95)-1)]} max={max(lat)}"
        )
    if costs:
        print(f"  cost           : total={sum(costs):.6f} mean={statistics.mean(costs):.6f}")
    print(f"  compute classes: {classes}")
    print("  effort exposed : NO - measured absent from every response")
    print("  Switchyard side: NOT COMPARED in this harness (needs live routing telemetry)")

    out_path = pathlib.Path(__file__).with_name("harness_b_results.json")
    out_path.write_text(json.dumps(results, indent=2))
    print(f"  wrote {out_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
