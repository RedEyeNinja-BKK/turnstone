# LC-RD-007 — SIWC credential lifecycle design

Status: **DESIGN ONLY — nothing implemented, nothing migrated.** Submitted for operator review
before any code, per the standing production-change gate (`feedback_production_code_modification_gate`).

Scope: how the native OpenAI OAuth ("Sign In With ChatGPT" / token-sharing) credential should be
owned, refreshed and consumed so that **Console and the five nodes** can each obtain a valid access
token without racing each other, without a second authoritative copy, and without a credential ever
appearing in argv, the environment, a log, or a repository.

## 1. What exists today (measured 2026-10-17)

| Item | Value |
|---|---|
| Working store | `/var/lib/turnstone/github/siwc-creds` — dir `0700 turnstone:turnstone` |
| File | `credentials.json` `0600 turnstone:turnstone`, 3908 B |
| Fields | `client_id`, `access_token`, `refresh_token`, `id_token`, `token_type`, `expires_at`, `granted_scopes`, `sub`, `email`, `host_id`, `resource` |
| `host_id` | separate file, `urn:uuid:…` (stable `ext_agent_host_id`) |
| Sole consumer | `scripts/siwc_direct_oauth_probe.py` (`SIWC_STORE_DIR`, default that path) |
| Refresh today | implemented — `_valid_access_token()` refreshes when `expires_at - now <= 60`, persists the rotated `refresh_token` |
| Process identity | the probe runs as `turnstone`; so do `turnstone-server`, `turnstone-node@2..5` and `turnstone-console` |

Two structural facts drive the whole design:

1. **All six serving processes run as the same OS user (`turnstone`).** There is no privilege
   boundary between them, so "who owns the refresh token" is a *concurrency* question between
   cooperating peers, not an isolation question between privilege levels.
2. **The refresh token must be readable by whichever process performs inference**, because it is
   that process which exchanges it for an access token. Any design that makes it root-only therefore
   also has to move the inference call itself behind root.

## 2. Why an `openai-oauth` category alone is not sufficient

Adding a write path for `oauth/openai` would give us canonical storage and an inventory reference —
nothing more. The unsolved problems are all in *lifecycle*:

- **Refresh rotation race.** The provider issues a NEW `refresh_token` on each refresh and invalidates
  the old one. Six processes refreshing concurrently means five of them write back a token the
  provider has already retired; the account silently ends up revoked. This is the dominant failure
  mode and no amount of directory layout fixes it.
- **Crash safety.** A non-atomic write can leave a truncated `credentials.json`, destroying the only
  refresh token.
- **Renewal window.** A fixed 60 s margin is too tight for a long-running call that must stay valid
  for the duration of a streamed inference.
- **Revocation and identity.** `invalid_grant` must terminate retries and mark the store as needing
  re-authorization rather than looping; and `sub`/`email` must be compared on every refresh so an
  account switch is surfaced, never silently absorbed.

## 3. Options considered

### Option A — root credential broker (new privileged service or launcher)
A root-owned process holds the refresh token, performs refreshes, and hands out short-lived access
tokens over a unix socket / fd.

