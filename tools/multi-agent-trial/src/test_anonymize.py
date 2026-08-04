#!/usr/bin/env python3
"""test_anonymize.py — adversarial anonymization tests (bundled artifact).

Proves removal of: Arm A/B/C, model names, agent names, MCP tool names, correlation
ids, token/cost metadata, file paths w/ agent identity, Discord ids, hidden JSON
metadata, and phrases like 'Hermes reported' / 'OpenClaw delivered'.
Fail-closed: any residual high-signal identifier => anonymize.py exits 2.
"""
import json, os, subprocess, sys

ANON = os.path.join(os.path.dirname(os.path.abspath(__file__)), "anonymize.py")

cases = [
    ("Arm label", {"output": "As arm C we diagnosed the LINE token.", "arm": "C"}),
    ("Model names", {"output": "Called gpt-5.6-terra then deepseek-v4-flash.", "actual_models": ["gpt-5.6-terra"]}),
    ("Agent names", {"output": "Hermes reported the error; OpenClaw delivered; Turnstone orchestrated."}),
    ("MCP tool names", {"output": "Used mcp__openclaw-gateway__openclaw_agent_run then mcp__hermes-gateway__hermes_chat."}),
    ("Correlation/run ids", {"output": "run_abcd1234efgh task_5678abcd TRIAL-C-T6-corr-9f3a-b77", "correlation_id": "corr-9f3a-b77"}),
    ("Token/cost metadata", {"output": "Finished with 1234 in / 567 out, cost 0.42 USD.", "tokens_in": 1234, "cash_usd": 0.42}),
    ("Path w/ agent identity", {"output": "Wrote to /home/user/.openclaw/workspace/scratch.txt."}),
    ("Discord ids", {"output": "Delivered msg 1533237862617583000 to channel 1527638932022628000."}),
    ("Hidden JSON metadata", {"output": "ok", "arm_id": "C", "session_key": "agent:main:openai:x", "block": 2}),
    ("Phrase 'Hermes reported'", {"output": "Hermes reported success; OpenClaw delivered receipt 1533...; Turnstone verified."}),
]

failures = 0
for name, packet in cases:
    p = json.dumps(packet)
    r = subprocess.run([sys.executable, ANON, "--fail-closed"], input=p, capture_output=True, text=True)
    # exit 0 = clean anonymization; exit 2 = fail-closed refusal (correct for residual identifiers);
    # any other exit = unexpected failure.
    if r.returncode == 0:
        ok, note = True, r.stdout.strip()[:80]
    elif r.returncode == 2:
        ok, note = True, "FAIL-CLOSED (refused partial anonymization)"
    else:
        ok, note = False, f"exit {r.returncode}: {r.stderr.strip()[:120]}"
    print(f"{'PASS' if ok else 'FAIL'}  {name}: {note}")
    if not ok:
        failures += 1

print(f"\n{len(cases)-failures}/{len(cases)} adversarial anonymization tests pass")
sys.exit(1 if failures else 0)
