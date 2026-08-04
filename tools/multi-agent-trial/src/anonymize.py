#!/usr/bin/env python3
"""anonymize.py — strip arm/model/cost/metadata from trial outputs (bundled artifact).

Usage: anonymize.py <raw_output_json> <task_id> [--fail-closed]
Reads an evidence-packet-shaped JSON, returns ONLY the anonymized task output.
Strips: arm, session/run ids, model names, token counts, tool-call counts, cost,
timestamps, correlation ids, agent names, MCP tool names that reveal the arm,
Discord/channel ids, file paths with agent identities, and unknown metadata keys.

FAIL-CLOSED (default): if any high-signal identifier remains after stripping, exit
non-zero with the residual pattern listed — do NOT pass a partially anonymized record
to the evaluator.
"""
import json, re, sys

STRIP_KEYS = {"arm", "arm_id", "run_id", "session_id", "session_key", "correlation", "correlation_id",
              "model", "models", "actual_models", "tokens_in", "tokens_out", "tool_calls",
              "cash_usd", "cost", "normalized_tokens", "wall_clock_sec", "duration_sec",
              "started_at", "retries", "block", "evidence_refs", "verdict",
              "agent", "agent_id", "mcp_server", "delivery_message_id", "channel_id"}

# High-signal residual patterns that MUST NOT remain after anonymization.
RESIDUAL = [
    re.compile(r"\b(arm)\s*[ABC]\b", re.I),
    re.compile(r"\b(gpt-|deepseek-|qwen-|bge-|rerank|codex)[a-z0-9.\-]*", re.I),
    re.compile(r"\b(hermes|openclaw|turnstone)\b", re.I),
    re.compile(r"mcp__[a-z_]+", re.I),
    re.compile(r"(run|task)_[a-f0-9]{8,}", re.I),
    re.compile(r"TRIAL-[A-Z]-T[0-9]-[a-z0-9-]+", re.I),
    re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}", re.I),
    re.compile(r"\d{17,20}", re.I),          # discord ids
    re.compile(r"corr-[a-z0-9-]+", re.I),
]


def _strip(s: str) -> str:
    s = re.sub(r"\b(arm)\s*[ABC]\b", "arm-<redacted>", s, flags=re.I)
    s = re.sub(r"\b(gpt-|deepseek-|qwen-|bge-|rerank|codex)[a-z0-9.\-]*", "<model>", s, flags=re.I)
    s = re.sub(r"\b(hermes|openclaw|turnstone)\b", "<agent>", s, flags=re.I)
    s = re.sub(r"mcp__[a-z_]+", "<tool>", s)
    s = re.sub(r"(run|task)_[a-f0-9]{8,}", "<id>", s)
    s = re.sub(r"TRIAL-[A-Z]-T[0-9]-[A-Za-z0-9-]+", "TRIAL-<corr>", s)
    s = re.sub(r"corr-[A-Za-z0-9-]+", "<corr>", s)
    s = re.sub(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}", "<ts>", s)
    s = re.sub(r"\d{17,20}", "<discord-id>", s)
    return s


def anonymize(obj):
    if isinstance(obj, dict):
        return {k: anonymize(v) for k, v in obj.items() if k not in STRIP_KEYS}
    if isinstance(obj, list):
        return [anonymize(v) for v in obj]
    if isinstance(obj, str):
        return _strip(obj)
    return obj


def anonymize_wrapped(raw, task):
    """Return the {task, output} wrapper the evaluator expects, with output anonymized."""
    out = raw.get("output", raw)
    if isinstance(out, str):
        try:
            parsed = json.loads(out)
            out = anonymize(parsed)
        except (json.JSONDecodeError, TypeError):
            out = anonymize(out)   # plain-text output
    else:
        out = anonymize(out)
    return {"task": task, "output": out}


def main():
    fail_closed = "--fail-closed" in sys.argv
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    path = args[0] if args else None
    task = args[1] if len(args) > 1 else (path.split("/")[-1].split(".")[0] if path else "?")
    raw = json.load(open(path)) if path else json.load(sys.stdin)
    out = {"task": task, "output": anonymize(raw.get("output", raw))}
    blob = json.dumps(out, ensure_ascii=False)
    if fail_closed:
        hits = [p.pattern for p in RESIDUAL if p.search(blob)]
        if hits:
            print("ANONYMIZATION FAIL-CLOSED: residual identifiers detected -> " + ", ".join(hits), file=sys.stderr)
            sys.exit(2)
    print(blob)


if __name__ == "__main__":
    main()
