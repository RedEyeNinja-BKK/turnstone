#!/usr/bin/env python3
"""Harness A - three-way decision-provider comparison.

Feeds IDENTICAL stored bounded states to the currently reachable decision
providers and records normalised, comparable answers.

Backends:
    span       via Switchyard, the R1 leg of switchyard-smart-aux-turnstone
    laya       direct on HTPC :8011, the R2 leg
    jev-style  local 0.8B scorer  -- OFFLINE ONLY until deployed (§ blocker)

What this harness does NOT do, deliberately:
  * it never touches the live sensor hooks
  * it never writes to the production observation store
  * it never calls Jev-Style until that service exists (see --backend)

Stored observations carry signals, not task prose, so the replayable input is the
five bounded signal values plus the trigger class. That is enough to compare
*stability and agreement* across providers; it is NOT enough to measure routing
quality, and this harness does not claim to.

Usage:
    python3 harness_a_decision_compare.py --limit 20 --dry-run
    python3 harness_a_decision_compare.py --limit 20 --backends laya,span
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
SWITCHYARD = "http://127.0.0.1:4000"
LAYA = "http://100.105.20.36:8011/v1/systemone"
AUX_LANE = "switchyard-smart-aux-turnstone"

SIGNALS = (
    "task_complexity",
    "reasoning_demand",
    "tool_dependency",
    "agentic_complexity",
    "route_sufficiency",
)
LEVELS = ("trivial", "simple", "moderate", "complex", "very complex")
QUESTIONS = {
    "task_complexity": "Rate the complexity of this engineering task.",
    "reasoning_demand": "Rate how much deep reasoning this task demands.",
    "tool_dependency": "Rate how much this task depends on tool use.",
    "agentic_complexity": "Rate how agentic and multi-step this task is.",
    "route_sufficiency": "Rate how well the currently available routes suit this task.",
}


def laya_key() -> str:
    env = dict(os.environ)
    for line in pathlib.Path(
        "/home/vincent/.local/lib/localclaw-switchyard/htpc.env"
    ).read_text().splitlines():
        line = line.strip()
        if line and "=" in line and not line.startswith("#"):
            k, v = line.split("=", 1)
            env.setdefault(k, v)
    key = env.get("LOCALCLAW_LAYA_API_KEY", "")
    if not key:
        raise SystemExit("no Laya credential")
    return key


def span_key() -> str:
    env = dict(os.environ)
    for line in pathlib.Path(
        "/home/vincent/.local/lib/localclaw-switchyard/resource.env"
    ).read_text().splitlines():
        line = line.strip()
        if line and "=" in line and not line.startswith("#"):
            k, v = line.split("=", 1)
            env.setdefault(k, v)
    key = env.get("OPENROUTER_API_KEY", "")
    if not key:
        raise SystemExit("no Span credential")
    return key


def bounded_state(state: dict[str, Any]) -> dict[str, Any]:
    return {
        "objective": state.get("objective", "")[:400],
        "phase": state.get("phase", "unknown"),
        "blockers": state.get("blockers", "")[:400],
        "history": state.get("history", "")[:400],
    }


def build_laya_body(state: dict[str, Any]) -> dict[str, Any]:
    """Laya accepts graded ``score`` questions with level descriptions.

    Verified live: Laya accepts this body verbatim and returns score + legend +
    probabilities + confidence for all five signals.
    """
    return {
        "state": bounded_state(state),
        "questions": {
            name: {"type": "score", "instructions": QUESTIONS[name],
                   "criteria": list(LEVELS)}
            for name in SIGNALS
        },
    }


def build_span_body(state: dict[str, Any]) -> dict[str, Any]:
    """Span accepts ONLY ``noul`` questions with plain-string criteria.

    Measured, not assumed: sending a graded ``score`` question to Span returns
    HTTP 400 "Respan only accepts noul questions whose instructions and criteria
    are plain strings". Span answers with a single ``noul`` probability in 0..1,
    so its scale is continuous where Laya's is ordinal-graded. The comparison
    normalises both onto 0..1 and records which provider produced which scale.
    """
    return {
        "model": AUX_LANE,
        "state": bounded_state(state),
        "questions": {
            name: {
                "type": "noul",
                "instructions": f"{QUESTIONS[name]} Answer yes if it applies.",
                "criteria_true": "yes",
                "criteria_false": "no",
            }
            for name in SIGNALS
        },
    }


def post(url: str, body: dict[str, Any], headers: dict[str, str],
         timeout: int = 120) -> tuple[int, dict[str, Any], int]:
    req = urllib.request.Request(
        url, data=json.dumps(body).encode(),
        headers={"content-type": "application/json", **headers},
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
        return 0, {"error": f"{type(exc).__name__}: {exc}"}, int(
            (time.monotonic() - started) * 1000
        )
    return code, payload, int((time.monotonic() - started) * 1000)


def normalise_laya(payload: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for name, ans in (payload.get("answers") or {}).items():
        if not ans.get("available", True) and ans.get("reason"):
            out[name] = {"available": False, "value": None,
                         "reason": ans.get("reason")}
            continue
        out[name] = {
            "available": True,
            "value": ans.get("score"),
            "confidence": ans.get("confidence"),
            "answer_confidence": ans.get("answer_confidence"),
            "probabilities": ans.get("probabilities"),
        }
    return out


def normalise_span(payload: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for name, ans in (payload.get("answers") or {}).items():
        if ans.get("type") == "noul":
            out[name] = {"available": True, "value": ans.get("noul"),
                         "confidence": None, "probabilities": None,
                         "scale": "continuous 0..1"}
        else:
            out[name] = {"available": False, "value": None, "reason": "unexpected type"}
    return out


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
    ap.add_argument("--backends", default="laya,span")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    backends = [b.strip() for b in args.backends.split(",") if b.strip()]
    rows = load_rows(args.limit)

    print("harness A - decision provider comparison")
    print(f"  corpus    : {len(rows)} genuine post-T0 observations")
    print(f"  backends  : {', '.join(backends)}")
    print(f"  signals   : {len(SIGNALS)} ({', '.join(SIGNALS)})")
    print("  note      : local_suitability excluded by sensor contract, never inferred")
    if args.dry_run:
        st = {"objective": "<from store>", "phase": "unknown"}
        lb, sb = build_laya_body(st), build_span_body(st)
        print(f"  laya body : {len(lb['questions'])} score questions with graded criteria")
        print(f"  span body : {len(sb['questions'])} noul questions with string criteria")
        print("  (the two providers accept DIFFERENT question types - measured)")
        return 0

    results = []
    for idx, row in enumerate(rows, 1):
        # reconstruct a comparable bounded state from what the store holds
        sig = {s["name"]: s for s in row.get("signals", [])}
        state = {
            "objective": "bounded engineering state "
                         f"(complexity={sig.get('task_complexity', {}).get('value')}, "
                         f"reasoning={sig.get('reasoning_demand', {}).get('value')}, "
                         f"tools={sig.get('tool_dependency', {}).get('value')})",
            "phase": row.get("trigger", "unknown"),
            "blockers": "",
            "history": "",
        }
        record: dict[str, Any] = {
            "index": idx,
            "fingerprint": row.get("state_fingerprint", "")[:12],
            "stored_backend": row.get("backend"),
            "backends": {},
        }
        if "laya" in backends:
            code, payload, ms = post(
                LAYA, build_laya_body(state), {"Authorization": f"Bearer {laya_key()}"}
            )
            record["backends"]["laya"] = {
                "http": code,
                "latency_ms": ms,
                "answers": normalise_laya(payload) if code == 200 else None,
                "error": (payload.get("detail") or payload.get("error")),
            }
        if "span" in backends:
            code, payload, ms = post(
                f"{SWITCHYARD}/v1/decisions",
                build_span_body(state),
                {"Authorization": f"Bearer {span_key()}"},
            )
            record["backends"]["span"] = {
                "http": code, "latency_ms": ms,
                "answers": normalise_span(payload) if code == 200 else None,
                "error": ((payload.get("error") or {}).get("message")),
                "served_model": payload.get("model"),
                "scale": "continuous 0..1 (noul)",
            }
        results.append(record)
        print(f"  [{idx:>2}/{len(rows)}] " + "  ".join(
            f"{b}={record['backends'].get(b, {}).get('http', '-')}"
            f"/{record['backends'].get(b, {}).get('latency_ms', '-')}ms"
            for b in backends
        ))

    out = pathlib.Path(__file__).with_name("harness_a_results.json")
    out.write_text(json.dumps(results, indent=2))

    print()
    for b in backends:
        codes = [r["backends"].get(b, {}).get("http") for r in results]
        lat = [r["backends"][b]["latency_ms"] for r in results
               if r["backends"].get(b, {}).get("http") == 200]
        ok = sum(1 for c in codes if c == 200)
        print(f"  {b:<6} ok {ok}/{len(results)}", end="")
        if lat:
            print(f"  p50={statistics.median(lat):.0f}ms max={max(lat)}ms")
        else:
            print("  (no successful calls)")
    print(f"  wrote {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
