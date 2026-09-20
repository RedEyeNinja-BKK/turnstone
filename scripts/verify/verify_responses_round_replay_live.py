#!/usr/bin/env python3
"""DECISIVE ACCEPTANCE TEST: post-compaction Responses replay on the live lane.

Runs the REAL lowering (`OpenAIResponsesProvider._convert_messages`) for the
production compaction shape, then puts the resulting Responses input array on the
wire, so the assertion is made against what the provider actually emits -- not
against a hand-built approximation.

Run it twice to compare pre-patch and candidate code WITHOUT deploying either:

    # BEFORE -- the live slot's own interpreter, code pinned to the live slot
    /opt/turnstone/runtimes/1.8.4-34fa4a70/venv/bin/python3.11 \
        scripts/verify/verify_responses_round_replay_live.py \
        --label before --expect 400 \
        --turnstone-root /opt/turnstone/runtimes/1.8.4-34fa4a70/venv/lib/python3.11/site-packages

    # AFTER -- the candidate worktree
    /opt/turnstone/fork/venv/bin/python \
        scripts/verify/verify_responses_round_replay_live.py \
        --label after --expect 200 \
        --turnstone-root /opt/turnstone/fork/tt-responses-replay-wt

`--turnstone-root` is REQUIRED and is asserted: the fork venv carries an editable
install pointing at another source tree, so an unpinned run silently exercises the
wrong code.  The arm aborts (exit 2) unless the imported package really lives under
the requested root.

Exit 0 = every case matched its expectation.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request

BASE = "http://127.0.0.1:4000/v1/responses"
MODEL = "switchyard-smart-turnstone"
MARKER = "[Conversation summary] user asked X; assistant replied Y."

# Responses-wire tool shape (flat).  The Chat-Completions nested form
# ({"type":"function","function":{...}}) is rejected 422 by the endpoint.
TOOLS = [
    {
        "type": "function",
        "name": "get_status",
        "description": "Get service status",
        "parameters": {"type": "object", "properties": {}, "required": []},
        "strict": False,
    }
]


def _tool_call(call_id: str) -> dict:
    return {
        "role": "assistant",
        "content": None,
        "tool_calls": [
            {"id": call_id, "type": "function",
             "function": {"name": "get_status", "arguments": "{}"}},
        ],
    }


# ── the production shapes ────────────────────────────────────────────────────
# 1. compaction reproducer: thinking turn -> tool activity -> mid-turn compaction
#    (non-thinking compaction lane writes the summary marker) -> resumed call.
COMPACTION = [
    {"role": "user", "content": "Check the service status."},
    {"role": "assistant", "content": "I will check."},
    {"role": "user", "content": MARKER},
    {"role": "assistant", "content": "Backend request failed before generation started."},
    _tool_call("call_1"),
    {"role": "tool", "tool_call_id": "call_1", "content": '{"state":"active"}'},
]

# 2. control: the same round, but the tool-call turn already carries reasoning.
WITH_REASONING = [
    {"role": "user", "content": MARKER},
    {
        "role": "assistant",
        "content": "calling",
        "_provider_content": [
            {"type": "reasoning", "id": "rs_native",
             "summary": [{"type": "summary_text", "text": "I should call get_status."}],
             "content": [{"type": "reasoning_text", "text": "I should call get_status."}]},
        ],
        "_producer": "openai",
        "tool_calls": [
            {"id": "call_2", "type": "function",
             "function": {"name": "get_status", "arguments": "{}"}},
        ],
    },
    {"role": "tool", "tool_call_id": "call_2", "content": '{"state":"active"}'},
]

# 3. negative control: a plain user turn -- no tool round to continue.
PLAIN = [
    {"role": "user", "content": MARKER},
    {"role": "assistant", "content": "plain answer"},
    {"role": "user", "content": "carry on"},
]

CASES = {
    "compaction_reproducer": COMPACTION,
    "existing_reasoning_control": WITH_REASONING,
    "plain_user_turn_control": PLAIN,
}


def pin_turnstone(root: str) -> None:
    """Force the requested source root and PROVE the import came from it."""
    root = os.path.abspath(root)
    sys.path.insert(0, root)
    # Drop any module already resolved from elsewhere (editable installs).
    for name in [m for m in sys.modules if m == "turnstone" or m.startswith("turnstone.")]:
        del sys.modules[name]
    import turnstone  # noqa: PLC0415

    actual = os.path.abspath(turnstone.__file__)
    if not actual.startswith(root + os.sep):
        print(f"ABORT: turnstone imported from {actual}, not from --turnstone-root {root}")
        sys.exit(2)
    print(f"turnstone resolved under requested root: {actual}")


def lower(history: list[dict]) -> tuple[str | None, list[dict]]:
    from turnstone.core.providers._openai_responses import OpenAIResponsesProvider

    provider = object.__new__(OpenAIResponsesProvider)
    return provider._convert_messages(history, replay_reasoning_to_model=True)


def post(instructions: str | None, items: list[dict]) -> tuple[int, str]:
    payload: dict = {"model": MODEL, "input": items, "tools": TOOLS,
                     "max_output_tokens": 64, "stream": False}
    if instructions:
        payload["instructions"] = instructions
    req = urllib.request.Request(
        BASE, data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=90) as r:
            body = json.loads(r.read())
            return r.status, str(body.get("status", ""))
    except urllib.error.HTTPError as e:
        raw = e.read().decode()[:400]
        try:
            raw = json.loads(raw)["error"]["message"][:110]
        except Exception:
            pass
        return e.code, raw


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--label", required=True)
    ap.add_argument("--expect", type=int, required=True,
                    help="expected HTTP status for the compaction reproducer")
    ap.add_argument("--turnstone-root", required=True,
                    help="source root the imported turnstone MUST come from")
    args = ap.parse_args()

    print(f"=== {args.label} arm ===")
    pin_turnstone(args.turnstone_root)
    print(f"{'case':28} {'synth':5} {'http':4}  status/note")
    print("-" * 78)

    failures = 0
    for name, history in CASES.items():
        try:
            instructions, items = lower(history)
        except Exception as exc:  # lowering itself must never raise
            print(f"{name:28} {'-':5} {'--':4}  LOWERING RAISED {type(exc).__name__}: {exc}")
            failures += 1
            continue

        synthesized = [i for i in items if i.get("type") == "reasoning"]
        status, note = post(instructions, items)
        expected = args.expect if name == "compaction_reproducer" else 200
        ok = "OK " if status == expected else "FAIL"
        if status != expected:
            failures += 1
        print(f"{name:28} {len(synthesized):<5} {status:<4}  {ok} {note}")
        if name == "compaction_reproducer":
            for item in synthesized:
                carriers = [p.get("type") for p in item.get("content") or []]
                print(f"{'':28} {'':5} {'':4}      synthesized id={item.get('id')!r} "
                      f"content={carriers}")

    print()
    if failures:
        print(f"RESULT: FAIL ({failures} case(s) off expectation)")
        return 1
    print("RESULT: PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
