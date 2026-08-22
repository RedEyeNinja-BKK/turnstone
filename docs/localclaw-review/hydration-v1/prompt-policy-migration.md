# Prompt Policy Migration — Hydration v1

Nine prompt policies currently exist. **All nine are globally injected into every session** regardless of persona (verified in `compose_system_message`: DB policies are appended unconditionally). H2.1 decision: only genuinely **universal** rules stay globally injected; **domain** rules move behind their existing persona activation path.

The table below shows, for every policy, the required behavior, its authoritative destination, how/when the behavior becomes available after disablement, and the verification case. Domain policies are **not** disabled on semantic similarity alone — each destination already contains the policy's required behavior (verified against the persona catalog on 2026-08-22).

## Keep (universal — remain globally injected)

| Policy | Required behavior | Activation path | Verification |
|---|---|---|---|
| **protect-deployment-facts-yaml** | Never overwrite the deployment-facts YAML via `write_file`; use surgical `edit_file`; validate YAML parses after any edit | Global (unchanged) | Fresh-session prompt dump shows the rule; facts file intact after any session |
| **discovery-authority-separation** | DISCOVER → CLASSIFY → SELECT → AUTHORIZE → EXECUTE; capability ≠ authority; read-only limits what may be *executed*, not what may be *discovered* | Global (unchanged) | Fresh-session prompt dump shows the rule; battery case 1 (authority vs executor) PASS |
| **three-agent-orchestration-stance** | Turnstone owns intent/coordination/supervision/acceptance; choose lane by fit; never curl other agents' ports — MCP gateways only | Global (unchanged) | Fresh-session prompt dump shows the rule; battery case 1 + case 3 PASS |

## Disable (domain — behavior already lives in persona/skill)

| Policy | Required behavior | Authoritative destination | Activation path (after disable) | Verification |
|---|---|---|---|---|
| **hermes-manager-prompt-policy** | Hermes MCP-only direct surface; operator gates for updates/restarts/config/approvals; evidence states; never bypass | `hermes-manager` persona (already embeds this content) | `hermes-manager` persona session active | hermes-manager session exhibits the rules; a non-Hermes session does **not** carry them |
| **openclaw-manager-prompt-policy** | OpenClaw LOCAL/REMOTE via MCP only; one command at a time; operator gates | `openclaw-manager` persona (already embeds) | `openclaw-manager` persona session active | openclaw-manager session exhibits the rules |
| **truenas-manager-prompt-policy** | TrueNAS read-only; operator-gated writes; update-hang history; key path only | `truenas-manager` persona (already embeds) | `truenas-manager` persona session active | truenas-manager session exhibits the rules |
| **proxmox-manager-prompt-policy** | Proxmox read-before-act; pfs-pair hazard; operator gates for power/kernel/firewall | `proxmox-manager` persona (already embeds) | `proxmox-manager` persona session active | proxmox-manager session exhibits the rules |
| **ChummyThailand Etsy OBM** | Etsy analysis/drafting scope; privacy; no Etsy mutations | `etsy-store-manager` persona (already embeds) | `etsy-store-manager` persona session active | etsy session exhibits the rules |
| **process-engine-context** | PE pipeline; operator-only gate; nothing ships untried | `process-engine` / `process-engine-generator` personas + PE skills (already embed) | PE persona/skills session active | PE session exhibits the rules |

## Expected token effect

- Policies injected/session: 9 (~9.3KB, ~2.3K tokens) → **3 (~1.4KB, ~0.35K tokens)**.
- **~1.9K tokens/session saved.**

## Notes

- No policy is deleted; disablement is reversible (re-enable via admin API, byte-identical).
- Domain behavior availability is preserved through the **existing** persona activation path — no new mechanism.
- Universal rules stay unconditional because they govern every session regardless of domain (facts-file integrity, discovery/authority separation, 3-agent orchestration).
