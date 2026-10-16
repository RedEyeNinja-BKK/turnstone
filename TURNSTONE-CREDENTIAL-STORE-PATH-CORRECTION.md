# Turnstone credential store path correction — 2026-10-16

## Authority decision

The clean deployment uses **`/srv/turnstone/credentials`** as its sole *new-runtime* credential store. The original staging script mistakenly used an older stack prefix. Do not propagate that prefix into any new service unit, environment file, manifest, application configuration, automation, or operational instruction.

The old-prefixed directory is used **only as a migration source in** `scripts/turnstone_credential_store_rename.sh`. No compatibility symlink should be created.

## Operator-proven current stage

Before migration, two fresh client Ed25519 keys were minted once on `turnstone-vm`. They are NOT YET remotely authorized.

| Identity | Verified SHA-256 public-key fingerprint |
|---|---|
| `turnstone-agent-vm-ed25519` | `SHA256:tprbJhQJiYZDEfBfu9OPdaJTEZbXK+bYTa499w` |
| `turnstone-switchyard-vm-ed25519` | `SHA256:xzfw8ginB2oAh+f8wSEgeJi5Ei0ZR+yGMpg7U0Pyj4c` |

Original store, SSH directories mode 0750 `root:turnstone`, private key files mode 0600 `turnstone:turnstone`. Neither remote host's `authorized_keys` or sudoers were altered by staging. Three original provider API keys remain in PostgreSQL.

## Fix in place; do NOT run fresh-install stage

On `turnstone-vm`:

```bash
git -C "$HOME/turnstone-five-node-source" pull --ff-only
bash -n "$HOME/turnstone-five-node-source/scripts/turnstone_credential_store_rename.sh"
sudo bash "$HOME/turnstone-five-node-source/scripts/turnstone_credential_store_rename.sh" preflight
sudo bash "$HOME/turnstone-five-node-source/scripts/turnstone_credential_store_rename.sh" migrate
sudo bash "$HOME/turnstone-five-node-source/scripts/turnstone_credential_store_rename.sh" status
```

**Preflight fails closed** if the already-minted private key identity, fingerprints, key ownership, manifest paths, source parent contents, or destination absence do not match the operator report. It refuses a populated destination. Migration moves the existing store on the same filesystem, updates the two non-secret manifest path references using an atomic file rename, preserves key bytes, prints the same fingerprints, and removes the old parent with `rmdir` only when empty. No new keys, sudo grants, remote authorization, or service changes.

The fresh-install template `scripts/turnstone_credential_store_stage.sh` uses the corrected directory but **must not be executed on a VM whose credential store is already staged/migrated**. After migration, use only its `status` command if desired.

## Required after-migration validation

- New store exists as root:turnstone 0750 under /srv/turnstone; private key identities remain owned turnstone:turnstone 0600.
- Manifest has exactly two `key_path` entries under `/srv/turnstone/credentials/ssh/keys`.
- Public-key fingerprints exactly equal the two operator-verified values above.
- Legacy directory is absent; no compatibility symlink and no duplicate store.
- `runuser -u turnstone -- ssh-keygen -y -f` works for both private keys.
- No Turnstone service restart, API-key rotation, or remote SSH authorization occurred.

**Pending**: encrypted off-VM backup, remote SSH host-key pinning, explicit additive grants to agent-vm and switchyard-vm, and credential-store integration into Turnstone API/model configuration. No remote authorization should be inferred from a staged key alone.

## Live post-migration cleanliness audit — 2026-10-16

Operator reported the following verified results on `turnstone-vm`:

- `/srv/turnstone` and `/srv/turnstone/credentials`: **root:turnstone, 0750**.
- Old incorrectly named store and parent: **absent**.
- `grep -RIlF` over `/etc/systemd/system` and `/etc/turnstone`: **no references to the old path**.
- `realpath /srv/turnstone/credentials`: resolves to itself; no compatibility symlink.
- The staged key fingerprints remain the two previously documented values; migration/status passed.
- **One obsolete script remains:** `/home/vincent/LC-RD-003B-stage-credential-store.sh`. Operator was given a guarded, interactive deletion command (`rm -i`). **Its deletion has not yet been confirmed**.

**Result:** Turnstone credential-store path and active-configuration cleanup **PASS**. Housekeeping of the obsolete user-home script remains pending operator confirmation. Legacy SSH authorization still present on `agent-vm` is a separate management-access concern; do not retire it without explicit operator approval. No server/service/model configuration change was made by this audit.
