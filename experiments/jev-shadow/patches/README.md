# Shadow-only Switchyard route - retained as an artifact, NOT live configuration

## Status

```text
IN PRODUCTION CONFIG : NO  (deliberately removed 2026-10-01)
ON-DISK routes.toml  : sha256 c16930a10f10... - matches the running process exactly
RUNNING SWITCHYARD   : PID 3101732, never restarted, NRestarts=0
```

## Why it is not in routes.toml

The route was staged on disk while the hosted Jev Router was being probed. Leaving
it there would have meant an **unrelated future Switchyard restart silently
activating a shadow-only route** into the production routing table - a latent
activation nobody would be watching for at that moment.

Operator ruling 2026-10-01: hosted Jev stays a direct shadow oracle and is
**disqualified from authoritative or advisory-in-the-loop routing** until
candidate governance and effort observability exist. A disqualified component must
not sit latent in cold-start configuration.

## What it contains

`jev-router-shadow-route.patch.toml` - the exact candidate that was staged:

- `routes.switchyard-jev-router-shadow-turnstone` (passthrough, shadow_only)
- `targets.openrouter-typesafe-jev-router`
- `llm_clients.openrouter-typesafe-jev-router`

It was a **pure insertion**: 0 lines removed or changed, routes 45->46, targets
25->26, llm_clients 25->26, `capabilities` 6->6 and `capability_clients` 5->5
untouched.

## Why it is not needed to run the harness

The harness calls OpenRouter **directly**. It never used Switchyard. Re-adding
this route would buy credential indirection and telemetry that the harness does
not currently require, at the cost of a live surface with no consumer.

## If it is ever revived

Revival requires the two disqualifying findings to be resolved first, and a
separate operator GO for a Switchyard restart:

1. candidate controls must actually bind (`models`/`exclude`/`provider.order`
   currently do not), and
2. selected reasoning effort must be observable (currently never returned).
