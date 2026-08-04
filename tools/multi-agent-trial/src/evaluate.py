#!/usr/bin/env python3
"""evaluate.py — blinded content scorer, PINNED (bundled artifact).

- Evaluator model + endpoint are PINNED (sealed constants). Environment overrides are REJECTED
  if they differ from the sealed values (and the resolved values are recorded).
- On evaluator infrastructure failure: returns EVALUATOR_INDETERMINATE with NO Layer-1 scores
  (retryable) — never a zero-quality score.
- Validates the returned schema: all keys present, numeric, 0..5 range, no malformed extras.
- Receives ONLY {task, anchors, output} (anonymized). Never arm/model/cost/operational data.
"""
import json, os, sys, urllib.request

SEALED_MODEL = "gpt-5.6-terra"
SEALED_CATALOG = "http://127.0.0.1:8645/v1/chat/completions"
PROMPT = (
    "You are scoring an anonymized agent output for a task. Score ONLY the content on three "
    "criteria, each 0-5, where 5 is best:\n"
    "1. Correctness: is the answer factually correct given the task inputs?\n"
    "2. Completeness: does it address every required element of the task?\n"
    "3. Usefulness/clarity: is it clear, well-structured, and actionable?\n"
    "Do NOT score efficiency, speed, or transition legality — those are scored separately by a "
    "deterministic process. Output ONLY JSON: "
    '{"correctness":N,"completeness":N,"usefulness":N,"notes":"<1-2 sentences>"}'
)
INDETERMINATE = {"state": "EVALUATOR_INDETERMINATE", "reason": "evaluator infrastructure failure"}


def _resolved():
    # reject overrides that differ from the sealed trial configuration
    m = os.environ.get("TRIAL_EVAL_MODEL")
    c = os.environ.get("TURNSTONE_CATALOG_URL")
    if m and m != SEALED_MODEL:
        raise SystemExit(f"FATAL: TRIAL_EVAL_MODEL override {m} != sealed {SEALED_MODEL}")
    if c and c != SEALED_CATALOG:
        raise SystemExit(f"FATAL: TURNSTONE_CATALOG_URL override {c} != sealed {SEALED_CATALOG}")
    return SEALED_MODEL, SEALED_CATALOG


def _validate(scores):
    if not isinstance(scores, dict):
        return None
    need = {"correctness", "completeness", "usefulness"}
    if not need.issubset(scores.keys()):
        return None
    extra = set(scores.keys()) - need - {"notes"}
    if extra:
        return None
    for k in need:
        v = scores[k]
        if not isinstance(v, (int, float)) or isinstance(v, bool) or not (0 <= v <= 5):
            return None
    return scores


def _call(model, catalog, messages):
    payload = {"model": model, "messages": messages, "temperature": 0}
    headers = {"Content-Type": "application/json"}
    token = os.environ.get("OPENAI_CATALOG_TOKEN")
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(catalog, data=json.dumps(payload).encode(),
                                 headers=headers, method="POST")
    with urllib.request.urlopen(req, timeout=120) as resp:
        return json.load(resp)


def main():
    model, catalog = _resolved()
    data = json.load(open(sys.argv[1])) if len(sys.argv) > 1 else json.load(sys.stdin)
    # evaluate.py consumes the anonymized wrapper; if the file is the evidence-wrapped
    # shape {raw_hash, anonymized:{task,output}}, unwrap it.
    if "anonymized" in data and isinstance(data["anonymized"], dict):
        data = data["anonymized"]
    user = json.dumps({"task": data.get("task"), "anchors": data.get("anchors", []),
                       "output": data.get("output", "")}, ensure_ascii=False)[:8000]
    messages = [{"role": "system", "content": PROMPT}, {"role": "user", "content": user}]
    for attempt in range(2):  # 1 retry on transport
        try:
            body = _call(model, catalog, messages)
            content = body["choices"][0]["message"]["content"]
            start, end = content.find("{"), content.rfind("}") + 1
            parsed = json.loads(content[start:end]) if start >= 0 and end > start else None
            scores = _validate(parsed)
            if scores is None:
                raise ValueError("invalid evaluator output")
            print(json.dumps({"state": "ok", "model": model, "scores": scores}, ensure_ascii=False))
            return
        except Exception:  # noqa: BLE001
            if attempt == 1:
                print(json.dumps(INDETERMINATE, ensure_ascii=False))
                return


if __name__ == "__main__":
    main()
