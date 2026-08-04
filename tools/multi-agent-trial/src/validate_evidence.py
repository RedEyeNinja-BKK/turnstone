#!/usr/bin/env python3
"""validate_evidence.py — deterministic route validation from CONCRETE RECEIPTS (bundled artifact).

Derives transition legality from task/arm-specific receipts — never a trusted boolean.

Route contracts:
  Arm A            : turnstone_receipt {run_id, state=completed}
  Arm B            : hermes_receipt {run_id, state=completed} + turnstone_verify_receipt
  Arm C (T1-T5)    : hermes_receipt + openclaw_review_receipt + turnstone_reconcile_receipt
  Arm C (T6)       : hermes_receipt + openclaw_delivery_receipt + webhook_readback_receipt
                     + turnstone_final_receipt

Missing required receipt => INDETERMINATE (exit 3), never inferred PASS.
prohibited_side_effect must be explicitly false (else INDETERMINATE/FAIL).
"""
import json, sys


def _r(l2, key):
    v = l2.get(key)
    if not isinstance(v, dict):
        return False
    if v.get("receipt_id"):           # receipt-style (e.g. webhook read-back)
        return True
    if v.get("state") in ("completed", "COMPLETE"):
        return True
    return bool(v.get("run_id") and v.get("status") in ("completed", "COMPLETE"))


def route_legal(arm, task, l2):
    """Return (legal, missing). legal: True | False | None(unknown) | 'PROHIBITED'."""
    if not isinstance(l2.get("prohibited_side_effect"), bool):
        return None, ["prohibited_side_effect not explicit"]
    if l2["prohibited_side_effect"]:
        return "PROHIBITED", []
    if arm == "A":
        need = ["turnstone_receipt"]
        ok = all(_r(l2, k) for k in need)
        return (ok, [k for k in need if not _r(l2, k)])
    if arm == "B":
        need = ["hermes_receipt", "turnstone_verify_receipt"]
        ok = all(_r(l2, k) for k in need)
        return (ok, [k for k in need if not _r(l2, k)])
    if arm == "C":
        if task == "T6":
            need = ["hermes_receipt", "openclaw_delivery_receipt", "webhook_readback_receipt",
                    "turnstone_final_receipt"]
        else:
            need = ["hermes_receipt", "openclaw_review_receipt", "turnstone_reconcile_receipt"]
        missing = [k for k in need if not _r(l2, k)]
        return (not missing, missing)
    return None, [f"unknown arm {arm}"]


def main():
    e = json.load(open(sys.argv[1])) if len(sys.argv) > 1 else json.load(sys.stdin)
    arm, task = e.get("arm", "?"), e.get("task", "?")
    l2 = e.get("layer2", {})
    legal, missing = route_legal(arm, task, l2)
    if legal is None:
        print(json.dumps({"verdict": "INDETERMINATE", "reason": ", ".join(missing)})); sys.exit(3)
    if legal == "PROHIBITED":
        print(json.dumps({"verdict": "FAIL", "reason": "prohibited side effect"})); sys.exit(1)
    if not legal:
        print(json.dumps({"verdict": "INDETERMINATE", "reason": "missing/absent receipts: " + ", ".join(missing)}))
        sys.exit(3)
    # T6 webhook read-back must include hash+correlation match
    if arm == "C" and task == "T6":
        rb = l2.get("webhook_readback_receipt", {})
        if rb.get("hash_match") is not True or rb.get("correlation_match") is not True:
            print(json.dumps({"verdict": "INDETERMINATE", "reason": "webhook read-back hash/correlation not matched"}))
            sys.exit(3)
    print(json.dumps({"verdict": "PASS", "route_legal": True, "receipts": list(l2.keys())}))
    sys.exit(0)


if __name__ == "__main__":
    main()
