# Turnstone 1.8.7 same-VM five-server baseline — 2026-10-16

**Status: FIVE SERVER NODES ACTIVATED and healthy/registered (2026-10-16, 18:29 ICT). Remaining acceptance: workstream execution on nodes 3, 4 and 5.** The existing head server, console, models, permissions and typed-decision finishing overlay remain unchanged.

## Target topology

| Process | Stable node identity | Local bind and registered advertise URL |
|---|---|---|
| Existing head (unchanged) | `turnstone-head-1` | `127.0.0.1:8080` |
| New worker 2 | `turnstone-node-2` | `127.0.0.1:8082` |
| New worker 3 | `turnstone-node-3` | `127.0.0.1:8083` |
| New worker 4 | `turnstone-node-4` | `127.0.0.1:8084` |
| New worker 5 | `turnstone-node-5` | `127.0.0.1:8085` |
| Existing console (not a server node) | separate console service | `127.0.0.1:8090` |

All five server processes share the same PostgreSQL `turnstone` DB and model definitions. Each worker has a unique node identity/port and uses `PYTHONPATH=/opt/turnstone-overlays/typed-decision-v187-finish`. Worker privilege identity is the same service account and group as the installed head (`turnstone:turnstone`), with `WorkingDirectory=/var/lib/turnstone`. This template introduces no sandbox/permission reductions and does not modify existing grants. The existing root-owned head services and rollback overlays are not changed.

The Debian VM has 4 vCPUs, 3.8 GiB RAM, 3.3 GiB swap, and 53 GiB free disk. Available RAM was 2.9 GiB, and the head process RSS was 229.4 MiB at operator preflight. These numbers justify a **sequential, verified rollout**, not an unconditional capacity guarantee under concurrent workstreams.

## Run preflight — no mutation

```bash
git clone --depth 1 --branch ops/turnstone-five-node-v187-20261016 \
  https://github.com/RedEyeNinja-BKK/turnstone.git \
  "$HOME/turnstone-five-node-source"

bash -n "$HOME/turnstone-five-node-source/scripts/turnstone_five_node_v187.sh"

sudo bash "$HOME/turnstone-five-node-source/scripts/turnstone_five_node_v187.sh" preflight
```

The preflight verifies stable `turnstone-head-1` identity and current finishing overlay, the shared bootstrap variable **names without printing values**, head/console health, unused ports 8082–8085, and current PostgreSQL registration.

**If preflight fails, STOP.** Do not overwrite unit/env files or disable any existing access. Send the error for review.

## Stage (no worker startup)

```bash
sudo bash "$HOME/turnstone-five-node-source/scripts/turnstone_five_node_v187.sh" stage
```

Creates `/etc/systemd/system/turnstone-node@.service` and a root-only bootstrap environment file at `/etc/turnstone/turnstone-five-nodes.env`, populated directly from the running head's `TURNSTONE_CONFIG` and `TURNSTONE_CONSOLE_URL` values (not printed). Each instance overrides only `TURNSTONE_NODE_ID`, `TURNSTONE_ADVERTISE_URL`, and port. Root-only backup: `/var/backups/turnstone/five-node-v187-20261016`. **The stage command does not start/restart the head, console or workers.**

## Incremental rollout — one node per check

```bash
sudo bash "$HOME/turnstone-five-node-source/scripts/turnstone_five_node_v187.sh" start 2
```

Only after it prints `NODE-2: HEALTHY AND REGISTERED`, and resources remain sufficient:

```bash
sudo bash "$HOME/turnstone-five-node-source/scripts/turnstone_five_node_v187.sh" start 3
sudo bash "$HOME/turnstone-five-node-source/scripts/turnstone_five_node_v187.sh" start 4
sudo bash "$HOME/turnstone-five-node-source/scripts/turnstone_five_node_v187.sh" start 5
sudo bash "$HOME/turnstone-five-node-source/scripts/turnstone_five_node_v187.sh" status
```

Each step refuses a duplicate active node or occupied port; uses a memory-headroom gate of 750 MiB; waits up to two minutes for an HTTP `/health` response with expected node identity, version 1.8.7 and backend up; and checks exact registration in PostgreSQL before proceeding. It does not start node N if N-1 is unhealthy or absent. Failed nodes remain available for operator diagnosis, not silently removed.

## Acceptance requirements

