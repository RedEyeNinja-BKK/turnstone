# HTPC management lane - exact requirement (Option A)

## Status: NOT YET IN PLACE. The operator has authorised it; the key is not installed.

```text
target        : 100.105.20.36
account       : hermes-admin
probed        : 2026-10-01
result        : Permission denied (publickey,password)
ssh verbose   : Offering public key: id_ed25519_htpc ED25519
                SHA256:r6sGp3ow8l0xvo7YGcD5WqVUkFMXhz/NezltUd+OUS0 explicit
                Authentications that can continue: publickey,password
```

The key is offered and refused. Nothing further can proceed from this host until
it is accepted.

## What is needed

Add this **public** key to `hermes-admin`'s `authorized_keys` on the HTPC. No new
credential is required - this is the already-configured identity.

```bash
# run ON THE HTPC, as the hermes-admin account
mkdir -p ~/.ssh && chmod 700 ~/.ssh
echo 'ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAILzEH13iiARs2Ssqt3KZHHchP9QICGC0fPMnzse0Oye+ localclaw-htpc-mgmt' >> ~/.ssh/authorized_keys
chmod 600 ~/.ssh/authorized_keys
```

Verify the fingerprint after installing:

```text
SHA256:r6sGp3ow8l0xvo7YGcD5WqVUkFMXhz/NezltUd+OUS0
```

## Boundaries this lane will and will not have

Once installed, the deployment uses it for:

```text
read-only HTPC inventory (Laya unit, process, ports, resources)
place Jev-Style artifacts under a dedicated directory
create a dedicated service account and unit for Jev-Style on :8012
start/stop that unit
```

It will **not** be used to:

```text
modify Laya's model, unit, environment, port, credentials or restart state
change SSH configuration
repurpose the Laya API credential
touch RENINJA
modify Switchyard
```

Laya's decision API key remains a *service* credential, distinct from this
*management* credential. They are never interchanged.

## Intended topology

```text
localclaw / Hermes
    │  SSH management (this lane)
    ▼
HTPC
    ├── Laya       :8011   existing, untouched
    └── Jev-Style  :8012   new, isolated, CPU-first

decision clients
    └── authenticated HTTP only
```

## Why Option A rather than Option B

Option B (operator runs the install by hand) would produce a component that works
but cannot be inspected, upgraded, rolled back or repaired by the management
plane - the same class of problem the Turnstone source-sync pass just closed,
where the production runtime lived on an unreachable commit. A management lane is
the durable fix and also serves the next HTPC model experiment.
