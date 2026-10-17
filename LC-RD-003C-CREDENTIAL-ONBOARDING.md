# LC-RD-003C — Credential onboarding gate, 2026-10-16

## Closure carried forward
- `/srv/turnstone/credentials` is the sole active new Turnstone credential authority. Source and destination path migration, owner/modes, SSH fingerprints and manifest path checks passed.
- `/srv/localclaw` and its credential child are absent; no active systemd or Turnstone configuration references remained at final audit. The superseded user-home staging script was interactively deleted by the operator.
- New client keys are minted but not yet remotely authorized: agent-vm `SHA256:tprbJhQJiYZDEfBfu9OPdaJTEZbXnmfcXK+bYTa499w`; switchyard-vm `SHA256:xzfw8ginB2oAh+f8wSEgeJi5Ei0ZR+yGMpg7U0Pyj4c`.
- New VM model DB has five rows: configured Direct DeepSeek, OpenRouter GLM, ThaiLLM Typhoon keys; unauthenticated Switchyard smart-bounded and typed tier1-decision. Preserve all working keys and aliases.
- Existing full sudo for `turnstone-mgmt` on agent-vm remains intentionally untouched; legacy authorized key unchanged pending separate explicit operator approval. switchyard-vm has no public-key entries in audited root/vincent authorized_keys; user retains their own password SSH.

## New scripts (staged in repository, NOT run in production)
- `scripts/turnstone_ssh_host_pin.sh`: read-only `preflight`, additive `pin`, and `status`; only tailnet hostname or IP. Checks new client store permissions and verifies Ed25519 SSH host fingerprints against operator's **independent** remote audits:
  - agent-vm server: `SHA256:MIPln7fkKZaaiCfPqOEX4+H0JBrzqlrNy+DszIKJ+Ak`
  - switchyard-vm server: `SHA256:DSLHWq01pNsbSfoKOs9LEV4/una186mLNEmCrtYHslQ`
  On match, pins under `/srv/turnstone/credentials/ssh/known_hosts` (root:turnstone 0640). Does not attempt remote authorization, mutate client keys, or trust TOFU.
- `scripts/turnstone_credential_add.sh`: root-only hidden-TTY prompt and confirmation for `grafana-api` and `turnstone-api`; refuses overwrite; stages as `root:root 0600` in already-created root-only category dirs. No tokens on command line, command history, stdout, GitHub, or manifest. **Staging does not authenticate to Grafana or Turnstone and does not configure the running processes.** `status` reports only presence/permissions.

## Run sequence

On turnstone-vm:
```bash
git -C "$HOME/turnstone-five-node-source" pull --ff-only
bash -n "$HOME/turnstone-five-node-source/scripts/turnstone_ssh_host_pin.sh"
bash -n "$HOME/turnstone-five-node-source/scripts/turnstone_credential_add.sh"
sudo bash "$HOME/turnstone-five-node-source/scripts/turnstone_credential_add.sh" preflight
sudo bash "$HOME/turnstone-five-node-source/scripts/turnstone_credential_add.sh" status

tailscale status | grep -Ei 'agent-vm|switchyard-vm'
```

After checking actual tailnet names/IPs, use `turnstone_ssh_host_pin.sh preflight DEST HOST`, inspect fingerprint PASS; then `pin DEST HOST`; `status DEST HOST`. The audited switchyard FQDN is `switchyard-vm.tailc8ef7b.ts.net`, but test connection first; do not assume agent-vm's Tailnet hostname.

When a new Grafana API service-account token is available to the operator:
```bash
sudo bash "$HOME/turnstone-five-node-source/scripts/turnstone_credential_add.sh" put grafana-api
```
It prompts twice, without echoing the value. Do not paste tokens into chat or a terminal command. For native Turnstone self API, first issue a NEW supported admin token with intended scopes and expiry, then stage with `put turnstone-api`.

Next: encrypted **off-VM** backup with operator-owned recovery authority; host-key pinning; additive new client-key authorization on agent-vm (keep legacy grants), and new full-management account on switchyard-vm (keep operator account). Test SSH as unprivileged Turnstone OS user using the pinned host keys and explicit `IdentityFile` then `sudo -n` and remote tool health before claiming access. Any new remote service account with `NOPASSWD:ALL` and full agent tool access is explicitly requested by the operator; do not silently reduce it.

Do not duplicate existing model keys in root-only files and call the work complete: pinned 1.8.7 lacks model secret references and the process cannot read root-only Grafana files until an approved runtime integration exists. No Switchyard route import, smart/tier reformation, dashboard redeploy, Codex OAuth, HTPC/comfyninja mutations or per-node service changes are authorized by these scripts.

## Runtime integration added, 2026-10-17

The operator approved an additive, exact-argv sudo grant so the Turnstone
service identity can use the existing Grafana credential from its own runtime,
without an operator shell. The credential remains root-owned and unreadable by
the service identity; the grant carries one literal rule per verb.

- `scripts/turnstone_grafana_runtime.py` — bounded read surface:
  authenticated identity, organization, permissions, health, datasources,
  folders, dashboard listing, dashboard detail, alerts, and one bounded
  Prometheus/Loki query. Read verbs issue GET only against a fixed allow-list;
  the query body is built from a validated request, and SQL datasource types
  are refused because read-only cannot be structurally guaranteed.
- `dashboard-write` — the single mutating entry point: create, update and
  delete one dashboard. Update requires the dashboard's current version, so a
  concurrent edit fails as a conflict instead of being overwritten; every
  mutation writes the current document to `/var/log/turnstone-grafana/backups/`
  first; delete is restricted to the `turnstone-` uid namespace; and a
  non-empty revision message is mandatory so Grafana's own history records why.
- `scripts/launch-grafana-readonly.sh` — accepts one verb from a closed set and
  execs the root-owned module. No daemon, no socket, no free-form argument.
- `scripts/turnstone-grafana-readonly.sudoers` — one literal rule per verb, no
  wildcards.
- `scripts/install_grafana_runtime.sh install` — idempotent install with
  backup-before-replace and a `visudo` syntax gate.

The existing self-management MCP bridge grant is unchanged. Installation is a
root step: the service identity cannot write `/etc/sudoers.d`. After installing,
`install_grafana_runtime.sh check` reports drift and `verify` exercises the
interface as `turnstone`.