- Meets "single owner" and "root-only storage" cleanly.
- But: it is **new privileged machinery** (the user's explicit caution), it puts an always-on root
  listener in the trust path of every inference call, and it only actually protects the refresh token
  if the *broker* makes the provider call — i.e. all LLM traffic proxied through root. That is a
  far larger architectural change than the credential problem warrants.

### Option B — single shared store, exclusive lock, atomic replace *(recommended)*
One credential file, one lock, one atomic rename. All six processes cooperate through the lock
file; whichever holds the lock performs the single refresh and publishes the new token.

- Requires **no new privileged component** — it is ordinary code in the Turnstone package.
- Correct by construction for the rotation race: the lock serialises refresh, and the holder
  re-reads the store *after* acquiring the lock, so a peer that already refreshed is detected and
  its result reused rather than a second refresh being issued.
- Matches an **existing, accepted pattern in this same store**: `github/*.pat` is group-readable
  precisely because its consumer is unprivileged. The SIWC case is that pattern, not the
  `api/`/`grafana/` root-consumer pattern.

### Option C — per-node stores with staggered refresh
Rejected: N authoritative refresh tokens cannot exist (the provider invalidates on use), and
staggering only narrows the race window rather than closing it.

## 4. Recommended design (Option B)

**Storage.** `/srv/turnstone/credentials/oauth/openai/` — `0750 root:turnstone`, file
`credentials.json` `0640 root:turnstone`, plus `credentials.lock` and `host_id` `0640`. This mirrors
the accepted `github/` precedent and keeps the store root-owned while remaining readable by the
`turnstone` UID that must consume it. The existing `oauth/` directory mode (`0700 root:root`) is
correct for a root-consumer; this category deliberately differs for the same reason `github/` does.

**Consumption.** A provider-neutral helper, e.g. `turnstone/core/oauth_token_store.py`:

```
access_token(now) ->
    read store
    if expires_at - now > RENEWAL_MARGIN: return access_token        # fast path, no lock
    open <store>.lock, flock(LOCK_EX)                                # slow path
        re-read store           # a peer may have refreshed while we waited
        if expires_at - now > RENEWAL_MARGIN: unlock, return          # reuse peer's result
        refresh via POST /oauth/token (grant_type=refresh_token)
        on invalid_grant -> mark NEEDS_REAUTH, raise OAuthRevoked     # no retry loop
        verify sub/email unchanged, else raise OAuthIdentityChanged   # never silently absorb
        write credentials.json atomically (tmp + fsync + rename)
    unlock
    return access_token
```

**Renewal margin.** Proposed 300 s, comfortably longer than a normal streamed call, configurable.

**Atomic persistence.** `os.open(tmp, O_WRONLY|O_CREAT|O_EXCL, 0o600)` → `write` → `fsync` →
`os.replace()` → `fsync(dirfd)`. Crash-safe at every point; a torn write is impossible.

**Credential hygiene.** The token is read from a file into memory. It is never placed on argv
(`reject_credentialish_argv()` already guards this in the probe), never exported to the environment
(cf. `feedback_secrets_not_in_env`), never logged (the dispatcher's existing "root-only `ts_` token is
never returned to a model" discipline extends here), and never committed.

**Explicit inventory reference.** One `manifest.yaml` entry naming the path, the consumer
(`turnstone-server`, `turnstone-node@*`), and the refresh mode — a path reference, never a secret.

**Preservation until cutover.** `siwc-creds` stays exactly as it is. Cutover = point
`SIWC_STORE_DIR` at the new store, verify live inference, then retire the old path — a later,
separately approved step.

## 5. Why Option B meets "coordinated refresh across Console and the five nodes"

The invariant that matters is *at most one refresh per rotation*, and it is enforced by the
exclusive lock plus the re-read-after-lock. Every process — Console included — goes through the same
function against the same file, so:

- exactly one process refreshes; the other five block briefly, re-read, and reuse the result;
- no process can observe a half-written file (atomic replace);
- a crash mid-refresh leaves the previous token intact and valid;
- a revoked grant fails fast and loudly in all six, rather than six retry storms.

This is achievable with **no new privileged launcher and no second authoritative copy**, which is
the least machinery that satisfies every stated requirement.

## 6. Where the provider integration plugs in (LC-RD-007)

The native Responses integration needs a model-registry `auth_mode` that is not in today's set
(`{static, entra_obo, entra_app, rfc8693_obo}`). The narrow shape: a new dynamic mode whose
credential supplier calls `access_token()` above, so the existing "mint at call time" seam
(`has_dynamic_auth()`) is reused rather than a second credential path being invented. Token
lifecycle lives in the store helper; the provider layer only ever receives a ready access token.

## 7. Open decisions for the operator

1. Approve Option B (shared store + lock + atomic replace) over Option A (root broker).
2. Approve the `oauth/openai/` mode as `0750 root:turnstone` / `0640` (the `github/` precedent) —
   this is the one place the design touches a root-only category.
3. Approve the renewal margin (proposed 300 s).
4. Confirm the cutover sequence and whether `siwc-creds` is retained afterward as a rollback anchor.

Nothing in this document has been implemented.
