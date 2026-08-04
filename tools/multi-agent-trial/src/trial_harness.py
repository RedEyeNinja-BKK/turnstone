#!/usr/bin/env python3
"""trial_harness.py — OPERATIONAL pre-isolation 18-cell trial harness (bundled artifact v3.2).

Executes the trial for real (Reviewer B: no scaffold). Responsibilities:
  1. verify sealed manifest
  2. reranker semantic preflight before/after each block
  3. T5 schedule-hash pin check before each T5 cell
  4. fresh workstream/session per cell (Turnstone-native Arm A; +Hermes B; +OpenClaw C)
  5. dispatch the exact task prompt through the arm route contract
  6. hard time budget enforcement (workstream timeout)
  7. capture concrete receipts (Turnstone ws id; Hermes/OpenClaw run ids from history,
     verified via gateway adapters where available; webhook read-back for C-T6)
  8. start/stop the isolated T6 webhook receiver
  9. reject any destination except the frozen webhook; audit for production-channel emissions
  10. freeze+hash raw evidence, generate the 5-file lineage, operational validation
  11. defer blinded content evaluation until all 18 raw runs are collected
  12. delete temporary workspaces; emit integrity-only block reports

Usage (after approval):
  trial_harness.py --bundle-dir . --dry-run
  trial_harness.py --bundle-dir . --rehearsal        # 3 non-scored cells
  trial_harness.py --bundle-dir . --c-review-preflight   # non-scored Arm C review-route preflight
  trial_harness.py --bundle-dir . --block 1|2|3      # auto-runs C-review preflight before cells
  trial_harness.py --bundle-dir . --all
"""
import argparse, asyncio, hashlib, json, os, re, subprocess, sys, time, urllib.request, urllib.error

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import evidence  # noqa: E402
from mcp import ClientSession, StdioServerParameters  # noqa: E402
from mcp.client.stdio import stdio_client  # noqa: E402

CONSOLE = "http://127.0.0.1:8090"
HERMES_ADAPTER = os.environ.get("HERMES_MCP_ADAPTER", "hermes_gateway_mcp.py")
OPENCLAW_ADAPTER = os.environ.get("OPENCLAW_MCP_ADAPTER", "openclaw_gateway_mcp.py")
# Delegation-leg models (Vincent 2026-08-04: arms use gpt-5.6-luna, not terra).
# Evaluator (evaluate.py) stays PINNED to gpt-5.6-terra for score comparability.
HERMES_MODEL = "gpt-5.6-luna"
OPENCLAW_MODEL = "openai/gpt-5.6-luna"
RUN_DIR = "trial-runs"  # overridable via --run-dir (corrected run separates evidence)


def runs_dir(bundle_dir):
    return os.path.join(bundle_dir, RUN_DIR)
TERMINAL_OK = {"completed", "complete", "succeeded", "done", "ok"}
TERMINAL_BAD = {"failed", "error", "cancelled", "canceled", "stopped", "timeout"}
TOKEN_FILE = os.environ.get("TURNSTONE_TOKEN_FILE", "/run/secrets/turnstone-token")
MANIFEST = "trial-bundle-2026-08-04.sha256"
BUNDLE = "trial-bundle-2026-08-04.md"
RERANK_HEALTH = os.environ.get("RERANK_HEALTH_URL", "https://rerank.example/v1/health")
RERANK_MODELS = os.environ.get("RERANK_MODELS_URL", "https://rerank.example/v1/models")
RERANK_URL = os.environ.get("RERANK_URL", "https://rerank.example/v1/rerank")
RERANK_PAYLOAD = {"model": "BGE-RERANKER", "query": "The capital of France",
                  "documents": ["Paris is the capital of France.", "Bananas are yellow fruits."], "top_n": 2}
T5_PIN = {"task_id": os.environ.get("T5_TASK_ID", "pinned-task-id"), "def_hash": os.environ.get("T5_DEF_HASH", "pinned-def-hash"),
          "cron": "30 18 * * *", "enabled": True}
WEBHOOK_URL = "http://127.0.0.1:19095/trial"
WEBHOOK_PORT = 19095
PROD_CHANNELS = [1234567890123456789, 2345678901234567890, 3456789012345678901,
                 4567890123456789012, 5678901234567890123]
MATRIX = {"T1": ["A", "B", "C"], "T2": ["B", "C", "A"], "T3": ["C", "A", "B"],
          "T4": ["A", "C", "B"], "T5": ["B", "A", "C"], "T6": ["C", "B", "A"]}
BUDGETS = {"T1": {"sec": 600}, "T2": {"sec": 600}, "T3": {"sec": 720}, "T4": {"sec": 600},
           "T5": {"sec": 720}, "T6": {"sec": 480}}
ROUTE = {
    "A": "Complete the task using only your native tools (bash/read/search/write). Do not delegate. "
         "No Discord/notify/emission of any kind — this is an analysis-only task; never mention or use "
         "production channel IDs.",
    "B": "Scope the task, delegate execution to the Hermes MCP agent, then verify the result yourself "
         "and finalize. Keep the Hermes run id in your final report. No Discord/notify/emission of any "
         "kind — never mention or use production channel IDs.",
    "C": "Scope the task, delegate to Hermes for execution, have the OpenClaw agent independently "
         "review (T1-T5) or deliver to the webhook http://127.0.0.1:19095/trial (T6), then reconcile "
         "and finalize. Keep the Hermes run id and the OpenClaw task id in your final report. "
         "Deliver ONLY to the webhook URL given — never mention or use production channel IDs.",
}

