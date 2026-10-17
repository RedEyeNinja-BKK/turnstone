# MagicDNS on turnstone-vm — remediation record and readiness check

Status: **REPAIRED and verified 2026-10-17.** Configuration preserved.

## What was wrong

MagicDNS itself was healthy the whole time; the OS resolver never asked it.

| Measurement | Before | After |
|---|---|---|
| `dig @100.100.100.100 agent-vm.tailc8ef7b.ts.net` | 100.127.26.70 | 100.127.26.70 |
| `getent ahostsv4 agent-vm.tailc8ef7b.ts.net` | **fails** | 100.127.26.70 |
| `getent ahostsv4 switchyard-vm…` | 100.65.106.53 — **from `/etc/hosts`** | 100.65.106.53 — **from DNS** |
| `tailscale get accept-dns` | `false` | `true` |
| `/etc/resolv.conf` | hand-written `10.10.50.1`, `1.1.1.1` | tailscaled-managed `100.100.100.100` |
| `scripts/turnstone_magicdns_check.py` | `MAGICDNS_NOT_READY` (rc 2) | `MAGICDNS_READY` (rc 0) |

Two things kept this invisible:

1. **`/etc/hosts` masked it.** `100.65.106.53 switchyard-vm.tailc8ef7b.ts.net` made one
   peer resolve while the other did not, so the fault looked peer-specific. It was
   uniform. The entry has since been removed *after* DNS was proven.
2. **No DNS manager existed.** systemd-resolved, NetworkManager, dnsmasq, unbound and
   nscd were all inactive; `resolvectl`/`nmcli` absent; static ifupdown with no DHCP
   client and no `resolvconf`. Nothing overwrote `resolv.conf` — and nothing was going
   to repair it either.

Also worth recording: pointing `resolv.conf` at `100.100.100.100` *without* enabling
`accept-dns` breaks public DNS. With `accept-dns=false` the Tailscale resolver has no
upstreams and returns `SERVFAIL` for anything outside the tailnet. The supported
integration is `tailscale set --accept-dns=true`, which lets tailscaled capture the
existing upstreams and forward to them.

## The applied fix

```bash
sudo tailscale set --accept-dns=true
```

- Coordination server publishes no resolvers, so tailscaled captured the existing
  `10.10.50.1` / `1.1.1.1` as upstreams and forwards non-tailnet names to them.
- The preference is stored in `tailscaled.state` and re-applied on boot — it survives
  reboot without any file edit.
- Rollback: `sudo tailscale set --accept-dns=false` restores the saved system config.

## Preserve this configuration

- Do **not** hand-edit `/etc/resolv.conf` again; tailscaled owns it now, and a static
  file reintroduces exactly this failure at the next reboot.
- Do **not** re-add tailnet names to `/etc/hosts`. A static entry resolves on one host,
  hides a resolver fault from everyone else, and silently breaks SSH-by-name host-key
  checking expectations.
- If a future DNS manager is installed (systemd-resolved, NetworkManager), reconcile it
  with Tailscale's route-only handling rather than disabling `accept-dns`.

## Bounded readiness check

`scripts/turnstone_magicdns_check.py` — read-only, no privilege required, safe to run
from the service account. Run it before any SSH/remote-management task and after any
network change.

```bash
sudo runuser -u turnstone -- \
  python3 /var/lib/turnstone/github/turnstone/scripts/turnstone_magicdns_check.py
python3 /var/lib/turnstone/github/turnstone/scripts/turnstone_magicdns_check.py --json
```

It reports, per management peer: `magicdns` (Tailscale resolver, direct query),
`dns (os)` with its **source** (`DNS` or `HOSTS_FILE`), `tcp/22`, `hostkey` against
the pinned fingerprint, and `auth`. Exit status `0` ready, `2` not ready.

Deliberate design rules:

- A **`HOSTS_FILE`** answer is reported as `HOSTS_FILE`, never as healthy DNS.
- A reachable port and a matching host key never together imply DNS health.
- `auth` stays **`UNPROVEN`** unless `--probe-auth` is passed. A pinned key is not
  access, and the check must not imply otherwise.
- `magicdns=OK` with `dns=FAIL` is named as a resolver-integration gap, not a peer
  fault — that distinction is what this incident lacked.

## Operational invariant

If `MAGICDNS_NOT_READY` appears, treat it as a **blocking** condition for
remote-management work, and read the two DNS columns before assuming a peer problem:

| magicdns | dns (os) | Meaning | Repair |
|---|---|---|---|
| OK | OK `[DNS]` | healthy | — |
| OK | `HOSTS_FILE` | static workaround masking a resolver gap | integrate the resolver; remove the hosts entry |
| OK | FAIL | MagicDNS fine, OS bypasses it | `tailscale set --accept-dns=true` |
| FAIL | FAIL | tailnet DNS view broken | check tailnet DNS settings / tailscaled health |