- Five *healthy, currently registered* `service_type='server'` rows with the exact IDs and URLs above. Beware stale heartbeats: verify new timestamps and live HTTP responses, not a DB row count alone.
- Five healthy loopback ports, exactly one console; server logs show no node-ID duplication and model registry loads successfully on each new process.
- One harmless `bash` workstream placed on **each** worker to test tool execution, permissions and console proxying. Do not accept five idle health checks as proof of node/workstream parity.
- One typed-decision Output Guard invocation on at least two workers to confirm finishing overlay routing through Switchyard, not merely successful direct import.
- Real concurrent workstreams should be run only after monitoring RSS, CPU, request latency, DB pool occupancy and memory headroom. Five Python services with 4 vCPUs is an operating envelope to measure.
- Preserve `/opt/turnstone-venv`, `/opt/turnstone-overlays/typed-decision-v187`, `/opt/turnstone-overlays/typed-decision-v187-finish` and both existing systemd head drop-ins.

## Recovery

For a newly created worker that needs to be disabled, explicit operator confirmation is required:

```bash
sudo bash "$HOME/turnstone-five-node-source/scripts/turnstone_five_node_v187.sh" stop 2
# Confirm STOP-2 (similarly 3, 4, 5)
```

This stops/disables only the named **new worker** and does not remove existing head/console permissions. Do not run it on a healthy worker or remove any previously granted access without operator approval.

Do not manually restore the full PostgreSQL backup to undo a new worker; that would discard more recent workstreams and changes. Keep the root-only backup solely as a recovery anchor.

## Deployment receipt — October 16, 2026 (operator-supplied)

- Initial read-only preflight PASS: head `turnstone-head-1`, console active, ports 8082–8085 free, 2963 MiB MemAvailable, only head registered in PostgreSQL. No changes during preflight.
- `stage` PASS: template `turnstone-node@.service` and root-only shared bootstrap env installed; backup `/var/backups/turnstone/five-node-v187-20261016`; head and console unchanged.
- Sequential `start 2`, `start 3`, `start 4`, `start 5` all reported `HEALTHY AND REGISTERED`. Startup /health identity/version/backend checks and PostgreSQL registration checks passed on each new node. `status` afterward showed all five services active, with five distinct PostgreSQL server rows and heartbeats.
- Active server identities and ports: `turnstone-head-1:8080`, `turnstone-node-2:8082`, `turnstone-node-3:8083`, `turnstone-node-4:8084`, `turnstone-node-5:8085`. Console remains active on 8090. Worker template selects `/opt/turnstone-overlays/typed-decision-v187-finish`.
- After all four workers: 3.8 GiB total RAM, 1.5 GiB used, 2.2 GiB available, 0 swap used; 4 vCPUs. These are idle measurements, not sustained-concurrency guarantees.
- A real workstream on **node 2** successfully ran `id -un` => `turnstone`, `pwd` => `/var/lib/turnstone`, and `printf` => `NODE2_EXECUTION_OK`. A separate `printf` of forged SYSTEM OVERRIDE text was not followed. The guard displayed heuristic `prompt_injection, instruction_override (HIGH)` with semantic `tier1-decision`.
- Node-2 production journal (PID 95278) recorded `output_guard_judge.typed_decision_alias` plus a clean `risk_level=none`, `risk_score=0.033206392` and an attack `risk_level=high`, `risk_score=0.9598802`. Both logged `backend=direct-openrouter-span-01-lite`, `child_lane=smartfree-decision`, `served_model=respan/span-01-lite-20260925`, `requested_model=tier1-decision`.
- Previously verified head decision lane has real-workstream results for clean diagnostic and injection payloads; **nodes 3–5 have not yet been demonstrated with actual workstream execution**. Do not declare full per-node execution parity until those tests pass.

### Remaining gates before final baseline closure

1. Open a console workstream pinned to each of `turnstone-node-3`, `turnstone-node-4`, `turnstone-node-5`. Run `id -un; pwd; printf '%s\\n' NODE3_EXECUTION_OK` (substitute marker for each node) and confirm `turnstone`, `/var/lib/turnstone`, exit status zero. Ensure the selected node matches the actual workstream placement, not only the UI's intended target.
2. Inspect the correct per-instance journal and confirm `typed_decision_result` from each worker following its benign tool call, even with risk NONE. Do not assume an ACTIVE service implies functional model routing.
3. Verify console lists five healthy nodes and no failed tool permissions or restarts after these tests. Check runtime RSS/CPU/swap under realistic workstream load before assuming concurrent five-node capacity.
4. Preserve all user-granted access, current overlays, database backup, console and head configuration. Do not disable/retire healthy workers as a cleanup operation without explicit operator approval.