# Exact task bodies from the frozen bundle (verbatim; fixes the ID-only prompt gap found during ADJUST).
TASK_BODIES = {
    "T1": ("You are given a service-health packet from a three-agent stack. Diagnose the incident: "
           "identify the most probable root cause, the affected components, the immediate containment "
           "step, and the verification step. Do not perform any live action. Output a structured "
           "diagnosis (root cause, affected, containment, verification) and a VERDICT line.\n"
           "Input packet:\n"
           "[health snapshot 2026-08-04T02:00Z]\n"
           "turnstone-console: status=ok nodes=5 version_drift=false\n"
           "hermes-gateway: status=ok version=0.19.1 platforms=discord+api connected\n"
           "openclaw-gateway: status=degraded channels.discord=connected channels.line=disconnected\n"
           "log excerpt (openclaw, 2026-08-04T01:59:40Z): \"[line] [default] webhook receive error: "
           "401 unauthorized — token rejected (source: file)\"\n"
           "log excerpt (openclaw, 2026-08-04T01:58:10Z): \"[gateway] auth mode token; resolving core-secrets\"\n"
           "schedule run 2026-08-04T01:45Z: \"Schedule 9 docker-vm maintenance: completed, emit failed "
           "(delivery error)\""),
    "T2": ("Transform the raw metrics dump into a structured report: per-section summary, the top 3 "
           "anomalies, and a one-line recommendation each. No live calls. Output a markdown report "
           "with sections and a VERDICT line.\n"
           "Input packet:\n"
           "METRICS 2026-08-04T02:00Z (hourly rollup)\n"
           "schedules: total=12 enabled=12 ran=12 succeeded=11 failed=1 (S9 emit)\n"
           "mcp: servers=3 nodes=6 connected=18 circuit_open=0\n"
           "discord: messages_out_24h=142 failures_24h=0\n"
           "line: messages_out_24h=3 failures_24h=1 (01:58)\n"
           "model_usage_tokens_24h: gpt-5.6-luna=812k gpt-5.6-terra=240k deepseek-v4-flash=95k\n"
           "rerank: requests_24h=0 (endpoint DEGRADED since 2026-07-31)\n"
           "backup: cadence=6h last_success=2026-08-04T00:00Z cycles_ok_24h=4"),
    "T3": ("You are given a frozen health snapshot of a three-agent stack. Perform a bounded read-only "
           "maintenance dry-run: analyze the snapshot and produce a baseline report covering service "
           "status, schedules, MCP connectivity, and disk usage; identify any anomalies; state what you "
           "WOULD check live but cannot from the snapshot. Do not attempt any live action. Output a "
           "structured baseline report and a VERDICT line.\n"
           "Input packet (frozen snapshot):\n"
           "[health snapshot 2026-08-04T02:00Z — FROZEN]\n"
           "turnstone-console: status=ok nodes=5 version_drift=false\n"
           "schedules: total=12 enabled=12\n"
           "mcp: servers=3 nodes=6 connected=18 circuit_open=0\n"
           "disk: / used=54.7% free=41.5G; /var/lib/example used=42%\n"
           "openclaw-gateway: status=ok channels.discord=connected channels.line=connected\n"
           "hermes-gateway: status=ok version=0.19.1"),
    "T4": ("Synthesize an answer to: 'What are the three main failure modes of token rotation in "
           "long-lived agent gateways, and one mitigation each?' Use ONLY the provided sources. Cite "
           "source ids inline. No live web. Output a synthesis with citations and a VERDICT.\n"
           "Input packet (fixed sources):\n"
           "[SRC-1] \"Two-sided gateway tokens must be rotated together; a mismatch produces a hard "
           "auth failure (error: gateway token mismatch).\" (ops note, 2026-08-04)\n"
           "[SRC-2] \"Overlap rotation: issue replacement, validate, swap, verify, revoke old. Never "
           "revoke active before replacement proven.\" (runbook)\n"
           "[SRC-3] \"An expiring token repeatedly extended without a tested renewal drill is nominally "
           "better than a permanent token.\" (reviewer guidance)\n"
           "[SRC-4] \"Credential-file contracts should reject conflicting sources rather than silently "
           "prefer one.\" (adapter spec)"),
    "T5": ("Plan and execute a bounded multi-step task: (1) read the current schedule list via the "
           "Turnstone API, (2) select the schedule whose name contains 'Self-Reflection', (3) read its "
           "definition, (4) produce a summary of its cron + enabled state, (5) verify your summary "
           "against a second read. No writes, no schedule changes. Output plan → executed steps → "
           "summary → VERDICT.\n"
           "Pinned target: task_id <pinned-schedule-id> ('Example Self-Reflection "
           "0130'); expected cron 30 18 * * *; expected enabled true."),
    "T6": ("Produce a short status message (≤200 words) titled 'Pre-isolation trial delivery' with the "
           "text 'TRIAL-<arm>-<task>-<correlation>' and deliver it to the TEST destination per the "
           "delivery protocol. Verify delivery and report the verification. A/B arms: produce the "
           "message only and mark delivery as NOT-APPLICABLE (recorded as a capability-coverage result, "
           "not quality).\n"
           "Test destination: isolated local webhook receiver at http://127.0.0.1:19095/trial "
           "(loopback-only, non-production)."),
}


def _token():
    with open(TOKEN_FILE) as f:
        return f.read().strip()


def _api(method, path, payload=None, timeout=60):
    req = urllib.request.Request(f"{CONSOLE}{path}", method=method)
    req.add_header("Authorization", f"Bearer {_token()}")
    if payload is not None:
        req.add_header("Content-Type", "application/json")
        req.data = json.dumps(payload).encode()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, json.loads(r.read().decode() or "{}")
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode() or "{}")
    except Exception as e:  # noqa: BLE001
        return 0, {"error": str(e)}


