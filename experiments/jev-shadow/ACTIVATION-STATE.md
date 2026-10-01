# ACTIVATION STATE - shadow route staged, restart pending operator GO

## What is true right now

```text
routes.toml on disk : 918bac0b5bca6d4ea64fdf868606e5bb883da476f55ba508de07befeca8792a7
  (was c16930a10f10ee39612d1862c7149a8b96542701d18ad99f9a7b3577c0507188)
Switchyard process  : PID 3101732, still running the PREVIOUS config
live /v1/models     : 51 models, jev NOT present
health              : {"status":"ok"}
```

The config change is **staged on disk but NOT live**. Nothing about the running
router has changed. This is deliberate.

## Why it is not live yet

Switchyard has no hot-reload surface:

```text
/admin/reload      -> 404
/reload            -> 404
/v1/admin/reload   -> 404
/-/reload          -> 404
SIGHUP handler     : 0 occurrences in the binary
"reload" strings   : 0 occurrences in the binary
```

Activating the route requires a **service restart**. That is a production-affecting
action on the routing authority, it is not required by the shadow harnesses yet,
and the service is currently live-serving traffic (18 completed requests in the
last 2 minutes). Restarting it is therefore held for an explicit operator GO
rather than done unilaterally.

## The change is a pure insertion

```diff
+[llm_clients.openrouter-typesafe-jev-router]   format=openai_chat, target=..., timeout=120
+[targets.openrouter-typesafe-jev-router]       provider=openrouter, model=typesafe/jev-router
+[routes.switchyard-jev-router-shadow-turnstone] type=passthrough, shadow_only=true
```

```text
lines removed or changed : 0
routes      45 -> 46
targets     25 -> 26
llm_clients 25 -> 26
capabilities 6 -> 6     (UNTOUCHED)
capability_clients 5 -> 5 (UNTOUCHED)
```

## Authority boundary, proven pre-activation

```text
shadow route referenced by any other entry : NONE
any fallback_target pointing at the shadow : NONE
AUX chain intact  : smart-aux = Span R1 -> smartlocal-aux (Laya)
                    smartfree-aux = Span only
                    smartlocal-aux = Laya only
capabilities table: unchanged, 6 entries
```

## Rollback

```bash
cp /home/vincent/.local/lib/localclaw-switchyard/routes.toml.pre-jev-20261001-152042 \
   /home/vincent/.local/lib/localclaw-switchyard/routes.toml
```

Restores sha256 `c16930a10f10ee39612d1862c7149a8b96542701d18ad99f9a7b3577c0507188`.
