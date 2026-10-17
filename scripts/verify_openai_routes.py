#!/usr/bin/env python3
"""Ordinary functional check for the three direct-OAuth registry aliases.

This is the same proportional check any new model gets: does the alias resolve,
does it answer, is the served model what the row claims, and does the
non-thinking route actually withhold reasoning.

It reads the LIVE rows through the self-management API (so it checks the
configuration that is actually stored, not a default), then drives Turnstone's
own dispatch seam -- registry config -> resolve_model_backend_auth_token ->
token store -> Responses provider adapter -- so it exercises the code that
serves real traffic rather than a bespoke HTTP client.

Read-only against the registry. One request per alias, plus one identity probe.
It does not create a workstream, modify a row, or change a setting.

Usage:
  python scripts/verify_openai_routes.py
  python scripts/verify_openai_routes.py gpt-6-luna gpt-6-luna-nt
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from turnstone.core.model_backend_auth import (  # noqa: E402
    DIRECT_OAUTH_RESOURCE,
    resolve_model_backend_auth_token,
)
from turnstone.core.model_registry import ModelConfig  # noqa: E402
from turnstone.core.providers import create_client, create_provider  # noqa: E402
from turnstone_self_api import call  # noqa: E402

EXPECT = {
    "gpt-6-luna": {"model": "gpt-6-luna", "thinking": "on", "tool": False},
    "gpt-6-luna-nt": {"model": "gpt-6-luna", "thinking": "off", "tool": False},
    "gpt-6.1-sol": {"model": "gpt-6.1-sol", "thinking": "on", "tool": True},
}

PROMPT_TOOL = "What is the weather in Bangkok? Use the get_weather tool."
PROMPT_PLAIN = "Reply with exactly: OK"


def _tool() -> dict:
    return {
        "type": "function",
        "function": {
            "name": "get_weather",
            "description": "Current weather for a city",
            "parameters": {
                "type": "object",
                "properties": {"city": {"type": "string"}},
                "required": ["city"],
            },
        },
    }


def _row_to_config(row: dict, alias: str) -> ModelConfig:
    caps_raw = row.get("capabilities") or "{}"
    if isinstance(caps_raw, str):
        try:
            caps = json.loads(caps_raw)
        except json.JSONDecodeError:
            caps = {}
    else:
        caps = dict(caps_raw)
    return ModelConfig(
        alias=alias,
        base_url=str(row.get("base_url") or ""),
        api_key="",  # deliberately not read: the lane must not use a static key
        model=str(row.get("model") or ""),
        context_window=int(row.get("context_window") or 0),
        provider=str(row.get("provider") or ""),
        capabilities=caps,
        auth_mode=str(row.get("auth_mode") or "static"),
        obo_audience=str(row.get("obo_audience") or ""),
        reasoning_effort=row.get("reasoning_effort"),
        surface_persisted_reasoning=bool(row.get("surface_persisted_reasoning", True)),
        replay_reasoning_to_model=bool(row.get("replay_reasoning_to_model", False)),
    )


def _served_model(token: str, model: str) -> str:
    """Ask the Responses API which model actually answered.

    The Turnstone adapter deliberately does not surface the payload's ``model``
    field, so identity is read here with the same credential. This is the check
    that catches a wrong-version mapping -- GPT-6 Sol answering where GPT-6.1 Sol
    was configured.
    """
    from openai import OpenAI

    client = OpenAI(base_url=DIRECT_OAUTH_RESOURCE, api_key=token)
    try:
        resp = client.responses.create(model=model, input=PROMPT_PLAIN, store=False)
    except Exception as exc:  # noqa: BLE001 - the failure text is the result
        return f"<probe failed: {type(exc).__name__}>"
    return str(getattr(resp, "model", "") or "<not reported>")


def check_alias(cfg: ModelConfig, alias: str) -> tuple[bool, list[str]]:
    spec = EXPECT[alias]
    notes: list[str] = []

    # 1. The row is bound the way the migration intends.
    wrong = []
    if cfg.provider != "openai":
        wrong.append(f"provider={cfg.provider!r} (want 'openai')")
    if cfg.auth_mode != "openai_oauth":
        wrong.append(f"auth_mode={cfg.auth_mode!r} (want 'openai_oauth')")
    if cfg.obo_audience != DIRECT_OAUTH_RESOURCE:
        wrong.append(f"resource={cfg.obo_audience!r}")
    if cfg.base_url != DIRECT_OAUTH_RESOURCE:
        wrong.append(f"base_url={cfg.base_url!r}")
    if cfg.model != spec["model"]:
        wrong.append(f"model={cfg.model!r} (want {spec['model']!r})")
    if wrong:
        return False, ["row not migrated: " + "; ".join(wrong)]

    # 2. The credential resolves through the DIRECT-OAUTH branch. A failure here
    #    is the honest result: it must never fall back to the stored static key.
    try:
        token = resolve_model_backend_auth_token(
            alias, cfg, principal_id="", config_store=None, mint_client=None
        )
    except Exception as exc:  # noqa: BLE001
        return False, [f"credential resolution failed: {type(exc).__name__}: {exc}"]
    if not token:
        return False, ["credential resolution returned no token (would use a static key)"]
    notes.append(f"credential: direct OAuth token acquired ({len(token)} chars, not printed)")

    # 3. One real request through Turnstone's provider adapter.
    provider = create_provider(cfg.provider)
    client = create_client(
        cfg.provider, base_url=cfg.base_url, api_key="", follow_redirects=False
    ).with_options(api_key=token)
    caps = provider.get_capabilities(cfg.model)
    chunks = list(
        provider.create_streaming(
            client=client,
            model=cfg.model,
            messages=[{"role": "user", "content": PROMPT_TOOL if spec["tool"] else PROMPT_PLAIN}],
            tools=[_tool()] if spec["tool"] else None,
            max_tokens=256,
            temperature=None,
            reasoning_effort=cfg.reasoning_effort or None,
            capabilities=caps,
        )
    )
    text = "".join(getattr(c, "content_delta", "") or "" for c in chunks)
    reasoning = "".join(getattr(c, "reasoning_delta", "") or "" for c in chunks)
    emitted_reasoning_blocks = any(
        b.get("type") in ("reasoning", "thinking")
        for c in chunks
        for b in (getattr(c, "provider_blocks", None) or [])
        if isinstance(b, dict)
    )
    tool_names = [d.name for c in chunks for d in (getattr(c, "tool_call_deltas", None) or [])]

    if spec["tool"]:
        if "get_weather" not in tool_names:
            return False, [f"no get_weather tool call (got {tool_names!r}); text={text[:120]!r}"]
        notes.append("streaming tool call: get_weather")
    elif not text.strip():
        return False, ["empty response"]

    # 4. The reasoning contract -- the part that cannot be faked.
    if spec["thinking"] == "off":
        if reasoning.strip() or emitted_reasoning_blocks:
            return False, [
                f"reasoning emitted on a non-thinking route "
                f"({len(reasoning)} chars delta, reasoning_blocks={emitted_reasoning_blocks})"
            ]
        notes.append("reasoning: none emitted (contract holds)")
    else:
        notes.append(
            "reasoning: emitted"
            if (reasoning.strip() or emitted_reasoning_blocks)
            else "reasoning: not present in this sample (thinking still enabled)"
        )

    # 5. Served-model identity.
    served = _served_model(token, cfg.model)
    if served.startswith("<"):
        return False, [f"identity probe inconclusive: {served}"]
    if served != cfg.model:
        return False, [f"served model is {served!r}, expected {cfg.model!r}"]
    notes.append(f"served_model={served}")
    return True, notes


async def _rows() -> dict[str, dict]:
    out = await call("GET", "/v1/api/admin/model-definitions")
    if out.get("status") != 200:
        raise SystemExit(f"listing model definitions failed: HTTP {out.get('status')}")
    body = out.get("body") or {}
    rows = body.get("models") if isinstance(body, dict) else body
    return {r["alias"]: r for r in rows if isinstance(r, dict) and r.get("alias") in EXPECT}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("alias", nargs="*")
    args = ap.parse_args()
    wanted = args.alias or list(EXPECT)

    unknown = [a for a in wanted if a not in EXPECT]
    if unknown:
        print(f"unknown alias(es): {unknown}", file=sys.stderr)
        return 2

    rows = asyncio.run(_rows())
    failed = 0
    for alias in wanted:
        print(f"\n== {alias} ==")
        row = rows.get(alias)
        if row is None:
            print("  FAIL: alias not present in the registry")
            failed += 1
            continue
        ok, notes = check_alias(_row_to_config(row, alias), alias)
        for n in notes:
            print(f"  {n}")
        print("  PASS" if ok else "  FAIL")
        if not ok:
            failed += 1

    print(f"\n{len(wanted) - failed}/{len(wanted)} passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