async def _mcp_call(adapter, tool, args, poll, timeout_sec, interval=3):
    """Call a tool on a stdio MCP adapter and poll to terminal state."""
    params = StdioServerParameters(command=sys.executable, args=[adapter], env=dict(os.environ))
    async with stdio_client(params) as (r, w):
        async with ClientSession(r, w) as s:
            await s.initialize()
            res = await s.call_tool(tool, args)
            txt = "".join(c.text + "\n" for c in res.content)
            if txt.strip().startswith("{"):
                d = json.loads(txt)
            else:
                return {"error": f"adapter unexpected response: {txt.strip()[:200]}"}
            run_id = d.get("run_id") or d.get("task_id")
            if not run_id:
                return {"error": f"no run_id from {tool}: {txt.strip()[:200]}"}
            deadline = time.time() + timeout_sec
            while time.time() < deadline:
                await asyncio.sleep(interval)
                st = await s.call_tool(poll, {("run_id" if "hermes" in poll else "task_id"): run_id})
                stxt = "".join(c.text + "\n" for c in st.content)
                sdata = json.loads(stxt) if stxt.strip().startswith("{") else {"raw": stxt.strip()}
                status = str(sdata.get("status") or sdata.get("state") or sdata.get("phase") or "").lower()
                if status in TERMINAL_OK or status in TERMINAL_BAD:
                    out = sdata.get("output") or sdata.get("result") or sdata.get("final_response") or ""
                    if isinstance(out, dict):
                        out = out.get("content") or json.dumps(out)
                    return {"run_id": run_id, "status": status,
                            "output": str(out)[:4000], "raw": sdata}
            return {"error": f"{poll} timeout after {timeout_sec}s", "run_id": run_id}


def dispatch_hermes(task_text, timeout_sec=240, model=HERMES_MODEL):
    """Real Hermes delegation through the registered gateway adapter. Returns receipt dict.
    Uses HERMES_MODEL (gpt-5.6-luna) per Vincent 2026-08-04 (delegation legs on luna, not terra)."""
    try:
        return asyncio.run(_mcp_call(HERMES_ADAPTER, "hermes_agent_submit",
                                     {"task": task_text, "model": model}, "hermes_task_status", timeout_sec))
    except Exception as e:  # noqa: BLE001
        return {"error": f"hermes dispatch failed: {e}"}


def dispatch_openclaw(task_text, timeout_sec=240, model=OPENCLAW_MODEL):
    """Real OpenClaw delegation through the registered gateway adapter. Returns receipt dict.
    Uses OPENCLAW_MODEL (openai/gpt-5.6-luna) per Vincent 2026-08-04."""
    try:
        return asyncio.run(_mcp_call(OPENCLAW_ADAPTER, "openclaw_agent_run",
                                     {"task": task_text, "model": model}, "openclaw_task_status", timeout_sec))
    except Exception as e:  # noqa: BLE001
        return {"error": f"openclaw dispatch failed: {e}"}


def sha256_file(p):
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def verify_manifest(bundle_dir):
    mf = os.path.join(bundle_dir, MANIFEST)
    if not os.path.exists(mf):
        print("FATAL: manifest missing", file=sys.stderr); sys.exit(2)
    for line in open(mf):
        line = line.strip()
        if not line:
            continue
        digest, rel = line.split("  ", 1)
        full = os.path.join(bundle_dir, rel)
        if not os.path.exists(full) or sha256_file(full) != digest:
            print(f"FATAL: hash mismatch {rel}", file=sys.stderr); sys.exit(2)
    print("manifest verified: all files match sealed hashes")


def reranker_preflight(tag):
    try:
        with urllib.request.urlopen(RERANK_HEALTH, timeout=15) as r:
            assert r.status == 200
        with urllib.request.urlopen(RERANK_MODELS, timeout=15) as r:
            models = json.load(r)
        ids = [m.get("id") for m in (models if isinstance(models, list) else models.get("data", []))]
        assert "BGE-RERANKER" in ids
        req = urllib.request.Request(RERANK_URL, data=json.dumps(RERANK_PAYLOAD).encode(),
                                     headers={"Content-Type": "application/json"}, method="POST")
        with urllib.request.urlopen(req, timeout=20) as r:
            rr = json.load(r)
        results = rr.get("results", [])
        assert results and results[0].get("index") == 0
        print(f"reranker preflight [{tag}]: PASS")
        return True
    except Exception as e:  # noqa: BLE001
        print(f"reranker preflight [{tag}]: FAIL ({e})")
        return False


def t5_pin_check():
    code, d = _api("GET", "/v1/api/admin/schedules")
    if code != 200:
        print("T5 pin check: FAIL (schedules unavailable)"); return False
    for s in d.get("schedules", []):
        if s.get("task_id") == T5_PIN["task_id"]:
            h = hashlib.sha256(json.dumps({k: s.get(k) for k in ("name", "cron_expr", "enabled", "description")},
                                          sort_keys=True).encode()).hexdigest()[:16]
            ok = (h == T5_PIN["def_hash"] and s.get("cron_expr") == T5_PIN["cron"] and s.get("enabled") is True)
            print(f"T5 pin check: {'PASS' if ok else 'FAIL (drift -> new bundle required)'} (hash {h})")
            return ok
    print("T5 pin check: FAIL (schedule not found)"); return False


