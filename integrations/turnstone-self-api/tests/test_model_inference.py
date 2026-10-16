#!/usr/bin/env python3
"""LC-RD-004 — real inference test through Turnstone's own model stack.

Not a config inspection: this loads the *production* registry
(``load_model_registry(storage=...)``, the same call ``turnstone-server`` makes
at startup), resolves each alias through ``ModelRegistry.resolve`` /
``get_provider`` (so the stored ``server_compat.api_surface``, capabilities,
base_url and credential are all applied exactly as a live session applies them),
then issues a real streaming completion and drains it.

That exercises: DB row -> ModelConfig -> capability parse -> provider factory
(``api_surface``) -> network inference -> StreamChunk normalization.

Usage:
    python3 test_model_inference.py ALIAS [ALIAS ...]
    python3 test_model_inference.py --reasoning-check ALIAS   # asserts no reasoning channel
"""

from __future__ import annotations

import argparse
import json
import sys
import time

CONFIG_PATH = "/etc/turnstone/config.toml"


def build_registry():
    from turnstone.core.config import load_config
    from turnstone.core.model_registry import load_model_registry
    from turnstone.core.storage import get_storage, init_storage, is_storage_initialized

    cfg = load_config()
    if not is_storage_initialized():
        db = cfg.get("database", {}) or {}
        backend = str(db.get("backend", "") or "").strip()
        url = str(db.get("url", "") or "").strip()
        if not backend:
            raise RuntimeError(
                f"no [database] backend configured in {CONFIG_PATH}; refusing the "
                "SQLite auto-init default, which would build an EMPTY registry"
            )
        # run_migrations=False / create_tables=False: this is a strictly
        # read-only open. The schema is already owned by the running cluster.
        init_storage(backend, url=url, run_migrations=False, create_tables=False)
    storage = get_storage()
    return load_model_registry(storage=storage)


def probe(registry, alias: str, prompt: str, max_tokens: int = 4096) -> dict:
    from turnstone.core.providers import drain_stream

    out: dict = {"alias": alias}
    t0 = time.time()
    try:
        client, model_name, cfg, _gen = registry.resolve(alias)
    except Exception as exc:
        return {**out, "ok": False, "stage": "resolve", "error": f"{type(exc).__name__}: {exc}"}
    out["model"] = model_name
    out["provider"] = cfg.provider
    out["base_url"] = cfg.base_url
    out["api_surface"] = (cfg.server_compat or {}).get("api_surface")
    out["capabilities"] = cfg.capabilities

    try:
        provider = registry.get_provider(alias)
    except Exception as exc:
        return {**out, "ok": False, "stage": "provider", "error": f"{type(exc).__name__}: {exc}"}

    try:
        chunks = provider.create_streaming(
            client=client,
            model=model_name,
            messages=[{"role": "user", "content": prompt}],
            max_tokens=max_tokens,
        )
        result = drain_stream(chunks)
    except Exception as exc:
        return {
            **out,
            "ok": False,
            "stage": "inference",
            "error": f"{type(exc).__name__}: {exc}"[:400],
            "elapsed_s": round(time.time() - t0, 2),
        }

    # CompletionResult.content -- NOT .text (a prior revision of this harness read
    # a non-existent attribute and silently reported empty content for EVERY
    # model, including the pre-existing ones; the control run caught it).
    text = getattr(result, "content", "") or ""
    reasoning = getattr(result, "reasoning", "") or ""
    usage = getattr(result, "usage", None)
    tool_calls = getattr(result, "tool_calls", None)
    finish_reason = getattr(result, "finish_reason", None)
    incomplete_reason = getattr(result, "incomplete_reason", None)
    out.update(
        ok=True,
        content=text.strip()[:200],
        content_len=len(text),
        reasoning_present=bool(reasoning),
        reasoning_head=(reasoning[:120] if reasoning else ""),
        finish_reason=finish_reason,
        incomplete_reason=incomplete_reason,
        tool_calls=len(tool_calls) if tool_calls else 0,
        usage=(usage if isinstance(usage, dict) else str(usage)) if usage else None,
        elapsed_s=round(time.time() - t0, 2),
    )
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("aliases", nargs="+")
    ap.add_argument("--prompt", default="Reply with exactly the single word: pong")
    ap.add_argument("--max-tokens", type=int, default=4096)
    ap.add_argument(
        "--reasoning-check",
        action="store_true",
        help="fail (exit 1) if any alias emits a reasoning channel",
    )
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    try:
        registry = build_registry()
    except Exception as exc:
        print(f"FATAL: could not build registry: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2

    results = []
    for alias in args.aliases:
        r = probe(registry, alias, args.prompt, args.max_tokens)
        results.append(r)

    if args.json:
        print(json.dumps(results, indent=2, default=str))
    else:
        for r in results:
            print(f"\n=== {r['alias']} ===")
            print(f"  model        {r.get('model')}   provider={r.get('provider')}  surface={r.get('api_surface')}")
            if not r.get("ok"):
                print(f"  ** FAILED at {r.get('stage')}: {r.get('error')}")
                continue
            print(f"  HTTP/infer   OK in {r['elapsed_s']}s")
            print(f"  content      {r['content']!r}")
            print(f"  reasoning    present={r['reasoning_present']}" + (f" head={r['reasoning_head']!r}" if r["reasoning_present"] else ""))
            print(f"  tool_calls   {r['tool_calls']}   usage={r['usage']}")

    failed = [r for r in results if not r.get("ok")]
    if args.reasoning_check:
        violators = [r["alias"] for r in results if r.get("ok") and r["reasoning_present"]]
        if violators:
            print(f"\nREASONING CHECK FAILED: {violators} emitted a reasoning channel", file=sys.stderr)
            return 1
        if failed:
            print(f"\nREASONING CHECK INCONCLUSIVE: {[r['alias'] for r in failed]} failed inference", file=sys.stderr)
            return 2
        print("\nREASONING CHECK PASSED: no alias emitted reasoning")
        return 0
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