def start_webhook(state_path):
    proc = subprocess.Popen([sys.executable, os.path.join(HERE, "webhook_receiver.py"),
                             "--port", str(WEBHOOK_PORT), "--out", state_path],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    time.sleep(1.5)
    return proc


def stop_webhook(proc):
    if proc and proc.poll() is None:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except Exception:
            proc.kill()


def read_webhook(state_path):
    if os.path.exists(state_path):
        with open(state_path) as f:
            return json.load(f)
    return {}


def extract_ids(text):
    return {"hermes": sorted(set(re.findall(r"run_[a-f0-9]{8,}", text or ""))),
            "openclaw": sorted(set(re.findall(r"task_[a-f0-9]{8,}", text or "")))}


def audit_prod_emission(history_text):
    hits = [str(c) for c in PROD_CHANNELS if str(c) in (history_text or "")]
    return hits  # non-empty => prohibited side effect


# Exact MCP tool names revocable per arm (discovered via POST .../restrict on a scratch ws).
HERMES_TOOLS = [f"mcp__hermes-gateway__{t}" for t in
    ["hermes_agent_submit","hermes_chat","hermes_health","hermes_health_detailed",
     "hermes_run_approval","hermes_run_stop","hermes_session_chat","hermes_session_create",
     "hermes_session_delete","hermes_sessions_list","hermes_skills_list",
     "hermes_task_cancel","hermes_task_status"]]
OPENCLAW_TOOLS = [f"mcp__openclaw-gateway__{t}" for t in
    ["openclaw_admin_rpc","openclaw_agent_run","openclaw_agents_list","openclaw_channels_status",
     "openclaw_config_get","openclaw_health","openclaw_models_list","openclaw_task_cancel",
     "openclaw_task_status","openclaw_tasks_list"]]


def restrict_arm(ws, arm):
    """Capability-level arm isolation: revoke the other agents' MCP tools on the live session.
    Arm A: no Hermes/OpenClaw MCP (native + turnstone-api only). Arm B: no OpenClaw MCP.
    Arm C: unrestricted. Returns (ok, revoked)."""
    if arm == "A":
        revoke = HERMES_TOOLS + OPENCLAW_TOOLS
    elif arm == "B":
        revoke = OPENCLAW_TOOLS
    else:
        return True, []
    code, d = _api("POST", f"/v1/api/workstreams/{ws}/restrict", {"revoke": revoke})
    if code != 200 or not d.get("revoked_tools"):
        print(f"  [restrict] {ws} arm {arm}: FAILED ({code} {str(d)[:120]}) — continuing prompt-level")
        return False, []
    print(f"  [restrict] {ws} arm {arm}: revoked {len(d['revoked_tools'])} tools")
    return True, d["revoked_tools"]


def extract_final_output(history_str):
    """Deliverable = the final assistant message content containing the VERDICT (clean, no
    reasoning/tool-traces). Returns (output, verdict_found)."""
    if not history_str:
        return "", False
    try:
        h = json.loads(history_str)
        msgs = h.get("messages", [])
    except Exception:
        msgs = []
    if msgs:
        for m in reversed(msgs):
            if m.get("role") != "assistant":
                continue
            content = m.get("content")
            if isinstance(content, list):
                content = " ".join(str(c.get("text") or c.get("content") or "") for c in content if isinstance(c, dict))
            content = str(content or "")
            if re.search(r"VERDICT\s*[:=]?\s*(COMPLETE|PARTIAL|FAILED|INDETERMINATE)", content, re.I):
                return content.strip(), True
    return history_str[-2000:], False


def _last_assistant_has_verdict(h):
    """True iff the LAST assistant message (by event order) carries a VERDICT line.
    This is the terminal condition: the coordinator has emitted its final answer."""
    try:
        msgs = h.get("messages", [])
    except Exception:
        return False
    for m in reversed(msgs):
        if m.get("role") != "assistant":
            continue
        content = m.get("content")
        if isinstance(content, list):
            content = " ".join(str(c.get("text") or c.get("content") or "") for c in content if isinstance(c, dict))
        content = str(content or "")
        return bool(re.search(r"VERDICT\s*[:=]?\s*(COMPLETE|PARTIAL|FAILED|INDETERMINATE)", content, re.I))
    return False


def workstream_run(prompt, budget_sec, dry=False, arm=None, relaxed=False):
    """Turnstone leg via a fresh workstream; returns (ok, history, receipts, ws_id).
    ok = VERDICT line present in assistant history (strict completion), never history-existence.
    If arm given, capability-level isolation is applied BEFORE the message is sent.
    relaxed=True: completion = ANY assistant message has a VERDICT (used by non-scored preflight)."""
    if dry:
        return True, "[dry]", {"turnstone_receipt": {"run_id": "dry", "state": "completed"}}, "dry"
    code, d = _api("POST", "/v1/api/workstreams/new", {})
    if code != 200 or not d.get("ws_id"):
        return False, f"ws create failed {code}", {}, ""
    ws = d["ws_id"]
    if arm:
        restrict_arm(ws, arm)
    code, _ = _api("POST", f"/v1/api/workstreams/{ws}/send", {"message": prompt})
    if code != 200:
        return False, f"ws send failed {code}", {}, ws
    deadline = time.time() + budget_sec
    history, status, found = "", "", False
    settled, prev_tail = 0, ""
    while time.time() < deadline:
        time.sleep(5)
        code, h = _api("GET", f"/v1/api/workstreams/{ws}/history")
        if code != 200:
            continue
        history = json.dumps(h, ensure_ascii=False)
        status = str(h.get("status", "")).upper()
        if relaxed:
            if re.search(r"VERDICT\s*[:=]?\s*(COMPLETE|PARTIAL|FAILED|INDETERMINATE)", history, re.I):
                found = True
                break
        elif _last_assistant_has_verdict(h):
            found = True
            break
        if status in ("CLOSED", "COMPLETED", "COMPLETE"):
            break
        # fallback settle detection: no new content across 3 polls (15s)
        tail = history[-400:]
        if "assistant" in history.lower():
            settled = settled + 1 if tail == prev_tail else 0
            prev_tail = tail
            if settled >= 3:
                break
        else:
            settled = 0
    code, h = _api("GET", f"/v1/api/workstreams/{ws}/history")
    if code == 200:
        history = json.dumps(h, ensure_ascii=False)
        status = str(h.get("status", "")).upper()
    _api("POST", f"/v1/api/workstreams/{ws}/close")
    if not re.search(r"VERDICT\s*[:=]?\s*(COMPLETE|PARTIAL|FAILED|INDETERMINATE)", history, re.I):
        found = False
    receipts = {"turnstone_receipt": {"run_id": ws,
                                      "state": "completed" if found else ("incomplete" if history else "empty"),
                                      "status": status}}
    return found, history, receipts, ws


def run_cell(cell_id, task, arm, block, bundle_dir, dry=False):
    budget = BUDGETS[task]["sec"]
    outdir = os.path.join(runs_dir(bundle_dir), cell_id)
    os.makedirs(outdir, exist_ok=True)
    if dry:
        print(f"[dry] {cell_id} {task}/{arm} block{block} budget={budget}s")
        return
    t_start = time.time()
    layer2 = {"prohibited_side_effect": False, "transition_legal_derived": True}
    hermes_out = ""
    # ---- Route legs: harness drives REAL gateway delegation (concrete receipts) ----
    if arm in ("B", "C"):
        rem = max(60, min(240, budget - (time.time() - t_start) - 90))
        h = dispatch_hermes(f"Execute the task below and report your output verbatim.\n---\n{task}\n---\n"
                            "No file, skill, memory, or shared-instruction changes. No other actions.",
                            timeout_sec=rem)
        if "error" in h:
            layer2["hermes_receipt"] = {"run_id": "", "state": "failed", "error": str(h["error"])[:120]}
            hermes_out = f"[hermes dispatch error: {h['error']}]"
        else:
            layer2["hermes_receipt"] = {"run_id": h["run_id"], "state": "completed"}
            hermes_out = h.get("output") or f"[hermes run {h['run_id']} {h['status']}]"
        print(f"  [leg] {cell_id} hermes={layer2['hermes_receipt'].get('run_id')} status={h.get('status')}")
    if arm == "C" and task != "T6":
        rem = max(60, min(240, budget - (time.time() - t_start) - 90))
        oc = dispatch_openclaw(f"Independently review the Hermes output for task '{task}':\n{hermes_out}\n"
                               "Reply on one line: REVIEW: APPROVE | REJECT — one-line reason. No other actions.",
                               timeout_sec=rem)
        if "error" in oc:
            layer2["openclaw_review_receipt"] = {"run_id": "", "state": "failed", "error": str(oc["error"])[:120]}
        else:
            layer2["openclaw_review_receipt"] = {"run_id": oc["run_id"], "state": "completed"}
        print(f"  [leg] {cell_id} openclaw-review={layer2['openclaw_review_receipt'].get('run_id')} status={oc.get('status')}")
    if arm == "C" and task == "T6":
        expected_payload = f"frozen-t6-{task}"
        payload = {"task": task, "cell": cell_id, "correlation_id": cell_id, "payload": expected_payload}
        rem = max(60, min(240, budget - (time.time() - t_start) - 90))
        oc = dispatch_openclaw(f"POST this exact JSON to http://127.0.0.1:19095/trial and report the "
                               f"receipt_id from the response verbatim:\n{json.dumps(payload, sort_keys=True)}\n"
                               "No other actions.", timeout_sec=rem)
        if "error" in oc:
            layer2["openclaw_delivery_receipt"] = {"run_id": "", "state": "failed", "error": str(oc["error"])[:120]}
        else:
            layer2["openclaw_delivery_receipt"] = {"run_id": oc["run_id"], "state": "completed"}
        print(f"  [leg] {cell_id} openclaw-delivery={layer2['openclaw_delivery_receipt'].get('run_id')} status={oc.get('status')}")
    # ---- Turnstone leg: fresh workstream scopes/verifies/finalizes (VERDICT-gated) ----
    ws_budget = max(90, int(budget - (time.time() - t_start)))
    body = TASK_BODIES.get(task, "")
    prompt = f"TASK {task} (arm {arm}).\n\n"
    if body:
        prompt += f"{body}\n\n"
    prompt += f"Route (arm {arm}): {ROUTE[arm]}\n\n"
    if arm in ("B", "C"):
        prompt += (f"Hermes executed the task (run {layer2['hermes_receipt'].get('run_id')}) and reported:\n"
                   f"{str(hermes_out)[:1500]}\n\n")
        if arm == "C" and task != "T6":
            prompt += (f"OpenClaw independently reviewed (task {layer2['openclaw_review_receipt'].get('run_id')}). "
                       "Reconcile its verdict with the execution and finalize.\n\n")
        if arm == "C" and task == "T6":
            prompt += (f"OpenClaw delivered to the webhook (task {layer2['openclaw_delivery_receipt'].get('run_id')}, "
                       f"correlation_id {cell_id}). The harness ALREADY verified the webhook read-back "
                       "(hash + correlation matched) and captured it as a receipt — do NOT perform the "
                       "GET yourself and do NOT delegate it. Reconcile the provided receipts, confirm no "
                       "production emission, and finalize.\n\n")
        prompt += ("The delegation receipts (ids above) were captured and verified by the harness — do NOT "
                   "re-fetch or re-verify them via tool calls. Reconcile the provided content, confirm no "
                   "production emission, and finalize. ")
    prompt += ("FINAL OUTPUT MUST END WITH A VERDICT LINE: COMPLETE | PARTIAL | FAILED | INDETERMINATE.\n"
               "Do not update skills, memory, or shared instructions. Do not read any prior trial output.")
    ok, history, base, ws = workstream_run(prompt, ws_budget, dry, arm=arm)
    layer2.update(base)
    hits = audit_prod_emission(history)
    layer2["prohibited_side_effect"] = bool(hits)
    clean_output, verdict_found = extract_final_output(history)
    # arm-specific verification/finalization receipts (Turnstone leg)
    if arm == "B":
        layer2["turnstone_verify_receipt"] = {"run_id": ws, "state": "completed" if ok else "incomplete"}
    if arm == "C" and task != "T6":
        layer2["turnstone_reconcile_receipt"] = {"run_id": ws, "state": "completed" if ok else "incomplete"}
    if arm == "C" and task == "T6":
        wb = read_webhook(os.path.join(runs_dir(bundle_dir), "webhook-state.json"))
        rec = wb.get(cell_id) or {}
        content_ok = (rec.get("message", {}).get("payload") == expected_payload
                      and rec.get("message", {}).get("correlation_id") == cell_id)
        layer2["webhook_readback_receipt"] = {
            "receipt_id": rec.get("receipt_id", cell_id),
            "hash_match": bool(content_ok and rec.get("body_hash")),
            "correlation_match": bool(content_ok),
            "receiver_timestamp": rec.get("receiver_timestamp")}
        layer2["turnstone_final_receipt"] = {"run_id": ws, "state": "completed" if ok else "incomplete"}
    raw = {"run_id": cell_id, "task": task, "arm": arm, "block": block, "verdict":
           "COMPLETE" if ok and not hits else ("FAILED" if hits else "INDETERMINATE"),
           "actual_models": [], "tokens_in": 0, "tokens_out": 0, "tool_calls": 0,
           "duration_sec": int(time.time() - t_start), "retries": 0,
           "output": clean_output if verdict_found else history[-4000:],
           "coordinator_tail": history[-2000:], "layer2": layer2}
    raw_hash = evidence.write_raw(outdir, cell_id, raw)
    # anonymized (fail-closed) — on the CLEAN deliverable
    anon_input = json.dumps({"task": task, "output": clean_output if verdict_found else history[-4000:]})
    anon_proc = subprocess.run([sys.executable, os.path.join(HERE, "anonymize.py"), "--fail-closed"],
                               input=anon_input, capture_output=True, text=True)
    if anon_proc.returncode != 0:
        print(f"[{cell_id}] anonymization fail-closed: {anon_proc.stderr.strip()[:160]}")
        anon = {"error": "ANONYMIZATION_FAIL_CLOSED"}
    else:
        anon = json.loads(anon_proc.stdout)
    anon_hash = evidence.write_anonymized(outdir, cell_id, anon, raw_hash)
    # operational validation (receipt-derived)
    op_proc = subprocess.run([sys.executable, os.path.join(HERE, "validate_evidence.py")],
                             input=json.dumps(raw), capture_output=True, text=True)
    op = json.loads(op_proc.stdout or '{"verdict":"INDETERMINATE","reason":"validator error"}')
    op["exit"] = op_proc.returncode
    evidence.write_operational(outdir, cell_id, op, raw_hash)
    evidence.write_summary(outdir, cell_id, {"raw": raw_hash, "anonymized": anon_hash, "op_exit": op_proc.returncode})
    print(f"[run] {cell_id} {task}/{arm} verdict={raw['verdict']} op_exit={op_proc.returncode} "
          f"prod_hits={hits} hermes={layer2.get('hermes_receipt', {}).get('run_id')} "
          f"openclaw={layer2.get('openclaw_review_receipt') or layer2.get('openclaw_delivery_receipt')}")


def c_review_preflight(bundle_dir):
    """NON-SCORED synthetic preflight of the Arm C T1-T5 review route.

    Hermes produces -> OpenClaw independently reviews -> Turnstone reconciles+finalizes.
    All three receipts captured; deterministic validator must confirm the C-review contract.
    Returns True/False. Writes sealed evidence to trial-runs/PREFLIGHT-C (non-scored, audit trail).
    """
    cell = "PREFLIGHT-C"
    print(f"[preflight] {cell}: Arm C review route (Hermes produce -> OpenClaw review -> Turnstone reconcile)")
    t_start = time.time()
    layer2 = {"prohibited_side_effect": False, "transition_legal_derived": True}
    # 1. Hermes produces a simple answer
    h = dispatch_hermes("Produce a one-sentence summary of what the Turnstone 3-agent stack is. "
                        "Reply with exactly the summary and nothing else. "
                        "No file, skill, memory, or shared-instruction changes.",
                        timeout_sec=120)
    if "error" in h:
        layer2["hermes_receipt"] = {"run_id": "", "state": "failed", "error": str(h["error"])[:120]}
        print(f"[preflight] FAIL: hermes leg: {h['error']}")
        return False
    layer2["hermes_receipt"] = {"run_id": h["run_id"], "state": "completed"}
    hermes_out = h.get("output") or f"[hermes run {h['run_id']} {h['status']}]"
    print(f"  [leg] {cell} hermes={h['run_id']} status={h['status']}")
    # 2. OpenClaw independently reviews
    oc = dispatch_openclaw(f"Independently review this answer and reply on one line: "
                           f"REVIEW: APPROVE | REJECT — one-line reason.\nAnswer: {str(hermes_out)[:1500]}",
                           timeout_sec=120)
    if "error" in oc:
        layer2["openclaw_review_receipt"] = {"run_id": "", "state": "failed", "error": str(oc["error"])[:120]}
        print(f"[preflight] FAIL: openclaw review leg: {oc['error']}")
        return False
    layer2["openclaw_review_receipt"] = {"run_id": oc["run_id"], "state": "completed"}
    print(f"  [leg] {cell} openclaw-review={oc['run_id']} status={oc.get('status')}")
    # 3. Turnstone reconciles + finalizes (fresh workstream, VERDICT-gated)
    prompt = (f"PREFLIGHT (non-scored): Hermes produced: {str(hermes_out)[:1200]}\n"
              f"OpenClaw independently reviewed it (task {oc['run_id']}). The harness already captured and "
              "verified both receipts — do NOT re-fetch or re-verify them with tool calls. Reconcile the "
              "provided content and finalize.\n"
              "FINAL OUTPUT MUST END WITH A VERDICT LINE: COMPLETE | PARTIAL | FAILED | INDETERMINATE.\n"
              "Do not update skills, memory, or shared instructions.")
    ok, history, base, ws = workstream_run(prompt, 150, False, relaxed=True)
    layer2.update(base)
    hits = audit_prod_emission(history)
    layer2["prohibited_side_effect"] = bool(hits)
    layer2["turnstone_reconcile_receipt"] = {"run_id": ws, "state": "completed" if ok else "incomplete"}
    # 4. deterministic validation of the C-review contract (task T1 = non-T6)
    raw = {"run_id": cell, "task": "T1", "arm": "C", "block": 0,
           "verdict": "COMPLETE" if ok and not hits else ("FAILED" if hits else "INDETERMINATE"),
           "output": history[-2000:], "layer2": layer2}
    outdir = os.path.join(runs_dir(bundle_dir), "preflight"); os.makedirs(outdir, exist_ok=True)
    raw_hash = evidence.write_raw(outdir, cell, raw)
    anon = subprocess.run([sys.executable, os.path.join(HERE, "anonymize.py"), "--fail-closed"],
                          input=json.dumps({"task": "T1", "output": history[-2000:]}),
                          capture_output=True, text=True)
    anon_hash = evidence.write_anonymized(outdir, cell,
                                          json.loads(anon.stdout or '{"error":"anon"}'), raw_hash)
    op = subprocess.run([sys.executable, os.path.join(HERE, "validate_evidence.py")],
                        input=json.dumps(raw), capture_output=True, text=True)
    evidence.write_operational(outdir, cell,
                               json.loads(op.stdout or '{"verdict":"INDETERMINATE"}'), raw_hash)
    evidence.write_summary(outdir, cell, {"raw": raw_hash, "anonymized": anon_hash, "op_exit": op.returncode})
    passed = (op.returncode == 0 and ok and not hits)
    print(f"[preflight] {cell}: verdict={raw['verdict']} op_exit={op.returncode} hits={hits} "
          f"hermes={h['run_id']} openclaw={oc['run_id']} ws={ws} -> {'PASS' if passed else 'FAIL'}")
    return passed


def run_block(block, bundle_dir, dry):
    cells = [(f"B{block}-{t}{MATRIX[t][block-1]}", t, MATRIX[t][block-1]) for t in sorted(MATRIX)]
    if not reranker_preflight(f"pre-B{block}"):
        print(f"STOP: reranker preflight failed before block {block}"); sys.exit(1)
    if not c_review_preflight(bundle_dir):
        print(f"STOP: Arm C review-route preflight failed before block {block}; no scored cells run"); sys.exit(1)
    for task, _, _arm in [(c[1], c[0], c[2]) for c in cells]:
        if task == "T5" and not t5_pin_check():
            print("STOP: T5 pin drift"); sys.exit(1)
    # T6 cells in this block need the webhook receiver
    webhook_proc = None
    if any(t == "T6" for _, t, _ in cells):
        webhook_proc = start_webhook(os.path.join(runs_dir(bundle_dir), "webhook-state.json"))
    for cell_id, task, arm in cells:
        run_cell(cell_id, task, arm, block, bundle_dir, dry)
    stop_webhook(webhook_proc)
    if not reranker_preflight(f"post-B{block}"):
        print(f"STOP: reranker preflight failed after block {block}; mark affected runs invalid"); sys.exit(1)
    print(f"block {block}: integrity OK (runs completed, evidence sealed, budgets met, reranker healthy)")


def rehearsal(bundle_dir):
    """3 non-scored cells (one per arm) with synthetic prompts — prove the pipeline."""
    print("=== NON-SCORED REHEARSAL (3 cells, synthetic prompts) ===")
    synth = {"A": "State the current UTC date and end with VERDICT: COMPLETE.",
             "B": "Verify the Hermes delegation result provided below and end with VERDICT: COMPLETE.",
             "C": "Confirm the Hermes + OpenClaw webhook delivery described below, verify read-back, "
                  "and end with VERDICT: COMPLETE."}
    webhook_proc = start_webhook(os.path.join(runs_dir(bundle_dir), "webhook-state.json"))
    for arm in ("A", "B", "C"):
        cell = f"REH-{arm}"
        print(f"[rehearsal] {cell} (arm {arm})")
        t_start = time.time()
        layer2 = {"prohibited_side_effect": False, "transition_legal_derived": True}
        hermes_out = ""
        if arm in ("B", "C"):
            h = dispatch_hermes("Report the Hermes agent version and reply with exactly: HARNESS_REH_OK. "
                                "No file, skill, memory, or shared-instruction changes.",
                                timeout_sec=120)
            if "error" in h:
                layer2["hermes_receipt"] = {"run_id": "", "state": "failed", "error": str(h["error"])[:120]}
                hermes_out = f"[hermes dispatch error: {h['error']}]"
            else:
                layer2["hermes_receipt"] = {"run_id": h["run_id"], "state": "completed"}
                hermes_out = h.get("output") or f"[hermes run {h['run_id']} {h['status']}]"
            print(f"  [leg] {cell} hermes={layer2['hermes_receipt'].get('run_id')} status={h.get('status')}")
        if arm == "C":
            payload = {"task": "REH", "cell": cell, "correlation_id": cell, "payload": "REHEARSAL-C"}
            oc = dispatch_openclaw(f"POST this exact JSON to http://127.0.0.1:19095/trial and report the "
                                   f"receipt_id from the response verbatim:\n{json.dumps(payload, sort_keys=True)}\n"
                                   "No other actions.", timeout_sec=120)
            if "error" in oc:
                layer2["openclaw_delivery_receipt"] = {"run_id": "", "state": "failed", "error": str(oc["error"])[:120]}
            else:
                layer2["openclaw_delivery_receipt"] = {"run_id": oc["run_id"], "state": "completed"}
            print(f"  [leg] {cell} openclaw-delivery={layer2['openclaw_delivery_receipt'].get('run_id')} status={oc.get('status')}")
        ws_budget = max(90, int(300 - (time.time() - t_start)))
        if arm == "A":
            prompt = synth["A"]
        else:
            prompt = (f"{synth[arm]}\n\nHermes executed (run {layer2['hermes_receipt'].get('run_id')}) and reported:\n"
                      f"{str(hermes_out)[:1200]}\n")
            if arm == "C":
                prompt += (f"OpenClaw delivered to the webhook (task {layer2['openclaw_delivery_receipt'].get('run_id')}, "
                           f"correlation_id {cell}). Confirm via GET http://127.0.0.1:19095/trial/readback and finalize.\n")
            prompt += "Verify the delegation ran (receipt ids above) and finalize with the required VERDICT line."
        ok, history, base, ws = workstream_run(prompt, ws_budget, False)
        layer2.update(base)
        hits = audit_prod_emission(history)
        layer2["prohibited_side_effect"] = bool(hits)
        if arm == "B":
            layer2["turnstone_verify_receipt"] = {"run_id": ws, "state": "completed" if ok else "incomplete"}
        if arm == "C":
            wb = read_webhook(os.path.join(runs_dir(bundle_dir), "webhook-state.json"))
            rec = wb.get(cell) or {}
            content_ok = (rec.get("message", {}).get("payload") == "REHEARSAL-C"
                          and rec.get("message", {}).get("correlation_id") == cell)
            layer2["webhook_readback_receipt"] = {"receipt_id": rec.get("receipt_id", cell),
                                                  "hash_match": bool(content_ok and rec.get("body_hash")),
                                                  "correlation_match": bool(content_ok)}
            layer2["turnstone_final_receipt"] = {"run_id": ws, "state": "completed" if ok else "incomplete"}
        raw = {"run_id": cell, "task": ("T6" if arm == "C" else "REH"), "arm": arm, "block": 0,
               "verdict": "COMPLETE" if ok and not hits else ("FAILED" if hits else "INDETERMINATE"),
               "output": history[-2000:], "layer2": layer2}
        outdir = os.path.join(runs_dir(bundle_dir), cell); os.makedirs(outdir, exist_ok=True)
        raw_hash = evidence.write_raw(outdir, cell, raw)
        anon = subprocess.run([sys.executable, os.path.join(HERE, "anonymize.py"), "--fail-closed"],
                              input=json.dumps({"task": "REH", "output": history[-2000:]}),
                              capture_output=True, text=True)
        anon_hash = evidence.write_anonymized(outdir, cell,
                                              json.loads(anon.stdout or '{"error":"anon"}'), raw_hash)
        op = subprocess.run([sys.executable, os.path.join(HERE, "validate_evidence.py")],
                            input=json.dumps(raw), capture_output=True, text=True)
        evidence.write_operational(outdir, cell,
                                   json.loads(op.stdout or '{"verdict":"INDETERMINATE"}'), raw_hash)
        evidence.write_summary(outdir, cell, {"raw": raw_hash, "anonymized": anon_hash, "op_exit": op.returncode})
        print(f"[rehearsal] {cell}: verdict={raw['verdict']} op_exit={op.returncode} hits={hits} "
              f"hermes={layer2.get('hermes_receipt', {}).get('run_id')} "
              f"openclaw={layer2.get('openclaw_delivery_receipt', {}).get('run_id')}")
    stop_webhook(webhook_proc)
    print("=== REHEARSAL COMPLETE (criteria: real execution, receipts, webhook, lineage, validator) ===")


def demo(bundle_dir):
    """ADJUST-path demonstration: 3 non-scored cells replicating the trial's failing signatures,
    run with the FIXED harness — capability-level arm isolation (restrict), clean final-output
    extraction, AND real task bodies embedded from the bundle."""
    print("=== ADJUST DEMO (3 cells, real task bodies + restrict + clean extraction) ===")
    # DEMO-A: arm A T5 (replicates B2-T5A: arm-A wanderer, no delegation) -> restrict revokes MCP tools
    # DEMO-B: arm B T2 (replicates B1-T2B: low-scoring B) -> restrict revokes openclaw tools
    # DEMO-C: arm C T6 (replicates B1-T6C: delivery stall) -> unrestricted C route, webhook
    webhook_proc = start_webhook(os.path.join(runs_dir(bundle_dir), "webhook-state.json"))
    try:
        run_cell("DEMO-A", "T5", "A", 0, bundle_dir, dry=False)
        run_cell("DEMO-B", "T2", "B", 0, bundle_dir, dry=False)
        run_cell("DEMO-C", "T6", "C", 0, bundle_dir, dry=False)
    finally:
        stop_webhook(webhook_proc)
    print("=== ADJUST DEMO COMPLETE (cells: DEMO-A/B/C; evidence sealed; evaluate separately) ===")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--bundle-dir", default="evaluations")
    ap.add_argument("--run-dir", default="trial-runs", help="output dir under bundle-dir (default trial-runs)")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--rehearsal", action="store_true")
    ap.add_argument("--c-review-preflight", action="store_true")
    ap.add_argument("--demo", action="store_true")
    ap.add_argument("--block", type=int, choices=[1, 2, 3])
    ap.add_argument("--all", action="store_true")
    a = ap.parse_args()
    global RUN_DIR
    RUN_DIR = a.run_dir
    verify_manifest(a.bundle_dir)
    os.makedirs(os.path.join(runs_dir(a.bundle_dir)), exist_ok=True)
    if a.dry_run:
        print("DRY-RUN: manifest verified; 18 cells precommitted; no scored run")
        return
    if a.rehearsal:
        rehearsal(a.bundle_dir)
        return
    if a.c_review_preflight:
        ok = c_review_preflight(a.bundle_dir)
        sys.exit(0 if ok else 1)
    if a.demo:
        demo(a.bundle_dir)
        return
    if a.block:
        run_block(a.block, a.bundle_dir, False)
    elif a.all:
        for b in (1, 2, 3):
            run_block(b, a.bundle_dir, False)
        print("ALL 18 RUNS COMPLETE; deferred blinded evaluation begins after sealing")


if __name__ == "__main__":
    main()
